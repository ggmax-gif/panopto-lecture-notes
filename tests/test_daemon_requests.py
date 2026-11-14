"""What the daemon refuses, tested against a running one.

Audit, November 2026. The daemon binds to loopback and sends no CORS header
(test_daemon_origin.py), which stops another site reading a reply. It did not
stop one sending a request:

- a cross-site POST with a text/plain body needs no preflight, and the body
  was parsed as JSON regardless, so any page could queue downloads and paid
  analysis runs;
- export was a GET that writes into the vault, which an image tag triggers;
- nothing checked the Host header, so a domain rebound to 127.0.0.1 was the
  same origin and could read the whole library.

Each server here listens on a port the OS picks and serves a library under
tmp_path.
"""

import http.client
import json
import threading

import pytest

from conftest import VID

LECTURE_URL = ("https://recapexeter.cloud.panopto.eu/Panopto/Pages/"
               f"Viewer.aspx?id={VID}")


@pytest.fixture
def daemon(ls, cfg, vault, tmp_path, monkeypatch):
    """A running daemon. Returns request(method, path, body, headers).

    Downloading is stubbed to fail: a job that gets as far as fetch_url was
    accepted, which is all these tests ask.
    """
    monkeypatch.setattr(ls, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(ls, "write_agents_file", lambda: None)

    def no_download(*a, **k):
        raise RuntimeError("stub: no downloads in tests")
    monkeypatch.setattr(ls, "fetch_url", no_download)

    server = ls.build_server(cfg(obsidian_vault=str(vault)), 0)
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    port = server.server_port

    def request(method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        data = body.encode() if isinstance(body, str) else body
        if data is not None and "Content-Length" not in (headers or {}):
            conn.putheader("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, raw, r

    request.port = port
    yield request
    server.shutdown()
    server.server_close()


JSON = {"Content-Type": "application/json"}


# ------------------------------------------------- the viewer still works

def test_the_viewers_own_requests_are_served(daemon, lecture):
    lecture(info={"title": "x"}, notes="# Notes\n")
    status, raw, _ = daemon("GET", "/api/library",
                            headers={"Sec-Fetch-Site": "same-origin"})
    assert status == 200 and json.loads(raw)["modules"]

    status, raw, _ = daemon("GET", "/", host=f"localhost:{daemon.port}")
    assert status == 200 and b"<html" in raw.lower()


def test_a_same_origin_post_queues_a_job(daemon):
    status, raw, _ = daemon(
        "POST", "/jobs", json.dumps({"url": LECTURE_URL}),
        {**JSON, "Origin": f"http://127.0.0.1:{daemon.port}",
         "Sec-Fetch-Site": "same-origin"})
    assert status == 202 and json.loads(raw)["state"] in ("queued", "running", "error")


# ------------------------------------------------------- what is refused

def test_a_text_plain_post_is_refused(daemon):
    """The cross-site form of the request: no preflight is sent for it."""
    status, raw, _ = daemon("POST", "/jobs", json.dumps({"url": LECTURE_URL}),
                            {"Content-Type": "text/plain;charset=UTF-8"})
    assert status == 403
    assert "application/json" in json.loads(raw)["error"]


def test_a_request_labelled_cross_site_is_refused(daemon):
    for method, path, body, headers in [
        ("GET", "/api/library", None, {}),
        ("POST", "/jobs", json.dumps({"url": LECTURE_URL}), JSON),
    ]:
        status, _, _ = daemon(method, path, body,
                              {**headers, "Sec-Fetch-Site": "cross-site"})
        assert status == 403, (method, path)


def test_a_foreign_origin_is_refused(daemon):
    status, _, _ = daemon("POST", "/jobs", json.dumps({"url": LECTURE_URL}),
                          {**JSON, "Origin": "https://evil.example"})
    assert status == 403


def test_a_rebound_domain_cannot_read_the_library(daemon, lecture):
    """DNS rebinding: the browser thinks evil.example is this daemon, so the
    request is same-origin and readable. Only the Host header gives it away."""
    lecture(info={"title": "x"})
    for path in ("/api/library", f"/api/lecture?id={VID}", "/"):
        status, raw, _ = daemon("GET", path, host=f"evil.example:{daemon.port}")
        assert status == 403, path
        assert b"modules" not in raw


# ----------------------------------------------------------------- export

def test_export_is_not_reachable_by_get(daemon, lecture, vault):
    lecture(info={"title": "x"}, notes="# Notes\n")
    before = sorted(vault.rglob("*.md"))
    status, _, _ = daemon("GET", f"/api/export?id={VID}")
    assert status == 404
    assert sorted(vault.rglob("*.md")) == before


def test_export_by_post_writes_the_note(daemon, lecture, vault):
    lecture(info={"title": "x"}, notes="# Notes\n\nsomething at 12:58\n")
    status, raw, _ = daemon("POST", "/api/export", json.dumps({"id": VID}), JSON)
    assert status == 200, raw
    assert (vault / json.loads(raw)["note"]).exists()


def test_export_refuses_a_notes_name_outside_the_lecture(daemon, lecture, vault, tmp_path):
    lecture(info={"title": "x"}, notes="# Notes\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET 4242")
    for name in (str(secret), "../../../secret.txt"):
        status, raw, _ = daemon("POST", "/api/export",
                                json.dumps({"id": VID, "notes": name}), JSON)
        assert status == 400 and "not a notes file" in json.loads(raw)["error"]
    assert not any("TOP SECRET" in p.read_text() for p in vault.rglob("*.md"))


# ------------------------------------------------------------ small ones

@pytest.mark.parametrize("length", ["-1", str((1 << 20) + 1), "abc"])
def test_a_bad_content_length_is_answered_not_waited_on(daemon, length):
    """A negative length made the handler read until the client hung up."""
    status, _, _ = daemon("POST", "/jobs", None, {**JSON, "Content-Length": length})
    assert status == 400


@pytest.mark.parametrize("header,want", [
    ("bytes=0-9", (0, 9)),
    ("bytes=90-", (90, 99)),
    ("bytes=-10", (90, 99)),        # the last ten bytes, not the first eleven
    ("bytes=-500", (0, 99)),
    ("bytes=50-9999", (50, 99)),
    ("bytes=200-", None),
    ("bytes=-0", None),
    ("", None),
    ("items=0-9", None),
])
def test_range_headers(ls, header, want):
    assert ls.parse_range(header, 100) == want


def test_a_suffix_range_serves_the_end_of_the_file(daemon, lecture):
    d, _ = lecture(info={"title": "x"})
    (d / "clip.bin").write_bytes(bytes(range(100)))
    rel = f"BEF2014/{d.name}/clip.bin".replace(" ", "%20")
    status, raw, r = daemon("GET", f"/media?path={rel}", headers={"Range": "bytes=-10"})
    assert status == 206
    assert raw == bytes(range(90, 100))
    assert r.getheader("Content-Range") == "bytes 90-99/100"
