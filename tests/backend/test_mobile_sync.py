# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mailintel.api.routers.mobile_sync import register_routes
from mailintel.db import connect_database, init_database
from mailintel.repositories import Repository


@pytest.fixture
def sync_db():
    url = os.environ.get('MAILINTEL_TEST_DATABASE_URL')
    if not url:
        pytest.skip('MAILINTEL_TEST_DATABASE_URL requires a disposable database')
    connection = connect_database(url)
    schema = 'sync_test_' + uuid.uuid4().hex
    connection.execute('CREATE EXTENSION IF NOT EXISTS vector')
    connection.execute(f'CREATE SCHEMA {schema}')
    connection.execute(f'SET search_path TO {schema}, public')
    init_database(connection)
    try:
        yield connection, schema, url
    finally:
        connection.rollback()
        connection.execute('SET search_path TO public')
        connection.execute(f'DROP SCHEMA {schema} CASCADE')
        connection.commit()
        connection.close()


def seed(connection):
    account = connection.execute("INSERT INTO mail_accounts(name,provider_type,auth_mode,encrypted_password) VALUES ('Account','imap','env','SECRET') RETURNING id").fetchone()['id']
    mailbox = connection.execute("INSERT INTO mailboxes(account_id,provider_mailbox_id,name,sync_cursor) VALUES (%s,'INBOX','Inbox','PRIVATE') RETURNING id", (account,)).fetchone()['id']
    message = connection.execute("INSERT INTO messages(mailbox_id,provider_message_id,subject,snippet_hash,metadata_json) VALUES (%s,'provider-id','Hello','hash','{\"secret\":\"PRIVATE\"}') RETURNING id", (mailbox,)).fetchone()['id']
    connection.commit()
    return message


def generation(connection):
    return connection.execute('SELECT generation FROM sync_counter WHERE id=1').fetchone()['generation']


def test_sync_route_bounds_reset_and_capabilities():
    calls = []
    def page(after, limit):
        calls.append((after, limit))
        return {'generation': after, 'tables': {}, 'has_more': False, 'reset_required': after == 9}
    app = FastAPI()
    register_routes(app, SimpleNamespace(repository=SimpleNamespace(mobile_sync_page=page, mobile_sync_ids=lambda: {'tables': {}})))
    client = TestClient(app)
    assert client.get('/api/sync?after=3&limit=7').status_code == 200
    assert calls == [(3, 7)]
    for query in ['after=-1', 'after=9223372036854775808', 'limit=0', 'limit=2001']:
        assert client.get('/api/sync?' + query).status_code == 400
    assert client.get('/api/sync?after=9').status_code == 409
    assert client.get('/api/sync/ids').json() == {'tables': {}}
    assert all(route.endpoint.mailintel_capability == 'mail.read' for route in app.routes if route.path.startswith('/api/sync'))


def test_generation_changes_rollback_and_relationship_removal(sync_db):
    connection, _, _ = sync_db
    message = seed(connection)
    before = generation(connection)
    connection.execute('UPDATE messages SET star=2 WHERE id=%s', (message,))
    assert generation(connection) > before
    connection.rollback()
    assert generation(connection) == before
    connection.execute("UPDATE mailboxes SET crawl_status='running'")
    assert generation(connection) == before
    profile = connection.execute("INSERT INTO interest_profiles(name,description) VALUES ('Interest','Description') RETURNING id").fetchone()['id']
    connection.execute('INSERT INTO message_interest_matches(message_id,interest_profile_id,score,rationale) VALUES (%s,%s,1,\'Relevant\')', (message,profile))
    connection.commit()
    before = generation(connection)
    assert Repository(connection).mobile_sync_page(0,500)['tables']['messages'][0]['interest_matches']
    connection.execute('DELETE FROM message_interest_matches WHERE message_id=%s', (message,))
    connection.commit()
    page = Repository(connection).mobile_sync_page(before,500)
    assert page['tables']['messages'][0]['interest_matches'] == []
    assert page['generation'] > before


def test_sync_pages_cover_all_tables_without_private_fields(sync_db):
    connection, _, _ = sync_db
    message = seed(connection)
    connection.execute("INSERT INTO message_summaries(message_id,summary_text,importance_score,rationale,confidence,model_name) VALUES (%s,'Summary',1,'Reason',1,'Model')", (message,))
    connection.commit()
    repo = Repository(connection)
    after = 0
    seen = {}
    for _ in range(20):
        page = repo.mobile_sync_page(after,1)
        assert page['generation'] >= after
        after = page['generation']
        for table, rows in page['tables'].items():
            seen.setdefault(table, []).extend(rows)
        if not page['has_more']:
            break
    assert len(seen['messages']) == 1
    assert len(seen['message_summaries']) == 1
    assert len(seen['mailboxes']) == 1
    assert 'metadata_json' not in seen['messages'][0]
    assert 'sync_cursor' not in seen['mailboxes'][0]
    assert 'SECRET' not in str(seen) and 'PRIVATE' not in str(seen)
    assert repo.mobile_sync_page(after,1)['tables']['messages'] == []
    connection.execute('DELETE FROM messages WHERE id=%s', (message,))
    connection.commit()
    assert repo.mobile_sync_ids()['tables']['messages'] == []
    assert repo.mobile_sync_ids()['tables']['message_summaries'] == []


def test_writer_waits_for_previous_generation_commit(sync_db):
    connection, schema, url = sync_db
    seed(connection)
    connection.execute("UPDATE messages SET subject='First writer'")
    first = generation(connection)
    with connect_database(url) as second:
        second.execute(f'SET search_path TO {schema}, public')
        second.execute("SET lock_timeout='100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            second.execute("INSERT INTO interest_profiles(name,description) VALUES ('Other writer','Description')")
        second.rollback()
        connection.commit()
        second.execute(f'SET search_path TO {schema}, public')
        second.execute("INSERT INTO interest_profiles(name,description) VALUES ('Other writer','Description')")
        second.commit()
        assert generation(second) > first


def test_existing_baseline_upgrades_and_reinitializes(sync_db):
    connection, schema, _ = sync_db
    connection.execute(f'DROP SCHEMA {schema} CASCADE')
    connection.execute(f'CREATE SCHEMA {schema}')
    migration_dir = Path(__file__).parents[2] / 'backend/mailintel/persistence/migrations/versions'
    connection.execute((migration_dir / '0001_initial_schema.sql').read_text())
    message = seed(connection)
    init_database(connection)
    initial = generation(connection)
    assert initial > 0
    assert Repository(connection).mobile_sync_page(0,500)['tables']['messages'][0]['id'] == message
    init_database(connection)
    assert generation(connection) == initial


def test_page_includes_generation_ties_and_reports_following_rows(sync_db):
    connection, _, _ = sync_db
    connection.execute("INSERT INTO interest_profiles(name,description) VALUES ('A','a'),('B','b'),('C','c')")
    connection.execute('ALTER TABLE interest_profiles DISABLE TRIGGER sync_generation')
    connection.execute("UPDATE interest_profiles SET sync_generation=1 WHERE name IN ('A','B')")
    connection.execute("UPDATE interest_profiles SET sync_generation=2 WHERE name='C'")
    connection.execute('ALTER TABLE interest_profiles ENABLE TRIGGER sync_generation')
    connection.commit()
    page = Repository(connection).mobile_sync_page(0,1)
    assert len(page['tables']['interest_profiles']) == 2
    assert page['generation'] == 1
    assert page['has_more'] is True
    following = Repository(connection).mobile_sync_page(1,1)
    assert [row['name'] for row in following['tables']['interest_profiles']] == ['C']


def test_account_rename_and_cascaded_profile_deletion(sync_db):
    connection, _, _ = sync_db
    message = seed(connection)
    repo = Repository(connection)
    before = generation(connection)
    connection.execute("UPDATE mail_accounts SET name='Renamed'")
    connection.commit()
    page = repo.mobile_sync_page(before,500)
    assert page['tables']['mailboxes'][0]['account_name'] == 'Renamed'
    profile = connection.execute("INSERT INTO interest_profiles(name,description) VALUES ('Interest','Description') RETURNING id").fetchone()['id']
    connection.execute('INSERT INTO message_interest_matches(message_id,interest_profile_id,score,rationale) VALUES (%s,%s,1,\'Relevant\')', (message,profile))
    topic = connection.execute("INSERT INTO topic_clusters(topic_key,name,description) VALUES ('topic','Topic','Description') RETURNING id").fetchone()['id']
    connection.execute('INSERT INTO message_topic_memberships(message_id,topic_cluster_id,score,is_primary) VALUES (%s,%s,1,true)', (message,topic))
    connection.commit()
    before = generation(connection)
    connection.execute('DELETE FROM interest_profiles WHERE id=%s', (profile,))
    connection.execute('DELETE FROM topic_clusters WHERE id=%s', (topic,))
    connection.commit()
    page = repo.mobile_sync_page(before,500)
    assert page['tables']['messages'][0]['interest_matches'] == []
    assert page['tables']['messages'][0]['topic_memberships'] == []
    ids = repo.mobile_sync_ids()['tables']
    assert profile not in ids['interest_profiles']
    assert topic not in ids['topic_clusters']
