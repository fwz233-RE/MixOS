"""Make the test package importable and its helpers available to pytest.

Both runners are supported on purpose:

    python -m pytest tests
    python -m unittest discover -s tests -v

conftest.py is only read by pytest, so the sys.path work it performs is also
done by tests/_support.py, which every test imports.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _support

# Referencing the module keeps the import meaningful to a linter and documents
# what it is for: importing it is what puts tools/ and linux/ on sys.path.
TEST_ROOT = _support.ROOT
