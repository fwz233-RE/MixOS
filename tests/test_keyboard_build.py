"""Regression checks for the STM32 target memory audit (no ARM tools needed)."""
import importlib.util
from pathlib import Path
import tempfile
import struct
import zlib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("keyboard_builder", ROOT / "tools/build_keyboard.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class KeyboardBuildAuditTests(unittest.TestCase):
    def audit(self, flash_size="00004000", stack_symbols=True):
        sections = f"""
  0 .text         {flash_size}  08000000  08000000  00010000  2**2
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  1 .mstack       00000300  20000000  20000000  00020000  2**3
                  ALLOC
  2 .pstack       00000600  20000300  20000300  00020000  2**3
                  ALLOC
  3 .data         00000010  20000900  08004000  00020900  2**2
                  CONTENTS, ALLOC, LOAD, DATA
  4 .bss          00000400  20000910  08004010  00020910  2**2
                  ALLOC
  5 .heap         00000af0  20000d10  20000d10  00020d10  2**3
                  ALLOC
"""
        symbols = "20000d10 B __heap_base__\n"
        if stack_symbols:
            symbols += """20000000 B __main_stack_base__
20000300 B __main_stack_end__
20000300 B __process_stack_base__
20000900 B __process_stack_end__
"""
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "target"
            with mock.patch.object(BUILDER, "capture", side_effect=[sections, symbols, "size output"]):
                return BUILDER.audit(prefix.with_suffix(".elf"), prefix)

    def test_unused_heap_is_not_static_ram(self):
        result = self.audit()
        self.assertEqual(result["ram_static_bytes"], 1040)
        self.assertEqual(result["reserved_stacks"], {"main": 768, "process": 1536})
        self.assertEqual(result["ram_used_including_stacks"], 3344)
        self.assertEqual(result["ram_remaining_bytes"], 2800)
        self.assertEqual(result["flash_eeprom_reserved_bytes"], 2048)
        self.assertEqual(result["flash_image_bytes"], 0x4010)  # NOLOAD .bss consumes no flash.

    def test_flash_must_leave_eeprom_pages(self):
        with self.assertRaisesRegex(RuntimeError, "budget exceeded"):
            self.audit(flash_size="00007c00")

    def test_missing_stack_symbols_reject_verification(self):
        with self.assertRaisesRegex(RuntimeError, "stack linker symbols"):
            self.audit(stack_symbols=False)

    def test_dfu_suffix_validation(self):
        payload = bytes(range(192))
        prefix = payload + struct.pack("<HHHH3sB", 0xffff, 0xdf11, 0x0483, 0x0100, b"UFD", 16)
        image = prefix + struct.pack("<I", zlib.crc32(prefix) ^ 0xffffffff)
        self.assertEqual(BUILDER.strip_dfu_suffix(image), payload)
        for offset in (-8, -5, -1, 0):  # Signature, length, CRC, payload corruption.
            with self.subTest(offset=offset):
                corrupted = bytearray(image)
                corrupted[offset] ^= 1
                with self.assertRaises(RuntimeError):
                    BUILDER.strip_dfu_suffix(bytes(corrupted))
        with self.assertRaisesRegex(RuntimeError, "Truncated"):
            BUILDER.strip_dfu_suffix(b"short")

    def test_source_verification_detects_added_changed_and_removed_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyboard = Path(tmp)
            config = keyboard / "config.h"
            config.write_text("#define BASELINE 1\n")
            expected = BUILDER.firmware_source_hashes(keyboard)
            with mock.patch.object(BUILDER, "KEYBOARD", keyboard):
                BUILDER.verify_firmware_sources(expected)
                override = keyboard / "mcuconf.h"
                override.write_text("#include_next <mcuconf.h>\n")
                with self.assertRaisesRegex(RuntimeError, "mcuconf.h"):
                    BUILDER.verify_firmware_sources(expected)
                override.unlink()
                config.write_text("#define BASELINE 2\n")
                with self.assertRaisesRegex(RuntimeError, "config.h"):
                    BUILDER.verify_firmware_sources(expected)
                config.unlink()
                with self.assertRaisesRegex(RuntimeError, "config.h"):
                    BUILDER.verify_firmware_sources(expected)

    def test_source_hashes_match_staging_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyboard = Path(tmp)
            (keyboard / "mcuconf.h").write_text("clock settings")
            for name in (".git", "tools", "tests", "release", "__pycache__"):
                (keyboard / name).mkdir()
                (keyboard / name / "ignored.h").write_text("excluded")
            (keyboard / "QMK_PIN.json").write_text("{}")
            self.assertEqual(set(BUILDER.firmware_source_hashes(keyboard)), {"mcuconf.h"})

    def test_raw_export_checks_source_set_before_writing_artifacts(self):
        manifest = {"target_build_verified": True, "qmk_commit": "pin", "source_hashes": {}}
        pin = {"target_build_verified": True, "commit": "pin"}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            import json
            (output / "manifest.json").write_text(json.dumps(manifest))
            with mock.patch.object(BUILDER, "OUTPUT", output), \
                 mock.patch.object(BUILDER, "export_raw") as export:
                with self.assertRaisesRegex(RuntimeError, "Firmware changed"):
                    BUILDER.export_verified_existing(pin)
                export.assert_not_called()

    def test_target_compile_cleans_stale_board_dependencies(self):
        root, log = Path("pinned-qmk"), Path("build.log")
        with mock.patch.object(BUILDER, "run") as run:
            BUILDER.compile_target(root, "keebdeck_6r11c", "default", 4, log)
        run.assert_called_once_with(
            ["qmk", "compile", "--clean", "-kb", "keebdeck_6r11c",
             "-km", "default", "-j", "4"], root, log)

    def test_board_restores_flash_vector_mapping_after_rom_handoff(self):
        source = (ROOT / "firmware/keyboard/keebdeck_6r11c.c").read_text()
        self.assertIn("SYSCFG_CFGR1_MEM_MODE", source)
        self.assertIn("~SYSCFG_CFGR1_MEM_MODE", source)
        self.assertIn("SYSCFG_CFGR1_PA11_PA12_RMP", source)

    def test_linker_reserves_eeprom(self):
        linker = (ROOT / "firmware/keyboard/ld/STM32F042x6.ld").read_text()
        self.assertIn("org = 0x08000000, len = 30k", linker)
        self.assertIn("org = 0x20000000, len = 6k", linker)
        self.assertIn('INCLUDE rules.ld', linker)
        self.assertIn("EEPROM_DRIVER = legacy_stm32_flash", (ROOT / "firmware/keyboard/rules.mk").read_text())


if __name__ == "__main__":
    unittest.main()
