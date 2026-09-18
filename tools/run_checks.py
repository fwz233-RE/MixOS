#!/usr/bin/env python3
"""Run every check this repository knows how to run, and report honestly.

Before this script, verifying the project meant executing five commands in the
right order, and those commands were described differently in four documents.
Worse, roughly a third of the Python tests skip silently on a Windows
development machine, so "all tests pass" could mean "83 tests never ran".

This runner therefore prints the skip count as prominently as the failure
count, and ``--strict`` turns skipped tests into a non-zero exit so continuous
integration cannot go green on a suite that did not execute.

    python tools/run_checks.py              # everything available here
    python tools/run_checks.py --quick      # skip the C toolchain stages
    python tools/run_checks.py --strict     # skips are failures
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Stage:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.status = "pending"
        self.detail = ""
        self.seconds = 0.0


def run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd or ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def stage_ctest(stage: Stage) -> None:
    if not shutil.which("cmake"):
        stage.status = "unavailable"
        stage.detail = "cmake not on PATH"
        return
    configure = run(["cmake", "-S", "tests", "-B", "build/host"])
    if configure.returncode:
        stage.status = "fail"
        stage.detail = configure.stdout[-2000:] + configure.stderr[-2000:]
        return
    build = run(["cmake", "--build", "build/host"])
    if build.returncode:
        stage.status = "fail"
        stage.detail = build.stdout[-2000:] + build.stderr[-2000:]
        return
    test = run(["ctest", "--test-dir", "build/host", "--output-on-failure"])
    stage.status = "pass" if test.returncode == 0 else "fail"
    match = re.search(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)", test.stdout)
    stage.detail = match.group(0) if match else (test.stdout[-2000:] + test.stderr[-2000:])


def stage_python(stage: Stage) -> None:
    result = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    text = result.stdout + result.stderr
    match = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
    total = int(match.group(1)) if match else 0
    failures = len(re.findall(r"^(FAIL|ERROR):", text, re.MULTILINE))
    skipped = len(re.findall(r"\.\.\. skipped", text))
    stage.status = "pass" if result.returncode == 0 else "fail"
    stage.detail = f"{total} tests, {failures} failed, {skipped} skipped"
    stage.skipped = skipped
    if result.returncode:
        tail = "\n".join(line for line in text.splitlines()
                         if line.startswith(("FAIL:", "ERROR:", "AssertionError")))
        stage.detail += "\n" + tail[:3000]


def stage_preview(stage: Stage) -> None:
    if not shutil.which("node"):
        stage.status = "unavailable"
        stage.detail = "node not on PATH"
        return
    result = run(["node", "tests/test_preview.cjs"])
    stage.status = "pass" if result.returncode == 0 else "fail"
    stage.detail = (result.stdout + result.stderr).strip()[-2000:]


def stage_docs(stage: Stage) -> None:
    """Every file path a document mentions must exist.

    Only references carrying a known file extension are checked. Prose names a
    module as ``mix_ui`` or a directory as ``docs/`` often enough that matching
    bare words would drown a real broken link in false positives.
    """
    suffixes = (".py", ".c", ".h", ".sh", ".md", ".json", ".csv", ".cjs",
                ".txt", ".service", ".rules", ".mk", ".cmake", ".ini", ".terminfo")
    pattern = re.compile(r"(?<![\w/.-])((?:tools|linux|tests|protocol|docs|firmware|archive)"
                         r"/[\w./-]*[\w])")
    broken: list[str] = []
    checked = 0
    for doc in sorted(ROOT.rglob("*.md")):
        if any(part in (".tools", "build", "managed_components", "archive")
               for part in doc.parts):
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for reference in sorted(set(pattern.findall(text))):
            if not reference.endswith(suffixes) or "*" in reference:
                continue
            checked += 1
            # A document inside firmware/keyboard/ may write "tools/verify_qmk.py"
            # meaning its own sibling directory, so try the document's folder
            # before declaring the reference broken.
            if (ROOT / reference).exists() or (doc.parent / reference).exists():
                continue
            broken.append(f"{doc.relative_to(ROOT)} -> {reference}")
    stage.status = "pass" if not broken else "fail"
    stage.detail = (f"{checked} file references checked"
                    if not broken else "\n".join(sorted(set(broken))))


def stage_lint(stage: Stage) -> None:
    """Static checks that need no toolchain: valid UTF-8, parseable, no dead imports.

    Vendored third-party sources are excluded. They are byte-exact copies whose
    digests are recorded in a PROVENANCE.json next to them; editing one to
    satisfy our style would break the only thing that makes it verifiable.
    """
    problems: list[str] = []
    files = []
    for folder in ("tools", "tests", "linux"):
        files += [p for p in sorted((ROOT / folder).rglob("*.py"))
                  if "vendor" not in p.relative_to(ROOT).parts]

    for path in files:
        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Windows PowerShell rewrites files in the ANSI code page unless
            # told otherwise, which silently destroys every non-ASCII byte.
            problems.append(f"{path.relative_to(ROOT)}: not valid UTF-8 at byte {exc.start}")

    result = run([sys.executable, "-m", "pyflakes", "tools", "tests", "linux"])
    if "No module named" in result.stderr:
        stage.status = "unavailable" if not problems else "fail"
        stage.detail = "\n".join(problems) or "pyflakes not installed (pip install pyflakes)"
        return
    problems += [line for line in result.stdout.splitlines()
                 if line.strip() and "vendor" not in line.split(":", 1)[0].replace("\\", "/").split("/")]

    stage.status = "pass" if not problems else "fail"
    stage.detail = (f"{len(files)} Python files checked"
                    if not problems else "\n".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true",
                        help="skip stages that need a C toolchain")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when any Python test was skipped")
    parser.add_argument("--staging", action="store_true",
                        help="also verify the last staged package matches this tree")
    args = parser.parse_args()

    if args.staging:
        os.environ["MIXOS_CHECK_STAGING"] = "1"

    stages: list[tuple[Stage, callable]] = []
    stages.append((Stage("lint", "UTF-8 integrity and unused imports"), stage_lint))
    if not args.quick:
        stages.append((Stage("ctest", "host C unit tests via CMake/CTest"), stage_ctest))
    stages.append((Stage("python", "Python test suite"), stage_python))
    stages.append((Stage("preview", "UI preview renderer (node)"), stage_preview))
    stages.append((Stage("docs", "documentation path references"), stage_docs))

    for stage, function in stages:
        print(f"==> {stage.name}: {stage.description}", flush=True)
        start = time.monotonic()
        try:
            function(stage)
        except Exception as exc:  # a broken stage must not hide the other stages
            stage.status = "fail"
            stage.detail = f"{type(exc).__name__}: {exc}"
        stage.seconds = time.monotonic() - start
        print(f"    {stage.status}: {stage.detail.splitlines()[0] if stage.detail else ''}"
              f"  ({stage.seconds:.1f}s)", flush=True)

    print("\n" + "=" * 72)
    failed = [s for s, _ in stages if s.status == "fail"]
    unavailable = [s for s, _ in stages if s.status == "unavailable"]
    skipped_tests = sum(getattr(s, "skipped", 0) for s, _ in stages)

    for stage, _ in stages:
        print(f"{stage.status:>12}  {stage.name:<10} {stage.detail.splitlines()[0] if stage.detail else ''}")
    if skipped_tests:
        print(f"\n{skipped_tests} Python tests were skipped on this machine. "
              f"Run on Linux, or install WSL with build-essential, to execute them.")
    if unavailable:
        print(f"{len(unavailable)} stage(s) could not run here: "
              + ", ".join(s.name for s in unavailable))

    for stage in failed:
        print(f"\n--- {stage.name} ---\n{stage.detail}")

    if failed:
        return 1
    if args.strict and skipped_tests:
        return 2
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
