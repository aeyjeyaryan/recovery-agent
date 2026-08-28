"""Dashboard data-helper tests (pure pandas logic, no Streamlit runtime)."""

from __future__ import annotations

import pandas as pd
import pytest

from dashboard import utils


@pytest.fixture()
def failed_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": 1, "payment_id": "pay_1", "amount": 129900,
             "failure_reason": "insufficient_funds", "recovery_status": "recovered"},
            {"id": 2, "payment_id": "pay_2", "amount": 500,
             "failure_reason": "bank_timeout", "recovery_status": "in_progress"},
            {"id": 3, "payment_id": "pay_3", "amount": 250000,
             "failure_reason": None, "recovery_status": "pending"},
        ]
    )


@pytest.fixture()
def attempts_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": 1, "failed_payment_id": 1, "channel": "telegram",
             "outcome": "paid"},
            {"id": 2, "failed_payment_id": 2, "channel": "telegram",
             "outcome": "delivered"},
            {"id": 3, "failed_payment_id": 1, "channel": "sms",
             "outcome": "failed"},
        ]
    )


class TestComputeMetrics:
    def test_counts_and_recovery_rate(self, failed_df, attempts_df):
        m = utils.compute_metrics(failed_df, attempts_df)
        assert m["total_failed"] == 3
        # id=1 is status 'recovered' AND has the paid attempt.
        assert m["recovered"] == 1
        assert m["in_progress"] == 1
        assert m["unrecoverable"] == 0
        assert m["recovery_rate"] == pytest.approx(100 / 3)

    def test_empty_frames(self):
        empty = pd.DataFrame()
        m = utils.compute_metrics(empty, pd.DataFrame())
        assert m == {
            "total_failed": 0,
            "recovered": 0,
            "in_progress": 0,
            "unrecoverable": 0,
            "recovery_rate": 0.0,
        }


class TestBreakdowns:
    def test_channel_breakdown_sorted(self, attempts_df):
        counts = utils.channel_breakdown(attempts_df)
        assert list(counts.columns) == ["channel", "attempts"]
        assert counts.iloc[0]["channel"] == "telegram"
        assert counts.iloc[0]["attempts"] == 2

    def test_channel_breakdown_empty(self):
        assert utils.channel_breakdown(pd.DataFrame()).empty

    def test_failure_reason_breakdown_buckets_unclassified(self, failed_df):
        reasons = utils.failure_reason_breakdown(failed_df)
        names = set(reasons["failure_reason"])
        assert "unclassified" in names          # None reason bucketed
        assert "insufficient_funds" in names

    def test_failure_reason_breakdown_empty(self):
        assert utils.failure_reason_breakdown(pd.DataFrame()).empty

    def test_outcomes_breakdown(self, attempts_df):
        outcomes = utils.outcomes_breakdown(attempts_df)
        assert dict(zip(outcomes["outcome"], outcomes["count"])) == {
            "paid": 1, "delivered": 1, "failed": 1,
        }


class TestAmountConversion:
    def test_paise_to_rupees(self):
        s = pd.Series([129900, 500])
        assert list(utils.amount_inr(s)) == [1299.0, 5.0]


class TestGetEngine:
    def test_sqlite_url(self, tmp_path):
        engine = utils.get_engine(f"sqlite:///{tmp_path / 't.db'}")
        assert str(engine.url.database).endswith("t.db")
