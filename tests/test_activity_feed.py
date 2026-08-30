"""The mapping wait can be 10-20 minutes.

Without a feed the page shows one stage label over a creeping bar for all of it,
and a run going wrong looks exactly like a run going well until the workbook
lands. These cover the rendering and the plumbing that carries it to the browser.
"""

from __future__ import annotations

from hotel_pl_normalizer.activity import describe_round, describe_tool_call
from hotel_pl_normalizer.web import jobs as jobstore


def test_tool_calls_are_described_in_the_reader_s_terms():
    """Sheets and rows, not tool names and argument dicts."""
    assert describe_tool_call(
        "read_range", {"sheet_name": "Rooms Detail", "start_row": 120, "end_row": 180}
    ) == 'Read rows 120-180 of "Rooms Detail"'
    assert describe_tool_call("inspect_workbook", {}) == "Looked over the workbook's sheets"
    assert (
        describe_tool_call("find_rows", {"query": "banquet"})
        == 'Searched for "banquet"'
    )
    assert (
        describe_tool_call("validate_mapping", {"decisions": [{}, {}, {}]})
        == "Proposed a mapping for 3 accounts"
    )
    assert (
        describe_tool_call("patch_mapping", {"replacements": [{}]})
        == "Revised 1 account"
    )


def test_a_failed_call_says_so():
    """Repeated corrections are how a run going wrong becomes visible early."""
    line = describe_tool_call("read_range", {"sheet_name": "Rooms"}, ok=False)
    assert "did not work" in line


def test_an_unknown_tool_still_renders_something_readable():
    assert describe_tool_call("some_new_tool", {}) == "Some new tool"


def test_lines_stay_short_enough_for_the_feed():
    line = describe_tool_call("find_rows", {"query": "x" * 500})
    assert len(line) <= 120


def test_round_headers_count_their_steps():
    assert describe_round(3, 8, 14) == "Round 3 of 8 · 14 steps"
    assert describe_round(1, 8, 1) == "Round 1 of 8 · 1 step"
    assert describe_round(1, 8, 0) == "Round 1 of 8"


def test_activity_is_bounded_and_counts_as_progress():
    """Every line rewrites state.json, which the browser re-reads every 2.5s."""
    job = jobstore.Job(id="a" * 16, status=jobstore.RUNNING, source_name="x.xlsx", created_at=0.0)

    for n in range(jobstore.MAX_ACTIVITY_LINES + 25):
        job.log_activity(f"line {n}")

    assert len(job.activity) == jobstore.MAX_ACTIVITY_LINES
    # Oldest dropped, newest kept -- the feed reads bottom-up.
    assert job.activity[-1] == f"line {jobstore.MAX_ACTIVITY_LINES + 24}"
    assert job.last_progress_at is not None


def test_blank_lines_are_ignored():
    job = jobstore.Job(id="b" * 16, status=jobstore.RUNNING, source_name="x.xlsx", created_at=0.0)
    job.log_activity("   ")
    assert job.activity == []


def test_the_job_record_carries_activity_to_the_browser():
    job = jobstore.Job(id="c" * 16, status=jobstore.RUNNING, source_name="x.xlsx", created_at=0.0)
    job.log_activity("Round 1 of 8 · 6 steps")

    assert job.public()["activity"] == ["Round 1 of 8 · 6 steps"]
