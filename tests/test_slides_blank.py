"""Blank-frame judgement must never be the last word on a file.

Two incidents:

d3ca0bf — the old measure was pixel spread on a 48x48 downsample, which is
inverted for the slides most worth keeping: dense small text averages to flat
grey, so the denser the slide the blanker it scored. Across a real library the
three closest to deletion were the densest present, a LaTeX regression table.

1eb2a7c — local contrast is better but still cannot separate a clean desktop
(0.14) from a pale line diagram (0.13). An adversarial review deleted a visible
diagram with OCR on. So: judge only after OCR has testified, never judge at all
without it, and set aside rather than delete.
"""

import json

from conftest import blank_desktop, dense_text, pale_diagram


def test_dense_text_scores_far_above_a_desktop(ls, tmp_path):
    """The inversion that the old metric got backwards, pinned as numbers.

    Under pixel spread the table scored *lower* than the desktop. Under local
    contrast it must score decisively higher.
    """
    dense_text().save(tmp_path / "dense.jpg")
    blank_desktop().save(tmp_path / "blank.jpg")

    from PIL import Image, ImageChops, ImageFilter, ImageStat

    def contrast(p):
        with Image.open(p) as im:
            g = im.convert("L").resize((512, 512))
            return ImageStat.Stat(ImageChops.difference(
                g, g.filter(ImageFilter.GaussianBlur(2)))).mean[0]

    dense, blank = contrast(tmp_path / "dense.jpg"), contrast(tmp_path / "blank.jpg")
    assert blank < ls.BLANK_FRAME_CONTRAST < dense
    assert dense > blank * 4, f"dense {dense:.2f} vs blank {blank:.2f}"

    assert not ls.is_blank_frame(tmp_path / "dense.jpg")
    assert ls.is_blank_frame(tmp_path / "blank.jpg")


def test_a_pale_diagram_is_not_defensible_by_contrast_alone(ls, tmp_path):
    """Documents the limit rather than pretending it away: this is a real
    slide that scores below the threshold. Every other test in this file
    exists because of it."""
    pale_diagram().save(tmp_path / "diagram.jpg")
    assert ls.is_blank_frame(tmp_path / "diagram.jpg")


def test_ocr_text_overrides_a_low_contrast_score(ls, cfg, lecture, stub_ocr):
    """Two signals must agree. Text present means keep, whatever the picture
    scores."""
    stub_ocr({"slide_0001.jpg": ["Slide with faint but real text"]})
    d, entries = lecture(slides={"slide_0001.jpg": pale_diagram()})

    kept = ls.annotate_slides(cfg(), d, entries)

    assert [s["file"] for s in kept] == ["slides/slide_0001.jpg"]
    assert (d / "slides" / "slide_0001.jpg").exists()


def test_a_blank_frame_is_set_aside_not_deleted(ls, cfg, lecture, stub_ocr):
    """The core guarantee. A misjudged frame stays on disk, because the video
    it came from is pruned minutes later and there is no other copy."""
    stub_ocr({"slide_0002.jpg": ["Real slide text here"]})
    d, entries = lecture(slides={"slide_0001.jpg": pale_diagram(),
                                 "slide_0002.jpg": dense_text()})

    kept = ls.annotate_slides(cfg(), d, entries)

    assert [s["file"] for s in kept] == ["slides/slide_0002.jpg"]
    assert not (d / "slides" / "slide_0001.jpg").exists()
    assert (d / "slides" / "dropped" / "slide_0001.jpg").exists(), \
        "a dropped frame must remain recoverable"


def test_without_ocr_nothing_is_dropped(ls, cfg, lecture, stub_ocr):
    """With Vision missing, contrast is the only voice — and it is not
    trustworthy alone, so it gets no vote."""
    stub_ocr(available=False)
    d, entries = lecture(slides={"slide_0001.jpg": pale_diagram(),
                                 "slide_0002.jpg": blank_desktop()})

    kept = ls.annotate_slides(cfg(), d, entries)

    assert len(kept) == 2
    assert (d / "slides" / "slide_0001.jpg").exists()
    assert (d / "slides" / "slide_0002.jpg").exists()


def test_ocr_disabled_in_config_drops_nothing(ls, cfg, lecture, stub_ocr):
    stub_ocr()  # available, but config says no
    d, entries = lecture(slides={"slide_0001.jpg": pale_diagram()})

    kept = ls.annotate_slides(cfg(ocr=False), d, entries)

    assert len(kept) == 1
    assert (d / "slides" / "slide_0001.jpg").exists()


def test_all_blank_lecture_records_that_it_has_no_slides(ls, cfg, lecture, stub_ocr):
    """The latch found by adversarial review.

    When every frame was judged blank the function returned before writing
    slides.json, leaving a stale file naming frames that no longer existed.
    That read downstream as "slides extracted" and authorised a prune to take
    the video — the only remaining copy of a lecture with nothing on disk.
    """
    stub_ocr()  # nothing reads as text
    d, entries = lecture(slides={"slide_0001.jpg": blank_desktop(),
                                 "slide_0002.jpg": blank_desktop()},
                         bundle="# transcript only", video=True)

    kept = ls.annotate_slides(cfg(), d, entries)

    assert kept == []
    assert json.loads((d / "slides.json").read_text()) == []
    assert not ls.slides_extracted(d)
    assert ls.prune_video(d) == 0, "an all-blank lecture must not authorise a prune"
    assert ls.find_video(d) is not None


def test_all_blank_lecture_does_not_crash_the_merge(ls, cfg, lecture, stub_ocr):
    """The build-merge step indexed slides[0] on a list the blank drop had
    just emptied."""
    stub_ocr()
    d, entries = lecture(slides={"slide_0001.jpg": blank_desktop()})
    assert ls.annotate_slides(cfg(), d, entries) == []


def test_already_annotated_slides_are_left_alone(ls, cfg, lecture, stub_ocr):
    """Re-running `process` must not re-judge frames it already kept."""
    stub_ocr()
    d, entries = lecture(slides={"slide_0001.jpg": pale_diagram()})
    for e in entries:
        e["text"] = ["previously read text"]

    kept = ls.annotate_slides(cfg(), d, entries)

    assert len(kept) == 1
    assert (d / "slides" / "slide_0001.jpg").exists()


def test_an_unreadable_frame_is_kept_not_dropped(ls, cfg, lecture, stub_ocr):
    """A frame the metric cannot open is not evidence of blankness, so it
    fails in the safe direction — kept, and left for something downstream to
    skip. export_obsidian already skips slide entries whose file is gone.
    """
    stub_ocr({"slide_0002.jpg": ["text"]})
    d, entries = lecture(slides={"slide_0002.jpg": dense_text()})
    entries.insert(0, {"file": "slides/gone.jpg", "time": 1.0})

    kept = ls.annotate_slides(cfg(), d, entries)

    assert "slides/gone.jpg" in [s["file"] for s in kept]
    assert "slides/slide_0002.jpg" in [s["file"] for s in kept]
