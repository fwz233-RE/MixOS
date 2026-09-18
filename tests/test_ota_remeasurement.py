"""Bounded read-only remeasurement through production Link/Client byte framing.

Only the device, clock and transport are simulated. No hardware or wall sleeps.
"""
import hashlib
import json
import struct
import unittest
from unittest import mock

from test_ota_v2 import fixture, ota_esp, v2, C, T


class RemeasurementTests(unittest.TestCase):
    def client(self, *, timeout=0.25, no_heartbeats=False):
        updater, device, data, clock = fixture(no_heartbeats=no_heartbeats, protected=0)
        device.running = device.boot = 1
        device.flags = v2.JOURNAL | v2.EXACT_HASH
        device.measurement_ready = True
        device.phase = v2.Phase.CONFIRMED
        client = updater.open()
        client.timeout = timeout
        client.request_id = 100
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        device.binding = binding
        device.received = bytearray(data)
        return client, device, binding, clock

    def verify(self, client, binding, clock, *, seconds=12, deadline=None):
        return v2.verify_actual(client, binding, bytes(range(32)),
                                clock.now + seconds if deadline is None else deadline)

    def drop(self, device, indices=None, *, after_ack=True):
        original = device.response
        seen = []
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING and (device.health_acknowledged or not after_ack):
                seen.append(request)
                if indices is None or len(seen) in indices:
                    return
            return original(request, **kwargs)
        device.response = response
        return seen

    def requests(self, device):
        return [f for f in device.writes if f.type == T.OTA_REQUEST and
                f.payload[1] == v2.Op.VERIFY_RUNNING]

    def assert_read_only(self, device, *, ack=1):
        self.assertEqual(device.ops.count(v2.Op.HEALTH_ACK), ack)
        self.assertTrue(all(op in (v2.Op.VERIFY_RUNNING, v2.Op.HEALTH_ACK) for op in device.ops))
        self.assertEqual(sum(f.type == T.CAPS_QUERY for f in device.writes), 1)

    def test_one_lost_reply_recovers_with_new_id_same_full_binding_and_boot(self):
        client, device, binding, clock = self.client()
        seen = self.drop(device, {1})
        result = self.verify(client, binding, clock)
        diag = result['diagnostics']['verification']
        self.assertEqual(result['state'], 'confirmed')
        self.assertTrue(result['actual_file_verified'])
        self.assertTrue(result['maintenance_health_acknowledged'])
        self.assertGreaterEqual(result['heartbeat_seconds'], 6)
        self.assertEqual(diag['extra_requests'], 1)
        self.assertTrue(diag['recovered'])
        self.assertEqual(diag['timeout_events'][0]['stage'], 'wait')
        self.assertTrue(diag['timeout_events'][0]['retry'])
        frames = self.requests(device)
        ids = [struct.unpack_from('<I', f.payload, 4)[0] for f in frames]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(diag['last_attempt']['request_id'], ids[-1])
        self.assertGreater(ids[-1], struct.unpack_from('<I', seen[0].payload, 4)[0])
        self.assertTrue(all(f.payload[8:] == frames[0].payload[8:] for f in frames))
        self.assertTrue(all((f.epoch, f.session) == (frames[0].epoch, frames[0].session) for f in frames))
        self.assertEqual(diag['measurement_attempts'], len(frames))
        self.assertEqual(client.timeout, 0.25)
        self.assert_read_only(device)

    def test_first_measurement_loss_can_recover_but_never_repeats_ack(self):
        client, device, binding, clock = self.client()
        self.drop(device, {1}, after_ack=False)
        result = self.verify(client, binding, clock)
        self.assertEqual(result['diagnostics']['verification']['extra_requests'], 1)
        self.assert_read_only(device)

    def test_all_replies_lost_stop_after_two_extra_requests_despite_valid_and_heartbeats(self):
        client, device, binding, clock = self.client()
        self.drop(device)
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        exc = caught.exception
        diag = exc.diagnostics['verification']
        self.assertEqual(exc.state, 'unknown')
        self.assertEqual(exc.response.state, v2.VALID)
        self.assertTrue(exc.response.flags & v2.HEALTH_ACKED)
        self.assertEqual(len(self.requests(device)), 4)  # first measurement + three timed out
        self.assertEqual(diag['extra_requests'], 2)
        self.assertEqual(len(diag['timeout_events']), 3)
        self.assertEqual(diag['timeout_events'][-1]['stop_reason'], 'retry-budget-exhausted')
        self.assertFalse(diag['recovered'])
        self.assertTrue(all(e['heartbeat_count_since_start'] > 0 for e in diag['timeout_events']))
        self.assert_read_only(device)

    def test_retry_budget_is_global_across_separate_lost_replies(self):
        client, device, binding, clock = self.client()
        self.drop(device, {1, 3, 5})
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        diag = caught.exception.diagnostics['verification']
        self.assertEqual(diag['extra_requests'], 2)
        self.assertEqual(diag['measurement_attempts'], 6)
        self.assertEqual([e['attempt'] for e in diag['timeout_events']], [2, 4, 6])
        self.assert_read_only(device)

    def test_old_request_id_replies_cannot_substitute_for_new_measurement(self):
        client, device, binding, clock = self.client()
        original = device.response
        held = []
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
                if not held:
                    held.append(request)
                else:
                    original(held[0])  # fresh wire sequence, stale request ID
                return
            return original(request, **kwargs)
        device.response = response
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        exc = caught.exception
        self.assertEqual(exc.state, 'unknown')
        self.assertEqual(exc.diagnostics['response_id_mismatches'], 1)
        self.assertEqual(exc.diagnostics['verification']['extra_requests'], 2)
        self.assertEqual(len(self.requests(device)), 4)
        self.assert_read_only(device)

    def test_late_old_reply_is_ignored_before_matching_fresh_reply(self):
        client, device, binding, clock = self.client()
        original = device.response
        held = []
        returned = []
        call = client.call
        def checked_call(*args, **kwargs):
            reply = call(*args, **kwargs)
            self.assertEqual(reply.request_id, client.request_id)
            returned.append(reply.request_id)
            return reply
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged and not held:
                held.append(request)
                return
            if held:
                original(held[0])
            return original(request, **kwargs)
        client.call = checked_call
        device.response = response
        result = self.verify(client, binding, clock)
        old_id = struct.unpack_from('<I', held[0].payload, 4)[0]
        self.assertNotIn(old_id, returned)
        self.assertEqual(result['diagnostics']['verification']['extra_requests'], 1)
        self.assert_read_only(device)

    def test_no_heartbeat_progress_never_remeasures(self):
        client, device, binding, clock = self.client(no_heartbeats=True)
        client.link.heartbeat_times.extend([clock.now - 1, clock.now - 0.5])
        self.drop(device)
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        self.assertEqual(len(self.requests(device)), 2)
        diag = caught.exception.diagnostics['verification']
        self.assertEqual(diag['extra_requests'], 0)
        self.assertEqual(diag['timeout_events'][0]['stop_reason'], 'no-fresh-heartbeat')
        self.assert_read_only(device)

    def test_stale_heartbeat_after_some_progress_never_remeasures(self):
        client, device, binding, clock = self.client(timeout=60)
        self.drop(device)
        stop = clock.now + 0.3
        read = device.read
        def read_until_stale(size):
            if clock.now >= stop:
                device.no_heartbeats = True
            return read(size)
        device.read = read_until_stale
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        diag = caught.exception.diagnostics
        self.assertGreater(diag['heartbeat_count_since_start'], 0)
        self.assertGreater(diag['latest_heartbeat_age_seconds'], 3.5)
        self.assertEqual(diag['verification']['extra_requests'], 0)
        self.assertEqual(len(self.requests(device)), 2)

    def test_epoch_or_session_change_never_remeasures(self):
        for change in ('epoch', 'session'):
            with self.subTest(change=change):
                client, device, binding, clock = self.client()
                original = device.response
                def response(request, **kwargs):
                    if request.payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
                        if change == 'epoch':
                            device.reconnect()
                        else:
                            client.link.session += 1
                        return
                    return original(request, **kwargs)
                device.response = response
                with self.assertRaises(v2.OutcomeError) as caught:
                    self.verify(client, binding, clock)
                self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 0)
                self.assertEqual(len(self.requests(device)), 2)
                self.assert_read_only(device)

    def test_incomplete_send_never_remeasures_even_with_heartbeats(self):
        client, device, binding, clock = self.client()
        send = client.link.send
        def block_measurement(channel, kind, session, payload=b''):
            if kind == T.OTA_REQUEST and payload[1] == v2.Op.VERIFY_RUNNING:
                device.write = lambda raw: 0
            return send(channel, kind, session, payload)
        with mock.patch.object(client.link, 'send', side_effect=block_measurement), \
                mock.patch.object(ota_esp.time, 'sleep', side_effect=clock.sleep), \
                self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        diag = caught.exception.diagnostics
        self.assertEqual(diag['stage'], 'send')
        self.assertGreater(diag['heartbeat_count_since_start'], 0)
        self.assertEqual(diag['verification']['measurement_attempts'], 1)
        self.assertEqual(diag['verification']['extra_requests'], 0)
        self.assertTrue(client.link.outbox)
        self.assertEqual(device.ops, [])

    def test_transport_disconnect_never_remeasures(self):
        client, device, binding, clock = self.client()
        original = device.response
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
                device.read = mock.Mock(side_effect=OSError('transport disconnected'))
                return
            return original(request, **kwargs)
        device.response = response
        with self.assertRaises(OSError) as caught:
            self.verify(client, binding, clock)
        self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 0)
        self.assertEqual(len(self.requests(device)), 2)

    def test_two_lost_replies_can_recover_within_one_budget(self):
        client, device, binding, clock = self.client()
        self.drop(device, {1, 2})
        result = self.verify(client, binding, clock)
        diag = result['diagnostics']['verification']
        self.assertEqual(diag['extra_requests'], 2)
        self.assertTrue(diag['recovered'])
        self.assertEqual(len(diag['timeout_events']), 2)
        for event in diag['timeout_events']:
            self.assertGreater(event['retry_request_id'], event['request_id'])
        self.assertEqual(diag['timeout_events'][-1]['retry_outcome'], 'response-received')
        self.assert_read_only(device)

    def test_unchanged_boot_busy_retry_never_substitutes_journal_for_measurement(self):
        client, device, binding, clock = self.client()
        original = device.response
        seen = []
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
                seen.append(request)
                if len(seen) == 1:
                    return
                kwargs['result'] = v2.Result.BUSY
            return original(request, **kwargs)
        device.response = response
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock, seconds=2)
        self.assertEqual(caught.exception.state, 'unknown')
        self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 1)
        self.assertNotIn(v2.Op.QUERY, device.ops)
        self.assert_read_only(device)

    def test_expired_absolute_request_deadline_does_not_send(self):
        client, device, binding, clock = self.client()
        with self.assertRaises(ota_esp.Timeout) as caught:
            client.call(v2.Op.VERIFY_RUNNING, binding, device.boot_id, deadline=clock.now)
        self.assertEqual(caught.exception.diagnostics['stage'], 'send')
        self.assertEqual(self.requests(device), [])

    def test_matching_reply_after_absolute_deadline_cannot_succeed(self):
        client, device, binding, clock = self.client(timeout=60)
        deadline = clock.now + 0.1
        poll = client.link.poll
        def delayed():
            frames = poll()
            clock.now = deadline + 0.01
            return frames
        with mock.patch.object(client.link, 'poll', side_effect=delayed), \
                self.assertRaises(ota_esp.Timeout):
            client.call(v2.Op.VERIFY_RUNNING, binding, device.boot_id, deadline=deadline)
        self.assertEqual(len(self.requests(device)), 1)

    def test_deadline_expiring_during_send_prevents_another_request(self):
        client, device, binding, clock = self.client(timeout=60)
        deadline = clock.now + 0.25
        send = client.link.send
        def delayed_send(channel, kind, session, payload=b''):
            result = send(channel, kind, session, payload)
            if kind == T.OTA_REQUEST and payload[1] == v2.Op.VERIFY_RUNNING:
                clock.now = deadline
            return result
        with mock.patch.object(client.link, 'send', side_effect=delayed_send), \
                self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock, deadline=deadline)
        diag = caught.exception.diagnostics['verification']
        self.assertEqual(diag['measurement_attempts'], 1)
        self.assertEqual(diag['extra_requests'], 0)
        self.assertEqual(len(self.requests(device)), 1)
        self.assertEqual(client.timeout, 60)

    def test_missing_retry_evidence_is_never_defaulted_to_healthy(self):
        fields = ('diagnostics', 'stage', 'operation', 'request_id', 'current_epoch',
                  'current_session', 'expected_epoch', 'expected_session',
                  'decoder_errors_before', 'decoder_errors_after', 'response_size_mismatches',
                  'heartbeat_count_since_start', 'latest_heartbeat_age_seconds')
        for missing in fields:
            with self.subTest(missing=missing):
                client, device, binding, clock = self.client()
                self.drop(device, after_ack=False)
                call = client.call
                def incomplete(*args, **kwargs):
                    try:
                        return call(*args, **kwargs)
                    except ota_esp.Timeout as exc:
                        if missing == 'diagnostics':
                            del exc.diagnostics
                        else:
                            exc.diagnostics.pop(missing)
                        raise
                client.call = incomplete
                with self.assertRaises(v2.OutcomeError) as caught:
                    self.verify(client, binding, clock)
                self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 0)
                self.assertEqual(len(self.requests(device)), 1)

    def test_decoder_error_during_timeout_never_remeasures(self):
        client, device, binding, clock = self.client()
        original = device.response
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING:
                device.out.extend(b'\x01\0')
                return
            return original(request, **kwargs)
        device.response = response
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 0)
        self.assertEqual(len(self.requests(device)), 1)

    def test_fresh_retry_response_must_pass_every_identity_and_health_check(self):
        # Field offsets are independently encoded protocol offsets, not host serializers.
        changes = {'version': (0, 1), 'phase': (2, 5), 'refused': (3, 3),
                   'transaction': (8, 99), 'binding_sha': (24, 99), 'size': (56, 0),
                   'received': (60, 0), 'boot_id': (64, 99), 'target': (68, 0),
                   'running': (69, 0), 'boot_slot': (70, 0), 'invalid_state': (71, 3),
                   'error': (72, 1), 'file_sha': (80, 99), 'elf': (112, 99)}
        for change in (*changes, 'journal', 'exact_hash', 'acked', 'challenge', 'short'):
            with self.subTest(change=change):
                client, device, binding, clock = self.client()
                self.drop(device, {1})
                emit = device.emit
                def corrupt(channel, kind, session=0, payload=b'', **overrides):
                    if kind == T.OTA_RESPONSE and payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
                        payload = bytearray(payload)
                        if change in changes:
                            at, value = changes[change]
                            payload[at] = value
                        elif change == 'short':
                            payload = payload[:20]
                        elif change == 'challenge':
                            payload[144:192] = b'health-challenge:87654321'.ljust(48, b'\0')
                        else:
                            payload[76] &= ~{'journal': v2.JOURNAL, 'exact_hash': v2.EXACT_HASH,
                                             'acked': v2.HEALTH_ACKED}[change]
                    return emit(channel, kind, session, payload, **overrides)
                device.emit = corrupt
                with self.assertRaises(v2.OutcomeError) as caught:
                    self.verify(client, binding, clock)
                self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 1)
                self.assertEqual(len(self.requests(device)), 3)
                self.assert_read_only(device)

    def test_pending_retry_response_is_not_confirmed_by_old_valid_measurement(self):
        client, device, binding, clock = self.client()
        self.drop(device, {1})
        emit = device.emit
        def pending(channel, kind, session=0, payload=b'', **overrides):
            if kind == T.OTA_RESPONSE and payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
                payload = bytearray(payload)
                payload[2], payload[71] = v2.Phase.RUNNING_PENDING_VERIFY, 1
            return emit(channel, kind, session, payload, **overrides)
        device.emit = pending
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock, seconds=2)
        self.assertEqual(caught.exception.state, 'pending')
        self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 1)
        self.assert_read_only(device)

    def test_request_limit_is_five_seconds_and_budget_is_two(self):
        client, device, binding, clock = self.client(timeout=60)
        self.drop(device, after_ack=False)
        started = clock.now
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock, seconds=90)
        diag = caught.exception.diagnostics['verification']
        self.assertEqual(len(self.requests(device)), 3)
        self.assertEqual(diag['extra_requests'], 2)
        self.assertEqual([e['timeout_seconds'] for e in diag['timeout_events']], [5, 5, 5])
        self.assertLess(clock.now - started, 15.1)
        self.assertEqual(client.timeout, 60)
        self.assert_read_only(device, ack=0)

    def test_caller_timeout_below_five_is_preserved(self):
        client, device, binding, clock = self.client(timeout=0.25)
        self.drop(device, after_ack=False)
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        events = caught.exception.diagnostics['verification']['timeout_events']
        self.assertEqual([e['timeout_seconds'] for e in events], [0.25] * 3)
        self.assertEqual(client.timeout, 0.25)

    def test_total_deadline_caps_retry_and_is_not_extended(self):
        client, device, binding, clock = self.client(timeout=60)
        self.drop(device, after_ack=False)
        deadline = clock.now + 7
        sent_at = []
        call = client.call
        def timed(*args, **kwargs):
            sent_at.append(clock.now)
            return call(*args, **kwargs)
        client.call = timed
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock, deadline=deadline)
        diag = caught.exception.diagnostics['verification']
        self.assertEqual(len(self.requests(device)), 2)
        self.assertEqual(diag['extra_requests'], 1)
        self.assertEqual(diag['deadline_monotonic'], deadline)
        self.assertEqual(diag['timeout_events'][-1]['stop_reason'], 'deadline-exhausted')
        self.assertLess(diag['timeout_events'][-1]['timeout_seconds'], 2)
        self.assertTrue(all(t < deadline for t in sent_at))
        self.assertLess(clock.now, deadline + 0.03)  # simulated clock ticks per read, not extended budget
        self.assertEqual(client.timeout, 60)

    def test_expired_deadline_sends_nothing(self):
        client, device, binding, clock = self.client()
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock, deadline=clock.now)
        self.assertEqual(caught.exception.diagnostics['verification']['measurement_attempts'], 0)
        self.assertFalse(any(f.channel == C.MAINTENANCE for f in device.writes))

    def test_lost_health_ack_is_not_repeated_or_replaced_by_verify(self):
        client, device, binding, clock = self.client()
        original = device.response
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.HEALTH_ACK:
                return
            return original(request, **kwargs)
        device.response = response
        with self.assertRaises(ota_esp.Timeout) as caught:
            self.verify(client, binding, clock)
        self.assertEqual(caught.exception.diagnostics['operation'], 'HEALTH_ACK')
        self.assertEqual(caught.exception.diagnostics['verification']['extra_requests'], 0)
        self.assertEqual(len(self.requests(device)), 1)
        self.assert_read_only(device)

    def test_caps_is_not_retried(self):
        client, device, binding, clock = self.client()
        device.support = False
        with self.assertRaises(v2.OutcomeError) as caught:
            self.verify(client, binding, clock)
        self.assertEqual(caught.exception.diagnostics['operation'], 'CAPS_QUERY')
        self.assertEqual(caught.exception.diagnostics['verification']['measurement_attempts'], 0)
        self.assert_read_only(device, ack=0)

    def test_failure_after_recovery_keeps_bounded_diagnostics_without_payloads(self):
        client, device, binding, clock = self.client()
        self.drop(device, {1})
        original = device.response
        def response(request, **kwargs):
            if request.payload[1] == v2.Op.VERIFY_RUNNING and len(self.requests(device)) > 5:
                raise OSError('later transport failure')
            return original(request, **kwargs)
        device.response = response
        with self.assertRaises(OSError) as caught:
            self.verify(client, binding, clock)
        diag = caught.exception.diagnostics['verification']
        self.assertEqual(diag['extra_requests'], 1)
        self.assertEqual(len(diag['timeout_events']), 1)
        self.assertEqual(diag['outcome'], 'failed')
        text = json.dumps(caught.exception.diagnostics, allow_nan=False)
        self.assertNotIn(binding.sha256.hex(), text)
        self.assertNotIn('payload', text)
        self.assertLess(len(text), 6000)


if __name__ == '__main__':
    unittest.main()
