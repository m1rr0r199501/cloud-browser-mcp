"""When page_click waits for a single-page-app navigation, and when it must not."""

from __future__ import annotations

from cbmcp.tools.page_tools import _navigates_in_place

HERE = "https://github.com/org/repo"


def link(href: str, target: str = "", download: bool = False) -> dict:
    return {"href": href, "target": target, "download": download}


def test_same_tab_link_to_another_page_waits():
    assert _navigates_in_place(link(HERE + "/issues"), HERE, "left", None)


def test_clicks_that_do_not_replace_the_page_do_not_wait():
    assert not _navigates_in_place(None, HERE, "left", None)  # a button, a checkbox
    assert not _navigates_in_place(link(HERE + "/x", target="_blank"), HERE, "left", None)
    assert not _navigates_in_place(link(HERE + "/file.zip", download=True), HERE, "left", None)
    assert not _navigates_in_place(link(HERE + "#readme"), HERE, "left", None)
    assert not _navigates_in_place(link("javascript:void(0)"), HERE, "left", None)
    assert not _navigates_in_place(link(HERE + "/x"), HERE, "middle", None)
    assert not _navigates_in_place(link(HERE + "/x"), HERE, "left", ["Control"])
