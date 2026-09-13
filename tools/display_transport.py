"""Pinned display-only esptool runtime; no hardware operations on import."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import zipfile

VERSION = '5.4.0'
SOURCE_SHA256 = 'fd756598db0a26c9975fa18511b08687c54bf2ce7322ede80cf1f5117dad1f50'
WHEEL_SHA256 = '0a08e50b745eb33764365c4aa332f7ae9da0bf95ebda1f24d0adcd8e7cbac119'
STUB_SHA256 = '8816e0611701e8f7396a9fee9d1d33bc2021751bf24bda54560fb87c62de3f0b'
# The main wheel was built locally from the official source distribution.
# Dependencies below are publisher-provided pure Python wheels. Existing CM5
# system libraries supply cryptography/serial/bitstring/rich and are import-tested.
PACKAGES = {
    'esptool.whl': ('esptool-5.4.0-py3-none-any.whl', WHEEL_SHA256),
    'runtime/esp_pylib.whl': ('esp_pylib-1.1.5-py3-none-any.whl',
                            'be04824c2da8d0af3ae891f93d0e4059c14d5f2c3c828b19a1c12d916c6a4d74'),
    'runtime/click.whl': ('click-8.1.8-py3-none-any.whl',
                        '63c132bbbed01578a06712a2d1f497bb62d9c1c0d329b7903a866228027263b2'),
    'runtime/rich_click.whl': ('rich_click-1.9.9-py3-none-any.whl',
                             '365e7a9d0adb42e41ea832a0a12e02c44c079a26536dee688125eb9814f97274'),
}


def local_packages(root):
    return {name: root / '.tools/flash-packages-5.4' / filename
            for name, (filename, _) in PACKAGES.items()}


def verify_packages(files):
    for name, (_, expected) in PACKAGES.items():
        if hashlib.sha256(files[name].read_bytes()).hexdigest() != expected:
            raise ValueError('Display runtime package hash mismatch: ' + name)


def prepare(package, vendor):
    files = {name: package / name for name in PACKAGES}
    verify_packages(files)
    vendor.mkdir(mode=0o700, exist_ok=False)
    seen = set()
    for name in PACKAGES:
        with zipfile.ZipFile(files[name]) as archive:
            names = archive.namelist()
            if (len(names) != len(set(names)) or any(
                    item.startswith('/') or '\\' in item or '..' in Path(item).parts
                    or (item in seen and not item.endswith('/')) for item in names)):
                raise ValueError('Invalid/overlapping runtime package paths')
            archive.extractall(vendor)
            seen.update(names)
    stub = vendor / 'esptool/targets/stub_flasher/2/esp32s3.json'
    if hashlib.sha256(stub.read_bytes()).hexdigest() != STUB_SHA256:
        raise ValueError('Unexpected modern ESP32-S3 stub')
    config = vendor / 'esptool.cfg'
    with config.open('x') as out:
        out.write('[esptool]\n'); out.flush(); os.fsync(out.fileno())
    os.environ['ESPTOOL_CFGFILE'] = str(config)
    os.environ.pop('ESPRESSIF_IDE_WS', None)
    script = ('import sys; sys.path.insert(0, ' + repr(str(vendor)) + '); '
              'import esptool; from esptool.cmds import connect_esp; '
              'from pathlib import Path; import json; '
              's=json.loads((Path(esptool.__file__).parent / "targets/stub_flasher/2/esp32s3.json").read_text()); '
              'assert s["text"] and s["entry"]; print(esptool.__version__)')
    check = subprocess.run([sys.executable, '-I', '-B', '-c', script],
                           capture_output=True, text=True, timeout=20)
    if check.returncode or check.stdout.strip() != VERSION:
        raise RuntimeError('Isolated display runtime import failed: ' + check.stdout + check.stderr)
    return dict(os.environ, PYTHONPATH=str(vendor))
