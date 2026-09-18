"""Offline install/boot-only package tests; SYNTHETIC temporary inputs only.

Every approval in these fixtures binds synthetic bytes, not a real candidate or
historical artifact. Privileged environment, chown and systemctl are mocked;
all simulated installation writes are confined to TemporaryDirectory. No device,
network, actual install/start/enable, original artifact or task-state mutation.
"""
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import bootstrap_install_install as install
import bootstrap_install_package as package
import bootstrap_qualification_install as qualify

safe = install.safe
TASK = 'mixos-install-fixture-only'
PRIOR = 'mixos-qualify-reviewed-binary-fixture-only'
REMOTE = '/opt/mixos-bootstrap-packages/' + TASK
STAGING = '/var/tmp/' + TASK


def specification(kind='install'):
    common = dict(candidate_evidence_sha256='a' * 64, runtime_risk_acceptance=dict(install.RISK_ACCEPTANCE))
    if kind == 'install':
        return dict(common, qualification_task=PRIOR, qualification_sha256='b' * 64)
    return dict(common, source_task='mixos-install-source-fixture', source_verified_sha256='c' * 64,
                expected_sha256='d' * 64)


class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.addCleanup(self.make_writable)
        self.repo = self.temp / 'repo'
        self.paths = {}
        for name in safe.MINIMUM_CODE | safe.NATIVE_EXTRA:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(('# SYNTHETIC TEST CODE ONLY: ' + name + '\n').encode())
            if name not in safe.NATIVE_EXTRA:
                self.paths[name] = target
        self.sources = dict(schema=1, artifacts={}, runtime={})
        names = safe.ARTIFACT_NAMES | {'candidate', 'candidate_review', 'candidate_evidence/source/foo.c',
                                      'candidate_evidence/config/sdkconfig', 'extra/evidence.json'}
        pins = {}
        for group, keys in (('artifacts', names), ('runtime', safe.RUNTIME_SHA256)):
            for name in keys:
                target = self.temp / 'sources' / group / name
                target.parent.mkdir(parents=True, exist_ok=True)
                data = ('SYNTHETIC TEST INPUT ONLY: ' + group + '/' + name).encode()
                target.write_bytes(data)
                self.sources[group][name] = str(target)
                pins[name] = safe.sha(data)
        self.stack.enter_context(mock.patch.object(package, 'ROOT', self.repo))
        self.stack.enter_context(mock.patch.object(package, 'code_paths', side_effect=lambda: dict(self.paths)))
        self.stack.enter_context(mock.patch.object(package, 'check_policy_pins'))
        for name in ('ORIGINAL_SHA256', 'RECOVERY_SHA256', 'RUNTIME_SHA256'):
            mapping = getattr(safe, name)
            self.stack.enter_context(mock.patch.dict(mapping, {key: pins[key] for key in mapping}, clear=True))
        self.process = self.stack.enter_context(mock.patch.object(safe.subprocess, 'run'))
        self.chown = self.stack.enter_context(mock.patch.object(os, 'chown', create=True))
        self.old_validate = self.stack.enter_context(mock.patch.object(safe, 'validate_payload',
            side_effect=AssertionError('Must not use qualify-only payload validator')))
        self.old_install = self.stack.enter_context(mock.patch.object(safe, 'install',
            side_effect=AssertionError('Must not use qualify-only installer')))
        self.args = dict(sources=self.sources, kind='install', evidence_mode=install.ROUTE,
                         identifier=TASK, remote_package=REMOTE, task_spec=specification())
        self.files, self.installer_bytes, self.primitives_bytes = package.snapshot(**self.args)
        self.approval = safe.strict_json(self.files['approval.json'])
        self.approval_hash = safe.sha(self.files['approval.json'])

    def make_writable(self):
        if self.temp.exists():
            for path in self.temp.rglob('*'):
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)

    def manifest(self, files):
        manifest = safe.strict_json(files['manifest.json'])
        manifest['files'] = safe.file_records({n: d for n, d in files.items() if n != 'manifest.json'})
        files['manifest.json'] = safe.encoded(manifest)
        return files

    def external(self, approval=None):
        # Pure synthetic test fixture. Never an authorization of actual bytes.
        value = dict(self.approval if approval is None else approval, approved=True)
        raw = json.dumps(value, indent=3).encode()
        path = self.temp / 'external-fixture.json'
        path.write_bytes(raw)
        return path, safe.sha(raw), raw

    def approved_files(self):
        path, digest, _ = self.external()
        files, _, _ = package.snapshot(**dict(self.args, approval_path=path, approval_sha256=digest))
        return files, safe.strict_json(files['approval.json']), digest

    def build(self, **extra):
        return package.build_package(**dict(self.args, output=self.temp / 'out', remote_staging=STAGING, **extra))

    def archive(self, files=None):
        raw = safe.tar_bytes(self.files if files is None else files)
        path = self.temp / 'input.tar'
        path.write_bytes(raw)
        return path, safe.sha(raw)

    def test_deterministic_complete_draft_with_nested_artifacts(self):
        result = self.build()
        output = self.temp / 'out'
        self.assertEqual((output / 'package.tar').read_bytes(), safe.tar_bytes(self.files))
        self.assertEqual((output / install.PRIMITIVES_NAME).read_bytes(), self.primitives_bytes)
        self.assertEqual((output / install.SCRIPT_NAME).read_bytes(), self.installer_bytes)
        files, manifest, approval = install.preflight(output / 'package.tar', result['archive_sha256'], result['approval_sha256'])
        self.assertEqual(files, self.files)
        for key in ('approved', 'final_approval_issued', 'installed', 'started', 'device_opened',
                    'task_state_created', 'claims_created', 'candidate_semantics_validated'):
            self.assertIs(result[key], False)
        self.assertEqual(manifest['primitives_sha256'], safe.sha(self.primitives_bytes))
        self.assertTrue(safe.ARTIFACT_NAMES <= set(approval['artifacts']))
        record = approval['artifacts']['candidate_evidence/source/foo.c']
        self.assertEqual(record['path'], REMOTE + '/artifacts/candidate_evidence/source/foo.c')
        self.assertEqual(record['sha256'], safe.sha(files['artifacts/candidate_evidence/source/foo.c']))
        self.process.assert_not_called()
        self.chown.assert_not_called()
        self.old_validate.assert_not_called()
        self.old_install.assert_not_called()

    def test_boot_only_carries_all_original_candidate_and_evidence_inputs(self):
        files, _, _ = package.snapshot(**dict(self.args, kind='boot-only', task_spec=specification('boot-only')))
        approval = safe.strict_json(files['approval.json'])
        install.validate_payload(files, safe.sha(files['approval.json']))
        self.assertEqual(approval['kind'], 'boot-only')
        self.assertNotIn('qualification_task', approval)
        self.assertEqual(approval['expected_sha256'], 'd' * 64)
        self.assertTrue(safe.ARTIFACT_NAMES <= set(approval['artifacts']))
        self.assertIn('candidate', approval['artifacts'])

    def test_exact_external_approval_bytes_preserved(self):
        path, digest, raw = self.external()
        result = self.build(approval_path=path, approval_sha256=digest)
        self.assertTrue(result['approved'])
        self.assertFalse(result['final_approval_issued'])
        self.assertEqual((self.temp / 'out/approval.external.json').read_bytes(), raw)
        self.assertEqual(result['approval_sha256'], digest)

    def test_external_approval_hash_and_every_binding_are_exact(self):
        path, _, raw = self.external()
        with self.assertRaisesRegex(safe.Refused, 'hash mismatch'):
            package.load_approval(path, '0' * 64, self.approval)
        for key, value in (('schema', True), ('kind', 'boot-only'), ('approved', False),
                           ('candidate_evidence_sha256', 'e' * 64), ('qualification_sha256', 'e' * 64),
                           ('runtime_risk_acceptance', dict(install.RISK_ACCEPTANCE, no_fault_injection=1)),
                           ('task_id', TASK + '-changed'), ('managed_service', False), ('code_sha256', {})):
            changed = safe.strict_json(raw)
            changed[key] = value
            data = safe.encoded(changed)
            path.write_bytes(data)
            with self.subTest(key=key), self.assertRaises(safe.Refused):
                package.load_approval(path, safe.sha(data), self.approval)
        with self.assertRaises(safe.Refused):
            package.load_approval(path, None, self.approval)

    def test_dynamic_evidence_change_cannot_reuse_external_approval(self):
        path, digest, _ = self.external()
        source = Path(self.sources['artifacts']['candidate_evidence/source/foo.c'])
        source.write_bytes(source.read_bytes() + b'changed')
        with self.assertRaisesRegex(safe.Refused, 'exact task/code/artifact/evidence'):
            self.build(approval_path=path, approval_sha256=digest)
        self.assertFalse((self.temp / 'out').exists())

    def test_manifest_rehash_cannot_authorize_changed_evidence_or_code(self):
        for name, message in (('artifacts/candidate_evidence/source/foo.c', 'Artifact hash'),
                              ('tools/bootstrap_ota_on_pi.py', 'Code hash')):
            files = dict(self.files)
            files[name] += b'changed'
            self.manifest(files)
            with self.subTest(name=name), self.assertRaisesRegex(safe.Refused, message):
                install.validate_payload(files, self.approval_hash)

    def test_exact_original_pins_survive_approval_and_manifest_rehash(self):
        files = dict(self.files)
        name = 'reference_elf'
        files['artifacts/' + name] += b'changed'
        approval = copy.deepcopy(self.approval)
        approval['artifacts'][name]['sha256'] = safe.sha(files['artifacts/' + name])
        files['approval.json'] = safe.encoded(approval)
        self.manifest(files)
        with self.assertRaisesRegex(safe.Refused, 'Fixed original'):
            install.validate_payload(files, safe.sha(files['approval.json']))

    def test_source_changes_to_fixed_originals_and_runtime_refused(self):
        for group, name in (('artifacts', 'bootloader'), ('runtime', 'esptool.whl')):
            source = Path(self.sources[group][name])
            original = source.read_bytes()
            source.write_bytes(original + b'changed')
            with self.subTest(group=group), self.assertRaises(safe.Refused):
                package.snapshot(**self.args)
            source.write_bytes(original)

    def test_exact_source_schema_and_runtime_required(self):
        for action in ('schema', 'base', 'candidate', 'evidence', 'runtime', 'relative', 'extra-field'):
            changed = copy.deepcopy(self.sources)
            if action == 'schema':
                changed['schema'] = True
            elif action == 'base':
                del changed['artifacts']['recovery_boot']
            elif action == 'candidate':
                del changed['artifacts']['candidate']
            elif action == 'evidence':
                changed['artifacts'] = {k: v for k, v in changed['artifacts'].items() if not k.startswith('candidate_evidence/')}
            elif action == 'runtime':
                changed['runtime'] = {}
            elif action == 'relative':
                changed['artifacts']['candidate'] = 'relative.bin'
            else:
                changed['approval'] = True
            with self.subTest(action=action), self.assertRaises(safe.Refused):
                package.source_paths(safe.encoded(changed))

    def test_nested_source_keys_reject_unsafe_and_colliding_paths(self):
        for name in ('../escape', '/absolute', 'a/../b', 'a//b', 'a/./b', 'a\\b', 'a b',
                     'a\nUser=root', 'a/%i', 'C:/foo', 'a/', 'candidate_evidence'):
            changed = copy.deepcopy(self.sources)
            changed['artifacts'][name] = self.sources['artifacts']['candidate']
            with self.subTest(name=name), self.assertRaises(safe.Refused):
                package.source_paths(safe.encoded(changed))

    def test_task_spec_digest_types_and_exact_risk_acceptance(self):
        for kind in ('install', 'boot-only'):
            good = specification(kind)
            for key in good:
                changed = copy.deepcopy(good)
                del changed[key]
                with self.subTest(kind=kind, missing=key), self.assertRaises(safe.Refused):
                    install.task_spec(changed, kind)
            for bad in ('', 'A' * 64, 'a' * 63, 'a' * 65, True, None, 'x' * 64):
                with self.subTest(kind=kind, hash=bad), self.assertRaises(safe.Refused):
                    install.task_spec(dict(good, candidate_evidence_sha256=bad), kind)
            for risk in ({}, dict(install.RISK_ACCEPTANCE, no_fault_injection=1),
                         dict(install.RISK_ACCEPTANCE, preserve_baseline_a=False),
                         dict(install.RISK_ACCEPTANCE, additional=True)):
                with self.subTest(kind=kind, risk=risk), self.assertRaises(safe.Refused):
                    install.task_spec(dict(good, runtime_risk_acceptance=risk), kind)
            with self.assertRaises(safe.Refused):
                install.task_spec(dict(good, approved=True), kind)

    def test_kind_route_remote_and_source_task_are_explicit(self):
        for change in ({'kind': 'qualify'}, {'kind': None}, {'evidence_mode': qualify.ROUTE},
                       {'evidence_mode': 'historical-build'}, {'evidence_mode': 'auto'},
                       {'identifier': '../escape'}, {'remote_package': '/opt/' + TASK},
                       {'remote_package': REMOTE + '-other'}, {'remote_package': '/opt/%i/' + TASK},
                       {'task_spec': dict(specification(), qualification_task=TASK)}):
            with self.subTest(change=change), self.assertRaises(safe.Refused):
                package.snapshot(**dict(self.args, **change))

    def test_payload_exact_fields_and_kind_specific_extras(self):
        for key, value in (('schema', True), ('approved', 1), ('kind', 'qualify'),
                           ('bootloader_evidence_mode', qualify.ROUTE), ('code_sha256', {}),
                           ('runtime_risk_acceptance', {}), ('source_task', PRIOR)):
            files = dict(self.files)
            approval = copy.deepcopy(self.approval)
            approval[key] = value
            files['approval.json'] = safe.encoded(approval)
            self.manifest(files)
            with self.subTest(key=key), self.assertRaises(safe.Refused):
                install.validate_payload(files, safe.sha(files['approval.json']))

    def test_exact_manifest_and_approved_fileset(self):
        for action in ('extra', 'missing', 'size', 'hash', 'mode', 'schema', 'bool-size'):
            files = dict(self.files)
            manifest = safe.strict_json(files['manifest.json'])
            if action == 'extra':
                files['linux/evil.py'] = b'raise AssertionError("must never import")'
                self.manifest(files)
            elif action == 'missing':
                del files['linux/protocol.py']
            elif action == 'schema':
                manifest['schema'] = qualify.SCHEMA
                files['manifest.json'] = safe.encoded(manifest)
            else:
                key = {'size': 'bytes', 'hash': 'sha256', 'mode': 'mode', 'bool-size': 'bytes'}[action]
                manifest['files']['linux/protocol.py'][key] = True if action == 'bool-size' else 'invalid'
                files['manifest.json'] = safe.encoded(manifest)
            with self.subTest(action=action), self.assertRaises(safe.Refused):
                install.validate_payload(files, self.approval_hash)

    def test_draft_and_tampered_approved_payload_never_reach_writer(self):
        with mock.patch.object(install, 'install_environment') as env:
            with self.assertRaisesRegex(safe.Refused, 'draft'):
                install.install(self.files, self.approval, self.approval_hash)
            files, approval, digest = self.approved_files()
            files['linux/protocol.py'] += b'tampered'
            with self.assertRaises(safe.Refused):
                install.install(files, approval, digest)
        env.assert_not_called()
        self.process.assert_not_called()
        self.chown.assert_not_called()

    def test_no_output_on_validation_failure_and_no_overwrite(self):
        with self.assertRaises(safe.Refused):
            self.build(task_spec={})
        self.assertFalse((self.temp / 'out').exists())
        (self.temp / 'out').mkdir()
        (self.temp / 'out/original').write_bytes(b'keep')
        with self.assertRaisesRegex(safe.Refused, 'Existing'):
            self.build()
        self.assertEqual((self.temp / 'out/original').read_bytes(), b'keep')

    def test_optional_offline_validator_is_explicit_and_runs_before_output(self):
        validator = mock.Mock(side_effect=safe.Refused('semantic gate refused'))
        with self.assertRaisesRegex(safe.Refused, 'semantic gate'):
            self.build(offline_validator=validator)
        validator.assert_called_once()
        self.assertEqual(validator.call_args.args[1], self.approval)
        self.assertFalse((self.temp / 'out').exists())
        accepted = mock.Mock()
        result = self.build(offline_validator=accepted)
        self.assertTrue(result['offline_validator_used'])
        self.assertFalse(result['candidate_semantics_validated'])
        self.assertFalse(result['approved'])

    def test_code_edit_during_snapshot_refused(self):
        def edit(files, approval):
            path = self.paths['linux/protocol.py']
            path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(safe.Refused, 'Code changed'):
            self.build(offline_validator=edit)
        self.assertFalse((self.temp / 'out').exists())

    def test_printed_commands_only_preflight_and_pin_both_installers(self):
        self.build()
        text = (self.temp / 'out/commands.txt').read_text()
        actual = '\n'.join(line for line in text.splitlines() if not line.startswith('#'))
        self.assertIn('--primitives-sha256', actual)
        self.assertIn(install.PRIMITIVES_NAME, actual)
        self.assertIn('sha256sum --check --strict', actual)
        for forbidden in ('--execute', '--install', 'sudo ', 'ssh ', 'systemctl', 'mkdir', '--reset'):
            self.assertNotIn(forbidden, actual)
        with self.assertRaisesRegex(safe.Refused, 'separate'):
            package.dryrun_commands(self.approval, *(['a' * 64] * 4), REMOTE + '/stage')

    def test_installer_default_preflight_is_readonly_and_never_imports_archive(self):
        path, digest = self.archive()
        before = sorted(str(p) for p in self.temp.rglob('*'))
        with (mock.patch.object(install, '_load_primitives', return_value=safe) as load,
              redirect_stdout(io.StringIO()) as output):
            result = install.main(['--archive', str(path), '--archive-sha256', digest,
                                   '--approval-sha256', self.approval_hash,
                                   '--primitives-sha256', safe.sha(self.primitives_bytes)])
        self.assertEqual(result, 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value['dry_run'])
        self.assertFalse(value['host_preconditions_checked'])
        self.assertFalse(value['candidate_semantics_validated'])
        self.assertEqual(before, sorted(str(p) for p in self.temp.rglob('*')))
        load.assert_called_once()
        self.process.assert_not_called()
        self.chown.assert_not_called()

    def test_archive_pin_checked_before_unpack(self):
        path, _ = self.archive()
        with mock.patch.object(safe, 'unpack_checked') as unpack, self.assertRaisesRegex(safe.Refused, 'archive SHA256'):
            install.preflight(path, '0' * 64, self.approval_hash)
        unpack.assert_not_called()

    def test_required_unit_is_unprivileged_bounded_without_enable_section(self):
        for kind in ('install', 'boot-only'):
            approval = dict(self.approval, kind=kind)
            text = install.unit_text(approval, self.approval_hash).decode()
            for literal in ('Type=exec\n', 'User=pi\n', 'Group=dialout\n', 'Restart=no\n',
                            'RuntimeMaxSec=1500s\n', '--recover-service\n', 'KillMode=control-group\n'):
                self.assertIn(literal, text)
            self.assertIn('ExecStopPost=/usr/bin/python3 -I -B ', text)
            self.assertEqual(text.count('--execute'), 2)
            self.assertNotIn('[Install]', text)
            self.assertNotIn('User=root', text)

    def test_simulated_writer_immutable_exclusive_daemonreload_only(self):
        files, approval, digest = self.approved_files()
        remote = self.temp / 'installed'
        unit = self.temp / ('mixos-bootstrap-' + TASK + '.service')
        with (mock.patch.object(install, 'authenticate_installers') as auth,
              mock.patch.object(install, 'install_environment', return_value=(remote, unit)),
              mock.patch.object(install, 'prerequisite_unused') as prior,
              mock.patch.object(safe, 'unit_absent') as absent,
              mock.patch.object(safe, 'sync_directory'), mock.patch.object(safe, 'verify_installed') as verify,
              mock.patch.object(os, 'O_NOFOLLOW', getattr(os, 'O_NOFOLLOW', 0), create=True),
              mock.patch.object(os, 'fchmod', create=True) as fchmod):
            result = install.install(files, approval, digest)
            self.assertFalse(result['started'])
            self.assertFalse(result['enabled'])
            self.assertFalse(result['task_state_created'])
            self.assertFalse(result['claims_created'])
            self.assertEqual({p.relative_to(remote).as_posix() for p in remote.rglob('*') if p.is_file()}, set(files))
            for name, data in files.items():
                self.assertEqual((remote / name).read_bytes(), data)
            self.assertEqual(unit.read_bytes(), install.unit_text(approval, digest))
            self.assertEqual([call.args[1] for call in fchmod.call_args_list], [0o444] * (len(files) + 1))
            self.assertTrue(all(call.args[1:] == (0, 0) for call in self.chown.call_args_list))
            auth.assert_called_once()
            prior.assert_called_once_with(approval)
            absent.assert_called_once_with(unit.name)
            verify.assert_called_once_with(remote, files)
            with self.assertRaises(FileExistsError):
                install.install(files, approval, digest)
        self.process.assert_called_once_with(['/usr/bin/systemctl', 'daemon-reload'], check=True, timeout=30)
        self.old_install.assert_not_called()
        self.old_validate.assert_not_called()

    def test_partial_write_failure_retained_no_unit_or_reload(self):
        files, approval, digest = self.approved_files()
        remote = self.temp / 'partial'
        unit = self.temp / 'fixture.service'
        with (mock.patch.object(install, 'authenticate_installers'),
              mock.patch.object(install, 'install_environment', return_value=(remote, unit)),
              mock.patch.object(safe, 'new_root_file', side_effect=OSError('disk full'))):
            with self.assertRaisesRegex(OSError, 'disk full'):
                install.install(files, approval, digest)
        self.assertTrue(remote.exists())
        self.assertFalse(unit.exists())
        self.process.assert_not_called()

    def test_used_prerequisites_refused_for_both_kinds_without_claim_writes(self):
        registry = self.temp / 'registry'
        registry.mkdir()
        with mock.patch.object(safe, 'STATE', registry):
            for kind, category, source in (('install', 'qualification-use', PRIOR),
                    ('boot-only', 'reset-use', specification('boot-only')['source_task'])):
                approval = dict(self.approval, kind=kind, **specification(kind))
                claim = registry / (category + '-' + source + '.json')
                install.prerequisite_unused(approval)
                claim.write_bytes(b'EXISTING SYNTHETIC CLAIM')
                with self.subTest(kind=kind), self.assertRaisesRegex(safe.Refused, 'Existing'):
                    install.prerequisite_unused(approval)
                self.assertEqual(claim.read_bytes(), b'EXISTING SYNTHETIC CLAIM')
                claim.unlink()
        self.assertEqual(list(registry.iterdir()), [])

    def test_environment_refuses_existing_task_package_unit_and_own_claims(self):
        registry = self.temp / 'registry'
        units = self.temp / 'units'
        registry.mkdir(); units.mkdir()
        original_lstat = Path.lstat
        def info(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path == registry:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000)
            return value
        fake_pwd = SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000))
        fake_grp = SimpleNamespace(getgrnam=lambda _: SimpleNamespace(gr_gid=20, gr_mem=['pi']))
        approval = dict(self.approval, runtime_package=str(self.temp / 'remote'))
        with (mock.patch.object(safe, 'STATE', registry), mock.patch.object(safe, 'UNIT_DIRECTORY', units),
              mock.patch.object(sys, 'platform', 'linux'),
              mock.patch.object(sys, 'flags', SimpleNamespace(isolated=1)),
              mock.patch.object(os, 'geteuid', return_value=0, create=True),
              mock.patch.dict(sys.modules, pwd=fake_pwd, grp=fake_grp),
              mock.patch.object(safe, 'protected_directory'),
              mock.patch.object(Path, 'lstat', autospec=True, side_effect=info),
              mock.patch.object(safe, 'unit_absent')):
            targets = [registry / TASK, registry / ('qualification-use-' + TASK + '.json'),
                       registry / ('reset-use-' + TASK + '.json'), Path(approval['runtime_package']),
                       units / ('mixos-bootstrap-' + TASK + '.service'),
                       registry / ('qualification-use-' + PRIOR + '.json')]
            for target in targets:
                target.write_bytes(b'existing')
                with self.subTest(target=target), self.assertRaisesRegex(safe.Refused, 'Existing'):
                    install.install_environment(approval)
                self.assertEqual(target.read_bytes(), b'existing')
                target.unlink()
        self.assertEqual(list(registry.iterdir()), [])
        self.chown.assert_not_called()

    def test_loaded_or_unknown_unit_refused(self):
        self.process.return_value = SimpleNamespace(returncode=0, stdout='LoadState=loaded\n')
        with self.assertRaisesRegex(safe.Refused, 'Existing/active'):
            safe.unit_absent('fixture.service')
        self.process.return_value = SimpleNamespace(returncode=1, stdout='')
        with self.assertRaises(safe.Refused):
            safe.unit_absent('fixture.service')


class ArchiveAndLoaderSafety(unittest.TestCase):
    def hostile_tar(self, names, **metadata):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name in names:
                info = tarfile.TarInfo(name)
                info.mode = 0o444
                info.size = 1
                for key, value in metadata.items():
                    setattr(info, key, value)
                archive.addfile(info, io.BytesIO(b'x' * info.size))
        return stream.getvalue()

    def test_archive_rejects_duplicates_traversal_and_unsafe_names(self):
        for names in (['same', 'same'], ['../escape'], ['/absolute'], ['a/../escape'], ['a//b'],
                      ['./a'], ['C:/escape'], ['a\\b'], ['a\nUser=root'], ['a b']):
            with self.subTest(names=names), self.assertRaises(safe.Refused):
                safe.unpack_checked(self.hostile_tar(names))

    def test_archive_rejects_links_devices_fifo_sparse(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE,
                     tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind), self.assertRaises(safe.Refused):
                safe.unpack_checked(self.hostile_tar(['unsafe'], type=kind, linkname='target', size=0))

    def test_archive_rejects_noncanonical_metadata_and_extra_dirs(self):
        for values in ({'uid': 1000}, {'gid': 1000}, {'mode': 0o644}, {'mode': 0o4755},
                       {'mtime': 1}, {'uname': 'root'}, {'type': tarfile.DIRTYPE, 'mode': 0o555, 'size': 0}):
            with self.subTest(values=values), self.assertRaises(safe.Refused):
                safe.unpack_checked(self.hostile_tar(['unsafe'], **values))

    def test_archive_rejects_trailing_records_and_truncation(self):
        valid = safe.tar_bytes({'artifacts/candidate_evidence/source/foo.c': b'not executable code'})
        self.assertEqual(safe.unpack_checked(valid), {'artifacts/candidate_evidence/source/foo.c': b'not executable code'})
        for raw in (valid + b'hidden', valid + valid, valid[:-512], valid[:100]):
            with self.subTest(length=len(raw)), self.assertRaises(safe.Refused):
                safe.unpack_checked(raw)

    def test_strict_json_duplicate_nonfinite_and_root_type(self):
        for raw in (b'{"schema":1,"schema":1}', b'{"n":NaN}', b'{"n":Infinity}', b'[]'):
            with self.assertRaises(safe.Refused):
                safe.strict_json(raw)

    def test_companion_pin_verified_before_any_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = root / install.SCRIPT_NAME
            script.write_bytes(b'# synthetic installer path only')
            companion = root / install.PRIMITIVES_NAME
            companion.write_bytes(b'raise AssertionError("UNTRUSTED CODE EXECUTED")')
            with mock.patch.object(install, '__file__', str(script)):
                with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                    install._load_primitives('0' * 64)
                for value in (None, '', 'A' * 64):
                    if value is None:
                        continue  # library-local loading intentionally has no external CLI pin
                    with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                        install._load_primitives(value)

    def test_companion_hardlink_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = root / install.SCRIPT_NAME
            script.write_bytes(b'# synthetic')
            companion = root / install.PRIMITIVES_NAME
            companion.write_bytes(b'raise AssertionError("must not run")')
            os.link(companion, root / 'hardlink')
            with mock.patch.object(install, '__file__', str(script)), self.assertRaisesRegex(ValueError, 'single-link'):
                install._load_primitives('a' * 64)

    def test_root_loader_checks_both_scripts_before_loading(self):
        with mock.patch.object(install, '_protected_script', side_effect=ValueError('unprotected')) as check:
            with self.assertRaisesRegex(ValueError, 'unprotected'):
                install._load_primitives('a' * 64, protected=True)
        check.assert_called_once()

    def test_protected_script_requires_isolated_root_and_0444_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / install.SCRIPT_NAME
            path.write_bytes(b'# synthetic')
            with (mock.patch.object(sys, 'platform', 'linux'),
                  mock.patch.object(sys, 'flags', SimpleNamespace(isolated=0)),
                  mock.patch.object(os, 'geteuid', return_value=0, create=True)):
                with self.assertRaisesRegex(ValueError, 'isolated'):
                    install._protected_script(path)
            for uid, mode, links in ((1000, 0o444, 1), (0, 0o644, 1), (0, 0o444, 2)):
                with (mock.patch.object(sys, 'platform', 'linux'),
                      mock.patch.object(sys, 'flags', SimpleNamespace(isolated=1)),
                      mock.patch.object(os, 'geteuid', return_value=0, create=True),
                      mock.patch.object(Path, 'resolve', return_value=path),
                      mock.patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | mode,
                                                                                  st_uid=uid, st_gid=0, st_nlink=links))):
                    with self.subTest(uid=uid, mode=mode, links=links), self.assertRaisesRegex(ValueError, '0444'):
                        install._protected_script(path)

    def test_source_links_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'source'
            source.write_bytes(b'synthetic')
            hard = root / 'hard'
            os.link(source, hard)
            with self.assertRaisesRegex(safe.Refused, 'single-link'):
                safe.read_regular(source)
            hard.unlink()
            with mock.patch.object(Path, 'resolve', return_value=root / 'elsewhere'):
                with self.assertRaisesRegex(safe.Refused, 'Canonical'):
                    safe.read_regular(source)


class ProductionContract(unittest.TestCase):
    def test_unmodified_qualification_pins_and_code_closure_reused(self):
        package.check_policy_pins()
        self.assertEqual(len(safe.ARTIFACT_NAMES), 14)
        self.assertEqual(len(safe.RUNTIME_SHA256), 4)
        self.assertEqual(package.code_paths(), package.qualification.code_paths())
        self.assertEqual(safe.reviewed_source_sha256,
                         safe.sha(safe.read_regular(ROOT / 'tools' / install.PRIMITIVES_NAME)))
        self.assertNotEqual(install.ROUTE, qualify.ROUTE)
        self.assertNotEqual(install.SCHEMA, qualify.SCHEMA)

    def test_qualification_purpose_guard_is_unchanged(self):
        qualify.task_id(PRIOR)
        with self.assertRaisesRegex(qualify.Refused, 'qualify-reviewed-binary'):
            qualify.task_id(TASK)
        self.assertEqual(qualify.ROUTE, 'mixos-reviewed-bootloader-binary-evidence/v1')


if __name__ == '__main__':
    unittest.main()
