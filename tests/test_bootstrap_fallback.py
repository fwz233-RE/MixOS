"""Offline first-B fallback: exact A recovery is not installation success."""
import contextlib
import copy
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux'), str(ROOT / 'tests')]
import bootstrap_ota_on_pi as cli
import ota_esp
from _mixlib import ota_bootstrap as p
from _mixlib import bootstrap_service as service
import mixos_esp_update as native
from test_ota_v2 import fixture as link_fixture
from test_ota_bootstrap import fixture, app, FakeBackend, qualification, running
from test_bootstrap_service import FakeService


class ObservationTests(unittest.TestCase):
    def setup_link(self, slot=0, pending=False):
        _, device, data, clock = link_fixture(stay_pending=pending)
        device.running = device.boot = slot
        device.state = 1 if pending else 2
        device.flags = 0
        device.valid_at = 0
        link = ota_esp.Link(device, clock=clock, sleep=clock.sleep, random_sessions=True)
        candidate = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                         elf_sha256=bytes(range(32)).hex())
        baseline = dict(candidate)
        return link, device, clock, candidate, baseline

    def test_exact_a_observed_once_between_two_windows_is_not_b_success(self):
        link, device, clock, candidate, baseline = self.setup_link()
        original = device.handle
        queries = []
        def handle(frame):
            if frame.type == ota_esp.T.OTA_IDENTIFY:
                queries.append(clock())
            return original(frame)
        device.handle = handle
        result = cli.observe_boot_health(link, candidate, baseline)
        self.assertEqual(result['outcome'], 'baseline-restored')
        self.assertEqual(result['running']['identity_queries'], 1)
        self.assertEqual(len(queries), 1)
        self.assertGreaterEqual(queries[0], 20)
        self.assertGreaterEqual(clock(), 40)
        self.assertGreaterEqual(result['running']['observed_seconds'], 15)
        self.assertEqual(device.ops, [])  # no v2 write/verify on legacy A

    def test_exact_b_still_requires_valid_and_actual_file_measurement(self):
        link, device, _, candidate, baseline = self.setup_link(1)
        result = cli.observe_boot_health(link, candidate, baseline)
        self.assertEqual(result['outcome'], 'candidate-confirmed')
        self.assertTrue(result['running']['actual_file_verified'])
        self.assertTrue(result['running']['measurement']['actual_file_verified'])

    def test_wrong_a_elf_or_nonvalid_a_never_counts_as_recovery(self):
        for change in ('elf', 'state', 'no-baseline'):
            link, device, _, candidate, baseline = self.setup_link(pending=change == 'state')
            if change == 'elf': baseline['elf_sha256'] = 'a' * 64
            if change == 'no-baseline': baseline = None
            with self.subTest(change=change), self.assertRaisesRegex(p.Refused, 'protected A'):
                cli.observe_boot_health(link, candidate, baseline)
            self.assertEqual(len([f for f in device.writes if f.type == ota_esp.T.OTA_IDENTIFY]), 1)

    def test_wrong_b_or_missing_file_measurement_is_not_success(self):
        for change in ('elf', 'hash'):
            link, device, _, candidate, baseline = self.setup_link(1)
            if change == 'elf': candidate['elf_sha256'] = 'a' * 64
            else: device.verify_never_accepted = True
            with self.subTest(change=change), self.assertRaises((p.Refused, ota_esp.UpdateError)):
                cli.observe_boot_health(link, candidate, baseline)

    def test_no_heartbeat_never_queries_identity(self):
        link, device, _, candidate, baseline = self.setup_link()
        device.no_heartbeats = True
        with self.assertRaises((p.Refused, ota_esp.Timeout)):
            cli.observe_boot_health(link, candidate, baseline)
        self.assertFalse(any(f.type == ota_esp.T.OTA_IDENTIFY for f in device.writes))

    def test_disconnect_after_identity_never_retries(self):
        link, device, _, candidate, baseline = self.setup_link()
        original = device.handle
        def handle(frame):
            original(frame)
            if frame.type == ota_esp.T.OTA_IDENTIFY:
                device.no_heartbeats = True
        device.handle = handle
        with self.assertRaises((p.Refused, ota_esp.Timeout)):
            cli.observe_boot_health(link, candidate, baseline)
        self.assertEqual(len([f for f in device.writes if f.type == ota_esp.T.OTA_IDENTIFY]), 1)

    def test_missing_first_identity_response_is_unknown_without_second_query(self):
        link, device, _, candidate, baseline = self.setup_link()
        original = device.handle
        queries = []
        def handle(frame):
            if frame.type == ota_esp.T.OTA_IDENTIFY:
                queries.append(frame)
                if len(queries) == 1:
                    return  # Later requests would succeed: they must not be sent.
            return original(frame)
        device.handle = handle
        with self.assertRaises(ota_esp.Timeout):
            cli.observe_boot_health(link, candidate, baseline)
        self.assertEqual(len(queries), 1)
        self.assertEqual(device.ops, [])

    def test_epoch_change_never_combines_boots(self):
        link, _, _, candidate, baseline = self.setup_link()
        original = cli.identify_once
        def identify(*args):
            result = original(*args)
            link.epoch += 1
            return result
        with mock.patch.object(cli, 'identify_once', side_effect=identify), \
                self.assertRaisesRegex(p.Refused, 'epoch/session'):
            cli.observe_boot_health(link, candidate, baseline)

    def test_short_budget_stops_before_query(self):
        link, device, _, candidate, baseline = self.setup_link()
        with self.assertRaisesRegex(p.Refused, 'budget'):
            cli.observe_boot_health(link, candidate, baseline, timeout=35)
        self.assertFalse(any(f.type == ota_esp.T.OTA_IDENTIFY for f in device.writes))


class PolicyAndServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.registry = Path(temp.name)
        before, self.trust = fixture()
        backend = FakeBackend(before, self.trust)
        self.source = p.Journal(self.registry, 'install-fallback-test', 'install', 'a' * 64)
        self.verified = p.install(backend, self.source, self.trust, app(2), p.sha(app(2)),
                                  qualification(self.trust, before))
        self.stored = bytes(backend.memory)
        self.backend = FakeBackend(self.stored, self.trust)
        self.backend.observe_boot = mock.Mock(return_value=dict(outcome='baseline-restored',
            running=dict(running(self.trust), identity_queries=1,
                         pre_identity_heartbeat=dict(pings=4, observed_seconds=15.1))))
        self.job = p.Journal(self.registry, 'boot-fallback-test', 'boot-only', 'b' * 64)

    def boot(self):
        return p.boot_only(self.backend, self.job, self.source.path, self.verified,
                           self.verified['expected_sha256'])

    def test_fallback_is_durable_separate_failed_install_outcome_no_retry(self):
        result = self.boot()
        self.assertEqual(result['kind'], 'boot-only-fallback')
        self.assertFalse(result['candidate_confirmed'])
        self.assertFalse(result['rollback_mechanism_verified'])
        self.assertTrue((self.job.path / 'boot-fallback.json').is_file())
        self.assertFalse((self.job.path / 'boot-result.json').exists())
        self.assertEqual(self.backend.resets, 1)
        self.assertEqual(self.backend.writes, [])
        self.assertNotIn('enter', self.backend.events)
        self.assertEqual(p.validate_boot_fallback(result, self.verified, self.stored),
                         p.baseline_description(self.trust))
        another = p.Journal(self.registry, 'boot-fallback-again', 'boot-only', 'c' * 64)
        with self.assertRaises(FileExistsError):
            p.boot_only(self.backend, another, self.source.path, self.verified,
                        self.verified['expected_sha256'])
        self.assertEqual(self.backend.resets, 1)

    def test_baseline_mismatch_rejected_before_device_or_reset(self):
        for change in ('baseline', 'trust', 'sector', 'sequence'):
            bad = copy.deepcopy(self.verified)
            if change == 'baseline': bad['baseline']['sha256'] = 'a' * 64
            elif change == 'trust': bad['trust_binding'] = 'a' * 64
            elif change == 'sector': bad['plan']['active_sector'] = 1
            else: bad['plan']['old_seq'] += 2
            with self.subTest(change=change), self.assertRaises(p.Refused):
                p.boot_only(self.backend, self.job, self.source.path, bad, bad['expected_sha256'])
            self.assertEqual(self.backend.events, [])
            self.assertEqual(self.backend.resets, 0)

    def test_legacy_source_without_baseline_remains_candidate_only(self):
        del self.verified['baseline']
        result = self.boot()
        self.assertEqual(result['kind'], 'boot-only-result')
        self.backend.observe_boot.assert_not_called()

    def test_unknown_observation_or_bad_health_never_saves_recovery(self):
        self.backend.observe_boot.return_value['running']['state'] = 'pending-verify'
        with self.assertRaises(p.Refused): self.boot()
        self.assertFalse((self.job.path / 'boot-fallback.json').exists())
        self.assertEqual(self.backend.resets, 1)
        self.assertEqual(self.backend.writes, [])

    def owner(self):
        native.atomic_json(self.source.path / 'service.json', dict(schema=1, task_id=self.source.identifier,
            kind='install', original='active', state='held-stopped'))
        approval = dict(task_id=self.job.identifier, kind='boot-only', source_task=self.source.identifier,
            source_verified_sha256=p.sha((self.source.path / 'verified.json').read_bytes()),
            expected_sha256=self.verified['expected_sha256'])
        self.service = FakeService('inactive')
        return service.ServiceOwner(self.registry, approval, self.service)

    def baseline_pins(self):
        return mock.patch.multiple(p, BASELINE_SHA256=self.trust.baseline_sha256,
            BASELINE_BYTES=self.trust.baseline_bytes, BASELINE_ELF=self.trust.baseline_elf)

    def test_service_restores_after_exact_a_proof_without_marking_b_success(self):
        owner = self.owner()
        with self.baseline_pins(), owner:
            self.boot()
        self.assertEqual(self.service.current, 'active')
        self.assertEqual(native.strict_json(self.job.path / 'service.json')['state'], 'restored')
        self.assertFalse((self.job.path / 'boot-result.json').exists())
        self.service.current = 'inactive'
        self.service.events.clear()
        owner.recover()
        self.assertEqual(self.service.events, [])

    def test_cli_returns_failure_after_restoring_exact_a_without_second_reset(self):
        self.owner()  # source held-stopped record
        approval = dict(schema=1, approved=True, task_id='boot-cli-fallback', kind='boot-only',
            runtime_package='/unused', managed_service=True, artifacts={},
            reset_method=p.RESET_METHOD, allow_clear_force_download=True,
            source_task=self.source.identifier, expected_sha256=self.verified['expected_sha256'],
            source_verified_sha256=p.sha((self.source.path / 'verified.json').read_bytes()))
        self.backend.prepare = mock.Mock()
        def read(path, *args, **kwargs):
            return p.encoded(approval) if Path(path).name == 'approval.json' else p.encoded(self.verified)
        owner_class = service.ServiceOwner
        def own(registry, value):
            return owner_class(registry, value, self.service)
        with self.baseline_pins(), contextlib.ExitStack() as stack:
            for patch in (
                mock.patch.object(cli, 'sys', types.SimpleNamespace(platform='linux',
                    flags=types.SimpleNamespace(isolated=1))),
                mock.patch.object(cli.os, 'geteuid', return_value=1000, create=True),
                mock.patch.object(cli.shutil, 'which', return_value='/usr/bin/fuser'),
                mock.patch.object(cli, 'timeouts_enforced', return_value=True),
                mock.patch.object(cli, 'local_bytes', side_effect=read),
                mock.patch.object(cli, 'verify_code'),
                mock.patch.object(cli, 'verify_state_directory'),
                mock.patch.object(cli, 'verify_unit'),
                mock.patch.object(cli, 'check_service'),
                mock.patch.object(cli, 'STATE', self.registry),
                mock.patch.object(cli, 'PiBackend', return_value=self.backend),
                mock.patch.object(cli, 'ServiceOwner', side_effect=own),
                mock.patch.object(cli.native, 'DeviceLock', return_value=contextlib.nullcontext()),
                mock.patch.object(cli.native, 'termination_handler', return_value=contextlib.nullcontext()),
            ):
                stack.enter_context(patch)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(['--approval', '/offline/approval.json', '--approval-sha256', 'd' * 64,
                                 '--execute'])
        self.assertEqual(code, 2)
        self.assertIn('baseline-restored', output.getvalue())
        self.assertEqual(self.service.current, 'active')
        self.assertEqual(self.backend.resets, 1)
        self.assertEqual(self.backend.writes, [])
        self.assertFalse((self.registry / approval['task_id'] / 'boot-result.json').exists())

    def test_changed_readback_or_wrong_protected_a_never_restores(self):
        owner = self.owner()
        owner.__enter__()
        self.boot()
        # Synthetic A is intentionally different from the production pinned A.
        with self.assertRaisesRegex(p.Refused, 'protected A'): owner.recover()
        data = bytearray((self.job.path / 'boot-check-8MB.bin').read_bytes())
        data[p.APP0] ^= 1
        (self.job.path / 'boot-check-8MB.bin').write_bytes(data)
        with self.baseline_pins(), self.assertRaises(p.Refused): owner.recover()
        self.assertEqual(self.service.current, 'inactive')
        self.assertNotIn('restore:active', self.service.events)

    def test_bad_fallback_claims_cannot_restore_service(self):
        owner = self.owner()
        owner.__enter__()
        original = self.boot()
        for key, value in (('candidate_confirmed', True), ('rollback_mechanism_verified', True),
                           ('source_task', 'other-install-test'), ('flash_programming', True),
                           ('outcome', 'candidate-confirmed')):
            native.atomic_json(self.job.path / 'boot-fallback.json', dict(original, **{key: value}))
            with self.subTest(key=key), self.baseline_pins(), self.assertRaises(p.Refused):
                owner.recover()
        self.assertEqual(self.service.current, 'inactive')
        self.assertNotIn('restore:active', self.service.events)


if __name__ == '__main__':
    unittest.main()
