#!/usr/bin/env python3
"""Prepare an offline, reviewable, no-write qualification package; no SSH.

Required --sources JSON (schema=1): artifacts={exact name: absolute local path},
runtime={esptool.whl/runtime/*.whl: absolute local path}. See ARTIFACT_NAMES and
RUNTIME_SHA256 in bootstrap_qualification_install.py for the exact sets.

Default output contains approved=false; it is a DRAFT, never an authorization.
To package a separately issued approval, supply BOTH --approval and its external
--approval-sha256. Its entire document must match the computed task/code/artifact
bindings, with approved=true. The tool preserves those exact external bytes;
it never changes the draft into an approval or manufactures historical receipts.
Every invocation requires a NEW local output directory and explicit kind/route,
task ID, remote package and remote staging paths. No runtime task state is made.

Examples (paths/names and authorization are chosen by the external reviewer):
  py -3 -B tools/bootstrap_qualification_package.py --kind qualify \
    --bootloader-evidence-mode mixos-reviewed-bootloader-binary-evidence/v1 \
    --task-id mixos-qualify-reviewed-binary-20260917 \
    --remote-package /opt/mixos-bootstrap-packages/mixos-qualify-reviewed-binary-20260917 \
    --remote-staging /var/tmp/mixos-qualify-reviewed-binary-20260917 \
    --sources ABSOLUTE_SOURCES_JSON --output NEW_ABSOLUTE_DIRECTORY
Run the printed dry-run commands only after separate transport/inventory review.
Installing the package later requires the independent --install gate and never
starts/enables the unit. Starting qualification requires separate authorization.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import bootstrap_qualification_install as installer
import bootstrap_ota_on_pi as bootstrap
import display_transport
import mixos_esp_update as native
from _mixlib import bootloader_evidence


def source_paths(raw):
    value = installer.strict_json(raw)
    installer.require(set(value) == {'schema', 'artifacts', 'runtime'}
                      and type(value['schema']) is int and value['schema'] == 1,
                      'Exact source-map schema 1 required')
    installer.require(type(value['artifacts']) is dict and set(value['artifacts']) == installer.ARTIFACT_NAMES,
                      'Exact artifact source set required; no historical substitution/candidate')
    installer.require(type(value['runtime']) is dict and set(value['runtime']) == set(installer.RUNTIME_SHA256),
                      'Exact pinned runtime source set required')
    for group in ('artifacts', 'runtime'):
        for name, path in value[group].items():
            installer.require(isinstance(path, str) and Path(path).is_absolute(),
                              'Absolute local source path required: ' + name)
    return value


def code_paths():
    paths = {name: ROOT / name for name in installer.MAIN_CODE}
    for directory in ('tools/_mixlib', 'linux'):
        for path in (ROOT / directory).glob('*.py'):
            paths[path.relative_to(ROOT).as_posix()] = path
    installer.require(installer.MINIMUM_CODE <= set(paths), 'Required local code dependency missing')
    installer.require(set(native.RUNNER_FILES) <= set(paths) | installer.NATIVE_EXTRA,
                      'Native bundle dependencies changed; review package implementation')
    return dict(sorted(paths.items()))


def check_policy_pins():
    originals = {'bootloader' if name == 'old_bootloader' else name: value
                 for name, value in bootloader_evidence.ARTIFACT_SHA256.items()}
    installer.require(originals == installer.ORIGINAL_SHA256 and len(originals) == 10,
                      'Standalone installer original pins differ from production')
    installer.require(installer.ROUTE == bootloader_evidence.ROUTE
                      and installer.RESET_METHOD == bootstrap.policy.RESET_METHOD,
                      'Standalone qualification policy differs from production')
    installer.require(installer.RECOVERY_SHA256 == {
        'recovery_flash': bootstrap.policy.RECOVERED_FULL_SHA256,
        'migration_receipt': bootstrap.RECOVERED_MIGRATION_SHA256}, 'Recovery pins differ from production')
    installer.require(installer.RUNTIME_SHA256 == {name: value for name, (_, value) in display_transport.PACKAGES.items()},
                      'Runtime pins differ from production')


def load_approval(path, expected_sha256, draft):
    """Consume an external approval, never issue/rewrite/normalize one."""
    installer.require(path is not None and expected_sha256 is not None, 'Both external approval and SHA256 required')
    raw = installer.read_regular(path, 1024 * 1024)
    installer.require(installer.sha(raw) == installer.digest(expected_sha256), 'External approval hash mismatch')
    external = installer.strict_json(raw)
    installer.require(external.get('approved') is True, 'External approved=true document required')
    comparison = dict(external, approved=False)
    installer.require(comparison == draft, 'External approval differs from exact task/code/artifact bindings')
    return raw


def snapshot(sources, *, kind, evidence_mode, identifier, remote_package,
             approval_path=None, approval_sha256=None):
    installer.require(kind == 'qualify' and evidence_mode == installer.ROUTE,
                      'Only explicit qualify + reviewed binary evidence route permitted')
    installer.task_id(identifier)
    remote = installer.posix_absolute(remote_package)
    installer.require(remote.parts[1:2] == ('opt',) and len(remote.parts) >= 4 and remote.name == identifier,
                      'Purpose-named package beneath protected /opt parent required')
    check_policy_pins()
    sources = source_paths(installer.encoded(sources))
    files = {name: installer.read_regular(path) for name, path in code_paths().items()}
    code_hashes = {name: installer.sha(data) for name, data in files.items()}
    files.update({name: installer.read_regular(ROOT / name) for name in installer.NATIVE_EXTRA})
    artifacts = {name: installer.read_regular(Path(path)) for name, path in sources['artifacts'].items()}
    # Production's PURE recovery gate: original migration, full 8 MiB identity,
    # layout/padding, health and all ten immutable binary-review originals.
    # No PiBackend construction, process, service, serial or device operation.
    bootstrap.recovery_trust(artifacts, evidence_mode=evidence_mode, task_kind=kind)
    files.update({'artifacts/' + name: data for name, data in artifacts.items()})
    files.update({name: installer.read_regular(Path(path)) for name, path in sources['runtime'].items()})
    draft = dict(schema=1, approved=False, task_id=identifier, kind=kind,
                 bootloader_evidence_mode=evidence_mode, reset_method=installer.RESET_METHOD,
                 allow_clear_force_download=True, managed_service=True,
                 code_sha256=code_hashes, runtime_package=str(remote),
                 artifacts={name: dict(path=str(remote / 'artifacts' / name), sha256=installer.sha(data))
                            for name, data in sorted(artifacts.items())})
    if approval_path is None and approval_sha256 is None:
        raw_approval = installer.encoded(draft)
    else:
        raw_approval = load_approval(approval_path, approval_sha256, draft)
    files['approval.json'] = raw_approval
    installer_bytes = installer.read_regular(TOOLS / 'bootstrap_qualification_install.py')
    manifest = dict(schema=installer.SCHEMA, task_id=identifier, remote_package=str(remote),
                    files=installer.file_records(files), installer_sha256=installer.sha(installer_bytes))
    files['manifest.json'] = installer.encoded(manifest)
    installer.validate_payload(files, installer.sha(raw_approval))
    # Reject a source edit during package preparation, rather than bind a mixed
    # snapshot to a validator imported before that edit.
    final_paths = code_paths()
    installer.require(set(final_paths) == set(code_hashes), 'Code set changed during snapshot')
    for name, path in final_paths.items():
        installer.require(installer.sha(installer.read_regular(path)) == code_hashes[name],
                          'Code changed during snapshot; review and prepare anew')
    return files, installer_bytes


def dryrun_commands(approval, archive_hash, approval_hash, installer_hash, remote_staging):
    staging = installer.posix_absolute(remote_staging)
    remote = installer.posix_absolute(approval['runtime_package'])
    installer.require(staging != remote and staging not in remote.parents and remote not in staging.parents,
                      'Staging and immutable package directories must be separate')
    install_script = str(staging / 'bootstrap_qualification_install.py')
    archive = str(staging / 'package.tar')
    checksum = installer_hash + '  ' + install_script
    check = "printf '%s\\n' " + shlex.quote(checksum) + ' | sha256sum --check --strict -'
    preview = shlex.join(['/usr/bin/python3', '-I', '-B', install_script,
                          '--archive', archive, '--archive-sha256', archive_hash,
                          '--approval-sha256', approval_hash])
    bootstrap_preview = shlex.join(['/usr/bin/python3', '-I', '-B',
        str(remote / 'tools/bootstrap_ota_on_pi.py'), '--approval', str(remote / 'approval.json'),
        '--approval-sha256', approval_hash])
    return ('# Review pins independently. Commands are printed only; no SSH/upload/install/start is performed.\n'
            '# After separately reviewed staging, this unprivileged preflight writes nothing:\n'
            + check + ' && ' + preview + '\n'
            '# Later root installation needs a protected, independently authenticated installer,\n'
            '# an external approved=true package, and a separately authorized --install invocation.\n'
            '# Never execute a pi-writable installer as root; stage reviewed installer under root protection.\n'
            '# After separately authorized installation, offline production validation (NO --execute):\n'
            + bootstrap_preview + '\n'
            '# No systemctl start/enable command is generated. No task state is created.\n')


def build_package(*, sources, output, kind, evidence_mode, identifier, remote_package,
                  remote_staging, approval_path=None, approval_sha256=None):
    output = Path(output)
    installer.require(output.is_absolute() and output.parent.resolve(strict=True) == output.parent,
                      'New absolute output directory under an existing canonical parent required')
    installer.require(output.name not in ('.', '..') and output.resolve(strict=False) == output,
                      'Canonical new output directory required')
    installer.require_absent(output)
    files, installer_bytes = snapshot(sources, kind=kind, evidence_mode=evidence_mode, identifier=identifier,
        remote_package=remote_package, approval_path=approval_path, approval_sha256=approval_sha256)
    archive = installer.tar_bytes(files)
    archive_hash, approval_hash = installer.sha(archive), installer.sha(files['approval.json'])
    approval = installer.strict_json(files['approval.json'])
    commands = dryrun_commands(approval, archive_hash, approval_hash, installer.sha(installer_bytes), remote_staging)
    # All validation and serialization precede creation. mkdir and xb refuse
    # races/overwrites. Failed partial outputs are retained, never silently reused.
    output.mkdir(mode=0o700)
    review = dict(schema=installer.SCHEMA, task_id=identifier, kind=kind, approved=approval['approved'],
                  final_approval_issued=False, archive_sha256=archive_hash, approval_sha256=approval_hash,
                  installer_sha256=installer.sha(installer_bytes), remote_package=remote_package,
                  remote_staging=remote_staging, installed=False, started=False, device_opened=False,
                  task_state_created=False, code_files=sorted(approval['code_sha256']),
                  native_extra=sorted(installer.NATIVE_EXTRA), artifacts=sorted(installer.ARTIFACT_NAMES),
                  runtime=sorted(installer.RUNTIME_SHA256),
                  boundary='Current-A no-write qualification only; no B install permission or historical provenance')
    outputs = {'package.tar': archive, 'manifest.json': files['manifest.json'],
               'approval.external.json' if approval['approved'] else 'approval.draft.json': files['approval.json'],
               'bootstrap_qualification_install.py': installer_bytes,
               'review.json': installer.encoded(review), 'commands.txt': commands.encode('ascii')}
    for name, data in outputs.items():
        with (output / name).open('xb') as stream:
            stream.write(data)
    return review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--kind', choices=['qualify'], required=True)
    parser.add_argument('--bootloader-evidence-mode', choices=[installer.ROUTE], required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--remote-package', required=True)
    parser.add_argument('--remote-staging', required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--approval-sha256')
    args = parser.parse_args(argv)
    sources = source_paths(installer.read_regular(args.sources, 1024 * 1024))
    result = build_package(sources=sources, output=args.output, kind=args.kind,
                          evidence_mode=args.bootloader_evidence_mode, identifier=args.task_id,
                          remote_package=args.remote_package, remote_staging=args.remote_staging,
                          approval_path=args.approval, approval_sha256=args.approval_sha256)
    print(installer.encoded(result).decode('ascii'), end='')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STOP: ' + str(exc) + '; no deployment performed.', file=sys.stderr)
        raise SystemExit(2)
