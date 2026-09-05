"""Export must not destroy a note it did not write.

Incident (1eb2a7c): the supersede check read the first 800 characters of the
whole file, so a user's own revision note qualified as "generated" for
containing a pasted lecture link and a body bullet beginning "- transcript".
A purely user-authored note was permanently unlinked.

Two defences are tested here, and the second is the one that matters: even a
correct guard can misjudge, so removal must be recoverable.
"""

from conftest import VID

NOTES = "# Notes\n\nThe lecturer said something at 12:58.\n"


def export(ls, cfg, d, vault, **kw):
    return ls.export_obsidian(cfg(obsidian_folder="Lectures"), d, vault, **kw)


def test_user_note_mentioning_the_lecture_survives(ls, cfg, lecture, vault):
    """The exact file the incident destroyed.

    Frontmatter of the user's own; a pasted lecture URL in the body; and an
    indented bullet whose text begins "- transcript". All three of the old
    guard's conditions, none of them evidence this exporter wrote it.
    """
    d, _ = lecture(notes=NOTES)
    mine = vault / "Lectures" / "BEF2014" / "Week 1 revision plan.md"
    mine.write_text(
        "---\ntags:\n  - revision\n---\n\n"
        "# Week 1 revision\n"
        "- rewatch the tricky bits\n"
        "  - transcript is here if needed: "
        f"[recording](https://recapexeter.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id={VID})\n"
    )
    before = mine.read_text()

    export(ls, cfg, d, vault)

    assert mine.exists(), "a user-authored note was deleted"
    assert mine.read_text() == before


def test_note_whose_frontmatter_only_mentions_the_id_survives(ls, cfg, lecture, vault):
    """A `source:` line alone is not proof. The transcript companion is
    identified by `source:` *and* a transcript tag; a note by `lecture-id:`."""
    d, _ = lecture(notes=NOTES)
    mine = vault / "Lectures" / "BEF2014" / "My own index.md"
    mine.write_text(f"---\nsource: https://x/Viewer.aspx?id={VID}\ntags:\n  - mine\n---\n\nnotes\n")

    export(ls, cfg, d, vault)

    assert mine.exists()


def test_body_text_is_never_evidence(ls, cfg, lecture, vault):
    """Frontmatter is parsed to its closing `---` and no further."""
    d, _ = lecture(notes=NOTES)
    mine = vault / "Lectures" / "BEF2014" / "Body only.md"
    mine.write_text(
        "---\ntags:\n  - mine\n---\n\n"
        f"lecture-id: {VID}\n"          # in the BODY, not the frontmatter
        "  - transcript\n"
    )

    export(ls, cfg, d, vault)

    assert mine.exists()


def test_unclosed_frontmatter_is_not_frontmatter(ls, cfg, lecture, vault):
    d, _ = lecture(notes=NOTES)
    mine = vault / "Lectures" / "BEF2014" / "Unclosed.md"
    mine.write_text(f"---\nlecture-id: {VID}\n\nnever closed, so not a block\n")

    export(ls, cfg, d, vault)

    assert mine.exists()


def test_renamed_note_is_superseded_but_recoverable(ls, cfg, lecture, vault):
    """The behaviour the feature exists for — a note this exporter wrote under
    an older naming scheme — plus the guarantee that it goes to .trash/.

    No test can tell a generated note from a copy of one somebody annotated:
    they carry identical frontmatter. So removal is a move, never an unlink.
    """
    d, _ = lecture(notes=NOTES)
    stale = vault / "Lectures" / "BEF2014" / "2026-03-24 Financial Reporting.md"
    stale.write_text(
        f"---\ntitle: \"Financial Reporting\"\nmodule: BEF2014\n"
        f"lecture-id: {VID}\ntags:\n  - lecture\n---\n\nold generated body\n")

    r = export(ls, cfg, d, vault)

    assert not stale.exists(), "the stale generated note should be superseded"
    assert stale.stem in r["superseded"]
    recovered = vault / ".trash" / stale.name
    assert recovered.exists(), "superseded notes must be recoverable, not deleted"
    assert "old generated body" in recovered.read_text()


def test_trash_does_not_overwrite_an_earlier_casualty(ls, cfg, lecture, vault):
    """Two supersedes of the same filename must not lose the first."""
    trash = vault / ".trash"
    trash.mkdir()
    (trash / "2026-03-24 Financial Reporting.md").write_text("the first one")

    d, _ = lecture(notes=NOTES)
    stale = vault / "Lectures" / "BEF2014" / "2026-03-24 Financial Reporting.md"
    stale.write_text(f"---\nlecture-id: {VID}\ntags:\n  - lecture\n---\n\nthe second one\n")

    export(ls, cfg, d, vault)

    assert (trash / "2026-03-24 Financial Reporting.md").read_text() == "the first one"
    assert (trash / "2026-03-24 Financial Reporting (1).md").exists()


def test_a_different_lectures_note_is_untouched(ls, cfg, lecture, vault):
    other = "aaf677bd-ae57-45a5-bf7b-b40f00cfafab"
    d, _ = lecture(notes=NOTES)
    theirs = vault / "Lectures" / "BEF2014" / "2026-03-19 Another Lecture.md"
    theirs.write_text(f"---\nlecture-id: {other}\ntags:\n  - lecture\n---\n\nbody\n")

    export(ls, cfg, d, vault)

    assert theirs.exists()


def test_written_for_is_case_sensitive_about_the_id(ls, lecture):
    """A UUID differing only in case is a different string, and the guard
    should not guess that it means the same recording."""
    d, _ = lecture(notes=NOTES)
    note = d / "n.md"
    note.write_text(f"---\nlecture-id: {VID.upper()}\ntags:\n  - lecture\n---\n\nx\n")
    assert not ls.written_for(note, VID)


def test_module_note_counts_the_lecture_once(ls, cfg, lecture, vault):
    """The symptom that exposed the rename orphan: BEF2014.md said
    "2 lecture(s)" for one recording, because the old note was left behind."""
    d, _ = lecture(notes=NOTES)
    stale = vault / "Lectures" / "BEF2014" / "2026-03-24 Financial Reporting.md"
    stale.write_text(f"---\ndate: 2026-03-24\nlecture-id: {VID}\ntags:\n  - lecture\n---\n\nx\n")

    export(ls, cfg, d, vault)

    assert "1 lecture(s)" in (vault / "Lectures" / "BEF2014.md").read_text()
