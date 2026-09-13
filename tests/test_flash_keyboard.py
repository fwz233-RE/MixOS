"""Portable tests: fake USB/sysfs/dfu-util only; never touch real hardware."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "flash_keyboard", Path(__file__).resolve().parents[1] / "tools" / "flash_keyboard_on_pi.py")
flash = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flash)
SERIAL = "123456789ABC"
LAYOUT = "@Internal Flash /0x08000000/032*001Kg"
IDENTITY = {"path": "5-1.1", "vid_pid": "0483:df11", "serial": SERIAL,
            "busnum": 5, "devnum": 7, "descriptors_sha256": "a" * 64}


def image(size=1501):
    data = bytearray(b"\x00" * size)
    struct.pack_into("<II", data, 0, flash.RAM_BASE + flash.RAM_SIZE, flash.FLASH_BASE + 193)
    data[192:] = b"\x42" * (size - 192)
    return bytes(data)


def listing(layout=LAYOUT):
    return ('dfu-util 0.11\n\nFound DFU: [0483:df11] ver=2200, devnum=7, '
            'cfg=1, intf=0, path="5-1.1", alt=0, name="' + layout + '", serial="' + SERIAL + '"\n')


def descriptors():
    return (bytes((18, 1)) + b"\x00" * 16
            + bytes((9, 2, 27, 0, 1, 1, 0, 0x80, 50))
            + bytes((9, 4, 0, 0, 0, 0xfe, 1, 2, 0))
            + bytes((9, 0x21, 0x0b, 0xff, 0, 0, 8, 0x1a, 1)))


class FakeBackend:
    def __init__(self):
        self.commands = []
        self.memory = bytes(range(256)) * 128
        self.before = self.memory
        self.listing = listing()
        self.fail = None
        self.short = None
        self.corrupt = None
        self.change_at = None
        self.identities = 0
        self.write_checks = None
        self.exception = None

    def identity(self, serial):
        self.identities += 1
        row = dict(IDENTITY)
        if self.change_at is not None and self.identities >= self.change_at:
            row["devnum"] += 1
        return row

    def run(self, command):
        self.commands.append(command)
        if "--list" in command:
            operation = "inspect"
        elif "-D" in command:
            operation = "program"
        elif "-U" in command:
            operation = "backup" if Path(command[-1]).name == "backup.bin" else "readback"
        else:
            operation = "leave"
        if operation == "program" and self.write_checks:
            self.write_checks(Path(command[-1]).parent)
        if operation == self.exception:
            raise subprocess.TimeoutExpired(command, 120)
        if operation == self.fail:
            return subprocess.CompletedProcess(command, 74, "Simulated permission/disconnect/protection failure")
        if operation in ("backup", "readback"):
            data = self.memory
            if operation == self.short:
                data = data[:-1]
            if operation == "readback" and self.corrupt is not None:
                data = bytearray(data)
                data[self.corrupt] ^= 1
                data = bytes(data)
            Path(command[-1]).write_bytes(data)
        elif operation == "program":
            data = Path(command[-1]).read_bytes()
            self.memory = data + self.memory[len(data):]
        return subprocess.CompletedProcess(command, 0, self.listing if operation == "inspect" else "OK")


class ImageTests(unittest.TestCase):
    def validate(self, data):
        return flash.validate_image(data, flash.sha256(data))

    def test_padding_and_bounds(self):
        for size in (194, 1024, 1025, 32768):
            with self.subTest(size=size):
                data = image(size)
                padded = self.validate(data)
                self.assertEqual(len(padded) % 1024, 0)
                self.assertEqual(padded[:size], data)
                self.assertEqual(padded[size:], b"\xff" * (len(padded) - size))
                self.assertLessEqual(len(padded), 32768)

    def test_reject_size_hash_and_suffix(self):
        for data in (b"", b"\0" * 191, image(32769)):
            with self.subTest(size=len(data)), self.assertRaises(flash.SafetyError):
                self.validate(data)
        for digest in (None, "", "a" * 63, "x" * 64, "a" * 64):
            with self.subTest(digest=digest), self.assertRaises(flash.SafetyError):
                flash.validate_image(image(), digest)
        data = bytearray(image())
        data[-8:-5] = b"UFD"
        with self.assertRaises(flash.SafetyError):
            self.validate(data)

    def test_stack_and_reset_vectors(self):
        for stack in (0, 0xffffffff, flash.RAM_BASE, flash.RAM_BASE + 4,
                      flash.RAM_BASE + flash.RAM_SIZE + 8):
            data = bytearray(image())
            struct.pack_into("<I", data, 0, stack)
            with self.subTest(stack=stack), self.assertRaises(flash.SafetyError):
                self.validate(data)
        for reset in (0, 0xffffffff, flash.FLASH_BASE + 192, flash.FLASH_BASE + 1,
                      flash.FLASH_BASE + 1501, flash.FLASH_BASE + 32769, flash.RAM_BASE + 193):
            data = bytearray(image())
            struct.pack_into("<I", data, 4, reset)
            with self.subTest(reset=reset), self.assertRaises(flash.SafetyError):
                self.validate(data)

    def test_interrupt_vectors(self):
        for vector in (0, flash.FLASH_BASE + 193):
            data = bytearray(image())
            struct.pack_into("<I", data, 47 * 4, vector)
            self.validate(data)
        for vector in (0xffffffff, flash.RAM_BASE + 1, flash.FLASH_BASE + 192):
            data = bytearray(image())
            struct.pack_into("<I", data, 2 * 4, vector)
            with self.subTest(vector=vector), self.assertRaises(flash.SafetyError):
                self.validate(data)


class DescriptorTests(unittest.TestCase):
    def test_layout(self):
        for layout in (LAYOUT, "@Internal Flash  /0x08000000/016*001Kg,016*001Kg"):
            flash.validate_layout(layout)
        for layout in ("@Option Bytes /0x1ffff800/01*016 e", LAYOUT + "/0x1ffff800/01*016 e",
                       LAYOUT.replace("032", "064"), LAYOUT.replace("032", "031"),
                       LAYOUT.replace("001K", "002K"), LAYOUT.replace("Kg", "Ka"),
                       LAYOUT.replace("08000000", "08000400"), LAYOUT + ",000*001Kg",
                       LAYOUT.replace("001Kg", "001Mg"), LAYOUT + " trailing"):
            with self.subTest(layout=layout), self.assertRaises(flash.SafetyError):
                flash.validate_layout(layout)

    def test_listing_identity_ambiguity_version(self):
        flash.validate_listing(listing(), IDENTITY)
        for output in ("", listing() + listing(), listing().replace("0.11", "0.10"),
                       listing().replace("0483:df11", "c182:6b11"),
                       listing().replace("5-1.1", "5-1.2"), listing().replace(SERIAL, ""),
                       listing().replace("devnum=7", "devnum=8"),
                       listing().replace("alt=0", "alt=1"), listing().replace("intf=0", "intf=1"),
                       listing().replace("cfg=1", "cfg=2"), listing().replace("Found DFU", "Found Runtime"),
                       listing().replace("032*001Kg", "064*001Kg")):
            with self.subTest(output=output), self.assertRaises(flash.SafetyError):
                flash.validate_listing(output, IDENTITY)

    def test_raw_descriptors(self):
        flash.validate_usb_descriptors(descriptors())
        bad = [b"", descriptors()[:-1], descriptors() + b"\0\0",
               descriptors() + descriptors()[-9:]]
        for offset, value in ((27 + 7, 1), (36 + 2, 1), (36 + 7, 0x10), (36, 8)):
            data = bytearray(descriptors())
            data[offset] = value
            bad.append(bytes(data))
        for data in bad:
            with self.subTest(data=data), self.assertRaises(flash.SafetyError):
                flash.validate_usb_descriptors(data)

    def test_selection_rejects_esp_wildcards_mcu(self):
        flash.validate_selection("5-1.1", SERIAL, flash.MCU)
        for path, serial, mcu in (("5-1.2", SERIAL, flash.MCU), ("5-1", SERIAL, flash.MCU),
                                  ("5-1.1", "*", flash.MCU), ("5-1.1", "", flash.MCU),
                                  ("5-1.1", SERIAL + ",", flash.MCU),
                                  ("5-1.1", SERIAL, "STM32F072")):
            with self.subTest(path=path, serial=serial, mcu=mcu), self.assertRaises(flash.SafetyError):
                flash.validate_selection(path, serial, mcu)

    def test_independent_sysfs_corroboration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def device(path, vid="0483", pid="df11", serial=SERIAL):
                target = root / path
                target.mkdir()
                for name, value in {"idVendor": vid, "idProduct": pid, "serial": serial,
                                    "busnum": "5", "devnum": "7"}.items():
                    (target / name).write_text(value, encoding="ascii")
                (target / "descriptors").write_bytes(descriptors())
            device("5-1.1")
            device("5-1.2", "303a", "1001", "ESP")
            self.assertEqual(flash.corroborate_sysfs(SERIAL, root)["path"], "5-1.1")
            with self.assertRaises(flash.SafetyError):
                flash.corroborate_sysfs("ABCDEF123456", root)
            device("5-1.3")
            with self.assertRaises(flash.SafetyError):
                flash.corroborate_sysfs(SERIAL, root)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "once"
        self.backend = FakeBackend()
        self.data = image()

    def deploy(self, **kwargs):
        return flash.deploy(self.data, flash.sha256(self.data), SERIAL, flash.USB_PATH,
                            flash.MCU, self.work, backend=self.backend, **kwargs)

    def events(self):
        return [json.loads(line) for line in (self.work / "audit.jsonl").read_text().splitlines()]

    def writes(self):
        return [cmd for cmd in self.backend.commands if "-D" in cmd]

    def leaves(self):
        return [cmd for cmd in self.backend.commands if "0x08000000:leave" in cmd]

    def test_default_is_list_only(self):
        self.assertEqual(self.deploy()["status"], "inspection_only")
        self.assertEqual(len(self.backend.commands), 1)
        self.assertIn("--list", self.backend.commands[0])
        self.assertFalse((self.work / "backup.bin").exists())
        self.assertEqual(self.backend.memory, self.backend.before)

    def test_complete_backup_program_verify_leave_order(self):
        def before_write(directory):
            self.assertEqual((directory / "backup.bin").read_bytes(), self.backend.before)
            self.assertIn(flash.sha256(self.backend.before), (directory / "backup.bin.sha256").read_text())
            events = self.events()
            self.assertTrue(any(row["event"] == "artifact_durable" and row["name"] == "backup.bin" for row in events))
            self.assertTrue(any(row["event"] == "write_intent" for row in events))
        self.backend.write_checks = before_write
        result = self.deploy(execute=True, leave=True)
        self.assertEqual(result["status"], "verified_leave_requested")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(self.leaves()), 1)
        self.assertEqual(self.backend.memory[:len(self.data)], self.data)
        self.assertEqual(self.backend.memory[len(self.data):2048], b"\xff" * (2048 - len(self.data)))
        self.assertEqual(self.backend.memory[2048:], self.backend.before[2048:])
        events = self.events()
        verified = next(i for i, row in enumerate(events) if row["event"] == "verified")
        leave = next(i for i, row in enumerate(events) if row.get("operation") == "leave")
        self.assertLess(verified, leave)
        for command in self.backend.commands:
            for option, value in (("-d", ",0483:df11"), ("-a", "0"), ("-p", "5-1.1"),
                                  ("-S", SERIAL), ("-i", "0"), ("-c", "1"), ("-n", "7")):
                self.assertEqual(command[command.index(option) + 1], value)
            for forbidden in ("-R", "-e", "-w", "--wait", "force", "unprotect", "mass-erase", "will-reset"):
                self.assertNotIn(forbidden, " ".join(command))
        self.assertEqual(self.writes()[0][-3:], ["0x08000000", "-D", str(self.work / "program.bin")])
        for name in ("image.bin", "program.bin", "backup.bin", "readback.bin", "expected-full.bin"):
            self.assertIn(flash.sha256((self.work / name).read_bytes()),
                          (self.work / (name + ".sha256")).read_text())

    def test_execute_without_leave_stays_dfu(self):
        self.assertEqual(self.deploy(execute=True)["status"], "verified_in_dfu")
        self.assertFalse(self.leaves())

    def test_leave_requires_execute(self):
        with self.assertRaises(flash.SafetyError):
            self.deploy(leave=True)
        self.assertFalse(self.backend.commands)
        self.assertFalse(self.work.exists())

    def test_bad_hash_has_no_side_effects(self):
        with self.assertRaises(flash.SafetyError):
            flash.deploy(self.data, "0" * 64, SERIAL, flash.USB_PATH, flash.MCU,
                         self.work, execute=True, backend=self.backend)
        self.assertFalse(self.work.exists())
        self.assertFalse(self.backend.commands)

    def test_workdir_exclusive_even_empty_or_failed(self):
        self.work.mkdir()
        with self.assertRaises(FileExistsError):
            self.deploy(execute=True)
        self.assertFalse(self.backend.commands)

    def test_wrong_descriptor_never_uploads_or_writes(self):
        self.backend.listing = listing().replace("032*001Kg", "128*001Kg")
        with self.assertRaises(flash.SafetyError):
            self.deploy(execute=True)
        self.assertEqual(len(self.backend.commands), 1)
        with self.assertRaises(FileExistsError):
            self.deploy(execute=True)

    def test_backup_failure_or_short_upload_never_writes(self):
        for failure in ("fail", "short", "exception"):
            with self.subTest(failure=failure):
                self.work = Path(self.temp.name) / failure
                self.backend = FakeBackend()
                setattr(self.backend, failure, "backup")
                with self.assertRaises((flash.SafetyError, subprocess.TimeoutExpired)):
                    self.deploy(execute=True, leave=True)
                self.assertFalse(self.writes())
                self.assertFalse(self.leaves())

    def test_failed_write_or_readback_is_terminal(self):
        for operation in ("program", "readback", "leave"):
            for failure in ("fail", "exception"):
                with self.subTest(operation=operation, failure=failure):
                    self.work = Path(self.temp.name) / (operation + failure)
                    self.backend = FakeBackend()
                    setattr(self.backend, failure, operation)
                    with self.assertRaises((flash.SafetyError, subprocess.TimeoutExpired)):
                        self.deploy(execute=True, leave=True)
                    self.assertEqual(len(self.writes()), 1)
                    self.assertEqual(len(self.leaves()), int(operation == "leave"))
                    self.assertEqual(self.events()[-1]["event"], "failed")
                    self.assertFalse(self.events()[-1]["retry_allowed"])

    def test_corrupt_image_padding_or_untouched_tail_prevents_leave(self):
        for offset in (200, 1800, 32000):
            with self.subTest(offset=offset):
                self.work = Path(self.temp.name) / str(offset)
                self.backend = FakeBackend()
                self.backend.corrupt = offset
                with self.assertRaises(flash.SafetyError):
                    self.deploy(execute=True, leave=True)
                self.assertEqual(len(self.writes()), 1)
                self.assertFalse(self.leaves())
                self.assertTrue((self.work / "readback.bin.sha256").exists())

    def test_device_reenumeration_never_retries(self):
        for at in (2, 4, 6, 8):
            with self.subTest(at=at):
                self.work = Path(self.temp.name) / str(at)
                self.backend = FakeBackend()
                self.backend.change_at = at
                with self.assertRaises(flash.SafetyError):
                    self.deploy(execute=True, leave=True)
                self.assertLessEqual(len(self.writes()), 1)
                self.assertFalse(self.leaves())

    def test_fsync_failure_before_write_stops(self):
        original = flash.Audit.finish_upload
        def fail(audit, name):
            if name == "backup.bin":
                with mock.patch.object(flash.os, "fsync", side_effect=OSError("disk failure")):
                    return original(audit, name)
            return original(audit, name)
        with mock.patch.object(flash.Audit, "finish_upload", fail):
            with self.assertRaises(OSError):
                self.deploy(execute=True, leave=True)
        self.assertFalse(self.writes())
        self.assertFalse(self.leaves())

    def test_manifest_failure_before_write_stops(self):
        original = flash.durable_write
        def fail(path, data):
            if path.name == "backup.bin.sha256":
                raise OSError("disk full")
            original(path, data)
        with mock.patch.object(flash, "durable_write", side_effect=fail):
            with self.assertRaises(OSError):
                self.deploy(execute=True)
        self.assertFalse(self.writes())

    def test_short_readback_prevents_leave(self):
        self.backend.short = "readback"
        with self.assertRaises(flash.SafetyError):
            self.deploy(execute=True, leave=True)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(self.leaves())

    def test_staged_program_tampering_prevents_write(self):
        original = self.backend.run
        def tamper(command):
            result = original(command)
            if "-U" in command and command[-1].endswith("backup.bin"):
                (self.work / "program.bin").write_bytes(b"tampered")
            return result
        self.backend.run = tamper
        with self.assertRaises(flash.SafetyError):
            self.deploy(execute=True, leave=True)
        self.assertFalse(self.writes())
        self.assertFalse(self.leaves())

    def test_readback_fsync_failure_prevents_leave(self):
        original = flash.Audit.finish_upload
        def fail(audit, name):
            if name == "readback.bin":
                with mock.patch.object(flash.os, "fsync", side_effect=OSError("disk failure")):
                    return original(audit, name)
            return original(audit, name)
        with mock.patch.object(flash.Audit, "finish_upload", fail), self.assertRaises(OSError):
            self.deploy(execute=True, leave=True)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(self.leaves())

    def test_write_intent_fsync_failure_prevents_write(self):
        original = flash.Audit.event
        def fail(audit, event, **details):
            if event == "write_intent":
                raise OSError("audit fsync failure")
            original(audit, event, **details)
        with mock.patch.object(flash.Audit, "event", fail), self.assertRaises(OSError):
            self.deploy(execute=True, leave=True)
        self.assertFalse(self.writes())
        self.assertFalse(self.leaves())

    def test_keyboard_interrupt_is_terminal(self):
        original = self.backend.run
        def interrupt(command):
            if "-D" in command:
                self.backend.commands.append(command)
                raise KeyboardInterrupt()
            return original(command)
        self.backend.run = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.deploy(execute=True, leave=True)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(self.leaves())
        self.assertTrue(self.events()[-1]["write_attempted"])


class ProcessSafetyTests(unittest.TestCase):
    def test_lock_rejects_concurrent_worker_and_closes_fd(self):
        fake = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4,
                                     flock=mock.Mock(side_effect=BlockingIOError()))
        info = types.SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
        with mock.patch.dict(flash.sys.modules, {"fcntl": fake}), \
                mock.patch.object(flash.os, "O_NOFOLLOW", 0, create=True), \
                mock.patch.object(flash.os, "open", return_value=42), \
                mock.patch.object(flash.os, "fstat", return_value=info), \
                mock.patch.object(flash.os, "close") as close:
            with self.assertRaises(flash.SafetyError), flash.device_lock():
                self.fail("lock must not be acquired")
            close.assert_called_once_with(42)

    def test_lock_rejects_unsafe_file(self):
        fake = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=mock.Mock())
        for mode, uid, links in ((0o100666, 0, 1), (0o100600, 1000, 1), (0o100600, 0, 2)):
            info = types.SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links)
            with self.subTest(mode=mode, uid=uid, links=links), \
                    mock.patch.dict(flash.sys.modules, {"fcntl": fake}), \
                    mock.patch.object(flash.os, "O_NOFOLLOW", 0, create=True), \
                    mock.patch.object(flash.os, "open", return_value=42), \
                    mock.patch.object(flash.os, "fstat", return_value=info), \
                    mock.patch.object(flash.os, "close"):
                with self.assertRaises(flash.SafetyError), flash.device_lock():
                    self.fail("unsafe lock must not be acquired")
        fake.flock.assert_not_called()

    def test_termination_guard_raises_and_restores_handlers(self):
        with mock.patch.object(flash.signal, "SIGHUP", 1, create=True), \
                mock.patch.object(flash.signal, "signal", return_value="previous") as install:
            with flash.termination_guard():
                handler = install.call_args.args[1]
                with self.assertRaises(flash.SafetyError):
                    handler(15, None)
            self.assertEqual(install.call_count, 4)
            self.assertEqual(install.call_args.args[1], "previous")


class CliTests(unittest.TestCase):
    def test_no_arguments_inventory_only(self):
        with mock.patch.object(flash.sys, "platform", "linux"), \
                mock.patch.object(flash, "inventory", return_value=[IDENTITY]), \
                mock.patch.object(flash.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(flash.main([]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "inventory_only")
            run.assert_not_called()

    def test_incomplete_execute_and_wrong_platform_stop(self):
        for platform, args in (("linux", ["--execute"]), ("linux", ["--leave"]), ("win32", [])):
            with self.subTest(platform=platform, args=args), \
                    mock.patch.object(flash.sys, "platform", platform), \
                    mock.patch.object(flash.subprocess, "run") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(flash.main(args), 2)
                run.assert_not_called()

    def test_backend_uses_no_shell_clean_environment_timeout(self):
        command = flash.selectors(SERIAL, 7) + ["--list"]
        with mock.patch.object(flash.subprocess, "run") as run:
            flash.Backend().run(command)
            self.assertEqual(run.call_args.args, (command,))
            options = run.call_args.kwargs
            self.assertFalse(options.get("shell", False))
            self.assertEqual(options["timeout"], 120)
            self.assertNotIn("LD_PRELOAD", options["env"])
            self.assertEqual(options["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
