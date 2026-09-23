"""Production ESP OTA receiver/transaction tests: no board, real SHA-256."""
import unittest
from _support import ROOT, host_run, posix_path, require_host_cc
from _ota_sdk_stubs import HEADERS

OUT = ROOT / 'build/host-ota-firmware'

class OtaFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = require_host_cc()
        for name, content in HEADERS.items():
            p = OUT / 'stubs' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
        cls.exe = OUT / 'ota_firmware_harness'
        main = ROOT / 'firmware/esp32s3/main'
        host_run([cc, '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-DMIX_OTA_HOST_TEST',
                  '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
                  '-Wno-deprecated-declarations', '-g', '-O2',
                  '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                  '-I'+posix_path(OUT/'stubs'), '-I'+posix_path(main),
                  posix_path(ROOT/'tests/test_ota_firmware_host.c'),
                  posix_path(main/'mix_ota.c'), posix_path(main/'mix_ota_tx.c'),
                  posix_path(main/'mix_protocol.c'), '-lcrypto', '-o', posix_path(cls.exe)])

    def scenario(self, name):
        self.assertIn('PASS '+name, host_run([posix_path(self.exe), name]))

for name in ('success', 'end-failure', 'flash-corruption', 'sha-mismatch',
             'select-failure', 'select-uncertain', 'journal-before-select',
             'journal-after-select', 'reboot-journal-set-failure', 'reboot-journal-commit-failure',
             'idempotent-conflict', 'link-loss', 'pending',
             'unknown-state', 'mark-failure', 'local-failure', 'host-absent',
             'protect-baseline', 'malformed', 'no-journal-verify',
             'release-requires-matched-hash', 'release-durability', 'round-trip',
             'late-data', 'legacy-late-end', 'legacy-end-failure', 'reset-receiving',
             'health-heartbeat-only', 'health-measurement-only', 'health-ack',
             'health-bad-elf', 'health-bad-size', 'health-bad-boot',
             'health-bad-token', 'health-bad-id', 'health-bad-slot', 'health-bad-request',
             'health-wrong-hash', 'health-link-lost', 'health-session-change',
             'health-old-boot', 'health-state-read-failure', 'health-reply-full',
             'health-queue-busy', 'health-interrupted-hash', 'health-link-loss-during-hash',
             'health-local-flap', 'health-host-absent-after-ack', 'health-journal-unavailable',
             'health-ack-reply-lost', 'health-zero-token', 'health-malformed-ack',
             'health-malformed-verify', 'health-legacy-session-change'):
    setattr(OtaFirmwareTests, 'test_'+name.replace('-', '_'), lambda self, n=name: self.scenario(n))

if __name__ == '__main__':
    unittest.main()
