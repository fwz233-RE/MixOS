"""Native keyboard tests; run with WSL cc or set CC to a C11 compiler."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KBD = ROOT / "firmware/keyboard"
ESP = ROOT / "firmware/esp32s3/main"

class KeyboardNativeTests(unittest.TestCase):
    def compile_run(self, sources, includes):
        compiler = shlex.split(os.environ.get("CC", "cc"))
        if not shutil.which(compiler[0]):
            self.skipTest("C11 compiler unavailable; use WSL Ubuntu-22.04 cc")
        with tempfile.TemporaryDirectory(prefix="mix-keyboard-") as tmp:
            executable = str(Path(tmp) / "test")
            command = compiler + ["-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-g"]
            command += ["-I" + str(path) for path in includes]
            command += [str(path) for path in sources] + ["-o", executable]
            subprocess.run(command, check=True, timeout=90)
            subprocess.run([executable], check=True, timeout=30)

    def test_portable_input(self):
        self.compile_run([ROOT / "tests/test_input.c", ESP / "mix_input.c"], [ESP])

    def test_transport(self):
        self.compile_run([KBD / "tests/test_transport.c", KBD / "mix_kbd_transport.c"], [KBD])

    def test_driver(self):
        self.compile_run([KBD / "tests/test_driver.c", KBD / "mix_kbd_transport.c", ESP / "mix_keyboard.c", ESP / "mix_input.c"], [KBD / "tests/include", KBD, ESP])

    def test_stm32_hooks_and_irq(self):
        self.compile_run([KBD / "tests/test_stm32.c", KBD / "mix_kbd_transport.c", KBD / "i2c_slave_kbd.c", KBD / "keebdeck_6r11c.c"], [KBD / "tests/include/qmk", KBD])

    def test_usb_safety_configuration(self):
        config = json.loads((KBD / "keyboard.json").read_text())
        for feature in ("command", "console", "extrakey", "mousekey", "nkro"):
            self.assertFalse(config["features"][feature])
        self.assertTrue(config["features"]["bootmagic"])
        self.assertTrue(config["features"]["backlight"])
        self.assertEqual(config["bootmagic"]["matrix"], [2, 0])
        text = (KBD / "keymaps/default/keymap.c").read_text()
        body = text.split("LAYOUT_6x11(")[1].split(")")[0]
        tokens = [part.strip() for part in body.split(",")]
        self.assertEqual(len(tokens), 66)
        for i, token in enumerate(tokens):
            self.assertEqual(token, "P" if i >= 11 or (0x038e & (1 << i)) else "X")
        self.assertEqual((KBD / "keymaps/diag/keymap.c").read_text().splitlines()[-1], '#include "../default/keymap.c"')
        board = (KBD / "keebdeck_6r11c.c").read_text()
        self.assertIn("bool pre_process_record_kb", board)
        self.assertNotIn("return process_record_user", board)
        self.assertIn("IS_KEYEVENT(record->event)", board)
        self.assertIn("timer_elapsed32(started) >= 3000", board)
        self.assertIn("NO_USB_STARTUP_CHECK", (KBD / "config.h").read_text())

if __name__ == "__main__":
    unittest.main()
