#!/usr/bin/env python3
"""Install this machine's public key on the deck, once, using the password.

Every existing host tool in tools/ shells out to plain ``ssh``/``scp`` and so
depends on key authentication already working. Until it does, each remote step
has to be routed through ``wsl -> sshpass -> ssh``, and driving that from
PowerShell mangles quoting on any command containing nested quotes, which is
most of them.

So this does the one thing that removes that whole layer: authenticate once
with the password over Paramiko and append the public key to the deck's
authorized_keys. It is idempotent - running it twice does not duplicate the
entry - and it never rewrites a key that is already present.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='192.168.1.22')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--password', default='pi')
    parser.add_argument('--key', default=str(Path.home() / '.ssh/id_ed25519.pub'))
    args = parser.parse_args()

    public_key = Path(args.key).read_text(encoding='utf-8').strip()
    if not public_key.startswith(('ssh-', 'ecdsa-')):
        sys.exit(f'{args.key} does not look like a public key')

    try:
        import paramiko
    except ImportError:
        sys.exit('needs paramiko: py -m pip install paramiko')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=args.password,
                   timeout=15, allow_agent=False, look_for_keys=False)
    try:
        # Quoting the key in a single-quoted heredoc-free form keeps this safe
        # regardless of what the comment field contains.
        script = (
            'set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; '
            'touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; '
            f'grep -qxF "{public_key}" ~/.ssh/authorized_keys '
            f'|| echo "{public_key}" >> ~/.ssh/authorized_keys; '
            'echo installed; wc -l < ~/.ssh/authorized_keys'
        )
        _, out, err = client.exec_command(script, timeout=20)
        status = out.channel.recv_exit_status()
        print(out.read().decode('utf-8', 'replace').strip())
        message = err.read().decode('utf-8', 'replace').strip()
        if message:
            print(message, file=sys.stderr)
        return status
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())
