"""Linux CDC transport with no daemon imports and no reset-line sequence.

open_serial's default preserves mixosd's historical behavior. OTA explicitly
requests DTR only after sysfs verifies the application USB interface. RTS is
never changed; this is not a ROM/bootstrap transport.
"""
import os
from pathlib import Path, PurePosixPath
import stat
import struct


APP_USB_PRODUCT = 'TypixDeck UAC+CDC'
APP_USB_MANUFACTURER = 'TypixDeck'
APP_USB_INTERFACE = '03'


def validate_device_path(device):
    path = PurePosixPath(device)
    if '\\' in str(device) or str(path.parent) != '/dev/serial/by-id' or not path.name or path.name in ('.', '..'):
        raise ValueError('use one explicit /dev/serial/by-id/ identity, not a tty number')
    if not path.name.startswith('usb-TypixDeck_TypixDeck_UAC+CDC_') or not path.name.endswith('-if03'):
        raise ValueError('only a TypixDeck application CDC interface is allowed')
    return path.name


def verify_application_cdc(device, sysfs=Path('/sys/class/tty')):
    """Check the live USB descriptor chain, not just a possibly stale symlink."""
    validate_device_path(device)
    node = Path(device).resolve(strict=True)
    if not stat.S_ISCHR(node.stat().st_mode) or not node.name.startswith('ttyACM'):
        raise ValueError('device is not an application CDC character device')
    chain = (sysfs / node.name / 'device').resolve(strict=True)
    interface = None
    usb = None
    for ancestor in (chain, *chain.parents):
        if interface is None and (ancestor / 'bInterfaceNumber').is_file():
            interface = (ancestor / 'bInterfaceNumber').read_text().strip()
        if (ancestor / 'idVendor').is_file():
            usb = ancestor
            break
    if usb is None or interface != APP_USB_INTERFACE:
        raise ValueError('application USB interface could not be verified')
    vendor_id = (usb / 'idVendor').read_text().strip().lower()
    product_id = (usb / 'idProduct').read_text().strip().lower()
    if (vendor_id, product_id) != ('303a', '80c3'):
        raise ValueError('USB VID/PID is not the verified ESP32-S3 application interface')
    product = (usb / 'product').read_text().strip()
    manufacturer = (usb / 'manufacturer').read_text().strip()
    serial = (usb / 'serial').read_text().strip()
    if (product != APP_USB_PRODUCT or manufacturer != APP_USB_MANUFACTURER or
            not serial or not Path(device).name.endswith('_' + serial + '-if03')):
        raise ValueError('USB descriptor identity differs from the selected application device')
    return node.stat().st_rdev


def open_serial(device, assert_dtr=False, verifier=verify_application_cdc):
    import fcntl
    import termios
    import tty
    expected = verifier(device) if assert_dtr else None
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if expected is not None and os.fstat(fd).st_rdev != expected:
            raise ValueError('USB device changed while opening it')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~(termios.HUPCL | getattr(termios, 'CRTSCTS', 0))
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        if assert_dtr:
            # TIOCMBIS changes only the named bit: no RTS manipulation/reset.
            fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack('I', termios.TIOCM_DTR))
        return fd
    except BaseException:
        os.close(fd)
        raise


class SerialTransport:
    def __init__(self, device):
        self.fd = open_serial(device, assert_dtr=True)

    def read(self, limit):
        try:
            data = os.read(self.fd, limit)
        except (BlockingIOError, InterruptedError):
            return b''
        if not data:
            raise OSError('CDC disconnected')
        return data

    def write(self, data):
        try:
            return os.write(self.fd, data)
        except (BlockingIOError, InterruptedError):
            return 0

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
