"""Offline integration for explicit binary evidence; never device authorization."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import bootstrap_ota_on_pi as cli
from _mixlib import bootloader_evidence as evidence
from _mixlib import ota_bootstrap as policy
from test_bootloader_evidence import PATHS

class BinaryRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths=dict(PATHS)
        paths['bootloader']=paths.pop('old_bootloader')
        paths.update(recovery_flash=ROOT/'build/deploy/recovery-20260916-024325-readback-8MB.bin',
                     recovery_verification=ROOT/'build/deploy/recovery-20260916-024325-readback-verification.json',
                     recovery_boot=ROOT/'build/deploy/recovery-20260916-024325-boot-verification.json',
                     migration_receipt=ROOT/'build/deploy/mixos-display-20260913-110839.json')
        if not all(p.is_file() for p in paths.values()):
            raise unittest.SkipTest('archived original binary-review evidence unavailable')
        cls.files={name:path.read_bytes() for name,path in paths.items()}
    def test_explicit_qualify_route_validates_actual_old_binary_without_historical_config(self):
        with mock.patch.object(cli,'PiBackend') as backend, mock.patch.object(cli.subprocess,'run') as processes:
            trust=cli.recovery_trust(self.files,evidence_mode=evidence.ROUTE,task_kind='qualify')
        self.assertEqual(trust.boot_region_sha256,policy.sha(self.files['recovery_flash'][:policy.TABLE]))
        self.assertTrue(trust.rollback)
        self.assertNotIn('boot_config',self.files)
        backend.assert_not_called();processes.assert_not_called()
    def test_route_cannot_authorize_install_boot_only_or_unspecified_kind(self):
        for kind in ('install','boot-only',None):
            with self.subTest(kind=kind),self.assertRaisesRegex(policy.Refused,'only authorizes no-write'):
                cli.recovery_trust(self.files,evidence_mode=evidence.ROUTE,task_kind=kind)
    def test_default_route_still_refuses_missing_historical_provenance(self):
        with self.assertRaisesRegex(policy.Refused,'provenance missing'):
            cli.recovery_trust(self.files)
    def test_unknown_mode_has_no_fallback(self):
        with self.assertRaisesRegex(policy.Refused,'Unknown'):
            cli.recovery_trust(self.files,evidence_mode='auto',task_kind='qualify')
    def test_changed_reference_or_review_refused_before_backend(self):
        for name in ('reference_config','reference_bootloader','bounded-recheck.json'):
            files=dict(self.files);files[name]+=b'\n'
            with (self.subTest(name=name), mock.patch.object(cli,'PiBackend') as backend,
                  self.assertRaises(evidence.EvidenceRefused)):
                cli.recovery_trust(files,evidence_mode=evidence.ROUTE,task_kind='qualify')
            backend.assert_not_called()
    def test_unchanged_binary_does_not_excuse_changed_recovery_metadata(self):
        files=dict(self.files)
        report=json.loads(files['recovery_verification']);report['otadata_unchanged']=False
        files['recovery_verification']=json.dumps(report).encode()
        with self.assertRaisesRegex(policy.Refused,'comparison evidence'):
            cli.recovery_trust(files,evidence_mode=evidence.ROUTE,task_kind='qualify')
    def test_original_migration_still_required(self):
        files=dict(self.files);files['migration_receipt']+=b'\n'
        with self.assertRaisesRegex(policy.Refused,'Original reviewed migration'):
            cli.recovery_trust(files,evidence_mode=evidence.ROUTE,task_kind='qualify')

class BinaryRouteCLI(unittest.TestCase):
    def test_nonqualification_and_unknown_modes_refused_before_artifact_access(self):
        for kind, mode, message in (
                ('install', evidence.ROUTE, 'only authorizes no-write'),
                ('boot-only', evidence.ROUTE, 'only authorizes no-write'),
                ('boot-only', 'automatic-fallback', 'Unknown')):
            approval = json.dumps(dict(schema=1, approved=True, task_id='fixture-only',
                                       kind=kind, bootloader_evidence_mode=mode)).encode()
            with (self.subTest(kind=kind, mode=mode),
                  mock.patch.object(cli, 'local_bytes', return_value=approval) as read,
                  mock.patch.object(cli, 'PiBackend') as backend,
                  mock.patch.object(cli, 'verify_code') as verify,
                  self.assertRaisesRegex(policy.Refused, message)):
                cli.main(['--approval', '/fixture/approval.json', '--approval-sha256', 'a' * 64])
            self.assertEqual(read.call_count, 1)
            backend.assert_not_called(); verify.assert_not_called()


if __name__=='__main__':unittest.main()
