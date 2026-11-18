"""P1: the figures behind the progress cards: a month's pace, a task's pace."""

from datetime import UTC, datetime

from app.progress_cards import month_forecast, step_label, task_pace


def test_the_month_forecast_runs_the_spend_on_at_its_pace() -> None:
    # Halfway through September (30 days): $20 spent → $40 by the end.
    now = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    seen = month_forecast(20.0, 50.0, now)
    assert seen["elapsed"] == 0.5
    assert seen["forecast"] == 40.0
    assert seen["forecast_share"] == 0.8
    assert seen["month_end"] == "2026-09-30"


def test_no_forecast_this_early_in_a_month() -> None:
    """A day in, one task's spend would be multiplied thirtyfold."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert month_forecast(5.0, 50.0, now)["forecast"] is None


def test_a_project_without_its_own_limit_still_has_a_pace() -> None:
    now = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    seen = month_forecast(20.0, None, now)
    assert seen["forecast"] == 40.0 and seen["forecast_share"] is None and seen["share"] is None


def test_a_tasks_pace_forecasts_its_finish_and_warns_past_its_limit() -> None:
    inside = task_pace(done=2, total=6, elapsed_minutes=8.0, max_minutes=30)
    assert inside["remaining_minutes"] == 16.0
    assert inside["over_limit_by"] is None
    late = task_pace(done=1, total=6, elapsed_minutes=10.0, max_minutes=30)
    assert late["remaining_minutes"] == 50.0
    assert late["over_limit_by"] == 30.0


def test_no_pace_before_the_first_step_is_done() -> None:
    assert (
        task_pace(done=0, total=6, elapsed_minutes=4.0, max_minutes=30)["remaining_minutes"] is None
    )


def test_a_step_is_labelled_by_the_first_clause_of_its_brief() -> None:
    assert step_label({"id": "rts_rule", "brief": "仅核验EUR-Lex上的PSD2第97条，回答一个问题"}) == (
        "仅核验EUR-Lex上的PSD2第97条"
    )
    assert step_label({"id": "kyc_vendors", "brief": ""}) == "Kyc vendors"
    long = step_label(
        {"id": "x", "brief": "Compare the five largest KYC vendors on price and coverage"}
    )
    assert len(long) <= 36 and long.endswith("…")
