"""A notes name is a file inside the lecture, and writing notes loses nothing.

Two findings from the November 2026 audit.

The name came from the daemon's query string and from --notes, and was joined
onto the lecture directory unchecked. `d / name` follows ../ and is replaced
outright by an absolute name, so export would copy any readable file into the
vault and verify would read it.

Writing notes again replaced notes.md. On the claude backend that discarded a
result that had been paid for.
"""

import pytest


@pytest.mark.parametrize("name", [
    "../../../secret.txt", "../notes.md", "sub/notes.md", "bundle.md",
    "notes.md/../../x", "", "notes.txt",
])
def test_names_that_are_not_a_notes_file_are_refused(ls, lecture, name):
    d, _ = lecture()
    with pytest.raises(RuntimeError, match="not a notes file"):
        ls.notes_path(d, name)


def test_an_absolute_name_is_refused(ls, lecture, tmp_path):
    d, _ = lecture()
    secret = tmp_path / "notes-secret.md"   # even one shaped like a notes file
    secret.write_text("TOP SECRET")
    with pytest.raises(RuntimeError, match="not a notes file"):
        ls.notes_path(d, str(secret))


@pytest.mark.parametrize("name", ["notes.md", "notes-opus.md", "notes-haiku 5.5.md"])
def test_real_notes_names_resolve_inside_the_lecture(ls, lecture, name):
    d, _ = lecture()
    assert ls.notes_path(d, name) == d / name


def test_export_will_not_copy_a_file_from_outside_the_lecture(ls, cfg, lecture, vault, tmp_path):
    d, _ = lecture(notes="# Notes\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET 4242")

    for name in (str(secret), "../../../secret.txt"):
        with pytest.raises(RuntimeError, match="not a notes file"):
            ls.export_obsidian(cfg(), d, vault, name)

    assert not any("TOP SECRET" in p.read_text() for p in vault.rglob("*.md"))


def test_verify_will_not_read_a_file_from_outside_the_lecture(ls, lecture, tmp_path):
    d, _ = lecture(bundle="# Lecture\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET 4242")
    assert "not a notes file" in ls.verify_notes(d, str(secret))["error"]
    assert "not a notes file" in ls.verify_notes(d, "../../../secret.txt")["error"]


def test_writing_notes_again_keeps_the_earlier_version(ls, lecture):
    d, _ = lecture(notes="# First run\n")

    ls.write_notes(d, "notes.md", "# Second run")

    assert (d / "notes.md").read_text() == "# Second run\n"
    kept = list((d / ".trash").glob("notes.*.md"))
    assert [p.read_text() for p in kept] == ["# First run\n"]


def test_two_rewrites_in_one_second_keep_both_earlier_versions(ls, lecture, monkeypatch):
    monkeypatch.setattr(ls.time, "strftime", lambda fmt: "20261113-120000")
    d, _ = lecture(notes="one\n")
    ls.write_notes(d, "notes.md", "two")
    ls.write_notes(d, "notes.md", "three")
    assert sorted(p.read_text() for p in (d / ".trash").iterdir()) == ["one\n", "two\n"]


def test_rewriting_identical_notes_keeps_no_copy(ls, lecture):
    d, _ = lecture(notes="# Same\n")
    ls.write_notes(d, "notes.md", "# Same")
    assert not (d / ".trash").exists()


def test_the_viewer_does_not_list_kept_versions_as_notes(ls, lecture):
    """The viewer and analyse_batch glob notes*.md in the lecture directory.
    A kept version there would show up as a second set of notes."""
    d, _ = lecture(notes="one\n")
    ls.write_notes(d, "notes.md", "two")
    assert [p.name for p in d.glob("notes*.md")] == ["notes.md"]
