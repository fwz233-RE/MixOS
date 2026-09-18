"""Application CDC sysfs identity tests; no character devices are opened."""
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linux'))
import serial_transport as transport


class ApplicationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.usb = Path(self.temp.name) / 'usb'
        self.interface = self.usb / 'interface'
        (self.interface / 'ttyACM0/device').mkdir(parents=True)
        for name, value in {'idVendor': '303a', 'idProduct': '80c3',
                            'product': 'TypixDeck UAC+CDC', 'manufacturer': 'TypixDeck',
                            'serial': 'TD0720'}.items():
            (self.usb / name).write_text(value, encoding='ascii')
        (self.interface / 'bInterfaceNumber').write_text('03', encoding='ascii')
        self.device = '/dev/serial/by-id/usb-TypixDeck_TypixDeck_UAC+CDC_TD0720-if03'
        self.path = mock.Mock()
        self.path.name = self.device.rsplit('/', 1)[-1]
        node = self.path.resolve.return_value
        node.name = 'ttyACM0'
        node.stat.return_value.st_mode = stat.S_IFCHR
        node.stat.return_value.st_rdev = 123

    def verify(self):
        with mock.patch.object(transport, 'Path', return_value=self.path):
            return transport.verify_application_cdc(self.device, sysfs=self.interface)

    def test_exact_application_vid_pid_and_descriptors_match(self):
        self.assertEqual(self.verify(), 123)

    def test_rom_or_other_product_with_copied_name_is_refused(self):
        for pid in ('0009', '1001', '4001'):
            with self.subTest(pid=pid):
                (self.usb / 'idProduct').write_text(pid, encoding='ascii')
                with self.assertRaisesRegex(ValueError, 'VID/PID'):
                    self.verify()

    def test_other_vendor_with_copied_name_is_refused(self):
        (self.usb / 'idVendor').write_text('ffff', encoding='ascii')
        with self.assertRaisesRegex(ValueError, 'VID/PID'):
            self.verify()

    def test_wrong_serial_or_interface_is_refused(self):
        (self.usb / 'serial').write_text('OTHER', encoding='ascii')
        with self.assertRaisesRegex(ValueError, 'descriptor identity'):
            self.verify()
        (self.usb / 'serial').write_text('TD0720', encoding='ascii')
        (self.interface / 'bInterfaceNumber').write_text('00', encoding='ascii')
        with self.assertRaisesRegex(ValueError, 'interface'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
