"""Tests for the v2 dashboard data layer + a Streamlit AppTest smoke run."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from dashboard import utils


@pytest.fixture()
def failed_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": 1, "payment_id": "pay_1", "amount": 129900,
             "failure_reason": "insufficient_funds", "recovery_status": "recovered"},
            {"id": 2, "payment_id": "pay_2", "amount": 50500,
             "failure_reason": "bank_timeout", "recovery_status": "pending"},
            {"id": 3, "payment_id": "pay_3", "amount": 250000,
             "failure_reason": None, "recovery_status": "unrecoverable"},
        ]
    )


@pytest.fixture()
def attempts_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": 1, "failed_payment_id": 1, "channel": "telegram",
             "outcome": "paid", "timestamp": datetime(2026, 8, 23, 10, 0)},
            {"id": 2, "failed_payment_id": 2, "channel": "telegram",
             "outcome": "delivered", "timestamp": datetime(2026, 8, 23, 11, 0)},
        ]
    )


class TestAmountKpis:
    def test_money_math(self, failed_df, attempts_df):
        k = utils.amount_kpis(failed_df, attempts_df)
        assert k["failed_amount_inr"] == pytest.approx(4304.0)   # 1299+505+2500
        assert k["recovered_amount_inr"] == pytest.approx(1299.0)
        # unrecoverable id=3 excluded from recoverable
        assert k["recoverable_amount_inr"] == pytest.approx(1804.0)
        assert k["at_risk_amount_inr"] == pytest.approx(505.0)   # pending id=2
        assert k["avg_attempt_per_payment"] == pytest.approx(2 / 3)

    def test_empty(self):
        k = utils.amount_kpis(pd.DataFrame(), pd.DataFrame())
        assert k["failed_amount_inr"] == 0.0
        assert k["recoverable_amount_inr"] == 0.0
        assert k["recovered_amount_inr"] == 0.0
        assert k["at_risk_amount_inr"] == 0.0
        assert k["avg_attempt_per_payment"] == 0.0


class TestPaidAttemptCountsAsRecovered:
    def test_pending_status_with_paid_attempt_is_recovered(self):
        failed = pd.DataFrame(
            [{"id": 7, "payment_id": "p", "amount": 10000,
              "failure_reason": "x", "recovery_status": "pending"}]
        )
        attempts = pd.DataFrame(
            [{"id": 9, "failed_payment_id": 7, "channel": "telegram",
              "outcome": "paid"}]
        )
        m = utils.compute_metrics(failed, attempts)
        assert m["recovered"] == 1
        assert utils.amount_kpis(failed, attempts)["recovered_amount_inr"] == 100.0


class TestRecoveryFunnel:
    def test_stage_counts_monotonic(self, failed_df, attempts_df):
        funnel = utils.recovery_funnel(failed_df, attempts_df)
        assert list(funnel["stage"]) == ["Failed", "Contacted", "Delivered",
                                         "Recovered"]
        counts = dict(zip(funnel["stage"], funnel["count"]))
        assert counts["Failed"] == 3
        assert counts["Contacted"] == 2          # unique payments attempted
        assert counts["Delivered"] == 1          # only pay_2 had 'delivered'
        assert counts["Recovered"] == 1
        assert counts["Failed"] >= counts["Contacted"] >= counts["Delivered"]

    def test_empty_attempts(self, failed_df):
        funnel = utils.recovery_funnel(failed_df, pd.DataFrame())
        # No outreach, but the 'recovered' status flag still counts.
        assert list(funnel["count"]) == [3, 0, 0, 1]

    def test_empty_failed(self):
        funnel = utils.recovery_funnel(pd.DataFrame(), pd.DataFrame())
        assert funnel["count"].tolist() == [0, 0, 0, 0]


class TestActivityOverTime:
    def test_aligns_and_sorts_dates(self):
        failed = pd.DataFrame(
            [{"id": 1, "created_at": datetime(2026, 8, 22, 9, 0)},
             {"id": 2, "created_at": datetime(2026, 8, 23, 9, 0)}]
        )
        attempts = pd.DataFrame(
            [{"id": 1, "timestamp": datetime(2026, 8, 23, 10, 0)},
             {"id": 2, "timestamp": datetime(2026, 8, 23, 11, 0)}]
        )
        trend = utils.activity_over_time(failed, attempts)
        assert list(trend["date"]) == [pd.Timestamp("2026-08-22").date(),
                                       pd.Timestamp("2026-08-23").date()]
        assert list(trend["failures"]) == [1, 1]
        assert list(trend["attempts"]) == [0, 2]

    def test_both_empty(self):
        trend = utils.activity_over_time(pd.DataFrame(), pd.DataFrame())
        assert trend.empty

    def test_caps_at_30_active_days(self):
        days = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(40)]
        failed = pd.DataFrame(
            [{"id": i, "created_at": d} for i, d in enumerate(days)]
        )
        trend = utils.activity_over_time(failed, pd.DataFrame())
        assert len(trend) == 30


class TestFormatting:
    def test_format_inr(self):
        assert utils.format_inr(1299.0) == "\u20b91,299"
        assert utils.format_inr(0) == "\u20b90"
        assert utils.format_inr(1234567.89) == "\u20b91,234,568"


# --------------------------------------------------------------------- #
# Full-app smoke test via Streamlit's AppTest                           #
# --------------------------------------------------------------------- #
class TestDashboardAppRender:
    @pytest.fixture()
    def seeded_db(self, tmp_path, monkeypatch):
        """Real SQLite DB with one failed payment + one delivered attempt."""
        db_path = tmp_path / "dash.db"
        url = f"sqlite:///{db_path}"

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import app.database as database
        from app.models import FailedPayment, RecoveryAttempt

        engine = create_engine(url)
        database.Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        payment = FailedPayment(
            payment_id="pay_DASH_1",
            amount=149900,
            currency="INR",
            customer_name="Dash Tester",
            customer_contact="6789477144",
            customer_email="dash@example.com",
            payment_method="upi",
            error_code="GATEWAY_ERROR",
            error_description="Gateway rejected",
            failure_reason="gateway_error",
            recovery_status=FailedPayment.STATUS_IN_PROGRESS,
        )
        session.add(payment)
        session.flush()
        session.add(
            RecoveryAttempt(
                failed_payment_id=payment.id,
                timestamp=datetime.utcnow(),
                channel="telegram",
                action="message_sent",
                message="Hi! Your payment failed...",
                outcome="delivered",
                payment_link_id="plink_DASH_1",
            )
        )
        session.commit()
        session.close()

        monkeypatch.setenv("DATABASE_URL", url)

        # The app caches its loads with st.cache_data; clear any state left
        # over from earlier runs in this process.
        import streamlit as st

        st.cache_data.clear()
        return db_path

    def test_app_renders_without_exception(self, seeded_db):
        from streamlit.testing.v1 import AppTest

        app_path = (
            __import__("pathlib").Path(__file__).resolve().parent.parent
            / "dashboard"
            / "app.py"
        )
        at = AppTest.from_file(str(app_path), default_timeout=60)
        at.run()

        assert not at.exception, at.exception
        titles = [t.value for t in at.title]
        assert any("Revenue Recovery Ops" in t for t in titles)
        # KPI cards are markdown blocks rendered through our helper.
        markdown_blob = "\n".join(m.value for m in at.markdown)
        assert "Failed payments" in markdown_blob
        assert "Recovery rate" in markdown_blob
