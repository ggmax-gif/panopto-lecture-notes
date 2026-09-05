"""`prune` deletes the recording. What it takes as proof matters.

Incident (8ac1488): prune_video treated "a bundle exists" as proof the slides
had been extracted. A transcript-only lecture has a bundle *before* it has a
recording, so pruning one destroyed the video the moment it arrived — and
`process` then reported "skip (done)" forever, because the bundle it checks for
was present and the video it needed was gone.
"""

import json


def make(lecture, **kw):
    d, _ = lecture(**kw)
    return d


def test_transcript_only_lecture_keeps_its_new_video(ls, lecture):
    """The incident. Bundle from the captions-only pass, video freshly
    downloaded, slides not yet extracted."""
    d = make(lecture, bundle="# transcript only", video=True, slides_json=None)

    assert ls.prune_video(d) == 0
    assert ls.find_video(d) is not None


def test_empty_slides_json_is_not_proof(ls, lecture):
    d = make(lecture, bundle="# b", video=True, slides_json="[]")
    assert ls.prune_video(d) == 0
    assert ls.find_video(d) is not None


def test_unreadable_slides_json_is_not_proof(ls, lecture):
    d = make(lecture, bundle="# b", video=True, slides_json="{not json")
    assert ls.prune_video(d) == 0
    assert ls.find_video(d) is not None


def test_unprocessed_lecture_is_not_pruned(ls, lecture):
    d = make(lecture, video=True, slides_json='[{"file": "slides/a.jpg"}]')
    assert ls.prune_video(d) == 0
    assert ls.find_video(d) is not None


def test_a_processed_lecture_is_pruned(ls, lecture):
    d = make(lecture, bundle="# b", video=True,
             slides_json='[{"file": "slides/slide_0001.jpg", "time": 1.0}]')

    freed = ls.prune_video(d)

    assert freed > 0
    assert ls.find_video(d) is None


def test_is_pruned_distinguishes_thrown_away_from_never_downloaded(ls, lecture):
    """fetch_url uses this to decide whether re-downloading would fix a
    lecture or waste a hundred megabytes."""
    never = make(lecture, bundle="# b", slides_json=None)
    assert not ls.is_pruned(never)

    pruned = make(lecture, module="BEE2041", bundle="# b",
                  slides_json='[{"file": "slides/a.jpg"}]')
    assert ls.is_pruned(pruned)

    still_here = make(lecture, module="BEF2016", bundle="# b", video=True,
                      slides_json='[{"file": "slides/a.jpg"}]')
    assert not ls.is_pruned(still_here)


def test_slides_extracted_matches_is_pruned(ls, lecture):
    """The two asked the same question in two places once, and drifted. They
    now share a predicate; this keeps them sharing it."""
    for kw in ({"slides_json": None},
               {"slides_json": "[]"},
               {"slides_json": "{bad"},
               {"slides_json": '[{"file": "slides/a.jpg"}]'}):
        d = make(lecture, module=f"M{abs(hash(str(kw))) % 999}", bundle="# b", **kw)
        assert ls.is_pruned(d) == (ls.slides_extracted(d) and ls.find_video(d) is None)


def test_prune_reports_the_bytes_it_freed(ls, lecture):
    d = make(lecture, bundle="# b", video=True,
             slides_json='[{"file": "slides/a.jpg"}]')
    size = ls.find_video(d).stat().st_size
    assert ls.prune_video(d) == size
