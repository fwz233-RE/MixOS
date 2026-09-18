"""Host health ACK adversarial tests; all transports and clocks are simulated."""
import hashlib
import struct
import unittest
from test_ota_v2 import fixture, v2


class HealthAckTests(unittest.TestCase):
    def observe(self, transform=None):
        updater, device, data, clock = fixture()
        device.running = device.boot = 1
        device.state = 1
        device.flags = 0
        device.valid_at = clock() + 0.2
        if transform:
            original = device.emit
            def emit(channel, kind, session=0, payload=b'', **overrides):
                if kind == v2.T.OTA_RESPONSE:
                    payload = transform(bytearray(payload), device)
                    if payload is None:
                        return
                return original(channel, kind, session, payload, **overrides)
            device.emit = emit
        client = updater.open()
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        return client, binding, device, clock

    def verify(self, client, binding, clock):
        return v2.verify_actual(client, binding, bytes(range(32)), clock() + 3,
                                heartbeat_seconds=0.25, no_journal=True)

    def test_ack_is_after_checked_measurement_and_before_valid(self):
        client, binding, device, clock = self.observe()
        result = self.verify(client, binding, clock)
        self.assertTrue(result['maintenance_health_acknowledged'])
        self.assertEqual(device.ops.count(v2.Op.HEALTH_ACK), 1)
        self.assertLess(device.ops.index(v2.Op.VERIFY_RUNNING), device.ops.index(v2.Op.HEALTH_ACK))
        frame = next(f for f in device.writes if f.type == v2.T.OTA_REQUEST and f.payload[1] == 8)
        self.assertEqual(len(frame.payload), 72)
        self.assertEqual(frame.payload[8:24], binding.transaction)
        self.assertEqual(frame.payload[24:56], bytes(range(32)))
        self.assertEqual(struct.unpack_from('<I', frame.payload, 68)[0], device.health_challenge)
        self.assertNotIn(v2.Op.BEGIN, device.ops)
        self.assertNotIn(v2.Op.REBOOT, device.ops)

    def test_wrong_file_elf_size_slot_or_boot_never_sends_ack(self):
        for offset in (24, 56, 64, 68, 69, 70, 80, 112):
            def corrupt(payload, device):
                if payload[1] == v2.Op.VERIFY_RUNNING and payload[3] == v2.Result.OK:
                    payload[offset] ^= 1
                return payload
            client, binding, device, clock = self.observe(corrupt)
            with self.subTest(offset=offset), self.assertRaises(v2.OutcomeError):
                self.verify(client, binding, clock)
            self.assertNotIn(v2.Op.HEALTH_ACK, device.ops)

    def test_missing_or_malformed_challenge_never_sends_ack(self):
        for token in (b'', b'health-challenge:00000000', b'health-challenge:123',
                      b'health-challenge:zzzzzzzz', b'health-challenge:12345678junk'):
            def corrupt(payload, device):
                if payload[1] == v2.Op.VERIFY_RUNNING and payload[3] == 0:
                    payload[144:192] = token.ljust(48, b'\0')
                return payload
            client, binding, device, clock = self.observe(corrupt)
            with self.subTest(token=token), self.assertRaises(v2.OutcomeError):
                self.verify(client, binding, clock)
            self.assertNotIn(v2.Op.HEALTH_ACK, device.ops)

    def test_lost_ack_reply_is_unknown_without_repeated_ack_or_reflash(self):
        client, binding, device, clock = self.observe(
            lambda payload, device: None if payload[1] == v2.Op.HEALTH_ACK else payload)
        with self.assertRaises(v2.Timeout):
            self.verify(client, binding, clock)
        self.assertEqual(device.ops.count(v2.Op.HEALTH_ACK), 1)
        self.assertNotIn(v2.Op.BEGIN, device.ops)
        self.assertNotIn(v2.Op.REBOOT, device.ops)

    def test_wrong_ack_response_fields_or_busy_cannot_confirm(self):
        for offset in (3, 8, 24, 56, 64, 69, 70, 80, 112):
            def corrupt(payload, device):
                if payload[1] == v2.Op.HEALTH_ACK:
                    payload[offset] ^= 1
                return payload
            client, binding, device, clock = self.observe(corrupt)
            with self.subTest(offset=offset), self.assertRaises(v2.OutcomeError):
                self.verify(client, binding, clock)
            self.assertEqual(device.ops.count(v2.Op.HEALTH_ACK), 1)

    def test_ack_flag_or_challenge_change_after_ack_refuses(self):
        for change in ('flag', 'token'):
            def corrupt(payload, device):
                if payload[1] == v2.Op.HEALTH_ACK:
                    if change == 'flag': payload[76] &= ~v2.HEALTH_ACKED
                    else: payload[144:192] = b'health-challenge:87654321'.ljust(48, b'\0')
                return payload
            client, binding, device, clock = self.observe(corrupt)
            with self.subTest(change=change), self.assertRaises(v2.OutcomeError):
                self.verify(client, binding, clock)

    def test_old_receiver_without_health_ack_is_rejected_before_writes(self):
        updater, device, data, clock = fixture(features=15)
        client = updater.open()
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        with self.assertRaises(v2.OutcomeError): self.verify(client, binding, clock)
        self.assertEqual(device.ops, [])

    def test_challenge_reserved_for_ack_and_never_zero(self):
        for op, challenge in ((v2.Op.HEALTH_ACK, 0), (v2.Op.VERIFY_RUNNING, 1),
                              (v2.Op.BEGIN, 1), (v2.Op.HEALTH_ACK, -1),
                              (v2.Op.HEALTH_ACK, 2**32), (v2.Op.HEALTH_ACK, True)):
            with self.subTest(op=op, challenge=challenge), self.assertRaises(ValueError):
                v2.request_payload(op, 1, health_challenge=challenge)


if __name__ == '__main__':
    unittest.main()
