"""Checks for the AI stack: the vendored backend, its launcher, the model
staging tools, and the unit and policy files that run them on the device.

These run anywhere; nothing here needs the device, a model, or a network.
"""
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'linux/apps/translator/vendor'
SERVICE = ROOT / 'linux/apps/translator/service.py'

sys.path.insert(0, str(ROOT / 'tools'))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VendoredBackendTests(unittest.TestCase):
    """The copy is only worth anything if it is provably the upstream file."""

    def setUp(self):
        self.provenance = json.loads((VENDOR / 'PROVENANCE.json').read_text(encoding='utf-8'))

    def test_every_vendored_file_matches_its_recorded_digest(self):
        self.assertTrue(self.provenance['files'], 'PROVENANCE.json records no files')
        for name, record in self.provenance['files'].items():
            path = VENDOR / name
            self.assertTrue(path.is_file(), f'{name} is recorded but missing')
            data = path.read_bytes()
            self.assertEqual(len(data), record['bytes'], f'{name} length changed')
            self.assertEqual(hashlib.sha256(data).hexdigest(), record['sha256'],
                             f'{name} content changed; it is meant to be byte-exact')

    def test_provenance_pins_a_commit_and_a_license(self):
        self.assertRegex(self.provenance['commit'], r'^[0-9a-f]{40}$')
        self.assertEqual(self.provenance['license'], 'Apache-2.0')
        self.assertFalse(self.provenance['modified'])
        self.assertTrue((VENDOR / 'LICENSE').is_file())

    def test_the_license_is_recorded_in_sources(self):
        sources = (ROOT / 'docs/SOURCES.md').read_text(encoding='utf-8')
        self.assertIn(self.provenance['commit'], sources)
        self.assertIn('google-gemma/gemma-translator', sources)


class BackendLauncherTests(unittest.TestCase):
    """Every deviation from upstream lives in service.py, and is deliberate."""

    def setUp(self):
        self.text = SERVICE.read_text(encoding='utf-8')

    def test_binds_loopback_by_default(self):
        module = load('mixos_translator_service', SERVICE)
        self.assertEqual(module.DEFAULT_HOST, '127.0.0.1')
        self.assertEqual(module.DEFAULT_PORT, 3000)
        # Upstream binds every interface; ours must not do so silently.
        self.assertIn('WARNING', self.text)

    def test_does_not_prewarm_unless_asked(self):
        """Upstream loads the English speech models at startup. On a 4 GiB
        machine shared with a language model, the first request pays instead."""
        self.assertIn("'--prewarm'", self.text)
        prewarm = self.text.split("'--prewarm'", 1)[1].split('parser.add_argument', 1)[0]
        self.assertIn('default=None', prewarm)
        # The warm-up may only run when the flag asked for it.
        body = self.text.split('if args.prewarm:', 1)
        self.assertEqual(len(body), 2, 'the warm-up is not guarded by the flag')
        self.assertIn('get_stt_recognizer', body[1])

    def test_dependency_check_reports_instead_of_failing_per_request(self):
        module = load('mixos_translator_service3', SERVICE)
        self.assertEqual(module.main(['--check']), module.check_dependencies())

    def test_the_launcher_never_edits_the_vendored_file(self):
        for forbidden in ('write_text', 'open(VENDOR', 'replace(', 'sed'):
            self.assertNotIn(forbidden, self.text)


class StagingToolTests(unittest.TestCase):
    def setUp(self):
        self.stage = load('mixos_stage_models', ROOT / 'tools/stage_models.py')

    def test_every_declared_artifact_is_complete(self):
        self.assertTrue(self.stage.ARTIFACTS)
        for artifact in self.stage.ARTIFACTS:
            for key in ('name', 'repo', 'path', 'model_id', 'purpose'):
                self.assertIn(key, artifact)
                self.assertTrue(artifact[key])
            # A staged name becomes a filename on two machines; keep it plain.
            self.assertNotIn('/', artifact['name'])
            self.assertNotIn('..', artifact['name'])

    def test_url_is_built_from_the_endpoint_and_never_from_input(self):
        artifact = self.stage.ARTIFACTS[0]
        url = self.stage.url_for('https://example.invalid/', artifact)
        self.assertEqual(url, f"https://example.invalid/{artifact['repo']}"
                              f"/resolve/main/{artifact['path']}")

    def test_digest_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'blob'
            path.write_bytes(b'mixos' * 1000)
            self.assertEqual(self.stage.digest(path),
                             hashlib.sha256(b'mixos' * 1000).hexdigest())


class DeployToolTests(unittest.TestCase):
    def setUp(self):
        self.deploy = load('mixos_deploy_models', ROOT / 'tools/deploy_models.py')

    def test_refuses_a_manifest_that_does_not_match_the_staged_bytes(self):
        """A three-hour transfer of the wrong file is worse than no transfer."""
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'manifest.json'
            (Path(temporary) / 'model.bin').write_bytes(b'abc')
            manifest.write_text(json.dumps({'artifacts': [
                {'name': 'model.bin', 'bytes': 3, 'sha256': '0' * 64, 'model_id': 'x'}]}),
                encoding='utf-8')
            original, argv = self.deploy.MANIFEST, sys.argv
            try:
                self.deploy.MANIFEST = manifest
                sys.argv = ['deploy_models.py']
                with self.assertRaises(SystemExit) as caught:
                    self.deploy.main()
            finally:
                self.deploy.MANIFEST, sys.argv = original, argv
        # It must stop on the content mismatch, before ever opening a connection.
        self.assertIn('does not match the manifest', str(caught.exception))

    def test_refuses_when_the_staged_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'manifest.json'
            manifest.write_text(json.dumps({'artifacts': [
                {'name': 'model.bin', 'bytes': 3, 'sha256': '0' * 64, 'model_id': 'x'}]}),
                encoding='utf-8')
            original, argv = self.deploy.MANIFEST, sys.argv
            try:
                self.deploy.MANIFEST = manifest
                sys.argv = ['deploy_models.py']
                with self.assertRaises(SystemExit) as caught:
                    self.deploy.main()
            finally:
                self.deploy.MANIFEST, sys.argv = original, argv
        self.assertIn('missing', str(caught.exception).lower())

    def test_human_time_is_readable_at_every_scale(self):
        self.assertEqual(self.deploy.human_time(30), '30s')
        self.assertEqual(self.deploy.human_time(600), '10 min')
        self.assertEqual(self.deploy.human_time(12600), '3.5 h')

    def test_transfer_resumes_rather_than_restarting(self):
        text = (ROOT / 'tools/deploy_models.py').read_text(encoding='utf-8')
        self.assertIn('resuming at', text)
        # The remote length is re-checked before every append, because blindly
        # appending after a half-arrived block corrupts the file in place.
        self.assertIn('actual = remote_size(remote, destination)', text)
        # And the device does its own hashing before anything is imported.
        self.assertIn('sha256sum', text)


class DeviceUnitTests(unittest.TestCase):
    """The unit and policy files encode the measured limits of this machine."""

    def units(self):
        return {name: (ROOT / 'linux' / name).read_text(encoding='utf-8')
                for name in ('mixos-litertlm.service', 'mixos-aiserver.service')}

    def directives(self, text):
        """The directives systemd will act on, without the comments.

        Searching the raw text cannot tell a live setting from a comment
        describing one that was removed, and both files now explain in prose why
        they no longer set a memory ceiling. A test that reads those sentences as
        settings would fail on its own documentation.
        """
        found = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('['):
                continue
            key, _, value = line.partition('=')
            found.setdefault(key.strip(), []).append(value.strip())
        return found

    def test_neither_service_runs_as_root(self):
        for name, text in self.units().items():
            self.assertIn('User=pi', text, name)
            self.assertNotIn('User=root', text, name)
            self.assertIn('NoNewPrivileges=yes', text, name)

    def test_neither_service_declares_a_memory_ceiling(self):
        """The kernel schedules memory here, not a number in a unit file.

        Both units carried MemoryMax and OOMPolicy=stop until 2026-09-14. Two
        measurements retired them. They were inert - this kernel boots with
        `cgroup_disable=memory`, so there is no memory controller for a ceiling
        to be written into - and the machine does not need one: three complete
        translations peak at 2698 MiB of 4049 with 1350 MiB still available, no
        OOM kills and no restarts.
        """
        for name, text in self.units().items():
            live = self.directives(text)
            for setting in ('MemoryMax', 'MemorySwapMax', 'MemoryHigh',
                            'MemoryLow'):
                self.assertNotIn(setting, live, f'{name}: {setting}')

    def test_a_service_the_kernel_kills_comes_back(self):
        """With no ceiling, this restart is the entire safety net.

        Both halves are needed: Restart=on-failure asks for the restart, and
        OOMPolicy=continue permits it. The second is not redundant - this system
        defaults to DefaultOOMPolicy=stop, which takes the service down after an
        OOM kill and ignores Restart= entirely, leaving speech recognition absent
        until somebody power-cycles the device.
        """
        for name, text in self.units().items():
            live = self.directives(text)
            self.assertEqual(live.get('Restart'), ['on-failure'], name)
            self.assertEqual(live.get('OOMPolicy'), ['continue'], name)

    def test_neither_service_reaches_the_network(self):
        for name, text in self.units().items():
            self.assertIn('IPAddressAllow=localhost', text, name)
            self.assertIn('IPAddressDeny=any', text, name)
            # This network blocks the model host; a lookup is a stall, not an error.
            self.assertIn('HF_HUB_OFFLINE=1', text, name)

    def test_the_backend_is_launched_on_loopback(self):
        text = self.units()['mixos-aiserver.service']
        self.assertIn('service.py --host 127.0.0.1', text)

    def test_the_model_runtime_can_write_its_weight_cache(self):
        """ProtectHome=read-only over the model directory costs 32 s per start.

        litert-lm writes an XNNPack weight cache next to the model it imported,
        in /home/pi/.litert-lm/models. With that path read-only the cache could
        neither be saved nor read back, so every cold start repeated the engine
        initialisation while the person waited on the first translation.
        """
        text = self.units()['mixos-litertlm.service']
        paths = [line.split('=', 1)[1].split()
                 for line in text.splitlines() if line.startswith('ReadWritePaths=')]
        self.assertTrue(paths, 'no ReadWritePaths in the unit')
        writable = {entry for group in paths for entry in group}
        self.assertIn('/home/pi/.litert-lm', writable)
        # Still read-only everywhere else in the home directory.
        self.assertIn('ProtectHome=read-only', text)

    def test_the_polkit_rule_grants_only_what_the_settings_page_needs(self):
        text = (ROOT / 'linux/50-mixos-network.rules').read_text(encoding='utf-8')
        for action in ('wifi.scan', 'network-control', 'settings.modify.own'):
            self.assertIn(action, text)
        # modify.system would let the device rewrite the machine's networking.
        self.assertNotIn('settings.modify.system', text)
        self.assertIn('isInGroup("netdev")', text)
        # A system service has no seated session; requiring one rejects the
        # only caller this rule exists for.
        self.assertNotIn('subject.local', text.split('*/', 1)[1])
        self.assertNotIn('subject.active', text.split('*/', 1)[1])


if __name__ == '__main__':
    unittest.main()
