"""V2 host wire/state-machine tests using a byte-level in-memory USB receiver.

The fixture explicitly packs documented offsets; it does not reuse host response
serialization. All clocks and transports are fake; nothing touches hardware.
"""
import hashlib
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import ota_esp
import ota_v2 as v2
import mixos_esp_update as native
from protocol import Channel as C, Type as T, Frame, Decoder
from test_mixos_esp_update import make_image, manifest


class Clock:
    def __init__(self):
        self.now = 1.0
    def __call__(self):
        self.now += 0.001
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Device:
    def __init__(self, clock, data, *, support=True, features=31, drop_end=0,
                 drop_begin=0, drop_reboot=0, hash_mismatch=False, elf_mismatch=False,
                 stay_pending=False, no_heartbeats=False, protected=1):
        self.clock, self.data = clock, data
        self.support, self.features = support, features
        self.drop_end, self.drop_begin, self.drop_reboot = drop_end, drop_begin, drop_reboot
        self.hash_mismatch, self.elf_mismatch = hash_mismatch, elf_mismatch
        self.stay_pending, self.no_heartbeats = stay_pending, no_heartbeats
        self.protected = protected
        self.epoch, self.boot_id, self.sequence = 0x100, 55, 0
        self.decoder, self.out = Decoder(), bytearray()
        self.online = False
        self.binding = v2.EMPTY
        self.session = 0
        self.received = bytearray()
        self.running = self.boot = 0
        self.state = 2
        self.phase = v2.Phase.IDLE
        self.flags = 1
        self.ops = []
        self.request_ids = []
        self.writes = []
        self.ping_time = 0
        self.valid_at = 0
        self.verify_at = 0
        self.measurement_ready = False
        self.measured_binding = None
        self.health_challenge = 0x12345678
        self.health_acknowledged = False
        self.verify_never_accepted = False
        self.reboot_at = 0
        self.closed = 0
        self.drop_data = set()
        self.forge_ack = False
        self.reset_data = False
        self.wrong_responses = False
        self.hello()

    def emit(self, channel, kind, session=0, payload=b'', **override):
        fields = dict(channel=channel, type=kind, epoch=self.epoch, session=session,
                      sequence=self.sequence, payload=payload)
        fields.update(override)
        self.out += Frame(**fields).encode()
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def hello(self):
        self.emit(C.CONTROL, T.HELLO, payload=struct.pack('<HH', 512, 4096))

    def reconnect(self):
        self.online = False
        self.health_acknowledged = False
        self.measured_binding = None
        self.health_challenge = (self.health_challenge + 1) & 0xFFFFFFFF or 1
        self.epoch += 1
        self.sequence = 0
        self.out.clear()
        self.hello()

    def close(self):
        self.closed += 1

    def read(self, maximum):
        now = self.clock()
        if self.reboot_at and now >= self.reboot_at:
            self.reboot_at = 0
            self.running, self.boot = self.binding.target, self.binding.target
            self.data = bytes(self.received)
            self.state = 1
            self.phase = v2.Phase.RUNNING_PENDING_VERIFY
            self.boot_id += 1
            self.valid_at = now + 0.4
            self.reconnect()
        if self.verify_at and now >= self.verify_at:
            self.verify_at = 0
            self.measurement_ready = True
            self.flags |= v2.EXACT_HASH
            if self.binding == v2.EMPTY:
                self.phase = v2.Phase.IDLE
            else:
                self.phase = v2.Phase.CONFIRMED if self.state == 2 else v2.Phase.RUNNING_PENDING_VERIFY
        if (self.valid_at and now >= self.valid_at and not self.stay_pending
                and self.health_acknowledged):
            self.valid_at = 0
            self.state = 2
            self.phase = v2.Phase.CONFIRMED
        if self.online and not self.no_heartbeats and now - self.ping_time >= 0.12:
            self.ping_time = now
            self.emit(C.CONTROL, T.PING)
        data, self.out = bytes(self.out[:maximum]), self.out[maximum:]
        return data

    def write(self, raw):
        for frame in self.decoder.feed(raw):
            self.handle(frame)
        return len(raw)

    def response(self, request, *, result=0, message='', binding=None):
        p = bytearray(192)
        p[0:4] = bytes((2, request.payload[1], self.phase, result))
        p[4:8] = request.payload[4:8]
        measuring = request.payload[1] in (v2.Op.VERIFY_RUNNING, v2.Op.HEALTH_ACK) and result == 0
        b = binding or (self.measured_binding if measuring and request.payload[1] == v2.Op.HEALTH_ACK else
                       v2.Binding(bytes(request.payload[8:24]), bytes(request.payload[24:56]),
                       struct.unpack_from('<I', request.payload, 56)[0], request.payload[64])
                       if measuring else self.binding)
        p[8:24], p[24:56] = b.transaction, b.sha256
        struct.pack_into('<III', p, 56, b.size, b.size if measuring else len(self.received), self.boot_id)
        p[68:72] = bytes((b.target, self.running, self.boot, self.state))
        flags = self.flags | (4 if self.protected else 0)
        if measuring and self.features & v2.HEALTH_ACK_FEATURE:
            flags |= v2.HEALTH_CHALLENGE | (v2.HEALTH_ACKED if self.health_acknowledged else 0)
            message = f'health-challenge:{self.health_challenge:08x}'
        struct.pack_into('<iI', p, 72, 0, flags)
        p[80:112] = (b'\xff' * 32 if self.hash_mismatch else
                     hashlib.sha256(self.data[:b.size]).digest() if measuring else b.sha256)
        p[112:144] = b'\xfe' * 32 if self.elf_mismatch else bytes(range(32))
        msg = message.encode()[:47]
        p[144:144 + len(msg)] = msg
        if self.wrong_responses:
            self.wrong_responses = False
            for change in ('request', 'opcode', 'session', 'epoch'):
                wrong = bytearray(p)
                overrides = {}
                session = request.session
                if change == 'request':
                    wrong[4:8] = b'\x00' * 4
                elif change == 'opcode':
                    wrong[1] = 99
                elif change == 'session':
                    session += 1
                else:
                    overrides['epoch'] = self.epoch - 1
                self.emit(C.MAINTENANCE, T.OTA_RESPONSE, session, bytes(wrong), **overrides)
        self.emit(C.MAINTENANCE, T.OTA_RESPONSE, request.session, bytes(p))

    def handle(self, frame):
        self.writes.append(frame)
        if frame.type == T.HELLO_ACK:
            self.online = True
            return
        if frame.epoch != self.epoch or not self.online:
            return
        if frame.type == T.CAPS_QUERY and self.support:
            p = bytearray(36)
            p[:2] = bytes((2, self.features))
            struct.pack_into('<HH', p, 2, 508, 8)
            struct.pack_into('<I', p, 8, self.boot_id)
            p[12:15] = bytes((self.running, self.boot, self.state))
            struct.pack_into('<IIIII', p, 16, 0x10000, 0x1F0000, 0x610000, 0x1F0000, self.protected)
            self.emit(C.MAINTENANCE, T.CAPS, frame.session, p)
        elif frame.type == T.OTA_IDENTIFY:
            p = bytearray(136)
            p[:4] = bytes((1, self.running, self.state, 3))
            struct.pack_into('<I', p, 4, (0x10000, 0x610000)[self.running])
            p[8:40] = bytes(range(32))
            self.emit(C.MAINTENANCE, T.OTA_IDENTITY, frame.session, p)
        elif frame.type == T.OTA_DATA:
            if self.reset_data:
                self.reset_data = False
                self.phase = v2.Phase.ABORTED
                self.reconnect()
                return
            if frame.session != self.session:
                return
            position = struct.unpack_from('<I', frame.payload)[0]
            if position in self.drop_data:
                self.drop_data.remove(position)
                return
            if position == len(self.received):
                self.received += frame.payload[4:]
            ack = len(self.received) + 999999 if self.forge_ack else len(self.received)
            self.emit(C.MAINTENANCE, T.OTA_ACK, frame.session, struct.pack('<I', ack))
        elif frame.type == T.OTA_REQUEST:
            p = frame.payload
            op = v2.Op(p[1])
            binding = v2.Binding(bytes(p[8:24]), bytes(p[24:56]), struct.unpack_from('<I', p, 56)[0], p[64])
            self.ops.append(op)
            self.request_ids.append(struct.unpack_from('<I', p, 4)[0])
            if op == v2.Op.BEGIN:
                if self.binding.transaction != binding.transaction:
                    self.received.clear()
                self.binding = binding
                self.session = frame.session
                self.phase = v2.Phase.RECEIVING
                if self.drop_begin:
                    self.drop_begin -= 1
                    return
            elif op == v2.Op.END:
                if len(self.received) != self.binding.size:
                    return self.response(frame, result=3, message='incomplete')
                self.phase = v2.Phase.BOOT_SELECTED
                self.boot = self.binding.target
                self.flags |= v2.EXACT_HASH
                self.measurement_ready = False
                if self.drop_end:
                    self.drop_end -= 1
                    return
            elif op == v2.Op.REBOOT:
                if self.running != self.binding.target:
                    self.phase = v2.Phase.REBOOT_REQUESTED
                    self.reboot_at = self.clock() + 0.02
                if self.drop_reboot:
                    self.drop_reboot -= 1
                    return
            elif op == v2.Op.VERIFY_RUNNING:
                if self.verify_never_accepted:
                    return self.response(frame, result=1, message='worker queue full')
                if not self.measurement_ready:
                    self.phase = v2.Phase.VERIFYING_RUNNING
                    self.flags &= ~v2.EXACT_HASH
                    if not self.verify_at:
                        self.verify_at = self.clock() + 0.03
                    return self.response(frame, result=1)
                if self.measured_binding != binding:
                    self.measured_binding = binding
                    self.health_acknowledged = False
                    self.health_challenge = (self.health_challenge + 1) & 0xFFFFFFFF or 1
            elif op == v2.Op.HEALTH_ACK:
                measured = self.measured_binding
                if (measured is None or binding.transaction != measured.transaction
                        or binding.size != measured.size or binding.target != measured.target
                        or binding.sha256 != bytes(range(32))
                        or struct.unpack_from('<I', p, 60)[0] != self.boot_id
                        or struct.unpack_from('<I', p, 68)[0] != self.health_challenge):
                    return self.response(frame, result=3, message='invalid health acknowledgement')
                self.health_acknowledged = True
            elif op == v2.Op.RELEASE_BASELINE:
                self.protected = 0
            self.response(frame)


def fixture(**options):
    clock = Clock()
    data = make_image(6500)
    device = Device(clock, data, **options)
    connects = []
    def connect():
        if connects:
            device.reconnect()
        link = ota_esp.Link(device, clock=clock, random_sessions=True, sleep=clock.sleep)
        connects.append(link)
        link.handshake(1)
        return link
    updater = v2.Updater(connect, timeout=0.25, health_timeout=3,
                         heartbeat_seconds=0.25, clock=clock, sleep=clock.sleep)
    return updater, device, data, clock


class TransactionTests(unittest.TestCase):
    def test_complete_requires_opcode6_pending_to_valid_and_sustained_heartbeat(self):
        updater, device, data, _ = fixture()
        events = []
        result = updater.run(data, manifest(data), events.append)
        self.assertEqual(result['state'], 'confirmed')
        self.assertTrue(result['actual_file_verified'])
        self.assertGreaterEqual(result['heartbeat_seconds'], 0.25)
        self.assertEqual(result['image_state'], 2)
        self.assertIn(v2.Op.VERIFY_RUNNING, device.ops)
        self.assertNotIn(v2.Op.RELEASE_BASELINE, device.ops)
        self.assertEqual(bytes(device.received), data)
        self.assertEqual(events[0]['event'], 'transaction-bound')
        self.assertEqual(len(set(device.request_ids)), len(device.request_ids))
        self.assertTrue(all(f.session != 0 for f in device.writes if f.channel == C.MAINTENANCE))

    def test_missing_capabilities_and_missing_features_never_begin(self):
        for options in ({'support': False}, {'features': 7}):
            updater, device, data, _ = fixture(**options)
            with self.assertRaises(v2.OutcomeError):
                updater.run(data, manifest(data), lambda event: None)
            self.assertNotIn(v2.Op.BEGIN, device.ops)
            self.assertFalse(device.received)

    def test_lost_end_queries_commit_without_second_begin_or_reflash(self):
        updater, device, data, _ = fixture(drop_end=1)
        result = updater.run(data, manifest(data), lambda event: None)
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(device.ops.count(v2.Op.BEGIN), 1)
        self.assertEqual(device.ops.count(v2.Op.END), 1)
        self.assertIn(v2.Op.QUERY, device.ops[device.ops.index(v2.Op.END) + 1:])

    def test_lost_begin_reuses_binding_and_session_with_fresh_request(self):
        updater, device, data, _ = fixture(drop_begin=1)
        self.assertEqual(updater.run(data, manifest(data), lambda event: None)['state'], 'confirmed')
        requests = [f for f in device.writes if f.type == T.OTA_REQUEST and f.payload[1] == 1]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].payload[8:], requests[1].payload[8:])
        self.assertEqual(requests[0].session, requests[1].session)
        self.assertNotEqual(requests[0].payload[4:8], requests[1].payload[4:8])
        self.assertNotEqual(requests[0].sequence, requests[1].sequence)

    def test_lost_reboot_reply_and_fast_reenumeration_need_no_absent_node(self):
        updater, device, data, _ = fixture(drop_reboot=1)
        self.assertEqual(updater.run(data, manifest(data), lambda event: None)['state'], 'confirmed')
        self.assertEqual(device.boot_id, 56)

    def test_hash_elf_pending_and_no_heartbeat_are_not_success(self):
        cases = [({'hash_mismatch': True}, 'mismatch'), ({'elf_mismatch': True}, 'mismatch'),
                 ({'stay_pending': True}, 'pending'), ({'no_heartbeats': True}, 'unknown')]
        for options, state in cases:
            with self.subTest(options=options):
                updater, device, data, _ = fixture(**options)
                with self.assertRaises(v2.OutcomeError) as exc:
                    updater.run(data, manifest(data), lambda event: None)
                self.assertEqual(exc.exception.state, state)
                self.assertEqual(device.ops.count(v2.Op.BEGIN), 1)

    def test_ack_ahead_of_sent_and_epoch_reset_abort_without_blind_retry(self):
        for setting in ('forge_ack', 'reset_data'):
            updater, device, data, _ = fixture()
            setattr(device, setting, True)
            with self.assertRaises(v2.OutcomeError):
                updater.run(data, manifest(data), lambda event: None)
            self.assertEqual(device.ops.count(v2.Op.BEGIN), 1)
            self.assertNotIn(v2.Op.END, device.ops)

    def test_dropped_data_uses_query_to_retransmit_bounded_offset(self):
        updater, device, data, _ = fixture()
        device.drop_data = {508 * 3, 508 * 9}
        self.assertEqual(updater.run(data, manifest(data), lambda event: None)['state'], 'confirmed')
        self.assertEqual(bytes(device.received), data)

    def test_wrong_epoch_session_opcode_and_request_id_never_match(self):
        updater, device, data, _ = fixture()
        device.wrong_responses = True
        self.assertEqual(updater.run(data, manifest(data), lambda event: None)['state'], 'confirmed')

    def test_resume_receiving_refuses_new_begin(self):
        updater, device, data, _ = fixture()
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        device.binding, device.phase = binding, v2.Phase.RECEIVING
        with self.assertRaisesRegex(v2.OutcomeError, 'cannot be resumed'):
            updater.run(data, manifest(data), lambda event: None,
                        saved={'binding': binding.record(), 'source_boot_id': 55})
        self.assertNotIn(v2.Op.BEGIN, device.ops)

    def test_resume_selected_observes_same_binding_and_does_not_send_data(self):
        updater, device, data, _ = fixture()
        binding = v2.Binding(bytes(range(16)), hashlib.sha256(data).digest(), len(data), 1)
        device.binding, device.phase = binding, v2.Phase.BOOT_SELECTED
        device.received = bytearray(data)
        device.boot = 1
        result = updater.run(data, manifest(data), lambda event: None,
                             saved={'binding': binding.record(), 'source_boot_id': 55})
        self.assertEqual(result['state'], 'confirmed')
        self.assertNotIn(v2.Op.BEGIN, device.ops)
        self.assertFalse(any(f.type == T.OTA_DATA for f in device.writes))

    def test_protected_baseline_never_released_without_explicit_flag(self):
        updater, device, data, _ = fixture()
        device.running = device.boot = 1
        with self.assertRaisesRegex(v2.OutcomeError, 'allow-replace-baseline'):
            updater.run(data, manifest(data), lambda event: None)
        self.assertNotIn(v2.Op.RELEASE_BASELINE, device.ops)
        self.assertNotIn(v2.Op.BEGIN, device.ops)

    def test_guard_release_requires_independent_replacement_verification(self):
        updater, device, data, _ = fixture()
        old = make_image(4096)
        device.running = device.boot = 1
        device.phase = v2.Phase.CONFIRMED
        device.binding = v2.Binding(bytes(range(16)), hashlib.sha256(old).digest(), len(old), 1)
        device.received = bytearray(old)
        value = manifest(data)
        value['protected_baseline'] = {'image_sha256': native.BASELINE_SHA,
                                       'image_bytes': native.BASELINE_SIZE,
                                       'elf_sha256': native.BASELINE_ELF, 'slot': 'ota_0'}
        value['verified_replacement'] = {'image_sha256': hashlib.sha256(old).hexdigest(),
                                         'image_bytes': len(old), 'elf_sha256': bytes(range(32)).hex(),
                                         'slot': 'ota_1'}
        device.data = old
        result = updater.run(data, value, lambda event: None, allow_replace_baseline=True)
        self.assertEqual(result['state'], 'confirmed')
        self.assertLess(device.ops.index(v2.Op.VERIFY_RUNNING), device.ops.index(v2.Op.RELEASE_BASELINE))
        self.assertLess(device.ops.index(v2.Op.RELEASE_BASELINE), device.ops.index(v2.Op.BEGIN))
        self.assertEqual(device.running, 0)

    def test_busy_opcode6_plus_verified_journal_never_proves_actual_file(self):
        updater, device, data, _ = fixture()
        device.verify_never_accepted = True
        with self.assertRaises(v2.OutcomeError) as exc:
            updater.run(data, manifest(data), lambda event: None)
        self.assertEqual(exc.exception.state, 'unknown')
        self.assertGreater(device.ops.count(v2.Op.VERIFY_RUNNING), 1)

    def test_manifest_live_layout_mismatch_refuses_before_begin(self):
        updater, device, data, _ = fixture()
        value = manifest(data)
        value['layout'] = {'ota_0': {'address': 1, 'size': 2}, 'ota_1': {'address': 3, 'size': 4}}
        with self.assertRaisesRegex(v2.OutcomeError, 'layout'):
            updater.run(data, value, lambda event: None)
        self.assertNotIn(v2.Op.BEGIN, device.ops)


class BaselineGuardCompatibilityTests(unittest.TestCase):
    def setUp(self):
        sha256, elf, size = v2.LEGACY_GUARD_RECEIVER
        self.baseline = dict(image_sha256=native.BASELINE_SHA, image_bytes=native.BASELINE_SIZE,
                             elf_sha256=native.BASELINE_ELF, slot='ota_0')
        self.replacement = dict(image_sha256=sha256, elf_sha256=elf, image_bytes=size, slot='ota_1')
        self.proof = dict(actual_file_verified=True, maintenance_health_acknowledged=True,
            stored_sha256=sha256, elf_sha256=elf,
            binding=dict(sha256=sha256, size=size, target=1), running_slot=1, boot_slot=1,
            image_state=2, error=0, result='ok', evidence_kind='running-measurement', boot_id=55,
            flags=v2.EXACT_HASH | v2.HEALTH_ACKED)

    def test_exact_deployed_receiver_uses_legacy_wire_bytes_not_fake_image_identity(self):
        wire, mode = v2.baseline_guard_sha(self.baseline, self.replacement, self.proof)
        self.assertEqual(wire.hex(), '7875d9a513acb95463b72e785eb160c70d03f85e965c3a30a93d954bb4cff55f')
        self.assertEqual(self.baseline['image_sha256'], native.BASELINE_SHA)
        self.assertEqual(mode, 'known-health-ack-receiver-byte-array-fix')
        binding = v2.Binding(bytes(range(16)), wire, native.BASELINE_SIZE, 0)
        request = v2.request_payload(v2.Op.RELEASE_BASELINE, 17, binding, 55)
        self.assertEqual(request[24:56], wire)
        self.assertEqual(struct.unpack_from('<I', request, 56)[0], native.BASELINE_SIZE)
        self.assertEqual(request[64], 0)

    def test_other_receiver_file_elf_or_length_never_uses_compatibility(self):
        for field, value in [('image_sha256', 'a'*64), ('elf_sha256', 'b'*64), ('image_bytes', 910449)]:
            replacement = dict(self.replacement, **{field:value})
            wire, mode = v2.baseline_guard_sha(self.baseline, replacement, self.proof)
            self.assertEqual(wire.hex(), native.BASELINE_SHA)
            self.assertEqual(mode, 'canonical')

    def test_missing_or_wrong_health_file_slot_boot_evidence_refuses(self):
        for field, value in [('actual_file_verified', False), ('maintenance_health_acknowledged', False),
            ('stored_sha256', 'a'*64), ('elf_sha256', 'b'*64), ('binding', {}), ('running_slot', 0),
            ('boot_slot', 0), ('image_state', 1), ('error', 1), ('result', 'refused'),
            ('evidence_kind', 'transaction-and-measurement'), ('boot_id', 0), ('flags', v2.EXACT_HASH)]:
            with self.subTest(field=field), self.assertRaises(v2.OutcomeError):
                v2.baseline_guard_sha(self.baseline, self.replacement, dict(self.proof, **{field:value}))

    def test_wrong_true_baseline_is_never_renamed_to_legacy_wire_guard(self):
        for field, value in [('image_sha256', 'a'*64), ('image_bytes', 10), ('elf_sha256', 'b'*64), ('slot', 'ota_1')]:
            with self.subTest(field=field), self.assertRaises(v2.OutcomeError):
                v2.baseline_guard_sha(dict(self.baseline, **{field:value}), self.replacement, self.proof)


if __name__ == '__main__':
    unittest.main()
