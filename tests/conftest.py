"""Test sandbox: runs before any test module imports the app."""
import os
import sys
import tempfile

os.environ["MAYHEM_TESTING"] = "1"
os.environ["MAYHEM_NO_SEND"] = "1"
os.environ.setdefault("MAYHEM_DATA_DIR", tempfile.mkdtemp(prefix="mayhem-test-"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
