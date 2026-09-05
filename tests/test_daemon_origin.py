"""The daemon must not answer a cross-origin request.

Incident (4b003a0): the handler sent `Access-Control-Allow-Origin: *`, so any
page you happened to be visiting could read your whole library — lecture list,
transcripts, notes — because the daemon listens on localhost and the browser
makes the request on the page's behalf. That was narrowed to the Chrome
extension's origin; the extension has since been removed, and with it the last
reason for the daemon to allow any cross-origin access at all. The viewer is
served from the same origin and needs none.

This is a source canary, not a behavioural test: it cannot prove the running
server is safe, only that nobody has quietly reintroduced the headers without
reading why they went. Verified by hand against a live daemon at the time of
removal — no Access-Control header for `https://evil.example` or for a
`chrome-extension://` origin.
"""

import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "lecturescrape.py"


def test_no_cors_headers_are_sent():
    text = SRC.read_text()
    assert "Access-Control" not in text, (
        "The daemon serves the viewer from its own origin and needs no CORS. "
        "If you are adding a cross-origin client, read commit 4b003a0 first: "
        "a wildcard here hands every site you visit your entire library.")


def test_no_wildcard_origin_anywhere():
    assert '"*"' not in SRC.read_text().split("def cmd_serve")[-1], (
        "a wildcard in the daemon is how the library leaked the first time")
