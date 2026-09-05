"""One question, one answer.

yt-dlp's `*.info.json` sidecar was globbed, read and parsed at six call sites,
with three different sets of caught exceptions between them and not one
catching OSError — so an info.json that existed but could not be read took the
whole command down rather than degrading to "no metadata".

That is the same shape as the bug that had `is_pruned` and `slides_extracted`
answering one question in two places until they drifted. These tests hold the
consolidated helper to the union of what the six used to tolerate.
"""

import json


def test_missing_sidecar_is_empty(ls, lecture):
    d, _ = lecture()
    assert ls.lecture_info(d) == {}


def test_a_readable_sidecar_is_returned(ls, lecture):
    d, _ = lecture(info={"title": "Financial Reporting", "duration": 7080.0})
    assert ls.lecture_info(d)["title"] == "Financial Reporting"


def test_corrupt_json_is_empty(ls, lecture):
    d, _ = lecture()
    (d / "video.info.json").write_text("{not json at all")
    assert ls.lecture_info(d) == {}


def test_json_that_is_not_an_object_is_empty(ls, lecture):
    """A valid JSON array parses fine and then every caller's .get() raises."""
    d, _ = lecture()
    (d / "video.info.json").write_text("[1, 2, 3]")
    assert ls.lecture_info(d) == {}


def test_an_unreadable_sidecar_is_empty_not_an_exception(ls, lecture):
    """The case none of the six sites caught. A directory where the file
    should be raises OSError on read_text — previously fatal."""
    d, _ = lecture()
    (d / "video.info.json").mkdir()
    assert ls.lecture_info(d) == {}


def test_duration_survives_a_sidecar_that_has_none(ls, lecture):
    d, _ = lecture(info={"title": "x"})
    assert ls.lecture_duration(d) == 0.0


def test_duration_survives_a_non_numeric_duration(ls, lecture):
    d, _ = lecture(info={"duration": "not a number"})
    assert ls.lecture_duration(d) == 0.0


def test_duration_is_read_when_present(ls, lecture):
    d, _ = lecture(info={"duration": 7080.0})
    assert ls.lecture_duration(d) == 7080.0


def test_export_still_reads_the_sidecar(ls, cfg, lecture, vault):
    """The consolidation must not have cost the callers their metadata."""
    d, _ = lecture(notes="# Notes\n\nsomething at 12:58\n",
                   info={"title": "Financial Reporting and Analysis",
                         "duration": 7080.0,
                         "webpage_url": "https://example.invalid/Viewer.aspx?id=z"})

    r = ls.export_obsidian(cfg(), d, vault)

    written = (vault / r["note"]).read_text()
    assert "1:58:00" in written, "duration lost"
    assert "https://example.invalid/Viewer.aspx?id=z" in written, "url lost"


def test_a_corrupt_sidecar_does_not_stop_an_export(ls, cfg, lecture, vault):
    d, _ = lecture(notes="# Notes\n")
    (d / "video.info.json").write_text("{truncated")

    r = ls.export_obsidian(cfg(), d, vault)

    assert (vault / r["note"]).exists()


def test_is_lecture_dir_still_rejects_a_playlist_sidecar(ls, lecture):
    """is_lecture_dir deliberately does NOT use the helper: it scans every
    sidecar for a playlist marker rather than reading the first one's
    metadata, because a folder can hold both."""
    d, _ = lecture()
    (d / "video.info.json").write_text(json.dumps({"_type": "playlist"}))
    assert not ls.is_lecture_dir(d)

    (d / "lecture.info.json").write_text(json.dumps({"_type": "video"}))
    assert ls.is_lecture_dir(d)
