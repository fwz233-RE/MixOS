#!/usr/bin/env python3
"""Reversible boot-target change; dry-run by default. Never removes drivers."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

TARGETS = {'graphical.target', 'multi-user.target'}


def current_target():
    return subprocess.check_output(['systemctl', 'get-default'], text=True).strip()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['enable', 'restore'])
    p.add_argument('--backup', type=Path, required=True)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--confirm', help='must equal the requested target')
    a = p.parse_args(argv)
    if sys.platform != 'linux':
        p.error('requires Linux/systemd; no changes made')
    old = current_target()
    if old not in TARGETS:
        p.error('unknown default target; manual review required')
    if a.action == 'enable':
        target = 'multi-user.target'
        if a.backup.exists():
            p.error('backup already exists; will not overwrite it')
        backup = {'version': 1, 'original_target': old, 'applied_target': target}
    else:
        backup = json.loads(a.backup.read_text(encoding='utf-8'))
        target = backup.get('original_target')
        if (backup.get('version') != 1 or target not in TARGETS
                or backup.get('applied_target') != 'multi-user.target'):
            p.error('invalid backup; manual review required')
        if old != backup['applied_target']:
            p.error('target changed since enable; refusing to overwrite operator changes')
    print(json.dumps({'dry_run': not a.execute, 'from': old, 'to': target,
                      'backup': str(a.backup), 'takes_effect': 'next boot'}, indent=2))
    if not a.execute:
        return 0
    if os.geteuid() != 0 or a.confirm != target:
        p.error('explicit root invocation and --confirm target required; never invokes sudo')
    if a.action == 'enable':
        fd = os.open(a.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(backup, stream)
            stream.flush()
            os.fsync(stream.fileno())
    subprocess.run(['systemctl', 'set-default', target], check=True)
    print('Default target changed. Existing desktop is untouched; backup retained.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
