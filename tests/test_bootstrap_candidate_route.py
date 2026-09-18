"""Synthetic new-route gates; fixtures never grant real-device approval."""
import contextlib
import copy
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from test_bootstrap_candidate import fixture, sha, encoded
from _mixlib import bootstrap_candidate_route as route
from _mixlib import bootstrap_candidate as candidate
from _mixlib import ota_bootstrap as p
import bootstrap_ota_on_pi as cli


def route_fixture():
    evidence = fixture()
    host = {name: 'b' * 64 for name in route.HOST_CONTRACT_FILES}
    sources = {name[7:]: sha(raw) for name, raw in evidence.items() if name.startswith('source/')}
    log = b'SYNTHETIC TEST LOG ONLY\nRan 7 tests\nOK\n'
    tests = dict(schema=route.TEST_SCHEMA, exit_code=0, inputs_stable=True, log_sha256=sha(log),
                 firmware_sources_sha256=sources, host_code_sha256=host,
                 suites=sorted(route.REQUIRED_SUITES), tests_run=7, skipped=0,
                 test_sources_sha256={name+'.py': 'c'*64 for name in route.REQUIRED_SUITES})
    test_raw = encoded(tests)
    review = dict(schema=route.REVIEW_SCHEMA, candidate_sha256=sha(evidence['candidate_bin']),
                  candidate_elf_sha256=sha(evidence['candidate_elf']), contract=route.CONTRACT,
                  safe_receiver_reviewed=True, rollback_health_confirmation_reviewed=True,
                  firmware_sources_sha256=sources, host_code_sha256=host, test_report_sha256=sha(test_raw))
    files = {'candidate_evidence/'+name: data for name, data in evidence.items()}
    files.update(candidate=evidence['candidate_bin'], bootloader=evidence['old_bootloader'],
                 candidate_review=encoded(review), confirmation_test_report=test_raw, confirmation_test_log=log)
    approval = dict(schema=1, approved=True, task_id='new-candidate-fixture', kind='install',
                    bootloader_evidence_mode=route.ROUTE, code_sha256=host,
                    runtime_risk_acceptance=dict(route.RISK_ACCEPTANCE),
                    artifacts={name: dict(sha256=sha(raw), path='/fixture/'+name) for name, raw in files.items()})
    with mock.patch.object(candidate, '_validate_old'):
        facts = candidate.validate_candidate(evidence, expected_sha256={n:sha(d) for n,d in evidence.items()},
            target_slot='ota_1', confirmation_contract_sha256=sha(files['candidate_review']))
    approval['candidate_evidence_sha256'] = facts.binding_sha256
    return approval, files


class CandidateRouteTests(unittest.TestCase):
    def setUp(self):
        self.approval, self.files = route_fixture()
        patch = mock.patch.object(candidate, '_validate_old')
        patch.start()
        self.addCleanup(patch.stop)

    def validate(self):
        return route.validate(self.approval, self.files)

    def repin_review(self):
        evidence = {n[len('candidate_evidence/'):]:d for n,d in self.files.items()
                    if n.startswith('candidate_evidence/')}
        facts = candidate.validate_candidate(evidence, expected_sha256={n:sha(d) for n,d in evidence.items()},
            target_slot='ota_1', confirmation_contract_sha256=sha(self.files['candidate_review']))
        self.approval['candidate_evidence_sha256'] = facts.binding_sha256

    def test_bound_review_and_tests_allow_only_separate_candidate_route(self):
        with mock.patch.object(cli, 'PiBackend') as device, mock.patch.object(cli, 'ServiceOwner') as service:
            image, binding = self.validate()
        self.assertEqual(image['sha256'], sha(self.files['candidate']))
        self.assertEqual(binding, dict(route=route.ROUTE,
            candidate_evidence_sha256=self.approval['candidate_evidence_sha256']))
        device.assert_not_called(); service.assert_not_called()

    def test_risks_exact_bool_and_kind_cannot_be_defaulted(self):
        original = copy.deepcopy(self.approval)
        for value in (None, {}, dict(route.RISK_ACCEPTANCE, preserve_baseline_a=1),
                      dict(route.RISK_ACCEPTANCE, possible_manual_recovery=False)):
            self.approval = dict(original, runtime_risk_acceptance=value)
            with self.subTest(value=value), self.assertRaises(p.Refused): self.validate()
        for mode in ('historical-build', candidate.old_evidence.ROUTE, None):
            self.approval = dict(original, bootloader_evidence_mode=mode)
            with self.assertRaises(p.Refused): self.validate()
        self.approval = dict(original, kind='qualify')
        with self.assertRaises(p.Refused): self.validate()

    def test_old_review_booleans_cannot_replace_contract_or_test_evidence(self):
        self.files['candidate_review'] = encoded(dict(schema=1, safe_receiver_reviewed=True,
                                                    rollback_health_confirmation_reviewed=True))
        self.repin_review()
        with self.assertRaisesRegex(p.Refused, 'confirmation review'): self.validate()

    def test_rehashed_review_must_match_protocol_sources_and_host(self):
        original = self.files['candidate_review']
        for key, value in (('contract', dict(route.CONTRACT, health_ack_opcode=7)),
                           ('firmware_sources_sha256', {}), ('host_code_sha256', {}),
                           ('candidate_sha256', '0'*64), ('test_report_sha256', '0'*64)):
            review = json.loads(original); review[key] = value
            self.files['candidate_review'] = encoded(review)
            self.repin_review()
            with self.subTest(key=key), self.assertRaises(p.Refused): self.validate()

    def test_tests_cannot_skip_c_fail_or_substitute_inputs_or_log(self):
        original = self.files['confirmation_test_report']
        for key, value in (('exit_code', 1), ('exit_code', False), ('inputs_stable', False),
                           ('skipped', 1), ('suites', ['test_ota_v2']), ('tests_run', 0),
                           ('firmware_sources_sha256', {}), ('host_code_sha256', {}),
                           ('log_sha256', '0'*64), ('test_sources_sha256', {})):
            report = json.loads(original); report[key] = value
            self.files['confirmation_test_report'] = encoded(report)
            review = json.loads(self.files['candidate_review'])
            review['test_report_sha256'] = sha(self.files['confirmation_test_report'])
            self.files['candidate_review'] = encoded(review)
            self.repin_review()
            with self.subTest(key=key), self.assertRaises(p.Refused): self.validate()

    def test_substituted_image_loader_and_evidence_binding_refused(self):
        for name in ('candidate', 'bootloader'):
            original = self.files[name]; self.files[name] += b'changed'
            with self.subTest(name=name), self.assertRaises(p.Refused): self.validate()
            self.files[name] = original
        self.approval['candidate_evidence_sha256'] = 'f'*64
        with self.assertRaises(p.Refused): self.validate()

    def test_boot_source_must_retain_route_candidate_and_a(self):
        image, binding = self.validate()
        approval = dict(self.approval, kind='boot-only', source_task='writer-fixture', expected_sha256='d'*64)
        source = dict(kind='verified-install', task_id=approval['source_task'], reset_sent=False,
                      expected_sha256='d'*64, execution_evidence=binding, candidate=image,
                      baseline=p.baseline_description(p.Trust('0'*64, '0'*64)))
        route.validate_source(approval, self.files, source, binding)
        for bad in (None, dict(binding, candidate_evidence_sha256='e'*64)):
            with self.assertRaises(p.Refused): route.validate_source(approval, self.files, source, bad)
        for key, value in (('candidate', {}), ('baseline', {}), ('reset_sent', True)):
            with self.assertRaises(p.Refused):
                route.validate_source(approval, self.files, dict(source, **{key:value}), binding)

    def test_cli_invalid_candidate_route_has_no_service_or_device_effect(self):
        self.approval['runtime_risk_acceptance'] = {}
        approval_bytes = encoded(self.approval)
        def read(path, *args, **kwargs):
            if str(path).endswith('approval.json'): return approval_bytes
            return self.files[str(path).removeprefix('/fixture/')]
        with mock.patch.object(cli, 'local_bytes', side_effect=read), \
             mock.patch.object(cli, 'PiBackend') as device, \
             mock.patch.object(cli, 'ServiceOwner') as service, \
             self.assertRaisesRegex(p.Refused, 'risk acceptance'):
            cli.main(['--approval', '/fixture/approval.json', '--approval-sha256', 'a'*64])
        device.assert_not_called(); service.assert_not_called()


class ExecutionEvidenceTests(unittest.TestCase):
    def test_invalid_execution_binding_refused_before_backend_or_journal_effect(self):
        for bad in ({}, {'route': route.ROUTE}, dict(route='automatic', candidate_evidence_sha256='b'*64)):
            backend, journal = mock.Mock(), mock.Mock()
            with self.assertRaises(p.Refused):
                p.install(backend, journal, None, b'', 'a'*64, {}, execution_evidence=bad)
            backend.assert_not_called(); self.assertEqual(backend.mock_calls, [])
            self.assertEqual(journal.mock_calls, [])

    def test_boot_cannot_omit_route_to_bypass_candidate_approval(self):
        source = dict(execution_evidence=dict(route=route.ROUTE, candidate_evidence_sha256='b'*64))
        backend, journal = mock.Mock(), mock.Mock()
        with self.assertRaisesRegex(p.Refused, 'inherit'):
            p.boot_only(backend, journal, '/nonexistent/writer-fixture', source, 'a'*64)
        self.assertEqual(backend.mock_calls, [])
        self.assertEqual(journal.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
