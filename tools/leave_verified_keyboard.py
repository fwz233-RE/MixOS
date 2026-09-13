#!/usr/bin/env python3
"""One leave-only diagnostic after fresh full-flash verification; never programs."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flash_keyboard_on_pi as k

EXPECTED = '83787b1b472d0452825fe4732c141a6a50227a99379b4e89ed571b24bc495920'
SERIAL = 'FFFFFFFEFFFF'


def leave_verified(workdir, backend=None):
    backend = backend or k.Backend()
    audit = k.Audit(workdir)
    attempted = False
    try:
        identity = backend.identity(SERIAL)
        audit.event('leave_only_start', identity=identity, expected_full_sha256=EXPECTED)
        base = k.selectors(SERIAL, identity['devnum'])

        def run(operation, *args):
            k.require(backend.identity(SERIAL) == identity, 'Device identity changed')
            command = base + list(args)
            audit.event('command_start', operation=operation, argv=command)
            result = backend.run(command)
            audit.event('command_end', operation=operation, returncode=result.returncode, output=result.stdout)
            k.require(result.returncode == 0, operation + ' failed; no retry')
            return result.stdout

        k.validate_listing(run('inspect', '--list'), identity)
        run('verify_upload', '-s', '0x08000000:32768', '-U', str(audit.directory / 'readback.bin'))
        data = audit.finish_upload('readback.bin')
        k.require(k.sha256(data) == EXPECTED, 'Full flash differs from verified deployment')
        audit.event('full_flash_verified', sha256=EXPECTED, bytes=len(data))
        k.validate_listing(run('inspect', '--list'), identity)
        audit.event('leave_only_intent', flash_programming=False)
        attempted = True
        run('leave_only', '-s', '0x08000000:leave')
        audit.event('leave_requested', application_health='not_verified', flash_programming=False)
        return {'status': 'verified_leave_only_requested', 'audit': str(audit.directory)}
    except BaseException as error:
        audit.event('failed', error=str(error), leave_attempted=attempted, retry_allowed=False)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--workdir', type=Path, required=True)
    parser.add_argument('--execute-leave-only', action='store_true', required=True)
    args = parser.parse_args()
    k.require(sys.platform == 'linux' and os.geteuid() == 0, 'Requires isolated root Linux execution')
    k.trusted_parent(args.workdir)
    with k.termination_guard(), k.device_lock():
        print(json.dumps(leave_verified(args.workdir)))


if __name__ == '__main__':
    main()
