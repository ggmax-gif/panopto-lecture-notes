# lecturescrape

Turns a Panopto lecture recording into notes covering **what the lecturer said
that the slides don't**.

You already have the slides. Summarising them back to you is worthless — the
reason attending mattered is everything said out loud that never made it onto a
slide: why a rule exists, which parts are examinable, where a slide is wrong,
what someone asked and how it was answered.

So the notes are a *delta*, not a summary. Each lecture becomes a timestamped
transcript with its slide text read by OCR and interleaved at the moment each
slide appeared, and a model is asked for the difference between the two. A
typical note surfaces things like:

> **Beyond the slides** — accelerated depreciation exists as a government lever
> to encourage capital investment `15:26`
>
> **Emphasis** — "you don't need to memorise the decision rule", follow the
> payment-timing logic instead `18:57`
>
> **Corrections** — the units on the exercise slide should be dollars, not
> pounds `30:38`

Every timestamp deep-links back into the recording, so any claim is one click
from hearing it said. Notes are checked for invented figures and misquotes
before you trust them.

Everything except the optional hosted-model step runs locally.

## How it works

```
Recap link  →  fetch  →  captions  →  slide keyframes  →  delta notes  →  Obsidian
                 │          │              │                   │             │
          picks the      Panopto's     deduplicated       what the       timestamps
          full-frame     own, or        by OCR text,      recording      deep-link
          slide stream   Whisper        not pixels        adds           to Panopto
```

A processed lecture is about 3 MB — the recording is deleted once its slides and
transcript are extracted, since the notes don't need it and Panopto still does.

```
library/BEE2041/2026-03-19 L1 Data Science for Economics [aaf677bd…]/
  transcript.md     timestamped text
  slides/           slide_0001.jpg …
  bundle.md         transcript + slide text, interleaved  ← what the model reads
  notes.md          the delta
```

## Setup

```bash
brew install yt-dlp ffmpeg
pip install -r requirements.txt
cp config.yaml.example config.yaml
```

Then set `obsidian_vault` and add your Recap folder URLs to `sources` in
`config.yaml`. It's gitignored — it holds your vault path and folder ids.

`library/` is gitignored too. Downloaded recordings, transcripts and slide
images are the university's teaching material and stay on your machine.

`mlx-whisper` is only needed when a recording has no captions. `openai` is only
needed for `analyse` — it talks to local servers as happily as to hosted ones.

You also need to be logged into Recap in the browser named in `config.yaml`;
that session is what authorises the download.

## Quick start

```bash
./lecturescrape.py sync --url "<paste a Recap lecture or folder link>"
./lecturescrape.py process          # transcript, slides, OCR
./lecturescrape.py analyse --all    # write the notes
./lecturescrape.py verify "L1"      # check figures and quotes
./lecturescrape.py export --all     # send to Obsidian
```

Or skip the terminal: `./lecturescrape.py serve` opens a viewer at
<http://127.0.0.1:8420> with a paste box, a synced transcript, and buttons for
all of the above. `./build_app.py --desktop` wraps it as a Mac app.

Add `--captions-only` to `sync` to grab just the transcripts — seconds per
lecture instead of minutes, and enough to search a whole term. Fetch the video
later for the lectures you actually want slides from.

## Scope

This downloads recordings you already have access to, for your own study. It
authenticates as you, using your own browser session — it does not bypass access
control, and it can't reach anything your account can't.

Recordings, transcripts and slide images stay on your machine: `library/` is
gitignored, and lectures are pruned to text and keyframes once processed. The
material belongs to the university that made it. Keeping a local copy to revise
from is ordinary use; republishing it isn't.

The MIT licence covers this software only. Recordings, transcripts, slide
images and notes produced with it remain the property of whoever owns the
original teaching material.

## The app

```bash
./build_app.py --desktop
```

Builds **Lecture Notes.app** and puts it on the Desktop. Double-click it: the
daemon starts, and the viewer opens in a window with no address bar. Quitting
the app stops the daemon — unless one was already running from a terminal, in
which case it leaves it alone.

Two things a GUI app needs that a terminal one doesn't, both handled by the
generated launcher:

- **No inherited `PATH`.** Finder-launched apps get a bare environment, so
  `yt-dlp` and `ffmpeg` would simply vanish. The launcher puts
  `/opt/homebrew/bin` and `/usr/local/bin` back.
- **No idea which Python.** The interpreter path is baked in at build time, so
  it finds the one with `pyyaml`, `openai` and the Vision bindings rather than
  the system Python.

Because of that baking, **re-run `build_app.py` if you move the project or
change Python environments.** Pass `--python /path/to/python3` to pick a
specific interpreter.

Startup problems surface as a dialog rather than silently failing, and the
daemon's output goes to `~/Library/Logs/lecturescrape.log`.

The app is unsigned, which is fine for something you built yourself — the
builder clears the quarantine attribute so Gatekeeper doesn't block it.

## The viewer

The app opens it for you. To run it from a terminal instead:

```bash
./lecturescrape.py serve
```

Then open <http://127.0.0.1:8420>. Same daemon that backs the Chrome extension —
no second process.

### Adding lectures

Paste a Recap link into the box at the top left and hit **Add**. This is the
easiest way in — no config file, no extension, no terminal.

It takes anything Exeter's Recap produces:

| Pasted | Result |
|---|---|
| `…/Viewer.aspx?id=<uuid>&instance=MoodleELE2` | that lecture |
| `…/Sessions/List.aspx?folderID=<uuid>` | the whole module |
| `…/Sessions/List.aspx#folderID=%22<uuid>%22` | the whole module |
| `…/Embed.aspx?id=<uuid>` | that lecture |
| a bare UUID | that lecture |

The hash-fragment form matters: that's what you get from the address bar when
you browse folders inside Panopto's own interface, and the id is quoted and
URL-encoded there. Other Panopto institutions work too — the host is read from
whatever you paste, defaulting to Exeter's.

### Transcript only

Tick **Transcript only** (or pass `--captions-only` to `sync`) to fetch just the
captions and metadata, skipping the video entirely. Panopto's captions are all
the note-writing actually needs, and they arrive in seconds rather than the
minutes a 150 MB screen recording takes.

The trade is slides: no video means no keyframes, so no OCR, and any figure or
formula the lecturer showed but didn't say aloud is missing. The bundle says so
at the top, so a model reading it knows not to invent them.

Good for sweeping a whole module quickly, then filling in the ones that matter.
A transcript-only lecture shows a **Get video & slides** button in the viewer
that downloads the rest in place. That upgrade path is why caption-only fetches
deliberately skip the download archive — recording the id there would make the
later full download a silent no-op.

Leave **Module name** blank and each lecture files itself by the module code in
its own title — a Recap folder is usually a mixed archive rather than one
module, and anything with no recognisable code lands in `Unsorted`. Adding a
module downloads and processes every lecture in it but doesn't
write notes for any — that would run for hours unattended. Open one and hit
**Write notes**.

### Traceability

The point of the viewer is that **every claim in the notes should be
attachable to a moment in the recording and to the slide that was on screen.**
So the layout is video and transcript on the left, notes on the right, and
everything is cross-linked:

- Timestamps in the notes are clickable — jump straight to the moment a claim
  came from
- Transcript lines and slide thumbnails are clickable — seek from either
- Playback highlights the current line and slide, and follows along
- Slide markers sit inline in the transcript, the same interleaving `bundle.md`
  uses, so you can see what was on screen when a line was said
- Each notes file gets a tab, so `notes.md` and `notes-antigravity.md` sit side
  by side for comparison
- **Verify** runs the figure check and shows unsupported numbers inline
- **Write notes** runs the whole pipeline with live progress

Video is served with HTTP range requests, so seeking within a 150 MB lecture
works without downloading the whole file.

## Disk

A lecture is ~150 MB downloaded but ~3 MB once processed, so by default the
recording is deleted after its slides and transcript are extracted. A term
lands in a few hundred MB rather than several GB.

Nothing the notes depend on is lost: slides are images on disk, the transcript
is text, and every timestamp links back into Panopto for the moments you want
to hear. The viewer shows **Watch on Panopto** for a pruned lecture rather than
playing locally.

```bash
./lecturescrape.py status              # sizes, and what's reclaimable
./lecturescrape.py prune --dry-run     # what would go
./lecturescrape.py prune               # reclaim it
```

Set `keep_video: true` (or pass `--keep-video` to `process`) to keep recordings
for offline playback.

A pruned lecture is distinguishable from one that was never downloaded: slides
can only exist if a video was decoded, so their presence without a video means
it was pruned. That's why the viewer offers *Watch on Panopto* for a pruned
lecture and *Get video & slides* for a transcript-only one — re-downloading
150 MB you deliberately discarded would be the wrong fix.

## Sessions

The Recap cookie expires every few hours. Folder and caption fetches now check
it once before starting, so a 15-lecture module fails immediately with a clear
message rather than fifteen times with yt-dlp's own wording. Log back into
Recap in the browser named in `config.yaml` and re-run.

## Obsidian export

The point of the whole pipeline: if you miss a lecture, the teaching still
lands in your knowledge base.

```bash
./lecturescrape.py export "BEF2014" --with-transcript
```

Or hit **Send to Obsidian** in the viewer. Set the vault once in
`config.yaml`:

```yaml
obsidian_vault: "/path/to/your/vault"
obsidian_folder: "Lectures"
```

What lands:

```
Lectures/
  BEF2014.md                                   module index
  BEF2014/
    2026-03-24 Financial Reporting and Analysis.md
    2026-03-24 Financial Reporting and Analysis (transcript).md
  attachments/
    BEF2014-dc340e97-slide-01.jpg …
```

**Every timestamp becomes a link into the recording.** `20:29` in the notes
becomes a link to `…Viewer.aspx?id=…&start=1229`, so you read the note and
click through to hear anything you want in the lecturer's own words. Each slide
in the export carries the same jump link. That's what makes a note you didn't
sit through still trustworthy — nothing is a dead claim, it all traces back.

Notes carry YAML frontmatter (`module`, `date`, `duration`, `source`,
`lecture-id`, tags), so Dataview can query them and the graph clusters by
module. `[[BEF2014]]` resolves to an index note listing every lecture with its
date and length.

Two things worth knowing:

- Slide images are copied in with **vault-unique names** (`<module>-<id>-slide-NN.jpg`).
  Every lecture produces a `slide_0001.jpg`, so plain names would make
  `![[…]]` embeds resolve to whichever one Obsidian happened to find first.
- The module index is rebuilt on each export, but **anything you write above
  the `<!-- lecturescrape:index -->` marker is preserved** — so it's safe to
  add your own notes at the top.

`--all` exports every lecture that has notes.

## Cross-lecture concepts

Each lecture note is an island until its ideas are linked to the other lectures
that cover them.

```bash
./lecturescrape.py concepts                    # concepts seen in 2+ lectures
./lecturescrape.py concepts --min-lectures 1   # every concept
./lecturescrape.py concepts --module BEE2041   # one module
```

This writes `Lectures/Concepts/<Term>.md`, each listing every lecture the idea
appears in, with a jump link straight to the moment it was introduced. Exported
lecture notes get their bolded key terms wikilinked to those concept notes, so
Obsidian's graph joins lectures that share ideas rather than just lectures that
share a module. `--no-links` on `export` turns that off.

Extraction is deterministic, not another model pass: the notes are written to a
known shape, so the **Key concepts** section can simply be parsed. Two things
that shape forced:

- **Only top-level bullets count.** Indented children are a concept's own parts
  ("Statutory:", "Effective:") or asides that bold a word mid-sentence, and
  taking those produced fragments like `contingent` and a stray `Statutory`
  alongside the real `Statutory vs. Effective Tax Rate`.
- **Variants are merged.** `Deferred Tax Asset (DTA)`, `deferred tax assets`
  and `Deferred Tax Asset` normalise to one key, or the vault fills with
  near-duplicate notes each holding a third of the links. The longest spelling
  seen becomes the display name.

## The Chrome extension

Rather than remembering folder URLs, you can just click a button on the lecture
you're already watching.

Start the daemon once (leave it running in a terminal):

```bash
./lecturescrape.py serve
```

Load the extension: `chrome://extensions` → Developer mode → **Load unpacked** →
pick the `extension/` folder.

Now every Panopto viewer page gets two buttons, bottom right:

- **Notes** — downloads, transcribes, extracts slides, sends the transcript off
  for analysis, shows the notes in a side panel
- **+slides** — same but sends `bundle.md`, which includes the slide images.
  Only worth it on a vision model.

Progress streams into the panel as it goes. Everything is cached, so clicking
Notes again on a lecture you've already done returns instantly.

The daemon binds to `127.0.0.1` only — nothing is exposed off the machine. The
extension talks to it from the service worker rather than the page, because
Chrome blocks page-level requests to localhost from an https origin.

The module name is guessed from the Panopto title (it picks out codes like
`BEF2014`), so lectures file themselves into the right folder.

The daemon reads `config.yaml` once at startup — **restart it after editing the
config**, or you'll keep getting the old behaviour.

## Authentication

Recap sits behind Exeter SSO, so `yt-dlp` borrows a session cookie from Chrome.
This works as long as you're logged into Recap in Chrome's **default profile** —
that's what `browser: chrome` in `config.yaml` reads.

Other browsers on this machine didn't work: Brave and Chrome's Profile 1/2 have
keychain entries the terminal can't reach, and Safari's cookie file is
TCC-protected (it needs Full Disk Access for your terminal, in System Settings →
Privacy & Security).

Sessions expire every so often, and there are two symptoms rather than one. A
single lecture fails loudly: *"This video is only available for registered
users."* A folder doesn't fail at all — Panopto answers an unauthenticated
folder listing with an empty list, so `yt-dlp` exits successfully having found
nothing, and an unguarded sync reports success having downloaded no lectures.
`sync` and `autosync` check for both before starting and say which one they hit.
Either way: log back into Recap and re-run.

## Use

Find a module's folder URL in Recap (`…/Pages/Sessions/List.aspx?folderID=…`)
and add it to `config.yaml`:

```yaml
sources:
  - name: "Cell Biology"
    url: "https://recapexeter.cloud.panopto.eu/Panopto/Pages/Sessions/List.aspx?folderID=..."
```

Then:

```bash
./lecturescrape.py sync              # download anything new
./lecturescrape.py process           # transcribe + extract slides
./lecturescrape.py status            # what's done
```

Use this for bulk work — a whole term in one go — and the extension for the
lecture in front of you. They share the same library and download archive, so
they never duplicate effort.

`sync` keeps a download archive, so re-running it only fetches lectures you
don't already have — safe to run weekly for a whole term.

One-off, without touching the config:

```bash
./lecturescrape.py sync --url "https://recapexeter.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id=..." --name "Cell Biology"
```

### Which stream gets downloaded

Panopto serves the same lecture three ways. For a 2-hour recording:

| Stream | Contents | On disk (2 hours) |
|---|---|---|
| `DV` | dual view — camera and slides tiled into quadrants, with audio | ~470 MB |
| `OBJECT` | the slides alone, full frame, **no audio** | ~160 MB |
| `PODCAST` | Panopto's own merged download | varies |

The default pulls `OBJECT`. That matters for more than size: `DV` composites the
room camera and the slides into a 2×2 grid, so the slide only occupies a quarter
of the frame and reads much worse to a vision model. `OBJECT` is the slide at
full 1280×720.

Audio is never needed unless Whisper has to run, and then the smallest
audio-bearing stream is fetched separately.

Don't trust yt-dlp's `-F` size column here — the `DV` streams advertise roughly
3× what they actually take, because Panopto reports peak rather than average
bitrate. To see what a given lecture offers:

```bash
yt-dlp -F --cookies-from-browser chrome "<lecture url>"
```

## How the transcript is made

Panopto's own captions are used when present — they're already aligned and cost
nothing. Otherwise the audio is transcribed locally with MLX Whisper, which runs
fast on Apple Silicon. Force local transcription with `--force-whisper` if the
Panopto captions are poor (they often are for technical vocabulary).

## Slides

Keyframes come from ffmpeg scene detection, so you get one image per slide
change rather than one per second.

Three things the raw detector gets wrong, all handled:

- **Cross-fades.** A single slide change fires the detector three or four times
  as the transition plays. Keyframes closer together than `min_slide_gap`
  (2s) are collapsed to one, keeping the *last* — the frame where the new slide
  has finished rendering rather than a half-faded blend.
- **Over-triggering.** If a lecture is mostly a talking head you can get
  hundreds of frames. `max_slides` (60) thins them evenly. It thins rather than
  re-runs, because a second detection pass over a 2-hour video costs minutes.
- **Fades slow enough to vanish.** The detector scores each frame against the
  one before it, so at a video's own 25 fps it's being asked whether 1/25th of a
  second changed anything. A slide that cross-fades over half a second is 25
  changes too small to clear the threshold, and is missed outright.
  `scene_scan_fps` (2) thins the stream first, putting the whole transition
  between one comparison and the next.

That last one is also where the scan time went. Measured on a 1080p
reconstruction of a real lecture: **4.33s at full rate against 1.40s at 2 fps**,
finding the same slides on hard cuts — and on a version cross-fading over 0.4s,
one transition of three rather than none at all. Set `scene_scan_fps: 0` to score
every frame as it used to.

Hardware decoding is the obvious next idea and it's a trap: `-hwaccel
videotoolbox` measured **10.47s** on the same file, 2.4x *slower*, because every
frame has to be copied back off the GPU for the filter anyway.

Take the cross-fade half of that on its merits, though: it is measured on
synthetic fades, and there's no sign the existing library needed it. Checking
every keyframe gap over five minutes against the transcript — on the theory that
a missed slide would leave the lecturer audibly pointing at one — the largest
gaps (15, 12, 8 and 63 minutes) contain no slide references at all. They look
like genuine dwells. The speedup is real either way; the recovered-slides benefit
is unproven on real recordings.

Tune `slide_threshold` if you want more or fewer.

### Slide OCR

Every kept slide is read with Apple's Vision framework and the text is inlined
into `bundle.md` under its image. This is the single biggest quality lever in
the pipeline, for two reasons:

- **Text-only models can read the slides.** A local Gemma or Qwen can't see a
  JPEG, but it can read `Double-declining balance (DDB) rate = 33.33% × 2 =
  66.67%`. Panopto's ASR renders that as "times two give you 66 six".
  Equations, tables and figures survive; the transcript's version of them
  doesn't.
- **It's a much better duplicate test than timing.** Animated builds re-show the
  previous slide plus a line. Comparing OCR text catches those regardless of how
  far apart they are; on a real lecture it merged 36 keyframes down to 30 after
  the timing pass had already run.

Vision needs no model download and runs on the GPU — measured at 3.96s for one
lecture's 29 slides. Don't bother threading that loop: it already serialises on
the GPU, and a `ThreadPoolExecutor` over the same slides came back 3% faster,
which is noise. It's a separate stage from frame extraction, so
enabling it on already-downloaded lectures doesn't re-decode the video:

```bash
pip install pyobjc-framework-Vision
./lecturescrape.py process --force
```

Set `ocr: false` to skip it. If the module is missing the pipeline says so and
carries on without it.

Ideas for this stage came from [drpwchen/lecture-to-notes](https://github.com/drpwchen/lecture-to-notes),
which does the same job with a much heavier stack (Surya + RapidOCR + a local
VLM). Worth reading if you want to go further — particularly its
never-auto-correct-the-transcript rule and its cross-correlation alignment for
multi-camera recordings.

## Handing it to a model

`bundle.md` is designed to be dropped straight into a long-context model —
drag the lecture folder into Gemini/AI Studio and ask for notes, and the slide
images resolve as relative paths alongside the text.

```bash
./lecturescrape.py analyse "Enzyme Kinetics"     # one lecture
./lecturescrape.py analyse --module BEE2041      # a whole module
./lecturescrape.py analyse --all                 # everything without notes
```

Batch runs skip lectures that already have notes and keep going past a failure,
so a term can be left to process unattended. `--redo` rewrites existing notes.

A batch is almost entirely spent waiting on someone else's model, and lectures
don't depend on each other, so `--jobs N` puts several in flight at once. On four
BEE2041 lectures through `agy`: **2m51s at `--jobs 1`, 50s at `--jobs 4`.**

```bash
./lecturescrape.py analyse --module BEE2041 --backend antigravity --jobs 4
```

It stays at 1 unless you ask, because the local backend is a single Ollama
instance — handing it four lectures at once just makes all four slow. Raise it
for a hosted backend.

### What the notes contain

**You already have the slides**, so summarising them back is worthless. The
default prompt asks for the opposite: only what the recording adds that the
slides don't.

- **Beyond the slides** — the explanations, intuitions and asides given aloud
  that appear nowhere in the slide text. This gets the most room.
- **Emphasis and exam signals** — quoted directly.
- **Corrections** — where the lecturer contradicted or fixed what was on screen.
- **Questions and answers** from the room.
- **Key concepts** in the lecturer's framing, not the slide's wording.
- **Gaps** — what was rushed or assumed.

Sections with nothing genuine say "nothing notable" rather than padding
themselves from slide content.

This only works when slides exist to differ from, so `analyse` picks the style
per lecture: the delta prompt when slide text is present, a plain summary for
transcript-only lectures. `--style delta|summary` overrides, `--prompt file`
replaces it entirely.

The difference is stark. A summary tells you deferred tax assets exist; the
delta tells you the lecturer said not to memorise the DTA/DTL rule, that fines
aren't deductible because a deduction would have the government subsidising the
penalty, and that the units on one exercise slide were wrong.

### Checking the notes

Notes are for revising from, so a confident wrong number is worse than a vague
right one — you memorise it. `verify` checks every figure in a set of notes
against the transcript and slide OCR, and flags citations pointing past the end
of the recording:

```bash
./lecturescrape.py verify "BEF2014"
./lecturescrape.py verify "BEF2014" --notes notes-antigravity.md
```

It works on any notes file, so you can hold two models to the same standard.

It also checks **quotes**, which matters more than figures for delta-style
notes: those carry few numbers (the numbers are on your slides) but many direct
quotations, and a plausible line the lecturer never said is exactly the failure
this style invites. A quote passes if its words appear in the transcript, with
tolerance for the ASR mangling wording.

That check earns its place. Across twelve lectures it found 9 unverified quotes
out of 80 — mostly tidied-up paraphrases wrapped in quote marks rather than
inventions, which is why the prompt now says to quote only actual transcript
wording.

**What it can't catch:** misattribution. A real figure attached to the wrong
concept passes, because the number does appear in the source. It's also lenient
by nature — any number occurring anywhere in a 22k-token bundle counts as
supported. So treat a clean run as "nothing was invented", not "everything is
correct".

### Backends

**Local (default).** Ollama is already running on `:11434` and needs no key:

```yaml
backend: "openai"
endpoint: "http://localhost:11434/v1"
model: "qwen3.8:27b-mlx"
```

**Which local model.** One BEF2014 lecture, same prompt, all on the same 32 GB
Mac. "Wrong" is quotes that don't appear in the transcript, per `verify`. The
last two rows are the fair pair — identical trimmed 80k-char bundle; the rows
above saw the untrimmed 90k one, before `est_tokens` was corrected:

| model | on disk | wall | prefill | gen | notes | quotes / wrong |
|---|---|---|---|---|---|---|
| `gemma4:12b-mlx` | 7.7 GB | 448s | 116 t/s | 20.3 t/s | 9.4 KB | 12 / 1 |
| `gemma4:26b-mlx` | 17 GB | **207s** | 318 t/s | 27.0 t/s | 4.9 KB | 9 / 0 |
| `muse-glimmer:30b-mlx` | 21 GB | 1614s | 202 t/s | 10.2 t/s | 12.0 KB | 30 / **0** |
| `nemotron` *(untrimmed)* | 22 GB | 597s | **880 t/s** | 36.1 t/s | 15.7 KB | 44 / 2 |
| **`qwen3.8:27b-mlx`** | **18 GB** | **661s** | 182 t/s | 13.7 t/s | **22.4 KB** | **102** / 6 |
| `nemotron-3.5-lightning` | 22 GB | 1467s | 486 t/s | 37.2 t/s | 11.8 KB | 52 / 8 |

qwen3.8 is the default on that comparison: on the same input it is 2.2x faster
than nemotron for 1.9x the notes, cites 47 figures with **none** unsupported
against nemotron's 11, and quotes twice as often at a third the error rate. It
also reads slide images, so `--vision` needs no second model — one model is the
whole pipeline.

Note which row has the *lowest* generation rate. Raw tok/s barely predicts
anything here: what matters is how much of the generation is reasoning the model
then discards. qwen3.8 spent 5 KB on thinking; nemotron burned 160 KB.

**Nemotron's problem is variance**, which is why it isn't the default despite
the fastest prefill here. The same lecture took 597s and 18.7k generated tokens
one run, 1467s and 52k the next — a term of twelve could take two hours or five.
It also intermittently returns an empty message with the finished notes in the
reasoning field, three runs of four, the fourth returning identical work as
content. `analyse` falls back to that channel and says so on stdout; without it
the run fails outright with the notes already written and thrown away. Worth
knowing if you point that model anywhere else.

Take `gemma4:26b-mlx` when you want a term done fast and can live with notes a
quarter the length.

**Size does not decide this.** `qwen3.6:27b-mlx` at 19 GB overcommits a 32 GB
machine once a 32k KV cache lands on top, and thrashes at 0.3 tok/s — three hours
for one lecture. Nemotron is 3 GB larger and perfectly happy. Architecture and
KV-cache shape matter more than the number on the download, so measure a
candidate before trusting it; `--label` exists for exactly that.

**The context window is a server setting, and `analyse` reads it rather than
assuming it.** Ollama serves 32,768 by default, and you cannot ask for more per
request: `/v1/chat/completions` silently drops `options`, so passing
`num_ctx: 65536` through the client's `extra_body` returns a perfectly good
answer while `ollama ps` still reports 32,768. Only the native `/api/generate`
honours it, which is why a benchmark script can set the window and the real code
path can't.

What you can do is raise it on the server:

```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 65536   # then restart Ollama
```

`analyse` asks `/api/ps` what window is actually being served and budgets against
that, falling back to `max_context_tokens` only for endpoints that aren't Ollama.
So the two can't drift apart — a config that disagrees with the server would
otherwise either trim slide text for no reason or overrun in silence.

**It's worth doing.** The same BEF2014 lecture, trimmed to fit 32,768 versus sent
whole into 65,536:

| | 32,768 | 65,536 |
|---|---|---|
| wall | 661s | 976s |
| figures cited | 47 | **93** |
| unsupported | 0 | 2 |
| quotes / unverified | 102 / 6 | 103 / **3** |

Twice the figures at a 2% error rate, and *better* quote fidelity, for about five
minutes more per lecture. Memory holds: 22 GB resident with the larger cache.

`est_tokens` is deliberately pessimistic for the same family of reasons. Four
characters a token is a prose rule, and a bundle is timestamps, numbers and
markdown: measured at 3.10 chars/token for gemma4, 3.22 for nemotron and 3.61 for
muse-glimmer. It uses `//3`, above every ratio measured, so `fit_to_context` trims
while there is still room.

The ceiling that matters is lower than the budget suggests, because the reply
shares the window with the prompt. A full set of notes is ~4,900 tokens, so at a
32,768 window anything past **~89,800 characters** overruns once the model starts
writing. BEF2014 sat just inside that at 90,075: sent whole it was 28,511 prefill
plus ~4,886 emitted, over by 629 tokens, with the start of the transcript falling
out silently.

**Gemini, metered.** A Google AI Pro plan is *not* API access — the API bills
per token through a separate account. Either set `GEMINI_API_KEY` and point the
`openai` backend at Google's OpenAI-compatible endpoint, or use
`backend: "gemini-cli"`, which shells out to Gemini CLI.

Both bill per token. As of August 2026 Google withdrew Gemini Code Assist for
individuals from third-party clients, so the CLI can no longer spend an AI Pro
subscription — it reports *"This client is no longer supported for Gemini Code
Assist for individuals"* and points you at Antigravity. Which, as it turns out,
is scriptable.

**Gemini, on the subscription.** Antigravity ships a CLI, `agy`, and its print
mode answers one prompt and exits — so it drops straight into the backend slot
the Gemini CLI vacated:

```yaml
backend: "antigravity"
model: "gemini-3.1-pro-high"
```

`agy models` lists the ids it takes, which include Claude Opus 4.6 and Sonnet 4.6
alongside the Gemini line. It has to be on PATH and signed in — run `agy` once in
a terminal — and it wants one of those ids rather than a local tag like
`gemma4:12b-mlx`, which the backend checks for up front rather than failing a
lecture later.

Two details differ from the other backends and are worth knowing if you edit the
prompts. `agy` ignores stdin, so the bundle is passed as a command-line argument;
that puts the ceiling at `ARG_MAX` rather than a context window, which at ~600 KB
is about ten times the largest bundle here, so `max_context_tokens` is ignored
and nothing gets trimmed. And it's an agent in a workspace rather than a
completion endpoint: any tool it reaches for in print mode is auto-denied,
because there's nobody there to approve it. The prompt therefore tells it the
lecture is already inline and that the answer comes back as the reply — a custom
`--prompt` asking it to read or write files will fail on that.

Measured at 45 seconds for a two-hour lecture on `gemini-3.6-flash-low`, most of
which is startup.

**Comparing backends.** `--model`, `--backend` and `--label` override the config
for one run, so you can put two models side by side on the same lecture without
overwriting the result:

```bash
./lecturescrape.py analyse "BEF2014" --slides --model "gemma4:e4b-mlx" --label "e4b"
./lecturescrape.py analyse "BEF2014" --slides --backend antigravity --model "gemini-3.1-pro-high" --label "pro"
```

That writes `notes-e4b.md` and `notes-pro.md` alongside each other. Worth doing
once on a lecture you know well, to see whether the subscription model is worth
the wait over the local default for routine lectures.

**Reading the slides, not just their text.** OCR is what flattens an equation
into nonsense, and the image it was reading is sitting right there on disk. With
`--vision`, or `vision: true` in config, the antigravity backend points the model
at the slide folder and tells it to open a slide before citing any figure,
formula or table:

```bash
./lecturescrape.py analyse "BEF2014" --slides --backend antigravity --vision
```

On the accounting lecture it opened four slides out of thirty-odd — the ones
with the numbers on — and left the prose slides alone. On a workflow lecture
whose slides are screenshots it opened none, correctly. Reckon on a few extra
tool round trips: 50 seconds against 45 on the same lecture.

It reads only. `view_file` needs no permission grant, which is what makes this
safe to leave running; `run_command` and `write_to_file` do, and a permission
prompt in print mode ends the run, so the prompt rules both out and the notes
still come back over stdout for `analyse` to write. Nothing gets approved on your
behalf — `--dangerously-skip-permissions` is not involved. `--vision` forces
`bundle.md` as the source, since `transcript.md` names no slides for it to open.

**The same thing locally, on the default model.** `--vision` also works on the
`openai` backend, and `qwen3.8:27b-mlx` can see, so nothing else is needed:

```bash
./lecturescrape.py analyse "BEF2014" --slides --vision
```

`ollama show` reports which models have the capability, and a model that doesn't
is turned away up front rather than silently ignoring every image sent — the
gemma4 line and nemotron are all text-only.

The mechanics differ from the agent route in a way worth knowing. There, the
model opens slides itself and pays nothing from your context. Here `analyse`
picks the slides and sends them, so they're charged against the same window as
the transcript — about 1200 tokens each, measured. `vision_slides` (6) caps how
many go, chosen by which carry the most figures, and their cost comes out of the
text budget before the bundle is trimmed to fit around them.

The selection agrees with the agent's own judgement: on the accounting lecture it
picks slides 13, 15, 20, 21, 32 and 40, four of which are exactly the ones
Antigravity chose to open unprompted.

Worth it for the maths-heavy modules specifically. Slide 13 of that lecture is
two depreciation tables, which OCR flattens into one jumbled column. Asked to
reproduce them, `qwen3.8:27b-mlx` returned **all 15 cells correct** across both
tables with the right column structure, in 51 seconds. `muse-glimmer:30b-mlx`
matched it cell for cell and additionally read the title as `Solution – Tax
Reporting (£)` where OCR had `(f)` — but takes 2.4x as long on a full lecture,
which is why it's no longer worth keeping around for this.

muse-glimmer end to end on that lecture took 18m51s, against 26m54s for the same
model on text alone: the images displace text from the window, so there's less to
prefill. `verify` found 50 quotations, none absent from the transcript.

Don't expect more *figures* in the notes, though. The delta prompt tells the
model you already have the slides and not to restate them, so it doesn't recite
the table it just read. What the pictures buy is being right about a number when
it does cite one, not citing more of them.

**Antigravity in the IDE.** Still there, still occasionally the better tool —
it's interactive, so you can argue with it about a slide. Open `library/` as the
workspace and ask; `process` drops an `AGENTS.md` there describing the folder
layout and the note-writing task, so *"write notes for the BEF2014 lecture"* is
enough. Ask for `notes-antigravity.md` and it sits beside the rest.

That route is manual per lecture. The backend isn't — `analyse --all --vision`
walks a term unattended, which is the point of it.

## A note on the recordings

These are Exeter's material, licensed to you for personal study. Keeping local
copies to revise from is the ordinary use; redistributing them isn't.
