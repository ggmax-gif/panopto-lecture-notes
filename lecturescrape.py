#!/usr/bin/env python3
"""
lecturescrape — pull Panopto (Moodle/Recap) lecture recordings and turn them
into bundles an AI model can actually read: timestamped transcript + slide
keyframes, interleaved.

    ./lecturescrape.py sync              # download new recordings
    ./lecturescrape.py process           # transcribe + extract slides
    ./lecturescrape.py analyse <slug>    # send a bundle to a model
    ./lecturescrape.py status            # what's in the library

Auth is the only fiddly part: Panopto is behind SSO, so yt-dlp borrows your
browser cookies. See README.md.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
LIBRARY = ROOT / "library"
ARCHIVE = LIBRARY / ".download-archive.txt"

VIDEO_NAME = "video"
SLIDE_DIR = "slides"


# ---------------------------------------------------------------- config


@dataclass
class Config:
    browser: str
    sources: list[dict]
    video_format: str
    audio_format: str
    whisper_model: str
    slide_threshold: float
    scene_scan_fps: float
    min_slide_gap: float
    max_slides: int
    ocr: bool
    slide_text_similarity: float
    keep_video: bool
    backend: str
    vision: bool
    vision_slides: int
    endpoint: str
    model: str
    request_timeout: float
    max_context_tokens: int
    reserve_output_tokens: int
    port: int
    obsidian_vault: str
    obsidian_folder: str
    autosync_check: int

    @classmethod
    def load(cls) -> "Config":
        try:
            import yaml
        except ImportError:
            die("PyYAML is missing. Install it with: pip install pyyaml")
        if not CONFIG_PATH.exists():
            die(f"No config at {CONFIG_PATH}. Copy config.yaml.example to config.yaml.")
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return cls(
            browser=raw.get("browser", "chrome"),
            sources=raw.get("sources", []),
            video_format=raw.get("video_format", "bv*[acodec=none]/b"),
            audio_format=raw.get("audio_format", "worst[acodec!=none]/ba/b"),
            whisper_model=raw.get(
                "whisper_model", "mlx-community/whisper-large-v3-turbo"
            ),
            slide_threshold=float(raw.get("slide_threshold", 0.15)),
            scene_scan_fps=float(raw.get("scene_scan_fps", 2)),
            min_slide_gap=float(raw.get("min_slide_gap", 2.0)),
            max_slides=int(raw.get("max_slides", 60)),
            ocr=bool(raw.get("ocr", True)),
            slide_text_similarity=float(raw.get("slide_text_similarity", 0.90)),
            keep_video=bool(raw.get("keep_video", False)),
            backend=raw.get("backend", "openai"),
            vision=bool(raw.get("vision", False)),
            vision_slides=int(raw.get("vision_slides", 6)),
            endpoint=raw.get("endpoint", "http://localhost:11434/v1"),
            model=raw.get("model", "gemma4:12b-mlx"),
            request_timeout=float(raw.get("request_timeout", 3600)),
            max_context_tokens=int(raw.get("max_context_tokens", 32768)),
            reserve_output_tokens=int(raw.get("reserve_output_tokens", 3000)),
            port=int(raw.get("port", 8420)),
            obsidian_vault=raw.get("obsidian_vault", ""),
            obsidian_folder=raw.get("obsidian_folder", "Lectures"),
            autosync_check=int(raw.get("autosync_check", 20)),
        )


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


# ---------------------------------------------------------------- sync


def cmd_sync(cfg: Config, args) -> None:
    if not have("yt-dlp"):
        die("yt-dlp not found. Install it with: brew install yt-dlp")

    sources = cfg.sources
    if args.url:
        # No --name means "work it out per lecture from the module code in the
        # title" — forcing a literal name here would file a mixed Recap folder
        # into one bucket.
        sources = [{"name": args.name, "url": args.url}]
    if not sources:
        die("No sources configured. Add them to config.yaml or pass --url.")

    LIBRARY.mkdir(exist_ok=True)
    failures = []

    for src in sources:
        name, url = src.get("name"), src.get("url")
        if not url:
            print(f"skipping malformed source: {src!r}")
            continue

        # Canonicalise first. A folder URL copied from Panopto's own UI keeps
        # the id in a #fragment, which is never sent to the server — yt-dlp
        # would see a bare List.aspx and silently list some default folder
        # instead of yours.
        target = parse_panopto(url)
        if not target:
            print(f"skipping (not a Panopto link): {url[:70]}")
            continue
        url = target["url"]
        label = name or (f"auto by module code "
                         f"({target['kind']} {target['id'][:8]})")

        print(f"\n=== {label} ===")

        # Check the session before starting. Without this an expired Recap
        # cookie surfaces as a bare yt-dlp exit code, once per lecture, with
        # the one thing you need to know — log back in — buried in it.
        try:
            preflight_auth(cfg, url)
        except RuntimeError as e:
            print(f"  ! {e}")
            failures.append(label)
            continue

        cmd = ytdlp_cmd(cfg, name, url, captions_only=args.captions_only)
        if args.limit:
            cmd += ["--playlist-items", f"1:{args.limit}"]
        if args.dry_run:
            cmd += ["--simulate"]
        cmd.append(url)

        rc = subprocess.call(cmd)
        if rc != 0:
            failures.append(label)
            print(f"  ! yt-dlp exited {rc} for {label}")

    if failures:
        print(f"\nfinished with problems in: {', '.join(failures)}")
        print("if this is an auth error, see the Authentication section of README.md")
    else:
        print("\nsync complete")


def safe_name(s: str) -> str:
    s = re.sub(r"[/\\:]+", "-", s).strip()
    return re.sub(r"\s+", " ", s)


def ytdlp_cmd(cfg: Config, module: str | None, url: str,
              captions_only: bool = False) -> list[str]:
    """Shared yt-dlp invocation. One directory per lecture, so the transcript
    and slides end up alongside the video. With no module name, each lecture
    files itself by the module code in its own title."""
    extract_module = []
    # Cap the *title field* rather than using --trim-filenames, which truncates
    # the whole path tail: a long title loses its "[id]" and even the "/video"
    # component, dumping loose files into the module folder with a mangled id.
    if module:
        dest = LIBRARY / safe_name(module)
        template = f"%(title).80s [%(id)s]/{VIDEO_NAME}.%(ext)s"
    else:
        # Panopto reports playlist_title as the useless literal "panopto_list",
        # and a Recap folder is usually a mixed archive rather than one module.
        # The module code lives in each lecture's own title, so pull it from
        # there and let every lecture file itself.
        dest = LIBRARY
        extract_module = [
            "--parse-metadata",
            r"title:(?P<module>\b[A-Z]{2,4}\d{3,4})",
        ]
        template = (f"%(module|Unsorted)s/"
                    f"%(title).80s [%(id)s]/{VIDEO_NAME}.%(ext)s")
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", cfg.browser,
        "--paths", str(dest),
        "-o", template,
        *extract_module,
        # Panopto titles carry dates and trailing slashes ("24/03/2026 @ 13:32 -
        # BEF2014_L1/ - ..."), which yt-dlp would otherwise escape to "⧸".
        "--replace-in-metadata", "title", r"[/:]", "-",
        "--replace-in-metadata", "title", r"\s*-\s*-\s*", " - ",
        "--replace-in-metadata", "title", r"[\s-]+$", "",
        "-f", cfg.video_format,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*,en",
        "--no-write-playlist-metafiles",   # a folder isn't a lecture
        "--no-overwrites",
        "--ignore-errors",
        "--retries", "5",
        "--concurrent-fragments", "4",
        # yt-dlp repaints progress with \r many times a second. Piped to a log
        # or into the daemon's job buffer that's hundreds of KB of noise which
        # buries the lines that matter, so throttle it and keep one per line.
        "--progress-delta", "5",
        "--newline",
    ]
    if captions_only:
        # No archive entry: recording the id here would make a later full
        # download a silent no-op, stranding the lecture without its video.
        cmd.append("--skip-download")
    else:
        cmd += ["--download-archive", str(ARCHIVE)]
    return cmd


AUTH_HINT = (
    "Recap rejected the session. Open {host} in {browser}, log in, and try "
    "again — the cookie expires every few hours."
)


def preflight_auth(cfg: Config, url: str) -> None:
    """Check the session once before a batch.

    Without this a 15-lecture module fails 15 times with yt-dlp's own wording,
    which buries the one thing you need to know: log back into Recap.
    """
    proc = subprocess.run(
        ["yt-dlp", "--simulate", "--quiet", "--no-warnings",
         "--flat-playlist", "--print", "%(id)s",
         "--playlist-items", "1", "--socket-timeout", "20",
         "--cookies-from-browser", cfg.browser, url],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        if any(line.strip() for line in (proc.stdout or "").splitlines()):
            return
        # Exit 0 having listed nothing. A single lecture fails loudly when the
        # session has expired, but Panopto's folder API answers an
        # unauthenticated caller with an empty list rather than an error — so
        # yt-dlp succeeds, and a sync of a 131-lecture folder reports
        # "sync complete" having done nothing at all. All term, every week.
        host = re.search(r"https?://([^/\s]+)", url)
        raise RuntimeError(
            f"{host.group(1) if host else 'Recap'} listed no lectures here. "
            f"Either the session in {cfg.browser} has expired — log back in "
            "and try again — or this folder is empty, which is what a Recap "
            "folder looks like once you've been unenrolled from the module."
        )

    err = (proc.stderr or "") + (proc.stdout or "")
    if "registered users" in err or "not available" in err.lower():
        host = re.search(r"https?://([^/\s]+)", url)
        raise RuntimeError(AUTH_HINT.format(
            host=host.group(1) if host else "Recap", browser=cfg.browser))
    if "unsupported browser" in err.lower():
        raise RuntimeError(
            f"config.yaml asks for cookies from {cfg.browser!r}, which yt-dlp "
            "doesn't support. Use one of: brave, chrome, chromium, edge, "
            "firefox, opera, safari, vivaldi, whale."
        )
    if "could not find" in err.lower() and "cookies" in err.lower():
        raise RuntimeError(
            f"Couldn't read cookies from {cfg.browser}. Check the browser name "
            "in config.yaml, and that you're logged into Recap in it."
        )
    # Anything else is the caller's problem to report in context.


def panopto_id(url: str) -> str | None:
    m = re.search(r"[?&]id=([0-9a-fA-F-]{36})", url)
    return m.group(1) if m else None


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DEFAULT_HOST = "recapexeter.cloud.panopto.eu"


def parse_panopto(text: str) -> dict | None:
    """Work out what someone pasted.

    Exeter's Recap serves single lectures as Viewer.aspx?id=<uuid> and modules
    as Sessions/List.aspx?folderID=<uuid>, usually with an &instance= suffix
    from Moodle. A bare UUID is treated as a lecture on the default host.
    """
    text = (text or "").strip()
    if not text:
        return None

    host = DEFAULT_HOST
    m = re.search(r"https?://([^/\s]+)", text)
    if m and "panopto" in m.group(1).lower():
        host = m.group(1)

    # Browsing folders in Panopto's own UI puts the id in a hash fragment with
    # URL-encoded quotes — List.aspx#folderID=%22<uuid>%22 — so accept that too.
    folder = re.search(r"[?&#]folderID=(?:%22|\"|')?([0-9a-fA-F-]{36})", text, re.I)
    if folder:
        return {
            "kind": "folder", "id": folder.group(1), "host": host,
            "url": (f"https://{host}/Panopto/Pages/Sessions/List.aspx"
                    f"?folderID={folder.group(1)}"),
        }

    m = re.search(r"[?&#]id=(?:%22|\"|')?([0-9a-fA-F-]{36})", text, re.I)
    vid = m.group(1) if m else (text if UUID_RE.fullmatch(text) else None)
    if vid:
        return {
            "kind": "lecture", "id": vid, "host": host,
            "url": f"https://{host}/Panopto/Pages/Viewer.aspx?id={vid}",
        }
    return None


def find_by_id(vid: str) -> Path | None:
    for d in LIBRARY.rglob("*"):
        if d.name.endswith(f"[{vid}]") and is_lecture_dir(d):
            return d
    return None


def fetch_url(cfg: Config, url: str, module: str = "Unsorted") -> Path:
    """Download one lecture and return its directory. Idempotent: if it's
    already in the library the download archive skips it."""
    vid = panopto_id(url)
    if vid:
        existing = find_by_id(vid)
        # A transcript-only lecture has a directory but no recording, so it
        # must fall through and download — otherwise "get video & slides" can
        # never upgrade it. A pruned one is already done; leave it alone.
        if existing and (find_video(existing) or is_pruned(existing)):
            print(f"already downloaded: {existing.name}")
            return existing
        if existing:
            print(f"upgrading transcript-only lecture: {existing.name[:52]}")

    print("downloading...")
    rc = subprocess.call(ytdlp_cmd(cfg, module, url) + [url])
    if rc != 0:
        raise RuntimeError(
            "yt-dlp failed — usually an expired session. Log into Recap in "
            f"{cfg.browser} and try again."
        )

    d = find_by_id(vid) if vid else None
    if not d:
        raise RuntimeError("download finished but the lecture folder wasn't found")
    return d


def fetch_captions(cfg: Config, url: str, module: str | None = None) -> list[Path]:
    """Pull just the captions and metadata — no video.

    Panopto's own captions are all you need for notes, and they arrive in
    seconds instead of the minutes a 150 MB screen recording takes. You lose
    the slides, so no OCR and no figures; run a normal fetch later to fill
    those in.

    Deliberately skips --download-archive: recording the id here would make a
    later full download a no-op, stranding the lecture without its video.
    """
    preflight_auth(cfg, url)
    cmd = ytdlp_cmd(cfg, module, url, captions_only=True)

    print("fetching captions only (no video)...")
    before = set(lecture_dirs())
    rc = subprocess.call(cmd + [url])
    if rc != 0:
        print("  (yt-dlp reported errors; continuing with whatever arrived)")

    after = lecture_dirs()
    fresh = [d for d in after if d not in before]
    got = fresh or [d for d in after if panopto_id(url) and
                    d.name.endswith(f"[{panopto_id(url)}]")]
    if not got:
        raise RuntimeError(
            "no captions arrived — the lecture may have none, or your Recap "
            f"session in {cfg.browser} has expired"
        )
    return got


def fetch_folder(cfg: Config, url: str, module: str | None = None) -> list[Path]:
    """Download a whole Panopto folder. Returns every lecture it contains,
    including ones already present — the download archive skips those."""
    before = {d for d in lecture_dirs()}

    preflight_auth(cfg, url)

    print("downloading module...")
    rc = subprocess.call(ytdlp_cmd(cfg, module, url) + [url])
    if rc != 0:
        print("  (yt-dlp reported errors; continuing with whatever arrived)")

    after = [d for d in lecture_dirs()]
    fresh = [d for d in after if d not in before]
    if not fresh and not after:
        raise RuntimeError(
            "nothing downloaded — check the folder URL, and that you're logged "
            f"into Recap in {cfg.browser}"
        )
    print(f"{len(fresh)} new lecture(s), {len(after)} in the library")
    return fresh or after


# ---------------------------------------------------------------- process


def is_lecture_dir(d: Path) -> bool:
    """A lecture is a directory holding a recording *or* just its captions —
    transcript-only lectures have no video file at all.

    yt-dlp also drops a playlist-level info.json when it walks a folder, so
    those are excluded or the module itself shows up as a lecture.
    """
    if not d.is_dir():
        return False
    if find_video(d):
        return True
    for info in d.glob("*.info.json"):
        try:
            if json.loads(info.read_text()).get("_type") != "playlist":
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def lecture_richness(d: Path) -> tuple:
    """How much work is invested in a directory, for picking between copies."""
    return (
        len(list(d.glob("notes*.md"))),
        (d / "slides.json").exists(),
        (d / "bundle.md").exists(),
        bool(find_video(d)),
        len(list(d.iterdir())),
    )


def lecture_dirs() -> list[Path]:
    """Every lecture, one entry per Panopto id.

    The same lecture can land in two directories: a caption-only fetch skips
    the download archive on purpose, so a later folder sync re-fetches it, and
    any change to title normalisation gives the new copy a different name. Keep
    whichever copy has the most work in it.
    """
    if not LIBRARY.exists():
        return []
    best: dict[str, Path] = {}
    for d in sorted(LIBRARY.rglob("*")):
        if not is_lecture_dir(d):
            continue
        vid = lecture_id(d)
        if vid not in best or lecture_richness(d) > lecture_richness(best[vid]):
            best[vid] = d
    return sorted(best.values())


def lecture_info(d: Path) -> dict:
    """yt-dlp's sidecar metadata for a lecture, or {} when there isn't any.

    Six call sites globbed, read and parsed this file for themselves, with
    three different sets of caught exceptions between them and not one
    catching OSError — so an info.json that exists but cannot be read took
    the whole command down. Two places asking the same question is how
    is_pruned and slides_extracted drifted apart; one question, one answer.
    """
    info_file = next(iter(d.glob("*.info.json")), None)
    if not info_file:
        return {}
    try:
        info = json.loads(info_file.read_text())
    except (OSError, ValueError):     # JSONDecodeError is a ValueError
        return {}
    # A JSON file is not necessarily a JSON *object*; every caller .get()s.
    return info if isinstance(info, dict) else {}


def find_video(d: Path) -> Path | None:
    for ext in ("mp4", "mkv", "webm", "m4v"):
        p = d / f"{VIDEO_NAME}.{ext}"
        if p.exists():
            return p
    return None


def cmd_process(cfg: Config, args) -> None:
    if not have("ffmpeg"):
        die("ffmpeg not found. Install it with: brew install ffmpeg")
    if args.whisper_model:
        cfg.whisper_model = args.whisper_model

    dirs = lecture_dirs()
    if args.only:
        dirs = [d for d in dirs if args.only.lower() in d.name.lower()]
    if not dirs:
        die("No downloaded lectures found. Run `sync` first.")

    write_agents_file()

    for d in dirs:
        bundle = d / "bundle.md"
        # A transcript-only lecture already has a bundle. Once it gains a
        # video its slides still need extracting, so "has a bundle" isn't
        # enough to call it done.
        needs_slides = find_video(d) and not slides_extracted(d)
        if bundle.exists() and not args.force and not needs_slides:
            print(f"skip (done): {d.name}")
            continue

        print(f"\n=== {d.name} ===")
        video = find_video(d)
        try:
            segments = get_transcript(cfg, d, video, force_whisper=args.force_whisper)
            slides = annotate_slides(cfg, d, extract_slides(cfg, d, video))
            write_bundle(d, segments, slides)
            if slides and not (cfg.keep_video or args.keep_video):
                freed = prune_video(d)
                if freed:
                    print(f"  pruned video, freed {human_size(freed)}")
            print(f"  -> {bundle.relative_to(ROOT)}")
        except Exception as e:  # keep going; one bad lecture shouldn't stop the run
            print(f"  ! failed: {e}")


# ------------------------------------------------- transcript


def get_transcript(cfg: Config, d: Path, video: Path, force_whisper: bool):
    cached = d / "transcript.json"
    if cached.exists() and not force_whisper:
        return json.loads(cached.read_text())

    segments = None
    if not force_whisper:
        subs = sorted(d.glob("*.srt")) + sorted(d.glob("*.vtt"))
        if subs:
            segments = parse_vtt(subs[0])
            if segments:
                print(f"  transcript: Panopto captions ({len(segments)} segments)")

    if not segments:
        segments = whisper_transcribe(cfg, d, video)

    cached.write_text(json.dumps(segments, indent=1))
    return segments


def parse_vtt(path: Path) -> list[dict]:
    """Minimal WebVTT/SRT parser. Panopto serves .srt; both are cue blocks."""
    segments = []
    cue_time = re.compile(
        r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
    )
    start = end = None
    lines: list[str] = []

    def flush():
        if start is not None and lines:
            text = " ".join(lines).strip()
            text = re.sub(r"<[^>]+>", "", text)  # strip karaoke/word tags
            if text:
                segments.append({"start": start, "end": end, "text": text})

    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        m = cue_time.search(line)
        if m:
            flush()
            lines = []
            start, end = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
        elif not line:
            flush()
            lines = []
            start = end = None
        elif start is not None and line != "WEBVTT" and not line.isdigit():
            lines.append(line)
    flush()

    # Panopto auto-captions repeat the rolling line; drop consecutive dupes
    deduped = []
    for s in segments:
        if deduped and deduped[-1]["text"] == s["text"]:
            deduped[-1]["end"] = s["end"]
        else:
            deduped.append(s)
    return deduped


def ts_to_sec(ts: str) -> float:
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def has_audio(media: Path) -> bool:
    proc = run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(media)],
        capture=True,
    )
    return "audio" in proc.stdout


def fetch_audio(cfg: Config, d: Path) -> Path:
    """The screen-capture stream carries no audio, so pull the smallest
    audio-bearing stream separately when Whisper needs it."""
    cached = next(iter(d.glob("audio_src.*")), None)
    if cached:
        return cached

    url = lecture_info(d).get("webpage_url")
    if not url:
        raise RuntimeError("no audio in the video and no source URL to re-fetch it")

    print("  video has no audio track — fetching audio stream...")
    rc = subprocess.call([
        "yt-dlp",
        "--cookies-from-browser", cfg.browser,
        "-f", cfg.audio_format,
        "--paths", str(d),
        "-o", "audio_src.%(ext)s",
        "--no-write-info-json", "--no-write-subs",
        "--retries", "5", "--concurrent-fragments", "4",
        url,
    ])
    got = next(iter(d.glob("audio_src.*")), None)
    if rc != 0 or not got:
        raise RuntimeError("couldn't fetch an audio stream for transcription")
    return got


def whisper_transcribe(cfg: Config, d: Path, video: Path) -> list[dict]:
    try:
        import mlx_whisper
    except ImportError:
        die(
            "No captions available and mlx-whisper isn't installed.\n"
            "  Install it with: pip install mlx-whisper"
        )

    source = video if has_audio(video) else fetch_audio(cfg, d)

    audio = d / "audio.wav"
    if not audio.exists():
        print("  extracting audio...")
        run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(audio)]
        )

    print(f"  transcribing locally with {cfg.whisper_model} ...")
    result = mlx_whisper.transcribe(
        str(audio), path_or_hf_repo=cfg.whisper_model, verbose=False
    )
    audio.unlink(missing_ok=True)

    return [
        {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        for s in result["segments"]
        if s["text"].strip()
    ]


# ------------------------------------------------- slide OCR


def ocr_available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


def ocr_image(path: Path) -> list[str]:
    """Read a slide with Apple's Vision framework — no model download, runs on
    the GPU, and it's very accurate on clean presentation text."""
    import Quartz
    import Vision

    p = str(path).encode()
    url = Quartz.CFURLCreateFromFileSystemRepresentation(None, p, len(p), False)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if not src:
        return []
    image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if not image:
        return []

    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    handler.performRequests_error_([req], None)

    lines = []
    for obs in req.results() or []:
        best = obs.topCandidates_(1)
        if best:
            text = best[0].string().strip()
            if text:
                lines.append(text)
    return lines


# Local contrast below which a frame is considered to have nothing on it.
# Measured on this library: a bare desktop scores 0.14, the sparsest real
# slide 1.24, so 0.5 clears both by about 3x.
BLANK_FRAME_CONTRAST = 0.5


def is_blank_frame(path: Path) -> bool:
    """A frame with nothing on it is the presenter's desktop between slides,
    not a slide. Scene detection catches these because the screen genuinely
    changed, but they carry nothing and end up embedded in the exported notes.

    Measured as local contrast — how far the frame differs from its own blur —
    and not as overall spread, because spread is inverted for exactly the
    slides worth keeping: a page packed with small text averages to a flat grey
    once downsampled, so the denser the slide the blanker it scores. Every one
    of the 175 slides in this library scored above the old 8.0 cutoff, but the
    three closest to it, at 8.21, were the densest slides present — a LaTeX
    regression table of coefficients and standard errors.
    """
    try:
        from PIL import Image, ImageChops, ImageFilter, ImageStat
    except ImportError:
        return False
    try:
        with Image.open(path) as im:
            grey = im.convert("L").resize((512, 512))
            edges = ImageChops.difference(
                grey, grey.filter(ImageFilter.GaussianBlur(2)))
            return ImageStat.Stat(edges).mean[0] < BLANK_FRAME_CONTRAST
    except OSError:
        return False


def text_key(lines: list[str]) -> str:
    """Normalised bag of words, for comparing one slide against the next."""
    return " ".join(sorted(set(re.findall(r"[a-z0-9]+", " ".join(lines).lower()))))


def similarity(a: str, b: str) -> float:
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 1.0 if wa == wb else 0.0
    return len(wa & wb) / len(wa | wb)


# ------------------------------------------------- slides


def extract_slides(cfg: Config, d: Path, video: Path | None) -> list[dict]:
    out = d / SLIDE_DIR
    cached = d / "slides.json"
    if cached.exists() and out.exists():
        return json.loads(cached.read_text())
    if not video:
        return []  # transcript-only lecture; nothing to extract frames from

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # One decode pass only. A 2-hour lecture takes minutes to scan, so thinning
    # an over-eager result is far cheaper than re-running at a higher threshold.
    #
    # The scene filter scores each frame against the one before it, so at full
    # rate it is asked whether 1/25th of a second changed anything — which for a
    # slide that cross-fades over half a second is 25 changes too small to clear
    # the threshold, and the slide is missed outright. Thinning to a couple of
    # frames a second puts the whole transition between one comparison and the
    # next. Measured on a 1080p reconstruction of a real lecture: 4.33s at full
    # rate against 1.40s at 2 fps, finding the same slides on hard cuts — and on
    # a version cross-fading over 0.4s, one of three transitions rather than the
    # none full rate manages.
    #
    # (Hardware decode is the obvious next thought and it is a trap: -hwaccel
    # videotoolbox measured 10.47s on the same file, since every frame has to
    # come back off the GPU for the filter anyway.)
    prefilter = f"fps={cfg.scene_scan_fps:g}," if cfg.scene_scan_fps else ""
    vf = (
        rf"{prefilter}select='eq(n\,0)+gt(scene\,{cfg.slide_threshold})',"
        r"scale='min(1280,iw)':-2,showinfo"
    )
    proc = run(
        ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y",
         "-i", str(video), "-vf", vf, "-vsync", "vfr", "-q:v", "4",
         str(out / "slide_%04d.jpg")],
        capture=True,
    )
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", proc.stderr)]
    files = sorted(out.glob("slide_*.jpg"))
    if not files:
        tail = "\n".join(proc.stderr.strip().splitlines()[-3:])
        raise RuntimeError(f"ffmpeg produced no keyframes:\n{tail}")

    slides = [
        {"file": f"{SLIDE_DIR}/{f.name}", "time": times[i] if i < len(times) else 0.0}
        for i, f in enumerate(files)
    ]

    # Slide changes cross-fade, so one transition fires the detector several
    # times in a row. Collapse each burst and keep the last frame — that's the
    # one where the new slide has finished rendering.
    if slides:
        collapsed = [slides[0]]
        for s in slides[1:]:
            if s["time"] - collapsed[-1]["time"] < cfg.min_slide_gap:
                (d / collapsed[-1]["file"]).unlink(missing_ok=True)
                collapsed[-1] = s
            else:
                collapsed.append(s)
        if len(collapsed) < len(slides):
            print(f"  {len(slides)} keyframes -> {len(collapsed)} after "
                  "merging transition bursts")
        slides = collapsed

    print(f"  slides: {len(slides)} keyframes")
    cached.write_text(json.dumps(slides, indent=1))
    return slides


def annotate_slides(cfg: Config, d: Path, slides: list[dict]) -> list[dict]:
    """Read the slides and thin them. Kept separate from extraction so it can
    run against already-extracted frames — re-decoding two hours of video just
    to add OCR would be absurd."""
    if not slides:
        return slides
    if all("text" in s for s in slides):
        return slides  # already annotated

    def drop_blanks(items: list[dict]) -> list[dict]:
        """Blank frames are the presenter's desktop between slides. Decided by
        file path rather than dict equality — comparing dicts against a growing
        list is quadratic, and a code-heavy lecture yields hundreds of frames.

        Set-aside frames move into slides/dropped/, they are not deleted.
        Contrast cannot tell a clean desktop (0.14) from a pale line diagram
        (0.13), so a wrongly judged frame has to stay recoverable after the
        video is pruned: a kept blank costs an empty image in the notes, a
        lost diagram costs the only copy in existence."""
        keep = [s for s in items
                if s.get("text") or not is_blank_frame(d / s["file"])]
        keeping = {s["file"] for s in keep}
        bin_ = d / SLIDE_DIR / "dropped"
        for s in items:
            if s["file"] not in keeping and (d / s["file"]).exists():
                bin_.mkdir(parents=True, exist_ok=True)
                (d / s["file"]).rename(bin_ / Path(s["file"]).name)
        if len(keep) < len(items):
            print(f"  set aside {len(items) - len(keep)} blank frame(s) in "
                  f"{SLIDE_DIR}/dropped/")
        return keep

    reading = cfg.ocr and ocr_available()
    if cfg.ocr and not reading:
        print("  OCR unavailable (pip install pyobjc-framework-Vision) — skipping")

    if reading:
        for s in slides:
            s["text"] = ocr_image(d / s["file"])
        with_text = sum(1 for s in slides if s["text"])
        print(f"  OCR: text found on {with_text}/{len(slides)} slides")

        # Only with OCR's testimony in hand, so a frame is set aside for
        # having no text on it *and* nothing visible on it. Without OCR the
        # contrast score is the only voice and it cannot be trusted alone,
        # so blanks are simply kept.
        slides = drop_blanks(slides)
        if not slides:
            # Written here, not at the end this return skips: a stale
            # slides.json naming frames that no longer exist reads as
            # "slides extracted", and a later prune then takes the video —
            # the only remaining copy of a lecture with nothing on disk.
            (d / "slides.json").write_text("[]")
            return slides

        # Animated builds repeat the previous slide plus a line or two. Collapse
        # them and keep the last, which is the finished slide.
        merged = [slides[0]]
        for s in slides[1:]:
            a = text_key(merged[-1].get("text", []))
            b = text_key(s.get("text", []))
            if a and b and similarity(a, b) >= cfg.slide_text_similarity:
                (d / merged[-1]["file"]).unlink(missing_ok=True)
                merged[-1] = s
            else:
                merged.append(s)
        if len(merged) < len(slides):
            print(f"  {len(slides)} -> {len(merged)} after merging repeated text")
        slides = merged

    if len(slides) > cfg.max_slides:
        print(f"  {len(slides)} keyframes — thinning to {cfg.max_slides}")
        step = len(slides) / cfg.max_slides
        keep = {int(i * step) for i in range(cfg.max_slides)}
        for i, s in enumerate(slides):
            if i not in keep:
                (d / s["file"]).unlink(missing_ok=True)
        slides = [s for i, s in enumerate(slides) if i in keep]

    (d / "slides.json").write_text(json.dumps(slides, indent=1))
    return slides


# ------------------------------------------------- disk


def slides_extracted(d: Path) -> bool:
    """Whether this lecture's frames have actually been pulled out of the video.

    A missing or unreadable slides.json means they have not, and the recording
    is still the only copy of them.
    """
    try:
        return bool(json.loads((d / "slides.json").read_text()))
    except (OSError, ValueError):
        return False


def is_pruned(d: Path) -> bool:
    """Distinguishes 'we had the video and threw it away' from 'we never
    downloaded one'. Slides can only exist if a video was decoded, so their
    presence without a video means it was pruned — and re-downloading would be
    a waste rather than a fix.
    """
    return not find_video(d) and slides_extracted(d)


def prune_video(d: Path) -> int:
    """Drop the recording once slides and transcript are extracted.

    ~148 MB becomes ~5 MB. Everything the notes rely on survives: the slides
    are images on disk, the transcript is text, and each timestamp already
    links back into Panopto for the moments you want to actually hear.

    A bundle on its own is not proof of that. A lecture Panopto had captioned
    but that had not been downloaded yet gets a transcript-only bundle, so
    pruning on the strength of one destroys the recording as soon as it lands —
    and `process` then reports "skip (done)" forever, because the bundle it
    checks for is present and the video it would read slides from is gone.
    """
    video = find_video(d)
    if not video:
        return 0
    if not (d / "bundle.md").exists():
        return 0                      # never discard before it's been processed
    if not slides_extracted(d):
        return 0                      # the slides are still only inside the video
    freed = video.stat().st_size
    video.unlink()
    return freed


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def cmd_prune(cfg: Config, args) -> None:
    targets = lecture_dirs()
    if args.slug:
        targets = [d for d in targets if args.slug.lower() in d.name.lower()]
    if not targets:
        die("No matching lectures.")

    total, done = 0, 0
    for d in targets:
        if not find_video(d):
            continue
        if not (d / "bundle.md").exists():
            print(f"skip (not processed): {d.name[:56]}")
            continue
        if not slides_extracted(d):
            print(f"skip (slides not extracted yet): {d.name[:44]}")
            continue
        size = find_video(d).stat().st_size
        if args.dry_run:
            print(f"would free {human_size(size):>9}  {d.name[:52]}")
        else:
            prune_video(d)
            print(f"freed {human_size(size):>9}  {d.name[:52]}")
        total += size
        done += 1

    verb = "would free" if args.dry_run else "freed"
    print(f"\n{verb} {human_size(total)} across {done} lecture(s)")


# ------------------------------------------------- bundle


def write_bundle(d: Path, segments: list[dict], slides: list[dict]):
    info = lecture_info(d)
    title = info.get("title") or d.name
    duration = info.get("duration")
    upload = info.get("upload_date") or ""
    if len(upload) == 8:
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"

    lines = [f"# {title}", ""]
    meta = []
    if info.get("uploader"):
        meta.append(f"**Presenter:** {info['uploader']}")
    if upload:
        meta.append(f"**Recorded:** {upload}")
    if duration:
        meta.append(f"**Duration:** {hhmm(duration)}")
    if info.get("webpage_url"):
        meta.append(f"**Source:** {info['webpage_url']}")
    if meta:
        lines += [" · ".join(meta), ""]

    has_ocr = any(s.get("text") for s in slides)
    if slides:
        note = f"*{len(segments)} transcript segments, {len(slides)} slide keyframes.*"
    else:
        note = (f"*{len(segments)} transcript segments. Transcript only — no "
                "video was downloaded, so there are no slides. Figures and "
                "formulas the lecturer showed but didn't say aloud are missing.*")
    lines += [note, ""]
    if has_ocr:
        lines += [
            "> The transcript is auto-generated and garbles technical vocabulary. "
            "Each slide below is followed by its text read straight off the image, "
            "which is authoritative — prefer it over the transcript for any term, "
            "figure or formula, and correct the transcript against it.",
            "",
        ]
    lines += ["---", "", "## Transcript", ""]

    # interleave slide markers into the transcript by timestamp
    events = [("slide", s["time"], s) for s in slides]
    events += [("text", s["start"], s) for s in segments]
    events.sort(key=lambda e: e[1])

    for kind, t, payload in events:
        if kind == "slide":
            lines += ["", f"![slide @ {hhmm(t)}]({payload['file']})", ""]
            if payload.get("text"):
                lines.append(f"**Slide text @ {hhmm(t)}:**")
                lines += [f"> {l}" for l in payload["text"]]
                lines.append("")
        else:
            lines.append(f"`{hhmm(t)}` {payload['text']}")

    (d / "bundle.md").write_text("\n".join(lines) + "\n")

    plain = "\n".join(f"[{hhmm(s['start'])}] {s['text']}" for s in segments)
    (d / "transcript.md").write_text(f"# {title}\n\n{plain}\n")


AGENTS_MD = """# Lecture notes workspace

Each subfolder is one recorded university lecture, already transcribed with
slide keyframes extracted. Nothing here needs downloading or transcribing.

Per lecture:

- `bundle.md` — the timestamped transcript with slide images interleaved at the
  point each slide appears. **Start here.** Slide paths are relative.
- `transcript.md` — the same transcript, text only, no images.
- `slides/` — the slide keyframes as JPEGs.
- `notes.md` — the output. Write it here.

## The task

When asked to write up a lecture, read its `bundle.md` (open the referenced
slide images too — they carry the equations and tables the transcript garbles)
and write `notes.md` in that same folder containing:

1. **Summary** — what the lecture covered, 3-4 sentences.
2. **Key concepts** — each defined plainly, with the timestamp it's introduced.
3. **Worked examples** — the numbers from the slides, step by step.
4. **Exam signals** — anything flagged as important or examinable. Quote it.
5. **Gaps** — what was rushed or assumed, so it can be read up on.

Keep timestamps so the recording can be jumped back into.

The transcripts are auto-generated and mangle technical vocabulary badly —
treat the slide images as the authority on any term, figure, or formula, and
silently correct the transcript against them.
"""


def write_agents_file() -> None:
    """Drop an instruction file agentic IDEs pick up, so Antigravity (or any
    other agent pointed at library/) knows what to do without being told."""
    LIBRARY.mkdir(exist_ok=True)
    target = LIBRARY / "AGENTS.md"
    if not target.exists():
        target.write_text(AGENTS_MD)


def hhmm(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# ---------------------------------------------------------------- analyse

# The whole point: the student already has the slides. Summarising them back is
# worthless. What they didn't get — because they missed the lecture — is
# everything the lecturer said that never made it onto a slide.
DELTA_PROMPT = """Below is a recorded lecture: the transcript, with each slide's \
text inserted at the point it appeared.

**I already have the slides.** Do not summarise them back to me. Give me only \
what the recording adds that the slides do not.

1. **Beyond the slides** — the explanations, intuitions, analogies, worked \
reasoning and asides the lecturer gave out loud that appear nowhere in the \
slide text. This is the main thing I want, so give it the most room. Timestamp \
each point.
2. **Emphasis and exam signals** — anything flagged as important, examinable, \
commonly got wrong, or explicitly not needed. Quote the lecturer directly.
3. **Corrections to the slides** — anywhere the lecturer contradicted, fixed, \
updated or told you to ignore what was on screen.
4. **Questions and answers** — questions raised in the room and how they were \
answered.
5. **Key concepts** — each term in the lecturer's own framing rather than the \
slide's wording, with the timestamp it's introduced. One top-level bullet per \
concept, term in bold.
6. **Gaps** — what was rushed, assumed as prior knowledge, or skipped.

Rules:
- If a section has nothing genuine in it, write "nothing notable" — do not pad \
it by restating slide content.
- Use quotation marks only for words the lecturer actually said, copied from \
the transcript. If you are paraphrasing, write it as your own sentence without \
quote marks — a tidied-up paraphrase in quotes reads as verbatim and isn't.
- Where you cite a figure or formula, take it from the slide text, which is \
authoritative; the transcript garbles numbers. Never state a number the \
material doesn't contain.
- Keep timestamps so I can jump back to the moment.
"""

# For transcript-only lectures there are no slides to differ from, so ask for
# notes outright rather than a comparison against nothing.
SUMMARY_PROMPT = """Below is the transcript of a recorded lecture, with \
timestamps. No slides are available, so the transcript is all there is.

Produce structured notes:
1. **Summary** — what this lecture was about, in 3-4 sentences.
2. **Key concepts** — each with a plain-English definition and the timestamp \
where it's introduced. One top-level bullet per concept, term in bold.
3. **Worked examples / derivations** — spelled out step by step. Never state a \
number the material doesn't contain.
4. **Emphasis and exam signals** — anything flagged as important, examinable, \
or commonly misunderstood. Quote the lecturer directly.
5. **Gaps** — points rushed, unclear, or assuming prior knowledge.

Keep timestamps so I can jump back to the recording.
"""

DEFAULT_PROMPT = DELTA_PROMPT


MIN_SLIDE_CHARS_FOR_DELTA = 600


def choose_prompt(d: Path, use_slides: bool) -> str:
    """A delta against the slides only makes sense when there's slide content
    to differ from — measured by how much text is on them, not how many there
    are.

    Counting slides gets this wrong at both ends. A 90-minute exam-practice
    session leaves one dense question on screen throughout: four keyframes, but
    the transcript is the lecturer working through it, which is the most
    valuable delta there is. A rambling discussion with one holding slide
    reading "University" has plenty of nothing.
    """
    if not use_slides:
        return SUMMARY_PROMPT
    slides = d / "slides.json"
    if not slides.exists():
        return SUMMARY_PROMPT
    try:
        entries = json.loads(slides.read_text())
    except (OSError, json.JSONDecodeError):
        return SUMMARY_PROMPT
    chars = sum(len(" ".join(s.get("text") or [])) for s in entries)
    return DELTA_PROMPT if chars >= MIN_SLIDE_CHARS_FOR_DELTA else SUMMARY_PROMPT


def cmd_analyse(cfg: Config, args) -> None:
    # These have to land before the batch dispatch. Applied after it, as they
    # used to be, a `--all --backend antigravity` run quietly ignored both and
    # went to whatever config said.
    if args.model:
        cfg.model = args.model
    if args.backend:
        cfg.backend = args.backend
    if args.vision:
        cfg.vision = True
    if cfg.vision and cfg.backend not in ("antigravity", "openai"):
        die(f"--vision needs the antigravity or openai backend; {cfg.backend} "
            f"can't read the slide images.")
    if cfg.vision and cfg.backend == "openai":
        # Ollama knows which of its models can see, so a text-only model gets
        # turned away here rather than silently ignoring every slide sent.
        can_see = ollama_has_vision(cfg.endpoint, cfg.model)
        if can_see is False:
            die(f"{cfg.model} has no vision capability — `ollama show "
                f"{cfg.model}` lists what it can do. muse-glimmer:30b-mlx can.")

    if args.all or args.module:
        return analyse_batch(cfg, args)

    if not args.slug:
        die("Name a lecture, or pass --all / --module to do a batch.")
    dirs = [d for d in lecture_dirs() if args.slug.lower() in d.name.lower()]
    if not dirs:
        die(f"No lecture matching {args.slug!r}. Try `status`.")
    if len(dirs) > 1:
        print("Matches more than one lecture:")
        for d in dirs:
            print(f"  {d.name}")
        die("be more specific")

    if args.prompt:
        prompt = Path(args.prompt).read_text()
    elif args.style == "summary":
        prompt = SUMMARY_PROMPT
    elif args.style == "delta":
        prompt = DELTA_PROMPT
    else:
        prompt = choose_prompt(dirs[0], use_slides=True)

    # The delta needs the slide text, which only bundle.md carries.
    use_slides = args.slides or prompt is DELTA_PROMPT
    out_name = f"notes-{args.label}.md" if args.label else "notes.md"
    notes = run_analysis(cfg, dirs[0], use_slides=use_slides, prompt=prompt,
                         out_name=out_name)
    print(notes)
    print(f"\nsaved to {(dirs[0] / out_name).relative_to(ROOT)}")


def analyse_batch(cfg: Config, args) -> None:
    """Write notes for many lectures unattended.

    A term is dozens of lectures at minutes each, so this skips anything
    already done and keeps going past a failure rather than losing the run.
    """
    dirs = lecture_dirs()
    if args.module:
        dirs = [d for d in dirs if args.module.lower() in d.parent.name.lower()]
    dirs = [d for d in dirs if (d / "bundle.md").exists()]

    out_name = f"notes-{args.label}.md" if args.label else "notes.md"
    todo = [d for d in dirs if args.redo or not (d / out_name).exists()]
    if not todo:
        print(f"Nothing to do — all {len(dirs)} lecture(s) already have "
              f"{out_name}. Use --redo to rewrite them.")
        return

    where = ("via agy" + (", reading the slides" if cfg.vision else "")
             if cfg.backend == "antigravity" else f"at {cfg.endpoint}")
    print(f"{len(todo)} of {len(dirs)} lecture(s) need notes "
          f"({cfg.model} {where})\n")

    def write_one(d: Path) -> str:
        if args.prompt:
            prompt = Path(args.prompt).read_text()
        elif args.style == "summary":
            prompt = SUMMARY_PROMPT
        elif args.style == "delta":
            prompt = DELTA_PROMPT
        else:
            prompt = choose_prompt(d, use_slides=True)

        run_analysis(cfg, d, use_slides=args.slides or prompt is DELTA_PROMPT,
                     prompt=prompt, out_name=out_name)
        return "delta" if prompt is DELTA_PROMPT else "summary"

    done, failed = 0, []

    if args.jobs > 1:
        # Lectures don't depend on each other and the whole job is spent
        # waiting on someone else's model, so a term's worth can be in flight
        # at once. Threads, not processes: the wait is inside a subprocess or a
        # socket, both of which drop the GIL. Left at 1 by default because the
        # local backend is one Ollama instance and handing it four lectures at
        # once just makes all four slow.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        local = cfg.backend == "openai" and (
            "localhost" in cfg.endpoint or "127.0.0.1" in cfg.endpoint)
        if local:
            # Said rather than enforced: it's one model server either way, so
            # concurrency buys nothing and costs memory, but a local endpoint
            # can be fronting more than Ollama.
            print(f"  note: {cfg.endpoint} is local, so {args.jobs} at a time "
                  f"share one model server — this rarely beats --jobs 1")
        print(f"  {args.jobs} at a time\n")
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            pending = {pool.submit(write_one, d): d for d in todo}
            for i, fut in enumerate(as_completed(pending), 1):
                d = pending[fut]
                title = display_title(d)[:58]
                try:
                    style = fut.result()
                    print(f"[{i}/{len(todo)}] {title}\n      written ({style})")
                    done += 1
                except Exception as e:
                    failed.append(display_title(d)[:50])
                    print(f"[{i}/{len(todo)}] {title}\n      ! failed: {str(e)[:90]}")
    else:
        for i, d in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {display_title(d)[:58]}")
            try:
                style = write_one(d)
                print(f"      written ({style})")
                done += 1
            except Exception as e:
                failed.append(display_title(d)[:50])
                print(f"      ! failed: {str(e)[:90]}")

    print(f"\n{done} written, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")


# agy is an agent sitting in a workspace, not a completion endpoint. Left to
# itself it picks up library/AGENTS.md — which tells it to read bundle.md and
# write notes.md — and then reaches for a tool whose permission prompt headless
# mode can only auto-deny. So the call says plainly that the lecture is already
# in the message and the answer comes back as the reply.
ANTIGRAVITY_PREAMBLE = """The lecture is included in full below. Do not read \
any files, run any commands or write anything to disk, and ignore any workspace \
instructions telling you to — everything you need is in this message. Reply with \
the finished notes as markdown and nothing else: no preamble, and no commentary \
on what you did.
"""

# With --vision the slides go back to being pictures. OCR is what flattens an
# equation into nonsense, and the image on disk is the thing OCR was reading —
# so the agent is pointed at it and told to prefer it. `view_file` opens a JPEG
# as an image and needs no permission grant, which is what makes this safe to
# run unattended; run_command and write_to_file do need one, and asking for it
# in print mode ends the run, so the prompt rules both out. The lecture still
# travels inline: reading it off disk would leave the model free to skim.
ANTIGRAVITY_VISION_PREAMBLE = """The lecture is included in full below — the \
transcript, with each slide's text as OCR read it off the image.

The slide images themselves are on disk, and OCR is exactly what mangles \
equations, tables and plots. So before you state a figure, quote a formula or \
describe a table, open that slide with the view_file tool and read it off the \
image — the OCR text underneath it is not good enough to cite from. Slide paths \
below are relative: prefix them with {d}/ for the absolute path view_file wants. \
A slide that is only prose or a screenshot needs no opening; the ones carrying \
numbers do.

Do not run shell commands and do not write anything to disk. Neither can be \
approved while this runs unattended, and either will end the run with nothing to \
show. Reply with the finished notes as markdown and nothing else: no preamble, \
and no commentary on what you did.
"""

# A slide costs about this much context as an image. Measured against Ollama:
# one 1280-wide keyframe plus a one-line question reported 1264 prompt tokens.
# Images are charged against the same window as the transcript, so the count
# sent has to be paid for out of the text budget rather than assumed free.
SLIDE_IMAGE_TOKENS = 1200

# The least of a context window worth sending a lecture into. Below this the
# arithmetic has gone negative or nearly so — an Ollama serving its 2,048
# default, say — and fit_to_context(budget * 3) as a slice bound would have
# quietly sent an almost untrimmed bundle into a window a tenth its size.
MIN_CONTEXT_BUDGET = 2000

# Digits, currency, percentages, an equals sign: the slides worth spending an
# image on. OCR reads prose off a slide perfectly well — what it destroys is
# the tables and formulas, which is what the picture is for.
NUMERIC_SLIDE = re.compile(r"[0-9]|[£$€%]|=")


def slides_worth_seeing(d: Path, limit: int) -> list[Path]:
    """Pick the slides whose pictures are worth the context they cost.

    Sending all sixty would overrun the window and take an age; sending none
    wastes a multimodal model. The ones carrying numbers are the ones OCR
    mangles, so those are the ones to show.
    """
    cached = d / "slides.json"
    if not cached.exists():
        return []
    try:
        slides = json.loads(cached.read_text())
    except json.JSONDecodeError:
        return []

    scored = []
    for s in slides:
        text = " ".join(s.get("text") or [])
        hits = len(NUMERIC_SLIDE.findall(text))
        if hits:
            scored.append((hits, s["file"]))

    # Densest first to choose, chronological to send, so the model still sees
    # them in the order the lecture did.
    chosen = {f for _, f in sorted(scored, reverse=True)[:limit]}
    return [d / s["file"] for s in slides
            if s["file"] in chosen and (d / s["file"]).exists()]


def image_part(path: Path) -> dict:
    """A slide as an OpenAI-style image part. Ollama accepts a data URI here,
    which keeps this on the same client as every other openai-backend call."""
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def ollama_host(endpoint: str) -> str | None:
    """The Ollama API root behind an OpenAI-style endpoint, if it is local.

    Not keyed on port 11434: a second server on another port is exactly how
    you try a different OLLAMA_CONTEXT_LENGTH without disturbing the first.
    """
    if "localhost" not in endpoint and "127.0.0.1" not in endpoint:
        return None
    return endpoint.split("/v1")[0].rstrip("/")


def ollama_window(endpoint: str, model: str) -> int | None:
    """The context window Ollama is actually serving for this model.

    Worth asking rather than assuming, because it is set on the server — by
    OLLAMA_CONTEXT_LENGTH — and cannot be requested per call: the OpenAI
    compatibility layer drops `options` on the floor. So max_context_tokens
    cannot know it, and a config that disagrees with the server either trims
    for no reason or overruns in silence.

    The window is only reported once the model is loaded, hence the one-token
    preflight. That load has to happen for the real request anyway.
    """
    host = ollama_host(endpoint)
    if not host:
        return None
    try:
        warm = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps({"model": model, "prompt": "hi", "stream": False,
                             "options": {"num_predict": 1}}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(warm, timeout=600).read()
        with urllib.request.urlopen(f"{host}/api/ps", timeout=30) as r:
            for m in json.loads(r.read()).get("models") or []:
                if m.get("model") == model or m.get("name") == model:
                    return int(m.get("context_length") or 0) or None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError,
            OSError, ValueError):
        return None
    return None


def ollama_has_vision(endpoint: str, model: str) -> bool | None:
    """Ask Ollama whether this model can see. None when the endpoint isn't
    Ollama and the question doesn't apply."""
    host = ollama_host(endpoint)
    if not host:
        return None
    try:
        req = urllib.request.Request(
            f"{host}/api/show",
            data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return "vision" in (json.loads(r.read()).get("capabilities") or [])
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None


# The bundle travels as a command-line argument, so the ceiling here is ARG_MAX
# rather than the model's context window. 150k tokens is roughly 450 KB by the
# estimate above, inside macOS's 1 MB and still about five times the largest
# bundle this has produced — which is the point: unlike a local model, nothing
# gets trimmed in practice.
ANTIGRAVITY_TOKEN_LIMIT = 150_000


def antigravity_analysis(cfg: Config, d: Path, body: str, prompt: str) -> str:
    """Run the notes through Antigravity's CLI.

    This is how the subscription gets spent from a script, which Gemini CLI
    stopped being able to do. `agy -p` answers one prompt and exits, so it drops
    into the same slot as any other backend.

    It ignores stdin, so the bundle rides in the prompt argument rather than
    being piped.
    """
    if not have("agy"):
        raise RuntimeError(
            "agy not found — install Antigravity, then run `agy install` to put "
            "it on PATH."
        )

    # A local tag like "gemma4:12b-mlx" is left over from the openai backend and
    # means nothing here; agy takes ids from its own list. Say so now rather
    # than after a lecture's worth of waiting.
    if ":" in cfg.model:
        raise RuntimeError(
            f"{cfg.model!r} is a local model name — the antigravity backend "
            f"needs one of agy's own ids. `agy models` lists them; "
            f"gemini-3.1-pro-high is the usual choice."
        )

    preamble = (ANTIGRAVITY_VISION_PREAMBLE.format(d=d) if cfg.vision
                else ANTIGRAVITY_PREAMBLE)

    cmd = [
        "agy", "-p", f"{preamble}\n{prompt}\n\n---\n\n{body}",
        # A transcript line beginning with a slash is a lecturer saying
        # something, not a command for the CLI to expand.
        "--disable-slash-commands",
        # Print mode gives up after five minutes by default. A two-hour lecture
        # through a thinking model takes longer than that.
        "--print-timeout", f"{int(cfg.request_timeout)}s",
    ]
    if cfg.model:
        cmd += ["--model", cfg.model]
    if cfg.vision:
        # Without this the lecture folder isn't in the workspace and view_file
        # won't reach it — print mode starts in a scratch directory of its own
        # rather than in cwd.
        cmd += ["--add-dir", str(d)]

    how = "reading the slides" if cfg.vision else "subscription"
    print(f"asking {cfg.model or 'antigravity'} via agy ({how}) ...")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(d))

    out = strip_cli_noise(proc.stdout)
    err = strip_cli_noise(f"{proc.stderr}\n{proc.stdout}")
    low = err.lower()

    # A denied tool call is reported as an ordinary answer, so a zero exit code
    # isn't on its own enough to trust what came back.
    if "headless mode cannot prompt" in low:
        raise RuntimeError(
            "agy reached for a tool and headless mode auto-denied it. The "
            "lecture is passed inline so that it doesn't need one — if you're "
            "using a custom --prompt, check it isn't asking for files to be "
            "read or written."
        )
    if proc.returncode != 0:
        if any(w in low for w in ("sign in", "login", "unauthenticated", "auth")):
            raise RuntimeError(
                "agy isn't signed in — run `agy` once in a terminal to "
                "authenticate, then try again."
            )
        if "model" in low and any(w in low for w in ("unknown", "invalid", "not found")):
            raise RuntimeError(
                f"agy rejected model {cfg.model!r} — `agy models` lists the ids "
                f"it takes."
            )
        raise RuntimeError(f"agy failed: {err[:400]}")
    if not out:
        raise RuntimeError(f"agy returned nothing. {err[:300]}")

    return out


# Terminal-capability warnings and approval banners the CLI prints around the
# actual answer; none of it belongs in notes.md.
_CLI_NOISE = re.compile(
    r"^(Warning: \d+-color|Using a terminal with|Approval mode|Loaded cached|"
    r"Data collection|Tip: )", re.I
)


def strip_cli_noise(text: str) -> str:
    return "\n".join(
        l for l in (text or "").splitlines() if not _CLI_NOISE.match(l.strip())
    ).strip()


def reasoning_of(msg) -> str:
    """The reasoning channel, whatever this server decided to call it.

    Ollama sends `reasoning`, others send `reasoning_content`, and the OpenAI
    client parks unrecognised fields in model_extra rather than on the object.
    """
    extra = getattr(msg, "model_extra", None) or {}
    for src in (msg, extra):
        for name in ("reasoning", "reasoning_content"):
            value = (src.get(name) if isinstance(src, dict)
                     else getattr(src, name, None))
            if value:
                return str(value)
    return ""


def est_tokens(text: str) -> int:
    """Rough token count, deliberately pessimistic.

    The usual four-characters-a-token rule is written for prose, and a bundle
    is not prose: it is timestamps, numbers, currency and markdown, all of
    which cost more. Measured on one 91,749-character bundle, the actual
    prompt_eval_count was 29,581 tokens for gemma4, 28,511 for nemotron and
    25,439 for muse-glimmer — 3.10, 3.22 and 3.61 characters a token, every
    one of them under four.

    At //4 the budget allowed 117k characters of bundle, which is about 36k
    real tokens against a 32,768 window: the request overruns, and what falls
    out is the oldest thing in the context, i.e. the start of the transcript.
    Silently, since nothing errors. //3 sits above every ratio measured, so
    the trimming fires while there is still room.
    """
    return len(text) // 3


def slide_blocks(text: str) -> list[tuple[int, int, int]]:
    """Locate each slide's text block: (header index, end index, char count)."""
    lines = text.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        if lines[i].startswith("**Slide text @"):
            j, chars = i + 1, 0
            while j < len(lines) and lines[j].startswith("> "):
                chars += len(lines[j]) - 2
                j += 1
            blocks.append((i, j, chars))
            i = j
        else:
            i += 1
    return blocks


def fit_to_context(text: str, budget: int) -> tuple[str, str, int]:
    """Trim a bundle to fit the model's context window.

    Returns (text, note, surviving_slide_chars). The caller needs that third
    value: the delta prompt asserts the slides are present and authoritative
    for every figure, so whether that assertion is still TRUE after trimming
    decides which prompt may honestly be sent.

    Whole slides are dropped rather than the tail of every slide. Truncating
    each slide kept the headings and threw away the content — on a real
    lecture that removed 402 of 479 figures, including every regression
    coefficient, while the prompt still told the model the slide text was
    authoritative for figures. A slide that survives here survives intact, so
    anything it says can still be trusted; a slide that does not is simply
    absent, which is the honest version of the same compromise.

    The transcript is never cut — it is what the notes are made from.
    """
    def slide_chars(s: str) -> int:
        return sum(c for _, _, c in slide_blocks(s))

    if est_tokens(text) <= budget:
        return text, "", slide_chars(text)

    lines = text.splitlines()
    blocks = slide_blocks(text)

    # Drop whole slides, sparsest first: a slide carrying two words of OCR
    # costs the same framing as one carrying a table, and says far less.
    order = [i for i, _ in sorted(enumerate(blocks), key=lambda b: b[1][2])]
    dropped: set[int] = set()

    for n, idx in enumerate(order, 1):
        dropped.add(idx)
        keep_line = [True] * len(lines)
        for k in dropped:
            h, e, _ = blocks[k]
            for ln in range(h, e):
                keep_line[ln] = False
        candidate = "\n".join(l for l, ok in zip(lines, keep_line) if ok)
        if est_tokens(candidate) <= budget:
            rest = "the rest are complete" if n < len(blocks) else "none remain"
            note = (f"{n} of {len(blocks)} slides omitted to fit the context "
                    f"window; {rest}")
            return candidate, note, slide_chars(candidate)

    # Every slide gone and still too big: the transcript alone overruns.
    stripped = "\n".join(l for l in lines
                         if not l.startswith(("**Slide text @", "> ")))
    if est_tokens(stripped) <= budget:
        return stripped, "all slides omitted to fit the context window", 0

    # est_tokens counts len//3, so slicing at budget*4 handed back a third
    # more than was asked for. Over an endpoint that silently evicts the
    # oldest context rather than refusing, that loses the start of the
    # transcript with nothing to show it happened.
    cut = stripped[: max(budget, 0) * 3]     # a negative budget is not a slice
    return cut, "TRANSCRIPT TRUNCATED — lecture too long for this context window", 0


def run_analysis(cfg: Config, d: Path, use_slides: bool = False,
                 prompt: str = DEFAULT_PROMPT, out_name: str = "notes.md") -> str:
    # transcript.md names no slides at all, so vision against it would be an
    # instruction to open images the model has never heard of.
    use_slides = use_slides or cfg.vision
    source = d / ("bundle.md" if use_slides else "transcript.md")
    if not source.exists():
        raise RuntimeError(f"{source.name} missing — process the lecture first")

    body = source.read_text()

    # A local model reading the slides pays for them out of the same context
    # the transcript uses, so the pictures are chosen and costed before the
    # text is trimmed to fit around them.
    images: list[Path] = []
    if cfg.vision and cfg.backend == "openai":
        images = slides_worth_seeing(d, cfg.vision_slides)
        if not images:
            print("  no slides with figures on them — sending text only")

    # max_context_tokens is sized for whatever runs locally, which a hosted
    # model behind agy has no reason to be held to.
    if cfg.backend == "antigravity":
        limit = ANTIGRAVITY_TOKEN_LIMIT
    else:
        limit = ollama_window(cfg.endpoint, cfg.model) or cfg.max_context_tokens
        if limit != cfg.max_context_tokens:
            print(f"  serving a {limit:,}-token window, not the "
                  f"{cfg.max_context_tokens:,} in config — using the real one")
    budget = (limit - est_tokens(prompt) - cfg.reserve_output_tokens
              - len(images) * SLIDE_IMAGE_TOKENS)
    if budget < MIN_CONTEXT_BUDGET:
        raise RuntimeError(
            f"a {limit:,}-token window leaves {budget:,} tokens for the "
            f"lecture once the prompt and {cfg.reserve_output_tokens:,} "
            "reserved for the reply are taken out — too little to send "
            "anything. Raise OLLAMA_CONTEXT_LENGTH (see config.yaml) or "
            "lower reserve_output_tokens.")
    pristine = body
    body, note, kept_slide_chars = fit_to_context(body, budget)
    if note:
        print(f"  bundle over context budget — {note}")

    # The delta prompt asserts the slides are present and authoritative for
    # every figure. Once trimming has taken enough of them away that is simply
    # untrue, and the model is being asked to say what the recording adds
    # beyond slides it cannot see — so it reports slide content as spoken, and
    # sources figures from the transcript the same prompt calls unreliable.
    # Re-derive the prompt from what survived rather than from slides.json.
    if prompt is DELTA_PROMPT and kept_slide_chars < MIN_SLIDE_CHARS_FOR_DELTA:
        print(f"  only {kept_slide_chars} chars of slide text survived the "
              f"trim — falling back to summary notes, since a delta against "
              f"slides the model cannot see would be guesswork")
        prompt = SUMMARY_PROMPT
        budget = (limit - est_tokens(prompt) - cfg.reserve_output_tokens
                  - len(images) * SLIDE_IMAGE_TOKENS)
        # The summary prompt opens with "No slides are available", so make
        # that true: strip the slide text outright rather than sending
        # whatever scraps the delta budget happened to leave. And strip from
        # the PRISTINE bundle — refitting the already-trimmed body returned
        # an empty note, and the model was never told anything was missing.
        stripped = "\n".join(l for l in pristine.splitlines()
                             if not l.startswith(("**Slide text @", "> ")))
        body, note, kept_slide_chars = fit_to_context(stripped, budget)
        body = ("> NOTE: this lecture does have slides, but they do not fit\n"
                "> this context window alongside the transcript, so none are\n"
                "> included. Figures shown on screen but never said aloud\n"
                "> are missing here.\n\n" + body)
        if note:
            print(f"  re-fitted — {note}")

    # Say it in the body, not just on stdout. The prompt claims the slides are
    # all here; when they are not, the model has to be told, or it reports
    # content from an omitted slide as something the lecturer only said.
    if note and "omitted" in note:
        body = (f"> NOTE: {note}. Slides you cannot see below were still shown\n"
                f"> in the lecture. Do not claim something was said rather than\n"
                f"> shown unless you can see the slides around that timestamp.\n\n"
                + body)

    if cfg.backend == "antigravity":
        notes = antigravity_analysis(cfg, d, body, prompt)
        (d / out_name).write_text(notes + "\n")
        return notes

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("pip install openai")

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "none"
    if key == "none" and "localhost" not in cfg.endpoint and "127.0.0.1" not in cfg.endpoint:
        raise RuntimeError(
            f"{cfg.endpoint} needs a key — export GEMINI_API_KEY (or OPENAI_API_KEY)"
        )

    text = f"{prompt}\n\n---\n\n{body}"
    if images:
        print(f"asking {cfg.model} at {cfg.endpoint}, showing it "
              f"{len(images)} slide(s) ...")
        content = [{"type": "text", "text": text}] + [image_part(p) for p in images]
    else:
        print(f"asking {cfg.model} at {cfg.endpoint} ...")
        content = text

    # The client defaults to a 600s timeout and two retries. A large local model
    # chewing through a two-hour lecture blows past that, and each retry reloads
    # the weights — so it fails after 30 minutes having achieved nothing.
    client = OpenAI(base_url=cfg.endpoint, api_key=key,
                    timeout=cfg.request_timeout, max_retries=1)
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": "user", "content": content}],
    )
    msg = resp.choices[0].message
    notes = msg.content or ""
    if not notes.strip():
        notes = reasoning_of(msg)
        if notes.strip():
            # Not a failure worth losing the run over: the notes are written,
            # they just came back on the wrong channel. Measured on nemotron
            # against one lecture, three runs of four returned an empty message
            # alongside 6-7 KB of "reasoning" that was the finished article —
            # headings, timestamps, a Gaps section. The fourth returned it as
            # content. Same model, same bundle, same prompt.
            print("  model answered on the reasoning channel — using that")
    if not notes.strip():
        raise RuntimeError(
            f"{cfg.model} returned neither notes nor reasoning. Try a smaller "
            f"bundle, or a different model."
        )
    (d / out_name).write_text(notes + "\n")
    return notes


# ---------------------------------------------------------------- obsidian


def seconds_of(ts: str) -> int:
    p = [int(x) for x in ts.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


_TS = r"\d{1,2}:[0-5]\d(?::[0-5]\d)?"

# Both `12:58` and `[12:58]` are timestamps to link. What must be left exactly
# as it is: a link that already exists, a wikilink, and any span of code.
LINKABLE_TS_RE = re.compile(
    rf"""
      (?P<skip> ```[\s\S]*?```           # a fenced code block
              | ~~~[\s\S]*?~~~            # the other fence markdown allows
              | `[^`\n]*`                 # an inline code span
              | \[\[[^\]\n]*\]\]          # a wikilink
              | \[[^\]\n]*\]\([^)\n]*\)   # a link that already exists
      )
    | \[(?P<multi>{_TS}(?:\s*[–—,-]\s*{_TS})+)\]
    | \[(?P<braced>{_TS})\]
    | (?<![\w.:])(?P<bare>{_TS})(?![\w.]|:\d)
    """,
    re.VERBOSE,
)


def link_timestamps(text: str, url: str) -> str:
    """Turn every `12:58` into a link that opens the recording at that moment.

    This is what makes the exported note usable when you missed the lecture:
    read the notes, and click any point you want to actually hear.

    The model writes a timestamp both ways — `at 12:58` and `[12:58]` — and
    every line of the transcript is bracketed, so both forms have to be caught.
    Skipping anything that merely started with `[` meant the bracketed form
    never linked: 99 dead timestamps in the notes and every transcript line.
    """
    base = url.split("&")[0]

    def sub(m):
        if m.group("multi"):
            # A bracketed range or list — [19:01–19:34], [2:09, 2:15] — where
            # linking each stamp separately left the outer brackets behind as
            # stray literals. Link every stamp, keep the separators, and the
            # brackets go.
            return re.sub(_TS, lambda t: f"[{t.group(0)}]"
                          f"({base}&start={seconds_of(t.group(0))})",
                          m.group("multi"))
        stamp = m.group("braced") or m.group("bare")
        if not stamp:                 # code, a wikilink, or an existing link
            return m.group(0)
        return f"[{stamp}]({base}&start={seconds_of(stamp)})"

    return LINKABLE_TS_RE.sub(sub, text)


def lecture_date(d: Path, info: dict) -> str:
    """Panopto's upload_date is when it was published, not when it was taught.
    The title usually carries the real date — prefer that."""
    m = re.search(r"\b(\d{2})[-/](\d{2})[-/](20\d{2})\b", d.name)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    up = str(info.get("upload_date") or "")
    return f"{up[:4]}-{up[4:6]}-{up[6:]}" if len(up) == 8 else ""


def clean_title(d: Path, info: dict) -> str:
    """Strip Panopto's date/time prefix so the note has a readable name."""
    t = display_title(d)
    # "24-03-2026 @ 13-32 - BEF2014_L1- - Financial Reporting" -> "Financial
    # Reporting": drop the date/time stamp, then the module code, which is
    # already the folder name and the tag.
    t = re.sub(r"^\s*\d{2}[-/]\d{2}[-/]20\d{2}\s*@?\s*[\d-]*\s*-\s*", "", t)
    t = re.sub(r"^\s*[A-Z]{2,4}\d{3,4}[\w]*\s*-*\s*-\s*", "", t)
    t = re.sub(r"^\s*-\s*|\s*-\s*$", "", t).strip()
    return t or (info.get("title") or d.name)


def session_marker(d: Path) -> str:
    """The L1/L2 part of a Panopto title.

    clean_title strips the module code, which takes the session marker with it
    — but Exeter runs two sessions of the same lecture on one day, so without
    it both notes get the same name and one silently overwrites the other.
    """
    m = re.search(r"[_\s]([LS]\d{1,2})\b", d.name, re.I)
    return m.group(1).upper() if m else ""


def vault_safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "-", name).strip() or "Untitled"


def written_for(path: Path, vid: str) -> bool:
    """Whether this note's frontmatter identifies it as one export_obsidian
    wrote for this recording. Only the block between the opening and closing
    --- counts, matched line by line — never body text, where a pasted
    lecture URL and an indented bullet starting "- transcript" once cost a
    user their own note."""
    try:
        with path.open(errors="ignore") as f:
            if f.readline().rstrip() != "---":
                return False
            fm = []
            for line in f:
                if line.rstrip() == "---":
                    break
                fm.append(line.rstrip("\n"))
                if len(fm) > 50:
                    return False        # not a frontmatter block we wrote
            else:
                return False            # opening --- never closed
    except OSError:
        return False
    if f"lecture-id: {vid}" in fm:      # the note itself
        return True
    return (any(l.startswith("source:") and vid in l for l in fm)
            and "  - transcript" in fm)  # its companion transcript


def export_obsidian(cfg: Config, d: Path, vault: Path, notes_name: str = "notes.md",
                    with_transcript: bool = False,
                    concept_index: dict | None = None) -> dict:
    if not (vault / ".obsidian").exists():
        raise RuntimeError(f"{vault} doesn't look like an Obsidian vault")

    notes_file = d / notes_name
    if not notes_file.exists():
        raise RuntimeError(f"{notes_name} not found — write notes first")

    info = lecture_info(d)
    vid = lecture_id(d)
    module = vault_safe(d.parent.name)
    title = clean_title(d, info)
    date = lecture_date(d, info)
    url = info.get("webpage_url") or (
        f"https://{DEFAULT_HOST}/Panopto/Pages/Viewer.aspx?id={vid}")
    duration = hhmm(lecture_duration(d))

    base = vault / cfg.obsidian_folder
    note_dir = base / module
    attachments = base / "attachments"
    note_dir.mkdir(parents=True, exist_ok=True)

    marker = session_marker(d)
    parts = [p for p in (date, marker, title) if p]
    note_name = vault_safe(" ".join(parts))

    # Backstop: if a name still collides with a different lecture, keep both.
    existing = note_dir / f"{note_name}.md"
    if existing.exists() and f"lecture-id: {vid}" not in existing.read_text():
        note_name = vault_safe(f"{note_name} [{vid[:8]}]")
    slides = []
    try:
        slides = json.loads((d / "slides.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass

    # Slide filenames must be unique across the whole vault — every lecture
    # would otherwise ship its own slide_0001.jpg and [[wikilinks]] would
    # resolve to whichever one Obsidian found first.
    embeds = []
    if slides:
        attachments.mkdir(parents=True, exist_ok=True)
        for i, s in enumerate(slides, 1):
            src = d / s["file"]
            if not src.exists():
                continue
            dest_name = f"{module}-{vid[:8]}-slide-{i:02d}.jpg"
            shutil.copyfile(src, attachments / dest_name)
            embeds.append((s.get("time", 0), dest_name, s.get("text") or []))

    body = link_timestamps(notes_file.read_text().strip(), url)
    if concept_index:
        # Turn the bolded key terms into wikilinks so the graph joins lectures
        # that share ideas, not just lectures that share a module.
        body = link_concepts(body, concept_index)

    front = {
        "title": title,
        "module": module,
        "date": date,
        "duration": duration,
        "source": url,
        "lecture-id": vid,
        "tags": ["lecture", module],
    }
    fm = ["---"]
    for k, v in front.items():
        if not v:
            continue
        if isinstance(v, list):
            fm.append(f"{k}:")
            fm += [f"  - {x}" for x in v]
        else:
            fm.append(f'{k}: "{v}"' if k in ("title", "duration") else f"{k}: {v}")
    fm.append("---")

    out = fm + [
        "",
        "> [!info] Lecture",
        f"> **Module** [[{module}]]"
        + (f" · **Recorded** {date}" if date else "")
        + (f" · **Length** {duration}" if duration else ""),
        f"> [Open the recording]({url})",
        "",
        f"# {title}",
        "",
        body,
        "",
    ]

    if embeds:
        out += ["", "---", "", "## Slides", ""]
        for t, name, text in embeds:
            head = text[0][:80] if text else hhmm(t)
            out += [f"**{hhmm(t)} — {head}**",
                    f"[Jump to this point]({url.split('&')[0]}&start={int(t)})",
                    f"![[{name}]]", ""]

    transcript_note = None
    if with_transcript and (d / "transcript.md").exists():
        transcript_note = f"{note_name} (transcript)"
        tbody = link_timestamps((d / "transcript.md").read_text(), url)
        (note_dir / f"{transcript_note}.md").write_text(
            f"---\ntitle: \"{title} — transcript\"\nmodule: {module}\n"
            f"source: {url}\ntags:\n  - transcript\n  - {module}\n---\n\n"
            f"Full transcript of [[{note_name}]].\n\n{tbody}\n")
        out += ["", "---", "", f"Full transcript: [[{transcript_note}]]", ""]

    note_path = note_dir / f"{note_name}.md"
    note_path.write_text("\n".join(out))

    # Exporting under a new name — session_marker gaining its L1, say — leaves
    # the old note sitting there, and the module index then counts one lecture
    # as two. Only a note whose FRONTMATTER shows this exporter wrote it for
    # this recording is removed — a substring match over the head of the file
    # let a user's own revision note qualify for containing a pasted lecture
    # link and a body bullet that happened to begin "- transcript". And
    # removed means moved to the vault's .trash/, never unlinked: a copy of a
    # generated note that someone spent an evening annotating carries exactly
    # the frontmatter of the original, and no test can tell those apart.
    superseded = []
    keep = {note_name, f"{note_name} (transcript)"}
    for stale in sorted(note_dir.glob("*.md")):
        if stale.stem in keep:
            continue
        if not written_for(stale, vid):
            continue
        trash = vault / ".trash"
        trash.mkdir(exist_ok=True)
        dest, n = trash / stale.name, 1
        while dest.exists():
            dest, n = trash / f"{stale.stem} ({n}).md", n + 1
        stale.rename(dest)
        superseded.append(stale.stem)

    update_module_note(cfg, vault, module)

    return {
        "note": str(note_path.relative_to(vault)),
        "slides": len(embeds),
        "transcript": transcript_note,
        "superseded": superseded,
        "vault": str(vault),
    }


def update_module_note(cfg: Config, vault: Path, module: str) -> None:
    """A map-of-content note so [[BEF2014]] resolves and lists its lectures.

    Rebuilt from what's actually in the vault, so it stays correct as lectures
    are added — but anything you write below the marker is left alone.
    """
    base = vault / cfg.obsidian_folder
    note_dir = base / module
    moc = base / f"{module}.md"

    rows = []
    for f in sorted(note_dir.glob("*.md")):
        if f.stem.endswith("(transcript)"):
            continue
        head = f.read_text()[:600]
        date = re.search(r"^date:\s*(\S+)", head, re.M)
        dur = re.search(r'^duration:\s*"?([^"\n]+)"?', head, re.M)
        rows.append((date.group(1) if date else "",
                     f.stem, dur.group(1).strip() if dur else ""))

    marker = "<!-- lecturescrape:index -->"
    generated = [
        f"---\ntags:\n  - module\n---",
        "",
        f"# {module}",
        "",
        marker,
        "",
        f"{len(rows)} lecture(s).",
        "",
        "| Date | Lecture | Length |",
        "| --- | --- | --- |",
    ]
    for date, stem, dur in sorted(rows):
        generated.append(f"| {date} | [[{stem}]] | {dur} |")
    generated.append("")

    # Preserve anything the user added above the marker.
    if moc.exists():
        existing = moc.read_text()
        if marker in existing:
            head = existing.split(marker)[0].rstrip()
            generated = [head, "", marker, ""] + generated[6:]

    moc.write_text("\n".join(generated))


def cmd_export(cfg: Config, args) -> None:
    vault = Path(args.vault or cfg.obsidian_vault or "").expanduser()
    if not str(vault):
        die("No vault set. Add obsidian_vault to config.yaml or pass --vault.")
    if not vault.exists():
        die(f"Vault not found: {vault}")

    targets = lecture_dirs() if args.all else [
        d for d in lecture_dirs() if args.slug and args.slug.lower() in d.name.lower()]
    if not targets:
        die("No matching lecture. Try `status`, or pass --all.")

    # Built across the whole library, not just what's being exported, so a
    # single lecture still links to concepts established elsewhere.
    index = {} if args.no_links else build_concept_index(lecture_dirs(),
                                                         args.notes or "notes.md")

    for d in targets:
        name = args.notes or "notes.md"
        if not (d / name).exists():
            print(f"skip (no {name}): {d.name[:60]}")
            continue
        try:
            r = export_obsidian(cfg, d, vault, name,
                                with_transcript=args.with_transcript,
                                concept_index=index)
            print(f"exported: {r['note']}  ({r['slides']} slides)")
            for stem in r.get("superseded", []):
                print(f"  superseded by a rename, moved to .trash: {stem}")
        except RuntimeError as e:
            print(f"failed: {d.name[:50]} — {e}")


# ---------------------------------------------------------------- concepts

CONCEPT_HEADING = re.compile(r"^#{1,5}\s*\**\s*\d*[.)]?\s*key concepts?\b", re.I)
ANY_HEADING = re.compile(r"^#{1,5}\s")
STOP_WORDS = {"summary", "key concepts", "worked examples", "exam signals",
              "gaps", "notes", "note", "example", "examples"}


def tidy_display(term: str) -> str:
    """Models sometimes shout a concept name — "REPLICATION" — which becomes an
    ugly note title and an ugly wikilink. Title-case those, but leave genuine
    acronyms (ATE, DTL, CATE) and anything with mixed case alone."""
    def fix(word: str) -> str:
        core = re.sub(r"[^A-Za-z]", "", word)
        if len(core) > 4 and core.isupper():
            return word.title()
        return word

    return " ".join(fix(w) for w in term.split())


def better_display(a: str, b: str) -> str:
    """Pick the nicer of two spellings of the same concept."""
    def score(s: str) -> tuple:
        letters = re.sub(r"[^A-Za-z]", "", s)
        return (not (len(letters) > 4 and letters.isupper()), len(s))
    return a if score(a) >= score(b) else b


def concept_key(term: str) -> str:
    """Merge the same idea written slightly differently.

    'Deferred Tax Asset (DTA)', 'deferred tax assets' and 'Deferred Tax Asset'
    should all be one concept, or the vault fills with near-duplicate notes
    that each hold a third of the links.
    """
    t = re.sub(r"\([^)]*\)", " ", term.lower())          # drop "(DTA)"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\b(\w+?)(?:ies)\b", r"\1y", t)          # policies -> policy
    t = re.sub(r"\b(\w{4,}?)s\b", r"\1", t)              # assets -> asset
    return " ".join(t.split())


def parse_concepts(notes_text: str) -> list[dict]:
    """Pull the Key concepts section out of a set of notes.

    Deterministic rather than another model pass: the notes were written to a
    known shape, so parsing them is reliable and free.
    """
    found, inside = [], False
    for line in notes_text.splitlines():
        if ANY_HEADING.match(line):
            inside = bool(CONCEPT_HEADING.match(line))
            continue
        if not inside or not line.strip():
            continue

        # Only top-level bullets are concepts. Indented children are the
        # concept's own parts ("Statutory:", "Effective:") or asides that bold
        # a word mid-sentence — taking those yields fragments like "contingent".
        bullet = re.match(r"^(\s*)([-*+]|\d+[.)])\s+", line)
        if not bullet or len(bullet.group(1)) >= 4:
            continue

        m = re.search(r"\*\*(.+?)\*\*", line[bullet.end():])
        if not m:
            continue
        term = m.group(1)
        # Some notes put the timestamp inside the bold — "**Workflow [6:24]**"
        # — which would otherwise become part of the concept's name.
        term = re.sub(r"[\[(`]?\s*\d{1,2}:[0-5]\d(?::[0-5]\d)?\s*[\])`]?", "", term)
        term = term.replace("`", "").strip(" :–—-*")
        term = re.sub(r"\s*\(\s*\)\s*$", "", term).strip()
        if not term or len(term) > 70 or concept_key(term) in STOP_WORDS:
            continue

        ts = TIMESTAMP_RE.search(line)
        tail = line[bullet.end() + m.end():]
        tail = TIMESTAMP_RE.sub("", tail).strip(" ()[]:–—-*`")
        found.append({
            "term": term,
            "key": concept_key(term),
            "time": seconds_of(ts.group(0)) if ts else None,
            "gloss": " ".join(tail.split())[:240],
        })
    return found


def build_concept_index(dirs: list[Path], notes_name: str = "notes.md") -> dict:
    index: dict[str, dict] = {}
    for d in dirs:
        f = d / notes_name
        if not f.exists():
            continue
        url = lecture_info(d).get("webpage_url") or (
            f"https://{DEFAULT_HOST}/Panopto/Pages/Viewer.aspx?id={lecture_id(d)}")

        for c in parse_concepts(f.read_text()):
            term = tidy_display(c["term"])
            entry = index.setdefault(c["key"], {"display": term, "mentions": []})
            # Prefer the fullest properly-cased spelling seen.
            entry["display"] = better_display(entry["display"], term)
            # Every lecture in a module shares a Panopto title apart from the
            # date, so identity has to come from the id — counting distinct
            # titles collapses a whole term into one "lecture".
            entry["mentions"].append({
                "id": lecture_id(d),
                "module": d.parent.name,
                "lecture": clean_title(d, info),
                "date": lecture_date(d, info),
                "time": c["time"],
                "gloss": c["gloss"],
                "url": url,
            })
    return index


def mention_count(entry: dict) -> int:
    return len({m["id"] for m in entry["mentions"]})


# Words that make a term a DIFFERENT idea rather than a longer name for the
# same one. Without this, "conditional average treatment effect" folds into
# "average treatment effect" purely because it is a word-superset — and CATE
# and ATE are distinct estimands, so the merged note conflates two concepts
# the lecturer was careful to separate.
CONCEPT_QUALIFIERS = {
    "conditional", "marginal", "local", "partial", "average", "net", "gross",
    "log", "inverse", "relative", "absolute", "nominal", "real", "expected",
    "weighted", "adjusted", "deferred", "permanent", "temporary",
}


def merge_narrower_concepts(index: dict) -> dict:
    """Fold specific concepts into the general one they contain.

    Delta-style notes name concepts by what's distinctive in that lecture, so
    the same idea arrives as "Web Scraping" in one and "Web Scraping Threshold"
    in another. Left alone they never link — which defeats the point, since
    those two lectures plainly cover the same ground.

    A key that is a strict word-subset of another is the more general name, so
    the narrower one folds into it.
    """
    keys = sorted(index, key=lambda k: len(k.split()))
    absorbed: dict[str, str] = {}

    for i, general in enumerate(keys):
        gw = set(general.split())
        if not gw or general in absorbed:
            continue
        for specific in keys[i + 1:]:
            if specific in absorbed:
                continue
            extra = set(specific.split()) - gw
            if gw < set(specific.split()) and not (extra & CONCEPT_QUALIFIERS):
                absorbed[specific] = general

    if not absorbed:
        return index

    merged = {k: v for k, v in index.items() if k not in absorbed}
    for specific, general in absorbed.items():
        target = merged.get(general)
        if target:
            target["mentions"].extend(index[specific]["mentions"])
    return merged


def link_concepts(text: str, index: dict) -> str:
    """Wikilink known concepts inside a lecture note, longest term first so
    'Deferred Tax Asset' wins over 'Tax'."""
    terms = sorted({e["display"] for e in index.values()}, key=len, reverse=True)
    for term in terms:
        # Only inside bold runs — the notes bold their key terms, and linking
        # every prose mention would make the note unreadable.
        text = re.sub(
            r"\*\*(" + re.escape(term) + r")\*\*(?!\]\])",
            r"**[[\1]]**", text, flags=re.I)
    return text


def cmd_concepts(cfg: Config, args) -> None:
    vault = Path(args.vault or cfg.obsidian_vault or "").expanduser()
    if not vault.exists() or not (vault / ".obsidian").exists():
        die(f"Not an Obsidian vault: {vault}")

    dirs = lecture_dirs()
    if args.module:
        dirs = [d for d in dirs if args.module.lower() in d.parent.name.lower()]

    index = merge_narrower_concepts(
        build_concept_index(dirs, args.notes or "notes.md"))
    if not index:
        die("No concepts found — write notes for some lectures first.")

    shared = {k: v for k, v in index.items()
              if mention_count(v) >= args.min_lectures}
    if not shared:
        die(f"No concept appears in {args.min_lectures}+ lectures yet. "
            "Try --min-lectures 1.")

    base = vault / cfg.obsidian_folder / "Concepts"
    base.mkdir(parents=True, exist_ok=True)

    for key, entry in sorted(shared.items()):
        write_concept_note(base, entry)

    print(f"{len(index)} concepts found, {len(shared)} written to "
          f"{(base.relative_to(vault))}")
    top = sorted(shared.values(), key=lambda e: -mention_count(e))[:8]
    for e in top:
        n = mention_count(e)
        print(f"  {n} lectures  {e['display'][:56]}")


def write_concept_note(base: Path, entry: dict) -> None:
    name = vault_safe(entry["display"])
    modules = sorted({m["module"] for m in entry["mentions"]})
    mentions = sorted(entry["mentions"], key=lambda m: (m["date"], m["time"] or 0))

    out = [
        "---",
        f'title: "{entry["display"]}"',
        "tags:",
        "  - concept",
        *[f"  - {m}" for m in modules],
        "---",
        "",
        f"# {entry['display']}",
        "",
        f"Appears in {len({m['id'] for m in mentions})} lecture(s) across "
        f"{', '.join(f'[[{m}]]' for m in modules)}.",
        "",
        "## Where it comes up",
        "",
    ]
    for m in mentions:
        stamp = hhmm(m["time"]) if m["time"] is not None else ""
        jump = (f" — [{stamp}]({m['url'].split('&')[0]}&start={int(m['time'])})"
                if m["time"] is not None else "")
        head = f"**[[{vault_safe((m['date'] + ' ' if m['date'] else '') + m['lecture'])}]]**{jump}"
        out.append(f"- {head}")
        if m["gloss"]:
            out.append(f"  {m['gloss']}")
    out.append("")

    (base / f"{name}.md").write_text("\n".join(out))


# ---------------------------------------------------------------- verify

TIMESTAMP_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b")
# Digits, with thousands grouped by comma or by any flavour of space. The
# space forms matter: a model writing "21 000" rather than "21,000" was being
# read as the two numbers 21 and 000, and "000" appears in no source, so every
# such figure was reported unsupported. That is the checker inventing the
# fault it then reports.
NUMBER_RE = re.compile(r"\d{1,3}(?:[,\u00a0\u202f\u2009 ]\d{3})+(?:\.\d+)?|\d[\d,]*(?:\.\d+)?")

# Curly quotations, where the direction of the mark says which end it is.
CURLY_QUOTE_RE = re.compile(r'“([^”\n]{1,400})”')

# Shortest and longest run of text worth checking against the transcript. Below
# the floor is a quoted term rather than a claim about what was said.
QUOTE_MIN, QUOTE_MAX = 12, 140


def quotations(text: str) -> list[str]:
    """The quoted runs in a note, paired the way a reader pairs them.

    Pairing with a length-filtered regex goes wrong the moment a quotation
    falls outside the filter: the skipped one's closing mark pairs with the
    next one's opening mark, and the connective prose between two real
    quotations gets checked as though it were one. A model that quotes short
    terms constantly — qwen3.8 quotes 107 times in a lecture — trips that on
    nearly every page, and the failures read like invented quotes.

    So pair first and filter second. Curly marks carry their own direction;
    straight ones are paired by position, every other field of a split.
    """
    found = [m.group(1) for m in CURLY_QUOTE_RE.finditer(text)]
    straight = CURLY_QUOTE_RE.sub("", text)
    found += [seg for i, seg in enumerate(straight.split('"'))
              if i % 2 and "\n" not in seg]
    return [q for q in found if QUOTE_MIN <= len(q) <= QUOTE_MAX]


def normalise_number(tok: str) -> str:
    tok = re.sub(r"[,\u00a0\u202f\u2009 ]", "", tok)
    if "." in tok:
        tok = tok.rstrip("0").rstrip(".")
    return tok or "0"


def numbers_in(text: str) -> set[str]:
    text = TIMESTAMP_RE.sub(" ", text)  # timestamps are checked separately
    return {normalise_number(t) for t in NUMBER_RE.findall(text)}


def lecture_duration(d: Path) -> float:
    try:
        return float(lecture_info(d).get("duration") or 0)
    except (TypeError, ValueError):   # a duration that isn't a number
        return 0.0


# Words whose absence flips a sentence. They must be matched outright, never
# bridged by the gap tolerance that forgives ASR hiccups.
NEGATIONS = {"not", "no", "never", "dont", "doesnt", "didnt", "wont", "cant",
             "cannot", "isnt", "arent", "wasnt", "werent", "none", "neither",
             "nor", "without", "except", "unless"}


def verify_notes(d: Path, notes_name: str = "notes.md") -> dict:
    """Check every figure and timestamp in a set of notes against the source.

    Notes are for revising from, so a confident wrong number is worse than a
    vague right one. This won't catch a bad interpretation — a real figure
    attached to the wrong concept passes — but it does catch invented figures
    and citations pointing past the end of the lecture.
    """
    notes_file, bundle = d / notes_name, d / "bundle.md"
    if not notes_file.exists():
        return {"error": f"{notes_name} not found"}
    if not bundle.exists():
        return {"error": "bundle.md missing — process the lecture first"}

    notes = notes_file.read_text()
    source_numbers = numbers_in(bundle.read_text())
    duration = lecture_duration(d)

    # Single digits are list markers and prose ("3-4 sentences"), so only check
    # tokens that could plausibly be a real quantity.
    checked, unsupported = 0, []
    for line in notes.splitlines():
        for tok in NUMBER_RE.findall(TIMESTAMP_RE.sub(" ", line)):
            norm = normalise_number(tok)
            if len(norm.replace(".", "")) < 2:
                continue
            checked += 1
            if norm not in source_numbers:
                unsupported.append({"value": norm, "context": line.strip()[:110]})

    # Citations past the end of the recording are a classic invention.
    bad_times = []
    for m in TIMESTAMP_RE.finditer(notes):
        h, mnt, s = m.group(1), m.group(2), m.group(3)
        sec = (int(h) * 3600 + int(mnt) * 60 + int(s)) if s else (int(h) * 60 + int(mnt))
        if duration and sec > duration + 60:
            bad_times.append(m.group(0))

    # Delta-style notes carry few figures but many direct quotes, so the
    # quotes are the thing worth checking: a plausible-sounding line the
    # lecturer never said is exactly the failure this style invites.
    spoken = " ".join(s.get("text", "") for s in
                      (json.loads((d / "transcript.json").read_text())
                       if (d / "transcript.json").exists() else []))
    spoken_norm = re.sub(r"[^a-z0-9 ]+", " ", spoken.lower())
    spoken_norm = " ".join(spoken_norm.split())
    spoken_seq = spoken_norm.split()

    # Lecturers read things out, and models quote what was on screen — terminal
    # output, a slide heading, a formula. Checking only the transcript flagged
    # those as invented, so the slide text is a legitimate source too.
    slide_text = " ".join(
        " ".join(s.get("text") or []) for s in
        (json.loads((d / "slides.json").read_text())
         if (d / "slides.json").exists() else [])
    )
    slide_norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", slide_text.lower()).split())
    corpora = [spoken_seq] + ([slide_norm.split()] if slide_norm else [])

    bad_quotes, quotes_checked = [], 0
    for q in quotations(notes):
        norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", q.lower()).split())
        if not norm:
            continue
        quotes_checked += 1
        if norm in spoken_norm:
            continue
        # ASR mangles wording, so accept a quote whose words are mostly there
        # rather than demanding an exact match.
        #
        # Whole words only. Testing `w in spoken_norm` searched the transcript
        # as one long string, so "cat" matched inside "concatenate" and "ion"
        # inside "million" — a quote built from common short words scored 100%
        # without ever having been said, and the check passed invented lines.
        # Order matters, gaps do not. Membership alone ("are these words
        # anywhere in the lecture") lets a quote assembled from scattered
        # words pass, and since deletion only raises that score, striking
        # "not" from a real sentence produced an inverted quote that verified
        # clean. But demanding one unbroken run is too harsh the other way: a
        # single ASR hiccup mid-sentence flagged genuine quotations.
        #
        # So: sum the in-order matching blocks. SequenceMatcher only pairs
        # words that appear in the same sequence, which keeps the ordering
        # guarantee, while tolerating a garbled word in the middle.
        words = norm.split()
        best, best_covered = 0, set()
        for hay in corpora:
            blocks = difflib.SequenceMatcher(
                None, words, hay, autojunk=False).get_matching_blocks()
            size = sum(b.size for b in blocks)
            if size > best:
                best = size
                best_covered = {i for b in blocks for i in range(b.a, b.a + b.size)}

        # A negation carries the whole meaning of a sentence, so it cannot be
        # one of the words the gap-tolerance skips over. Allowing it lets
        # "you do not need to know this for the exam" match a passage saying
        # you DO — the quote inverts the lecturer and still verifies clean.
        negated = {i for i, w in enumerate(words) if w in NEGATIONS}
        if negated - best_covered:
            bad_quotes.append(q[:110])
            continue

        if best / len(words) < 0.8:
            bad_quotes.append(q[:110])

    return {
        "notes": notes_name,
        "checked": checked,
        "unsupported": unsupported,
        "timestamps": len(TIMESTAMP_RE.findall(notes)),
        "out_of_range": sorted(set(bad_times)),
        "quotes_checked": quotes_checked,
        "bad_quotes": bad_quotes,
        "duration": duration,
        "clean": not unsupported and not bad_times and not bad_quotes,
    }


def cmd_verify(cfg: Config, args) -> None:
    dirs = [d for d in lecture_dirs() if args.slug.lower() in d.name.lower()]
    if not dirs:
        die(f"No lecture matching {args.slug!r}.")
    d = dirs[0]

    r = verify_notes(d, args.notes or "notes.md")
    if "error" in r:
        die(r["error"])

    print(f"\n{r['notes']}  —  {d.name[:52]}")
    print(f"  figures checked : {r['checked']}")
    print(f"  unsupported     : {len(r['unsupported'])}")
    print(f"  timestamps      : {r['timestamps']}")
    print(f"  out of range    : {len(r['out_of_range'])}")
    print(f"  quotes checked  : {r.get('quotes_checked', 0)}")
    print(f"  not in transcript: {len(r.get('bad_quotes', []))}")

    if r.get("bad_quotes"):
        print("\n  quoted but not found in what was said:")
        for q in r["bad_quotes"][:8]:
            print(f"    \"{q}\"")

    if r["unsupported"]:
        print("\n  figures not found anywhere in the transcript or slide text:")
        for u in r["unsupported"][:15]:
            print(f"    {u['value']:>12}   {u['context']}")
        if len(r["unsupported"]) > 15:
            print(f"    ... and {len(r['unsupported']) - 15} more")
    if r["out_of_range"]:
        print(f"\n  timestamps past the end ({hhmm(r['duration'])}): "
              f"{', '.join(r['out_of_range'][:10])}")
    if r["clean"]:
        print("\n  every figure and citation traces back to the source.")


# ---------------------------------------------------------------- web data


def lecture_id(d: Path) -> str:
    return d.name.rsplit("[", 1)[-1].rstrip("]") if d.name.endswith("]") else d.name


def display_title(d: Path) -> str:
    return re.sub(r"\s*\[[0-9a-fA-F-]{36}\]$", "", d.name).strip()


def library_index() -> list[dict]:
    modules: dict[str, list] = {}
    for d in lecture_dirs():
        video = find_video(d)
        modules.setdefault(d.parent.name, []).append({
            "id": lecture_id(d),
            "title": display_title(d),
            "duration": lecture_duration(d),
            "has_transcript": (d / "transcript.json").exists(),
            "has_slides": bool(json.loads((d / "slides.json").read_text() or "[]"))
                          if (d / "slides.json").exists() else False,
            "has_video": bool(video),
            "pruned": is_pruned(d),
            "size": dir_size(d),
            "has_bundle": (d / "bundle.md").exists(),
            "notes": sorted(p.name for p in d.glob("notes*.md")),
            "video": str(video.relative_to(LIBRARY)) if video else None,
        })
    return [{"name": k, "lectures": v} for k, v in sorted(modules.items())]


def lecture_payload(vid: str) -> dict | None:
    d = find_by_id(vid) if vid else None
    if not d:
        return None

    def load(name, default):
        p = d / name
        try:
            return json.loads(p.read_text()) if p.exists() else default
        except json.JSONDecodeError:
            return default

    video = find_video(d)
    info = lecture_info(d)
    slides = load("slides.json", [])
    for s in slides:  # make paths servable
        s["url"] = f"/media?path={quote(str((d / s['file']).relative_to(LIBRARY)))}"

    return {
        "id": vid,
        "title": display_title(d),
        "module": d.parent.name,
        "duration": lecture_duration(d),
        "video": (f"/media?path={quote(str(video.relative_to(LIBRARY)))}"
                  if video else None),
        "pruned": is_pruned(d),
        "source": info.get("webpage_url") or
                  f"https://{DEFAULT_HOST}/Panopto/Pages/Viewer.aspx?id={vid}",
        "segments": load("transcript.json", []),
        "slides": slides,
        "notes": {p.name: p.read_text() for p in sorted(d.glob("notes*.md"))},
    }


# ---------------------------------------------------------------- schedule


AGENT_LABEL = "local.lecturescrape.sync"


def notify(title: str, message: str) -> None:
    """A scheduled run has no terminal, so say something visible instead of
    failing into a log nobody reads."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f"display notification {json.dumps(message)} "
             f"with title {json.dumps(title)}"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def cmd_autosync(cfg: Config, args) -> None:
    """One unattended pass: pull new captions, process them, say what arrived.

    Captions-only by design — a weekly job should be cheap and silent. Notes
    are still yours to ask for on the lectures you care about.
    """
    if not cfg.sources:
        die("No sources in config.yaml — add the module folders to watch.")

    # A scheduled run has no terminal. Anything that would otherwise be a
    # traceback in a log nobody opens has to become a visible notification.
    missing = [b for b in ("yt-dlp", "ffmpeg") if not have(b)]
    if missing:
        msg = (f"{', '.join(missing)} not found. If this only fails on the "
               "schedule, the PATH in the launch agent is wrong — re-run "
               "`schedule install`.")
        notify("Lecture sync failed", msg)
        die(msg)

    before = {lecture_id(d) for d in lecture_dirs()}
    problems = []

    for src in cfg.sources:
        url, name = src.get("url"), src.get("name")
        if not url:
            continue
        target = parse_panopto(url)
        if not target:
            problems.append(f"not a Panopto link: {url[:50]}")
            continue
        try:
            preflight_auth(cfg, target["url"])
        except RuntimeError as e:
            problems.append(str(e))
            continue

        cmd = ytdlp_cmd(cfg, name, target["url"], captions_only=True)
        # Only look at the newest few — a Recap folder can hold hundreds, and
        # anything new lands at the top.
        cmd += ["--playlist-items", f"1:{args.check or cfg.autosync_check}"]
        subprocess.call(cmd + [target["url"]])

    new = [d for d in lecture_dirs() if lecture_id(d) not in before]

    if new:
        try:
            cmd_process(cfg, argparse.Namespace(
                only=None, force=False, force_whisper=False,
                whisper_model=None, keep_video=False))
        except Exception as e:
            notify("Lecture sync: processing failed",
                   f"{len(new)} downloaded but not processed — {str(e)[:120]}")
            raise

    if problems:
        notify("Lecture sync failed", problems[0][:170])
        print(f"\nproblems:\n  " + "\n  ".join(problems))
        return

    if new:
        titles = ", ".join(display_title(d)[:40] for d in new[:3])
        extra = f" (+{len(new) - 3} more)" if len(new) > 3 else ""
        notify(f"{len(new)} new lecture(s)", titles + extra)
        print(f"\n{len(new)} new lecture(s):")
        for d in new:
            print(f"  {display_title(d)[:64]}")
    else:
        print("\nnothing new")


def agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def cmd_schedule(cfg: Config, args) -> None:
    import plistlib

    plist = agent_plist_path()

    if args.action == "status":
        proc = run(["launchctl", "list"], capture=True)
        loaded = AGENT_LABEL in (proc.stdout or "")
        print(f"plist  : {plist} {'(exists)' if plist.exists() else '(absent)'}")
        print(f"loaded : {'yes' if loaded else 'no'}")
        log = Path.home() / "Library" / "Logs" / "lecturescrape-sync.log"
        if log.exists():
            print(f"log    : {log}")
            tail = log.read_text(errors="replace").strip().splitlines()[-4:]
            for line in tail:
                print(f"         {line[:88]}")
        return

    if args.action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True)
        plist.unlink(missing_ok=True)
        print("scheduled sync removed")
        return

    # install
    if not cfg.sources:
        die("No sources in config.yaml — add the module folders to watch first.")

    log = Path.home() / "Library" / "Logs" / "lecturescrape-sync.log"
    spec = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "lecturescrape.py"),
                             "autosync"],
        "WorkingDirectory": str(ROOT),
        # launchd gives a bare environment, so yt-dlp and ffmpeg need putting back.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StartCalendarInterval": {"Weekday": args.weekday, "Hour": args.hour,
                                  "Minute": 0},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "RunAtLoad": False,
        # Catch up if the Mac was asleep at the scheduled time.
        "StartInterval": None,
    }
    spec = {k: v for k, v in spec.items() if v is not None}

    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(spec))

    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                   capture_output=True)
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"launchctl refused the job: {(proc.stderr or '').strip()[:200]}")

    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday"]
    print(f"scheduled: every {days[args.weekday % 7]} at {args.hour:02d}:00")
    print(f"  plist : {plist}")
    print(f"  log   : {log}")
    print(f"  checks the newest {cfg.autosync_check} of each source, captions only")


# ---------------------------------------------------------------- serve


MAX_JOB_LOG = 2000       # a folder of 15 lectures produces a lot of yt-dlp noise
MAX_JOBS_KEPT = 40


def job_snapshot(job: dict, lock, since: int = 0) -> dict:
    """A consistent copy for the wire.

    Taken under the lock because the worker mutates the job while request
    threads serialise it. `since` returns only log lines the client hasn't
    seen — re-sending the whole log every 1.5s makes a long folder run
    quadratically expensive.
    """
    with lock:
        snap = {k: v for k, v in job.items() if k != "log"}
        total = len(job["log"])
        snap["log"] = job["log"][max(0, since):]
    snap["log_total"] = total
    return snap


class _Tee:
    """Mirror stdout into a job's log so the browser panel can show progress.

    The log is read by request threads while this writes from the worker, so
    every mutation takes the lock — otherwise json.dumps can iterate the list
    mid-append and blow up the response.
    """

    def __init__(self, sink: list, real, lock):
        self.sink, self.real, self.lock, self.buf = sink, real, lock, ""

    def write(self, s):
        self.real.write(s)
        self.buf += s.replace("\r", "\n")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            with self.lock:
                if self.sink and self.sink[-1] == line:
                    continue          # yt-dlp repeats progress lines verbatim
                self.sink.append(line)
                if len(self.sink) > MAX_JOB_LOG:
                    del self.sink[:len(self.sink) - MAX_JOB_LOG]

    def flush(self):
        self.real.flush()


def cmd_serve(cfg: Config, args) -> None:
    """Local daemon the Chrome extension talks to. Bound to loopback only."""
    import contextlib
    import mimetypes
    import queue
    import threading
    import traceback
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, unquote, urlparse

    jobs: dict[str, dict] = {}
    pending: queue.Queue = queue.Queue()
    lock = threading.Lock()

    def worker():
        while True:
            job = pending.get()
            job["state"] = "running"
            tee = _Tee(job["log"], sys.stdout, lock)
            try:
                with contextlib.redirect_stdout(tee):
                    write_agents_file()

                    if job["captions_only"]:
                        targets = fetch_captions(cfg, job["url"],
                                                 job["module"] or None)
                    elif job["kind"] == "folder":
                        targets = fetch_folder(cfg, job["url"], job["module"] or None)
                    else:
                        targets = [fetch_url(cfg, job["url"],
                                             job["module"] or "Unsorted")]

                    # One unprocessable lecture must not sink a whole module.
                    done, failed = [], []
                    for i, d in enumerate(targets, 1):
                        if len(targets) > 1:
                            print(f"[{i}/{len(targets)}] {d.name[:60]}")
                        try:
                            video = find_video(d)
                            segments = get_transcript(cfg, d, video,
                                                      force_whisper=False)
                            slides = annotate_slides(
                                cfg, d, extract_slides(cfg, d, video))
                            write_bundle(d, segments, slides)
                            if slides and not cfg.keep_video:
                                freed = prune_video(d)
                                if freed:
                                    print(f"  pruned video, freed {human_size(freed)}")
                            done.append(d)
                        except Exception as e:
                            failed.append(d.name)
                            print(f"  ! skipped {d.name[:50]}: {e}")

                    if not done:
                        raise RuntimeError(
                            "nothing could be processed"
                            + (f" ({len(failed)} failed)" if failed else ""))

                    first = done[0]
                    job["dir"], job["title"] = str(first), first.name
                    job["lectures"] = [lecture_id(d) for d in done]
                    job["failed"] = failed

                    # Only write notes for a single lecture — analysing a whole
                    # module unattended would run for hours.
                    if job["analyse"] and job["kind"] == "lecture":
                        # Slide-bearing lectures get the delta prompt; a
                        # transcript-only one has no slides to differ from.
                        p = choose_prompt(first, use_slides=True)
                        job["notes"] = run_analysis(
                            cfg, first, prompt=p,
                            use_slides=job["slides"] or p is DELTA_PROMPT)
                    else:
                        job["notes"] = None
                        if job["kind"] == "folder":
                            print(f"processed {len(done)} lecture(s)"
                                  + (f", {len(failed)} failed" if failed else "")
                                  + " — open one to write notes")
                job["state"] = "done"
            except Exception as e:
                job["state"] = "error"
                job["error"] = str(e)
                with lock:
                    job["log"].append(f"error: {e}")
                traceback.print_exc()
            finally:
                pending.task_done()

    threading.Thread(target=worker, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _cors(self):
            """Only the Chrome extension may reach this cross-origin.

            The wildcard this used to send let ANY page you happened to be
            visiting read your whole library — lecture list, transcripts and
            notes — because the daemon listens on localhost and the browser
            will happily make that request on the page's behalf. The viewer is
            served from this same origin and needs no CORS at all.
            """
            origin = self.headers.get("Origin", "")
            if not origin.startswith("chrome-extension://"):
                return
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -------------------------------------------------- static + media

        def _send_bytes(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path):
            """Serve with Range support — without it the browser can't seek
            within a 148 MB lecture video."""
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

            start, length, code = 0, size, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                start = int(s) if s.isdigit() else 0
                end = int(e) if e.isdigit() else size - 1
                end = min(end, size - 1)
                if start > end:
                    start, end = 0, size - 1
                length, code = end - start + 1, 206

            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range",
                                 f"bytes {start}-{start + length - 1}/{size}")
            self.end_headers()

            try:
                with path.open("rb") as fh:
                    fh.seek(start)
                    left = length
                    while left > 0:
                        chunk = fh.read(min(262144, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player aborts the request on every seek; normal

        def _safe_media(self, rel: str) -> Path | None:
            """Resolve under library/ only — no escaping the tree."""
            try:
                target = (LIBRARY / unquote(rel)).resolve()
                target.relative_to(LIBRARY.resolve())
            except (ValueError, OSError):
                return None
            return target if target.is_file() else None

        # -------------------------------------------------- routes

        def do_GET(self):
            parsed = urlparse(self.path)
            route, q = parsed.path, parse_qs(parsed.query)

            if route in ("/", "/index.html"):
                page = ROOT / "webui.html"
                if not page.exists():
                    return self._send(404, {"error": "webui.html missing"})
                return self._send_bytes(page.read_bytes(), "text/html; charset=utf-8")

            if route == "/health":
                return self._send(200, {"ok": True, "model": cfg.model,
                                        "backend": cfg.backend})

            if route == "/api/library":
                return self._send(200, {"modules": library_index()})

            if route == "/api/lecture":
                lec = lecture_payload(q.get("id", [""])[0])
                if not lec:
                    return self._send(404, {"error": "no such lecture"})
                return self._send(200, lec)

            if route == "/api/export":
                d = find_by_id(q.get("id", [""])[0])
                if not d:
                    return self._send(404, {"error": "no such lecture"})
                vault = Path(cfg.obsidian_vault or "").expanduser()
                if not cfg.obsidian_vault or not vault.exists():
                    return self._send(400, {
                        "error": "No Obsidian vault configured — set "
                                 "obsidian_vault in config.yaml"})
                try:
                    r = export_obsidian(cfg, d, vault,
                                        q.get("notes", ["notes.md"])[0],
                                        with_transcript=q.get("transcript",
                                                              ["0"])[0] == "1")
                    return self._send(200, r)
                except RuntimeError as e:
                    return self._send(400, {"error": str(e)})

            if route == "/api/verify":
                d = find_by_id(q.get("id", [""])[0])
                if not d:
                    return self._send(404, {"error": "no such lecture"})
                return self._send(200, verify_notes(d, q.get("notes", ["notes.md"])[0]))

            if route == "/media":
                target = self._safe_media(q.get("path", [""])[0])
                if not target:
                    return self._send(404, {"error": "not found"})
                return self._send_file(target)

            if route.startswith("/jobs/"):
                job = jobs.get(route.split("/")[-1])
                if not job:
                    return self._send(404, {"error": "no such job"})
                since = q.get("since", ["0"])[0]
                return self._send(200, job_snapshot(job, lock,
                                                    int(since) if since.isdigit()
                                                    else 0))

            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/jobs":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad json"})

            target = parse_panopto(req.get("url", ""))
            if not target:
                return self._send(400, {
                    "error": "That doesn't look like a Panopto link. Paste a "
                             "lecture URL (…/Viewer.aspx?id=…) or a module "
                             "folder URL (…/List.aspx?folderID=…)."
                })

            # already queued or finished? hand back the existing job
            for j in jobs.values():
                if j["url"] == target["url"] and j["state"] in ("queued", "running",
                                                                "done"):
                    return self._send(200, j)

            module = req.get("module") or ""
            job = {
                "id": uuid.uuid4().hex[:12],
                "url": target["url"],
                "kind": target["kind"],
                "module": safe_name(module) if module else "",
                "analyse": bool(req.get("analyse", True)),
                "slides": bool(req.get("slides", False)),
                "captions_only": bool(req.get("captions_only", False)),
                "state": "queued",
                "log": [],
                "notes": None,
                "dir": None,
                "title": None,
                "lectures": [],
            }
            with lock:
                jobs[job["id"]] = job
                # Don't accumulate finished jobs for the life of the daemon.
                if len(jobs) > MAX_JOBS_KEPT:
                    stale = [k for k, v in list(jobs.items())
                             if v["state"] in ("done", "error")][:-MAX_JOBS_KEPT // 2]
                    for k in stale:
                        jobs.pop(k, None)
            pending.put(job)
            return self._send(202, job_snapshot(job, lock))

        def log_message(self, *a):  # quiet; the pipeline prints its own progress
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port or cfg.port), Handler)
    print(f"lecturescrape daemon on http://127.0.0.1:{server.server_port}")
    print(f"  analysis -> {cfg.model} at {cfg.endpoint}")
    print("  load extension/ in chrome://extensions (Developer mode > Load unpacked)")
    print("  ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ---------------------------------------------------------------- status


def cmd_status(cfg: Config, args) -> None:
    dirs = lecture_dirs()
    if not dirs:
        print("Library is empty. Run `sync` to download.")
        return

    by_module: dict[str, list[Path]] = {}
    for d in dirs:
        by_module.setdefault(d.parent.name, []).append(d)

    total = 0
    for module, lectures in sorted(by_module.items()):
        print(f"\n{module}  ({len(lectures)})")
        for d in sorted(lectures):
            marks = "".join(
                m if (d / f).exists() else "·"
                for m, f in (("T", "transcript.json"),
                             ("S", "slides.json"),
                             ("B", "bundle.md"),
                             ("N", "notes.md"))
            )
            size = dir_size(d)
            total += size
            tag = " pruned" if is_pruned(d) else ""
            print(f"  [{marks}] {human_size(size):>8}{tag:<7} {d.name[:60]}")

    print("\nT=transcript  S=slides  B=bundle  N=notes")
    print(f"library: {human_size(total)}")
    reclaimable = sum(
        find_video(d).stat().st_size for d in dirs
        if find_video(d) and (d / "bundle.md").exists())
    if reclaimable:
        print(f"{human_size(reclaimable)} reclaimable — run `prune`")


# ---------------------------------------------------------------- plumbing


def run(cmd: list[str], capture: bool = False):
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if proc.returncode != 0 and not capture:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode})")
    return proc


def main() -> None:
    p = argparse.ArgumentParser(prog="lecturescrape", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="download new recordings")
    s.add_argument("--url", help="one-off folder or lecture URL")
    s.add_argument("--name", help="module name for --url")
    s.add_argument("--limit", type=int, help="only the first N of each folder")
    s.add_argument("--dry-run", action="store_true", help="list, don't download")
    s.add_argument("--captions-only", action="store_true",
                   help="fetch captions and metadata, skip the video (fast, no slides)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("process", help="transcribe and extract slides")
    s.add_argument("--only", help="substring match on lecture name")
    s.add_argument("--force", action="store_true", help="redo finished lectures")
    s.add_argument("--force-whisper", action="store_true",
                   help="ignore Panopto captions, transcribe locally")
    s.add_argument("--whisper-model", help="override the model in config.yaml")
    s.add_argument("--keep-video", action="store_true",
                   help="don't prune the recording after processing")
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("prune", help="delete processed lectures' video files")
    s.add_argument("slug", nargs="?", help="substring match (default: all)")
    s.add_argument("--dry-run", action="store_true", help="show what would go")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("analyse", help="send a bundle to a model")
    s.add_argument("slug", nargs="?", help="substring match on lecture name")
    s.add_argument("--all", action="store_true", help="every lecture with a bundle")
    s.add_argument("--module", help="every lecture in one module")
    s.add_argument("--redo", action="store_true",
                   help="rewrite notes that already exist")
    s.add_argument("--slides", action="store_true",
                   help="use bundle.md (with slide refs) instead of transcript.md")
    s.add_argument("--prompt", help="path to a custom prompt file")
    s.add_argument("--model", help="override the model in config.yaml")
    s.add_argument("--backend", choices=["openai", "antigravity"],
                   help="override the backend in config.yaml")
    s.add_argument("--jobs", type=int, default=1, metavar="N",
                   help="lectures to analyse at once on a batch run; worth "
                        "raising for a hosted backend, leave at 1 for a local "
                        "model")
    s.add_argument("--vision", action="store_true",
                   help="let the model read the slide images for the equations "
                        "and tables OCR mangles; needs the antigravity backend "
                        "or a local model that can see")
    s.add_argument("--label", help="save as notes-LABEL.md, for comparing models")
    s.add_argument("--style", choices=["auto", "delta", "summary"], default="auto",
                   help="delta = only what the recording adds beyond the slides "
                        "(default when slides exist); summary = notes on everything")
    s.set_defaults(func=cmd_analyse)

    s = sub.add_parser("export", help="export notes into an Obsidian vault")
    s.add_argument("slug", nargs="?", help="substring match on lecture name")
    s.add_argument("--all", action="store_true", help="every lecture with notes")
    s.add_argument("--vault", help="override the vault in config.yaml")
    s.add_argument("--notes", help="which notes file (default notes.md)")
    s.add_argument("--with-transcript", action="store_true",
                   help="also write a companion transcript note")
    s.add_argument("--no-links", action="store_true",
                   help="don't wikilink concept terms in the exported note")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("concepts", help="build cross-lecture concept notes")
    s.add_argument("--module", help="limit to one module")
    s.add_argument("--vault", help="override the vault in config.yaml")
    s.add_argument("--notes", help="which notes file (default notes.md)")
    s.add_argument("--min-lectures", type=int, default=2,
                   help="only write concepts seen in at least this many lectures")
    s.set_defaults(func=cmd_concepts)

    s = sub.add_parser("verify", help="check notes' figures against the source")
    s.add_argument("slug", help="substring match on lecture name")
    s.add_argument("--notes", help="which notes file (default notes.md)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("autosync", help="one unattended captions pull (used by the schedule)")
    s.add_argument("--check", type=int,
                   help="how many of the newest to check per source")
    s.set_defaults(func=cmd_autosync)

    s = sub.add_parser("schedule", help="run autosync weekly in the background")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--weekday", type=int, default=1,
                   help="0=Sunday … 1=Monday (default)")
    s.add_argument("--hour", type=int, default=8, help="hour of day, 24h")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("serve", help="run the daemon the Chrome extension uses")
    s.add_argument("--port", type=int, help="override the port in config.yaml")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("status", help="show the library")
    s.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(Config.load(), args)


if __name__ == "__main__":
    main()
