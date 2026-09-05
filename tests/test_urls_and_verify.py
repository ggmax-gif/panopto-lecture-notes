"""Two smaller incident families.

URLs (the folder that listed 5 items instead of 131): a folder link copied
from Panopto's own UI keeps its id in a #fragment, which is never sent to the
server. Passed to yt-dlp raw, it listed some default folder instead.

verify (cf73427, acb7cdb, 933e1c5): each false positive below was a real one —
notes flagged as unsupported when they were not.
"""

import pytest

HOST = "recapexeter.cloud.panopto.eu"
FOLDER_ID = "a6724dd2-a15b-414f-afd2-b41600bd9a8a"
VIDEO_ID = "dc340e97-6b24-4109-8030-b41400e842da"


@pytest.mark.parametrize("pasted", [
    f"https://{HOST}/Panopto/Pages/Sessions/List.aspx#folderID=%22{FOLDER_ID}%22",
    f"https://{HOST}/Panopto/Pages/Sessions/List.aspx?folderID={FOLDER_ID}",
    f'https://{HOST}/Panopto/Pages/Sessions/List.aspx#folderID="{FOLDER_ID}"',
])
def test_folder_links_canonicalise_to_a_query(ls, pasted):
    """The hash fragment never reaches the server, so it must become ?folderID."""
    t = ls.parse_panopto(pasted)
    assert t["kind"] == "folder"
    assert t["id"] == FOLDER_ID
    assert t["url"] == (f"https://{HOST}/Panopto/Pages/Sessions/"
                        f"List.aspx?folderID={FOLDER_ID}")
    assert "#" not in t["url"]


@pytest.mark.parametrize("pasted", [
    f"https://{HOST}/Panopto/Pages/Viewer.aspx?id={VIDEO_ID}&instance=MoodleELE2",
    f"https://{HOST}/Panopto/Pages/Viewer.aspx?id={VIDEO_ID}",
    VIDEO_ID,
])
def test_lecture_links_canonicalise(ls, pasted):
    t = ls.parse_panopto(pasted)
    assert t["kind"] == "lecture"
    assert t["id"] == VIDEO_ID


def test_a_bare_uuid_assumes_the_default_host(ls):
    assert ls.parse_panopto(VIDEO_ID)["host"] == ls.DEFAULT_HOST


@pytest.mark.parametrize("junk", ["", "   ", "https://example.com/video", "hello"])
def test_rubbish_is_rejected(ls, junk):
    assert ls.parse_panopto(junk) is None


# ------------------------------------------------------------------- verify

def test_a_spaced_thousands_number_is_one_number(ls):
    """"21 000" read as 21 and 000 made a supported figure look invented."""
    assert ls.numbers_in("about 21 000 people") == ls.numbers_in("about 21000 people")


def test_quotations_pair_before_filtering_by_length(ls):
    """Pairing with a length filter let a skipped short quotation's closing
    mark pair with the next one's opening mark, so the prose between two real
    quotations was checked as though it were a quotation."""
    text = ('He called it "the" and then said "a much longer quotation that '
            'certainly passes any length filter you care to apply here".')
    quotes = ls.quotations(text)
    assert not any("and then said" in q for q in quotes)


def test_curly_quotations_are_found(ls):
    text = '“this is a curly quoted sentence of some length”'
    assert any("curly quoted" in q for q in ls.quotations(text))


SPOKEN = ["Welcome. The sample has 21 000 firms in it.",
          "As I said, the coefficient is 5.63 and it matters."]
SOURCE = "[0:01] " + SPOKEN[0] + "\n[0:30] " + SPOKEN[1] + "\n"


def test_verify_passes_a_note_whose_figures_are_all_real(ls, lecture):
    """Every one of these was a false positive once: the spaced thousands,
    the quotation, the timestamps."""
    notes = ('# Notes\n\n'
             '- The sample has 21 000 firms [0:01]\n'
             '- The coefficient is 5.63 [0:30]\n'
             '- He said "the coefficient is 5.63 and it matters" [0:30]\n')
    d, _ = lecture(bundle=SOURCE, spoken=SPOKEN, notes=notes)

    r = ls.verify_notes(d)

    assert r["unsupported"] == [], r
    assert r["bad_quotes"] == [], r
    assert r["clean"], r


def test_verify_catches_an_invented_figure(ls, lecture):
    notes = "# Notes\n\n- The sample has 44 219 firms [0:01]\n"
    d, _ = lecture(bundle=SOURCE, spoken=SPOKEN, notes=notes)

    r = ls.verify_notes(d)

    assert r["unsupported"], "an invented figure should be reported"
    assert not r["clean"]


def test_verify_catches_an_invented_quotation(ls, lecture):
    notes = '# Notes\n\n- He said "this sentence appears nowhere in the source" [0:30]\n'
    d, _ = lecture(bundle=SOURCE, spoken=SPOKEN, notes=notes)

    r = ls.verify_notes(d)

    assert r["bad_quotes"], "a fabricated quotation should be reported"


def test_verify_needs_a_bundle(ls, lecture):
    d, _ = lecture(notes="# Notes\n")
    assert "error" in ls.verify_notes(d)
