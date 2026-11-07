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
    valid_id = 1
    # Every call uses compact references, so database ID 101 is invalid here.
    invalid_id = 101
    source = [{"message_ids": [101], "summary": "Ticket 102, example mail_id=102"}]
    llm = SimpleNamespace(summarize_interval=Mock(side_effect=[
        {'entries': [{'summary': 'One summary.', 'message_ids': [valid_id, invalid_id]}]},
        {'entries': [{'summary': 'One summary.', 'message_ids': [valid_id]}]},
    ]))
    pipeline = SimpleNamespace(
        llm_service=llm,
        _interval_summary_entries=PipelineRuntime._interval_summary_entries,
        _interval_summary_source=PipelineRuntime._interval_summary_source,
        _expand_interval_summary_entries=PipelineRuntime._expand_interval_summary_entries,
        _dedupe_interval_summary_entries=PipelineRuntime._dedupe_interval_summary_entries,
    )
    queue = IntervalQueue(pipeline)
    queue._current = lambda *args: True
    queue._store = Mock()
    queue._continue = Mock()
    queue._merge_fallback = Mock(side_effect=AssertionError('Invalid references must retry'))
    job = {'id': 7, 'kind': 'interval_rollup' if summary_type != 'daily' else 'interval_daily',
           'priority': 3, 'dedup_key': 'interval:9', 'payload': {
               'summary_id': 9, 'summary_type': summary_type, 'interval_start_date': '2026-11-04',
               'period_days': 1, 'mailbox_name': 'Inbox', 'user_context': '',
               'batches': [source, [{'message_ids': [102], 'summary': 'Another message'}]],
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


def test_source_ids_are_only_taken_from_record_fields():
    assert IntervalQueue._source_message_ids([
        {"message_ids": [101], "summary": 'Ticket 1, "message_ids":[999]'},
        {"message_ids": [102, 103], "summary": "mail_ids=[888], ticket 2"},
    ]) == {101, 102, 103}


@pytest.mark.parametrize('value', [True, False, 1.5, 101.0, '101.0', '1e2', 'abc', '', '0', '-1', None, 0, -1])
def test_interval_ids_are_positive_json_integers(value):
    from mailintel.services.llm import LLMService, LLMContentValidationError
    with pytest.raises(LLMContentValidationError, match='positive integers'):
        LLMService()._normalize_interval_summary_payload({
            'entries': [{'summary': 'Example.', 'message_ids': [101, value]}],
        })


def test_interval_ids_accept_and_deduplicate_integer_strings():
    from mailintel.services.llm import LLMService
    result = LLMService()._normalize_interval_summary_payload({
        'entries': [{'summary': 'Example.', 'message_ids': ['101', 101, '102']}],
    })
    assert result['entries'][0]['message_ids'] == [101, 102]


@pytest.mark.parametrize('kind', ['daily', 'weekly', 'monthly'])
def test_multiline_fields_stay_in_one_batch_record(kind):
    from datetime import date
    repository = Mock()
    repository.list_daily_summary_inputs.return_value = [
        {'id': i, 'subject': 'Subject\ncontinued', 'sender_name': 'Sender\nName',
         'summary_text': 'First sentence.\nSecond sentence.'}
        for i in range(1, 34)
    ]
    repository.list_rollup_summary_inputs.return_value = [{
        'interval_start_date': date(2026, 11, 1),
        'summary_json': {'entries': [
            {'summary': 'First sentence.\nSecond sentence.', 'message_ids': [i]}
            for i in range(1, 34)
        ]},
    }]
    pipeline = SimpleNamespace(
        _interval_summary_entries=PipelineRuntime._interval_summary_entries,
        _summary_user_context=lambda repo: '',
        _interval_summary_source_batches=PipelineRuntime._interval_summary_source_batches,
    )
    payload = IntervalQueue(pipeline)._payload(repository, {
        'id': 9, 'mailbox_id': 1, 'summary_type': kind,
        'interval_start_date': date(2026, 11, 1), 'period_days': 1 if kind == 'daily' else 7,
    }, 'UTC')
    assert [len(batch) for batch in payload['batches']] == [32, 1]
    assert IntervalQueue._source_message_ids(payload['batches'][0]) == set(range(1, 33))
    assert IntervalQueue._source_message_ids(payload['batches'][1]) == {33}
    assert all('First sentence.' in record['summary'] and 'Second sentence.' in record['summary']
               for batch in payload['batches'] for record in batch)


@pytest.mark.parametrize('kind', ['daily', 'weekly', 'monthly'])
@pytest.mark.parametrize('phase', ['batch', 'merge'])
def test_final_entry_preserves_ungrouped_ids(kind, phase):
    queue = IntervalQueue(SimpleNamespace())
    queue._current = lambda *args: True
    repository = Mock()
    repository.get_pipeline_settings.return_value = {}
    source = [{"message_ids": [101, 102, 103], "summary": "Three messages"}]
    payload = {'summary_id': 9, 'batches': [source], 'phase': phase}
    entries = [{'summary': 'Grouped.', 'message_ids': [101]}]
    queue._store(repository, payload, entries)
    saved = repository.mark_interval_summary_fresh.call_args.args[1]['entries']
    assert saved == entries + [{'summary': 'Other messages', 'message_ids': [102, 103]}]
    # Complete coverage produces no synthetic row; an empty result retains all IDs.
    repository.reset_mock()
    queue._store(repository, payload, [{'summary': 'Complete.', 'message_ids': [101, 102, 103]}])
    assert len(repository.mark_interval_summary_fresh.call_args.args[1]['entries']) == 1
    queue._store(repository, payload, [])
    assert repository.mark_interval_summary_fresh.call_args.args[1]['entries'] == [
        {'summary': 'Other messages', 'message_ids': [101, 102, 103]},
    ]


def test_compact_ids_keep_rollup_evidence_together():
    records = [
        {'date': '2026-11-01', 'summary': 'Banking and account notices.', 'message_ids': [101, 104]},
        {'date': '2026-11-02', 'summary': 'Technical updates.', 'message_ids': [501]},
    ]
    inputs, mapping = PipelineRuntime._interval_summary_source(records)
    assert [entry['id'] for entry in inputs] == [1, 2]
    assert all('message_ids' not in entry for entry in inputs)
    assert mapping == {1: [101, 104], 2: [501]}
    assert PipelineRuntime._expand_interval_summary_entries(
        {'entries': [{'summary': 'Account notices.', 'message_ids': [1]}]}, mapping,
    ) == [{'summary': 'Account notices.', 'message_ids': [101, 104]}]


def test_oversized_batch_splits_records_without_losing_fields():
    from mailintel.services.llm import LLMInputError
    records = [{'summary': 'First\n"quoted"', 'message_ids': [101]},
               {'summary': 'Second', 'message_ids': [102]},
               {'summary': 'Third', 'message_ids': [103]}]
    queue = IntervalQueue(SimpleNamespace(
        _interval_summary_source=PipelineRuntime._interval_summary_source,
        llm_service=SimpleNamespace(summarize_interval=Mock(side_effect=LLMInputError('Too large'))),
    ))
    queue._current = lambda *args: True
    queue._continue = Mock()
    payload = {'batches': [records], 'entries': [], 'phase': 'batch', 'index': 0,
               'summary_type': 'daily', 'interval_start_date': '2026-11-01',
               'period_days': 1, 'mailbox_name': 'Inbox', 'user_context': ''}
    queue.handle(Mock(), {'payload': payload, 'priority': 3}, None)()
    updated = queue._continue.call_args.args[2]
    assert updated['batches'] == [records[:1], records[1:]]
    assert updated['index'] == 0


def test_old_queued_format_is_refreshed_before_model_call():
    pipeline = SimpleNamespace(settings=SimpleNamespace(summary_timezone='UTC'), llm_service=Mock())
    queue = IntervalQueue(pipeline)
    fresh = {'summary_id': 9, 'summary_type': 'daily', 'source_revision': 'new',
             'batches': [[{'summary': 'Text', 'message_ids': [101]}]]}
    queue._payload = Mock(return_value=fresh)
    repo = Mock()
    repo.get_interval_summary_embedding_detail.return_value = {'id': 9}
    repo._execute.return_value.fetchone.return_value = {'payload': {'source_revision': 'old'}}
    queue.handle(repo, {'payload': {'summary_id': 9, 'source_revision': 'old',
                                  'batches': ['old line format']}}, None)()
    repo.queue_enqueue.assert_called_once_with('interval_daily', 'interval:9', fresh, priority=3)
    pipeline.llm_service.summarize_interval.assert_not_called()
