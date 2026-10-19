"""SQL work ownership. Every queue transition and its event share a transaction."""
from contextlib import contextmanager
from psycopg.types.json import Jsonb


class WorkQueueRepository:
    @contextmanager
    def transaction(self):
        outer = not getattr(self, '_transaction_depth', 0)
        self._transaction_depth = getattr(self, '_transaction_depth', 0) + 1
        try:
            yield self
            if outer:
                self.connection.commit()
        except BaseException:
            if outer:
                self.connection.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def _queue_lock(self):
        self._execute('SELECT pg_advisory_xact_lock(748214, 7)')

    def _queue_event(self, action, job=None, *, worker_id=None, preset_id=None, source_id=None, reason=''):
        job = job or {}
        self._execute('''INSERT INTO queue_events
          (occurred_at,action,job_id,kind,worker_id,preset_id,source_id,retries,reason)
          VALUES (clock_timestamp(),%s,%s,%s,%s,%s,%s,%s,%s)''',
          (action, job.get('id'), job.get('kind'), worker_id or job.get('worker_id'),
           preset_id if preset_id is not None else job.get('preset_id'),
           source_id if source_id is not None else job.get('source_id'), job.get('retries'), reason[:2000]))
        self._execute('''DELETE FROM queue_events WHERE id <=
          (SELECT id FROM queue_events ORDER BY id DESC OFFSET 5000 LIMIT 1)''')

    def queue_enqueue(self, kind, dedup_key, payload, priority=2, source_id=None, preset_id=None):
        with self.transaction():
            self._queue_lock()
            old = self._execute('SELECT * FROM work_queue WHERE dedup_key=%s FOR UPDATE', (dedup_key,)).fetchone()
            if old and old['payload'] == payload and old['kind'] == kind and old['target_preset_id'] == preset_id and old['source_id'] == source_id:
                return dict(old)
            row = self._execute('''INSERT INTO work_queue(kind,dedup_key,payload,priority,source_id,target_preset_id)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(dedup_key) DO UPDATE SET
              kind=EXCLUDED.kind,payload=EXCLUDED.payload,
              source_id=EXCLUDED.source_id,target_preset_id=EXCLUDED.target_preset_id,
              revision=work_queue.revision+1 RETURNING *''',
              (kind,dedup_key,Jsonb(payload),priority,source_id,preset_id)).fetchone()
            self._queue_event('queued', row, reason='Updated pending work' if old else '')
            return dict(row)

    def queue_claim(self, worker_id, preset_id, allowed_types, source_id=None, embedding_model=None):
        if not allowed_types:
            return None
        with self.transaction():
            self._queue_lock()
            model_join = ''
            model_filter = ''
            values = [list(allowed_types)]
            if embedding_model is not None:
                model_join = 'CROSS JOIN unnest(%s::text[]) AS models(model)'
                model_filter = "AND COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','') = models.model"
                values.append(['',embedding_model])
            # Pick only the first row in each allowed kind/model/target lane;
            # unrelated queue kinds never enter this indexed lookup.
            lane = """SELECT id,priority FROM work_queue WHERE state='new'
              AND kind=allowed.kind {target_filter} {model_filter}
              AND (%s::bigint IS NULL OR source_id=%s)
              ORDER BY priority,id LIMIT 1"""
            unbound = lane.format(target_filter='AND target_preset_id IS NULL',model_filter=model_filter)
            targeted = lane.format(target_filter='AND target_preset_id=%s',model_filter=model_filter)
            query = """SELECT q.* FROM work_queue q WHERE q.id=(
              SELECT candidate.id FROM unnest(%s::text[]) AS allowed(kind) """ + model_join + """
              CROSS JOIN LATERAL ((""" + unbound + """) UNION ALL (""" + targeted + """)) candidate
              ORDER BY candidate.priority,candidate.id LIMIT 1) FOR UPDATE OF q"""
            values.extend((source_id,source_id,preset_id,source_id,source_id))
            row = self._execute(query,tuple(values)).fetchone()
            if not row:
                return None
            row = self._execute('''UPDATE work_queue SET state='processing',worker_id=%s,preset_id=%s,
              claimed_revision=revision WHERE id=%s RETURNING *''', (worker_id,preset_id,row['id'])).fetchone()
            self._queue_event('accepted',row)
            return dict(row)

    def _queue_owned(self, job):
        row = self._execute('SELECT * FROM work_queue WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
        if not row or row['state'] != 'processing' or row['worker_id'] != job['worker_id'] or row['claimed_revision'] != job['claimed_revision']:
            return None
        return row

    def queue_is_owned(self, job):
        with self.transaction():
            self._queue_lock()
            return self._queue_owned(job) is not None

    def queue_complete(self, job, apply=None):
        with self.transaction():
            self._queue_lock()
            row = self._queue_owned(job)
            if not row:
                return False
            if row['revision'] != job['claimed_revision']:
                self._queue_return(row, 'Input changed while processing')
                return False
            job['priority'] = row['priority']
            self._execute('DELETE FROM work_queue WHERE id=%s',(job['id'],))
            if apply:
                apply()
            self._queue_event('completed',row)
            return True

    def _queue_return(self,row,reason):
        updated = self._execute('''UPDATE work_queue SET state='new',retries=retries+1,
          worker_id=NULL,preset_id=NULL,claimed_revision=NULL WHERE id=%s RETURNING *''',(row['id'],)).fetchone()
        self._queue_event('requeued', dict(updated), worker_id=row.get('worker_id'), preset_id=row.get('preset_id'),reason=reason)

    def queue_requeue(self,job,reason=''):
        with self.transaction():
            self._queue_lock()
            row = self._queue_owned(job)
            if row:
                self._queue_return(row,reason)
                return True
            return False

    def queue_interrupt_presets(self, reason):
        with self.transaction():
            self._queue_lock()
            rows = self._execute("""UPDATE work_queue SET revision=revision+1
              WHERE state='processing' AND preset_id IS NOT NULL RETURNING *""").fetchall()
            for row in rows:
                self._queue_return(row, reason)
            return len(rows)

    def queue_recover_processing(self):
        with self.transaction():
            self._queue_lock()
            rows = self._execute("SELECT * FROM work_queue WHERE state='processing' ORDER BY id FOR UPDATE").fetchall()
            for row in rows:
                self._queue_return(row,'Application startup recovery')
            workers = self._execute("SELECT * FROM queue_workers WHERE state <> 'stopped' FOR UPDATE").fetchall()
            for worker in workers:
                self.queue_set_worker(worker['worker_id'], 'stopped', worker['preset_id'], worker['source_id'], 'Application startup recovery')
            return len(rows)

    def queue_cancel(self,dedup_key,reason=''):
        return self._queue_cancel('dedup_key=%s',(dedup_key,),reason)

    def queue_cancel_prefix(self,prefix,reason=''):
        return self._queue_cancel('starts_with(dedup_key,%s)',(prefix,),reason)

    def _queue_cancel(self,where,params,reason):
        with self.transaction():
            self._queue_lock()
            rows = self._execute('DELETE FROM work_queue WHERE '+where+' RETURNING *',params).fetchall()
            for row in rows:
                self._queue_event('cancelled',row,reason=reason)
            return len(rows)

    def queue_update_priority(self, job_id, priority):
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            raise ValueError('Priority must be a nonnegative integer')
        with self.transaction():
            self._queue_lock()
            row = self._execute('SELECT * FROM work_queue WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if not row:
                return None
            if row['priority'] != priority:
                old_priority = row['priority']
                row = self._execute('UPDATE work_queue SET priority=%s WHERE id=%s RETURNING *',
                                    (priority, job_id)).fetchone()
                self._queue_event('reprioritized', row, reason=f'Priority {old_priority} → {priority}')
            return self._queue_control_result(row)

    def queue_pause(self, job_id):
        return self._queue_set_job_state(job_id, 'paused')

    def queue_resume(self, job_id):
        return self._queue_set_job_state(job_id, 'new')

    def _queue_set_job_state(self, job_id, state):
        with self.transaction():
            self._queue_lock()
            row = self._execute('SELECT * FROM work_queue WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if not row:
                return None
            if row['state'] != state and (state == 'paused' or row['state'] == 'paused'):
                previous = dict(row)
                row = self._execute("""UPDATE work_queue SET state=%s, worker_id=NULL,
                  preset_id=NULL, claimed_revision=NULL, revision=revision+1
                  WHERE id=%s RETURNING *""", (state, job_id)).fetchone()
                self._queue_event('paused' if state == 'paused' else 'resumed', row,
                                  worker_id=previous['worker_id'], preset_id=previous['preset_id'])
            return self._queue_control_result(row)

    def queue_delete(self, job_id):
        with self.transaction():
            self._queue_lock()
            row = self._execute('DELETE FROM work_queue WHERE id=%s RETURNING *', (job_id,)).fetchone()
            if not row:
                return None
            self._queue_event('deleted', row, reason='Deleted by user')
            return dict(row)

    @staticmethod
    def _queue_control_result(row):
        return {key: value for key, value in row.items() if key != 'payload'}

    def queue_list(self, limit=100, offset=0, state=None, filters=None, q=None, kind=None):
        if state is not None and state not in ('new', 'processing', 'paused'):
            raise ValueError('Invalid job state')
        limit, offset = max(1, min(int(limit), 5000)), max(0, int(offset))
        conditions, values = (['state=%s'], [state]) if state else ([], [])
        for key, value in (filters or {}).items():
            if value is None or value == '':
                continue
            if key == 'job_id':
                conditions.append('id=%s')
                values.append(value)
            elif key in ('worker_id', 'source_id'):
                conditions.append(key + '=%s')
                values.append(value)
            elif key == 'preset_id':
                conditions.append('(preset_id=%s OR target_preset_id=%s)')
                values.extend((value, value))
        if kind:
            conditions.append('kind=%s')
            values.append(kind)
        # Queue payloads contain identifiers, not guaranteed foreign keys. Invalid
        # or deleted references must leave a usable row in Troubleshooting.
        def payload_id(key):
            return f"CASE WHEN payload->>'{key}' ~ '^[0-9]{{1,18}}$' THEN (payload->>'{key}')::bigint END"
        message_id, summary_id, search_id, topic_id = [payload_id(key) for key in
            ('message_id', 'summary_id', 'search_id', 'topic_id')]
        query = str(q or '').strip()[:200]
        needle = ''
        if query:
            pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            needle = "WITH needle AS (SELECT %s::text AS pattern, websearch_to_tsquery('simple', %s) AS terms), "
            values = [pattern, query] + values
            conditions.append(f"""(
              concat_ws(' ',id::text,kind,dedup_key,worker_id,
                payload->>'mailbox_name',payload->>'summary_type',payload->>'interval_start_date',
                payload->>'embedding_model') ILIKE (SELECT pattern FROM needle)
              OR {message_id} IN (SELECT id FROM messages WHERE search_document @@ (SELECT terms FROM needle)
                UNION SELECT id FROM messages WHERE subject ILIKE (SELECT pattern FROM needle)
                OR sender_name ILIKE (SELECT pattern FROM needle) OR sender_address ILIKE (SELECT pattern FROM needle))
              OR {summary_id} IN (SELECT s.id FROM mailbox_interval_summaries s JOIN mailboxes b ON b.id=s.mailbox_id
                WHERE concat_ws(' ',b.name,s.summary_type,s.interval_start_date::text) ILIKE (SELECT pattern FROM needle))
              OR {search_id} IN (SELECT job_id FROM smart_search_jobs WHERE question ILIKE (SELECT pattern FROM needle))
              OR {topic_id} IN (SELECT id FROM topic_clusters WHERE name ILIKE (SELECT pattern FROM needle))
              OR source_id IN (SELECT id FROM mail_accounts WHERE name ILIKE (SELECT pattern FROM needle))
              OR COALESCE(preset_id,target_preset_id) IN (SELECT id FROM llm_presets WHERE name ILIKE (SELECT pattern FROM needle))
            )""")
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        # Search/count before pagination; expensive excerpts only join the page.
        row = self._execute((needle or 'WITH ') + """selected AS NOT MATERIALIZED (
          SELECT * FROM work_queue""" + where + f"""
        ), page AS (
          SELECT q.id,q.kind,q.dedup_key,q.priority,q.state,q.retries,q.worker_id,q.preset_id,
            q.target_preset_id,q.source_id,q.created_at,p.name AS preset_name,a.name AS source_name,
            left(COALESCE(
              CASE WHEN m.id IS NOT NULL THEN concat_ws(E'\n',NULLIF(m.subject,''),
                NULLIF(concat_ws(' ',m.sender_name,m.sender_address),''),
                left(COALESCE(NULLIF(m.cleaned_text,''),ms.summary_text,''),350)) END,
              CASE WHEN s.id IS NOT NULL THEN concat_ws(' · ',b.name,s.summary_type,
                s.interval_start_date::text,s.period_days::text || ' days') END,
              sj.question,topic.name,CASE WHEN q.kind='preset_check' THEN p.name END,
              NULLIF(concat_ws(' · ',q.payload->>'mailbox_name',
                q.payload->>'summary_type',q.payload->>'interval_start_date'),''),q.dedup_key),350) AS preview
          FROM (SELECT * FROM selected ORDER BY priority,id LIMIT %s OFFSET %s) q
          LEFT JOIN llm_presets p ON p.id=COALESCE(q.preset_id,q.target_preset_id)
          LEFT JOIN mail_accounts a ON a.id=q.source_id
          LEFT JOIN messages m ON m.id={message_id.replace('payload', 'q.payload')}
          LEFT JOIN message_summaries ms ON ms.message_id=m.id
          LEFT JOIN mailbox_interval_summaries s ON s.id={summary_id.replace('payload', 'q.payload')}
          LEFT JOIN mailboxes b ON b.id=s.mailbox_id
          LEFT JOIN smart_search_jobs sj ON sj.job_id={search_id.replace('payload', 'q.payload')}
          LEFT JOIN topic_clusters topic ON topic.id={topic_id.replace('payload', 'q.payload')}
        ) SELECT (SELECT count(*) FROM selected) AS total,
          COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY priority,id) FROM page),'[]'::jsonb) AS jobs,
          COALESCE((SELECT jsonb_agg(kind ORDER BY kind) FROM (SELECT DISTINCT kind FROM work_queue) kinds),'[]'::jsonb) AS kinds""",
          tuple(values + [limit, offset])).fetchone()
        return {'jobs': row['jobs'], 'total': row['total'], 'kinds': row['kinds'], 'limit': limit, 'offset': offset}

    def queue_set_worker(self,worker_id,state,preset_id=None,source_id=None,reason=''):
        if state not in ('online','unavailable','stopped'):
            raise ValueError('Invalid worker state')
        with self.transaction():
            self._queue_lock()
            old = self._execute('SELECT state FROM queue_workers WHERE worker_id=%s',(worker_id,)).fetchone()
            if old and old['state'] == state:
                return
            # A worker may finish after its preset/source has been deleted.
            stored_preset = self._execute('SELECT id FROM llm_presets WHERE id=%s', (preset_id,)).fetchone() if preset_id is not None else None
            stored_source = self._execute('SELECT id FROM mail_accounts WHERE id=%s', (source_id,)).fetchone() if source_id is not None else None
            self._execute('''INSERT INTO queue_workers(worker_id,state,preset_id,source_id,reason)
              VALUES(%s,%s,%s,%s,%s) ON CONFLICT(worker_id) DO UPDATE SET state=EXCLUDED.state,
              preset_id=EXCLUDED.preset_id,source_id=EXCLUDED.source_id,reason=EXCLUDED.reason,changed_at=clock_timestamp()''',
              (worker_id,state,stored_preset['id'] if stored_preset else None,stored_source['id'] if stored_source else None,reason[:2000]))
            action = {'unavailable':'unavailable','stopped':'stopped','online':'recovered' if old and old['state']=='unavailable' else 'started'}[state]
            self._queue_event(action,worker_id=worker_id,preset_id=preset_id,source_id=source_id,reason=reason)

    def queue_activity(self):
        jobs = self._execute("""SELECT id,kind,preset_id,state,target_preset_id,source_id,worker_id
          FROM work_queue WHERE state='processing' ORDER BY id""").fetchall()
        pending = self._execute("""SELECT kind,target_preset_id,
          COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','') AS embedding_model,
          count(*) AS count FROM work_queue WHERE state='new'
          GROUP BY kind,target_preset_id,COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','')""").fetchall()
        return {'jobs':[dict(row) for row in jobs], 'pending':[dict(row) for row in pending]}

    def queue_snapshot(self,limit=200,filters=None):
        conditions,worker_conditions,values,worker_values=[],[],[],[]
        for key,value in (filters or {}).items():
            if value is None or value == '':
                continue
            if key in ('id','job_id'):
                conditions.append('id=%s')
                values.append(value)
            elif key in ('worker_id','source_id'):
                conditions.append(key+'=%s')
                values.append(value)
                worker_conditions.append(key+'=%s')
                worker_values.append(value)
            elif key == 'state':
                conditions.append('state=%s')
                values.append(value)
            elif key == 'preset_id':
                conditions.append('(preset_id=%s OR target_preset_id=%s)')
                values.extend((value,value))
                worker_conditions.append('preset_id=%s')
                worker_values.append(value)
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        worker_where = ' WHERE '+' AND '.join(worker_conditions) if worker_conditions else ''
        values.append(max(1,min(int(limit),5000)))
        jobs = self._execute('SELECT id,kind,dedup_key,priority,state,revision,claimed_revision,retries,worker_id,preset_id,target_preset_id,source_id,created_at FROM work_queue'+where+' ORDER BY priority,id LIMIT %s',tuple(values)).fetchall()
        workers = self._execute('SELECT w.*, p.name AS preset_name, a.name AS source_name FROM '
            '(SELECT * FROM queue_workers'+worker_where+') w '
            'LEFT JOIN llm_presets p ON p.id=w.preset_id '
            'LEFT JOIN mail_accounts a ON a.id=w.source_id ORDER BY w.worker_id',tuple(worker_values)).fetchall()
        return {'jobs':[dict(row) for row in jobs], 'workers':[dict(row) for row in workers]}

    def queue_events(self,filters=None,limit=200):
        conditions,values=[],[]
        for key,value in (filters or {}).items():
            if key in ('job_id','worker_id','preset_id','source_id','action') and value is not None:
                conditions.append(key+'=%s')
                values.append(value)
        values.append(max(1,min(int(limit),5000)))
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        return [dict(row) for row in self._execute('SELECT * FROM queue_events'+where+' ORDER BY id DESC LIMIT %s',tuple(values)).fetchall()]
