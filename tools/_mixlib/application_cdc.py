"""Bootstrap application CDC adapter; no ROM, reset or flash operations.

The caller owns the maintenance locks and has stopped mixosd. Resolve the tty
selected by physical topology to its verified stable application identity;
never pass a tty number to the native transport or weaken its verifier.
"""
import os
from pathlib import Path

from serial_transport import SerialTransport


class ApplicationCdc(SerialTransport):
    def __init__(self, device, stable_device):
        node = Path(device).resolve(strict=True)
        if Path(stable_device).resolve(strict=True) != node:
            raise ValueError('Application tty and stable USB identity differ')
        expected = node.stat().st_rdev
        super().__init__(stable_device)  # live descriptor verification, DTR only
        try:
            if (Path(stable_device).resolve(strict=True) != node
                    or os.fstat(self.fd).st_rdev != expected):
                raise ValueError('Application CDC changed while opening')
        except BaseException:
            self.close()
            raise

    def fileno(self):
        return self.fd

    def flush(self):
        import termios
        termios.tcdrain(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
