"""An expired Recap session must never look like success.

Incident (d35146c): a single lecture fails loudly when the session dies, but
Panopto's folder API answers an unauthenticated caller with an *empty list*.
yt-dlp exits 0 having listed nothing, so preflight passed, `sync` printed
"sync complete", and the weekly autosync would have done nothing every week
all term without ever saying so.

Every case here fakes subprocess.run: the point is to pin how each shape of
yt-dlp output is classified, not to talk to Panopto.
"""

import subprocess
import types

import pytest

FOLDER = ("https://recapexeter.cloud.panopto.eu/Panopto/Pages/Sessions/"
          "List.aspx?folderID=a6724dd2-a15b-414f-afd2-b41600bd9a8a")


@pytest.fixture
def fake_ytdlp(monkeypatch):
    def apply(returncode=0, stdout="", stderr=""):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr=stderr))
    return apply


def test_a_listed_lecture_passes(ls, cfg, fake_ytdlp):
    fake_ytdlp(stdout="dc340e97-6b24-4109-8030-b41400e842da\n")
    assert ls.preflight_auth(cfg(), FOLDER) is None


def test_exit_zero_with_an_empty_listing_is_a_failure(ls, cfg, fake_ytdlp):
    """The incident. Success and silence is the shape an expired session takes
    on a folder URL."""
    fake_ytdlp(returncode=0, stdout="")
    with pytest.raises(RuntimeError, match="listed no lectures"):
        ls.preflight_auth(cfg(), FOLDER)


def test_whitespace_only_output_counts_as_empty(ls, cfg, fake_ytdlp):
    fake_ytdlp(returncode=0, stdout="\n  \n")
    with pytest.raises(RuntimeError, match="listed no lectures"):
        ls.preflight_auth(cfg(), FOLDER)


def test_the_empty_listing_message_names_both_causes(ls, cfg, fake_ytdlp):
    """It is either an expired cookie or unenrolment, and the user needs to
    know which one to go and check."""
    fake_ytdlp(returncode=0, stdout="")
    with pytest.raises(RuntimeError) as e:
        ls.preflight_auth(cfg(browser="chrome"), FOLDER)
    msg = str(e.value)
    assert "expired" in msg and "unenrolled" in msg
    assert "chrome" in msg
    assert "recapexeter.cloud.panopto.eu" in msg


def test_a_rejected_session_says_log_back_in(ls, cfg, fake_ytdlp):
    fake_ytdlp(returncode=1,
               stderr="ERROR: This video is only available for registered users.")
    with pytest.raises(RuntimeError, match="rejected the session"):
        ls.preflight_auth(cfg(), FOLDER)


def test_a_mistyped_browser_is_named(ls, cfg, fake_ytdlp):
    """Previously swallowed: yt-dlp exited 2, preflight recognised neither
    pattern, and the run proceeded to fail once per lecture."""
    fake_ytdlp(returncode=2,
               stderr='yt-dlp: error: unsupported browser specified for cookies: "chrom".')
    with pytest.raises(RuntimeError, match="chrom"):
        ls.preflight_auth(cfg(browser="chrom"), FOLDER)


COOKIES_MISSING = ('ERROR: could not find chrome cookies database in '
                   '"/Users/x/Library/Application Support/Google/Chrome"')


def test_a_missing_cookie_database_points_at_the_browser_name(ls, cfg, fake_ytdlp, monkeypatch):
    """Pinned, not read from this machine: whether the folder is blocked
    depends on the Mac running the tests."""
    monkeypatch.setattr(ls, "browser_data_blocked", lambda browser: False)
    fake_ytdlp(returncode=1, stderr=COOKIES_MISSING)
    with pytest.raises(RuntimeError, match="browser name"):
        ls.preflight_auth(cfg(), FOLDER)


def test_the_macos_privacy_block_is_named_and_login_is_ruled_out(ls, cfg, fake_ytdlp, monkeypatch):
    """Incident: on macOS 27 the app said "usually an expired session. Log
    into Recap" while the user was logged in. yt-dlp's error is identical for
    a blocked folder and a missing one, so the folder is listed to tell them
    apart, and the message says a login will not help."""
    monkeypatch.setattr(ls, "browser_data_blocked", lambda browser: True)
    fake_ytdlp(returncode=1, stderr=COOKIES_MISSING)
    with pytest.raises(RuntimeError) as e:
        ls.preflight_auth(cfg(), FOLDER)
    assert "Files & Folders" in str(e.value)
    assert "will not help" in str(e.value)


def test_the_block_advice_names_this_python_under_launchd(ls, monkeypatch):
    """Incident, November 2026: the message said Full Disk Access. With that
    granted to the app the daemon was still refused. The switch that worked
    is the per-process one in Files & Folders, for the Python launchd
    started, and the advice has to name that Python."""
    monkeypatch.setattr(ls.os, "getppid", lambda: 1)
    msg = ls.browser_block_advice("chrome")
    assert "Files & Folders" in msg and "Google Chrome" in msg
    assert ls.sys.executable in msg
    assert "does not cover" in msg     # Full Disk Access is ruled out


def test_the_block_advice_names_the_terminal_otherwise(ls, monkeypatch):
    monkeypatch.setattr(ls.os, "getppid", lambda: 4242)
    msg = ls.browser_block_advice("chrome:Profile 1")
    assert "terminal" in msg and "Google Chrome" in msg
    assert ls.sys.executable not in msg


def test_browser_data_blocked_reads_eperm_as_the_privacy_block(ls, monkeypatch):
    def refuse(path):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(ls.os, "listdir", refuse)
    assert ls.browser_data_blocked("chrome")
    assert ls.browser_data_blocked("chrome:Profile 1")


def test_browser_data_blocked_treats_a_missing_folder_as_not_blocked(ls, monkeypatch):
    def missing(path):
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(ls.os, "listdir", missing)
    assert not ls.browser_data_blocked("chrome")


def test_browser_data_blocked_ignores_browsers_it_does_not_map(ls):
    assert not ls.browser_data_blocked("firefox")


def test_an_unrecognised_error_is_left_to_the_caller(ls, cfg, fake_ytdlp):
    """preflight only claims the failures it can explain; a network blip is
    the caller's to report in context."""
    fake_ytdlp(returncode=1, stderr="ERROR: unable to download: connection reset")
    assert ls.preflight_auth(cfg(), FOLDER) is None


def test_the_viewers_download_path_checks_the_session_first(ls, cfg, fake_ytdlp, monkeypatch):
    """fetch_url backs the viewer's Add and "Get video & slides" buttons. It
    skipped preflight_auth, so every failure there read "usually an expired
    session" whatever yt-dlp had actually said."""
    monkeypatch.setattr(ls, "browser_data_blocked", lambda browser: True)
    monkeypatch.setattr(ls, "find_by_id", lambda vid: None)
    downloads = []
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: downloads.append(a) or 0)
    fake_ytdlp(returncode=1, stderr=COOKIES_MISSING)

    lecture = ("https://recapexeter.cloud.panopto.eu/Panopto/Pages/Viewer.aspx"
               "?id=dc340e97-6b24-4109-8030-b41400e842da")
    with pytest.raises(RuntimeError, match="Files & Folders"):
        ls.fetch_url(cfg(), lecture)
    assert downloads == [], "nothing should be downloaded after a failed check"


def test_a_download_failure_does_not_claim_a_cause_it_cannot_know(ls, cfg, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(ls, "find_by_id", lambda vid: None)
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: 1)
    fake_ytdlp(stdout="dc340e97-6b24-4109-8030-b41400e842da\n")  # session fine
    lecture = ("https://recapexeter.cloud.panopto.eu/Panopto/Pages/Viewer.aspx"
               "?id=dc340e97-6b24-4109-8030-b41400e842da")
    with pytest.raises(RuntimeError) as e:
        ls.fetch_url(cfg(), lecture)
    assert "expired" not in str(e.value)
