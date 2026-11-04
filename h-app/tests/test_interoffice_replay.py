"""Persisted reply replay protection, using only the runner's private Redis."""

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import redis

from core.keys import prefix
from modules.api.port import MAILBOX_MAXLEN
from modules.interoffice import store as storage
from modules.interoffice.relay import OfficeWorker
from modules.interoffice.store import RelayStateError, Store
from test_interoffice_relay import add_office, egress, frame, message, sse_server, store


def receive(store, office, data=None):
    return OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(data or message())))


def sizes(store, office):
    ranked, indexed = store.ledger_keys(office)
    return store.r.zcard(ranked), store.r.hlen(indexed)


def test_seen_limit_covers_four_times_the_api_count_retention():
    assert storage.SEEN_LEDGER_LIMIT >= 4 * MAILBOX_MAXLEN


def test_cursor_loss_and_fresh_store_do_not_readmit_a_seen_stream_id(store):
    office = add_office(store)
    receive(store, office)
    store.reset_cursor(office)
    restarted = Store(store.r, pod=store.pod, tenant=store.tenant, allow_plaintext=True)
    receive(restarted, office)
    assert len(egress(store)) == 1
    assert store.cursor(office) == '10-0'
    assert sizes(store, office) == (1, 1)


def test_token_rotation_keeps_seen_identity_across_cursor_reset(store):
    office = add_office(store)
    receive(store, office)
    replacement = add_office(store, token=office.token[:-1] + 'c')
    store.reset_cursor(replacement)
    assert store.ledger_keys(office) == store.ledger_keys(replacement)
    receive(store, replacement)
    assert len(egress(store)) == 1
    assert store.cursor(replacement) == '10-0'


@pytest.mark.parametrize('field', ['name', 'url', 'participant'], ids=['alias', 'endpoint', 'participant'])
def test_distinct_cooperations_have_independent_stream_id_ledgers(store, field):
    office = add_office(store)
    receive(store, office)
    other = replace(office, **{field: {'name': 'other', 'url': 'http://127.0.0.1:2', 'participant': 'other'}[field]})
    store.r.hset(store.offices_key, other.name, json.dumps({'url': other.url, 'participant': other.participant}))
    store.r.hset(store.tokens_key, other.name, other.token)
    store.reset_cursor(other)
    assert store.ledger_keys(other) != store.ledger_keys(office)
    receive(store, other)
    assert len(egress(store)) == 2


@pytest.mark.parametrize('invalid', ['missing', None, 'bad.id', 'A' * 32, []],
                         ids=['missing', 'null', 'malformed', 'uppercase', 'nonstring'])
def test_unusable_remote_stream_id_cannot_be_admitted_or_checkpointed(store, invalid):
    office = add_office(store)
    data = message(stream_id=invalid)
    if invalid == 'missing':
        del data['stream_id']
    with pytest.raises(RelayStateError, match='invalid remote stream id'):
        receive(store, office, data)
    assert not egress(store)
    assert store.cursor(office) is None
    assert sizes(store, office) == (0, 0)


def test_failure_before_atomic_enqueue_leaves_the_reply_retryable(store, monkeypatch):
    office = add_office(store)
    original = store.r.eval
    def fail(script, *args):
        if args[-3] == 'enqueue':
            raise redis.ConnectionError('simulated pre-enqueue disconnect')
        return original(script, *args)
    monkeypatch.setattr(store.r, 'eval', fail)
    with pytest.raises(redis.ConnectionError):
        receive(store, office)
    assert not egress(store)
    assert store.cursor(office) is None
    assert sizes(store, office) == (0, 0)
    monkeypatch.setattr(store.r, 'eval', original)
    receive(store, office)
    assert len(egress(store)) == 1
    assert sizes(store, office) == (1, 1)


def test_lost_atomic_enqueue_response_does_not_duplicate_the_reply(store, monkeypatch):
    office = add_office(store)
    original = store.r.eval
    def lose_response(script, *args):
        result = original(script, *args)
        if args[-3] == 'enqueue':
            raise redis.ConnectionError('simulated lost enqueue response')
        return result
    monkeypatch.setattr(store.r, 'eval', lose_response)
    with pytest.raises(redis.ConnectionError):
        receive(store, office)
    assert len(egress(store)) == 1
    assert store.cursor(office) is None
    assert sizes(store, office) == (1, 1)
    monkeypatch.setattr(store.r, 'eval', original)
    receive(store, office)
    assert len(egress(store)) == 1
    assert store.cursor(office) == '10-0'


def test_concurrent_workers_append_one_local_envelope(store):
    office = add_office(store)
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(lambda _: receive(store, office), range(16)))
    assert len(egress(store)) == 1
    assert sizes(store, office) == (1, 1)
    assert store.cursor(office) == '10-0'


@pytest.mark.parametrize('pending', ['no-lead', 'unenrolled-relay'])
def test_pending_or_unenrolled_admission_does_not_consume_the_stream_id(store, pending):
    office = add_office(store)
    if pending == 'no-lead':
        lead = prefix(store.pod, store.tenant, resource='lead')
        store.r.delete(lead)
        worker = OfficeWorker(store, office, mapping_wait=0)
        thread = threading.Thread(target=lambda: worker.receive(io.BytesIO(frame(message()))))
        thread.start()
        time.sleep(.05)
        worker.stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        store.r.set(lead, 'lead')
    else:
        registry = prefix(store.pod, store.tenant, resource='registry')
        store.r.hdel(registry, 'relay')
        with pytest.raises(RelayStateError, match='not enrolled'):
            receive(store, office)
        store.r.hset(registry, 'relay', 'api')
    assert not egress(store)
    assert sizes(store, office) == (0, 0)
    assert store.cursor(office) is None
    receive(store, office)
    assert len(egress(store)) == 1


def test_policy_denial_is_checked_by_normal_send_and_remains_retryable(store):
    office = add_office(store)
    exports = prefix(store.pod, store.tenant, 'relay', 'tags')
    imports = prefix(store.pod, store.tenant, 'lead', 'tags')
    store.r.hset(exports, 'export', '["outbound"]')
    store.r.hset(imports, 'import', '["other"]')
    from core.envelope import EnvelopeError
    with pytest.raises(EnvelopeError):
        receive(store, office)
    assert sizes(store, office) == (0, 0)
    assert not egress(store)
    assert store.cursor(office) is None
    store.r.hdel(imports, 'import')
    receive(store, office)
    assert len(egress(store)) == 1


def test_wrong_egress_type_never_leaves_a_false_seen_record(store):
    office = add_office(store)
    queue = prefix(store.pod, store.tenant, 'relay', 'egress')
    store.r.set(queue, 'not-a-list')
    with pytest.raises(redis.ResponseError, match='invalid relay egress type'):
        receive(store, office)
    assert sizes(store, office) == (0, 0)
    assert store.cursor(office) is None
    assert store.r.get(queue) == b'not-a-list'
    store.r.delete(queue)
    receive(store, office)
    assert len(egress(store)) == 1


def test_count_trim_bounds_both_ledger_structures_and_retains_recent_replays(store, monkeypatch):
    monkeypatch.setattr(storage, 'SEEN_LEDGER_LIMIT', 4)
    office = add_office(store)
    for index in range(7):
        receive(store, office, message(f'100-{index}'))
    assert sizes(store, office) == (4, 4)
    for key in store.ledger_keys(office):
        assert store.r.ttl(key) == -1
    store.reset_cursor(office)
    for index in range(3, 7):
        receive(store, office, message(f'100-{index}'))
    assert len(egress(store)) == 7
    assert sizes(store, office) == (4, 4)


def test_ledger_order_keeps_full_uint64_cursor_precision(store, monkeypatch):
    monkeypatch.setattr(storage, 'SEEN_LEDGER_LIMIT', 3)
    office = add_office(store)
    cursors = ['9007199254740992-2', '9007199254740992-1',
               '9007199254740993-0', '18446744073709551615-18446744073709551615']
    for cursor in cursors:
        assert store.enqueue_once(office, message(cursor)['stream_id'], cursor, b'isolated-test-envelope')
    _, indexed = store.ledger_keys(office)
    retained = {key.decode() for key in store.r.hkeys(indexed)}
    assert retained == {message(cursor)['stream_id'] for cursor in (cursors[0], *cursors[2:])}
    assert sizes(store, office) == (3, 3)


def test_repeated_identity_at_a_new_cursor_refreshes_its_count_position(store, monkeypatch):
    monkeypatch.setattr(storage, 'SEEN_LEDGER_LIMIT', 2)
    office = add_office(store)
    first = message('10-0')
    receive(store, office, first)
    receive(store, office, message('20-0'))
    receive(store, office, message('30-0', stream_id=first['stream_id']))
    receive(store, office, message('40-0'))
    _, indexed = store.ledger_keys(office)
    assert store.r.hexists(indexed, first['stream_id'])
    assert not store.r.hexists(indexed, message('20-0')['stream_id'])
    store.reset_cursor(office)
    receive(store, office, first)
    assert store.r.hget(indexed, first['stream_id']).startswith(b'00000000000000000030-')
    assert len(egress(store)) == 3


def test_cooperation_change_at_the_atomic_boundary_prevents_stale_admission(store, monkeypatch):
    office = add_office(store)
    original = store.r.eval
    def rotate(script, *args):
        if args[-3] == 'enqueue':
            store.r.hset(store.tokens_key, office.name, office.token[:-1] + 'c')
        return original(script, *args)
    monkeypatch.setattr(store.r, 'eval', rotate)
    with pytest.raises(RelayStateError, match='cooperation or cursor changed'):
        receive(store, office)
    assert not egress(store)
    assert sizes(store, office) == (0, 0)
    assert store.cursor(office) is None
    monkeypatch.setattr(store.r, 'eval', original)
    replacement = store.snapshot()[0][office.name]
    receive(store, replacement)
    assert len(egress(store)) == 1


def test_legacy_cursor_backfill_marks_only_committed_entries_then_admits_newer(store):
    from core.channels import send
    office = add_office(store)
    store.checkpoint(office, '20-0')
    assert store.backfill_cursor(office) == '20-0'
    assert sizes(store, office) == (0, 0)
    newer_was_unseen = []
    def checked_send(client, **kwargs):
        newer_was_unseen.append(not store.seen(office, message('30-0')['stream_id'], '30-0'))
        return send(client, **kwargs)
    worker = OfficeWorker(store, office, mapping_wait=0, send_fn=checked_send)
    worker.receive(io.BytesIO(b''.join(frame(message(cursor)) for cursor in ('10-0', '20-0', '30-0'))),
                   backfill_cursor=store.backfill_cursor(office))
    assert newer_was_unseen == [True]
    assert len(egress(store)) == 1
    assert sizes(store, office) == (3, 3)
    assert store.cursor(office) == '30-0'
    assert store.backfill_cursor(office) is None
    store.reset_cursor(office)
    for cursor in ('10-0', '20-0', '30-0'):
        receive(store, office, message(cursor))
    assert len(egress(store)) == 1


def test_legacy_backfill_connection_starts_oldest_once_then_resumes(store, sse_server):
    from test_interoffice_relay import wait_until
    office = add_office(store, sse_server.url)
    store.checkpoint(office, '20-0')
    sse_server.responses = [b''.join(frame(message(cursor)) for cursor in ('10-0', '20-0', '30-0'))]
    worker = OfficeWorker(store, office, mapping_wait=0)
    worker.thread.start()
    try:
        wait_until(lambda: store.cursor(office) == '30-0')
    finally:
        worker.stop.set()
        worker.thread.join(timeout=6)
    assert len(sse_server.requests) == 1
    assert sse_server.requests[0][0] == '/agents/visitor/messages/stream'
    restarted = OfficeWorker(store, office, mapping_wait=0)
    restarted.thread.start()
    try:
        wait_until(lambda: len(sse_server.requests) == 2)
    finally:
        restarted.stop.set()
        restarted.thread.join(timeout=6)
    assert sse_server.requests[1][0] == '/agents/visitor/messages/stream?after=30-0'
    assert len(egress(store)) == 1


def test_partial_legacy_backfill_is_retryable_and_idempotent(store):
    office = add_office(store)
    store.checkpoint(office, '20-0')
    worker = OfficeWorker(store, office, mapping_wait=0)
    worker.receive(io.BytesIO(frame(message('10-0'))), backfill_cursor='20-0')
    assert store.backfill_cursor(office) == '20-0'
    assert sizes(store, office) == (1, 1)
    worker.receive(io.BytesIO(frame(message('10-0')) + frame(message('20-0'))), backfill_cursor='20-0')
    assert store.backfill_cursor(office) is None
    assert sizes(store, office) == (2, 2)
    assert not egress(store)
    assert store.cursor(office) == '20-0'


@pytest.mark.parametrize('history', [False, True], ids=['empty', 'trimmed-boundary'])
def test_tail_keepalive_completes_legacy_backfill_once(store, history):
    office = add_office(store)
    store.checkpoint(office, '20-0')
    raw = (frame(message('10-0')) if history else b'') + b': keepalive\n\n'
    OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(raw), backfill_cursor='20-0')
    assert store.backfill_cursor(office) is None
    assert store.cursor(office) == '20-0'
    assert sizes(store, office) == ((1, 1) if history else (0, 0))
    assert not egress(store)


def test_legacy_backfill_cannot_mark_newer_unadmitted_entries(store):
    office = add_office(store)
    store.checkpoint(office, '20-0')
    with pytest.raises(RelayStateError, match='exceeds committed cursor'):
        store.remember(office, message('30-0')['stream_id'], '30-0', '20-0')
    assert sizes(store, office) == (0, 0)
    assert not egress(store)
    assert store.backfill_cursor(office) == '20-0'


def test_without_a_committed_cursor_legacy_delivery_is_not_guessed(store):
    office = add_office(store)
    with pytest.raises(RelayStateError, match='cooperation or cursor changed'):
        store.remember(office, message()['stream_id'], '10-0', '10-0')
    assert sizes(store, office) == (0, 0)
    assert store.backfill_cursor(office) is None
    receive(store, office)
    assert len(egress(store)) == 1
    assert sizes(store, office) == (1, 1)


def test_reset_cursor_cannot_complete_a_stale_legacy_backfill(store):
    office = add_office(store)
    store.checkpoint(office, '20-0')
    store.remember(office, message()['stream_id'], '10-0', '20-0')
    store.reset_cursor(office)
    assert not store.finish_backfill(office, '20-0')
    assert not store.r.exists(store.ready_key(office))
    assert sizes(store, office) == (1, 1)
    assert not egress(store)


def test_legacy_backfill_refuses_history_larger_than_its_count_bound(store, monkeypatch):
    from modules.interoffice import relay
    monkeypatch.setattr(storage, 'SEEN_LEDGER_LIMIT', 2)
    monkeypatch.setattr(relay, 'SEEN_LEDGER_LIMIT', 2)
    office = add_office(store)
    store.checkpoint(office, '100-9')
    raw = b''.join(frame(message(f'100-{index}')) for index in range(3))
    with pytest.raises(RelayStateError, match='exceeds replay ledger bound'):
        OfficeWorker(store, office).receive(io.BytesIO(raw), backfill_cursor='100-9')
    assert sizes(store, office) == (2, 2)
    assert not store.r.exists(store.ready_key(office))
    assert not egress(store)
