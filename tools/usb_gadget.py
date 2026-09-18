#!/usr/bin/env python3
"""Switch the CM5 between its two mutually exclusive USB arrangements.

The Compute Module has one USB controller and a physical switch, SW8, that
decides where it goes. It cannot go to both places:

**Host** — the normal arrangement. The controller drives the internal hub, so
Linux sees the ESP32 (screen, keyboard input, USB audio) and the STM32
keyboard. `mixosd` has a serial port to talk to. This is what the device is for.

**Device** — the maintenance arrangement. The controller becomes a USB
peripheral on the bottom USB-C port, presents itself to a PC as a network
adapter, and the internal hub is gone. The screen goes dark, the keyboard stops,
`mixosd` has nothing to open. What you get in exchange is a link measured in
tens of megabytes a second instead of 0.20.

It is worth the trade exactly once: the language model is 2.6 GB, which is
about three and a half hours over Wi-Fi and a few minutes over USB.

The escape hatch is that **Wi-Fi is not on that USB controller**. `wlan0` is on
the SDIO bus, so it keeps working in either arrangement. If the USB network
never appears, the device is still reachable at its Wi-Fi address and can be put
back with one command. Nothing here can strand the device.

Switching modes is not something software can do alone. `rpi-usb-gadget` writes
the boot configuration and the kernel reads it once, at boot; SW8 is a switch
you move with your finger. So this tool does the part that is software, checks
the part that is not, and says plainly which step is yours.

    $env:MIXOS_SSH_PASSWORD='...'
    py -3.12 tools/usb_gadget.py --status  --host 192.168.1.22
    py -3.12 tools/usb_gadget.py --enable  --host 192.168.1.22
    py -3.12 tools/usb_gadget.py --measure --host 10.12.194.1
    py -3.12 tools/usb_gadget.py --disable --host 10.12.194.1
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_models import Remote, human_time                  # noqa: E402

# Measured on this device, 2026-09-10, with the official rpi-usb-gadget tool.
USB_ADDRESS = '10.12.194.1'
WIFI_ADDRESS = '192.168.1.22'
BOOT_CONFIG = '/boot/firmware/config.txt'
# Enough to measure a rate rather than a handshake, small enough to throw away.
PROBE_BYTES = 32 << 20


def state(remote: Remote) -> dict:
    """What arrangement the device is in, read rather than assumed."""
    script = (
        'set -u\n'
        f'echo "dr_mode=$(grep -o "dr_mode=[a-z]*" {BOOT_CONFIG} | tail -1)"\n'
        'echo "gadget_tool=$(command -v rpi-usb-gadget || echo absent)"\n'
        'echo "usb_net=$(ls /sys/class/net | grep -c "^usb" || true)"\n'
        'echo "hub=$(lsusb | grep -c "FE 2.1" || true)"\n'
        'echo "esp32=$(lsusb | grep -c "303a:80c3" || true)"\n'
        'echo "keyboard=$(lsusb | grep -c "c182:6b11" || true)"\n'
        'echo "mixosd=$(systemctl is-active mixosd.service || true)"\n'
        'echo "uptime=$(cut -d. -f1 /proc/uptime)"\n'
    )
    answers = {}
    for line in remote.text(script, check=False).splitlines():
        key, _, value = line.partition('=')
        answers[key.strip()] = value.strip()
    return answers


def describe(answers: dict) -> str:
    peripheral = 'peripheral' in answers.get('dr_mode', '')
    internal = answers.get('esp32') == '1' or answers.get('hub') == '1'
    if peripheral and not internal:
        return 'Device: the USB network is live and the screen is disconnected'
    if peripheral and internal:
        return ('configured for Device but still wired to the internal hub — '
                'SW8 is on Host, or the device has not rebooted since the change')
    if internal:
        return 'Host: the screen, the keyboard and mixosd are connected'
    return ('Host in the boot configuration, but nothing internal has enumerated; '
            'check SW8 and the power switch')


def report(remote: Remote) -> dict:
    answers = state(remote)
    print(describe(answers))
    for key in ('dr_mode', 'gadget_tool', 'usb_net', 'hub', 'esp32', 'keyboard',
                'mixosd'):
        print(f'  {key:12} {answers.get(key, "?")}')
    print(f'  {"uptime":12} {human_time(float(answers.get("uptime") or 0))}')
    return answers


def measure(remote: Remote) -> float:
    """How fast this link really is, before committing 2.6 GB to it.

    Random bytes, because the SSH transport compresses nothing by default but a
    filesystem might, and a measurement that a zero-filled file flatters is not
    a measurement.
    """
    payload = os.urandom(PROBE_BYTES)
    print(f'Sending {PROBE_BYTES / 1e6:,.0f} MB to /dev/null on the device')
    started = time.monotonic()
    remote.run('cat > /dev/null', data=payload, timeout=900)
    elapsed = max(time.monotonic() - started, 1e-6)
    rate = PROBE_BYTES / elapsed
    print(f'  {rate / 1e6:,.2f} MB/s')
    print(f'  a 2.6 GB model would take about {human_time(2.6e9 / rate)}')
    return rate


def enable(remote: Remote, password: str) -> None:
    answers = state(remote)
    if answers.get('gadget_tool') == 'absent':
        raise SystemExit(
            'rpi-usb-gadget is not on this device. It is the official Raspberry Pi '
            'tool that writes the boot configuration; without it the switch would '
            'have to be made by editing ' + BOOT_CONFIG + ' by hand, which this '
            'tool will not do.')
    if 'peripheral' in answers.get('dr_mode', ''):
        print('Already configured for Device mode.')
    else:
        print('Writing the boot configuration for Device mode')
        result = remote.run('sudo -S -p "" rpi-usb-gadget on',
                            data=(password + '\n').encode(), timeout=120, check=False)
        if result.returncode:
            raise SystemExit('rpi-usb-gadget on failed:\n' +
                             result.stderr.decode('utf-8', 'replace')[-800:])
        after = state(remote)
        if 'peripheral' not in after.get('dr_mode', ''):
            raise SystemExit('The tool reported success but ' + BOOT_CONFIG +
                             ' still does not say dr_mode=peripheral. Stopping '
                             'rather than rebooting into an unknown arrangement.')
        print('  configuration written')

    print('\nRebooting. It comes back on Wi-Fi either way.')
    # Detached, because the connection dies with the reboot and a command that
    # waits for its own exit status here always looks like a failure.
    remote.run('sudo -S -p "" systemd-run --on-active=2 systemctl reboot',
               data=(password + '\n').encode(), timeout=60, check=False)

    print("""
Now the part that is not software:

  1. Wait for the device to come back (about half a minute).
  2. Move SW8, the Host/Device switch on the top-left edge, to Device.
  3. Connect the PC to the bottom USB-C port with a data cable.
  4. Windows enumerates it as a network adapter and gives itself
     10.12.194.8/28; the device is 10.12.194.1.

The screen will be dark and the keyboard dead until you switch back. That is
the arrangement working, not a fault.

Then:

  py -3.12 tools/usb_gadget.py --measure --host 10.12.194.1
  py -3.12 tools/deploy_models.py --host 10.12.194.1 --block-mb 64
""")


def disable(remote: Remote, password: str) -> None:
    answers = state(remote)
    if 'peripheral' not in answers.get('dr_mode', ''):
        print('The boot configuration is already back on Host mode.')
    else:
        print('Restoring the boot configuration for Host mode')
        result = remote.run('sudo -S -p "" rpi-usb-gadget off',
                            data=(password + '\n').encode(), timeout=120, check=False)
        if result.returncode:
            raise SystemExit('rpi-usb-gadget off failed:\n' +
                             result.stderr.decode('utf-8', 'replace')[-800:])
        after = state(remote)
        if 'peripheral' in after.get('dr_mode', ''):
            raise SystemExit(BOOT_CONFIG + ' still says dr_mode=peripheral. The '
                             'device would boot back into Device mode; fix the '
                             'configuration before rebooting.')
        print('  configuration restored')

    print('\nRebooting.')
    # The reboot is scheduled rather than run in the foreground: this command
    # may be arriving over the very network interface it is about to remove.
    remote.run('sudo -S -p "" systemd-run --on-active=2 systemctl reboot',
               data=(password + '\n').encode(), timeout=60, check=False)

    print("""
Now the part that is not software:

  1. Move SW8 back to Host.
  2. Unplug the PC from the bottom USB-C port.
  3. The device comes back with its screen, its keyboard and mixosd.

Confirm it with:

  py -3.12 tools/usb_gadget.py --status --host 192.168.1.22
""")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default=None,
                        help=f'default: {WIFI_ADDRESS} for --status/--enable, '
                             f'{USB_ADDRESS} for --measure/--disable')
    parser.add_argument('--user', default='pi')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--status', action='store_true',
                        help='report which arrangement the device is in')
    action.add_argument('--enable', action='store_true',
                        help='configure Device mode and reboot')
    action.add_argument('--disable', action='store_true',
                        help='configure Host mode and reboot')
    action.add_argument('--measure', action='store_true',
                        help='measure the current link before trusting it with 2.6 GB')
    args = parser.parse_args()

    host = args.host or (USB_ADDRESS if (args.measure or args.disable) else WIFI_ADDRESS)
    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass(
        f'{args.user}@{host} password: ')
    if not password:
        raise SystemExit('No password supplied; set MIXOS_SSH_PASSWORD or type one.')

    with tempfile.TemporaryDirectory(prefix='mixos-usb-') as temporary:
        remote = Remote(host, args.user, password, Path(temporary))
        print(f'{args.user}@{host}')
        if args.status:
            report(remote)
        elif args.measure:
            report(remote)
            measure(remote)
        elif args.enable:
            enable(remote, password)
        else:
            disable(remote, password)
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    sys.exit(main())
