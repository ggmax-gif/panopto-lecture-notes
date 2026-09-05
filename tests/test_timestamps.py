"""Every claim in the notes should be one click from the moment it was said.

Incidents:

8ac1488 — link_timestamps skipped any match starting with "[", meaning to
leave existing links alone. But the model brackets its timestamps as often as
not, and every transcript line is bracketed, so the bracketed form never
linked at all: 99 dead timestamps in the vault and ~6,500 transcript lines.

1eb2a7c / 7ac3414 — a bracketed *run* of timestamps ([19:01-19:34], and then
[2:09, 2:15]) linked each stamp but left the outer brackets as stray literals.
"""

import pytest

URL = "https://recapexeter.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id=abc&instance=M"
BASE = "https://recapexeter.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id=abc"


def link(ls, text):
    return ls.link_timestamps(text, URL)


def at(secs, label):
    return f"[{label}]({BASE}&start={secs})"


@pytest.mark.parametrize("source,expected", [
    # the two forms the model actually writes
    ("at 12:58 he says", f"at {at(778, '12:58')} he says"),
    ("[9:07]: the process", f"{at(547, '9:07')}: the process"),
    # every transcript line looks like this
    ("[0:01] Uh, so this whole notion", f"{at(1, '0:01')} Uh, so this whole notion"),
    # hours
    ("1:02:33 into it", f"{at(3753, '1:02:33')} into it"),
    # bracketed runs
    ("[19:01–19:34]", f"{at(1141, '19:01')}–{at(1174, '19:34')}"),
    ("[19:01-19:34]", f"{at(1141, '19:01')}-{at(1174, '19:34')}"),
    ("[2:09, 2:15]", f"{at(129, '2:09')}, {at(135, '2:15')}"),
    ("[2:09, 2:15, 3:00]", f"{at(129, '2:09')}, {at(135, '2:15')}, {at(180, '3:00')}"),
    # an unbracketed range still links both ends
    ("10:00-11:30", f"{at(600, '10:00')}-{at(690, '11:30')}"),
    # emphasis around a stamp
    ("**12:58** bold", f"**{at(778, '12:58')}** bold"),
])
def test_timestamps_become_links(ls, source, expected):
    assert link(ls, source) == expected


@pytest.mark.parametrize("source", [
    # already a link — rewriting it nests brackets and breaks the markdown
    f"see [12:58]({BASE}&start=778) again",
    "[Jump to this point](" + BASE + "&start=42)",
    # a wikilink is Obsidian's, not ours
    "[[2026-03-24 L1 Financial Reporting]] intro",
    "![[BEF2014-slide-01.jpg]]",
    "[[Note|3:05]] aliased",
    # code spans and both fence styles
    "`sleep 12:58` in code",
    "```\nt = 12:58\n```",
    "~~~\nt = 12:58\n~~~",
    # not times
    "ratio 3:4 and 12:99 are not times",
    "v1:23 build",
    "ratio 3.12:58 and 12:58.5 and v1.2:30",
    "at ١٢:٥٨ arabic numerals",
])
def test_left_alone(ls, source):
    assert link(ls, source) == source


def test_running_twice_changes_nothing(ls):
    """export re-runs over notes that may already have been exported."""
    text = ("at 12:58 and [9:07] and [2:09, 2:15] and [19:01–19:34] "
            "and [[Wiki]] and `3:05` and 1:02:33")
    once = link(ls, text)
    assert link(ls, once) == once


def test_the_instance_suffix_is_stripped_from_the_base(ls):
    """&instance=MoodleELE2 must not end up before &start=."""
    out = link(ls, "12:58")
    assert "instance" not in out
    assert out.endswith("&start=778)")


@pytest.mark.parametrize("stamp,secs", [
    ("0:00", 0), ("0:01", 1), ("9:07", 547), ("59:59", 3599),
    ("1:00:00", 3600), ("1:02:33", 3753),
])
def test_seconds_of(ls, stamp, secs):
    assert ls.seconds_of(stamp) == secs


def test_every_linked_stamp_parses(ls):
    """The regex and seconds_of must agree on what a timestamp is: anything
    the first matches, the second has to parse without raising."""
    import re
    text = " ".join(f"{h}:{m:02d}:{s:02d}" for h in range(3)
                    for m in (0, 7, 59) for s in (0, 30, 59))
    text += " " + " ".join(f"{m}:{s:02d}" for m in range(0, 60, 7) for s in (0, 59))
    out = link(ls, text)
    for start in re.findall(r"&start=(\d+)\)", out):
        assert start.isdigit()
