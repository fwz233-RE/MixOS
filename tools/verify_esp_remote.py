#!/usr/bin/env python3
"""Verify the running MixOS USB protocol over SSH; never writes flash."""
import argparse
import getpass
import os
import shlex
from flash_esp_remote import connect, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', default='pi')
    parser.add_argument('--staging', required=True)
    args = parser.parse_args()
    if not args.staging.startswith('/home/pi/mixos-flash-'):
        parser.error('unexpected staging path')
    code = (
        "from flash_esp_on_pi import ports,verify_running; "
        "p=[x for x in ports() if x[1]['vid']=='303a' and x[1]['pid']=='80c3' "
        "and x[1]['serial']=='TD0720']; "
        "assert len(p)==1,repr(p); print(verify_running(*p[0]))"
    )
    env = f"PYTHONPATH={args.staging}/tools:{args.staging}/linux"
    command = env + ' ' + shlex.join(['python3', '-c', code])
    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass('SSH password: ')
    client = connect(args.host, args.user, password)
    try:
        return run(client, command, timeout=50)
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
