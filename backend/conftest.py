# conftest.py - project-wide pytest configuration for Sift backend.
# Ensures the project root is on sys.path for all tests.
# Redirects UPLOAD_DIR to a tmp_path so tests never write into the repo.
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _isolate_upload_dir(tmp_path, monkeypatch):
    """
    Redirect UPLOAD_DIR to a per-test temp directory so no test files
    are written into the repo uploads/ folder.
    Applies to every test automatically (autouse=True).
    """
    import main as _main_mod
    test_uploads = tmp_path / "uploads"
    test_uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_main_mod, "UPLOAD_DIR", test_uploads)
    yield test_uploads