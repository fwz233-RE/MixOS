#!/usr/bin/env python3
"""First safe OTA receiver installation; offline/dry-run unless --execute.

The supervisor installs an immutable root-owned approval/code package and
provides /var/lib/mixos/bootstrap-ota (private to the ordinary worker). Approved
managed_service=true tasks own mixosd only after taking both maintenance locks;
ExecStopPost uses --recover-service without device access. Unmanaged callers
must stop mixosd themselves. Failed ROM tasks keep it stopped until a separately
approved boot-only task proves application health. Tasks are never restarted.

Invocation (OFFLINE): --approval FILE --approval-sha256 HASH [--inspect BACKUP]
Execution additionally requires --execute on Linux as an ordinary dialout user.
There is deliberately no remote launcher, retry, force, chip erase, old OTA_END,
bootloader/table/font rewrite or hard_reset option.

Approval JSON schema 1 (all paths absolute, hashes lowercase SHA256):
  schema, approved=true, task_id, kind=qualify|install|boot-only,
  reset_method="official-esptool-watchdog-reset", allow_clear_force_download=true,
  code_sha256={relative package .py path: digest}, runtime_package=absolute path,
  artifacts={name: {path: absolute path, sha256: digest}}.
Qualification/install artifacts: recovery_flash, recovery_verification,
recovery_boot, migration_receipt, bootloader, boot_config,
bootloader_build_receipt, bootloader_source_snapshot, bootloader_build_log.
The original migration receipt must already pin the build receipt SHA256 in
artifact_provenance.bootloader_build_receipt_sha256. Its recorded-build schema
is mixos-bootloader-build/v1: target="esp32s3", inputs={source_snapshot: record},
artifacts={bootloader: record, boot_config: record}, and
build={builder: string, command: string, toolchain: string, log: record}.
Each record is {path: historical label, bytes: positive integer, sha256: digest}.
The source snapshot must archive all actual project/IDF sources (including
local changes), configuration and build scripts; the log must be from that
same completed build. These bytes must be separately approved artifacts.

Approval authenticates externally reviewed archived evidence, not the truth of
self-written JSON. This tool never issues receipts from current file paths.
The pinned 2026-09-13 migration has NO historical config/build binding, so
legacy approvals are deliberately refused before service/device effects.
Retrofitting that receipt or silently substituting current configuration is
not allowed. An explicit bootloader_evidence_mode=
"mixos-reviewed-bootloader-binary-evidence/v1" is supported ONLY for kind=qualify
(current A, no Flash writes). It keeps the four recovery/migration artifacts
and bootloader, and replaces historical-build artifacts with reference_bootloader,
reference_elf, reference_config, bounded-recheck.json, audited-behavior.json,
byte-comparison.json, semantic-verification.json, rollback-paths.json and
startup-takeover.json. Every exact artifact and validator module must be bound
to the immutable approval. This route proves neither historical configuration
nor whole-behavior equivalence nor candidate startup compatibility; it cannot
authorize install or boot-only and is never an automatic fallback.
A separate "mixos-reviewed-candidate-bootstrap/v1" install/boot-only route keeps
those old binary facts bounded and additionally requires exact candidate
BIN/ELF/build/input/link evidence, a candidate-bound maintenance confirmation
contract review, successful original C/wire/host test evidence, and explicit
startup/manual-recovery risk acceptance. It preserves A, prohibits fault
injection, and does not claim physical startup timing. Its evidence binding is
persisted in verified.json and must be inherited by the independent boot task.
New-route approvals include candidate_evidence_sha256, runtime_risk_acceptance,
candidate_evidence/*, candidate_review, confirmation_test_report/log, plus the
original qualification artifact (install) or source_verified artifact (boot).
The original qualification-only package is never replayed or relabelled.
Historical-build install also needs candidate, candidate_review (schema=1, sha256, elf_sha256,
safe_receiver_reviewed=true, rollback_health_confirmation_reviewed=true),
qualification_task, qualification_sha256 (prior qualification.json hash).
Boot-only needs source_task, source_verified_sha256, expected_sha256. The
source systemd unit must be mixos-bootstrap-<source_task>.service and stopped.
Task IDs and the whole approval must be new explicit operator permissions,
not timestamps generated on a retry. Approval issuance/staging is external.

Only volatile FORCE_DOWNLOAD_BOOT can be cleared, after a durable reset claim;
no eFuse write/unprotect API exists here. The official esptool 5.4.0 watchdog
reset then runs once. Install itself never resets: its separately approved
boot-only task first proves the writer stopped and the exact 8 MiB readback.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import time
import traceback

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from _mixlib import ota_bootstrap as policy
from _mixlib import bootloader_evidence
from _mixlib import bootstrap_candidate_route as candidate_route
from _mixlib.bootstrap_service import ServiceOwner, mark_rom_entry, verify_unit
from _mixlib.durable import durable_new
from _mixlib.guards import operation_timeout, timeouts_enforced
import display_transport
import flash_font_on_pi as font
import flash_esp_on_pi as esp
from update_esp import physical_identity, validate_image, LAYOUTS

import ota_esp
import ota_v2
import mixos_esp_update as native
from _mixlib.application_cdc import ApplicationCdc

STATE = Path('/var/lib/mixos/bootstrap-ota')
READ_CHUNK = 0x40000


def identify_once(link, timeout):
    """One physical identity request; a missing reply is unknown, never replayed."""
    epoch, session = link.epoch, link.session
    link.send(ota_esp.C.MAINTENANCE, ota_esp.T.OTA_IDENTIFY, session, b'')
    def identity(frame):
        if (frame.channel == ota_esp.C.MAINTENANCE and frame.type == ota_esp.T.OTA_IDENTITY
                and frame.epoch == epoch and frame.session == session):
            return ota_esp.decode_identity(frame.payload) or False
        return None
    result = link.wait(identity, timeout, 'single boot OTA_IDENTITY')
    policy.require((link.epoch, link.session) == (epoch, session), 'Boot epoch/session changed during identity query')
    return result


def observe_baseline_health(link, expected, timeout):
    """Observe the legacy A without repeatedly mapping otadata in its UI task.

    Its identity query still has the old cache-off risk. Observe 20 seconds of
    live protocol progress first, send exactly one identity query, then prove
    another 20 seconds on the same epoch/session. This reduces query exposure;
    it does not assert that startup timing alone fixes the old firmware fault.
    """
    deadline = link.clock() + timeout
    link.handshake(min(10.0, timeout))
    epoch, session = link.epoch, link.session

    def heartbeat_phase():
        begin = link.clock()
        until = begin + 20.0
        policy.require(until <= deadline, 'Insufficient legacy A health observation budget')
        while link.clock() < until:
            link.poll()
            policy.require((link.epoch, link.session) == (epoch, session),
                           'Legacy A epoch/session changed during health observation')
            link.sleep(min(0.01, max(0.0, until - link.clock())))
        beats = [t for t in link.heartbeat_times if t >= begin]
        policy.require(len(beats) >= 3 and beats[-1] - beats[0] >= 15
                       and link.clock() - beats[-1] <= 3.5
                       and all(b - a <= 3.5 for a, b in zip(beats, beats[1:])),
                       'Legacy A requires continuous fresh protocol heartbeats')
        return dict(pings=len(beats), observed_seconds=beats[-1] - beats[0])

    before = heartbeat_phase()
    policy.require(deadline - link.clock() >= 20.01, 'Legacy A identity budget exhausted')
    link.send(ota_esp.C.MAINTENANCE, ota_esp.T.OTA_IDENTIFY, session, b'')

    def identity(frame):
        if (frame.channel == ota_esp.C.MAINTENANCE and frame.type == ota_esp.T.OTA_IDENTITY
                and frame.epoch == epoch and frame.session == session):
            return ota_esp.decode_identity(frame.payload) or False
        return None

    running = link.wait(identity, min(8.0, deadline - link.clock() - 20.0), 'single baseline OTA_IDENTITY')
    policy.require((link.epoch, link.session) == (epoch, session), 'Legacy A epoch/session changed during identity query')
    policy.require(running and running['state'] == 'valid' and running['slot'] == 'ota_0'
                   and running['address'] == policy.APP0 and running['elf_sha256'].hex() == expected['elf_sha256'],
                   'Exact legacy A identity and VALID required')
    after = heartbeat_phase()
    result = dict(running, elf_sha256=running['elf_sha256'].hex(), **after,
                  pre_identity_heartbeat=before, identity_queries=1)
    policy.validate_running(result, expected, 0)
    return result


def observe_app_health(link, expected, slot, timeout=120.0):
    """Fresh identity samples allow NEW/PENDING -> VALID, never a new ELF/boot.

    Legacy ota_0 is only a pre-bootstrap observation; its exact bytes are
    checked in the separately saved full-flash readback. ota_1 additionally
    needs opcode-6 exact-file measurement on this boot, without fake history.
    """
    if slot == 0:
        return observe_baseline_health(link, expected, timeout)
    deadline = link.clock() + timeout
    link.handshake(min(10.0, timeout))
    epoch, session = link.epoch, link.session
    running = ota_esp.identify(link, min(2.0, max(0.01, deadline - link.clock())))
    policy.require((link.epoch, link.session) == (epoch, session), 'Application epoch/session changed')
    policy.require(running and running['slot'] == 'ota_' + str(slot)
                   and running['address'] == policy.APP1
                   and running['elf_sha256'].hex() == expected['elf_sha256'],
                   'Application full ELF identity or running slot mismatch')
    policy.require(running['state'] in ('new', 'pending-verify', 'valid'),
                   'Application state is not trial/VALID')
    # Exercise the maintenance worker while PENDING. Waiting for VALID first
    # would deadlock receivers whose confirmation requires this round trip.
    binding = ota_v2.Binding(secrets.token_bytes(16), bytes.fromhex(expected['sha256']),
                             expected['bytes'], slot)
    measurement = ota_v2.verify_actual(
        ota_v2.Client(link, min(5.0, max(0.01, deadline-link.clock())), link.sleep),
        binding, bytes.fromhex(expected['elf_sha256']), deadline,
        heartbeat_seconds=15.0, no_journal=True)
    policy.require((link.epoch, link.session) == (epoch, session), 'Application epoch/session changed')
    remaining = deadline - link.clock()
    policy.require(remaining > 0, 'Final identity observation budget exhausted')
    running = ota_esp.identify(link, min(2.0, remaining))
    policy.require((link.epoch, link.session) == (epoch, session), 'Application epoch/session changed')
    policy.require(running is not None, 'Final application identity is unavailable')
    result = dict(running, elf_sha256=running['elf_sha256'].hex(),
                  pings=measurement['heartbeat_count'], observed_seconds=measurement['heartbeat_seconds'],
                  measurement=measurement, actual_file_verified=True)
    policy.validate_running(result, expected, slot)
    return result


def observe_boot_health(link, expected, baseline, timeout=120.0):
    """Classify one post-reset boot; never reset/reflash or hide an epoch change.

    The first response may come from the preserved legacy A after bootloader
    fallback. Give it the same single-query/two-heartbeat-window treatment as
    qualification. Only the exact B takes the v2 measurement path. Seeing A
    proves baseline restoration, not that B ran or that rollback was exercised.
    """
    deadline = link.clock() + timeout
    link.handshake(min(10.0, timeout))
    epoch, session = link.epoch, link.session

    def heartbeat_phase():
        begin = link.clock()
        until = begin + 20.0
        policy.require(until <= deadline, 'Insufficient boot observation budget')
        while link.clock() < until:
            link.poll()
            policy.require((link.epoch, link.session) == (epoch, session),
                           'Boot epoch/session changed during observation')
            link.sleep(min(0.01, max(0.0, until - link.clock())))
        beats = [t for t in link.heartbeat_times if t >= begin]
        policy.require(len(beats) >= 3 and beats[-1] - beats[0] >= 15
                       and link.clock() - beats[-1] <= 3.5
                       and all(b - a <= 3.5 for a, b in zip(beats, beats[1:])),
                       'Boot observation requires continuous fresh heartbeats')
        return dict(pings=len(beats), observed_seconds=beats[-1] - beats[0])

    before = heartbeat_phase()
    policy.require(deadline - link.clock() >= 20.01, 'Insufficient post-identity boot budget')
    running = identify_once(link, min(8.0, deadline - link.clock() - 20.0))
    policy.require((link.epoch, link.session) == (epoch, session),
                   'Boot epoch/session changed during identity query')
    if running and running.get('slot') == 'ota_0':
        policy.require(baseline is not None and running.get('address') == policy.APP0
                       and running.get('state') == 'valid'
                       and running['elf_sha256'].hex() == baseline['elf_sha256'],
                       'Boot fallback must be the exact protected A in VALID state')
        after = heartbeat_phase()
        result = dict(running, elf_sha256=running['elf_sha256'].hex(), **after,
                      pre_identity_heartbeat=before, identity_queries=1)
        policy.validate_running(result, baseline, 0)
        return dict(outcome='baseline-restored', running=result)
    policy.require(running and running.get('slot') == 'ota_1'
                   and running.get('address') == policy.APP1
                   and running['elf_sha256'].hex() == expected['elf_sha256'],
                   'Boot identity is neither exact candidate B nor protected A')
    result = observe_app_health(link, expected, 1, deadline - link.clock())
    policy.require((link.epoch, link.session) == (epoch, session),
                   'Boot epoch/session changed during candidate verification')
    return dict(outcome='candidate-confirmed', running=result)


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            policy.require(key not in result, 'Duplicate JSON field: ' + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def local_bytes(path, expected, protected=False, limit=16 * 1024 * 1024):
    path = Path(path)
    policy.require(path.is_absolute() and path.resolve(strict=True) == path, 'Canonical absolute artifact path required')
    info = path.lstat()
    policy.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
                   'Unsafe or oversized artifact')
    data = font.trusted_bytes(path) if protected else path.read_bytes()
    policy.require(policy.sha(data) == policy.digest(expected), 'Artifact hash mismatch: ' + str(path))
    return data


# Independent historical anchor, not a digest supplied by the same new JSON
# that claims build provenance. The recorded receipt has no build/config pin:
# production remains blocked. Never rewrite evidence or repin this constant to
# a retroactively manufactured receipt to make qualification pass.
RECOVERED_MIGRATION_SHA256 = 'd694b2e699927e4d20f3c14cff6830a3e3c01b15dbf69843affff7237279f3c3'


def recovery_bytes(files, name):
    data = files.get(name)
    policy.require(isinstance(data, bytes) and data, 'Missing recovery artifact: ' + name)
    return data


def recovery_object(files, name):
    try:
        value = strict_json(recovery_bytes(files, name))
    except (ValueError, UnicodeError) as exc:
        raise policy.Refused('Invalid recovery JSON ' + name + ': ' + str(exc)) from exc
    policy.require(isinstance(value, dict), 'Recovery JSON object required: ' + name)
    return value


def bootloader_provenance(files, migration):
    """Check a recorded build, anchored independently of its claimed outputs.

    Authenticity comes from the reviewed original migration digest plus the
    immutable approval, not schema validity or a caller's provenance boolean.
    The current historical anchor cannot pass: it never recorded this binding.
    Positive tests use synthetic history with an independently pinned receipt;
    those tests do not create or authenticate evidence for the real bootloader.
    No source archive is extracted and no build command/path is executed/read.
    """
    policy.require(policy.sha(recovery_bytes(files, 'migration_receipt')) == RECOVERED_MIGRATION_SHA256,
                   'Original recorded migration receipt required; retrospective edits are not provenance')
    provenance = migration.get('artifact_provenance')
    policy.require(isinstance(provenance, dict)
                   and provenance.get('migrate_to_ab') is True
                   and 'bootloader_build_receipt_sha256' in provenance,
                   'Recorded bootloader build provenance missing in migration receipt; historical config is unproven')
    receipt_bytes = recovery_bytes(files, 'bootloader_build_receipt')
    policy.require(policy.sha(receipt_bytes) == policy.digest(provenance['bootloader_build_receipt_sha256']),
                   'Bootloader build receipt differs from original migration provenance')
    receipt = recovery_object(files, 'bootloader_build_receipt')
    policy.require(set(receipt) == {'schema', 'target', 'inputs', 'artifacts', 'build'}
                   and receipt['schema'] == 'mixos-bootloader-build/v1'
                   and receipt['target'] == 'esp32s3', 'Unsupported bootloader build receipt schema/target')
    inputs, artifacts, build = receipt['inputs'], receipt['artifacts'], receipt['build']
    policy.require(isinstance(inputs, dict) and set(inputs) == {'source_snapshot'}
                   and isinstance(artifacts, dict) and set(artifacts) == {'bootloader', 'boot_config'}
                   and isinstance(build, dict) and set(build) == {'builder', 'command', 'toolchain', 'log'},
                   'Recorded source/build provenance and exact bootloader/config outputs required')
    policy.require(all(isinstance(build[key], str) and build[key].strip()
                       for key in ('builder', 'command', 'toolchain')),
                   'Recorded builder, command and toolchain required')
    for name, record in (('bootloader_source_snapshot', inputs['source_snapshot']),
                         ('bootloader_build_log', build['log']),
                         ('bootloader', artifacts['bootloader']),
                         ('boot_config', artifacts['boot_config'])):
        policy.require(isinstance(record, dict) and set(record) == {'path', 'bytes', 'sha256'}
                       and isinstance(record['path'], str) and record['path'].strip()
                       and type(record['bytes']) is int and record['bytes'] > 0,
                       'Invalid recorded build artifact: ' + name)
        data = recovery_bytes(files, name)
        policy.require(len(data) == record['bytes'] and policy.sha(data) == policy.digest(record['sha256']),
                       'Recorded build artifact mismatch: ' + name)
    policy.require(artifacts['bootloader']['sha256'] == policy.RECOVERED_BOOT_SHA256,
                   'Build provenance is not for the exact recovered bootloader')


def recovery_trust(files, *, evidence_mode='historical-build', task_kind=None):
    """Bind recovery bytes using explicit historical or reviewed-binary evidence.

    Binary review is restricted to no-write current-A qualification. It does
    not claim historical configuration or candidate compatibility, and cannot
    authorize B installation. Other recovery/identity/layout gates stay intact.
    """
    policy.require(isinstance(files, dict), 'Recovery artifact mapping required')
    migration = recovery_object(files, 'migration_receipt')
    binary_review = None
    if evidence_mode == bootloader_evidence.ROUTE:
        policy.require(task_kind == 'qualify',
                       'Reviewed binary evidence only authorizes no-write qualification; candidate compatibility is unproven')
        policy.require(policy.sha(recovery_bytes(files, 'migration_receipt')) == RECOVERED_MIGRATION_SHA256
                       and isinstance(migration.get('artifact_provenance'), dict)
                       and migration['artifact_provenance'].get('migrate_to_ab') is True,
                       'Original reviewed migration identity required')
        artifacts = {name: recovery_bytes(files, 'bootloader' if name == 'old_bootloader' else name)
                     for name in bootloader_evidence.ARTIFACT_SHA256}
        binary_review = bootloader_evidence.validate_reviewed_binary_evidence(
            artifacts, purpose=bootloader_evidence.QUALIFICATION_PURPOSE)
    else:
        policy.require(evidence_mode == 'historical-build', 'Unknown bootloader evidence route')
        bootloader_provenance(files, migration)  # Pure preflight; before service/device effects.
    return _recovery_identity_trust(files, migration, binary_review)


def _recovery_identity_trust(files, migration, binary_review):
    """Check preserved bytes and identities; this helper grants no operation."""
    recovery = recovery_bytes(files, 'recovery_flash')
    verified = recovery_object(files, 'recovery_verification')
    boot = recovery_object(files, 'recovery_boot')
    config = recovery_object(files, 'boot_config') if binary_review is None else None
    raw_boot = recovery_bytes(files, 'bootloader')
    policy.require(len(recovery) == policy.FLASH_SIZE and policy.sha(recovery) == policy.RECOVERED_FULL_SHA256,
                   'Exact reviewed recovery snapshot required')
    policy.require(verified.get('readback_sha256') == policy.sha(recovery)
                   and verified.get('app_sha256') == policy.BASELINE_SHA256
                   and verified.get('app_bytes') == policy.BASELINE_BYTES
                   and verified.get('app_elf_sha256') == policy.BASELINE_ELF
                   and all(verified.get(name) is True for name in (
                       'all_other_flash_unchanged', 'bootloader_unchanged', 'partition_table_unchanged',
                       'nvs_and_phy_unchanged', 'otadata_unchanged', 'ota1_unchanged', 'font_unchanged')),
                   'Recovery comparison evidence mismatch')
    policy.require(boot.get('device') == policy.APP_IDENTITY, 'Recovery application USB identity mismatch')
    policy.require(isinstance(boot.get('running'), dict) and isinstance(boot.get('heartbeat'), dict),
                   'Recovery application health evidence must contain objects')
    running = dict(boot['running'], **boot['heartbeat'])
    policy.validate_running(running, policy.baseline_description(policy.Trust('0' * 64, '0' * 64)), 0)
    policy.require(32 <= len(raw_boot) <= policy.TABLE and raw_boot[0] == 0xE9
                   and policy.sha(raw_boot) == policy.RECOVERED_BOOT_SHA256
                   and isinstance(migration.get('hashes'), dict)
                   and migration['hashes'].get('bootloader.bin') == policy.RECOVERED_BOOT_SHA256
                   and recovery[:policy.TABLE] == raw_boot.ljust(policy.TABLE, b'\xff'),
                   'Recovered rollback bootloader bytes are not the approved migration binary')
    if binary_review is None:
        policy.require(config.get('BOOTLOADER_APP_ROLLBACK_ENABLE') is True
                       and config.get('BOOTLOADER_APP_ANTI_ROLLBACK') is False
                       and config.get('BOOTLOADER_WDT_ENABLE') is True
                       and type(config.get('BOOTLOADER_OFFSET_IN_FLASH')) is int
                       and config['BOOTLOADER_OFFSET_IN_FLASH'] == 0
                       and all(config.get(name) is False for name in (
                           'BOOTLOADER_SKIP_VALIDATE_ALWAYS', 'BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON',
                           'BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP', 'EFUSE_VIRTUAL')),
                       'Approved bootloader config must enable rollback and full image validation')
    else:
        policy.require(binary_review.old_boot_region_sha256 == policy.sha(recovery[:policy.TABLE])
                       and binary_review.old_nominal_startup_watchdog_ms == 9000
                       and binary_review.deployment_authorized is False,
                       'Reviewed binary evidence differs from recovered boot region or scope')
    trust = policy.Trust(policy.sha(recovery[:policy.TABLE]), policy.RECOVERED_TABLE_SHA256)
    policy.validate_baseline(recovery, trust)
    return trust


def candidate_recovery_trust(files):
    """Finite old-binary facts for the separate candidate-approved route.

    This deliberately retains the old validator's original no-write purpose;
    its output is not an install permission. main() separately requires the
    candidate/source/test evidence and explicit operation/risk approval.
    """
    migration = recovery_object(files, 'migration_receipt')
    policy.require(policy.sha(recovery_bytes(files, 'migration_receipt')) == RECOVERED_MIGRATION_SHA256
                   and migration.get('artifact_provenance', {}).get('migrate_to_ab') is True,
                   'Original reviewed migration identity required')
    artifacts = {name: recovery_bytes(files, 'bootloader' if name == 'old_bootloader' else name)
                 for name in bootloader_evidence.ARTIFACT_SHA256}
    bounded = bootloader_evidence.validate_reviewed_binary_evidence(
        artifacts, purpose=bootloader_evidence.QUALIFICATION_PURPOSE)
    return _recovery_identity_trust(files, migration, bounded)


def check_service(unit):
    policy.require(isinstance(unit, str) and re.fullmatch(r'[a-zA-Z0-9_-]+\.service', unit), 'Invalid service unit')
    result = subprocess.run(['systemctl', 'show', unit, '--no-pager', '-p', 'LoadState',
                             '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'ControlPID'],
                            capture_output=True, text=True, timeout=10, check=False)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    policy.require(result.returncode == 0 and fields.get('LoadState') == 'loaded'
                   and fields.get('ActiveState') in ('inactive', 'failed')
                   and fields.get('SubState') in ('dead', 'failed')
                   and fields.get('MainPID') == '0' and fields.get('ControlPID') == '0',
                   'Service must be demonstrably stopped with zero PIDs: ' + unit)


class PiBackend:
    """Pinned, same-handle esptool adapter. Constructor itself has no effects."""
    def __init__(self, approval, journal):
        self.approval, self.journal = approval, journal
        self.chip = None
        self.original_port = None
        self.dev = None
        self.identity = None
        self.usb_number = None
        self.block_size = None
        self.writing = False
        self.written = []
        self.esptool = None
        self.app_device = None

    def prepare(self):
        package = Path(self.approval['runtime_package'])
        for name, (_, expected) in display_transport.PACKAGES.items():
            local_bytes(package / name, expected, protected=True)
        policy.require(not any(name == 'esptool' or name.startswith('esptool.') for name in sys.modules),
                       'Refuse a preloaded unverified esptool runtime')
        env = display_transport.prepare(package, self.journal.path / '.esptool')
        sys.path.insert(0, env['PYTHONPATH'])
        import esptool
        from esptool.loader import StubFlasher
        policy.require(esptool.__version__ == display_transport.VERSION == policy.TRANSPORT_VERSION,
                       'Only pinned reviewed esptool 5.4.0 supported')
        policy.require(Path(esptool.__file__).resolve().is_relative_to(self.journal.path / '.esptool'),
                       'Unexpected esptool import origin')
        StubFlasher.STUB_SUBDIRS = ['2']
        self.esptool = esptool
        esp.ROOT = self.journal.path  # Logs only; immutable helper files stay unchanged.

    def assert_stopped(self, source=None):
        check_service('mixosd.service')
        if source:
            check_service('mixos-bootstrap-' + policy.task_id(source) + '.service')

    def open_application(self, dev):
        self.assert_stopped()
        policy.require(physical_identity(dev) == policy.APP_IDENTITY,
                       'Application physical identity mismatch before CDC open')
        esp.idle_port(dev)
        return ApplicationCdc(dev, native.DEVICE)

    def observe_boot(self, expected, baseline):
        return self.healthy_app(expected, 1, boot_baseline=baseline)

    def healthy_app(self, expected, slot, *, boot_baseline=None):
        """Reopen only before HELLO; health failures after HELLO fail closed.

        Enumeration, CDC open and HELLO each have a finite budget inside one
        120-second deadline. Reopening cannot hide a reboot or failed health
        observation, and never submits ENTER_BOOT, reset or flash operations.
        """
        self.assert_stopped()
        deadline = time.monotonic() + 120
        for attempt in range(3):
            link = None
            stage = 'enumeration'
            remaining = deadline - time.monotonic()
            policy.require(remaining > 0, 'Fresh app health budget exhausted')
            if attempt:
                time.sleep(min(1.0, remaining))
            remaining = deadline - time.monotonic()
            policy.require(remaining > 0, 'Fresh app health budget exhausted')
            dev, identity = esp.wait_port(policy.APP_IDENTITY['location'], rom=False,
                serial=policy.APP_IDENTITY['serial'], timeout=min(30, remaining))
            policy.require(identity == policy.APP_IDENTITY, 'Application physical identity mismatch')
            try:
                stage = 'cdc-open'
                with operation_timeout(stage, min(10, max(0.01, deadline-time.monotonic()))):
                    port = self.open_application(dev)
                with port:
                    stage = 'hello'
                    policy.require(physical_identity(dev) == policy.APP_IDENTITY,
                                   'Application changed while opening')
                    opened_device = os.fstat(port.fileno()).st_rdev
                    link = ota_esp.Link(port, random_sessions=True)
                    with operation_timeout(stage, min(10, max(0.01, deadline-time.monotonic()))):
                        link.handshake(min(10, max(0.01, deadline-time.monotonic())))
                    stage = 'health'
                    remaining = deadline-time.monotonic()
                    policy.require(remaining > 0, 'Fresh app health budget exhausted')
                    with operation_timeout(stage, remaining):
                        if boot_baseline is None:
                            result = observe_app_health(link, expected, slot, remaining)
                        else:
                            policy.require(slot == 1, 'Baseline fallback is only for first B boot')
                            result = observe_boot_health(link, expected, boot_baseline, remaining)
                    policy.require(physical_identity(dev) == policy.APP_IDENTITY
                                   and os.stat(dev).st_rdev == opened_device,
                                   'Application handle changed')
                    self.app_device = dev
                    return result
            except (OSError, ota_esp.Timeout) as exc:
                # A transport error after any accepted HELLO is a failed
                # observation, not permission to reset the health interval.
                esp.audit('bootstrap_app_observation_failed', attempt=attempt+1,
                          stage=stage, epoch=getattr(link, 'epoch', 0),
                          error_type=type(exc).__name__, errno=getattr(exc, 'errno', None),
                          reason=str(exc))
                if attempt == 2 or (link is not None and link.epoch) or stage == 'health':
                    raise
        raise policy.Refused('Application observation exhausted')

    def enter_boot(self):
        self.assert_stopped()
        policy.require(self.app_device is not None and physical_identity(self.app_device) == policy.APP_IDENTITY,
                       'Fresh healthy app required before PREPARE_UPDATE')
        mark_rom_entry(self.journal.path)
        with operation_timeout('PREPARE_UPDATE/ENTER_BOOT', 30):
            font.enter_download_direct(self.app_device, policy.APP_IDENTITY,
                                       open_port=self.open_application)
        self.dev, self.identity = esp.wait_port(policy.ROM_IDENTITY['location'], rom=True, timeout=25)
        self._identity_check()

    def _identity_check(self):
        observed = physical_identity(self.dev)
        observed = dict(observed, serial=observed['serial'].lower())
        policy.require(observed == policy.ROM_IDENTITY, 'Only exact same-chip USB-OTG ROM is supported')
        self.identity = observed

    def _usb_number(self):
        text = (Path('/sys/bus/usb/devices') / policy.ROM_IDENTITY['location'] / 'devnum').read_text().strip()
        policy.require(text.isdecimal() and 0 < int(text) < 128, 'Invalid USB enumeration identity')
        return text

    def _security(self):
        chip = self.chip
        with operation_timeout('current-handle chip/security/MAC', 30):
            security = chip.get_security_info(cache=False)
            mac = ':'.join(f'{b:02x}' for b in chip.read_mac())
            policy.require(security.get('flags') == 0 and security.get('chip_id') == 9
                           and security.get('flash_crypt_cnt') == 0 and mac == policy.ROM_IDENTITY['serial'],
                           'Chip, security or MAC mismatch')
        return security

    def connect(self, fresh, expected=None):
        self.assert_stopped()
        policy.require(self.esptool is not None and self.chip is None, 'Prepared single connection required')
        if not fresh:
            self.dev, self.identity = esp.wait_port(policy.ROM_IDENTITY['location'], rom=True, timeout=10)
        self._identity_check()
        self.usb_number = self._usb_number()
        if not fresh:
            policy.require(expected == self.continuity(), 'Prior verified handle/enumeration evidence mismatch')
        esp.idle_port(self.dev)
        from esptool.cmds import connect_esp
        with operation_timeout('ROM no-reset connection', 60):
            self.chip = connect_esp(port=self.dev, connect_attempts=1, open_port_attempts=1,
                                    initial_baud=115200, chip='esp32s3', before='no-reset')
        policy.require(self.chip is not None, 'ROM connection failed')
        font.configure_port(self.chip)
        self.original_port = self.chip._port
        self.device_number = os.fstat(self.original_port.fileno()).st_rdev
        self._identity_check()
        policy.require(self._usb_number() == self.usb_number, 'USB changed while connecting')
        existing = bool(getattr(self.chip, 'sync_stub_detected', False) or self.chip.IS_STUB)
        if fresh:
            policy.require(not existing, 'Fresh ROM required; stale/unidentified stub refused')
            self._security()
            with operation_timeout('pinned modern stub upload', 60):
                stub = self.chip.run_stub()
            if stub._port is not self.original_port:
                stub._port.close()
                raise policy.Refused('Uploaded stub replaced verified serial handle')
            self.chip = stub
        else:
            # Only the exact verified install's still-running stub is accepted.
            # Never upload one during boot-only recovery and never adopt ROM as
            # evidence that the source job left the expected process running.
            policy.require(existing and expected['stub_sha256'] == display_transport.STUB_SHA256,
                           'Boot-only requires the verified task stub; no upload permitted')
            if not self.chip.IS_STUB:
                self.chip = self.chip.STUB_CLASS(self.chip)
        policy.require(self.chip.IS_STUB and self.chip._port is self.original_port, 'Stub handle continuity failed')
        font.configure_port(self.chip)
        self.block_size = self.chip.FLASH_WRITE_SIZE
        with operation_timeout('SPI attach and flash identification', 30):
            self.chip.change_baud(460800)
            self.chip.flash_spi_attach(0)
            flash_id = self.chip.flash_id(cache=False)
            policy.require((flash_id >> 16) & 0xFF == 0x17, 'Flash is not exactly 8 MiB')
            self.chip.flash_set_parameters(policy.FLASH_SIZE)
        self.guard()

    def continuity(self):
        return dict(device=self.dev, identity=policy.ROM_IDENTITY, usb_number=self.usb_number,
                    version=policy.TRANSPORT_VERSION, stub_sha256=display_transport.STUB_SHA256)

    def guard(self):
        policy.require(self.chip is not None and self.chip._port is self.original_port
                       and self.chip.IS_STUB and not self.original_port.closed, 'Verified handle lost')
        self._identity_check()
        policy.require(self._usb_number() == self.usb_number
                       and os.stat(self.dev).st_rdev == self.device_number
                       and os.fstat(self.original_port.fileno()).st_rdev == self.device_number,
                       'ROM re-enumerated or current handle changed')
        self._security()

    def read_region(self, offset, length):
        policy.require(0 <= offset < policy.FLASH_SIZE and 0 < length <= policy.FLASH_SIZE - offset,
                       'Read range exceeds 8 MiB')
        deadline = time.monotonic() + 600
        parts = []
        for address in range(offset, offset + length, READ_CHUNK):
            size = min(READ_CHUNK, offset + length - address)
            remaining = deadline - time.monotonic()
            policy.require(remaining > 0, 'Flash read total deadline exceeded')
            with operation_timeout('flash read chunk', min(60, remaining)):
                data = self.chip.read_flash(address, size)
            policy.require(len(data) == size, 'Short/oversized flash read')
            parts.append(data)
        return b''.join(parts)

    def read_full(self, phase):
        self.guard()
        esp.audit('bootstrap_read_start', phase=phase)
        # Return immediately so the policy can durably publish a complete
        # backup even if a subsequent identity/layout/disk-audit guard refuses.
        return self.read_region(0, policy.FLASH_SIZE)

    def validate_candidate(self, data):
        # More restrictive than the ROM's revision-override eFuses: never force
        # a revision limit, even if a device happens to permit bypassing it.
        with operation_timeout('candidate chip/eFuse revision limits', 30):
            revision = self.chip.get_major_chip_version() * 100 + self.chip.get_minor_chip_version()
            efuse_revision = self.chip.get_blk_version_major() * 100 + self.chip.get_blk_version_minor()
        minimum, maximum = struct.unpack_from('<HH', data, 15)
        low, high = struct.unpack_from('<HH', data, 208)
        policy.require(revision >= minimum and (maximum in (0, 65535) or revision <= maximum)
                       and efuse_revision >= low and (high in (0, 65535) or efuse_revision <= high),
                       'Candidate hardware/eFuse revision limits mismatch')

    def write_once(self, offset, data):
        policy.require(self.approval['kind'] == 'install' and self.block_size == 0x800 and not self.writing,
                       'Write permission/geometry mismatch or an uncertain prior writer')
        policy.require((offset == policy.APP1 and len(data) == policy.APP_SIZE and not self.written)
                       or (offset in (policy.OTADATA, policy.OTADATA + policy.SECTOR)
                           and len(data) == policy.SECTOR and self.written == [policy.APP1]),
                       'Only ordered ota_1 and one exact 4 KiB metadata write permitted')
        self.assert_stopped()
        self.guard()
        self.writing = True  # A failed command leaves this latch set; no reset.
        self.written.append(offset)
        deadline = time.monotonic() + 600
        with operation_timeout('one flash begin', 60):
            self.chip.flash_begin(len(data), offset)
        for seq, start in enumerate(range(0, len(data), 0x800)):
            remaining = deadline - time.monotonic()
            policy.require(remaining > 0, 'Flash write total deadline exceeded')
            with operation_timeout('single submission flash block', min(35, remaining)):
                font.flash_block_once(self.chip, data[start:start + 0x800], seq)
        with operation_timeout('write completion barrier and device hash', 60):
            self.chip.read_reg(self.esptool.ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR, timeout=30)
            policy.require(self.chip.flash_md5sum(offset, len(data)) == hashlib.md5(data).hexdigest(),
                           'Device-side write hash mismatch; no retry')
        self.writing = False

    def assert_reset_safe(self):
        policy.require(not self.writing and self.approval['kind'] in ('qualify', 'boot-only')
                       and self.approval.get('reset_method') == policy.RESET_METHOD
                       and self.approval.get('allow_clear_force_download') is True,
                       'Separate explicit watchdog reset permission required')
        self.guard()

    def watchdog_reset(self):
        self.assert_reset_safe()
        # The durable claim is written by policy BEFORE this function. This
        # volatile mask is exactly esptool 5.4 ESP32S3ROM.hard_reset's preparatory
        # operation, but we never invoke hard_reset's RTS/fallback logic.
        policy.require((self.journal.path / 'reset-claim.json').is_file(), 'Missing durable reset claim')
        with operation_timeout('approved official watchdog reset once', 30):
            chip = self.chip
            if chip.read_reg(chip.RTC_CNTL_OPTION1_REG) & chip.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK:
                chip.write_reg(chip.RTC_CNTL_OPTION1_REG, 0, chip.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK)
            policy.require(chip.read_reg(chip.RTC_CNTL_OPTION1_REG) & chip.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK == 0,
                           'Force-download flag did not clear; reset refused')
            chip.watchdog_reset()

    def close(self):
        if self.chip is not None:
            self.chip._port.close()
            self.chip = None


def verify_code(approval):
    root = TOOLS.parent
    # Include the helper modules imported by existing tools, not just this CLI.
    paths = [TOOLS / name for name in ('bootstrap_ota_on_pi.py', 'flash_font_on_pi.py',
              'flash_esp_on_pi.py', 'update_esp.py', 'display_transport.py',
              'ota_esp.py', 'ota_v2.py', 'mixos_esp_update.py')]
    paths += list((TOOLS / '_mixlib').glob('*.py')) + list((root / 'linux').glob('*.py'))
    expected = approval.get('code_sha256', {})
    policy.require(set(expected) == {p.relative_to(root).as_posix() for p in paths},
                   'Approval must pin all imported local code files')
    for path in paths:
        local_bytes(path, expected[path.relative_to(root).as_posix()], protected=True)


def verify_state_directory():
    policy.require(STATE.resolve(strict=True) == STATE, 'State registry must already exist canonically')
    for path in (STATE, *STATE.parents):
        info = path.lstat()
        policy.require(stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
                       and info.st_uid == (os.geteuid() if path == STATE else 0),
                       'State registry must be private under root-owned protected ancestors')
    policy.require(shutil.disk_usage(STATE).free >= 80 * 1024 * 1024, 'Insufficient durable backup space')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--approval-sha256')
    parser.add_argument('--inspect', type=Path, help='Offline plan against an existing 8 MiB snapshot')
    parser.add_argument('--execute', action='store_true', help='Explicit real local-device permission; requires approved task')
    parser.add_argument('--recover-service', action='store_true', help='Approved ExecStopPost cleanup only; no device access')
    args = parser.parse_args(argv)
    policy.require(not args.recover_service or (args.execute and args.approval is not None),
                   'Service recovery requires explicit execution and immutable approval')
    if args.approval is None:
        policy.require(not args.execute and args.inspect is None, 'Approval required')
        print(json.dumps(dict(dry_run=True, device_opened=False, flash_programming=False,
                              required='immutable approved qualify -> install -> separate boot-only tasks')))
        return 0
    policy.require(args.approval_sha256 is not None, 'Explicit approval SHA256 required')
    policy.require(not (args.execute and args.inspect), 'Offline inspect cannot be executed')
    if args.execute:
        policy.require(sys.platform == 'linux' and os.geteuid() != 0 and timeouts_enforced()
                       and sys.flags.isolated and shutil.which('fuser'),
                       'Execute needs isolated Python (-I), Linux timeouts, fuser and an ordinary dialout user')
    raw = local_bytes(args.approval, args.approval_sha256, args.execute, limit=1024 * 1024)
    approval = strict_json(raw)
    policy.require(approval.get('schema') == 1 and approval.get('approved') is True
                   and approval.get('kind') in ('qualify', 'install', 'boot-only'), 'Unsupported/unapproved task')
    identifier = policy.task_id(approval.get('task_id'))
    kind = approval['kind']
    evidence_mode = approval.get('bootloader_evidence_mode', 'historical-build')
    policy.require(evidence_mode in ('historical-build', bootloader_evidence.ROUTE, candidate_route.ROUTE),
                   'Unknown bootloader evidence route')
    policy.require(evidence_mode != bootloader_evidence.ROUTE or kind == 'qualify',
                   'Reviewed binary evidence only authorizes no-write qualification; candidate compatibility is unproven')
    files = {name: local_bytes(spec['path'], spec['sha256'], args.execute)
             for name, spec in approval.get('artifacts', {}).items()}
    execution_evidence = None
    if evidence_mode == candidate_route.ROUTE:
        image, execution_evidence = candidate_route.validate(approval, files)
        trust = candidate_recovery_trust(files)
    else:
        trust = recovery_trust(files, evidence_mode=evidence_mode,
                               task_kind=kind) if kind != 'boot-only' else None
    if kind == 'install' and execution_evidence is None:
        image = policy.validate_app(files['candidate'], approval['artifacts']['candidate']['sha256'])
        review = strict_json(files['candidate_review'])
        policy.require(review.get('schema') == 1 and review.get('sha256') == image['sha256']
                       and review.get('elf_sha256') == image['elf_sha256']
                       and review.get('safe_receiver_reviewed') is True
                       and review.get('rollback_health_confirmation_reviewed') is True,
                       'Candidate receiver and rollback-health review required, not merely an image hash')
        # Reuse the existing complete file validator in addition to the pure
        # byte policy; recheck bytes afterwards to reject mutable input races.
        candidate_path = Path(approval['artifacts']['candidate']['path'])
        validate_image(candidate_path, image['sha256'], 'esp32s3', LAYOUTS['ab'])
        policy.require(candidate_path.read_bytes() == files['candidate'], 'Candidate changed during validation')
    qualification = source = prior = None
    if execution_evidence is not None:
        if kind == 'install':
            qualification = recovery_object(files, 'qualification')
            policy.require(policy.sha(files['qualification']) == approval['qualification_sha256']
                           and qualification.get('task_id') == approval['qualification_task'],
                           'Original qualification artifact binding mismatch')
            policy.validate_qualification(qualification, trust, identifier)
        else:
            source = recovery_object(files, 'source_verified')
            policy.require(policy.sha(files['source_verified']) == approval['source_verified_sha256'],
                           'Original source installation artifact binding mismatch')
            candidate_route.validate_source(approval, files, source, execution_evidence)
    if not args.execute:
        result = dict(dry_run=True, task_id=identifier, kind=kind, device_opened=False)
        if args.inspect:
            policy.require(kind == 'install', 'Inspect requires an install approval')
            result['plan'] = policy.plan_bootstrap(args.inspect.read_bytes(), files['candidate'], image['sha256'], trust).summary()
        print(json.dumps(result, sort_keys=True))
        return 0
    verify_code(approval)
    verify_state_directory()
    # Read and bind prerequisites before stopping service or constructing a
    # device backend. The stopped-source checks are repeated under the lock.
    if kind == 'install':
        prior = STATE / policy.task_id(approval['qualification_task'])
        qualification = strict_json(local_bytes(prior / 'qualification.json', approval['qualification_sha256']))
        policy.require(qualification.get('task_id') == prior.name, 'Qualification source task mismatch')
        policy.validate_qualification(qualification, trust, identifier)
        check_service('mixos-bootstrap-' + prior.name + '.service')
    elif kind == 'boot-only':
        prior = STATE / policy.task_id(approval['source_task'])
        source = strict_json(local_bytes(prior / 'verified.json', approval['source_verified_sha256']))
        candidate_route.validate_source(approval, files, source, execution_evidence)
        check_service('mixos-bootstrap-' + prior.name + '.service')
    policy.require(approval.get('reset_method') == policy.RESET_METHOD
                   and approval.get('allow_clear_force_download') is True, 'Explicit watchdog policy required')
    # Native DeviceLock also holds the exact legacy flash.lock inode. Both
    # paths now exclude each other; this changed code must be newly approved.
    managed = approval.get('managed_service') is True
    if args.recover_service:
        policy.require(managed, 'Service recovery is only authorized for managed tasks')
        with native.DeviceLock(native.DEFAULT_LOCKS, native.DEVICE), native.cleanup_signals():
            verify_unit(approval, recovery=True)
            print(json.dumps(ServiceOwner(STATE, approval).recover(), sort_keys=True))
        return 0
    if managed:
        verify_unit(approval)
    with native.DeviceLock(native.DEFAULT_LOCKS, native.DEVICE), native.termination_handler():
        journal = policy.Journal(STATE, identifier, kind, policy.sha(raw))
        durable_new(journal.path / 'approval.json', raw)
        owner = ServiceOwner(STATE, approval) if managed else nullcontext()
        with owner:
            check_service('mixosd.service')
            backend = PiBackend(approval, journal)
            try:
                if prior is not None:
                    check_service('mixos-bootstrap-' + prior.name + '.service')
                backend.prepare()
                if kind == 'qualify':
                    result = policy.qualify(backend, journal, trust)
                elif kind == 'install':
                    result = policy.install(backend, journal, trust, files['candidate'], image['sha256'], qualification,
                                            execution_evidence=execution_evidence)
                else:
                    result = policy.boot_only(backend, journal, prior, source, approval['expected_sha256'],
                                              execution_evidence=execution_evidence)
                print(json.dumps(result, sort_keys=True))
                # The context manager still restores the service using the
                # separate exact-A proof; B installation remains unsuccessful.
                if result.get('kind') == 'boot-only-fallback':
                    return 2
            except Exception as exc:
                journal.save('aborted.json', dict(reason=str(exc), error_type=type(exc).__name__,
                             errno=getattr(exc, 'errno', None), traceback=traceback.format_exc(),
                             automatic_retry=False, automatic_reset=False))
                raise
            finally:
                backend.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STOP: ' + str(exc) + '; preserve task evidence, do not resubmit.', file=sys.stderr)
        raise SystemExit(2)
