# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mailintel.services.interval_queue import IntervalQueue
from mailintel.services.pipeline import PipelineRuntime
from mailintel.services.work_queue import WorkQueueRuntime


@pytest.mark.parametrize('summary_type,phase', [
    ('daily', 'batch'), ('weekly', 'batch'), ('monthly', 'batch'), ('weekly', 'merge'),
])
def test_invalid_reference_requeues_then_valid_response_succeeds(summary_type, phase):
    merge = phase == 'merge'
    valid_id = 1 if merge else 101
    # 102 exists in another batch, but was not supplied to this call. For merging,
    # real message ID 101 is invalid: the model must use temporary entry ID 1.
    invalid_id = 101 if merge else 102
    source = ('- mail_id=101; 2026-11-04; Sender; Subject: Ticket 102, example mail_id=102'
              if summary_type == 'daily'
              else '- 2026-11-04; mail_ids=[101]: Ticket 102, example mail_ids=[102]')
    llm = SimpleNamespace(summarize_interval=Mock(side_effect=[
        {'entries': [{'summary': 'One summary.', 'message_ids': [valid_id, invalid_id]}]},
        {'entries': [{'summary': 'One summary.', 'message_ids': [valid_id]}]},
    ]))
    pipeline = SimpleNamespace(
        llm_service=llm,
        _interval_summary_entries=PipelineRuntime._interval_summary_entries,
        _interval_merge_source_text=PipelineRuntime._interval_merge_source_text,
        _expand_interval_merge_entries=PipelineRuntime._expand_interval_merge_entries,
        _dedupe_interval_summary_entries=PipelineRuntime._dedupe_interval_summary_entries,
    )
    queue = IntervalQueue(pipeline)
    queue._store = Mock()
    queue._continue = Mock()
    queue._merge_fallback = Mock(side_effect=AssertionError('Invalid references must retry'))
    job = {'id': 7, 'kind': 'interval_rollup' if summary_type != 'daily' else 'interval_daily',
           'priority': 3, 'dedup_key': 'interval:9', 'payload': {
               'summary_id': 9, 'summary_type': summary_type, 'interval_start_date': '2026-11-04',
               'period_days': 1, 'mailbox_name': 'Inbox', 'user_context': '',
               'batches': [source, '- mail_id=102; 2026-11-04; Sender; Another message'],
               'index': 0, 'phase': phase,
               'entries': [{'summary': 'Preliminary summary.', 'message_ids': [101]}] if merge else [],
           }}
    repository = Mock()
    repository.transaction.side_effect = lambda: nullcontext()
    repository.queue_complete.side_effect = lambda job, apply: apply()
    runtime = WorkQueueRuntime(SimpleNamespace(worker_poll_seconds=0), llm)
    runtime._wait = Mock()
    queue.register(runtime)

    runtime._run(repository, job, None)
    repository.queue_requeue.assert_called_once_with(job, 'LLM structured output invalid')
    repository.queue_complete.assert_not_called()
    queue._store.assert_not_called()
    queue._continue.assert_not_called()
    assert job['payload']['index'] == 0

    runtime._run(repository, job, None)
    assert llm.summarize_interval.call_count == 2
    repository.queue_complete.assert_called_once()
    assert repository.queue_requeue.call_count == 1
    if merge:
        assert queue._store.call_args.args[2] == [{'summary': 'One summary.', 'message_ids': [101]}]
    else:
        continued = queue._continue.call_args.args[2]
        assert continued['index'] == 1
        assert continued['entries'] == [{'summary': 'One summary.', 'message_ids': [101]}]


def test_source_ids_are_only_taken_from_record_prefixes():
    assert IntervalQueue._source_message_ids(
        '- mail_id=101; 2026-11-04; Sender; Ticket 1, mail_id=999\n'
        '- 2026-11-05; mail_ids=[102, 103]: mail_ids=[888], ticket 2'
    ) == {101, 102, 103}
