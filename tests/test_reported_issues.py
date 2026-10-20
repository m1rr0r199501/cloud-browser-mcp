"""Issues reported from real use: error pages, throttling, dropped connections."""

from __future__ import annotations

from cbmcp.netlog import NetEntry, NetworkRecorder, _is_browser_error_page
from cbmcp.tools._common import _is_connection_drop

CHROME_403 = (
    b'<html><body class="neterror"><div id="main-frame-error">'
    b"HTTP ERROR 403</div></body></html>"
)


def _entry(status: int, headers: dict[str, str] | None = None) -> NetEntry:
    return NetEntry(
        id="n1",
        started_at=0.0,
        method="GET",
        url="https://example.com/",
        resource_type="document",
        status=status,
        response_headers=headers or {},
    )


def test_chrome_error_page_is_not_mistaken_for_the_body():
    """A 403 with an empty body showed up as a ~250 KB "response": Chrome's own page."""
    assert _is_browser_error_page(_entry(403), CHROME_403)
    assert _is_browser_error_page(_entry(403, {"content-length": "0"}), b"<html>x</html>")
    # A real error body from the server is kept.
    assert not _is_browser_error_page(_entry(403), b"<html>Access denied by WAF</html>")
    # Success responses are never second-guessed.
    assert not _is_browser_error_page(_entry(200), CHROME_403)


def _recorder(tmp_path) -> NetworkRecorder:
    return NetworkRecorder(
        lease_dir=tmp_path,
        max_entries=100,
        max_body_bytes=1024,
        disk_quota_bytes=1024 * 1024,
    )


def test_throttle_hint_fires_on_bulk_blocking(tmp_path):
    rec = _recorder(tmp_path)
    for status in [200] * 4 + [403] * 8:
        rec._recent_status.append(status)
    hint = rec.throttle_hint()
    assert hint is not None and "rate-limiting" in hint


def test_throttle_hint_quiet_for_normal_traffic(tmp_path):
    rec = _recorder(tmp_path)
    for status in [200, 404, 200, 403, 200, 200, 301, 200]:
        rec._recent_status.append(status)
    assert rec.throttle_hint() is None
    # Recovered: the last answer is fine again.
    for status in [403] * 6 + [200]:
        rec._recent_status.append(status)
    assert rec.throttle_hint() is None


def _playwright_error(message: str) -> Exception:
    cls = type("Error", (Exception,), {"__module__": "playwright._impl._errors"})
    return cls(message)


def test_connection_drops_are_recognised():
    assert _is_connection_drop(_playwright_error("Target page, context or browser has been closed"))
    assert _is_connection_drop(_playwright_error("Connection closed while reading from the driver"))
    assert not _is_connection_drop(_playwright_error("Timeout 15000ms exceeded"))
    # Only Playwright's errors: our own messages may contain the same words.
    assert not _is_connection_drop(RuntimeError("connection closed"))
