#!/usr/bin/env python3
"""Generate offline provisioning commands for separate administrator review.

This program only prints shell text. It never executes privileged operations,
installs services, or grants bootstrap task/ROM/reset permission. Bootstrap's
immutable approvals and trusted supervisor remain a separate installation gate.
"""
import argparse
import hashlib
import re
import shlex

DEVICE_NAME = 'usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03'


def commands(user, group='dialout'):
    for name in (user, group):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name):
            raise ValueError('a plain local account/group name is required')
    if user == 'root':
        raise ValueError('worker must be an ordinary user, not root')
    lock = '/run/lock/mixos-esp-update/' + hashlib.sha256(DEVICE_NAME.encode()).hexdigest() + '.lock'
    policy = (user + ' ALL=(root) NOPASSWD: /usr/bin/systemctl stop mixosd.service, '
              '/usr/bin/systemctl start mixosd.service')
    tmpfiles = (f'd /run/lock/mixos-esp-update 0750 root {group} -\n'
                f'f {lock} 0660 root {group} -')
    return [
        '# Ordinary account must already have serial access and membership in ' + group + '.',
        '# Never delete/recreate a held lock inode; provision only while all maintenance is stopped.',
        "printf '%s\\n' " + shlex.quote(tmpfiles) + ' | sudo tee /etc/tmpfiles.d/mixos-esp-update.conf >/dev/null',
        'sudo systemd-tmpfiles --create /etc/tmpfiles.d/mixos-esp-update.conf',
        'sudo install -d -o root -g root -m 0755 /var/lib/mixos',
        f'sudo install -d -o {user} -g {group} -m 0700 /var/lib/mixos/bootstrap-ota',
        'sudo loginctl enable-linger ' + user,
        'sudo install -d -o root -g root -m 0755 /etc/sudoers.d',
        # The temporary policy is checked before it enters sudoers.d.
        "printf '%s\\n' " + shlex.quote(policy) + ' | sudo tee /etc/mixos-esp-update.sudoers.pending >/dev/null',
        'sudo visudo -cf /etc/mixos-esp-update.sudoers.pending && sudo install -o root -g root -m 0440 /etc/mixos-esp-update.sudoers.pending /etc/sudoers.d/mixos-esp-update',
        '# This provisions native OTA permissions only; bootstrap supervisor/immutable approvals are NOT installed.',
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--group', default='dialout')
    args = parser.parse_args(argv)
    try:
        lines = commands(args.user, args.group)
    except ValueError as exc:
        parser.error(str(exc))
    print('# Review and execute separately as an administrator; this tool executes nothing.')
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
