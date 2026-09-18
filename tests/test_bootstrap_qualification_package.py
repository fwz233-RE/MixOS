"""Offline qualification packaging tests; all outputs live in temporary dirs.

Synthetic fixture hashes/approvals below are explicitly mocked test inputs,
NEVER historical evidence or a real task approval. systemctl/chown are mocked;
no test calls SSH, touches /etc, creates production task state or opens devices.
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
import bootstrap_qualification_package as package
import bootstrap_qualification_install as install
import bootstrap_ota_on_pi as bootstrap
from _mixlib import bootloader_evidence

TASK = 'mixos-qualify-reviewed-binary-fixture-only'
REMOTE = '/opt/mixos-bootstrap-packages/' + TASK
STAGING = '/var/tmp/' + TASK


class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        # Windows read-only attributes must be removed for TemporaryDirectory
        # cleanup after the simulated 0444/0555 installation tests.
        self.addCleanup(self.make_writable)
        self.repo = self.temp / 'repo'
        real_paths = package.code_paths()
        for name, path in real_paths.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        for name in install.NATIVE_EXTRA:
            (self.repo / name).write_bytes((ROOT / name).read_bytes())
        self.sources = dict(schema=1, artifacts={}, runtime={})
        pins = {}
        for group, names in (('artifacts', install.ARTIFACT_NAMES), ('runtime', install.RUNTIME_SHA256)):
            for name in names:
                target = self.temp / 'source' / group / name
                target.parent.mkdir(parents=True, exist_ok=True)
                data = ('SYNTHETIC TEST INPUT ONLY: ' + group + '/' + name).encode()
                target.write_bytes(data)
                self.sources[group][name] = str(target)
                pins[name] = install.sha(data)
        self.stack.enter_context(mock.patch.object(package, 'ROOT', self.repo))
        self.stack.enter_context(mock.patch.object(package, 'check_policy_pins'))
        self.recovery = self.stack.enter_context(mock.patch.object(bootstrap, 'recovery_trust'))
        for name in ('ORIGINAL_SHA256', 'RECOVERY_SHA256', 'RUNTIME_SHA256'):
            mapping = getattr(install, name)
            self.stack.enter_context(mock.patch.dict(mapping, {key: pins[key] for key in mapping}, clear=True))
        self.process = self.stack.enter_context(mock.patch.object(install.subprocess, 'run'))
        self.chown = self.stack.enter_context(mock.patch.object(install.os, 'chown', create=True))
        self.backend = self.stack.enter_context(mock.patch.object(bootstrap, 'PiBackend'))
        self.args = dict(sources=self.sources, kind='qualify', evidence_mode=install.ROUTE,
                         identifier=TASK, remote_package=REMOTE)
        self.files, self.installer_bytes = package.snapshot(**self.args)
        self.approval = install.strict_json(self.files['approval.json'])
        self.approval_hash = install.sha(self.files['approval.json'])

    def make_writable(self):
        if self.temp.exists():
            for path in self.temp.rglob('*'):
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)

    def repack_manifest(self, files):
        manifest = install.strict_json(files['manifest.json'])
        manifest['files'] = install.file_records({n: d for n, d in files.items() if n != 'manifest.json'})
        files['manifest.json'] = install.encoded(manifest)
        return files

    def external_fixture(self):
        # No real evidence here; the fixture is confined to TemporaryDirectory.
        external = dict(self.approval, approved=True)
        path = self.temp / 'external-fixture.json'
        raw = json.dumps(external, indent=3).encode()  # preserved, not normalized
        path.write_bytes(raw)
        return path, install.sha(raw), raw

    def write_archive(self, files=None):
        raw = install.tar_bytes(self.files if files is None else files)
        path = self.temp / 'input.tar'
        path.write_bytes(raw)
        return path, install.sha(raw)

    def build(self, **extra):
        args = dict(self.args, output=self.temp / 'output', remote_staging=STAGING)
        args.update(extra)
        return package.build_package(**args)

    def test_draft_is_deterministic_complete_and_never_approved(self):
        result = self.build()
        self.assertFalse(result['approved'])
        self.assertFalse(result['final_approval_issued'])
        self.assertFalse(result['task_state_created'])
        self.assertEqual(result['code_files'], sorted(package.code_paths()))
        output = self.temp / 'output'
        self.assertEqual((output / 'package.tar').read_bytes(), install.tar_bytes(self.files))
        files, manifest, approval = install.preflight(output / 'package.tar', result['archive_sha256'], result['approval_sha256'])
        self.assertEqual(files, self.files)
        self.assertEqual(manifest['files'], install.file_records({n: d for n, d in files.items() if n != 'manifest.json'}))
        self.assertIs(approval['approved'], False)
        self.process.assert_not_called()
        self.chown.assert_not_called()
        self.backend.assert_not_called()
        self.recovery.assert_called_with(mock.ANY, evidence_mode=install.ROUTE, task_kind='qualify')

    def test_external_approval_is_consumed_byte_exact_without_issuance(self):
        path, digest, raw = self.external_fixture()
        result = self.build(approval_path=path, approval_sha256=digest)
        self.assertTrue(result['approved'])
        self.assertFalse(result['final_approval_issued'])
        self.assertEqual((self.temp / 'output/approval.external.json').read_bytes(), raw)
        self.assertEqual(result['approval_sha256'], digest)

    def test_external_approval_rejects_changed_binding_and_hash(self):
        path, digest, raw = self.external_fixture()
        with self.assertRaisesRegex(install.Refused, 'hash mismatch'):
            package.load_approval(path, '0' * 64, self.approval)
        for key, value in (('task_id', TASK + '-other'), ('kind', 'install'), ('approved', False),
                           ('managed_service', False), ('code_sha256', {})):
            with self.subTest(key=key):
                changed = install.strict_json(raw)
                changed[key] = value
                data = install.encoded(changed)
                path.write_bytes(data)
                with self.assertRaises(install.Refused):
                    package.load_approval(path, install.sha(data), self.approval)
        with self.assertRaises(install.Refused):
            package.load_approval(path, None, self.approval)

    def test_explicit_kind_route_and_purpose_are_mandatory(self):
        for change in ({'kind': 'install'}, {'kind': 'boot-only'}, {'kind': None},
                       {'evidence_mode': 'historical-build'}, {'evidence_mode': 'auto'},
                       {'identifier': 'qualify-a7-old-task'}):
            with self.subTest(change=change), self.assertRaises(install.Refused):
                package.snapshot(**dict(self.args, **change))

    def test_absolute_canonical_posix_paths_not_windows_shell_or_traversal(self):
        for value in ('D:\\package', 'relative/path', '/opt/../etc/task', '/opt//x', '/opt/x/',
                      '//opt/x', '/opt/./x', '/opt/x y', '/opt/x\nUser=root', '/opt/$HOME', '/opt/%i'):
            with self.subTest(value=value), self.assertRaises(install.Refused):
                install.posix_absolute(value)
        for value in ('/etc/' + TASK, '/opt/' + TASK, REMOTE + '-wrong'):
            with self.subTest(value=value), self.assertRaises(install.Refused):
                package.snapshot(**dict(self.args, remote_package=value))

    def test_no_local_overwrite_or_output_on_preflight_failure(self):
        output = self.temp / 'output'
        output.mkdir()
        marker = output / 'original'
        marker.write_bytes(b'keep')
        with self.assertRaisesRegex(install.Refused, 'Existing'):
            self.build()
        self.assertEqual(marker.read_bytes(), b'keep')
        with self.assertRaises(install.Refused):
            self.build(output=self.temp / 'bad', remote_package='/etc/' + TASK)
        self.assertFalse((self.temp / 'bad').exists())

    def test_source_maps_reject_candidate_missing_runtime_and_relative_path(self):
        for transform in ('extra', 'missing', 'runtime', 'relative'):
            changed = copy.deepcopy(self.sources)
            if transform == 'extra':
                changed['artifacts']['candidate'] = str(self.temp / 'candidate')
            elif transform == 'missing':
                del changed['artifacts']['reference_elf']
            elif transform == 'runtime':
                changed['runtime'] = {}
            else:
                changed['artifacts']['bootloader'] = 'relative.bin'
            with self.subTest(transform=transform), self.assertRaises(install.Refused):
                package.source_paths(install.encoded(changed))

    def test_snapshot_refuses_changed_original_even_if_sources_rehash_it(self):
        source = Path(self.sources['artifacts']['reference_config'])
        source.write_bytes(source.read_bytes() + b'changed')
        with self.assertRaisesRegex(install.Refused, 'Fixed original'):
            self.build()
        self.assertFalse((self.temp / 'output').exists())

    def test_snapshot_refuses_changed_runtime(self):
        source = Path(self.sources['runtime']['esptool.whl'])
        source.write_bytes(source.read_bytes() + b'changed')
        with self.assertRaisesRegex(install.Refused, 'runtime SHA256'):
            self.build()

    def test_new_linux_and_mixlib_files_are_included_and_pinned(self):
        for name in ('linux/additional.py', 'tools/_mixlib/additional.py'):
            (self.repo / name).write_bytes(b'# additional dependency\n')
        files, _ = package.snapshot(**self.args)
        approval = install.strict_json(files['approval.json'])
        for name in ('linux/additional.py', 'tools/_mixlib/additional.py'):
            self.assertEqual(approval['code_sha256'][name], install.sha(files[name]))

    def test_required_code_and_native_dependencies_cannot_disappear(self):
        (self.repo / 'linux/protocol.py').unlink()
        with self.assertRaisesRegex(install.Refused, 'dependency missing'):
            package.snapshot(**self.args)
        with mock.patch.object(package.native, 'RUNNER_FILES', ('linux/new-native-launcher',)):
            with self.assertRaises(install.Refused):
                package.code_paths()

    def test_dryrun_commands_contain_no_effects_or_start(self):
        self.build()
        text = (self.temp / 'output/commands.txt').read_text()
        actual = '\n'.join(line for line in text.splitlines() if not line.startswith('#'))
        self.assertIn('sha256sum --check --strict', actual)
        self.assertIn('/usr/bin/python3 -I -B', actual)
        for forbidden in ('--execute', '--install', 'sudo ', 'systemctl', 'ssh ', 'mkdir', 'taskstate'):
            self.assertNotIn(forbidden, actual)
        with self.assertRaisesRegex(install.Refused, 'separate'):
            package.dryrun_commands(self.approval, '0' * 64, '0' * 64, '0' * 64, REMOTE + '/stage')

    def test_installer_dryrun_never_writes_or_calls_systemctl(self):
        path, digest = self.write_archive()
        before = sorted(str(p) for p in self.temp.rglob('*'))
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(install.main(['--archive', str(path), '--archive-sha256', digest,
                                          '--approval-sha256', self.approval_hash]), 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result['dry_run'])
        self.assertFalse(result['approved'])
        self.assertFalse(result['host_preconditions_checked'])
        self.assertEqual(before, sorted(str(p) for p in self.temp.rglob('*')))
        self.process.assert_not_called()
        self.chown.assert_not_called()

    def test_outer_archive_hash_is_checked_before_tar_decode(self):
        path, _ = self.write_archive()
        with mock.patch.object(install, 'unpack_checked') as unpack, self.assertRaisesRegex(install.Refused, 'archive SHA256'):
            install.preflight(path, '0' * 64, self.approval_hash)
        unpack.assert_not_called()

    def test_exact_manifest_set_size_hash_and_mode(self):
        for transform in ('extra', 'missing', 'size', 'hash', 'mode'):
            files = dict(self.files)
            manifest = install.strict_json(files['manifest.json'])
            name = 'linux/protocol.py'
            if transform == 'extra':
                files['unexpected.py'] = b'evil'
            elif transform == 'missing':
                del files[name]
            else:
                manifest['files'][name][{'size': 'bytes', 'hash': 'sha256', 'mode': 'mode'}[transform]] = 1
                files['manifest.json'] = install.encoded(manifest)
            with self.subTest(transform=transform), self.assertRaises(install.Refused):
                install.validate_payload(files, self.approval_hash)

    def test_manifest_rehash_does_not_excuse_changed_original_or_code(self):
        for name, reason in (('artifacts/reference_elf', 'Artifact hash'), ('linux/protocol.py', 'Code hash')):
            files = dict(self.files)
            files[name] += b'changed'
            self.repack_manifest(files)
            with self.subTest(name=name), self.assertRaisesRegex(install.Refused, reason):
                install.validate_payload(files, self.approval_hash)
        files = dict(self.files)
        files['artifacts/reference_elf'] += b'changed'
        approval = copy.deepcopy(self.approval)
        approval['artifacts']['reference_elf']['sha256'] = install.sha(files['artifacts/reference_elf'])
        files['approval.json'] = install.encoded(approval)
        self.repack_manifest(files)
        with self.assertRaisesRegex(install.Refused, 'Fixed original'):
            install.validate_payload(files, install.sha(files['approval.json']))

    def test_rehashed_manifest_cannot_add_unapproved_dependency(self):
        files = dict(self.files)
        files['linux/evil.py'] = b'evil'
        self.repack_manifest(files)
        with self.assertRaisesRegex(install.Refused, 'Unexpected or missing'):
            install.validate_payload(files, self.approval_hash)

    def test_approval_rejects_default_route_install_extra_keys_and_missing_hashes(self):
        for key, value in (('kind', 'install'), ('bootloader_evidence_mode', 'historical-build'),
                           ('code_sha256', {}), ('schema', True), ('approved', 1),
                           ('candidate', '/tmp/fake')):
            files = dict(self.files)
            approval = copy.deepcopy(self.approval)
            approval[key] = value
            files['approval.json'] = install.encoded(approval)
            self.repack_manifest(files)
            with self.subTest(key=key), self.assertRaises(install.Refused):
                install.validate_payload(files, install.sha(files['approval.json']))

    def test_draft_cannot_install_and_has_no_side_effects(self):
        with mock.patch.object(install, 'install_environment') as environment, self.assertRaisesRegex(install.Refused, 'draft'):
            install.install(self.files, self.approval, self.approval_hash)
        environment.assert_not_called()
        self.process.assert_not_called()
        self.chown.assert_not_called()

    def test_unit_has_required_unprivileged_bounded_recovery_semantics(self):
        unit = install.unit_text(self.approval, self.approval_hash).decode()
        for text in ('Type=exec\n', 'User=pi\n', 'Group=dialout\n', '-I -B ', 'Restart=no\n',
                     'RuntimeMaxSec=1500s\n', 'KillMode=control-group\n', '--recover-service\n',
                     'WorkingDirectory=' + REMOTE + '\n'):
            self.assertIn(text, unit)
        self.assertIn('ExecStopPost=/usr/bin/python3', unit)
        self.assertEqual(unit.count('--execute'), 2)
        self.assertNotIn('[Install]', unit)
        self.assertNotIn('User=root', unit)

    def test_simulated_install_is_exclusive_root_owned_immutable_and_never_starts(self):
        path, digest, _ = self.external_fixture()
        files, _ = package.snapshot(**dict(self.args, approval_path=path, approval_sha256=digest))
        approval = install.strict_json(files['approval.json'])
        remote = self.temp / 'installed'
        units = self.temp / 'units'
        units.mkdir()
        unit = units / ('mixos-bootstrap-' + TASK + '.service')
        # The privileged environment and POSIX fsync/mode primitives are mocked
        # on Windows. All real writes remain beneath TemporaryDirectory.
        with (mock.patch.object(install, 'install_environment', return_value=(remote, unit)),
              mock.patch.object(install, 'unit_absent') as absent,
              mock.patch.object(install, 'sync_directory'),
              mock.patch.object(install, 'verify_installed') as verify,
              mock.patch.object(install.os, 'O_NOFOLLOW', getattr(os, 'O_NOFOLLOW', 0), create=True),
              mock.patch.object(install.os, 'fchmod', create=True) as fchmod):
            result = install.install(files, approval, digest)
            self.assertFalse(result['started'])
            self.assertFalse(result['task_state_created'])
            self.assertEqual({p.relative_to(remote).as_posix() for p in remote.rglob('*') if p.is_file()}, set(files))
            for name, data in files.items():
                self.assertEqual((remote / name).read_bytes(), data)
            self.assertEqual(unit.read_bytes(), install.unit_text(approval, digest))
            verify.assert_called_once_with(remote, files)
            absent.assert_called_once_with(unit.name)
            self.assertTrue(all(call.args[1:] == (0, 0) for call in self.chown.call_args_list))
            modes = [call.args[1] for call in fchmod.call_args_list]
            self.assertEqual(modes.count(0o444), len(files))
            self.assertEqual(modes.count(0o644), 1)
            with self.assertRaises(FileExistsError):
                install.install(files, approval, digest)
        self.process.assert_called_once_with(['/usr/bin/systemctl', 'daemon-reload'], check=True, timeout=30)
        self.assertFalse((self.temp / 'taskstate').exists())

    def test_approved_install_revalidates_before_creating_anything(self):
        path, digest, _ = self.external_fixture()
        files, _ = package.snapshot(**dict(self.args, approval_path=path, approval_sha256=digest))
        approval = install.strict_json(files['approval.json'])
        files['linux/protocol.py'] += b'changed after preflight'
        with mock.patch.object(install, 'install_environment') as environment, self.assertRaises(install.Refused):
            install.install(files, approval, digest)
        environment.assert_not_called()
        self.process.assert_not_called()
        self.chown.assert_not_called()

    def test_simulated_environment_rejects_spent_global_claims_without_state_creation(self):
        registry = self.temp / 'state'
        registry.mkdir()
        units = self.temp / 'unit-directory'
        units.mkdir()
        original_lstat = Path.lstat

        def state_info(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path == registry:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000)
            return info

        fake_pwd = SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000))
        fake_grp = SimpleNamespace(getgrnam=lambda _: SimpleNamespace(gr_gid=20, gr_mem=['pi']))
        with (mock.patch.object(install, 'STATE', registry), mock.patch.object(install, 'UNIT_DIRECTORY', units),
              mock.patch.object(install.sys, 'platform', 'linux'),
              mock.patch.object(install.sys, 'flags', SimpleNamespace(isolated=1)),
              mock.patch.object(install.os, 'geteuid', return_value=0, create=True),
              mock.patch.dict(sys.modules, pwd=fake_pwd, grp=fake_grp),
              mock.patch.object(install, 'protected_directory'),
              mock.patch.object(Path, 'lstat', autospec=True, side_effect=state_info),
              mock.patch.object(install, 'unit_absent') as absent):
            for category in ('reset-use', 'qualification-use'):
                claim = registry / (category + '-' + TASK + '.json')
                claim.write_bytes(b'prior claim')
                with self.subTest(category=category), self.assertRaisesRegex(install.Refused, 'Existing'):
                    install.install_environment(self.approval)
                claim.unlink()
            absent.assert_not_called()
        self.assertEqual(list(registry.iterdir()), [])
        self.chown.assert_not_called()
        self.process.assert_not_called()

    def test_postinstall_verifier_checks_exact_bytes_modes_and_ownership(self):
        root = self.temp / 'verify-root'
        (root / 'nested').mkdir(parents=True)
        payload = {'nested/file': b'original'}
        (root / 'nested/file').write_bytes(payload['nested/file'])
        original_lstat = Path.lstat
        fault = {}

        def root_info(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            result = SimpleNamespace(**{key: getattr(info, key) for key in (
                'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_dev', 'st_ino', 'st_mtime_ns')})
            if path == root or root in path.parents:
                result.st_uid = result.st_gid = 0
                result.st_mode = ((stat.S_IFDIR | 0o555) if stat.S_ISDIR(info.st_mode)
                                  else (stat.S_IFREG | 0o444))
                if path == root / 'nested/file':
                    for key, value in fault.items():
                        setattr(result, key, value)
            return result

        with mock.patch.object(Path, 'lstat', autospec=True, side_effect=root_info):
            install.verify_installed(root, payload)
            for changes in ({'st_uid': 1000}, {'st_gid': 20}, {'st_nlink': 2},
                            {'st_mode': stat.S_IFREG | 0o644}):
                fault.update(changes)
                with self.subTest(changes=changes), self.assertRaises(install.Refused):
                    install.verify_installed(root, payload)
                fault.clear()
            (root / 'nested/file').write_bytes(b'changed')
            with self.assertRaisesRegex(install.Refused, 'bytes differ'):
                install.verify_installed(root, payload)
            (root / 'nested/file').write_bytes(b'original')
            (root / 'extra').write_bytes(b'extra')
            with self.assertRaises(install.Refused):
                install.verify_installed(root, payload)

    def test_existing_and_loaded_units_are_refused(self):
        self.process.return_value = mock.Mock(returncode=0, stdout=(
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n'))
        with self.assertRaisesRegex(install.Refused, 'Existing/active'):
            install.unit_absent('fixture.service')
        self.process.return_value = mock.Mock(returncode=4, stdout=(
            'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nUnitFileState=\n'))
        install.unit_absent('fixture.service')
        self.process.return_value.stdout = ''
        with self.assertRaises(install.Refused):
            install.unit_absent('fixture.service')


class ArchiveSafety(unittest.TestCase):
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

    def test_duplicates_traversal_absolute_and_shell_names_refused(self):
        for names in (['same', 'same'], ['../escape'], ['/escape'], ['a/../escape'], ['a//b'],
                      ['./a'], ['C:/escape'], ['a\\b'], ['a\nUser=root'], ['a b']):
            with self.subTest(names=names), self.assertRaises(install.Refused):
                install.unpack_checked(self.hostile_tar(names))

    def test_symlink_hardlink_fifo_character_block_and_sparse_refused(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE,
                     tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind), self.assertRaises(install.Refused):
                install.unpack_checked(self.hostile_tar(['unsafe'], type=kind, linkname='target', size=0))

    def test_owner_mode_timestamp_and_empty_extra_directory_refused(self):
        for values in ({'uid': 1000}, {'gid': 1000}, {'mode': 0o644}, {'mode': 0o4755},
                       {'mtime': 1}, {'uname': 'root'}, {'type': tarfile.DIRTYPE, 'mode': 0o555, 'size': 0}):
            with self.subTest(values=values), self.assertRaises(install.Refused):
                install.unpack_checked(self.hostile_tar(['unsafe'], **values))

    def test_trailing_hidden_archive_and_truncation_refused(self):
        valid = install.tar_bytes({'a': b'a'})
        self.assertEqual(install.unpack_checked(valid), {'a': b'a'})
        for raw in (valid + b'hidden', valid + valid, valid[:-512], valid[:100]):
            with self.subTest(length=len(raw)), self.assertRaises(install.Refused):
                install.unpack_checked(raw)

    def test_duplicate_json_and_nonfinite_values_refused(self):
        for raw in (b'{"schema":1,"schema":1}', b'{"value":NaN}', b'{"value":Infinity}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(install.Refused):
                install.strict_json(raw)

    def test_source_hardlink_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'source'
            source.write_bytes(b'original')
            hard = root / 'hard'
            os.link(source, hard)
            with self.assertRaisesRegex(install.Refused, 'single-link'):
                install.read_regular(source)
            hard.unlink()
            symbolic = root / 'symbolic'
            try:
                symbolic.symlink_to(source)
            except OSError:
                # Windows symlink privilege is not required for the remaining
                # link defense: a mocked canonical-path mismatch checks it.
                with mock.patch.object(Path, 'resolve', return_value=root / 'elsewhere'):
                    with self.assertRaisesRegex(install.Refused, 'Canonical'):
                        install.read_regular(source)
            else:
                with self.assertRaisesRegex(install.Refused, 'Canonical'):
                    install.read_regular(symbolic)

    def test_protected_directory_requires_root_and_pi_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()
            resolved = mock.patch.object(Path, 'resolve', side_effect=lambda **_: path)
            # Ancestor lstat is mocked; no real protected/system directories read.
            for uid, mode in ((1000, 0o755), (0, 0o777), (0, 0o700)):
                with (resolved, mock.patch.object(Path, 'lstat', return_value=mock.Mock(
                        st_mode=stat.S_IFDIR | mode, st_uid=uid)), self.assertRaises(install.Refused)):
                    install.protected_directory(path)


class ProductionContract(unittest.TestCase):
    def test_independent_installer_pins_match_current_production(self):
        package.check_policy_pins()
        self.assertEqual(len(install.ORIGINAL_SHA256), 10)
        self.assertEqual(install.ARTIFACT_NAMES, {'bootloader' if n == 'old_bootloader' else n
                         for n in bootloader_evidence.ARTIFACT_SHA256} |
                         {'recovery_flash', 'recovery_verification', 'recovery_boot', 'migration_receipt'})

    def test_code_set_equals_production_verify_code_and_native_bundle(self):
        paths = package.code_paths()
        approval = {'code_sha256': {name: install.sha(path.read_bytes()) for name, path in paths.items()}}
        with mock.patch.object(bootstrap, 'local_bytes') as read:
            bootstrap.verify_code(approval)
        self.assertEqual({call.args[0] for call in read.call_args_list}, set(paths.values()))
        self.assertTrue(set(package.native.RUNNER_FILES) <= set(paths) | install.NATIVE_EXTRA)

    def test_real_originals_package_when_archived_inputs_are_available(self):
        # Read actual archived originals only; copy to temporary sources. Nothing
        # from historical scripts is imported/run, modified or re-approved.
        from test_bootloader_evidence import PATHS
        artifacts = {'bootloader' if n == 'old_bootloader' else n: p for n, p in PATHS.items()}
        artifacts.update(
            recovery_flash=ROOT / 'build/deploy/recovery-20260916-024325-readback-8MB.bin',
            recovery_verification=ROOT / 'build/deploy/recovery-20260916-024325-readback-verification.json',
            recovery_boot=ROOT / 'build/deploy/recovery-20260916-024325-boot-verification.json',
            migration_receipt=ROOT / 'build/deploy/mixos-display-20260913-110839.json')
        runtime = package.display_transport.local_packages(ROOT)
        if not all(path.is_file() for path in [*artifacts.values(), *runtime.values()]):
            self.skipTest('exact archived originals or pinned runtime wheels unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sources = dict(schema=1, artifacts={}, runtime={})
            for group, paths in (('artifacts', artifacts), ('runtime', runtime)):
                for name, path in paths.items():
                    copied = root / 'inputs' / group / name
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    copied.write_bytes(path.read_bytes())
                    sources[group][name] = str(copied)
            with mock.patch.object(bootstrap, 'PiBackend') as backend, mock.patch.object(install.subprocess, 'run') as run:
                result = package.build_package(sources=sources, output=root / 'out', kind='qualify',
                    evidence_mode=install.ROUTE, identifier=TASK, remote_package=REMOTE, remote_staging=STAGING)
                files, _, approval = install.preflight(root / 'out/package.tar', result['archive_sha256'], result['approval_sha256'])
            self.assertFalse(approval['approved'])
            self.assertFalse(result['final_approval_issued'])
            self.assertEqual(len(files['artifacts/recovery_flash']), 8 * 1024 * 1024)
            backend.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
