"""Bounded failure diagnostics; byte-level fake USB only, no hardware access."""
import hashlib
import json
import struct
import unittest
from unittest import mock

from test_ota_v2 import fixture, ota_esp, v2, C, T


def valid_then_timeout(*, epoch_change=False):
    """Without fresh heartbeats, even an ACKed VALID reply cannot permit retry."""
    updater, device, data, clock = fixture(no_heartbeats=True)
    device.running = device.boot = 1
    device.flags = v2.EXACT_HASH
    device.measurement_ready = True
    client = updater.open()
    binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
    original = device.response

    def response(request, **kwargs):
        if request.payload[1] == v2.Op.VERIFY_RUNNING and device.health_acknowledged:
            if epoch_change:
                device.reconnect()
            return
        original(request, **kwargs)

    device.response = response
    try:
        v2.verify_actual(client, binding, bytes(range(32)), clock() + 10, no_journal=True)
    except v2.OutcomeError as exc:
        return exc, client, device, clock
    raise AssertionError('a last VALID measurement is not sustained-heartbeat success')


class RequestDiagnosticsTests(unittest.TestCase):
    def open_client(self, **options):
        updater, device, data, clock = fixture(**dict({'no_heartbeats': True}, **options))
        client = updater.open()
        client.request_id = 42
        return client, device, clock

    def test_true_silence_has_zero_frames_and_keeps_original_timeout(self):
        client, device, clock = self.open_client()
        device.response = lambda *args, **kwargs: None
        started = clock.now
        with self.assertRaisesRegex(ota_esp.Timeout, 'timed out waiting for VERIFY_RUNNING; outcome unknown') as caught:
            client.call(v2.Op.VERIFY_RUNNING)
        diag = caught.exception.diagnostics
        self.assertEqual(diag['operation'], 'VERIFY_RUNNING')
        self.assertEqual(diag['request_id'], 43)
        self.assertEqual(diag['expected_epoch'], diag['current_epoch'])
        self.assertEqual(diag['expected_session'], diag['current_session'])
        self.assertEqual(diag['valid_maintenance_frames_received'], 0)
        self.assertEqual(diag['maintenance_frames_examined'], 0)
        self.assertEqual(diag['response_op_mismatches'], 0)
        self.assertEqual(diag['response_id_mismatches'], 0)
        self.assertEqual(diag['decoder_errors_before'], 0)
        self.assertEqual(diag['decoder_errors_after'], 0)
        self.assertEqual(diag['heartbeat_count'], 0)
        self.assertEqual(diag['heartbeat_count_since_start'], 0)
        self.assertIsNone(diag['latest_heartbeat_age_seconds'])
        self.assertEqual(diag['stage'], 'wait')
        self.assertEqual(diag['waiting_reason'], str(caught.exception))
        self.assertEqual(diag['timeout_seconds'], client.timeout)
        self.assertGreaterEqual(diag['started_monotonic'], started)
        self.assertGreaterEqual(diag['elapsed_seconds'], client.timeout)
        self.assertLess(clock.now - started, client.timeout + 0.03)
        self.assertEqual(device.ops, [v2.Op.VERIFY_RUNNING])  # no retry
        self.assertLess(len(json.dumps(diag, allow_nan=False)), 2048)
        self.assertTrue(all(value is None or isinstance(value, (str, int, float))
                            for value in diag.values()))

    def test_heartbeats_without_a_response_are_not_silence_or_success(self):
        client, device, clock = self.open_client(no_heartbeats=False)
        device.response = lambda *args, **kwargs: None
        with self.assertRaises(ota_esp.Timeout) as caught:
            client.query()
        diag = caught.exception.diagnostics
        self.assertEqual(diag['valid_maintenance_frames_received'], 0)
        self.assertGreaterEqual(diag['heartbeat_count_since_start'], 1)
        self.assertGreaterEqual(diag['heartbeat_count'], diag['heartbeat_count_since_start'])
        self.assertGreaterEqual(diag['latest_heartbeat_age_seconds'], 0)
        self.assertLess(diag['latest_heartbeat_age_seconds'], 0.15)

    def test_epoch_change_keeps_expected_and_current_link_identity(self):
        client, device, clock = self.open_client()
        epoch, session = client.link.epoch, client.link.session
        device.response = lambda *args, **kwargs: device.reconnect()
        with self.assertRaisesRegex(ota_esp.Timeout, 'link changed while waiting for QUERY') as caught:
            client.query()
        diag = caught.exception.diagnostics
        self.assertEqual(diag['expected_epoch'], epoch)
        self.assertEqual(diag['current_epoch'], epoch + 1)
        self.assertEqual(diag['expected_session'], session)
        self.assertEqual(diag['current_session'], client.link.session)
        self.assertEqual(diag['operation'], 'QUERY')
        self.assertEqual(device.ops, [v2.Op.QUERY])

    def test_session_change_alone_is_a_failure_with_diagnostics(self):
        client, device, clock = self.open_client()
        epoch, session = client.link.epoch, client.link.session
        poll = client.link.poll

        def changed():
            frames = poll()
            client.link.session = (session + 1) & 0xFFFFFFFF or 1
            return frames

        client.link.poll = changed
        with self.assertRaisesRegex(ota_esp.Timeout, 'link changed') as caught:
            client.query()
        diag = caught.exception.diagnostics
        self.assertEqual(diag['expected_epoch'], epoch)
        self.assertEqual(diag['current_epoch'], epoch)
        self.assertEqual(diag['expected_session'], session)
        self.assertNotEqual(diag['current_session'], session)
        self.assertEqual(device.ops, [v2.Op.QUERY])

    def test_wrong_op_id_size_and_decoder_errors_are_distinct_counts(self):
        client, device, clock = self.open_client()
        client.link.decoder.errors = 3
        original = device.emit
        secret = b'not-a-password-to-record'

        def emit(channel, kind, session=0, payload=b'', **override):
            if kind != T.OTA_RESPONSE:
                return original(channel, kind, session, payload, **override)
            wrong_op = bytearray(payload)
            wrong_op[1] = v2.Op.END
            original(channel, kind, session, wrong_op)
            wrong_id = bytearray(payload)
            struct.pack_into('<I', wrong_id, 4, client.request_id + 1)
            original(channel, kind, session, wrong_id)
            original(channel, kind, session, secret.ljust(512, b'x'))
            # Valid wire frames rejected by Link must not count as accepted.
            original(channel, kind, session + 1, payload)
            original(channel, kind, session, payload, epoch=device.epoch - 1)
            device.out.extend(b'\x01\0')  # malformed wire frame, not an OTA reply

        device.emit = emit
        with self.assertRaises(ota_esp.Timeout) as caught:
            client.query()
        diag = caught.exception.diagnostics
        self.assertEqual(diag['valid_maintenance_frames_received'], 3)
        self.assertEqual(diag['maintenance_frames_examined'], 3)
        self.assertEqual(diag['response_op_mismatches'], 1)
        self.assertEqual(diag['response_id_mismatches'], 1)
        self.assertEqual(diag['response_size_mismatches'], 1)
        self.assertEqual(diag['decoder_errors_before'], 3)
        self.assertEqual(diag['decoder_errors_after'], 4)
        encoded = json.dumps(diag, allow_nan=False)
        self.assertNotIn(secret.decode(), encoded)
        self.assertNotIn('payload', encoded)
        self.assertLess(len(encoded), 2048)
        self.assertEqual(device.ops, [v2.Op.QUERY])

    def test_response_decode_failure_keeps_cause_and_observation_counts(self):
        client, device, clock = self.open_client()
        original = device.emit

        def emit(channel, kind, session=0, payload=b'', **override):
            if kind == T.OTA_RESPONSE:
                payload = bytearray(payload)
                payload[2] = 255  # invalid phase, despite valid wire encoding
            return original(channel, kind, session, payload, **override)

        device.emit = emit
        with self.assertRaisesRegex(v2.OutcomeError, 'invalid v2 response') as caught:
            client.query()
        error = caught.exception
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertEqual(error.diagnostics['valid_maintenance_frames_received'], 1)
        self.assertEqual(error.diagnostics['maintenance_frames_examined'], 1)
        self.assertEqual(error.diagnostics['decoder_errors_after'], 0)
        self.assertNotIn('waiting_reason', error.diagnostics)

    def test_send_timeout_includes_frames_received_while_write_is_blocked(self):
        client, device, clock = self.open_client()
        old_write_timeout = client.link.write_timeout
        device.emit(C.MAINTENANCE, T.CAPS, client.link.session, b'not retained')
        started = clock.now
        with mock.patch.object(device, 'write', return_value=0), \
                mock.patch.object(ota_esp.time, 'sleep', side_effect=clock.sleep), \
                mock.patch.object(client.link, 'send', wraps=client.link.send) as send, \
                self.assertRaisesRegex(ota_esp.Timeout, 'stopped accepting data') as caught:
            client.query()
        diag = caught.exception.diagnostics
        self.assertEqual(diag['stage'], 'send')
        self.assertEqual(diag['request_id'], 43)
        self.assertEqual(diag['valid_maintenance_frames_received'], 1)
        self.assertEqual(diag['maintenance_frames_examined'], 0)
        self.assertEqual(client.link.write_timeout, old_write_timeout)
        self.assertEqual(client.timeout, 0.25)
        self.assertLess(clock.now - started, client.timeout + 0.03)
        self.assertEqual(send.call_count, 1)

    def test_send_and_wait_still_share_one_budget(self):
        client, device, clock = self.open_client()
        device.response = lambda *args, **kwargs: None
        send = client.link.send

        def slow_send(*args, **kwargs):
            clock.sleep(0.15)
            return send(*args, **kwargs)

        started = clock.now
        with mock.patch.object(client.link, 'send', side_effect=slow_send) as sends, \
                self.assertRaises(ota_esp.Timeout):
            client.query()
        self.assertLess(clock.now - started, client.timeout + 0.03)
        self.assertEqual(sends.call_count, 1)

    def test_optional_link_counters_can_be_unavailable(self):
        client, device, clock = self.open_client()
        device.response = lambda *args, **kwargs: None
        del client.link.maintenance_frames_received
        del client.link.decoder.errors
        with self.assertRaises(ota_esp.Timeout) as caught:
            client.query()
        diag = caught.exception.diagnostics
        self.assertIsNone(diag['valid_maintenance_frames_received'])
        self.assertIsNone(diag['decoder_errors_before'])
        self.assertIsNone(diag['decoder_errors_after'])
        json.dumps(diag, allow_nan=False)

    def test_capabilities_wrapper_keeps_diagnostics_and_exception_chain(self):
        client, device, clock = self.open_client(support=False)
        with self.assertRaisesRegex(v2.OutcomeError, 'no v2 capabilities established') as caught:
            client.capabilities()
        exc = caught.exception
        self.assertEqual(exc.state, 'refused')
        self.assertIsInstance(exc.__cause__, ota_esp.Timeout)
        self.assertIn(str(exc.__cause__), str(exc))
        self.assertIs(exc.diagnostics, exc.__cause__.diagnostics)
        self.assertEqual(exc.diagnostics['operation'], 'CAPS_QUERY')
        self.assertIsNone(exc.diagnostics['request_id'])
        self.assertEqual(exc.diagnostics['valid_maintenance_frames_received'], 0)

    def test_annotating_send_timeout_preserves_its_original_cause(self):
        client, device, clock = self.open_client()
        cause = OSError('simulated transport failure')
        try:
            raise ota_esp.Timeout('simulated write timeout') from cause
        except ota_esp.Timeout as exc:
            timeout = exc
        with mock.patch.object(client.link, 'send', side_effect=timeout), \
                self.assertRaises(ota_esp.Timeout) as caught:
            client.query()
        self.assertIs(caught.exception, timeout)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertEqual(caught.exception.diagnostics['stage'], 'send')


class VerificationDiagnosticsTests(unittest.TestCase):
    def test_valid_measurement_then_timeout_is_unknown_not_last_measurement_success(self):
        exc, client, device, clock = valid_then_timeout()
        self.assertEqual(exc.state, 'unknown')
        self.assertEqual(exc.response.state, v2.VALID)
        self.assertTrue(exc.response.flags & v2.HEALTH_ACKED)
        self.assertTrue(str(exc).startswith('actual-running measurement timed out: '))
        self.assertIn('timed out waiting for VERIFY_RUNNING; outcome unknown', str(exc))
        self.assertIsInstance(exc.__cause__, ota_esp.Timeout)
        self.assertIs(exc.diagnostics, exc.__cause__.diagnostics)
        self.assertEqual(exc.diagnostics['operation'], 'VERIFY_RUNNING')
        self.assertEqual(exc.diagnostics['request_id'], client.request_id)
        self.assertEqual(exc.diagnostics['heartbeat_count'], 0)
        recovery = exc.diagnostics['verification']
        self.assertEqual(recovery['measurement_attempts'], 2)
        self.assertEqual(recovery['extra_requests'], 0)
        self.assertEqual(recovery['timeout_events'][0]['stop_reason'], 'no-fresh-heartbeat')
        self.assertFalse(recovery['recovered'])
        self.assertEqual(device.ops, [v2.Op.VERIFY_RUNNING, v2.Op.HEALTH_ACK, v2.Op.VERIFY_RUNNING])
        self.assertEqual(client.timeout, 0.25)

    def test_verification_wrapper_retains_epoch_change_reason(self):
        exc, client, device, clock = valid_then_timeout(epoch_change=True)
        self.assertEqual(exc.state, 'unknown')
        self.assertIn('actual-running measurement timed out: link changed while waiting for VERIFY_RUNNING', str(exc))
        self.assertNotEqual(exc.diagnostics['expected_epoch'], exc.diagnostics['current_epoch'])
        self.assertIs(exc.diagnostics, exc.__cause__.diagnostics)
        self.assertEqual(exc.diagnostics['verification']['extra_requests'], 0)
        self.assertEqual(exc.diagnostics['verification']['timeout_events'][0]['stop_reason'], 'link-changed')
        self.assertEqual(device.ops.count(v2.Op.VERIFY_RUNNING), 2)

    def test_default_heartbeat_window_remains_six_seconds(self):
        updater, device, data, clock = fixture()
        device.running = device.boot = 1
        device.flags = v2.EXACT_HASH
        device.measurement_ready = True
        client = updater.open()
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        # Many valid replies/heartbeats within three seconds still cannot pass.
        with self.assertRaisesRegex(v2.OutcomeError, 'sustained heartbeat deadline expired'):
            v2.verify_actual(client, binding, bytes(range(32)), clock() + 3, no_journal=True)
        self.assertGreater(len(client.link.heartbeat_times), 3)
        # A full fresh six-second span with >=3 heartbeats can succeed.
        result = v2.verify_actual(client, binding, bytes(range(32)), clock() + 10, no_journal=True)
        self.assertEqual(result['state'], 'confirmed')
        self.assertGreaterEqual(result['heartbeat_seconds'], 6.0)
        self.assertGreaterEqual(result['heartbeat_count'], 3)
        self.assertNotIn(v2.Op.BEGIN, device.ops)


if __name__ == '__main__':
    unittest.main()
