"""Production USB IO interleavings with fake CDC/queues; never opens hardware."""
import unittest
from _support import ROOT, host_run, posix_path, require_host_cc
from test_link_update import HEADERS

OUT = ROOT / 'build/host-link-io'


class LinkIOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = require_host_cc()
        for name, content in HEADERS.items():
            path = OUT / 'stubs' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        cls.exe = OUT / 'link_io_harness'
        main = ROOT / 'firmware/esp32s3/main'
        host_run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                  '-Wno-misleading-indentation', '-g', '-O1',
                  '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                  '-I' + posix_path(OUT / 'stubs'),
                  posix_path(ROOT / 'tests/test_link_io_host.c'),
                  posix_path(main / 'mix_protocol.c'),
                  posix_path(main / 'mix_terminal.c'), '-lm', '-o', posix_path(cls.exe)])

    def scenario(self, name):
        self.assertIn('PASS ' + name, host_run([posix_path(self.exe), name]))


for name in ('epoch-interleavings', 'delimiter-pressure', 'disconnect-every-split',
             'persistent-disconnect', 'double-disconnect', 'rx-reset-interleavings',
             'fragmented-recovery'):
    setattr(LinkIOTests, 'test_' + name.replace('-', '_'),
            lambda self, n=name: self.scenario(n))

if __name__ == '__main__':
    unittest.main()
