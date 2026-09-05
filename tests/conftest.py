"""Fixtures for the regression suite.

Two rules shape everything here.

Nothing touches `library/` or a real vault. `library/` is gitignored, so it
does not exist on a fresh clone and cannot be a fixture; every lecture, slide
and note below is synthesised. That also means a test can never damage the
user's own recordings while proving that the code doesn't.

Nothing needs Vision, ffmpeg, yt-dlp or a model server. OCR is stubbed, so the
tests are deterministic and run in about a second. The point is to pin the
decisions this code makes, not to re-test Apple's text recognition.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent

# The Panopto id used throughout. Any 36-char UUID shape will do; this one
# matches the lecture the incident reports in `git log` were written against.
VID = "dc340e97-6b24-4109-8030-b41400e842da"


@pytest.fixture(scope="session")
def ls():
    """lecturescrape.py, imported as a module.

    It is a script rather than a package, and `main()` is guarded, so loading
    it by path has no side effects.
    """
    spec = importlib.util.spec_from_file_location(
        "lecturescrape", REPO / "lecturescrape.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lecturescrape"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cfg(ls):
    """A Config with every field set, overridable per test."""
    def make(**overrides):
        base = dict(
            browser="chrome", sources=[], video_format="bv*[acodec=none]/b",
            audio_format="worst[acodec!=none]/ba/b", whisper_model="whisper",
            slide_threshold=0.15, scene_scan_fps=2.0, min_slide_gap=2.0,
            max_slides=60, ocr=True, slide_text_similarity=0.90,
            keep_video=False, backend="openai", vision=False, vision_slides=6,
            endpoint="http://localhost:1/v1", model="test-model",
            request_timeout=5.0, max_context_tokens=32768,
            reserve_output_tokens=3000, port=8420, obsidian_vault="",
            obsidian_folder="Lectures", autosync_check=20,
        )
        base.update(overrides)
        return ls.Config(**base)
    return make


# ------------------------------------------------------------------ images
#
# Frames whose local-contrast scores straddle BLANK_FRAME_CONTRAST. The exact
# scores are asserted in test_slides_blank.py rather than trusted here — if
# Pillow's rendering shifts, that test fails loudly instead of these silently
# becoming meaningless.

def blank_desktop() -> Image.Image:
    """A presenter's desktop between slides: flat, with a menu bar and dock."""
    im = Image.new("RGB", (1280, 720), (60, 62, 70))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1280, 24], fill=(30, 30, 34))
    d.rectangle([500, 660, 780, 700], fill=(90, 92, 100))
    return im


def pale_diagram() -> Image.Image:
    """Axes and a trend line in light grey. No text, so OCR finds nothing, and
    its local contrast sits *below* the blank threshold — a real slide the
    metric cannot defend. It is why frames are set aside rather than deleted.
    """
    im = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.line([(150, 600), (400, 300), (650, 450), (900, 200)],
           fill=(225, 225, 228), width=3)
    d.line([(150, 600), (1100, 600)], fill=(230, 230, 230), width=2)
    d.line([(150, 600), (150, 150)], fill=(230, 230, 230), width=2)
    return im


def dense_text() -> Image.Image:
    """A wall of small text — a LaTeX regression table, in spirit.

    This is the shape the old pixel-spread measure scored as *blanker* than a
    desktop, because dense small text averages to flat grey once downsampled.
    """
    im = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(im)
    row = ("Canadian Name & 5.63*** & 5.44*** & 3.56*** & 7.29*** "
           "& (0.65) & (0.67) & (0.93) & 10184 ")
    for i in range(40):
        d.text((15, 8 + i * 17), row * 2, fill=(0, 0, 0))
    return im


# ----------------------------------------------------------------- lectures

@pytest.fixture
def lecture(tmp_path):
    """A lecture directory shaped the way `sync` leaves one.

    Returns a factory: `lecture(slides={"slide_0001.jpg": dense_text()})`.
    """
    def make(vid=VID, module="BEF2014", title=None, slides=None,
             slides_json="auto", bundle=None, notes=None, transcript=None,
             spoken=None, video=False, info=None):
        title = title or f"24-03-2026 @ 13-32 - {module}_L1 - Financial Reporting"
        d = tmp_path / "library" / module / f"{title} [{vid}]"
        d.mkdir(parents=True)

        entries = []
        if slides:
            (d / "slides").mkdir(exist_ok=True)
            for i, (name, image) in enumerate(slides.items()):
                image.save(d / "slides" / name)
                entries.append({"file": f"slides/{name}", "time": (i + 1) * 10.0})

        if slides_json == "auto":
            if entries:
                (d / "slides.json").write_text(json.dumps(entries, indent=1))
        elif slides_json is not None:
            (d / "slides.json").write_text(slides_json)

        if bundle is not None:
            (d / "bundle.md").write_text(bundle)
        if notes is not None:
            (d / "notes.md").write_text(notes)
        if transcript is not None:
            (d / "transcript.md").write_text(transcript)
        if spoken is not None:
            # `verify` reads the spoken words from transcript.json, not from
            # bundle.md — a quotation is checked against what was *said*.
            (d / "transcript.json").write_text(json.dumps(
                [{"start": i * 10.0, "text": t} for i, t in enumerate(spoken)]))
        if video:
            (d / "video.mp4").write_bytes(b"\0" * 4096)
        if info is not None:
            (d / "video.info.json").write_text(json.dumps(info))
        return d, entries
    return make


@pytest.fixture
def vault(tmp_path):
    """An Obsidian vault: export_obsidian refuses anything without .obsidian/."""
    v = tmp_path / "vault"
    (v / ".obsidian").mkdir(parents=True)
    (v / "Lectures" / "BEF2014").mkdir(parents=True)
    return v


@pytest.fixture
def stub_ocr(ls, monkeypatch):
    """Make OCR deterministic and dependency-free.

    A frame reads as whatever `texts` maps its filename to, defaulting to
    nothing. Real Vision OCR is neither installed everywhere nor stable enough
    to assert against.
    """
    def apply(texts=None, available=True):
        texts = texts or {}
        monkeypatch.setattr(ls, "ocr_available", lambda: available)
        monkeypatch.setattr(
            ls, "ocr_image", lambda path: list(texts.get(Path(path).name, [])))
    return apply
