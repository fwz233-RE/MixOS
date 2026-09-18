"""Python v2 client against compiled production C, not a Python device model."""
import hashlib
import re
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from _support import ROOT, host_command, posix_path
import test_ota_firmware as firmware_tests
sys.path.insert(0, str(ROOT/'tools'))
import ota_v2 as v2


class FirmwareWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        firmware_tests.OtaFirmwareTests.setUpClass()

    def start_receiver(self, mode='rpc'):
        process = subprocess.Popen(host_command([posix_path(firmware_tests.OtaFirmwareTests.exe), mode]),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        def cleanup():
            process.stdin.close()
            process.wait(timeout=20)
            errors = process.stderr.read()
            process.stdout.close(); process.stderr.close()
            self.assertEqual(process.returncode, 0, errors)
        self.addCleanup(cleanup)
        def rpc(line):
            process.stdin.write(line+'\n'); process.stdin.flush()
            frames = []
            while True:
                result = process.stdout.readline().strip()
                self.assertTrue(result, 'C receiver exited unexpectedly')
                if result == 'READY':
                    return frames
                kind, payload = result.split(' ', 1)
                frames.append((int(kind), bytes.fromhex(payload)))
        return rpc

    def ack_measurement(self, rpc, measurement, binding, request_id, expected_elf=bytes(range(32))):
        # Deliberately encode the new fixed-length contract independently of
        # host implementation, so firmware tests do not modify ota_v2.py.
        self.assertEqual(measurement.binding, binding)
        self.assertEqual(measurement.stored_sha256, binding.sha256)
        self.assertEqual(measurement.received, binding.size)
        self.assertEqual(measurement.elf_sha256, expected_elf)
        self.assertEqual((measurement.running, measurement.boot), (binding.target, binding.target))
        self.assertTrue(measurement.flags & 0x08)
        token = re.fullmatch(r'health-challenge:([0-9a-f]{8})', measurement.message)
        self.assertIsNotNone(token)
        payload = struct.pack('<BBHI16s32sIIB3xI', 2, 8, 0, request_id,
                              binding.transaction, expected_elf, binding.size,
                              measurement.boot_id, binding.target, int(token[1], 16))
        replies = rpc('79 77 '+payload.hex())
        answer = [v2.Response.decode(p) for kind, p in replies if kind == 81][-1]
        self.assertEqual(answer.op, 8)
        self.assertEqual(answer.request_id, request_id)
        self.assertEqual(answer.result, v2.Result.OK)
        self.assertEqual(answer.binding, binding)
        self.assertEqual(answer.elf_sha256, expected_elf)
        self.assertTrue(answer.flags & 0x10)
        return answer

    def test_real_worker_binding_commit_reboot_and_confirmation(self):
        rpc = self.start_receiver()
        image = bytes((i*13+7)&255 for i in range(2048))
        binding = v2.Binding(bytes([0xa5])*16, hashlib.sha256(image).digest(), len(image), 1)
        request_id = 100
        caps = v2.Capabilities.decode(rpc('CAPS')[0][1]); caps.require()
        self.assertEqual(caps.running, 0); self.assertEqual(caps.protected, 1)
        def call(op, boot=None):
            nonlocal request_id
            request_id += 1
            payload = v2.request_payload(op, request_id, binding, caps.boot_id if boot is None else boot)
            frames = rpc('79 77 '+payload.hex())
            answer = [v2.Response.decode(p) for kind, p in frames if kind == 81][-1]
            self.assertEqual(answer.request_id, request_id)
            self.assertEqual(answer.binding, binding)
            return answer
        self.assertEqual(call(v2.Op.BEGIN).phase, v2.Phase.RECEIVING)
        for at in range(0, len(image), 508):
            data = at.to_bytes(4, 'little')+image[at:at+508]
            ack = rpc('69 77 '+data.hex())[-1]
            self.assertEqual(ack[0], 70)
            self.assertEqual(int.from_bytes(ack[1], 'little'), min(at+508, len(image)))
        end = call(v2.Op.END)
        self.assertEqual(end.phase, v2.Phase.BOOT_SELECTED)
        self.assertEqual(end.stored_sha256, binding.sha256)
        self.assertEqual(call(v2.Op.END).phase, v2.Phase.BOOT_SELECTED)
        self.assertEqual(call(v2.Op.REBOOT).phase, v2.Phase.REBOOT_REQUESTED)
        rpc('BOOT')
        self.assertEqual(call(v2.Op.QUERY).phase, v2.Phase.RUNNING_PENDING_VERIFY)
        self.assertEqual(call(v2.Op.REBOOT).phase, v2.Phase.RUNNING_PENDING_VERIFY)
        rpc('HEALTH 20500')
        pending = call(v2.Op.QUERY)
        self.assertEqual(pending.state, 1)
        measured = call(v2.Op.VERIFY_RUNNING, pending.boot_id)
        request_id += 1
        self.ack_measurement(rpc, measured, binding, request_id)
        rpc('HEALTH 20500')
        confirmed = call(v2.Op.QUERY)
        self.assertEqual(confirmed.phase, v2.Phase.CONFIRMED)
        self.assertEqual(confirmed.state, v2.VALID)
        actual = call(v2.Op.VERIFY_RUNNING, confirmed.boot_id)
        self.assertEqual(actual.result, v2.Result.OK)
        self.assertEqual(actual.stored_sha256, binding.sha256)
        self.assertTrue(actual.flags & v2.EXACT_HASH)

    def test_real_host_verifier_accepts_bootstrap_without_inventing_journal(self):
        rpc = self.start_receiver('rpc-bootstrap')
        # Transport timing/heartbeats are injected here; requests and responses,
        # running flash hash, health confirmation and journal are production C.
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
            def sleep(self, unused):
                self.now += 1.0
                self.heartbeat_times.append(self.now)
            def send(self, channel, kind, session, payload):
                command = 'CAPS' if kind == v2.T.CAPS_QUERY else f'{int(kind)} {session} '+payload.hex()
                self.frames.extend(SimpleNamespace(type=t, payload=p, channel=channel,
                                                   epoch=self.epoch, session=session)
                                   for t, p in rpc(command))
            def poll(self):
                result, self.frames = self.frames, []
                return result
        link = Link()
        client = v2.Client(link, timeout=5, sleep=link.sleep)
        image = bytes((i*13+7)&255 for i in range(2048))
        binding = v2.Binding(bytes([0x37])*16, hashlib.sha256(image).digest(), len(image), 1)
        caps = client.capabilities()
        pending = client.call(v2.Op.VERIFY_RUNNING, binding, caps.boot_id)
        self.assertEqual(pending.state, 1)
        self.assertEqual(pending.received, len(image))
        self.assertEqual(pending.phase, v2.Phase.RUNNING_PENDING_VERIFY)
        self.assertEqual(client.query().phase, v2.Phase.IDLE)
        rpc('HEALTH 20500')
        pending = client.call(v2.Op.VERIFY_RUNNING, binding, caps.boot_id)
        self.assertEqual(pending.state, 1, 'measurement response alone must not confirm')
        ack_id = (client.request_id + 1) & 0xffffffff or 1
        self.ack_measurement(rpc, pending, binding, ack_id)
        client.request_id = ack_id
        rpc('HEALTH 20500')
        evidence = v2.verify_actual(client, binding, bytes(range(32)), deadline=20, no_journal=True)
        self.assertTrue(evidence['actual_file_verified'])
        self.assertEqual(evidence['evidence_kind'], 'running-measurement')
        self.assertEqual(evidence['state'], 'confirmed')
        self.assertEqual(client.query().phase, v2.Phase.IDLE)


if __name__ == '__main__':
    unittest.main()
