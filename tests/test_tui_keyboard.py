"""Host keyboard decoding tests. No terminal, network, or device is opened."""
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mixos_tui_keys', ROOT / 'linux/apps/tui.py')
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)


class KeyboardTests(unittest.TestCase):
    def keyboard(self, data):
        keyboard = tui.Keyboard(fd=123)
        keyboard.buffer = data
        return keyboard

    def test_shift_space_is_one_named_key_not_ctrl_a_or_text(self):
        keyboard = self.keyboard(b'\x1b[32;2uhello world')
        self.assertEqual(keyboard.read(), 'shift-space')
        self.assertEqual(keyboard.buffer, b'hello world')
        self.assertEqual(''.join(keyboard.read() for _ in range(11)), 'hello world')

    def test_every_split_arrives_during_the_short_escape_wait(self):
        sequence = b'\x1b[32;2u'
        for split in range(1, len(sequence)):
            with self.subTest(split=split):
                keyboard = self.keyboard(sequence[:split])
                with mock.patch.object(tui.select, 'select', return_value=([123], [], [])), \
                     mock.patch.object(tui.os, 'read', return_value=sequence[split:]):
                    self.assertEqual(keyboard.read(), 'shift-space')
                self.assertEqual(keyboard.buffer, b'')

    def test_csi_parameters_survive_a_delayed_read(self):
        sequence = b'\x1b[32;2u'
        for split in range(2, len(sequence)):
            with self.subTest(split=split):
                keyboard = self.keyboard(sequence[:split])
                with mock.patch.object(tui.select, 'select', return_value=([], [], [])), \
                     mock.patch.object(tui.os, 'read') as read:
                    self.assertIsNone(keyboard.read())
                    self.assertEqual(keyboard.buffer, sequence[:split])
                    read.assert_not_called()  # buffered input is not fd readiness
                keyboard.buffer += sequence[split:] + b'x'
                self.assertEqual(keyboard.read(), 'shift-space')
                self.assertEqual(keyboard.read(), 'x')

    def test_one_byte_chunks_and_adjacent_shortcuts(self):
        sequence = b'\x1b[32;2u'
        keyboard = self.keyboard(sequence[:1])
        chunks = [bytes([byte]) for byte in sequence[1:]]
        chunks[-1] += sequence + b' '
        with mock.patch.object(tui.select, 'select', return_value=([123], [], [])), \
             mock.patch.object(tui.os, 'read', side_effect=chunks):
            self.assertEqual(keyboard.read(), 'shift-space')
        self.assertEqual(keyboard.read(), 'shift-space')
        self.assertEqual(keyboard.read(), ' ')

    def test_ctrl_a_space_keeps_its_literal_upstream_meaning(self):
        keyboard = self.keyboard(b'\x01 ')
        self.assertEqual(keyboard.read(), 'ctrl-a')
        self.assertEqual(keyboard.read(), ' ')

    def test_existing_arrows_functions_and_escape_still_work(self):
        for raw, key in ((b'\x1b[A', 'up'), (b'\x1bOA', 'up'),
                         (b'\x1b[3~', 'delete'), (b'\x1bOP', 'f1'),
                         (b'\x1b[Z', 'shift-tab')):
            self.assertEqual(self.keyboard(raw).read(), key)
        keyboard = self.keyboard(b'\x1b')
        with mock.patch.object(tui.select, 'select', return_value=([], [], [])):
            self.assertEqual(keyboard.read(), 'escape')
            self.assertEqual(keyboard.buffer, b'')

    def test_unknown_modifier_does_not_become_printable_text(self):
        for raw in (b'\x1b[32;3u', b'\x1b[32;6u'):
            keyboard = self.keyboard(raw + b'q')
            self.assertIsNone(keyboard.read())
            self.assertEqual(keyboard.read(), 'q')

    def test_notes_without_term_ime_ignores_toggle_without_editing(self):
        # Run the real editor handler, not an approximation of its key filter.
        saved_paths = sys.path[:]
        try:
            spec = importlib.util.spec_from_file_location(
                'mixos_notes_ime_fallback', ROOT / 'linux/apps/notes/app.py')
            app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app)
        finally:
            sys.path[:] = saved_paths
        with tempfile.TemporaryDirectory(prefix='mixos-no-ime-') as directory:
            store = app.Store(directory)
            store.ensure()
            notes = app.Notes(tui.Screen(io.StringIO(), columns=64, rows=22), store)
            notes.create()
            notes.buffer.insert_text('unchanged')
            notes.buffer.column = 3
            notes.buffer.modified = False
            keyboard = self.keyboard(b'\x1b[32;2u\x1b[32;2u')
            self.assertTrue(notes.key(keyboard.read()))
            self.assertTrue(notes.key(keyboard.read()))
            self.assertEqual(notes.buffer.text(), 'unchanged')
            self.assertEqual(notes.buffer.column, 3)
            self.assertFalse(notes.buffer.modified)
            self.assertEqual(notes.mode, 'edit')
            self.assertEqual(keyboard.buffer, b'')

    def test_partial_escape_buffer_has_a_bound(self):
        keyboard = self.keyboard(b'\x1b[' + b'2' * 33)
        self.assertIsNone(keyboard.read())
        self.assertEqual(keyboard.buffer, b'')


if __name__ == '__main__':
    unittest.main()
