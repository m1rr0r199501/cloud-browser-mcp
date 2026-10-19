"""page_upload confinement.

The agent that picks upload paths reads untrusted pages, so a prompt injection
must not be able to send arbitrary local files (SSH keys, Keychain exports) to a
site. Uploads are limited to one directory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cbmcp.app import set_app
from cbmcp.errors import InvalidArgument
from cbmcp.tools.page_tools import _upload_paths


@pytest.fixture
def upload_root(settings):
    settings.ensure_dirs()
    set_app(SimpleNamespace(s=settings))
    yield settings.uploads_dir
    set_app(None)


def test_file_inside_the_upload_dir_is_allowed(upload_root):
    (upload_root / "doc.pdf").write_bytes(b"%PDF")
    assert _upload_paths(["doc.pdf"]) == [(upload_root / "doc.pdf").resolve()]
    assert _upload_paths([str(upload_root / "doc.pdf")]) == [(upload_root / "doc.pdf").resolve()]


def test_file_outside_the_upload_dir_is_refused(upload_root, tmp_path):
    secret = tmp_path / "id_ed25519"
    secret.write_text("PRIVATE KEY")
    with pytest.raises(InvalidArgument):
        _upload_paths([str(secret)])


def test_traversal_out_of_the_upload_dir_is_refused(upload_root, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    with pytest.raises(InvalidArgument):
        _upload_paths(["../../outside.txt"])


def test_missing_file_is_reported(upload_root):
    with pytest.raises(InvalidArgument):
        _upload_paths(["nope.bin"])
