"""Optional local release/staging consistency; never connects to a device.

These checks answer a deployment question, not a code question: "is the package
that was last uploaded to the device built from the sources in this working
tree?" During any development work the honest answer is no, so running them by
default made the whole suite red for a reason unrelated to code quality.

They are therefore opt-in:

    MIXOS_CHECK_STAGING=1 python -m unittest tests.test_release_artifacts
    python tools/run_checks.py --staging
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import display_transport as transport

ENABLED = os.environ.get('MIXOS_CHECK_STAGING') not in (None, '', '0')
DISABLED_REASON = (
    'staging freshness is opt-in; set MIXOS_CHECK_STAGING=1 to compare the last '
    'uploaded package against the current working tree'
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(ENABLED, DISABLED_REASON)
class ReleaseArtifactTests(unittest.TestCase):
    def test_latest_staged_packages_match_current_artifacts(self):
        deploy = ROOT / 'build/deploy'
        aliases = {
            'font.ttf': ROOT / 'build/font/MiSans-Normal-gb2312.ttf',
            'font-manifest.json': ROOT / 'build/font/MiSans-Normal-gb2312.ttf.manifest.json',
            'new-app.bin': ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin',
            'firmware/esp32s3/build/mixos_esp32s3.bin': ROOT / 'build/esp32s3/previous-mixos_esp32s3.bin',
            'partition-table.bin': ROOT / 'firmware/esp32s3/build/partition_table/partition-table.bin',
            **transport.local_packages(ROOT),
            'image.bin': ROOT / 'build/keyboard/keebdeck_6r11c_default.raw.bin',
            'manifest.json': ROOT / 'build/keyboard/manifest.json',
            'flash_keyboard_on_pi.py': ROOT / 'tools/flash_keyboard_on_pi.py',
        }
        receipts = []
        for kind in ('display', 'keyboard'):
            found = sorted(deploy.glob(f'mixos-{kind}-*.json'))
            if not found:
                self.skipTest('Optional staged hardware release receipts are absent')
            receipts.append(found[-1])
        for path in receipts:
            receipt = json.loads(path.read_text())
            if receipt['job'].startswith('mixos-display-'):
                with self.subTest(receipt=path.name, artifact='display runtime manifest'):
                    self.assertIn('tools/display_transport.py', receipt['hashes'])
                    for name, (_, expected) in transport.PACKAGES.items():
                        self.assertEqual(receipt['hashes'].get(name), expected)
            for name, expected in receipt['hashes'].items():
                if name == 'launch.sh':
                    continue  # Generated launch script is verified remotely before installation.
                source = aliases.get(name, ROOT / name)
                with self.subTest(receipt=path.name, artifact=name):
                    # Not a code defect: the newest package that was actually
                    # uploaded predates the current tree. Stage again before
                    # claiming this release matches these sources
                    # (tools/deploy_display.py --stage, --stage --migrate for
                    # a device still on the single-application layout).
                    self.assertEqual(sha(source), expected, 'Staged package is stale; stage final sources again')

    def test_esp_build_report_matches_current_sources(self):
        path = ROOT / 'build/esp32s3/font-app-build.json'
        if not path.exists():
            self.skipTest('Optional ESP target build report is absent')
        report = json.loads(path.read_text())
        self.assertEqual(sha(ROOT / 'firmware/esp32s3/build/mixos_esp32s3.bin'), report['app']['sha256'])
        for source in report['sources']:
            # Reports created on Windows must be checkable from WSL too.
            name = source['path'].replace('\\', '/').rsplit('/', 1)[-1]
            data = (ROOT / 'firmware/esp32s3/main' / name).read_bytes()
            # Cross-platform checkout may normalize CRLF. Accept only an exact
            # recorded digest or its byte-for-byte LF/CRLF representation, never
            # arbitrary whitespace/content differences. Binary hashes stay strict.
            lf = data.replace(b'\r\n', b'\n')
            candidates = (data, lf, lf.replace(b'\n', b'\r\n'))
            self.assertIn(source['sha256'], {hashlib.sha256(value).hexdigest()
                                             for value in candidates})


if __name__ == '__main__':
    unittest.main()
