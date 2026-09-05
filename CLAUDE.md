# lecturescrape — orientation

Read this before changing anything. One 3,422-line module, `lecturescrape.py`, with eleven subcommands: `sync process prune analyse export concepts verify autosync schedule serve status`. Plus a web UI and `build_app.py` for the packaged Mac app.

**What this is.** Panopto recording → notes covering *what the lecturer said that the slides don't*. The output is a **delta, not a summary**: timestamped transcript plus slide text from OCR, interleaved, and a model asked for the difference. Every claim carries a timestamp that deep-links back into the recording. Runs locally except the optional hosted-model step.

## The one thing to understand before touching anything

**This tool deletes data.** `prune` deletes recordings, `process` drops frames it judges blank, `export` supersedes notes in a real Obsidian vault. Most of the commits in this repo are post-mortems of it destroying or falsifying something. Read the log before changing a guard — `git log` here is the incident register, and the messages carry the reproductions.

A second rule, learned the same way: **when two places answer the same question, they drift.** `is_pruned` and `slides_extracted` did; so did six copies of the `*.info.json` parse, which disagreed on what to catch. One question, one predicate.

The rule the history has converged on: **when a guard misjudges, move the file, don't unlink it.** Blank frames go to `slides/dropped/`, superseded notes go to the vault's `.trash/`. Neither is deleted, because no test can distinguish a generated note someone annotated from the original it was copied from.

## The tests are the guard on all of that

```bash
pip install -r requirements-dev.txt && pytest
```

107 tests, under a second, no network, no ffmpeg, no Vision, no model server.
Every lecture, slide and vault is synthesised under `tmp_path` — `library/` is
gitignored and absent from a fresh clone, so no test can touch a real
recording while proving the code doesn't.

They are the incident register made executable: each test names the commit it
descends from and asserts what that commit bought. **If you change a guard,
the test that fails will tell you which lecture it once destroyed.**

The suite was validated by reverting each fix in a scratch copy and confirming
the matching test failed — seven mutations, seven caught. Do that again if you
add tests here: one written after the fix proves nothing until it has been
shown to fail without it.

## Destructive paths, and what actually guards them

- **`prune_video` requires the slides, not the bundle.** Taking "a bundle exists" as proof the slides were extracted destroyed a video the moment it arrived: a transcript-only lecture has a bundle *before* it has a recording, and afterwards `process` says "skip (done)" forever because the bundle is there and the video it needed is gone. `is_pruned` already asked the right question and now shares the predicate.
- **The all-blank path must write `slides.json` before returning.** The stale file it used to leave listed frames that no longer existed, which read downstream as "slides extracted" and authorised a prune that took the only remaining copy of a lecture.
- **Blank frames are judged only after OCR has testified.** Local contrast cannot separate a clean desktop (0.14) from a pale line diagram (0.13), so a frame goes for having no text *and* nothing visible — never on the picture alone.
- **`export`'s supersede check reads only the frontmatter block, line by line.** Reading the first 800 characters of the whole file killed a user's own hand-written revision note for containing a pasted lecture link and a bullet that began "- transcript". A note is superseded only when its frontmatter shows this exporter wrote it, for this recording.

## Silent-failure paths

The expensive failures here were never crashes. They were success messages.

- **Panopto's folder API answers an unauthenticated caller with an empty list**, and yt-dlp exits 0 having listed nothing. `sync` printed "sync complete" and the weekly autosync would have done nothing, every week, all term, without ever saying so. **Exit 0 with an empty listing is a failure.** Keep it that way.
- **`cmd_sync` must call `preflight_auth`.** Every other path did; the command you actually type went straight to `subprocess.call`, so an expired cookie surfaced as a bare exit code.
- **Context budget arithmetic can go negative** when Ollama serves its 2,048 default. `stripped[:budget*3]` with a negative bound returned the *last* 71k characters — nearly the whole lecture pushed into a window a tenth its size, which is the exact silent eviction this code exists to prevent. Slice is clamped, and it refuses outright below `MIN_CONTEXT_BUDGET`.
- **Ask the server what window it serves; don't trust config.** The configured number and the served number were not the same.
- **The DELTA→SUMMARY fallback strips slide text from the pristine bundle**, not from the already-trimmed body. Refitting the trimmed body left the omission note empty, so the model was told nothing was missing while the summary prompt opened with "No slides are available."

## Constants that encode a measurement, not a preference

- **`BLANK_FRAME_CONTRAST = 0.5`** — local contrast against the frame's own blur. The earlier measure (pixel spread on a 48×48 downsample) was *inverted* for the slides most worth keeping: dense small text averages to flat grey, so the denser the slide the blanker it scored. Across 175 slides the three closest to deletion, at 8.21, were the densest present — a LaTeX regression table. Measured now: desktop 0.14, sparsest real slide 1.24. Costs 2 ms a frame against OCR's 266 ms.
- **`MIN_CONTEXT_BUDGET = 2000`** — the floor below which it refuses rather than truncates.
- **`MIN_SLIDE_CHARS_FOR_DELTA = 600`** — below this there isn't enough slide text for a delta to mean anything, and it falls back to summary.
- **`LINKABLE_TS_RE`** is named that because a second module-level `TIMESTAMP_RE` is defined further down for `verify` and silently won. Don't reintroduce the collision.
- **`fit_to_context` drops whole slides, sparsest first** — not the tail of every slide. Trimming tails left 29 headings with their contents gone while the prompt still told the model the slide text was authoritative for every figure. Whole-slide dropping: 20 of 29 survive intact, and 402 lost figures became 266 kept.

## Verification quirks

`verify` checks the notes' figures and quotations against the source, and each of these was a real false positive: read `21 000` as one number, pair quotation marks *before* filtering by length, match quoted words as words rather than substrings, and check curly quotations as well as straight.

## Models and the daemon

Default is `qwen3.8:27b-mlx`, chosen by measurement rather than assumption: against nemotron on the same 80k bundle it was 2.2× faster for 1.9× the notes, with four times the figures cited and none unsupported. It also reads slide images, so `--vision` no longer needs a second model. Note the default answers on the reasoning channel — the notes are kept from there rather than discarded.

`serve` runs the viewer's daemon. It once handed the library to every site you visited via a wildcard CORS header; the Chrome extension that needed cross-origin access is gone and so is the header. It answers no cross-origin request now — don't reintroduce one.
