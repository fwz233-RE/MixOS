#!/usr/bin/env python3
"""Build and audit the pinned STM32F042 keyboard under Linux/WSL; never flash.

Example:
  /home/fwz233/mixos-qmk-venv/bin/python tools/build_keyboard.py
Dependencies: pinned QMK checkout + initialized ChibiOS/printf submodules,
QMK Python requirements, make, arm-none-eabi toolchain, and native C compiler.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
KEYBOARD = ROOT / "firmware/keyboard"
OUTPUT = ROOT / "build/keyboard"
PIN_FILE = KEYBOARD / "QMK_PIN.json"


def run(command, cwd, log):
    print("+ " + " ".join(map(str, command)), flush=True)
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env["QMK_HOME"] = str(args.qmk_root)
    env["SKIP_GIT"] = "yes"
    if log.exists():
        history = OUTPUT / "history"
        history.mkdir(exist_ok=True)
        shutil.copy2(log, history / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + log.name))
    with log.open("w") as handle:
        handle.write("$ " + " ".join(map(str, command)) + "\n")
        handle.flush()
        proc = subprocess.Popen(list(map(str, command)), cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            handle.write(line)
            handle.flush()
            print(line, end="", flush=True)
        result = proc.wait()
    if result:
        raise RuntimeError(f"Command exited {result}; see {log}")


def capture(command, cwd=None):
    return subprocess.check_output(command, cwd=cwd, text=True)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(elf, prefix):
    sections = capture(["arm-none-eabi-objdump", "-h", str(elf)])
    symbols = capture(["arm-none-eabi-nm", "-n", "-S", str(elf)])
    size = capture(["arm-none-eabi-size", "-A", str(elf)])
    prefix.with_suffix(".sections.txt").write_text(sections)
    prefix.with_suffix(".symbols.txt").write_text(symbols)
    prefix.with_suffix(".size.txt").write_text(size)
    allocated = []
    lines = sections.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"\s*\d+\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)", line)
        if match and index + 1 < len(lines) and "ALLOC" in lines[index + 1]:
            name, length, vma, lma = match.groups()
            allocated.append(dict(name=name, size=int(length, 16), vma=int(vma, 16), lma=int(lma, 16), load="LOAD" in lines[index + 1]))
    ram_sections = [s for s in allocated if s["size"] and s["name"] != ".heap" and 0x20000000 <= s["vma"] < 0x30000000]
    flash_sections = [s for s in allocated if s["size"] and s["load"] and 0x08000000 <= s["lma"] < 0x10000000]
    if not ram_sections or not flash_sections:
        raise RuntimeError("Missing ELF memory sections")
    ram_end = max(s["vma"] + s["size"] for s in ram_sections)
    flash_end = max(s["lma"] + s["size"] for s in flash_sections)
    symbol_values = {}
    for line in symbols.splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch("[0-9a-fA-F]+", parts[0]):
            symbol_values[parts[-1]] = int(parts[0], 16)
    stacks = {}
    for name in ("main", "process"):
        base, end = f"__{name}_stack_base__", f"__{name}_stack_end__"
        if base not in symbol_values or end not in symbol_values:
            raise RuntimeError(f"Missing {name} stack linker symbols")
        stacks[name] = symbol_values[end] - symbol_values[base]
        if stacks[name] <= 0:
            raise RuntimeError(f"No reserved {name} stack")
    ram_end = max(ram_end, symbol_values.get("__heap_base__", ram_end))
    ram_used = ram_end - 0x20000000
    flash_used = flash_end - 0x08000000
    if ram_used > 6144 or flash_used > 30720:
        raise RuntimeError(f"F042 memory budget exceeded (2 KiB flash reserved for EEPROM): RAM {ram_used}, flash {flash_used}")
    result = dict(flash_capacity=32768, flash_eeprom_reserved_bytes=2048,
                  flash_application_capacity=30720, flash_image_bytes=flash_used,
                  flash_remaining_bytes=30720 - flash_used,
                  ram_capacity=6144, ram_used_including_stacks=ram_used,
                  ram_static_bytes=sum(s["size"] for s in ram_sections if s["name"] not in (".mstack", ".pstack")),
                  ram_remaining_bytes=6144 - ram_used,
                  reserved_stacks=stacks, ram_sections=ram_sections,
                  flash_sections=flash_sections,
                  stack_note="Reserved linker stacks are included; runtime high-water marks require hardware.")
    prefix.with_suffix(".memory.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return result


def strip_dfu_suffix(image):
    """Validate the exact QMK STM32 DFU suffix before returning its payload."""
    if len(image) < 16:
        raise RuntimeError("Truncated DFU suffix")
    device, product, vendor, version, signature, length, crc = struct.unpack("<HHHH3sBI", image[-16:])
    if signature != b"UFD" or length != 16:
        raise RuntimeError("Invalid DFU suffix signature or length")
    if (device, product, vendor, version) != (0xffff, 0xdf11, 0x0483, 0x0100):
        raise RuntimeError("Unexpected QMK STM32 DFU suffix identity/version")
    # DFU uses reflected CRC-32 with initial 0xffffffff and no final XOR.
    expected = zlib.crc32(image[:-4]) ^ 0xffffffff
    if crc != expected:
        raise RuntimeError("DFU suffix CRC mismatch")
    return image[:-length]


def export_raw(stem):
    elf, qmk_bin = OUTPUT / (stem + ".elf"), OUTPUT / (stem + ".bin")
    destination = OUTPUT / (stem + ".raw.bin")
    payload = qmk_bin.read_bytes()
    suffix_validated = False
    try:
        payload = strip_dfu_suffix(payload)
        suffix_validated = True
    except RuntimeError as error:
        # BOOTLOADER=custom intentionally emits no QMK DFU suffix. The raw
        # ELF export remains the authoritative payload and is compared below.
        if len(payload) < 192 or len(payload) > 30720:
            raise error
    with tempfile.TemporaryDirectory(prefix="mix-keyboard-raw-") as tmp:
        raw_file = Path(tmp) / "image.bin"
        command = ["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(raw_file)]
        subprocess.run(command, check=True)
        raw = raw_file.read_bytes()
    if raw != payload:
        raise RuntimeError("ELF-derived raw image differs from CRC-validated QMK payload")
    spec = importlib.util.spec_from_file_location("mix_keyboard_flash_worker", ROOT / "tools/flash_keyboard_on_pi.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    digest = hashlib.sha256(raw).hexdigest()
    # Pure stdlib validation only: never call worker.main(), inventory() or deploy().
    padded = worker.validate_image(raw, digest)
    if len(padded) > 30720:
        raise RuntimeError("Worker page padding overlaps the reserved EEPROM region")
    destination.write_bytes(raw)
    result = dict(raw_file=destination.name, raw_bytes=len(raw), raw_sha256=digest,
                  qmk_file=qmk_bin.name, qmk_bytes=qmk_bin.stat().st_size,
                  suffix_bytes=16 if suffix_validated else 0, suffix_signature_length_crc_validated=suffix_validated,
                  elf_payload_matches_qmk_payload=True, worker_validation_passed=True,
                  worker_sha256=sha256(ROOT / "tools/flash_keyboard_on_pi.py"),
                  worker_padded_bytes=len(padded), worker_padded_sha256=hashlib.sha256(padded).hexdigest(),
                  hardware_operations=0)
    (OUTPUT / (stem + ".raw.validation.json")).write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: raw deployment image " + json.dumps(result), flush=True)
    return result


def write_manifest(manifest):
    manifest["hashes"] = {p.name: sha256(p) for p in sorted(OUTPUT.iterdir()) if p.suffix in (".bin", ".elf", ".map")}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (OUTPUT / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in manifest["hashes"].items()))


def firmware_source_hashes(directory):
    """Hash the complete staged-input set, including newly added board headers."""
    excluded = {".git", "tools", "tests", "release", "__pycache__"}
    return {p.relative_to(directory).as_posix(): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file() and not excluded.intersection(p.relative_to(directory).parts)
            and p.suffix in (".c", ".h", ".mk", ".ld", ".json")
            and p.name != "QMK_PIN.json"}


def verify_firmware_sources(expected):
    current = firmware_source_hashes(KEYBOARD)
    if current != expected:
        changed = sorted(name for name in current.keys() | expected.keys()
                         if current.get(name) != expected.get(name))
        raise RuntimeError("Firmware changed since verified build: " + ", ".join(changed))


def export_verified_existing(pin):
    """Package existing verified artifacts without rebuilding or accessing hardware."""
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    if not pin["target_build_verified"] or not manifest["target_build_verified"] or manifest["qmk_commit"] != pin["commit"]:
        raise RuntimeError("Raw-only export requires an existing verified pinned target build")
    verify_firmware_sources(manifest["source_hashes"])
    for keymap in pin["keymaps"]:
        for suffix in ("bin", "elf", "map"):
            name = pin["keyboard_target"] + "_" + keymap + "." + suffix
            if sha256(OUTPUT / name) != manifest["hashes"][name]:
                raise RuntimeError("Verified artifact changed: " + name)
    exports = {keymap: export_raw(pin["keyboard_target"] + "_" + keymap) for keymap in pin["keymaps"]}
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_keyboard_build.py", "-v"], ROOT, OUTPUT / "raw_export_tests.log")
    manifest["raw_exports"] = exports
    manifest["raw_exported_at"] = datetime.now(timezone.utc).isoformat()
    manifest["raw_export_verification_hashes"] = {str(p.relative_to(ROOT)): sha256(p) for p in
                                               (Path(__file__), ROOT / "tools/flash_keyboard_on_pi.py", ROOT / "tests/test_keyboard_build.py")}
    write_manifest(manifest)


def compile_target(qmk_root, keyboard, keymap, jobs, log):
    # A newly added include-path override is absent from old .d files. Always
    # clean the dedicated checkout's build tree so inherited board objects
    # cannot hide new headers.
    run(["qmk", "compile", "--clean", "-kb", keyboard, "-km", keymap,
         "-j", str(jobs)], qmk_root, log)


def main():
    pin = json.loads(PIN_FILE.read_text())
    if args.export_raw_only:
        export_verified_existing(pin)
        return
    # Invalidate previous verification even if prerequisites or revision checks fail.
    pin["target_build_verified"] = False
    pin["hardware_verified"] = False
    PIN_FILE.write_text(json.dumps(pin, indent=2) + "\n")
    actual = capture(["git", "rev-parse", "HEAD"], args.qmk_root).strip()
    if actual != pin["commit"]:
        raise RuntimeError(f"Wrong QMK revision: {actual}")
    submodules = capture(["git", "submodule", "status", "lib/chibios", "lib/chibios-contrib", "lib/printf", "lib/lufa"], args.qmk_root)
    if any(line[0] != " " for line in submodules.splitlines()):
        raise RuntimeError("Required QMK submodules not initialized at pinned revisions: " + submodules)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = args.qmk_root / "keyboards" / pin["keyboard_target"]
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(KEYBOARD, target, ignore=shutil.ignore_patterns(".git", "tools", "tests", "release", "__pycache__"))
    versions = capture(["arm-none-eabi-gcc", "--version"]) + "\n" + capture(["arm-none-eabi-ld", "--version"])
    (OUTPUT / "toolchain.txt").write_text(versions)
    (OUTPUT / "python-packages.txt").write_text(capture([sys.executable, "-m", "pip", "freeze"]))
    (OUTPUT / "submodules.txt").write_text(submodules)
    results, exports = {}, {}
    for keymap in pin["keymaps"]:
        stem = pin["keyboard_target"] + "_" + keymap
        compile_target(args.qmk_root, pin["keyboard_target"], keymap, args.jobs,
                       OUTPUT / (stem + ".build.log"))
        for suffix in ("bin", "elf", "map"):
            source = args.qmk_root / ".build" / (stem + "." + suffix)
            if not source.is_file():
                raise RuntimeError(f"Expected build artifact missing: {source}")
            shutil.copy2(source, OUTPUT / source.name)
        results[keymap] = audit(OUTPUT / (stem + ".elf"), OUTPUT / stem)
        exports[keymap] = export_raw(stem)
    if not args.build_only:
        compiler = shlex.split(os.environ.get("CC", "cc"))
        if not compiler or not shutil.which(compiler[0]):
            raise RuntimeError("Native C compiler required; skipped sanitizer tests cannot verify a build")
        run([sys.executable, KEYBOARD / "tools/verify_qmk.py"], ROOT, OUTPUT / "verify_qmk.log")
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_keyboard*.py", "-v"], ROOT, OUTPUT / "tests.log")
    manifest = dict(qmk_commit=actual, built_at=datetime.now(timezone.utc).isoformat(),
                    target_build_verified=not args.build_only, hardware_verified=False,
                    toolchain=versions, submodules=submodules, keymaps=results, raw_exports=exports,
                    hashes={p.name: sha256(p) for p in sorted(OUTPUT.iterdir()) if p.suffix in (".bin", ".elf", ".map")},
                    source_hashes=firmware_source_hashes(target),
                    verification_hashes={str(p.relative_to(ROOT)): sha256(p) for p in [Path(__file__), KEYBOARD / "tools/verify_qmk.py", *sorted((ROOT / "tests").glob("test_keyboard*.py"))]})
    write_manifest(manifest)
    if not args.build_only:
        pin["target_build_verified"] = True
        pin["target_build_date"] = datetime.now(timezone.utc).date().isoformat()
        pin["target_build_manifest"] = "build/keyboard/manifest.json"
        PIN_FILE.write_text(json.dumps(pin, indent=2) + "\n")
    print("PASS: keyboard target builds and memory audit" + ("; source verifier and native tests" if not args.build_only else " (tests not run)"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmk-root", type=Path, default=ROOT / ".tools/qmk-0.28.0")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--build-only", action="store_true", help="Debug build only; cannot set target_build_verified")
    parser.add_argument("--export-raw-only", action="store_true", help="Validate and export existing verified ELF/QMK artifacts without rebuilding")
    args = parser.parse_args()
    main()
