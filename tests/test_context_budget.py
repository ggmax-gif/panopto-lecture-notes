"""Trimming a lecture to fit a context window must not lie about what it cut.

Incidents:

8e78fee — trimming the tail of every slide kept 29 headings and threw away
their contents, while the prompt still told the model the slide text was
authoritative for every figure. Whole slides are dropped instead, sparsest
first, so a surviving slide can still be trusted.

1eb2a7c — the budget can go negative (an Ollama serving its 2,048 default),
and `stripped[:budget * 3]` with a negative bound is a negative slice: it
returned the LAST 71k characters, nearly the whole lecture pushed into a
window a tenth its size — the exact silent eviction this code prevents.
"""

import pytest


# Slide sizes ascend, so drop order (sparsest first) is predictable by index.
SLIDE_SIZES = (400, 800, 1200, 1600, 2000, 2400)


def bundle(slide_sizes=SLIDE_SIZES, transcript_lines=30):
    """A bundle shaped the way write_bundle emits one.

    Slides carry most of the characters, which is the case that matters: a
    slide-heavy lecture is what overruns a window in the first place.
    """
    out = ["# Lecture", ""]
    for i, size in enumerate(slide_sizes):
        out.append(f"**Slide text @ {i}:0{i % 6}:**")
        out.append(f"> slide {i} " + "x" * size)
        out.append("")
    for j in range(transcript_lines):
        out.append(f"[{j // 60}:{j % 60:02d}] transcript line {j} " + "word " * 12)
    return "\n".join(out)


def transcript_only_cost(ls, text):
    """What the bundle costs once every slide is gone — the floor below which
    no amount of slide-dropping can help."""
    stripped = "\n".join(l for l in text.splitlines()
                          if not l.startswith(("**Slide text @", "> ")))
    return ls.est_tokens(stripped)


def test_surviving_slides_are_intact(ls):
    """The property the whole design rests on: a slide that survives is
    complete, so anything it says can still be trusted."""
    text = bundle()
    blocks_before = ls.slide_blocks(text)
    budget = ls.est_tokens(text) // 2

    out, note, kept = ls.fit_to_context(text, budget)

    assert ls.est_tokens(out) <= budget
    assert "omitted" in note
    # every slide still present is byte-identical to the original
    original = {text.splitlines()[h:e][0]: "\n".join(text.splitlines()[h:e])
                for h, e, _ in blocks_before}
    lines = out.splitlines()
    for h, e, _ in ls.slide_blocks(out):
        header = lines[h]
        assert "\n".join(lines[h:e]) == original[header]


def test_the_transcript_is_never_cut_while_slides_remain(ls):
    text = bundle()
    out, _, _ = ls.fit_to_context(text, ls.est_tokens(text) // 2)
    assert "transcript line 0 " in out
    assert "transcript line 29 " in out


def test_sparsest_slides_go_first(ls):
    text = bundle()
    out, _, _ = ls.fit_to_context(text, ls.est_tokens(text) * 3 // 4)
    # slide 0 is the sparsest, slide 5 the densest
    assert "slide 0 " not in out
    assert "slide 5 " in out


def test_note_counts_what_it_dropped(ls):
    text = bundle()
    before = len(ls.slide_blocks(text))
    out, note, _ = ls.fit_to_context(text, ls.est_tokens(text) // 2)
    after = len(ls.slide_blocks(out))
    assert note.startswith(f"{before - after} of {before} slides omitted")


def test_nothing_to_trim_returns_the_text_unchanged(ls):
    text = bundle()
    out, note, kept = ls.fit_to_context(text, ls.est_tokens(text) + 1000)
    assert out == text
    assert note == ""
    assert kept > 0


def test_dropping_every_slide_does_not_claim_the_rest_are_complete(ls):
    """Budget set just above what the transcript alone costs, so every slide
    goes but nothing is truncated."""
    text = bundle(slide_sizes=(500, 500, 500), transcript_lines=10)
    budget = transcript_only_cost(ls, text) + 2

    out, note, kept = ls.fit_to_context(text, budget)

    assert kept == 0
    assert "TRUNCATED" not in note
    assert "none remain" in note or "all slides omitted" in note


def test_a_negative_budget_returns_nothing_not_everything(ls):
    """The incident, exactly: asked for a negative budget, the old code
    returned 23,876 estimated tokens."""
    text = bundle(slide_sizes=(400,) * 4, transcript_lines=200)
    out, note, kept = ls.fit_to_context(text, -500)
    assert ls.est_tokens(out) == 0
    assert out == ""
    assert kept == 0
    assert "TRUNCATED" in note


def test_the_tail_cut_respects_the_budget(ls):
    """est_tokens counts len//3, so the slice bound must be budget*3 — at
    budget*4 it handed back a third more than was asked for."""
    text = bundle(slide_sizes=(400, 400), transcript_lines=400)
    budget = 200
    out, note, _ = ls.fit_to_context(text, budget)
    assert "TRUNCATED" in note
    assert ls.est_tokens(out) <= budget


def test_a_bundle_with_no_slides_still_fits(ls):
    """A transcript-only lecture has no slides to drop, so the only lever is
    the tail cut — which must still respect the budget."""
    text = "\n".join(f"[0:{i:02d}] line {i} " + "word " * 20 for i in range(60))
    out, note, kept = ls.fit_to_context(text, 100)
    assert kept == 0
    assert ls.est_tokens(out) <= 100
    assert "TRUNCATED" in note


def test_slide_blocks_matches_what_write_bundle_writes(ls, tmp_path):
    """Guards against drift between the writer and the parser: if the header
    format changes on one side, trimming silently stops finding any slides
    and falls straight through to truncating the transcript.
    """
    d = tmp_path / "lec"
    d.mkdir()
    segments = [{"start": 0.0, "text": "hello"}, {"start": 5.0, "text": "world"}]
    slides = [{"file": "slides/slide_0001.jpg", "time": 12.0,
               "text": ["Regression table", "coef 5.63"]},
              {"file": "slides/slide_0002.jpg", "time": 40.0,
               "text": ["Second slide"]}]
    ls.write_bundle(d, segments, slides)

    written = (d / "bundle.md").read_text()
    assert len(ls.slide_blocks(written)) == 2, \
        "slide_blocks no longer recognises the headers write_bundle emits"
