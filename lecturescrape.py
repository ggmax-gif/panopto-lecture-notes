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
import json
import os
import re
import shutil
import subprocess
import sys
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
    min_slide_gap: float
    max_slides: int
    ocr: bool
    slide_text_similarity: float
    keep_video: bool
    backend: str
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
            min_slide_gap=float(raw.get("min_slide_gap", 2.0)),
            max_slides=int(raw.get("max_slides", 60)),
            ocr=bool(raw.get("ocr", True)),
            slide_text_similarity=float(raw.get("slide_text_similarity", 0.90)),
            keep_video=bool(raw.get("keep_video", False)),
            backend=raw.get("backend", "openai"),
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

        print(f"\n=== {name or 'auto (by module code)'} ===")
        cmd = ytdlp_cmd(cfg, name, url, captions_only=args.captions_only)
        if args.limit:
            cmd += ["--playlist-items", f"1:{args.limit}"]
        if args.dry_run:
            cmd += ["--simulate"]
        cmd.append(url)

        rc = subprocess.call(cmd)
        if rc != 0:
            failures.append(name)
            print(f"  ! yt-dlp exited {rc} for {name}")

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
    and slides end up alongside the video. With no module name, a folder files
    itself under its own Panopto title."""
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
         "--playlist-items", "1", "--socket-timeout", "20",
         "--cookies-from-browser", cfg.browser, url],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return

    err = (proc.stderr or "") + (proc.stdout or "")
    if "registered users" in err or "not available" in err.lower():
        host = re.search(r"https?://([^/\s]+)", url)
        raise RuntimeError(AUTH_HINT.format(
            host=host.group(1) if host else "Recap", browser=cfg.browser))
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
        needs_slides = find_video(d) and not (d / "slides.json").exists()
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

    info_file = next(iter(d.glob("*.info.json")), None)
    url = None
    if info_file:
        try:
            url = json.loads(info_file.read_text()).get("webpage_url")
        except json.JSONDecodeError:
            pass
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


def is_blank_frame(path: Path) -> bool:
    """A near-uniform frame is the presenter's desktop between slides, not a
    slide. Scene detection catches these because the screen genuinely changed,
    but they carry nothing and end up embedded in the exported notes."""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return False
    try:
        with Image.open(path) as im:
            small = im.convert("L").resize((48, 48))
            return ImageStat.Stat(small).stddev[0] < 8.0
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
    vf = (
        rf"select='eq(n\,0)+gt(scene\,{cfg.slide_threshold})',"
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

    # Drop blank frames before spending OCR on them.
    def drop_blanks(items: list[dict], why: str) -> list[dict]:
        """Blank frames are the presenter's desktop between slides. Decided by
        file path rather than dict equality — comparing dicts against a growing
        list is quadratic, and a code-heavy lecture yields hundreds of frames."""
        keep = [s for s in items
                if s.get("text") or not is_blank_frame(d / s["file"])]
        keeping = {s["file"] for s in keep}
        for s in items:
            if s["file"] not in keeping:
                (d / s["file"]).unlink(missing_ok=True)
        if len(keep) < len(items):
            print(f"  dropped {len(items) - len(keep)} blank frame(s){why}")
        return keep

    slides = drop_blanks(slides, "")
    if not slides:
        return slides

    if cfg.ocr and ocr_available():
        for s in slides:
            s["text"] = ocr_image(d / s["file"])
        with_text = sum(1 for s in slides if s["text"])
        print(f"  OCR: text found on {with_text}/{len(slides)} slides")

        # A frame can still be blank once OCR confirms there's nothing on it.
        slides = drop_blanks(slides, " after OCR")

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
    elif cfg.ocr:
        print("  OCR unavailable (pip install pyobjc-framework-Vision) — skipping")

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


def is_pruned(d: Path) -> bool:
    """Distinguishes 'we had the video and threw it away' from 'we never
    downloaded one'. Slides can only exist if a video was decoded, so their
    presence without a video means it was pruned — and re-downloading would be
    a waste rather than a fix.
    """
    if find_video(d):
        return False
    try:
        return bool(json.loads((d / "slides.json").read_text()))
    except (OSError, json.JSONDecodeError):
        return False


def prune_video(d: Path) -> int:
    """Drop the recording once slides and transcript are extracted.

    ~148 MB becomes ~5 MB. Everything the notes rely on survives: the slides
    are images on disk, the transcript is text, and each timestamp already
    links back into Panopto for the moments you want to actually hear.
    """
    video = find_video(d)
    if not video:
        return 0
    if not (d / "bundle.md").exists():
        return 0                      # never discard before it's been processed
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
    info = {}
    info_file = next(iter(d.glob("*.info.json")), None)
    if info_file:
        try:
            info = json.loads(info_file.read_text())
        except json.JSONDecodeError:
            pass

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

    if args.model:
        cfg.model = args.model
    if args.backend:
        cfg.backend = args.backend

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

    print(f"{len(todo)} of {len(dirs)} lecture(s) need notes "
          f"({cfg.model} at {cfg.endpoint})\n")

    done, failed = 0, []
    for i, d in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {display_title(d)[:58]}")
        try:
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
            style = "delta" if prompt is DELTA_PROMPT else "summary"
            print(f"      written ({style})")
            done += 1
        except Exception as e:
            failed.append(display_title(d)[:50])
            print(f"      ! failed: {str(e)[:90]}")

    print(f"\n{done} written, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")


def gemini_cli_analysis(cfg: Config, d: Path, body: str, prompt: str) -> str:
    """Run the notes through Gemini CLI.

    This used to be the way to spend an AI Pro subscription rather than metered
    credits, via Code Assist auth. Google withdrew that from third-party clients
    in August 2026, so it now needs GEMINI_API_KEY and bills per token like the
    API. Antigravity against library/ is the remaining subscription route.
    """
    if not have("gemini"):
        raise RuntimeError("gemini CLI not found — npm install -g @google/gemini-cli")

    cmd = ["gemini", "-p", prompt, "--skip-trust", "--approval-mode", "plan"]
    if cfg.model:
        cmd[1:1] = ["-m", cfg.model]

    env = dict(os.environ)
    if not env.get("GEMINI_API_KEY"):
        env.setdefault("GOOGLE_GENAI_USE_GCA", "true")

    print(f"asking {cfg.model or 'gemini'} via the CLI (metered, needs an API key) ...")
    proc = subprocess.run(cmd, input=body, capture_output=True,
                          text=True, cwd=str(d), env=env)

    if proc.returncode != 0:
        err = strip_cli_noise(f"{proc.stderr}\n{proc.stdout}")
        if "no longer supported" in err or "migrate to the Antigravity" in err:
            raise RuntimeError(
                "Google has withdrawn Gemini Code Assist for individuals from "
                "this client, so the CLI can no longer spend an AI Pro "
                "subscription. Either set GEMINI_API_KEY for metered API "
                "billing, or use Antigravity: open library/ as a workspace and "
                "let its agent write the notes (see AGENTS.md there)."
            )
        needs_login = ("Auth method" in err or "GOOGLE_GENAI_USE_GCA" in err
                       or "Authentication cancelled" in err
                       or "FatalCancellationError" in err)
        if needs_login:
            raise RuntimeError(
                "Gemini CLI isn't signed in. Run `gemini` once in a terminal to "
                "authenticate. Note that subscription sign-in no longer works "
                "here — this path now needs GEMINI_API_KEY and bills per token."
            )
        raise RuntimeError(f"gemini CLI failed: {err[:400]}")

    return strip_cli_noise(proc.stdout)


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


def est_tokens(text: str) -> int:
    return len(text) // 4


def fit_to_context(text: str, budget: int) -> tuple[str, str]:
    """Trim a bundle to fit the model's context window.

    A code-heavy lecture yields 60 OCR'd slides and a 33k-token bundle, which
    overruns a 32k context. Ollama doesn't reject it — the request simply
    stalls, and a timeout on a silent-but-open socket never fires. So the size
    has to be dealt with before sending.

    Slide text is trimmed first, keeping each slide's opening lines: the
    heading says what was on screen, which is what the transcript needs to be
    read against. The transcript itself is never cut — it's the thing the notes
    are actually made from.
    """
    if est_tokens(text) <= budget:
        return text, ""

    for keep in (6, 3, 1):
        out, in_slide, kept = [], False, 0
        for line in text.splitlines():
            if line.startswith("**Slide text @"):
                in_slide, kept = True, 0
                out.append(line)
                continue
            if in_slide and line.startswith("> "):
                kept += 1
                if kept <= keep:
                    out.append(line)
                elif kept == keep + 1:
                    out.append("> …")
                continue
            in_slide = False
            out.append(line)
        trimmed = "\n".join(out)
        if est_tokens(trimmed) <= budget:
            return trimmed, f"slide text trimmed to {keep} line(s) per slide"

    # Slides gone and still too big: the transcript alone overruns the window.
    stripped = "\n".join(l for l in text.splitlines()
                         if not l.startswith(("**Slide text @", "> ")))
    if est_tokens(stripped) <= budget:
        return stripped, "slide text dropped entirely"

    cut = stripped[: budget * 4]
    return cut, "TRANSCRIPT TRUNCATED — lecture too long for this context window"


def run_analysis(cfg: Config, d: Path, use_slides: bool = False,
                 prompt: str = DEFAULT_PROMPT, out_name: str = "notes.md") -> str:
    source = d / ("bundle.md" if use_slides else "transcript.md")
    if not source.exists():
        raise RuntimeError(f"{source.name} missing — process the lecture first")

    body = source.read_text()
    budget = cfg.max_context_tokens - est_tokens(prompt) - cfg.reserve_output_tokens
    body, note = fit_to_context(body, budget)
    if note:
        print(f"  bundle over context budget — {note}")

    if cfg.backend == "gemini-cli":
        notes = gemini_cli_analysis(cfg, d, body, prompt)
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

    print(f"asking {cfg.model} at {cfg.endpoint} ...")
    # The client defaults to a 600s timeout and two retries. A large local model
    # chewing through a two-hour lecture blows past that, and each retry reloads
    # the weights — so it fails after 30 minutes having achieved nothing.
    client = OpenAI(base_url=cfg.endpoint, api_key=key,
                    timeout=cfg.request_timeout, max_retries=1)
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": "user", "content": f"{prompt}\n\n---\n\n{body}"}],
    )
    notes = resp.choices[0].message.content or ""
    (d / out_name).write_text(notes + "\n")
    return notes


# ---------------------------------------------------------------- obsidian


def seconds_of(ts: str) -> int:
    p = [int(x) for x in ts.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def link_timestamps(text: str, url: str) -> str:
    """Turn every `12:58` into a link that opens the recording at that moment.

    This is what makes the exported note usable when you missed the lecture:
    read the notes, and click any point you want to actually hear.
    """
    base = url.split("&")[0]

    def sub(m):
        if m.group(0).startswith("["):          # already inside a link
            return m.group(0)
        return f"[{m.group(1)}]({base}&start={seconds_of(m.group(1))})"

    return re.sub(r"\[?(\b\d{1,2}:[0-5]\d(?::[0-5]\d)?\b)\]?(?!\()",
                  lambda m: sub(m), text)


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


def export_obsidian(cfg: Config, d: Path, vault: Path, notes_name: str = "notes.md",
                    with_transcript: bool = False,
                    concept_index: dict | None = None) -> dict:
    if not (vault / ".obsidian").exists():
        raise RuntimeError(f"{vault} doesn't look like an Obsidian vault")

    notes_file = d / notes_name
    if not notes_file.exists():
        raise RuntimeError(f"{notes_name} not found — write notes first")

    info = {}
    info_file = next(iter(d.glob("*.info.json")), None)
    if info_file:
        try:
            info = json.loads(info_file.read_text())
        except json.JSONDecodeError:
            pass

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

    update_module_note(cfg, vault, module)

    return {
        "note": str(note_path.relative_to(vault)),
        "slides": len(embeds),
        "transcript": transcript_note,
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
        info = {}
        info_file = next(iter(d.glob("*.info.json")), None)
        if info_file:
            try:
                info = json.loads(info_file.read_text())
            except json.JSONDecodeError:
                pass
        url = info.get("webpage_url") or (
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
            if gw < set(specific.split()):
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
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def normalise_number(tok: str) -> str:
    tok = tok.replace(",", "")
    if "." in tok:
        tok = tok.rstrip("0").rstrip(".")
    return tok or "0"


def numbers_in(text: str) -> set[str]:
    text = TIMESTAMP_RE.sub(" ", text)  # timestamps are checked separately
    return {normalise_number(t) for t in NUMBER_RE.findall(text)}


def lecture_duration(d: Path) -> float:
    info_file = next(iter(d.glob("*.info.json")), None)
    if info_file:
        try:
            return float(json.loads(info_file.read_text()).get("duration") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return 0.0


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

    bad_quotes, quotes_checked = [], 0
    for q in re.findall(r'"([^"\n]{12,140})"', notes):
        norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", q.lower()).split())
        if not norm:
            continue
        quotes_checked += 1
        if norm in spoken_norm:
            continue
        # ASR mangles wording, so accept a quote whose words are mostly there
        # in order rather than demanding an exact match.
        words = norm.split()
        hits = sum(1 for w in words if w in spoken_norm)
        if hits / len(words) < 0.8:
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
    info = load(next(iter(p.name for p in d.glob("*.info.json")), "_"), {})
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
            self.send_header("Access-Control-Allow-Origin", "*")
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
    s.add_argument("--backend", choices=["openai", "gemini-cli"],
                   help="override the backend in config.yaml")
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
