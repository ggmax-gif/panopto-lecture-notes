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


def test_unreadable_cookies_are_named(ls, cfg, fake_ytdlp):
    fake_ytdlp(returncode=1, stderr="ERROR: could not find chrome cookies database")
    with pytest.raises(RuntimeError, match="[Cc]ookies"):
        ls.preflight_auth(cfg(), FOLDER)


def test_an_unrecognised_error_is_left_to_the_caller(ls, cfg, fake_ytdlp):
    """preflight only claims the failures it can explain; a network blip is
    the caller's to report in context."""
    fake_ytdlp(returncode=1, stderr="ERROR: unable to download: connection reset")
    assert ls.preflight_auth(cfg(), FOLDER) is None
