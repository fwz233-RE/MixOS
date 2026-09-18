"""Explicit first-B operation gate; static evidence is not runtime assurance.

The root-protected external approval authenticates selected review/test records.
Their hashes establish integrity, not semantic correctness or authenticity by
self-attestation. This module never issues approval, runs a test or touches a
service/device. The old binary review retains its qualification-only purpose.
"""
from . import bootstrap_candidate as candidate
from . import ota_bootstrap as policy
from .bootloader_evidence import _json_object

ROUTE = policy.CANDIDATE_ROUTE
REVIEW_SCHEMA = 'mixos-candidate-confirmation-review/v1'
TEST_SCHEMA = 'mixos-candidate-confirmation-tests/v1'
RISK_ACCEPTANCE = dict(unmeasured_candidate_startup=True, possible_manual_recovery=True,
                       no_fault_injection=True, preserve_baseline_a=True)
CONTRACT = dict(protocol=2, required_features=0x17, request_bytes=72, response_bytes=192,
                verify_running_opcode=6, health_ack_opcode=8, challenge_flag=8,
                health_acked_flag=16, local_healthy_hold_ms=20000,
                ack_binds_elf_file_slot_size_boot_session=True,
                pending_verify_before_ack=True, mark_valid_and_recheck_required=True,
                link_loss_or_ota_session_change_revokes_ack=True,
                caps_identify_never_grant_health_proof=True,
                lost_ack_response_is_unknown_without_replay=True)
REQUIRED_SUITES = frozenset({'test_ota_firmware', 'test_ota_firmware_wire',
    'test_bootstrap_health_wire', 'test_ota_health_ack', 'test_ota_v2',
    'test_host_integration', 'test_bootstrap_fallback'})
HOST_CONTRACT_FILES = frozenset({'tools/ota_v2.py', 'tools/bootstrap_ota_on_pi.py',
    'tools/_mixlib/ota_bootstrap.py', 'tools/_mixlib/bootstrap_service.py'})


def same(a, b):
    return policy.encoded(a) == policy.encoded(b)


def object_bytes(files, name):
    raw = files.get(name)
    policy.require(type(raw) is bytes and raw, 'Missing immutable candidate artifact: ' + name)
    return _json_object(raw)


def validate(approval, files):
    """Return candidate identity and execution binding, before any live effects."""
    policy.require(approval.get('bootloader_evidence_mode') == ROUTE
                   and approval.get('kind') in ('install', 'boot-only'),
                   'Candidate route requires explicit install or independent boot-only')
    policy.require(same(approval.get('runtime_risk_acceptance'), RISK_ACCEPTANCE),
                   'Exact first-B startup/manual-recovery risk acceptance required')
    policy.require(approval.get('approved') is True, 'External operation approval required')
    artifacts = approval.get('artifacts', {})
    prefix = 'candidate_evidence/'
    data = {name[len(prefix):]: raw for name, raw in files.items() if name.startswith(prefix)}
    pins = {name[len(prefix):]: spec['sha256'] for name, spec in artifacts.items()
            if name.startswith(prefix)}
    review = object_bytes(files, 'candidate_review')
    review_hash = policy.sha(files['candidate_review'])
    facts = candidate.validate_candidate(data, expected_sha256=pins, target_slot='ota_1',
                                         confirmation_contract_sha256=review_hash)
    policy.require(facts.binding_sha256 == policy.digest(approval.get('candidate_evidence_sha256')),
                   'Candidate static evidence binding differs from operation approval')
    policy.require(files.get('candidate') == data['candidate_bin']
                   and files.get('bootloader') == data['old_bootloader'],
                   'Install candidate/old bootloader differs from candidate evidence')
    policy.require(review.get('schema') == REVIEW_SCHEMA
                   and review.get('candidate_sha256') == facts.candidate_sha256
                   and review.get('candidate_elf_sha256') == facts.candidate_elf_sha256
                   and same(review.get('contract'), CONTRACT)
                   and review.get('safe_receiver_reviewed') is True
                   and review.get('rollback_health_confirmation_reviewed') is True,
                   'Exact candidate-bound receiver and maintenance confirmation review required')
    source_hashes = {name[len('source/'):]: policy.sha(raw) for name, raw in data.items()
                     if name.startswith('source/')}
    policy.require(same(review.get('firmware_sources_sha256'), source_hashes),
                   'Confirmation review must bind every candidate source input')
    host_hashes = {name: approval.get('code_sha256', {}).get(name) for name in HOST_CONTRACT_FILES}
    for digest in host_hashes.values():
        policy.digest(digest)
    policy.require(same(review.get('host_code_sha256'), host_hashes),
                   'Confirmation review must bind the executing host protocol/observer')
    tests = object_bytes(files, 'confirmation_test_report')
    log = files.get('confirmation_test_log')
    policy.require(type(log) is bytes and log
                   and review.get('test_report_sha256') == policy.sha(files['confirmation_test_report'])
                   and tests.get('schema') == TEST_SCHEMA
                   and tests.get('exit_code') == 0 and type(tests.get('exit_code')) is int
                   and tests.get('inputs_stable') is True
                   and tests.get('log_sha256') == policy.sha(log)
                   and same(tests.get('firmware_sources_sha256'), source_hashes)
                   and same(tests.get('host_code_sha256'), host_hashes),
                   'Successful original test log and stable candidate/host input binding required')
    policy.require(type(tests.get('suites')) is list
                   and REQUIRED_SUITES <= set(tests['suites'])
                   and type(tests.get('tests_run')) is int and tests['tests_run'] > 0
                   and type(tests.get('skipped')) is int and tests['skipped'] == 0
                   and type(tests.get('test_sources_sha256')) is dict
                   and {name + '.py' for name in REQUIRED_SUITES}
                       <= set(tests['test_sources_sha256']),
                   'Confirmation test inventory must include real C, wire, host and recovery suites without skips')
    for digest in tests['test_sources_sha256'].values():
        policy.digest(digest)
    # These assertions check an externally reviewed record. They do NOT prove
    # execution semantics, physical timing, LCD output, or future rollback.
    return dict(sha256=facts.candidate_sha256, elf_sha256=facts.candidate_elf_sha256,
                bytes=facts.candidate_bytes), dict(route=ROUTE,
                candidate_evidence_sha256=facts.binding_sha256)


def validate_source(approval, files, source, execution_evidence):
    """Bind independent boot permission to the writer's immutable success record."""
    actual = policy.validate_execution_evidence(source.get('execution_evidence'))
    policy.require(actual == execution_evidence,
                   'Boot-only must retain the source installation evidence route')
    if actual is None:
        return
    policy.require(approval.get('bootloader_evidence_mode') == ROUTE
                   and source.get('kind') == 'verified-install'
                   and source.get('task_id') == approval.get('source_task')
                   and source.get('expected_sha256') == approval.get('expected_sha256')
                   and source.get('reset_sent') is False,
                   'Candidate boot approval differs from verified installation')
    image = policy.validate_app(files['candidate'], policy.sha(files['candidate']))
    policy.require(source.get('candidate') == image
                   and source.get('baseline') == policy.baseline_description(policy.Trust('0'*64, '0'*64)),
                   'Boot candidate or protected A differs from approved installation')
