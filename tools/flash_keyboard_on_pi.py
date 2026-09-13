#!/usr/bin/env python3
"""Fail-closed, one-shot STM32F042 keyboard DFU worker (stdlib only).

No arguments: read Linux USB inventory only. See docs/KEYBOARD_FLASH.md.
Hardware operations require Linux; all safety logic is portable/testable.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import time

FLASH_BASE = 0x08000000
FLASH_SIZE = 32 * 1024
PAGE_SIZE = 1024
RAM_BASE = 0x20000000
RAM_SIZE = 6 * 1024
MCU = "STM32F042G6U6"
USB_PATH = "5-1.1"
ESP_PATH = "5-1.2"
ROM_ID = "0483:df11"
RUNTIME_ID = "c182:6b11"
SYSFS = Path("/sys/bus/usb/devices")
DFU_UTIL = "/usr/bin/dfu-util"


class SafetyError(RuntimeError):
    """Stop without retry, recovery write, or reset."""


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_selection(usb_path, serial, mcu):
    require(usb_path == USB_PATH, f"Only corroborated keyboard path {USB_PATH} is allowed")
    require(isinstance(serial, str) and re.fullmatch(r"[0-9A-Fa-f]{12}", serial),
            "An exact 12-hex-character STM32 ROM serial is required (no wildcards)")
    require(mcu == MCU, f"Explicit board MCU confirmation must be {MCU}")


def validate_image(image, expected_sha):
    require(isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha),
            "An exact 64-hex-character image SHA-256 is required")
    require(192 <= len(image) <= FLASH_SIZE, "Raw image must contain the 48-entry vector table and fit 32 KiB")
    require(sha256(image) == expected_sha.lower(), "Image SHA-256 mismatch")
    # Reject suffixes which dfu-util might strip instead of writing verbatim.
    require(image[-8:-5] != b"UFD", "DFU-suffixed images are forbidden; supply a raw .bin")
    stack, reset = struct.unpack_from("<II", image)
    require(RAM_BASE < stack <= RAM_BASE + RAM_SIZE and stack % 8 == 0,
            "Initial stack must be 8-byte aligned inside 6 KiB SRAM (top allowed)")
    require(reset & 1 and FLASH_BASE + 192 <= (reset & ~1) <= FLASH_BASE + len(image) - 2,
            "Reset vector must be Thumb code inside this image, after the vector table")
    for index in range(2, 48):
        vector = struct.unpack_from("<I", image, index * 4)[0]
        require(vector == 0 or (vector & 1 and
                FLASH_BASE + 192 <= (vector & ~1) <= FLASH_BASE + len(image) - 2),
                f"Vector {index} is neither zero nor Thumb code inside the image")
    length = (len(image) + PAGE_SIZE - 1) // PAGE_SIZE * PAGE_SIZE
    return image + b"\xff" * (length - len(image))


def validate_layout(name):
    match = re.fullmatch(r"@Internal Flash\s*/0x08000000/\s*(.+)", name)
    require(match is not None, "Alternate 0 must describe only internal flash at 0x08000000")
    pages = 0
    for segment in match[1].split(","):
        item = re.fullmatch(r"\s*(\d{1,3})\*0*1Kg\s*", segment)
        require(item is not None and int(item[1]) > 0,
                "Require readable/erasable/writable 1 KiB pages (001Kg), no other regions")
        pages += int(item[1])
    require(pages == 32, "DFU descriptor must report exactly 32 x 1 KiB flash pages")


def validate_usb_descriptors(data):
    """Check raw sysfs USB descriptors, independently of dfu-util's text."""
    offset, config, interface = 0, None, None
    target_interfaces, functions = 0, []
    while offset < len(data):
        require(offset + 2 <= len(data), "Truncated USB descriptor header")
        size, kind = data[offset:offset + 2]
        require(size >= 2 and offset + size <= len(data), "Malformed USB descriptor length")
        part = data[offset:offset + size]
        if kind == 2:
            require(size == 9, "Malformed configuration descriptor")
            config, interface = part[5], None
        elif kind == 4:
            require(size == 9, "Malformed interface descriptor")
            interface = part[2]
            if config == 1 and interface == 0 and part[3] == 0:
                require(part[5:8] == bytes((0xfe, 1, 2)), "Interface 0 must be ROM DFU protocol 2")
                target_interfaces += 1
        elif kind == 0x21 and config == 1 and interface == 0:
            require(size == 9, "Malformed DFU functional descriptor")
            attrs, _, transfer, version = struct.unpack_from("<BHHH", part, 2)
            require(attrs & 3 == 3 and version == 0x011a and 64 <= transfer <= 4096,
                    "Require DfuSe 1.1a with upload and download capability")
            functions.append((attrs, transfer, version))
        offset += size
    require(target_interfaces == 1 and len(functions) == 1,
            "Missing or ambiguous alternate-0 DFU/functional descriptors")


def inventory(root=SYSFS):
    require(root.is_dir(), "Linux USB sysfs is required; no hardware operation was attempted")
    result = []
    for device in sorted(root.iterdir()):
        if not re.fullmatch(r"\d+-\d+(?:\.\d+)*", device.name):
            continue
        # Enumeration races fail closed, rather than hiding a disappearing device.
        def text(name):
            return (device / name).read_text(encoding="ascii").strip()
        row = {"path": device.name, "vid_pid": text("idVendor") + ":" + text("idProduct"),
               "busnum": int(text("busnum")), "devnum": int(text("devnum")),
               "serial": text("serial") if (device / "serial").exists() else ""}
        result.append(row)
    return result


def corroborate_sysfs(serial, root=SYSFS):
    devices = inventory(root)
    matches = [row for row in devices if row["vid_pid"] == ROM_ID]
    require(len(matches) == 1, "Require exactly one ROM DFU device in Linux USB inventory")
    row = matches[0]
    require(row["path"] == USB_PATH and row["serial"] == serial and row["busnum"] == 5
            and 1 <= row["devnum"] <= 127, "USB sysfs path/serial/bus identity mismatch")
    descriptors = (root / USB_PATH / "descriptors").read_bytes()
    validate_usb_descriptors(descriptors)
    row["descriptors_sha256"] = sha256(descriptors)
    return row


LIST_LINE = re.compile(
    r'Found DFU: \[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\] ver=([0-9a-fA-F]{4}), '
    r'devnum=(\d+), cfg=(\d+), intf=(\d+), path="([^"]+)", alt=(\d+), '
    r'name="([^"]+)", serial="([^"]+)"')


def validate_listing(output, identity):
    require(re.search(r"^dfu-util 0\.11\s*$", output, re.M), "Only reviewed dfu-util 0.11 is supported")
    lines = [line.strip() for line in output.splitlines() if line.strip().startswith("Found ")]
    require(len(lines) == 1, "Missing or ambiguous pinned alternate-0 DFU listing")
    match = LIST_LINE.fullmatch(lines[0])
    require(match is not None, "Unrecognized DFU listing; refusing to guess")
    vid, _, devnum, cfg, intf, path, alt, name, serial = match.groups()
    require(vid.lower() == ROM_ID and int(devnum) == identity["devnum"] and cfg == "1"
            and intf == "0" and path == USB_PATH and alt == "0" and serial == identity["serial"],
            "dfu-util listing disagrees with independently read sysfs identity")
    validate_layout(name)
    return name


def sync_directory(path):
    # Windows cannot fsync directories; production entry point only permits Linux.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_write(path, data):
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


class Audit:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        # Existing directories (even empty), symlinks and reuse are forbidden.
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        sync_directory(self.directory.parent)
        self.path = self.directory / "audit.jsonl"
        durable_write(self.path, b"")

    def event(self, event, **details):
        line = json.dumps({"event": event, "time_ns": time.time_ns(), **details},
                          sort_keys=True) + "\n"
        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def artifact(self, name, data):
        path = self.directory / name
        durable_write(path, data)
        durable_write(self.directory / (name + ".sha256"),
                      (sha256(data) + "  " + name + "\n").encode("ascii"))
        self.event("artifact_durable", name=name, bytes=len(data), sha256=sha256(data))
        return path

    def finish_upload(self, name):
        path = self.directory / name
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Upload artifact must be a regular private file")
        with path.open("r+b") as handle:
            data = handle.read(FLASH_SIZE + 1)
            require(len(data) == FLASH_SIZE, f"{name}: upload must be exactly 32768 bytes")
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(self.directory)
        durable_write(self.directory / (name + ".sha256"),
                      (sha256(data) + "  " + name + "\n").encode("ascii"))
        self.event("artifact_durable", name=name, bytes=len(data), sha256=sha256(data))
        return data


class Backend:
    def identity(self, serial):
        return corroborate_sysfs(serial)

    def run(self, command):
        # No shell, inherited PYTHONPATH/LD_PRELOAD, sudo fallback, wait or retry.
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                              timeout=120, check=False,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/"})


def selectors(serial, devnum):
    # A single '-d 0483:df11' would ALSO match arbitrary DFU-mode IDs in 0.11!
    return [DFU_UTIL, "-d", ",0483:df11", "-a", "0", "-p", USB_PATH,
            "-S", serial, "-c", "1", "-i", "0", "-n", str(devnum)]


def deploy(image, expected_sha, serial, usb_path, mcu, workdir, *, execute=False,
           leave=False, backend=None):
    """One transaction; caller holds the device lock. Never resumes an audit directory."""
    validate_selection(usb_path, serial, mcu)
    padded = validate_image(image, expected_sha)
    require(not leave or execute, "--leave requires --execute")
    backend = backend or Backend()
    audit = Audit(workdir)
    attempted, verified = False, False
    try:
        audit.event("start", execute=execute, leave=leave, mcu=mcu, usb_path=usb_path,
                    serial=serial, image_sha256=sha256(image), image_bytes=len(image),
                    program_bytes=len(padded), flash_base=FLASH_BASE, flash_size=FLASH_SIZE)
        identity = backend.identity(serial)
        audit.event("identity", **identity)
        base = selectors(serial, identity["devnum"])

        def run(operation, *args):
            require(backend.identity(serial) == identity,
                    "Device changed/disconnected since inspection; no retry is permitted")
            command = base + list(args)
            audit.event("command_start", operation=operation, argv=command)
            result = backend.run(command)
            audit.event("command_end", operation=operation, returncode=result.returncode,
                        output=result.stdout)
            require(result.returncode == 0, f"{operation} failed; no retry or automatic recovery")
            return result.stdout

        def inspect():
            output = run("inspect", "--list")
            layout = validate_listing(output, identity)
            audit.event("descriptor_validated", layout=layout)

        inspect()
        if not execute:
            audit.event("inspection_complete", writes=0, uploads=0)
            return {"status": "inspection_only", "audit": str(audit.directory),
                    "image_sha256": sha256(image), "program_bytes": len(padded)}

        image_path = audit.artifact("image.bin", image)
        program_path = audit.artifact("program.bin", padded)
        run("backup", "-s", "0x08000000:32768", "-U", str(audit.directory / "backup.bin"))
        backup = audit.finish_upload("backup.bin")
        # Never erase the remaining pages: preserve old code/settings/EEPROM there.
        expected_full = padded + backup[len(padded):]
        audit.artifact("expected-full.bin", expected_full)
        inspect()
        require(image_path.read_bytes() == image and program_path.read_bytes() == padded,
                "Staged image changed before programming")
        require((audit.directory / "backup.bin").read_bytes() == backup,
                "Durable backup changed before programming")
        audit.event("write_intent", start=FLASH_BASE, end_exclusive=FLASH_BASE + len(padded),
                    backup_sha256=sha256(backup), expected_full_sha256=sha256(expected_full))
        attempted = True  # Set before the only write; even a launch failure is not retried.
        run("program", "-s", "0x08000000", "-D", str(program_path))
        inspect()
        run("readback", "-s", "0x08000000:32768", "-U", str(audit.directory / "readback.bin"))
        readback = audit.finish_upload("readback.bin")
        require(sha256(readback[:len(image)]) == sha256(image), "Readback image SHA-256 mismatch")
        require(sha256(readback[:len(padded)]) == sha256(padded), "Readback page-padded SHA-256 mismatch")
        require(sha256(readback) == sha256(expected_full),
                "Readback full-flash SHA-256 mismatch (including untouched pages)")
        audit.event("verified", image_sha256=sha256(image), padded_sha256=sha256(padded),
                    full_flash_sha256=sha256(readback))
        verified = True
        if leave:
            inspect()
            # Reviewed dfu-util 0.11 command mode: SET_ADDRESS + zero-length DNLOAD.
            # No file, erase or reset flag; only permitted after durable verification.
            run("leave", "-s", "0x08000000:leave")
            audit.event("leave_requested", application_health="not_verified")
        result = {"status": "verified_leave_requested" if leave else "verified_in_dfu",
                  "audit": str(audit.directory), "image_sha256": sha256(image),
                  "full_flash_sha256": sha256(readback), "program_bytes": len(padded)}
        audit.event("complete", **result)
        return result
    except BaseException as error:
        # Best effort only: an fsync/disk failure must never cause another USB operation.
        try:
            audit.event("failed", error=str(error), write_attempted=attempted,
                        readback_verified=verified, retry_allowed=False)
        except Exception:
            pass
        raise


@contextmanager
def termination_guard():
    # Raising through subprocess.run makes it kill/reap its child before unlocking.
    # SIGKILL/power loss cannot be handled and must be treated as uncertain writes.
    def terminate(signum, frame):
        raise SafetyError(f"Worker interrupted by signal {signum}")
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, terminate)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def device_lock():
    import fcntl
    path = "/run/lock/mixos-keyboard-5-1.1.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and info.st_mode & 0o077 == 0, "Unsafe device lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SafetyError("Another keyboard worker owns the device lock") from error
        yield
    finally:
        os.close(fd)  # Keep the inode on disk: unlinking breaks interprocess locking.


def trusted_parent(path):
    parent = path.parent
    require(parent.is_absolute() and parent.resolve(strict=True) == parent,
            "Workdir parent must be an existing canonical absolute path")
    for directory in (parent, *parent.parents):
        info = directory.stat()
        require(info.st_uid == 0 and info.st_mode & 0o022 == 0,
                "Privileged worker requires root-owned, non-group/world-writable parent directories")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--image", type=Path, help="Raw application .bin linked at 0x08000000")
    parser.add_argument("--sha256")
    parser.add_argument("--serial", help="Exact ROM DFU serial, not the runtime keyboard serial")
    parser.add_argument("--usb-path")
    parser.add_argument("--mcu")
    parser.add_argument("--workdir", type=Path, help="New single-use absolute audit directory")
    parser.add_argument("--execute", action="store_true", help="Authorize one bounded flash transaction")
    parser.add_argument("--leave", action="store_true", help="Leave DFU only after verified full readback")
    args = parser.parse_args(argv)
    try:
        require(sys.platform == "linux", "Hardware CLI requires Linux; portable tests use a fake backend")
        supplied = any((args.image, args.sha256, args.serial, args.usb_path, args.mcu,
                        args.workdir, args.execute, args.leave))
        if not supplied:
            print(json.dumps({"status": "inventory_only", "devices": inventory(),
                              "keyboard_path": USB_PATH, "esp_path_do_not_touch": ESP_PATH}, indent=2))
            return 0
        require(all((args.image, args.sha256, args.serial, args.usb_path, args.mcu, args.workdir)),
                "Image inspection/execution requires --image --sha256 --serial --usb-path --mcu --workdir")
        validate_selection(args.usb_path, args.serial, args.mcu)
        require(not args.leave or args.execute, "--leave requires --execute")
        require(args.image.suffix.lower() == ".bin", "Only a raw .bin image is allowed")
        require(stat.S_ISREG(args.image.stat().st_mode), "Image must be a regular file")
        with args.image.open("rb") as handle:
            require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "Image must be a regular file")
            image = handle.read(FLASH_SIZE + 1)
        validate_image(image, args.sha256)
        require(os.geteuid() == 0, "Launch this isolated worker using sudo -n /usr/bin/python3 -I; see docs")
        require(args.workdir.is_absolute(), "Workdir must be absolute")
        trusted_parent(args.workdir)
        with termination_guard(), device_lock():
            result = deploy(image, args.sha256, args.serial, args.usb_path, args.mcu,
                            args.workdir, execute=args.execute, leave=args.leave)
        print(json.dumps(result, indent=2))
        return 0
    except (SafetyError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"STOP: {error}. No automatic retry, rollback, unprotect or reset.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
