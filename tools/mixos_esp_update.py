#!/usr/bin/env python3
"""Linux-native, durable ESP update jobs. No source checkout or SSH is needed.

Release schema (all hashes are full lowercase SHA256, all sizes are integers):
  schema: "mixos-esp-release/v2", protocol: 2
  chip: {name: "esp32s3", id: 9}
  app: {file: "app.bin", size: int, sha256: hex, elf_sha256: hex}
  layout: {ota_0: {address: 65536, size: 2031616},
           ota_1: {address: 6356992, size: 2031616}}
  effective_config: {CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE: true, ...}
  provenance: {mode: str, build_report_sha256: hex, sdkconfig_sha256: hex,
               partition_table_sha256: hex, ...}
  protected_baseline: optional recovery-baseline JSON object, including
      image_sha256, image_bytes, elf_sha256, slot="ota_0".

A package is app.bin + manifest.json; it may additionally carry this runner's
standard-library files. Manifests provide consistency/provenance, not signing.
`apply` is explicit authorization and submits a systemd user service after
verifying that the user manager has lingering enabled. Exit 2 means queued or
running, never success. `status` uses exactly the same durable result as apply
and deploy_ota. Root access is limited to fixed mixosd.service stop/start.
Installers provision the shared lock directory, user-manager lingering, and
narrow sudo/polkit permissions; this program never installs privilege policy.
"""
import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
import ota_esp
import ota_v2 as v2
from serial_transport import SerialTransport, validate_device_path

SCHEMA = 'mixos-esp-release/v2'
SERVICE = 'mixosd.service'
DEVICE = '/dev/serial/by-id/usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03'
SLOT_SIZE = 0x1F0000
LAYOUT = {'ota_0': {'address': 0x10000, 'size': SLOT_SIZE},
          'ota_1': {'address': 0x610000, 'size': SLOT_SIZE}}
BASELINE_SHA = '7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f'
BASELINE_ELF = 'cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a'
BASELINE_SIZE = 894560
ROOT = Path(__file__).resolve().parents[1]
RUNNER_FILES = ('tools/mixos_esp_update.py', 'tools/ota_v2.py', 'tools/ota_esp.py',
                'linux/protocol.py', 'linux/serial_transport.py', 'linux/mixos-esp-update')
DEFAULT_STATE = Path.home() / '.local/state/mixos-esp-update'
DEFAULT_LOCKS = Path('/run/lock/mixos-esp-update')
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
REQUIRED_CONFIG = {
    'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE': True,
    'CONFIG_LCD_RGB_ISR_IRAM_SAFE': False,
    'CONFIG_GDMA_ISR_IRAM_SAFE': False,
    'CONFIG_SPIRAM_XIP_FROM_PSRAM': True,
    'CONFIG_SPIRAM_FETCH_INSTRUCTIONS': True,
    'CONFIG_SPIRAM_RODATA': True,
    'CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT': True,
    'CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT': False,
    'CONFIG_ESP_TASK_WDT_EN': True,
    'CONFIG_ESP_TASK_WDT_INIT': True,
    'CONFIG_ESP_TASK_WDT_PANIC': True,
    'CONFIG_BOOTLOADER_WDT_ENABLE': True,
    'CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE': True,
}


class JobError(Exception):
    pass


def full_hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def strict_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise JobError('duplicate JSON field: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs)
    except (ValueError, OSError) as exc:
        raise JobError(f'cannot read JSON {path}: {exc}') from exc


@dataclass
class Release:
    image: bytes
    manifest: dict


def load_release(package):
    package = Path(package)
    manifest = strict_json(package / 'manifest.json')
    image, digest = ota_esp.inspect_image(package / 'app.bin')
    expected = ota_esp.describe_image(image)
    try:
        if manifest['schema'] != SCHEMA or manifest['protocol'] != 2:
            raise JobError('release must declare the v2 schema and receiver protocol')
        if manifest['chip'] != {'name': 'esp32s3', 'id': 9}:
            raise JobError('release chip is not ESP32-S3')
        app = manifest['app']
        if (app['file'] != 'app.bin' or type(app['size']) is not int or
                app['size'] != len(image) or not 1024 <= len(image) <= SLOT_SIZE or
                not full_hash(app['sha256']) or app['sha256'] != digest.hex() or
                not full_hash(app['elf_sha256']) or
                app['elf_sha256'] != expected['elf_sha256'].hex() or
                expected['elf_sha256'] == bytes(32)):
            raise JobError('release app size, exact file SHA256 or full ELF identity is inconsistent')
        if manifest['layout'] != LAYOUT:
            raise JobError('release must describe the supported exact A/B layout')
        config = manifest['effective_config']
        if not isinstance(config, dict) or any(config.get(key) is not required for key, required in REQUIRED_CONFIG.items()):
            raise JobError('effective safety configuration is missing or unsafe: ' +
                           ', '.join(key for key, required in REQUIRED_CONFIG.items()
                                     if not isinstance(config, dict) or config.get(key) is not required))
        replacement = manifest.get('verified_replacement')
        if replacement is not None and (not isinstance(replacement, dict) or replacement.get('slot') != 'ota_1'
                or type(replacement.get('image_bytes')) is not int or not 1024 <= replacement['image_bytes'] <= SLOT_SIZE
                or not full_hash(replacement.get('image_sha256')) or not full_hash(replacement.get('elf_sha256'))
                or replacement['elf_sha256'] == '0' * 64):
            raise JobError('verified_replacement must bind a known ota_1 package size, file SHA and full ELF')
        provenance = manifest['provenance']
        if (not isinstance(provenance, dict) or not isinstance(provenance.get('mode'), str) or
                not provenance['mode'] or any(not full_hash(provenance.get(name)) for name in
                ('build_report_sha256', 'sdkconfig_sha256', 'partition_table_sha256'))):
            raise JobError('release lacks full build/configuration/layout provenance hashes')
        baseline = manifest.get('protected_baseline')
        if baseline is not None and (baseline.get('slot') != 'ota_0' or
                baseline.get('image_sha256') != BASELINE_SHA or
                baseline.get('image_bytes') != BASELINE_SIZE or baseline.get('elf_sha256') != BASELINE_ELF):
            raise JobError('protected baseline binding differs from the recorded cfacb3fe recovery image')
    except (KeyError, TypeError, AttributeError) as exc:
        raise JobError(f'incomplete release manifest: {exc}') from exc
    return Release(image, manifest)


def atomic_json(path, value):
    """File fsync + atomic replace + directory fsync; failures are propagated."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def device_key(device):
    return hashlib.sha256(validate_device_path(device).encode()).hexdigest()


class DeviceLock:
    """A shared persistent inode, independent of the CDC node and reenumeration.

    The lock file is never unlinked. Windows support is solely for offline tests.
    The production directory must be provisioned once by the installer.
    """
    def __init__(self, directory, device):
        self.path = Path(directory) / (device_key(device) + '.lock')
        self.fd = None
        self.legacy_fd = None

    def __enter__(self):
        if not self.path.parent.is_dir():
            raise JobError('shared lock directory must be provisioned by the installer: ' + str(self.path.parent))
        # The legacy ROM/maintenance tools use this per-user inode. Acquire
        # both locks, always legacy first, so native OTA cannot overlap bootstrap.
        # A custom offline lock root keeps tests out of the real user's cache.
        if os.name == 'posix':
            import fcntl
            legacy = (Path.home() / '.cache/mixos/flash.lock' if self.path.parent == DEFAULT_LOCKS
                      else self.path.parent / 'legacy-flash.lock')
            legacy.parent.mkdir(parents=True, exist_ok=True)
            self.legacy_fd = os.open(legacy, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                fcntl.flock(self.legacy_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(self.legacy_fd)
                self.legacy_fd = None
                raise JobError('another maintenance task owns the legacy flash lock')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        try:
            self.fd = os.open(self.path, flags, 0o660)
            if os.name == 'nt':
                import msvcrt
                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b'\0')
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            try:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
            finally:
                if self.legacy_fd is not None:
                    os.close(self.legacy_fd)
                    self.legacy_fd = None
            if isinstance(exc, OSError):
                raise JobError('another update owns the persistent device lock') from exc
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(self.fd)
                    self.fd = None
        finally:
            if self.legacy_fd is not None:
                import fcntl
                try:
                    fcntl.flock(self.legacy_fd, fcntl.LOCK_UN)
                finally:
                    os.close(self.legacy_fd)
                    self.legacy_fd = None


class SystemdService:
    """Only the fixed application service can be managed. All exits checked."""
    def __init__(self, run=subprocess.run):
        self.run = run

    def command(self, action):
        command = ['/usr/bin/systemctl', action, SERVICE]
        if action in ('stop', 'start'):
            command = ['/usr/bin/sudo', '-n', *command]
        else:
            command += ['--property=LoadState,ActiveState,SubState,MainPID,ControlPID']
        result = self.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise JobError(f'service {action} failed ({result.returncode}): {result.stderr.strip()}')
        return result.stdout

    def state(self):
        values = dict(line.split('=', 1) for line in self.command('show').splitlines() if '=' in line)
        if values.get('LoadState') != 'loaded':
            raise JobError('mixosd.service is not loaded')
        active, sub, pid = values.get('ActiveState'), values.get('SubState'), values.get('MainPID')
        if values.get('ControlPID') != '0':
            raise JobError('service has an unknown or active control process')
        if active == 'inactive' and sub == 'dead' and pid == '0':
            return 'inactive'
        if active == 'active' and sub == 'running' and pid is not None and pid.isdecimal() and int(pid) > 0:
            return 'active'
        raise JobError('service state is transitional/failed/unknown: ' + repr(values))

    def stop(self):
        self.command('stop')
        if self.state() != 'inactive':
            raise JobError('service stop was not verified; serial ownership is forbidden')

    def restore(self, original):
        if original not in ('active', 'inactive'):
            raise JobError('original service state is unknown')
        current = self.state()
        if current != original:
            self.command('start' if original == 'active' else 'stop')
        if self.state() != original:
            raise JobError('original service state was not restored')
        return dict(original=original, state='restored', observed=original, error=None)


def result_exit_code(result):
    """One authoritative success predicate shared with the SSH wrapper."""
    if result.get('state') in ('queued', 'running'):
        return 2
    if (result.get('state') == 'complete' and result.get('durable') is True and
            result.get('firmware', {}).get('state') == 'confirmed' and
            result.get('service', {}).get('state') == 'restored' and result.get('error') is None):
        return 0
    return 1


def fresh_result(job, device):
    return dict(schema='mixos-esp-job/v2', job=job, device=device, state='queued', durable=False,
                created_at=time.time(),
                firmware={'state': 'not-started'}, service={'state': 'untouched', 'original': None, 'error': None},
                error=None, events=[])


def make_connect(device, timeout):
    def connect():
        # Reopen immediately if a node already exists. A disappearance need not
        # be observed: fast USB reenumeration can occur between two polls.
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            transport = None
            try:
                transport = SerialTransport(device)
                link = ota_esp.Link(transport, random_sessions=True)
                link.handshake(min(3.0, max(0.01, deadline - time.monotonic())))
                return link
            except (OSError, ota_esp.Timeout) as exc:
                last = exc
                if transport:
                    transport.close()
                time.sleep(0.1)
            except BaseException:
                if transport:
                    transport.close()
                raise
        raise ota_esp.Timeout(f'application CDC reopen/HELLO failed: {last}')
    return connect


@contextmanager
def termination_handler():
    """systemd SIGTERM gets the same restoration/finalization as an exception."""
    previous = {}
    def stop(signum, frame):
        raise InterruptedError('worker received termination signal ' + str(signum))
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def cleanup_signals():
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def validate_plan(job_dir, plan):
    job_dir = Path(job_dir).resolve()
    if (not isinstance(plan, dict) or not JOB_ID.fullmatch(job_dir.name)
            or plan.get('job') != job_dir.name
            or job_dir != (Path(plan.get('state_root', '')).resolve() / 'jobs' / job_dir.name)):
        raise JobError('job directory/plan identity mismatch')
    validate_device_path(plan.get('device', ''))
    for key, low, high in (('timeout', 0.01, 120), ('health_timeout', 10, 300)):
        value = plan.get(key)
        if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
            raise JobError('job timeout is not finite/bounded: ' + key)
    if not Path(plan.get('lock_root', '')).is_absolute():
        raise JobError('absolute shared lock directory required')


def validate_result_identity(plan, result):
    if (not isinstance(result, dict) or result.get('job') != plan['job']
            or result.get('device') != plan['device'] or result.get('schema') != 'mixos-esp-job/v2'):
        raise JobError('job result identity/device mismatch')


def validate_job_snapshot(job_dir, plan, result):
    validate_plan(job_dir, plan)
    validate_result_identity(plan, result)
    expected = plan.get('artifact_sha256', {})
    required = {'package/app.bin', 'package/manifest.json'} | {'runtime/' + name for name in RUNNER_FILES}
    if set(expected) != required:
        raise JobError('job snapshot artifact inventory missing or changed')
    for name, digest in expected.items():
        path = Path(job_dir) / name
        if (not full_hash(digest) or path.is_symlink() or path.resolve() != path.absolute()
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise JobError('immutable job artifact digest mismatch: ' + name)


def run_worker(job_dir, service=None, connect=None, lock_factory=DeviceLock,
               save=atomic_json, updater_factory=v2.Updater, install_signals=True):
    job_dir = Path(job_dir).resolve()
    plan = strict_json(job_dir / 'plan.json')
    validate_plan(job_dir, plan)
    # Before ownership is acquired even result.json is only observation. A
    # duplicate worker returns a command receipt, never mutates the active job.
    lock = lock_factory(plan['lock_root'], plan['device'])
    try:
        lock.__enter__()
    except (Exception, KeyboardInterrupt) as exc:
        receipt = fresh_result(plan['job'], plan['device'])
        receipt.update(state='unknown', exit_code=1,
                       error={'code': 'lock-not-acquired', 'message': str(exc)},
                       command_receipt=True)
        return receipt
    try:
        locked_plan = strict_json(job_dir / 'plan.json')
        if locked_plan != plan:
            raise JobError('job plan changed while acquiring ownership')
        result = strict_json(job_dir / 'result.json')
        validate_job_snapshot(job_dir, plan, result)
        # The complete guard is outside cleanup: a repeat invocation must not
        # stop, start, restore or rewrite an already successful job.
        if result_exit_code(result) == 0:
            return read_status(job_dir.parent.parent, job_dir.name)
        with termination_handler() if install_signals else nullcontext():
            return _run_owned_worker(job_dir, plan, result, service or SystemdService(),
                                     connect, save, updater_factory)
    finally:
        lock.__exit__(None, None, None)


def _run_owned_worker(job_dir, plan, result, service, connect, save, updater_factory):
    original = None
    updater = None
    pointer = Path(plan['state_root']) / 'devices' / (device_key(plan['device']) + '.json')

    def persist(event=None):
        if event:
            result['events'].append(dict(at=time.time(), **event))
            if 'binding' in event:
                result['transaction'] = dict(binding=event['binding'], source_boot_id=event['source_boot_id'])
        save(job_dir / 'result.json', result)

    try:
        release = load_release(job_dir / 'package')
        if pointer.exists():
            prior = strict_json(pointer)
            prior_path = job_path(plan['state_root'], prior['job'])
            if prior['job'] != result['job']:
                prior_result = strict_json(prior_path / 'result.json')
                if (result_exit_code(prior_result) != 0 and
                        (prior_result.get('transaction') or prior_result['firmware']['state'] not in ('not-started', 'refused'))):
                    raise JobError('previous device outcome is unresolved; apply --resume ' + prior['job'])
        save(pointer, {'job': result['job']})
        original = result['service'].get('original')
        if original is None:
            original = service.state()
        result['service'] = dict(original=original, state='stopping', error=None)
        result.update(state='running', durable=False)
        persist({'event': 'service-original-recorded'})
        service.stop()
        result['service']['state'] = 'stopped'
        persist({'event': 'service-stop-verified'})
        result['firmware'] = {'state': 'unknown'}
        persist({'event': 'opening-application-cdc'})
        updater = updater_factory(connect or make_connect(plan['device'], plan['timeout']),
                                  timeout=plan['timeout'], health_timeout=plan['health_timeout'])
        result['firmware'] = updater.run(release.image, release.manifest, persist,
                                        saved=result.get('transaction'),
                                        allow_replace_baseline=plan.get('allow_replace_baseline', False))
        result['error'] = None
    except (Exception, KeyboardInterrupt) as exc:
        state = getattr(exc, 'state', 'unknown' if result['firmware']['state'] != 'not-started' else 'not-started')
        result['firmware'] = dict(state=state)
        if getattr(exc, 'response', None):
            result['firmware']['evidence'] = exc.response.record()
        result['error'] = dict(code=type(exc).__name__, message=str(exc))
        diagnostics = getattr(exc, 'diagnostics', None)
        if diagnostics is not None:
            result['firmware']['diagnostics'] = diagnostics
            result['error']['diagnostics'] = diagnostics
    finally:
        # A close failure or a second SIGTERM must not skip restoration.
        with cleanup_signals():
            if updater:
                try:
                    updater.close()
                except (Exception, KeyboardInterrupt) as exc:
                    result['error'] = dict(code='transport-close', message=str(exc))
            if original is not None:
                try:
                    result['service'] = service.restore(original)
                except (Exception, KeyboardInterrupt) as exc:
                    result['service'] = dict(original=original, state='restore-failed', error=str(exc))
                    if result['error'] is None:
                        result['error'] = dict(code='service-restore', message=str(exc))
            result.update(state='complete', durable=True)
            result['exit_code'] = result_exit_code(result)
            try:
                persist({'event': 'finished'})
            except Exception as exc:
                result.update(durable=False, exit_code=1,
                              error=dict(code='journal-write', message=str(exc)))
                try:
                    save(job_dir / 'result.json', result)
                except Exception:
                    pass
    return result


def recover_worker(job_dir, service=None, lock_factory=DeviceLock, save=atomic_json):
    """ExecStopPost: recover original service state after SIGKILL/crash as well."""
    job_dir = Path(job_dir).resolve()
    plan = strict_json(job_dir / 'plan.json')
    validate_plan(job_dir, plan)
    with lock_factory(plan['lock_root'], plan['device']), cleanup_signals():
        if strict_json(job_dir / 'plan.json') != plan:
            raise JobError('job plan changed before recovery acquired ownership')
        result = strict_json(job_dir / 'result.json')
        # Recovery never executes the package: a damaged artifact must not
        # prevent restoration of the separately recorded service state.
        validate_result_identity(plan, result)
        complete = result.get('state') == 'complete'
        if complete and result['service'].get('state') != 'restore-failed':
            return read_status(job_dir.parent.parent, job_dir.name)
        original = result['service'].get('original')
        if original is not None:
            try:
                result['service'] = (service or SystemdService()).restore(original)
                if complete and (result.get('error') or {}).get('code') == 'service-restore':
                    result['error'] = None
            except (Exception, KeyboardInterrupt) as exc:
                result['service'] = dict(original=original, state='restore-failed', error=str(exc))
        if not complete:
            result['error'] = dict(code='worker-terminated', message='worker exited without a durable final result; inspect/resume, never reflash blindly')
            if result['firmware']['state'] != 'not-started':
                result['firmware'] = {'state': 'unknown'}
        result.update(state='complete', durable=True)
        result['exit_code'] = result_exit_code(result)
        try:
            save(job_dir / 'result.json', result)
        except Exception as exc:
            result.update(durable=False, exit_code=1,
                          error=dict(code='journal-write', message=str(exc)))
        return result


def bundle_release(image, manifest, destination, runner_root=ROOT):
    """Create a portable directory; validate before committing the directory."""
    destination = Path(destination)
    if destination.exists():
        raise JobError('bundle destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.mixos-bundle-', dir=destination.parent))
    try:
        shutil.copyfile(image, temporary / 'app.bin')
        shutil.copyfile(manifest, temporary / 'manifest.json')
        load_release(temporary)
        for name in RUNNER_FILES:
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(runner_root) / name, target)
        (temporary / 'linux/mixos-esp-update').chmod(0o755)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def create_job(package, device, state_root=DEFAULT_STATE, lock_root=DEFAULT_LOCKS,
               timeout=30.0, health_timeout=90.0, allow_replace_baseline=False):
    finite_seconds(timeout, 'timeout', 0.01, 120)
    finite_seconds(health_timeout, 'health-timeout', 10, 300)
    release = load_release(package)
    validate_device_path(device)
    state_root = Path(state_root).resolve()
    job = uuid.uuid4().hex
    job_dir = state_root / 'jobs' / job
    job_dir.mkdir(parents=True, mode=0o700)
    package_dir = job_dir / 'package'
    package_dir.mkdir(mode=0o700)
    # Immutable snapshot: the submitted worker never rereads an operator's
    # mutable source package. Hashes are checked again inside the device lock.
    (package_dir / 'app.bin').write_bytes(release.image)
    atomic_json(package_dir / 'manifest.json', release.manifest)
    runtime = job_dir / 'runtime'
    for name in RUNNER_FILES:
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    artifacts = {'package/app.bin', 'package/manifest.json'} | {'runtime/' + name for name in RUNNER_FILES}
    hashes = {name: hashlib.sha256((job_dir / name).read_bytes()).hexdigest() for name in artifacts}
    # Publish the plan only after every snapshotted byte and directory entry
    # has been synchronized. Digest checks detect subsequent mutation; these
    # are same-user consistency guarantees, not a signature/trust boundary.
    for name in artifacts:
        with (job_dir / name).open('rb+') as artifact:
            os.fsync(artifact.fileno())
    if os.name == 'posix':
        directories = {job_dir, job_dir.parent, state_root}
        for name in artifacts:
            directories.update(p for p in (job_dir / name).parents if p.is_relative_to(job_dir))
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    atomic_json(job_dir / 'plan.json', dict(job=job, device=device, state_root=str(state_root),
                lock_root=str(Path(lock_root).resolve()), timeout=timeout, health_timeout=health_timeout,
                artifact_sha256=hashes, allow_replace_baseline=allow_replace_baseline))
    result = fresh_result(job, device)
    atomic_json(job_dir / 'result.json', result)
    return job_dir, result


def systemd_quote(word):
    return '"' + str(word).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def launch_job(job_dir, run=subprocess.run, uid=None, gid=None):
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    if uid == 0:
        raise JobError('run apply as the ordinary serial-access user, not root')
    job_dir = Path(job_dir).resolve()
    if not JOB_ID.fullmatch(job_dir.name):
        raise JobError('invalid job ID')
    plan = strict_json(job_dir / 'plan.json')
    validate_job_snapshot(job_dir, plan, strict_json(job_dir / 'result.json'))
    program = str(job_dir / 'runtime/tools/mixos_esp_update.py')
    common = ['/usr/bin/python3', '-I', '-u', program]
    recovery = ' '.join(map(systemd_quote, [*common, '_recover', '--job-dir', str(job_dir)]))
    lingering = run(['/usr/bin/loginctl', 'show-user', str(uid), '--property=Linger', '--value'],
                    capture_output=True, text=True, timeout=15)
    if lingering.returncode or lingering.stdout.strip() != 'yes':
        raise JobError('user-manager lingering is not verified; installer must enable it before background apply')
    command = ['/usr/bin/systemd-run', '--user',
               '--unit=mixos-esp-update-' + job_dir.name, '--collect', '--service-type=exec',
               '--property=UMask=0077', '--property=RuntimeMaxSec=900',
               '--property=TimeoutStopSec=60', '--property=KillSignal=SIGTERM',
               '--property=ExecStopPost=' + recovery,
               *common, '_worker', '--job-dir', str(job_dir)]
    answer = run(command, capture_output=True, text=True, timeout=30)
    if answer.returncode:
        raise JobError(f'systemd submission failed ({answer.returncode}): {answer.stderr.strip()}')


def prepare_resume(job_dir, run=subprocess.run, recover=recover_worker):
    """Confirm the old user unit is gone/inactive before changing its journal.

    After host power loss there was no ExecStopPost. Reconcile its saved service
    state under the same lock before allowing a new observer for that binding.
    """
    job_dir = Path(job_dir)
    # Success is observational/idempotent, even when the old user unit remains.
    result = read_status(job_dir.parent.parent, job_dir.name)
    if result_exit_code(result) == 0:
        return result
    unit = 'mixos-esp-update-' + job_dir.name + '.service'
    answer = run(['/usr/bin/systemctl', '--user', 'show', unit,
                  '--property=LoadState,ActiveState'], capture_output=True, text=True, timeout=15)
    values = dict(line.split('=', 1) for line in answer.stdout.splitlines() if '=' in line)
    stopped = (answer.returncode == 0 and values.get('LoadState') == 'loaded' and
               values.get('ActiveState') in ('inactive', 'failed'))
    absent = (answer.returncode in (0, 1) and values.get('LoadState') == 'not-found' and
              values.get('ActiveState') == 'inactive')
    if not (stopped or absent):
        raise JobError('previous worker is active or its systemd state is unknown; use status before resuming')
    result = strict_json(job_dir / 'result.json')
    if result.get('state') != 'complete' or result['service'].get('state') == 'restore-failed':
        result = recover(job_dir)
        if result.get('durable') is not True:
            return result
    return read_status(job_dir.parent.parent, job_dir.name)


def job_path(state_root, job):
    if not JOB_ID.fullmatch(job):
        raise JobError('job ID must be exactly 32 lowercase hexadecimal characters')
    return Path(state_root) / 'jobs' / job


def read_status(state_root, job):
    directory = job_path(state_root, job).resolve()
    path = directory / 'result.json'
    result = strict_json(path)
    plan = strict_json(directory / 'plan.json')
    validate_plan(directory, plan)
    validate_result_identity(plan, result)
    if result.get('state') in ('queued', 'running'):
        # No effects: report an overdue journal as an unknown observation,
        # never replace the worker's durable record or infer it never started.
        stamp = result.get('created_at')
        if result.get('events'):
            stamp = result['events'][-1].get('at', stamp)
        if type(stamp) not in (float, int) or not math.isfinite(stamp) or time.time() > stamp + 1020:
            return command_receipt(result, 'job-overdue',
                                   'job exceeded the 900s worker plus cleanup/submission budget; inspect/resume explicitly')
    if result_exit_code(result) == 0:
        # A crash/fsync failure can occur after rename but before the writer
        # returns. Independently establish durability before status reports 0.
        try:
            fd = os.open(path, os.O_RDWR if os.name == 'nt' else os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if os.name != 'nt':
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            result['durable'] = False
            result['error'] = {'code': 'journal-durability', 'message': str(exc)}
    result['exit_code'] = result_exit_code(result)
    return result


def inspect_device(device, timeout=15.0, legacy=False, service=None, connect=None,
                   lock_root=DEFAULT_LOCKS, lock_factory=DeviceLock):
    """Explicit live inspection coordinates ownership, but never writes flash."""
    finite_seconds(timeout, 'timeout', 0.01, 120)
    result = fresh_result('inspect', device)
    link = None
    service = service or SystemdService()
    original = None
    try:
        with termination_handler(), lock_factory(lock_root, device):
            try:
                original = service.state()
                service.stop()
                link = (connect or make_connect(device, timeout))()
                identity = ota_esp.identify(link, timeout)
                if not identity:
                    raise JobError('unreadable running identity')
                identity['elf_sha256'] = identity['elf_sha256'].hex()
                result['firmware'] = dict(state='observed', identity=identity, legacy_only=legacy)
                if not legacy:
                    client = v2.Client(link, timeout)
                    result['firmware']['capabilities'] = client.capabilities().record()
                    result['firmware']['journal'] = client.query().record()
            finally:
                with cleanup_signals():
                    if link:
                        try:
                            link.transport.close()
                        except (Exception, KeyboardInterrupt) as exc:
                            result['error'] = dict(code='transport-close', message=str(exc))
                    if original is not None:
                        try:
                            result['service'] = service.restore(original)
                        except (Exception, KeyboardInterrupt) as exc:
                            result['service'] = dict(state='restore-failed', original=original, error=str(exc))
                            result['error'] = dict(code='service-restore', message=str(exc))
    except (Exception, KeyboardInterrupt) as exc:
        result['error'] = dict(code=type(exc).__name__, message=str(exc))
    result['state'] = 'complete'
    # Observations do not meet installation's success predicate.
    result['exit_code'] = 1 if result['error'] else 0
    return result


def finite_seconds(value, name, low=0.01, high=1200):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        raise JobError(f'{name} must be finite and within {low}..{high} seconds')
    return value


def command_receipt(result, code, message):
    """Non-authoritative command outcome; never write this over result.json."""
    return dict(result, state='unknown', observed_state=result.get('state'),
                durable=False, command_receipt=True, exit_code=1,
                error={'code': code, 'message': message})


def wait_status(state_root, job, timeout=960, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + finite_seconds(timeout, 'wait-timeout')
    while True:
        result = read_status(state_root, job)
        if result.get('state') not in ('queued', 'running'):
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            return command_receipt(result, 'wait-timeout',
                                   'observation deadline expired; worker may still run; use status, never reflash blindly')
        sleep(min(0.25, remaining))


def submit_job(job_dir, result, launch=launch_job):
    if result_exit_code(result) == 0:
        return result
    try:
        launch(job_dir)
    except Exception as exc:
        return command_receipt(result, 'submission-unknown', str(exc) + '; worker may have been accepted; use status')
    # Launch returning is not evidence of firmware success. Read the current
    # result rather than overwriting a fast worker with the queued receipt.
    observed = read_status(Path(job_dir).parent.parent, Path(job_dir).name)
    if (observed.get('state') == 'complete' and result_exit_code(observed) != 0
            and observed == result):
        return command_receipt(observed, 'resume-submitted',
                               'worker accepted but has not published new progress; previous result retained; use status')
    return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect', help='validate a local package; --live explicitly opens the device')
    inspect.add_argument('--package', type=Path)
    inspect.add_argument('--live', action='store_true')
    inspect.add_argument('--legacy-identify', action='store_true', help='explicit read-only v1 identity compatibility')
    inspect.add_argument('--device', default=DEVICE)
    inspect.add_argument('--timeout', type=float, default=15.0)
    inspect.add_argument('--lock-root', type=Path, default=DEFAULT_LOCKS)
    inspect.add_argument('--dry-run', action='store_true')
    package = sub.add_parser('package', help='create app.bin/manifest.json plus a self-contained runner')
    package.add_argument('--image', required=True, type=Path)
    package.add_argument('--manifest', required=True, type=Path)
    package.add_argument('--output', required=True, type=Path)
    apply = sub.add_parser('apply', help='submit a durable systemd job; this is write authorization')
    apply.add_argument('--package', type=Path)
    apply.add_argument('--resume', help='observe/complete the existing binding; never resume a receive')
    apply.add_argument('--device', default=DEVICE)
    apply.add_argument('--state-root', type=Path, default=DEFAULT_STATE)
    apply.add_argument('--lock-root', type=Path, default=DEFAULT_LOCKS)
    apply.add_argument('--timeout', type=float, default=30.0)
    apply.add_argument('--health-timeout', type=float, default=90.0)
    apply.add_argument('--allow-replace-baseline', action='store_true')
    apply.add_argument('--dry-run', action='store_true')
    apply.add_argument('--wait', action='store_true')
    status = sub.add_parser('status', help='read the durable job result without opening hardware')
    status.add_argument('--job', required=True)
    status.add_argument('--state-root', type=Path, default=DEFAULT_STATE)
    status.add_argument('--wait', action='store_true')
    for waiting in (apply, status):
        waiting.add_argument('--wait-timeout', type=float, default=960,
                             help='finite observation budget; expiration never stops or rewrites a job')
    for name in ('_worker', '_recover'):
        worker = sub.add_parser(name, help=argparse.SUPPRESS)
        worker.add_argument('--job-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'package':
            path = bundle_release(args.image, args.manifest, args.output)
            result = dict(package=str(path), firmware={'state': 'not-started'},
                          service={'state': 'untouched'}, error=None)
            code = 0
        elif args.command == 'inspect':
            if args.package:
                release = load_release(args.package)
                result = dict(manifest=release.manifest, firmware={'state': 'not-started'},
                              service={'state': 'untouched'}, error=None)
            elif not args.live or args.dry_run:
                raise JobError('local inspect/dry-run requires --package')
            if args.live and not args.dry_run:
                validate_device_path(args.device)
                result = inspect_device(args.device, args.timeout, args.legacy_identify, lock_root=args.lock_root)
                code = result['exit_code']
            else:
                code = 0
        elif args.command in ('_worker', '_recover'):
            if os.name != 'posix' or os.geteuid() == 0:
                raise JobError('worker must run on Linux as the ordinary serial user')
            result = (run_worker if args.command == '_worker' else recover_worker)(args.job_dir)
            code = result_exit_code(result)
        elif args.command == 'apply':
            validate_device_path(args.device)
            finite_seconds(args.timeout, 'timeout', 0.01, 120)
            finite_seconds(args.health_timeout, 'health-timeout', 10, 300)
            finite_seconds(args.wait_timeout, 'wait-timeout')
            if bool(args.package) == bool(args.resume):
                raise JobError('choose exactly one of --package or --resume')
            if args.resume:
                directory = job_path(args.state_root, args.resume)
                release = load_release(directory / 'package')
                result = read_status(args.state_root, args.resume)
            else:
                release = load_release(args.package)
            if args.dry_run:
                result = dict(dry_run=True, manifest=release.manifest,
                              firmware={'state': 'not-started'}, service={'state': 'untouched'}, error=None)
                code = 0
            else:
                if os.name != 'posix' or os.geteuid() == 0:
                    raise JobError('apply must run on Linux as the ordinary serial-access user')
                if not args.resume:
                    directory, result = create_job(args.package, args.device, args.state_root, args.lock_root,
                                                   args.timeout, args.health_timeout, args.allow_replace_baseline)
                else:
                    # No journal mutation outside the device lock. The worker
                    # rereads the completed/unknown result after ownership.
                    result = prepare_resume(directory)
                result = submit_job(directory, result, launch=launch_job)
                if args.wait and not result.get('command_receipt'):
                    print(json.dumps(result, sort_keys=True), flush=True)
                    result = wait_status(args.state_root, result['job'], args.wait_timeout)
                code = result_exit_code(result)
        else:
            finite_seconds(args.wait_timeout, 'wait-timeout')
            result = (wait_status(args.state_root, args.job, args.wait_timeout) if args.wait
                      else read_status(args.state_root, args.job))
            code = result_exit_code(result)
    except (Exception, KeyboardInterrupt) as exc:
        result = dict(firmware={'state': 'unknown'}, service={'state': 'unknown'},
                      error={'code': type(exc).__name__, 'message': str(exc)}, durable=False)
        code = 1
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
