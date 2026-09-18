#!/usr/bin/env python3
"""Build an immutable first-install/boot-only task package OFFLINE; never deploy.

--sources JSON: exact {schema:1, artifacts:{safe relative key:absolute path},
 runtime:{the four pinned wheel names:absolute path}}. Includes all fourteen
qualification inputs, candidate, candidate_review and candidate_evidence/*.
Nested artifact keys (e.g. candidate_evidence/source/foo.c) are stored literally
under artifacts/<key>; every artifact is pinned by the external approval.

--task-spec is a separate JSON object containing ONLY candidate_evidence_sha256,
runtime_risk_acceptance and the kind-specific fields: qualification_task and
qualification_sha256 for install; source_task, source_verified_sha256 and
expected_sha256 for boot-only. The evidence digest is supplied externally, never
invented here. Runtime risk acceptance must equal install.RISK_ACCEPTANCE.

Default output is approved=false. An external --approval plus independently
known --approval-sha256 must match the ENTIRE computed draft except approved.
Those bytes are preserved, not issued/reformatted. Candidate semantic validation
and approval issuance are external responsibilities; snapshot/build_package
accept an optional pure offline_validator(files, approval) callback which must
raise on refusal. No provisional candidate-module interface is assumed.

Output includes a separately staged, hash-pinned unchanged qualification
installer companion for safe primitive reuse. Printed commands only preflight;
no transport, install/start, device, task state, claims, or reset/write commands.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import bootstrap_install_install as installer
import bootstrap_qualification_package as qualification

safe = installer.safe


def source_paths(raw):
    value = safe.strict_json(raw)
    safe.require(set(value) == {'schema', 'artifacts', 'runtime'}
                 and type(value['schema']) is int and value['schema'] == 1, 'Exact source-map schema 1 required')
    safe.require(type(value['artifacts']) is dict, 'Artifact source object required')
    installer.artifact_names(value['artifacts'])
    safe.require(type(value['runtime']) is dict and set(value['runtime']) == set(safe.RUNTIME_SHA256),
                 'Exact four pinned runtime wheel sources required')
    for group in ('artifacts', 'runtime'):
        for name, path in value[group].items():
            safe.require(isinstance(path, str) and Path(path).is_absolute(),
                         'Absolute local source path required: ' + name)
    return value


def code_paths():
    # Exactly the same local-code closure as bootstrap.verify_code; no installer
    # is added to the execution package or smuggled into code_sha256.
    return qualification.code_paths()


def check_policy_pins():
    qualification.check_policy_pins()
    for name in ('ORIGINAL_SHA256', 'RECOVERY_SHA256', 'RUNTIME_SHA256', 'ARTIFACT_NAMES',
                 'MAIN_CODE', 'MINIMUM_CODE', 'NATIVE_EXTRA', 'RESET_METHOD'):
        safe.require(getattr(safe, name) == getattr(qualification.installer, name),
                     'Reviewed primitive pins differ from current qualification: ' + name)


def load_approval(path, expected_sha256, draft):
    safe.require(path is not None and expected_sha256 is not None, 'Both external approval and SHA256 required')
    raw = safe.read_regular(path, 1024 * 1024)
    safe.require(safe.sha(raw) == safe.digest(expected_sha256), 'External approval hash mismatch')
    external = safe.strict_json(raw)
    safe.require(external.get('approved') is True, 'External approved=true document required')
    # Canonical JSON comparison is type-sensitive (True is NOT the integer 1).
    safe.require(safe.encoded(dict(external, approved=False)) == safe.encoded(draft),
                 'External approval differs from exact task/code/artifact/evidence bindings')
    return raw


def snapshot(sources, *, kind, evidence_mode, identifier, remote_package, task_spec,
             approval_path=None, approval_sha256=None, offline_validator=None):
    safe.require(kind in installer.KIND_FIELDS and evidence_mode == installer.ROUTE,
                 'Only explicit install/boot-only + reviewed candidate route permitted')
    installer.task_id(identifier)
    # Normalize only caller-owned specification, never the external approval.
    spec = installer.task_spec(safe.strict_json(safe.encoded(task_spec)), kind)
    remote = safe.posix_absolute(remote_package)
    safe.require(remote.parts[1:2] == ('opt',) and len(remote.parts) >= 4 and remote.name == identifier,
                 'Purpose-named package beneath protected /opt parent required')
    check_policy_pins()
    sources = source_paths(safe.encoded(sources))
    paths = code_paths()
    files = {name: safe.read_regular(path) for name, path in paths.items()}
    code_hashes = {name: safe.sha(data) for name, data in files.items()}
    files.update({name: safe.read_regular(ROOT / name) for name in safe.NATIVE_EXTRA})
    artifacts = {name: safe.read_regular(path) for name, path in sources['artifacts'].items()}
    files.update({'artifacts/' + name: data for name, data in artifacts.items()})
    files.update({name: safe.read_regular(path) for name, path in sources['runtime'].items()})
    draft = dict(schema=1, approved=False, task_id=identifier, kind=kind,
                 bootloader_evidence_mode=evidence_mode, reset_method=safe.RESET_METHOD,
                 allow_clear_force_download=True, managed_service=True,
                 code_sha256=code_hashes, runtime_package=str(remote),
                 artifacts={name: dict(path=str(remote / 'artifacts' / name), sha256=safe.sha(data))
                            for name, data in sorted(artifacts.items())}, **spec)
    if approval_path is None and approval_sha256 is None:
        raw_approval = safe.encoded(draft)
    else:
        raw_approval = load_approval(approval_path, approval_sha256, draft)
    files['approval.json'] = raw_approval
    installer_bytes = safe.read_regular(TOOLS / installer.SCRIPT_NAME)
    primitives_bytes = safe.read_regular(TOOLS / installer.PRIMITIVES_NAME)
    safe.require(safe.sha(primitives_bytes) == safe.reviewed_source_sha256,
                 'Primitive companion changed since import; restart preparation')
    manifest = dict(schema=installer.SCHEMA, task_id=identifier, remote_package=str(remote),
                    files=safe.file_records(files), installer_sha256=safe.sha(installer_bytes),
                    primitives_sha256=safe.sha(primitives_bytes))
    files['manifest.json'] = safe.encoded(manifest)
    _, checked = installer.validate_payload(files, safe.sha(raw_approval))
    if offline_validator is not None:
        # The trusted local callback must raise on semantic refusal. It is never
        # read/imported from the source map or archive, nor called by installer.
        offline_validator(dict(files), safe.strict_json(raw_approval))
        _, checked = installer.validate_payload(files, safe.sha(raw_approval))
    final_paths = code_paths()
    safe.require(set(final_paths) == set(paths), 'Code set changed during snapshot')
    for name, path in final_paths.items():
        safe.require(safe.sha(safe.read_regular(path)) == code_hashes[name],
                     'Code changed during snapshot; prepare anew: ' + name)
    for name in safe.NATIVE_EXTRA:
        safe.require(safe.read_regular(ROOT / name) == files[name], 'Native extra changed during snapshot')
    safe.require(safe.read_regular(TOOLS / installer.SCRIPT_NAME) == installer_bytes
                 and safe.read_regular(TOOLS / installer.PRIMITIVES_NAME) == primitives_bytes,
                 'Installer/primitive source changed during snapshot')
    return files, installer_bytes, primitives_bytes


def dryrun_commands(approval, archive_hash, approval_hash, installer_hash, primitives_hash, remote_staging):
    staging = safe.posix_absolute(remote_staging)
    remote = safe.posix_absolute(approval['runtime_package'])
    safe.require(staging != remote and staging not in remote.parents and remote not in staging.parents,
                 'Staging and immutable package directories must be separate')
    for digest in (archive_hash, approval_hash, installer_hash, primitives_hash):
        safe.digest(digest)
    script = str(staging / installer.SCRIPT_NAME)
    companion = str(staging / installer.PRIMITIVES_NAME)
    checks = [installer_hash + '  ' + script, primitives_hash + '  ' + companion]
    check = "printf '%s\\n' " + ' '.join(shlex.quote(line) for line in checks) + ' | sha256sum --check --strict -'
    preview = shlex.join(['/usr/bin/python3', '-I', '-B', script, '--archive', str(staging / 'package.tar'),
                          '--archive-sha256', archive_hash, '--approval-sha256', approval_hash,
                          '--primitives-sha256', primitives_hash])
    return ('# Commands are printed only. Independently authenticate both installer files and all pins.\n'
            '# After separately reviewed transport, unprivileged preflight writes nothing:\n'
            + check + ' && ' + preview + '\n'
            '# Root use additionally requires both scripts staged root-owned 0444 under protected ancestors.\n'
            '# Installation, execution, semantic evidence review and approval issuance remain separate.\n'
            '# No reset/write/start/enable command is generated. No claims/task state are created.\n')


def build_package(*, sources, output, kind, evidence_mode, identifier, remote_package, remote_staging,
                  task_spec, approval_path=None, approval_sha256=None, offline_validator=None):
    output = Path(output)
    safe.require(output.is_absolute() and output.parent.resolve(strict=True) == output.parent,
                 'New absolute output directory under an existing canonical parent required')
    safe.require(output.name not in ('.', '..') and output.resolve(strict=False) == output,
                 'Canonical new output directory required')
    safe.require_absent(output)
    files, installer_bytes, primitives_bytes = snapshot(sources, kind=kind, evidence_mode=evidence_mode,
        identifier=identifier, remote_package=remote_package, task_spec=task_spec,
        approval_path=approval_path, approval_sha256=approval_sha256, offline_validator=offline_validator)
    archive = safe.tar_bytes(files)
    archive_hash, approval_hash = safe.sha(archive), safe.sha(files['approval.json'])
    approval = safe.strict_json(files['approval.json'])
    commands = dryrun_commands(approval, archive_hash, approval_hash, safe.sha(installer_bytes),
                               safe.sha(primitives_bytes), remote_staging)
    review = dict(schema=installer.SCHEMA, task_id=identifier, kind=kind, approved=approval['approved'],
                  final_approval_issued=False, archive_sha256=archive_hash, approval_sha256=approval_hash,
                  installer_sha256=safe.sha(installer_bytes), primitives_sha256=safe.sha(primitives_bytes),
                  candidate_evidence_sha256=approval['candidate_evidence_sha256'],
                  remote_package=remote_package, remote_staging=remote_staging,
                  installed=False, started=False, device_opened=False, task_state_created=False,
                  claims_created=False, offline_validator_used=offline_validator is not None,
                  candidate_semantics_validated=False,
                  code_files=sorted(approval['code_sha256']), native_extra=sorted(safe.NATIVE_EXTRA),
                  artifacts=sorted(approval['artifacts']), runtime=sorted(safe.RUNTIME_SHA256),
                  boundary='Packaging integrity only; candidate semantics and exact authorization are external')
    # All serialization/validation precedes output creation. Never overwrite or
    # clean up partial output; its existence blocks a silent retry.
    output.mkdir(mode=0o700)
    outputs = {'package.tar': archive, 'manifest.json': files['manifest.json'],
               'approval.external.json' if approval['approved'] else 'approval.draft.json': files['approval.json'],
               installer.SCRIPT_NAME: installer_bytes, installer.PRIMITIVES_NAME: primitives_bytes,
               'review.json': safe.encoded(review), 'commands.txt': commands.encode('ascii')}
    for name, data in outputs.items():
        with (output / name).open('xb') as stream:
            stream.write(data)
    return review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--kind', choices=sorted(installer.KIND_FIELDS), required=True)
    parser.add_argument('--bootloader-evidence-mode', choices=[installer.ROUTE], required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--remote-package', required=True)
    parser.add_argument('--remote-staging', required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--task-spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--approval-sha256')
    args = parser.parse_args(argv)
    result = build_package(sources=source_paths(safe.read_regular(args.sources, 1024 * 1024)),
        task_spec=safe.strict_json(safe.read_regular(args.task_spec, 1024 * 1024)), output=args.output,
        kind=args.kind, evidence_mode=args.bootloader_evidence_mode, identifier=args.task_id,
        remote_package=args.remote_package, remote_staging=args.remote_staging,
        approval_path=args.approval, approval_sha256=args.approval_sha256)
    print(safe.encoded(result).decode('ascii'), end='')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STOP: ' + str(exc) + '; no deployment performed.', file=sys.stderr)
        raise SystemExit(2)
