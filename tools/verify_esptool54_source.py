"""Validate packaged esptool code/stubs against pinned upstream source archive."""
import hashlib
from pathlib import Path
import tarfile
import zipfile
import json
root=Path(__file__).resolve().parents[1]
p=root/'.tools/flash-packages-5.4'
source=p/'esptool-5.4.0.tar.gz'
assert hashlib.sha256(source.read_bytes()).hexdigest()=='fd756598db0a26c9975fa18511b08687c54bf2ce7322ede80cf1f5117dad1f50'
wheel=p/'esptool-5.4.0-py3-none-any.whl'
with tarfile.open(source) as t, zipfile.ZipFile(wheel) as z:
    expected={n.name.split('/',1)[1]:t.extractfile(n).read() for n in t.getmembers()
              if n.isfile() and n.name.split('/',1)[-1].startswith('esptool/')}
    actual={n:z.read(n) for n in z.namelist() if n.startswith('esptool/') and not n.endswith('/')}
    assert actual==expected, (set(actual)^set(expected),[n for n in actual if actual[n]!=expected.get(n)])
    print(json.dumps({'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
                      'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                      'exact_package_files':len(actual),'status':'matches_official_source'}))
