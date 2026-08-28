"""Tests for ``app.fixer.runstate`` — the run bookkeeping carried in the issue.

This is the seam that makes one-shot runs work, so the round trip is pinned
hard: whatever a run writes, the next tick must read back identically, and a
comment thread with a mix of human comments, agent prose, and older footers must
resolve to the most recent state rather than the first one found.
"""
from app.fixer.runstate import (
    RunRecord,
    latest_record,
    parse_footer,
    render_comment,
    render_footer,
)


def make_record(**kw) -> RunRecord:
    base = dict(job_id="job-1", started_at=1_700_000_000.0)
    base.update(kw)
    return RunRecord(**base)


# --------------------------------------------------------------------------- #
# Round trip.
# --------------------------------------------------------------------------- #
def test_a_rendered_record_parses_back_identically():
    record = make_record(commit="abc1234", fix_forward_attempts=2, chain_parent=7,
                         notes=["tried bumping the memory limit"])
    assert parse_footer(render_footer(record)) == record


def test_a_minimal_record_round_trips():
    record = make_record()
    assert parse_footer(render_footer(record)) == record


def test_the_footer_is_invisible_markdown():
    """It must render as nothing, so a reader sees only the prose."""
    footer = render_footer(make_record())
    assert footer.startswith("<!--") and footer.endswith("-->")
    assert "\n" not in footer


def test_render_comment_keeps_the_prose_first():
    body = render_comment("**Findings:** the pod is OOMKilling.", make_record())
    assert body.startswith("**Findings:** the pod is OOMKilling.")
    assert parse_footer(body) == make_record()


# --------------------------------------------------------------------------- #
# Parsing edges.
# --------------------------------------------------------------------------- #
def test_a_comment_without_a_footer_has_no_record():
    assert parse_footer("just a human asking what is going on") is None


def test_an_empty_body_has_no_record():
    assert parse_footer("") is None
    assert parse_footer(None) is None  # type: ignore[arg-type]


def test_a_malformed_footer_is_treated_as_absent():
    """One unparseable comment must not wedge a run."""
    assert parse_footer("<!-- fixer-state: {not json} -->") is None


def test_a_footer_missing_the_job_id_is_rejected():
    assert parse_footer('<!-- fixer-state: {"commit":"abc"} -->') is None


def test_the_last_footer_in_one_body_wins():
    a = render_footer(make_record(job_id="old"))
    b = render_footer(make_record(job_id="new"))
    parsed = parse_footer(f"{a}\nsome prose\n{b}")
    assert parsed is not None and parsed.job_id == "new"


def test_a_null_commit_stays_none_rather_than_the_string():
    parsed = parse_footer('<!-- fixer-state: {"job_id":"j","started_at":1,"commit":null} -->')
    assert parsed is not None and parsed.commit is None


def test_a_zero_chain_parent_reads_as_no_parent():
    """Issue numbers start at 1, so 0 is the absent value, not a real ancestor."""
    parsed = parse_footer('<!-- fixer-state: {"job_id":"j","started_at":1,"chain_parent":0} -->')
    assert parsed is not None and parsed.chain_parent is None


# --------------------------------------------------------------------------- #
# latest_record across a whole thread.
# --------------------------------------------------------------------------- #
def test_latest_record_reads_the_most_recent_footer():
    thread = [
        "emo: this is broken",
        render_comment("investigating", make_record(job_id="first")),
        "viktor: any luck?",
        render_comment("pushed a fix", make_record(job_id="second", commit="deadbee")),
    ]
    record = latest_record(thread)
    assert record is not None
    assert (record.job_id, record.commit) == ("second", "deadbee")


def test_latest_record_skips_trailing_human_comments():
    thread = [
        render_comment("pushed", make_record(job_id="only", commit="c0ffee")),
        "viktor: thanks",
        "emo: confirmed fixed",
    ]
    record = latest_record(thread)
    assert record is not None and record.job_id == "only"


def test_latest_record_on_a_thread_with_no_footers_is_none():
    assert latest_record(["emo: broken", "viktor: looking"]) is None


def test_latest_record_on_an_empty_thread_is_none():
    assert latest_record([]) is None


def test_latest_record_skips_a_malformed_footer_for_an_older_valid_one():
    thread = [
        render_comment("investigating", make_record(job_id="good")),
        "<!-- fixer-state: {broken json -->",
    ]
    record = latest_record(thread)
    assert record is not None and record.job_id == "good"


# --------------------------------------------------------------------------- #
# elapsed_seconds — the clock is injected, never read here.
# --------------------------------------------------------------------------- #
def test_elapsed_seconds_measures_from_the_start_stamp():
    assert make_record(started_at=1000.0).elapsed_seconds(1090.0) == 90.0


def test_elapsed_seconds_never_goes_negative():
    """Clock skew between pods must not read as a run from the future."""
    assert make_record(started_at=2000.0).elapsed_seconds(1000.0) == 0.0


# --------------------------------------------------------------------------- #
# find_pushed_commit — ONLY an explicit marker counts.
# --------------------------------------------------------------------------- #
def test_an_explicit_marker_is_read():
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit(["Pushed-Commit: 9f8e7d6c5b4a"]) == "9f8e7d6c5b4a"


def test_a_backticked_marker_is_read():
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit(["Pushed-Commit: `abc1234`"]) == "abc1234"


def test_the_marker_is_found_amid_surrounding_prose():
    from app.fixer.runstate import find_pushed_commit
    body = ("**Resolved:** raised the gunicorn timeout.\n\n"
            "Pushed-Commit: 1234abcdef01\n\n"
            "Re-checked the symptom: /healthz answers in 40ms.")
    assert find_pushed_commit([body]) == "1234abcdef01"


def test_the_marker_must_be_on_its_own_line():
    """Line-anchored so a sha mentioned mid-sentence is never mistaken for a
    declaration — the whole point of requiring a marker."""
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit(["I would have used Pushed-Commit: abc1234 here"]) is None


def test_a_later_marker_supersedes_an_earlier_one():
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit([
        "Pushed-Commit: aaaaaaa",
        "CI was red, corrected it.\n\nPushed-Commit: bbbbbbb",
    ]) == "bbbbbbb"


def test_prose_alone_is_never_a_commit():
    """Regression: loose hex matching read an IMAGE TAG as a pushed commit, so a
    run that changed nothing showed as pushed and waited on CI forever."""
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit([
        "Verified live on the current image (`b0ef3eca`) — no code change warranted.",
        "Pushed abc1234 for this",
        "commit deadbeef1234",
    ]) is None


def test_a_job_id_in_prose_is_never_a_commit():
    """Regression: job ids are 12 hex characters and every run prints its own."""
    from app.fixer.runstate import find_pushed_commit
    bodies = [render_comment("Picked this up.\n\n_Fixer run `e91bc819f056`._",
                             make_record(job_id="e91bc819f056"))]
    assert find_pushed_commit(bodies) is None


def test_no_marker_means_not_pushed_which_escalates_rather_than_hangs():
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit(["Investigating.", "Still looking."]) is None


def test_the_footer_is_never_a_commit_source():
    """The footer carries the job id and the commit; reading it back would loop."""
    from app.fixer.runstate import find_pushed_commit
    body = render_footer(make_record(job_id="aaaaaaaaaaaa", commit="bbbbbbbbbbbb"))
    assert find_pushed_commit([body]) is None


def test_an_excluded_value_is_refused_even_when_declared():
    from app.fixer.runstate import find_pushed_commit
    assert find_pushed_commit(["Pushed-Commit: abc1234"], exclude={"abc1234"}) is None


def test_all_job_ids_still_collects_every_footer_id():
    from app.fixer.runstate import all_job_ids
    thread = [
        render_comment("one", make_record(job_id="e91bc819f056")),
        render_comment("two", make_record(job_id="35af11b495cb")),
    ]
    assert all_job_ids(thread) == {"e91bc819f056", "35af11b495cb"}


# --------------------------------------------------------------------------- #
# redispatch_attempts — restarts after a lost job, persisted like the rest.
# --------------------------------------------------------------------------- #
def test_redispatch_attempts_survives_a_round_trip():
    record = RunRecord(job_id="abc123", started_at=1.0, redispatch_attempts=1)
    parsed = parse_footer(render_comment("restarted", record))
    assert parsed is not None
    assert parsed.redispatch_attempts == 1


def test_a_footer_written_before_the_field_existed_reads_as_zero():
    """Old footers on live issues must keep parsing — a run in flight when this
    shipped would otherwise lose its state and be handed to a human."""
    legacy = ('done\n\n<!-- fixer-state: {"chain_parent":null,"commit":null,'
              '"fix_forward_attempts":0,"job_id":"abc123","notes":[],'
              '"started_at":1.0} -->')
    parsed = parse_footer(legacy)
    assert parsed is not None
    assert parsed.job_id == "abc123"
    assert parsed.redispatch_attempts == 0
