#!/usr/bin/env python3
"""Bounded NetworkManager wrapper for MixOS.

Every call is an explicit argument vector passed to `nmcli`; no string is ever
handed to a shell, and no caller-supplied text becomes part of a command name
or option. An SSID or passphrase is only ever a single positional value. The
module never runs as root: it relies on the service user already being allowed
to manage connections, and reports an authorisation failure as an ordinary
error rather than escalating.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

SSID_MAX = 32
PSK_MAX = 63
SCAN_MAX = 12
FRAME_LIMIT = 512
# Terse output with nmcli's own escaping: a colon inside an SSID arrives as
# "\:" and is put back by _split, so a network named "a:b" stays one field.
_TERSE = ['--terse']
TIMEOUT_QUERY = 6
TIMEOUT_SCAN = 8
TIMEOUT_CONNECT = 45
# The device expires NET_SCAN after 20 seconds. Leave time for helper startup,
# daemon polling and serial delivery; every scan subprocess shares this budget.
TIMEOUT_SCAN_REQUEST = 16
TIMEOUT_ACTIVATE = 2


class NetError(Exception):
    """A request that failed for a reason worth showing on the device."""


class NetTimeout(NetError):
    """A timed-out activation may still be running in NetworkManager."""


def _binary():
    path = shutil.which('nmcli')
    if not path:
        raise NetError('NetworkManager is not installed on this host')
    return path


def _env():
    # A predictable locale keeps nmcli's own words out of the parsed fields.
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    return env


def _reason(text, code):
    lines = (text or '').strip().splitlines()
    detail = lines[0].strip() if lines else ''
    lowered = detail.lower()
    if 'not authorized' in lowered or 'permission' in lowered or 'policykit' in lowered:
        return 'not allowed to manage Wi-Fi; add the service user to netdev'
    if detail:
        return detail[:200]
    return 'nmcli exited with status %d' % code


def _run(args, timeout):
    try:
        done = subprocess.run(
            [_binary()] + args,
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=_env(), check=False)
    except subprocess.TimeoutExpired:
        raise NetTimeout('NetworkManager did not answer in time')
    except OSError as exc:
        raise NetError('cannot run nmcli: %s' % (exc.strerror or exc))
    if done.returncode != 0:
        raise NetError(_reason(done.stderr or done.stdout, done.returncode))
    return done.stdout


def _split(line):
    """Splits one terse nmcli line, honouring its backslash escaping."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == '\\':
            escaped = True
        elif ch == ':':
            fields.append(''.join(current))
            current = []
        else:
            current.append(ch)
    fields.append(''.join(current))
    return fields


def _fields(line, count):
    got = _split(line)
    return got if len(got) == count else None


def _check_ssid(ssid):
    if not isinstance(ssid, str) or not ssid:
        raise NetError('empty network name')
    if len(ssid.encode('utf-8')) > SSID_MAX:
        raise NetError('network name is too long')
    # A control character cannot appear in a real SSID and has no place in an
    # argument vector either.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in ssid):
        raise NetError('network name contains control characters')
    return ssid


def _check_psk(psk):
    if psk is None:
        return ''
    if not isinstance(psk, str):
        raise NetError('invalid passphrase')
    if len(psk) > PSK_MAX:
        raise NetError('passphrase is too long')
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in psk):
        raise NetError('passphrase contains control characters')
    return psk


def wifi_device():
    """The first Wi-Fi interface NetworkManager reports, or None."""
    out = _run(_TERSE + ['--fields', 'TYPE,DEVICE', 'device'], TIMEOUT_QUERY)
    for line in out.splitlines():
        got = _fields(line, 2)
        if got and got[0] == 'wifi':
            return got[1]
    return None


def _run_before(args, timeout, deadline):
    """Apply one request's remaining budget to each nmcli subprocess."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NetTimeout('network request deadline reached')
    return _run(args, min(timeout, remaining))


def saved_profiles(deadline=None):
    """Map real SSIDs to profile metadata without reading any stored secrets.

    connection.id (NAME) is only a user-assigned label. Enumerate UUIDs, then
    fetch actual wireless settings in one batch rather than N timed queries.
    """
    if deadline is None:
        deadline = time.monotonic() + TIMEOUT_QUERY
    out = _run_before(_TERSE + ['--fields', 'TYPE,UUID', 'connection', 'show'],
                      TIMEOUT_QUERY, deadline)
    uuids = []
    for line in out.splitlines():
        got = _fields(line, 2)
        if got and got[0] in ('802-11-wireless', 'wifi') and got[1]:
            uuids.append(got[1])
    if not uuids:
        return {}
    args = _TERSE + ['--mode', 'multiline', '--fields',
                    'connection.uuid,802-11-wireless.ssid,802-11-wireless.mode,'
                    '802-11-wireless-security.key-mgmt', 'connection', 'show']
    for uuid in uuids:
        args += ['uuid', uuid]
    out = _run_before(args, TIMEOUT_QUERY, deadline)
    records, record = [], {}
    for line in out.splitlines():
        # Unlike terse tabular output, multiline values are not colon/backslash
        # escaped. Split only the property separator and preserve the SSID.
        key, separator, value = line.partition(':')
        if not separator:
            continue
        if key == 'connection.uuid':
            record = {'uuid': value}
            records.append(record)
        else:
            record[key] = value
    profiles = {}
    expected = set(uuids)
    for record in records:
        ssid = record.get('802-11-wireless.ssid', '')
        if record['uuid'] in expected and ssid:
            profiles.setdefault(ssid, []).append(record)
    return profiles


def _strength(text):
    try:
        return max(0, min(100, int(text)))
    except (TypeError, ValueError):
        return 0


def scan(rescan=True, auto_connect=False):
    """Inspect every AP before limiting the display to SCAN_MAX networks."""
    deadline = time.monotonic() + TIMEOUT_SCAN_REQUEST
    args = _TERSE + ['--fields', 'SIGNAL,SECURITY,IN-USE,SSID', 'device', 'wifi', 'list',
                    '--rescan', 'yes' if rescan else 'no']
    out = _run_before(args, TIMEOUT_SCAN if rescan else TIMEOUT_QUERY, deadline)
    try:
        known = saved_profiles(deadline)
    except NetError:
        known = {}
    best = {}
    any_active = False
    for line in out.splitlines():
        got = _fields(line, 4)
        if not got:
            continue
        signal, security, in_use, ssid = got
        active = in_use.strip() == '*'
        any_active |= active  # Includes hidden APs and rows beyond SCAN_MAX.
        if not ssid:
            continue  # a hidden network has nothing to show or tap
        strength = _strength(signal)
        security = security.strip()
        entry = {
            'ssid': ssid,
            'signal': strength,
            'secured': bool(security) and security != '--',
            'known': ssid in known,
            'active': active,
        }
        previous = best.get(ssid)
        if previous is not None:
            # A stronger BSSID must not erase an association to a weaker one.
            entry['active'] |= previous['active']
            previous['active'] = entry['active']
        if previous is None or strength > previous['signal']:
            best[ssid] = entry
    ordered = sorted(best.values(), key=lambda e: (-e['signal'], e['ssid']))
    if auto_connect and not any_active:
        auto_connect_saved(ordered, known, deadline)
    return ordered[:SCAN_MAX]


def _wifi_idle(deadline):
    """Fail closed unless all Wi-Fi devices are idle and one is usable.

    Check the devices themselves, not a truncated/stale AP listing. This also
    protects hidden associations and connections currently being established.
    _env() fixes the locale, so these device-state names are stable.
    """
    out = _run_before(_TERSE + ['--fields', 'TYPE,STATE,DEVICE', 'device', 'status'],
                      TIMEOUT_QUERY, deadline)
    usable = False
    for line in out.splitlines():
        got = _fields(line, 3)
        if not got:
            return False
        kind, state, device = got
        if kind != 'wifi':
            continue
        if state not in ('disconnected', 'unavailable', 'unmanaged'):
            return False
        if state == 'disconnected' and _valid_interface(device):
            usable = True
    return usable


def activate_saved(uuid, deadline=None):
    """Submit a UUID activation; NetworkManager supplies its stored secrets.

    --wait 0 only waits for acceptance, not authentication/DHCP. Acceptance is
    not evidence of an association: the normal state query confirms that later.
    Let NetworkManager select a compatible interface rather than forcing the
    first radio (the profile may be bound to a different radio).
    """
    if deadline is None:
        deadline = time.monotonic() + TIMEOUT_ACTIVATE
    _run_before(['--wait', '0', 'connection', 'up', 'uuid', uuid],
                TIMEOUT_ACTIVATE, deadline)


def auto_connect_saved(entries, profiles, deadline):
    """Best effort within the scan budget, without displacing a connection."""
    if any(entry.get('active') for entry in entries):
        return ''
    for entry in entries:
        if not entry.get('secured'):
            continue
        for profile in profiles.get(entry['ssid'], []):
            if profile.get('802-11-wireless.mode') not in ('', 'infrastructure'):
                continue
            if profile.get('802-11-wireless-security.key-mgmt', '') in ('', '--', 'owe'):
                # OWE encrypts an open network without a password. Neither it
                # nor a same-named open profile qualifies as saved credentials.
                continue
            try:
                if not _wifi_idle(deadline):
                    return ''
            except NetError:
                return ''  # Unknown state must never be treated as offline.
            try:
                activate_saved(profile['uuid'], deadline)
            except NetTimeout:
                return ''  # Acceptance is unknown; do not start a rival attempt.
            except NetError:
                continue  # Immediate rejection: recheck state before another UUID.
            return 'activation requested for %s' % entry['ssid']
    return ''


def connect(ssid, passphrase=None):
    ssid = _check_ssid(ssid)
    passphrase = _check_psk(passphrase)
    args = ['device', 'wifi', 'connect', ssid]
    if passphrase:
        args += ['password', passphrase]
    try:
        device = wifi_device()
    except NetError:
        device = None
    if device:
        args += ['ifname', device]
    _run(args, TIMEOUT_CONNECT)
    return 'connected to %s' % ssid


def forget(ssid):
    ssid = _check_ssid(ssid)
    deadline = time.monotonic() + 8  # The daemon allows ten seconds for forget.
    profiles = saved_profiles(deadline).get(ssid, [])
    if not profiles:
        raise NetError('no saved profile for %s' % ssid)
    args = ['connection', 'delete']
    for profile in profiles:
        args += ['uuid', profile['uuid']]
    _run_before(args, TIMEOUT_QUERY, deadline)
    return 'forgot %s' % ssid


_INTERFACE_CHARS = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-')


def _valid_interface(device):
    """Accept only a kernel interface identity reported by NetworkManager."""
    if not isinstance(device, str) or not device or device in ('.', '..') or len(device) > 15:
        return False
    return all(ch in _INTERFACE_CHARS for ch in device)


def _interface_bytes(device, direction):
    """Read one counter for one explicitly reported, validated interface."""
    if not _valid_interface(device) or direction not in ('rx', 'tx'):
        return None
    try:
        path = Path('/sys/class/net') / device / 'statistics' / f'{direction}_bytes'
        value = int(path.read_text())
        return value if 0 <= value <= 0xffffffffffffffff else None
    except (OSError, ValueError, TypeError):
        return None


def _interface_link(device):
    """Return interface and carrier generations while the link is up."""
    if not _valid_interface(device):
        return None
    try:
        path = Path('/sys/class/net') / device
        if (path / 'carrier').read_text().strip() != '1':
            return None
        index = int((path / 'ifindex').read_text())
        changes = int((path / 'carrier_changes').read_text())
        return (index, changes) if index > 0 and changes >= 0 else None
    except (OSError, ValueError):
        return None


def state():
    """Current Wi-Fi state for the status report, or None when unknown.

    None and "not connected" are different answers: the device shows an absent
    report as unknown rather than inventing a disconnected radio. The active
    interface identity lets the persistent daemon sample the kernel counters
    independently of this slower NetworkManager query.
    """
    try:
        out = _run(_TERSE + ['--fields', 'IN-USE,DEVICE,SIGNAL,SSID',
                             'device', 'wifi', 'list', '--rescan', 'no'], TIMEOUT_QUERY)
    except NetError:
        return None
    result = {'connected': False, 'ssid': '', 'signal': None}
    for line in out.splitlines():
        got = _fields(line, 4)
        if not got:
            continue
        in_use, device, signal, ssid = got
        if in_use.strip() != '*':
            continue
        result.update(connected=True, ssid=ssid,
                      signal=_strength(signal))
        if _valid_interface(device):
            result['interface'] = device
        break
    return result


def local_address(device=None):
    """The host's own address on the Wi-Fi interface, or an empty string."""
    try:
        name = device or wifi_device()
        if not name:
            return ''
        out = _run(_TERSE + ['--fields', 'IP4.ADDRESS', 'device', 'show', name], TIMEOUT_QUERY)
    except NetError:
        return ''
    for line in out.splitlines():
        got = _split(line)
        if len(got) >= 2:
            value = got[1].strip()
            if value and value != '--':
                return value.split('/')[0]
    return ''


def pack_scan(entries):
    """Packs a scan result into one bounded frame payload.

    Layout: count:u8 then per entry flags:u8, signal:u8, length:u8, SSID bytes.
    An entry that would overflow the frame is dropped rather than truncated,
    because half an SSID is not a network anyone can choose.
    """
    body = bytearray()
    kept = 0
    for entry in entries[:SCAN_MAX]:
        name = entry['ssid'].encode('utf-8')[:SSID_MAX]
        if not name:
            continue
        flags = (1 if entry.get('secured') else 0) | (2 if entry.get('known') else 0)
        chunk = bytes([flags, min(100, int(entry.get('signal', 0))), len(name)]) + name
        if 1 + len(body) + len(chunk) > FRAME_LIMIT:
            break
        body += chunk
        kept += 1
    return bytes([kept]) + bytes(body)


def unpack_request(payload):
    """Reads the SSID and optional passphrase from a NET_CONNECT/FORGET frame."""
    at = 0
    values = []
    while at < len(payload) and len(values) < 2:
        length = payload[at]
        at += 1
        if at + length > len(payload):
            raise NetError('truncated network request')
        values.append(payload[at:at + length].decode('utf-8', 'replace'))
        at += length
    if not values:
        raise NetError('empty network request')
    values.append('')
    return values[0], values[1]


class Throttle:
    """Keeps repeated status queries off the critical path of the event loop."""

    def __init__(self, interval, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.due = 0.0
        self.value = None

    def get(self, produce):
        now = self.clock()
        if now >= self.due:
            self.due = now + self.interval
            try:
                self.value = produce()
            except NetError:
                self.value = None
        return self.value


def handle(request):
    """Runs one request. Returns the answer the daemon forwards to the device."""
    if not isinstance(request, dict):
        return {'ok': False, 'error': 'malformed network request'}
    verb = request.get('verb')
    try:
        if verb == 'scan':
            return {'ok': True, 'networks': scan(auto_connect=True)}
        if verb == 'state':
            current = state()
            observed_at = time.monotonic()
            device = current.get('interface') if current and current.get('connected') else None
            return {'ok': current is not None, 'state': current,
                    'observed_at': observed_at, 'ip': local_address(device),
                    'error': '' if current is not None else 'Wi-Fi state unavailable'}
        if verb == 'connect':
            return {'ok': True, 'message': connect(request.get('ssid'),
                                                   request.get('passphrase'))}
        if verb == 'forget':
            return {'ok': True, 'message': forget(request.get('ssid'))}
    except NetError as exc:
        return {'ok': False, 'error': str(exc)}
    return {'ok': False, 'error': 'unsupported network request'}


def main():
    """Reads one JSON request on stdin and writes one JSON answer on stdout.

    The request never travels on argv, so a Wi-Fi passphrase does not appear in
    the process table of a machine anyone can run `ps` on.
    """
    try:
        request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except (ValueError, UnicodeDecodeError, OSError):
        request = None
    answer = handle(request)
    sys.stdout.write(json.dumps(answer, separators=(',', ':'), allow_nan=False))
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
