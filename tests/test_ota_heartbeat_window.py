"""Deterministic heartbeat-window boundaries; no serial, hardware or wall clock."""
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]
import ota_v2 as v2


class ScriptedClient:
    """Keep exact measurements flowing while independently scheduling PINGs."""
    def __init__(self, beats, transform=None):
        self.now = 0.0
        self.timeout = 0.25
        self.schedule = iter(beats)
        self.next_beat = next(self.schedule, None)
        self.transform = transform
        self.calls = []
        self.acks = 0
        self.link = SimpleNamespace(clock=lambda: self.now, epoch=101, session=202,
                                    heartbeat_times=deque(maxlen=256))
        self.binding = v2.Binding(bytes(range(16)), bytes(range(32)), 4096, 1)
        self.elf = bytes(range(32, 64))
        self.response = v2.Response(
            op=v2.Op.VERIFY_RUNNING, phase=v2.Phase.CONFIRMED, result=v2.Result.OK,
            request_id=1, binding=self.binding, received=self.binding.size,
            boot_id=55, running=1, boot=1, state=v2.VALID, error=0,
            flags=v2.JOURNAL | v2.EXACT_HASH | v2.HEALTH_CHALLENGE,
            stored_sha256=self.binding.sha256, elf_sha256=self.elf,
            message='health-challenge:12345678')

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 10)

    def capabilities(self):
        return v2.Capabilities(features=31, chunk=508, window=8, boot_id=55,
                               running=1, boot=1, state=v2.VALID,
                               slots=((0x10000, 0x1F0000), (0x610000, 0x1F0000)), protected=0)

    def call(self, op, binding, boot_id):
        assert op == v2.Op.VERIFY_RUNNING
        assert binding == self.binding and boot_id == 55
        self.calls.append(self.now)
        while self.next_beat is not None and self.next_beat <= self.now:
            self.link.heartbeat_times.append(self.next_beat)
            self.next_beat = next(self.schedule, None)
        response = self.response
        return self.transform(self, response) if self.transform else response

    def acknowledge_health(self, binding, expected_elf, boot_id, challenge):
        assert (binding, expected_elf, boot_id, challenge) == (
            self.binding, self.elf, 55, 0x12345678)
        self.acks += 1
        self.response = replace(self.response, flags=self.response.flags | v2.HEALTH_ACKED)
        return replace(self.response, op=v2.Op.HEALTH_ACK)

    def verify(self, deadline):
        return v2.verify_actual(self, self.binding, self.elf, deadline)


class HeartbeatWindowTests(unittest.TestCase):
    def assert_deadline_failure(self, client, deadline):
        with self.assertRaisesRegex(v2.OutcomeError, 'sustained heartbeat deadline expired') as caught:
            client.verify(deadline)
        self.assertNotEqual(caught.exception.state, 'confirmed')
        self.assertEqual(client.now, deadline)
        self.assertEqual(client.timeout, 0.25)
        self.assertEqual(client.acks, 1)
        # One normal measurement per 0.1s iteration; no extra retry or deadline extension.
        self.assertEqual(client.calls, [round(n / 10, 10) for n in range(round(deadline * 10))])
        return caught.exception

    def test_old_gap_allows_a_new_full_continuous_window(self):
        client = ScriptedClient([1, 2, 3, 7, 10, 13])
        result = client.verify(20)
        self.assertEqual(client.now, 13)
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['heartbeat_count'], 3)
        self.assertEqual(result['heartbeat_seconds'], 6)
        self.assertEqual(result['binding'], client.binding.record())
        self.assertEqual(result['boot_id'], 55)
        self.assertTrue(result['actual_file_verified'])
        self.assertTrue(result['maintenance_health_acknowledged'])
        self.assertEqual(list(client.link.heartbeat_times), [1, 2, 3, 7, 10, 13])
        self.assertEqual(client.acks, 1)

    def test_gap_just_occurred_cannot_reuse_the_earlier_span(self):
        self.assert_deadline_failure(ScriptedClient([1, 4, 8]), 8.2)

    def test_only_the_suffix_after_the_last_gap_counts(self):
        # The interval beginning at 7 is interrupted again at 14.
        client = ScriptedClient([1, 2, 3, 7, 10, 14, 17, 20])
        result = client.verify(21)
        self.assertEqual(client.now, 20)
        self.assertEqual(result['heartbeat_count'], 3)
        self.assertEqual(result['heartbeat_seconds'], 6)

    def test_repeated_gaps_too_few_beats_or_short_span_cannot_pass(self):
        for beats, deadline in (([1, 5, 9, 13, 17, 21], 24),
                                ([1, 4], 8), ([1, 4, 6.9], 7)):
            with self.subTest(beats=beats):
                self.assert_deadline_failure(ScriptedClient(beats), deadline)

    def test_stale_full_window_cannot_pass_when_measurements_resume(self):
        def busy_until_stale(client, response):
            if 0 < client.now < 11:
                return replace(response, phase=v2.Phase.VERIFYING_RUNNING, result=v2.Result.BUSY)
            return response
        client = ScriptedClient([1, 4, 7], busy_until_stale)
        self.assert_deadline_failure(client, 12)

    def test_pending_then_valid_resets_heartbeat_window(self):
        def pending(client, response):
            if 5 <= client.now < 8:
                return replace(response, state=1, phase=v2.Phase.RUNNING_PENDING_VERIFY)
            return response
        # No gap exceeds 3.5s, but the beat at 7 belongs to the pending interval.
        short = ScriptedClient([1, 4, 7, 10, 13, 16], pending)
        self.assert_deadline_failure(short, 15)
        client = ScriptedClient([1, 4, 7, 10, 13, 16], pending)
        result = client.verify(20)
        self.assertEqual(client.now, 16)
        self.assertEqual(result['heartbeat_count'], 3)
        self.assertEqual(result['heartbeat_seconds'], 6)

    def test_continuous_measurements_do_not_reset_the_same_boot_window(self):
        client = ScriptedClient([1, 4, 7])
        result = client.verify(10)
        self.assertEqual(client.now, 7)
        self.assertGreater(len(client.calls), 60)
        self.assertEqual(result['heartbeat_count'], 3)
        self.assertEqual(result['heartbeat_seconds'], 6)

    def test_exact_gap_and_latest_age_limits_remain_inclusive(self):
        def busy_until_boundary(client, response):
            if 0 < client.now < 11.5:
                return replace(response, phase=v2.Phase.VERIFYING_RUNNING, result=v2.Result.BUSY)
            return response
        client = ScriptedClient([1, 4.5, 8], busy_until_boundary)
        result = client.verify(12)
        self.assertEqual(client.now, 11.5)
        self.assertEqual(result['heartbeat_seconds'], 7)

    def test_recovered_window_never_bypasses_identity_or_ack_checks(self):
        mutations = {
            'boot_id': dict(boot_id=56),
            'full_binding': dict(binding=v2.Binding(bytes(16), bytes(range(32)), 4096, 1)),
            'received': dict(received=4095),
            'running': dict(running=0), 'boot_slot': dict(boot=0),
            'elf': dict(elf_sha256=bytes(32)), 'file_sha': dict(stored_sha256=bytes(32)),
            'ack': dict(flags=v2.JOURNAL | v2.EXACT_HASH | v2.HEALTH_CHALLENGE),
            'exact_hash': dict(flags=v2.JOURNAL | v2.HEALTH_CHALLENGE | v2.HEALTH_ACKED),
            'challenge': dict(message='health-challenge:87654321'),
            'invalid_state': dict(state=3),
        }
        for change in (*mutations, 'epoch', 'session'):
            def corrupt(client, response):
                if client.now < 13:
                    return response
                if change in ('epoch', 'session'):
                    setattr(client.link, change, getattr(client.link, change) + 1)
                    return response
                return replace(response, **mutations[change])
            with self.subTest(change=change):
                client = ScriptedClient([1, 2, 3, 7, 10, 13], corrupt)
                with self.assertRaises(v2.OutcomeError):
                    client.verify(20)
                self.assertEqual(client.now, 13)
                self.assertEqual(client.acks, 1)


if __name__ == '__main__':
    unittest.main()
