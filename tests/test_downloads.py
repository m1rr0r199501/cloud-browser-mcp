"""Download retrieval when the browser is remote.

Measured against Browser Use Cloud: Playwright's save_as reports success but
writes an empty file, because the cloud browser saved the download on its own
disk. The bytes then have to come from somewhere else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cbmcp.session import BrowserSession, _DownloadUnavailable, _safe_filename


async def test_data_url_download_is_decoded_locally():
    data, via = await BrowserSession._fetch_download_bytes(
        SimpleNamespace(), None, "data:text/csv,a%2Cb%0A1%2C2%0A"
    )
    assert data == b"a,b\n1,2\n"
    assert via == "data_url"


async def test_base64_data_url_download_is_decoded_locally():
    data, _ = await BrowserSession._fetch_download_bytes(
        SimpleNamespace(), None, "data:application/octet-stream;base64,AAEC"
    )
    assert data == b"\x00\x01\x02"


async def test_unretrievable_scheme_is_reported_not_faked():
    with pytest.raises(_DownloadUnavailable):
        await BrowserSession._fetch_download_bytes(
            SimpleNamespace(context=None), None, "filesystem:whatever"
        )


def test_download_filenames_cannot_escape_the_downloads_dir():
    assert _safe_filename("../../.ssh/authorized_keys") == "authorized_keys"
    assert _safe_filename("..\\..\\evil.bat") == "evil.bat"
    assert _safe_filename("") == "download"
    assert "/" not in _safe_filename("a/b/c.txt")
