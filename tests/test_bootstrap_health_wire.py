"""Run the complete Python pending->health-ACK->VALID verifier against real C."""
import hashlib
from types import SimpleNamespace
import unittest
import test_ota_firmware_wire as wire
from test_ota_v2 import v2


class BootstrapHealthWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire.FirmwareWireTests.setUpClass()

    start_receiver = wire.FirmwareWireTests.start_receiver

    def test_pending_receiver_is_confirmed_only_by_real_host_ack_and_health_hold(self):
        rpc = self.start_receiver('rpc-bootstrap')
        operations = []
        states = []
        class Link:
            epoch = 42
            session = 77
            write_timeout = 5.0
            def __init__(self):
                self.now = 0.0
                self.frames = []
                self.heartbeat_times = []
            def clock(self):
                return self.now
            def sleep(self, seconds):
                # Advance real C's injected local+host health, rather than
                # switching a Python model to VALID after a fixed timestamp.
                self.now += 1.0
                rpc('HEALTH 1000')
                self.heartbeat_times.append(self.now)
            def send(self, channel, kind, session, payload):
                if kind == v2.T.OTA_REQUEST:
                    operations.append(payload[1])
                command = 'CAPS' if kind == v2.T.CAPS_QUERY else f'{int(kind)} {session} '+payload.hex()
                for kind, raw in rpc(command):
                    if kind == v2.T.OTA_RESPONSE:
                        states.append(v2.Response.decode(raw).state)
                    self.frames.append(SimpleNamespace(type=kind, payload=raw, channel=channel,
                        epoch=self.epoch, session=session))
            def poll(self):
                result, self.frames = self.frames, []
                return result
        link = Link()
        client = v2.Client(link, timeout=5, sleep=link.sleep)
        image = bytes((i*13+7)&255 for i in range(2048))
        binding = v2.Binding(bytes([0x37])*16, hashlib.sha256(image).digest(), len(image), 1)
        self.assertEqual(client.capabilities().state, 1)
        evidence = v2.verify_actual(client, binding, bytes(range(32)), deadline=60,
                                   heartbeat_seconds=15, no_journal=True)
        self.assertEqual(states[0], 1)
        self.assertEqual(evidence['image_state'], v2.VALID)
        self.assertTrue(evidence['actual_file_verified'])
        self.assertTrue(evidence['maintenance_health_acknowledged'])
        self.assertGreaterEqual(evidence['heartbeat_seconds'], 15)
        self.assertGreaterEqual(link.now, 35)
        self.assertEqual(operations.count(v2.Op.HEALTH_ACK), 1)
        self.assertNotIn(v2.Op.BEGIN, operations)
        self.assertNotIn(v2.Op.REBOOT, operations)
        self.assertEqual(client.query().phase, v2.Phase.IDLE)


if __name__ == '__main__':
    unittest.main()
