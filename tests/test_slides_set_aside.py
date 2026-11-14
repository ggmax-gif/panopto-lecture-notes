"""No step that thins the slides may delete one.

Incident (audit, November 2026): blank frames were already moved to
slides/dropped/, but three other steps unlinked what they removed: the
max_slides cap, the merge of frames whose OCR text matches, and the collapse
of frames less than min_slide_gap apart. The video is pruned as soon as the
slides exist, so each deleted frame had no other copy. Two real lectures sit
at exactly the 60-slide cap with their videos pruned.

All three are judgements that can be wrong. The cap drops by position alone,
and text overlap cannot see a diagram change: a supply curve before and after
it shifts carries the same labels.
"""

import json
import types

from conftest import dense_text


def frames(n):
    return {f"slide_{i:04d}.jpg": dense_text() for i in range(1, n + 1)}


def on_disk(d):
    return sorted(p.name for p in (d / "slides").rglob("*.jpg"))


def test_thinning_to_the_cap_keeps_every_frame_on_disk(ls, cfg, lecture, stub_ocr):
    names = list(frames(8))
    stub_ocr({n: [f"slide {i} " + " ".join(f"w{i}x{j}" for j in range(10))]
              for i, n in enumerate(names)})
    d, entries = lecture(slides=frames(8))

    kept = ls.annotate_slides(cfg(max_slides=3), d, entries)

    assert len(kept) == 3
    assert on_disk(d) == names, "a frame past the cap must stay recoverable"
    assert len(list((d / "slides" / "dropped").glob("*.jpg"))) == 5
    assert len(json.loads((d / "slides.json").read_text())) == 3


def test_frames_with_matching_text_are_merged_but_not_deleted(ls, cfg, lecture, stub_ocr):
    """Same labels, different picture: the case text overlap cannot judge."""
    labels = ["Supply and demand", "Price", "Quantity", "S", "D"]
    stub_ocr({"slide_0001.jpg": labels, "slide_0002.jpg": labels})
    d, entries = lecture(slides=frames(2))

    kept = ls.annotate_slides(cfg(), d, entries)

    assert [s["file"] for s in kept] == ["slides/slide_0002.jpg"]
    assert (d / "slides" / "dropped" / "slide_0001.jpg").exists()


def test_a_transition_burst_is_collapsed_but_not_deleted(ls, cfg, lecture, monkeypatch):
    """extract_slides keeps the last frame of a burst. ffmpeg is faked: it
    writes three frames, the first two 0.5s apart."""
    d, _ = lecture(video=True)

    def fake_ffmpeg(cmd, capture=False):
        out = d / "slides"
        for i in (1, 2, 3):
            dense_text().save(out / f"slide_{i:04d}.jpg")
        return types.SimpleNamespace(
            returncode=0, stderr="pts_time:10.0\npts_time:10.5\npts_time:60.0\n")

    monkeypatch.setattr(ls, "run", fake_ffmpeg)

    slides = ls.extract_slides(cfg(), d, d / "video.mp4")

    assert [s["file"] for s in slides] == ["slides/slide_0002.jpg",
                                           "slides/slide_0003.jpg"]
    assert (d / "slides" / "dropped" / "slide_0001.jpg").exists()
