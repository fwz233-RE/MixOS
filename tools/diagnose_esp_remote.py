#!/usr/bin/env python3
"""Read-only Pi USB/network/power diagnostics; never opens or resets the ESP serial port."""
import argparse
import getpass
import os
import shlex
from flash_esp_remote import connect, run

COMMAND = r'''
export SYSTEMD_PAGER=cat PAGER=cat
section() { printf '\n=== %s ===\n' "$1"; }
section 'TIME / UPTIME / LOAD'
date -Is; hostname; uptime; free -m; df -h / /home
section 'NETWORK ROUTES AND INTERFACE HARDWARE'
ip -br address; ip route; ip -s link
for p in /sys/class/net/*; do echo "$p -> $(readlink -f "$p/device")"; done
if test -x /usr/sbin/iw; then sudo -n /usr/sbin/iw dev; sudo -n /usr/sbin/iw dev wlan0 link; sudo -n /usr/sbin/iw dev wlan0 get power_save; fi
command -v nmcli >/dev/null && nmcli -f GENERAL.DEVICE,GENERAL.STATE,GENERAL.CONNECTION device show
section 'POWER AND TEMPERATURE'
command -v vcgencmd >/dev/null && { vcgencmd get_throttled; vcgencmd measure_temp; }
section 'USB TOPOLOGY / SERIAL OWNERS'
# USB enumeration queries are bounded: an unresponsive device must not block logs.
timeout -k 2 5 lsusb -t; ls -l /dev/serial/by-id/; timeout -k 2 5 fuser -v /dev/ttyACM* 2>&1
for p in /sys/bus/usb/devices/*; do
 if test -f "$p/idVendor"; then
  printf '%s ' "$p"; for f in idVendor idProduct product serial power/control power/runtime_status power/autosuspend_delay_ms; do printf '%s=' "$f"; cat "$p/$f" 2>/dev/null | tr '\n' ' '; done; echo
 fi
done
section 'ACTIVE UPDATE / SERIAL / TOOLBOX PROCESSES'
ps -eo pid,ppid,etimes,stat,args | grep -E 'flash_esp|esptool|typixdeck|mixos|ModemManager|brltty' | grep -v grep
section 'SSH SERVICE AND RECENT CONNECTION EVENTS'
systemctl --no-pager --full status ssh.service ssh.socket
sudo -n journalctl -u ssh.service --since '4 hours ago' -n 100 --no-pager -o short-iso
section 'KERNEL USB / POWER / NETWORK EVENTS'
sudo -n journalctl -k -b --no-pager -o short-iso | grep -Ei 'under.?voltage|voltage|over.?current|throttl|usb|dwc|brcm|wlan|firmware|mmc.*error|reset|disconnect' | tail -n 180
section 'NETWORK MANAGER EVENTS'
sudo -n journalctl -u NetworkManager --since '4 hours ago' -n 100 --no-pager -o short-iso
section 'PREVIOUS UPDATE AUDITS'
for p in /home/*/mixos-flash-*/flash-audit.jsonl; do test -f "$p" && { echo "$p"; tail -n 12 "$p"; }; done
section 'TOOLBOX USB / FLASH IMPLEMENTATION'
if test -r /usr/local/bin/typixdeck-toolbox; then grep -nE -C 5 'esptool|REBOOT_TO_BOOT|usbreset|unbind|bind|power_save|nmcli|GPIO|gpioset|systemctl|ttyACM' /usr/local/bin/typixdeck-toolbox | head -n 240; fi
true
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--address', help='Alternate address of same host, preserving trusted host-key verification')
    parser.add_argument('--user', default='pi')
    parser.add_argument('--sudo', action='store_true', help='Read privileged kernel/Wi-Fi diagnostics using sudo; no changes')
    parser.add_argument('--usb-only', action='store_true', help='Short USB driver log and serial hardware report')
    parser.add_argument('--wifi-only', action='store_true', help='Short Wi-Fi and kernel report for unstable links')
    parser.add_argument('--disable-wifi-power-save', action='store_true',
                        help='Temporarily disable wlan0 power saving without reconnecting (requires --sudo)')
    args = parser.parse_args()
    password = os.environ.get('MIXOS_SSH_PASSWORD') or getpass.getpass('SSH password: ')
    command = COMMAND
    if args.wifi_only:
        command = """/usr/sbin/iw dev wlan0 link; /usr/sbin/iw dev wlan0 get power_save;
        journalctl -k -b --no-pager -o short-iso | grep -Ei 'under.?voltage|over.?current|brcm|wlan|usb.*(reset|disconnect|error)|dwc.*(error|warn)' | tail -n 35;
        journalctl -u NetworkManager --since '4 hours ago' --no-pager -o short-iso | tail -n 15;
        systemctl --user is-system-running; true"""
    if args.usb_only:
        command = """date -Is; uptime; vcgencmd get_throttled;
        timeout -k 2 5 lsusb -t; python3 -m serial.tools.list_ports -v;
        grep -nE 'ant1|ant2|noant|dwc2|otg_mode|usb' /boot/firmware/config.txt;
        /usr/sbin/iw dev wlan0 station dump;
        journalctl -k -b --since '30 minutes ago' --no-pager -o short-iso | tail -n 60;
        ls -l /usr/local/bin/typixdeck-toolbox; head -n 25 /usr/local/bin/typixdeck-toolbox;
        ls -d /home/pi/.venv*/esptool* /home/pi/.venvs/esptool /usr/lib/python3/dist-packages/esptool/targets/stub_flasher 2>/dev/null;
        ps -eo pid,ppid,stat,args | grep -E 'lsusb|flash_esp|esptool' | grep -v grep; true"""
    if args.disable_wifi_power_save:
        if not args.sudo:
            parser.error('--disable-wifi-power-save requires --sudo')
        command = """set -e; /usr/sbin/iw dev wlan0 get power_save;
        /usr/sbin/iw dev wlan0 set power_save off;
        /usr/sbin/iw dev wlan0 get power_save; /usr/sbin/iw dev wlan0 link;
        ping -c 10 -W 1 192.168.1.1"""
    client = connect(args.host, args.user, password, args.address)
    try:
        if args.sudo:
            return run(client, "sudo -S -p '' sh -c " + shlex.quote(command), timeout=180,
                       input_data=(password + '\n').encode())
        return run(client, command, timeout=180)
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
