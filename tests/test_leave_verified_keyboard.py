"""Leave-only recovery tests use fake USB only."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import leave_verified_keyboard as leave
from test_flash_keyboard import FakeBackend, SERIAL


class LeaveOnlyTests(unittest.TestCase):
    def attempt(self, backend, expected=None):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(leave, 'SERIAL', SERIAL), mock.patch.object(
                    leave, 'EXPECTED', expected or leave.k.sha256(backend.memory)):
                return leave.leave_verified(Path(tmp) / 'session', backend)

    def test_verified_single_leave_never_programs(self):
        backend = FakeBackend()
        result = self.attempt(backend)
        self.assertEqual(result['status'], 'verified_leave_only_requested')
        self.assertEqual(sum('0x08000000:leave' in c for c in backend.commands), 1)
        self.assertFalse(any('-D' in c or '-R' in c for c in backend.commands))

    def test_hash_mismatch_never_leaves(self):
        backend = FakeBackend()
        with self.assertRaises(leave.k.SafetyError):
            self.attempt(backend, '0' * 64)
        self.assertFalse(any('0x08000000:leave' in c for c in backend.commands))

    def test_short_upload_never_leaves(self):
        backend = FakeBackend()
        backend.short = 'readback'
        with self.assertRaises(leave.k.SafetyError):
            self.attempt(backend)
        self.assertFalse(any('0x08000000:leave' in c for c in backend.commands))

    def test_changed_device_never_leaves(self):
        backend = FakeBackend()
        backend.change_at = 4
        with self.assertRaises(leave.k.SafetyError):
            self.attempt(backend)
        self.assertFalse(any('0x08000000:leave' in c for c in backend.commands))

    def test_failed_leave_is_not_retried(self):
        backend = FakeBackend()
        backend.fail = 'leave'
        with self.assertRaises(leave.k.SafetyError):
            self.attempt(backend)
        self.assertEqual(sum('0x08000000:leave' in c for c in backend.commands), 1)


if __name__ == '__main__':
    unittest.main()
