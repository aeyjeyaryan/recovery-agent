"""Data-access + aggregation helpers for the Streamlit recovery dashboard."""

from __future__ import annotations

import os

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

DEFAULT_DB_URL = "sqlite:///./recovery_agent.db"

STAGE_FAILED = "Failed"
STAGE_CONTACTED = "Contacted"
STAGE_DELIVERED = "Delivered"
STAGE_RECOVERED = "Recovered"


def get_engine(db_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine for the agent's database."""
    url = db_url or os.environ.get("DATABASE_URL", DEFAULT_DB_URL)
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


# --------------------------------------------------------------------- #
# Raw loads                                                             #
# --------------------------------------------------------------------- #
def load_failed_payments(engine: Engine) -> pd.DataFrame:
    """All failed payments, newest first."""
    return pd.read_sql(
        "SELECT * FROM failed_payments ORDER BY created_at DESC", engine
    )


def load_attempts(engine: Engine) -> pd.DataFrame:
    """All recovery attempts, newest first."""
    return pd.read_sql(
        "SELECT * FROM recovery_attempts ORDER BY timestamp DESC", engine
    )


# --------------------------------------------------------------------- #
# KPIs                                                                  #
# --------------------------------------------------------------------- #
def _recovered_ids(failed: pd.DataFrame, attempts: pd.DataFrame) -> set[int]:
    """Payments counted as recovered: status flag OR a paid attempt."""
    if failed.empty:
        return set()
    ids = set(failed.loc[failed["recovery_status"] == "recovered", "id"])
    if not attempts.empty:
        ids |= set(
            attempts.loc[attempts["outcome"] == "paid", "failed_payment_id"]
            .dropna()
            .astype(int)
        )
    return ids


def compute_metrics(failed: pd.DataFrame, attempts: pd.DataFrame) -> dict[str, float]:
    """Headline count/rate metrics."""
    total_failed = len(failed)
    if not total_failed:
        return {
            "total_failed": 0,
            "recovered": 0,
            "in_progress": 0,
            "unrecoverable": 0,
            "recovery_rate": 0.0,
        }
    statuses = failed["recovery_status"]
    recovered_ids = _recovered_ids(failed, attempts)
    recovered_mask = (statuses == "recovered") | (
        failed["id"].isin(recovered_ids)
    )
    recovered = int(recovered_mask.sum())
    return {
        "total_failed": total_failed,
        "recovered": recovered,
        "in_progress": int((statuses == "in_progress").sum()),
        "unrecoverable": int((statuses == "unrecoverable").sum()),
        "recovery_rate": recovered / total_failed * 100,
    }


def amount_kpis(failed: pd.DataFrame, attempts: pd.DataFrame) -> dict[str, float]:
    """Money-weighted KPIs (all *_inr values are rupees, not paise)."""
    if failed.empty:
        return {
            "failed_amount_inr": 0.0,
            "recoverable_amount_inr": 0.0,
            "recovered_amount_inr": 0.0,
            "at_risk_amount_inr": 0.0,
            "avg_attempt_per_payment": 0.0,
        }
    amounts = failed.set_index("id")["amount"].astype(float)

    recovered_ids = _recovered_ids(failed, attempts)
    unrecoverable_ids = set(
        failed.loc[failed["recovery_status"] == "unrecoverable", "id"]
    )
    pending_ids = set(failed.loc[failed["recovery_status"] == "pending", "id"])

    total_inr = amounts.sum() / 100.0
    recovered_inr = amounts.reindex(list(recovered_ids)).fillna(0).sum() / 100.0
    unrecoverable_inr = (
        amounts.reindex(list(unrecoverable_ids)).fillna(0).sum() / 100.0
    )
    pending_inr = amounts.reindex(list(pending_ids)).fillna(0).sum() / 100.0

    n_payments = max(len(failed), 1)
    n_attempts = 0 if attempts.empty else len(attempts)

    return {
        "failed_amount_inr": total_inr,
        "recoverable_amount_inr": total_inr - unrecoverable_inr,
        "recovered_amount_inr": recovered_inr,
        "at_risk_amount_inr": pending_inr,
        "avg_attempt_per_payment": n_attempts / n_payments,
    }


# --------------------------------------------------------------------- #
# Breakdowns                                                            #
# --------------------------------------------------------------------- #
def channel_breakdown(attempts: pd.DataFrame) -> pd.DataFrame:
    """Attempt counts grouped by outbound channel."""
    if attempts.empty:
        return pd.DataFrame(columns=["channel", "attempts"])
    counts = (
        attempts.groupby("channel")
        .size()
        .reset_index(name="attempts")
        .sort_values("attempts", ascending=False)
    )
    return counts


def failure_reason_breakdown(failed: pd.DataFrame) -> pd.DataFrame:
    """Failure reason frequencies with rupee exposure per reason."""
    if failed.empty:
        return pd.DataFrame(columns=["failure_reason", "count", "amount_inr"])
    frame = failed.copy()
    frame["failure_reason"] = (
        frame["failure_reason"].fillna("unclassified").replace("", "unclassified")
    )
    grouped = (
        frame.groupby("failure_reason")
        .agg(count=("id", "size"), amount=("amount", "sum"))
        .reset_index()
        .sort_values("count", ascending=False)
    )
    grouped["amount_inr"] = grouped["amount"].fillna(0) / 100.0
    return grouped[["failure_reason", "count", "amount_inr"]]


def outcomes_breakdown(attempts: pd.DataFrame) -> pd.DataFrame:
    """Outcome frequencies across all attempts."""
    if attempts.empty:
        return pd.DataFrame(columns=["outcome", "count"])
    counts = attempts["outcome"].value_counts().reset_index()
    counts.columns = ["outcome", "count"]
    return counts


# --------------------------------------------------------------------- #
# Trends & funnel                                                       #
# --------------------------------------------------------------------- #
def _daily(frame: pd.DataFrame, ts_col: str, name: str) -> pd.DataFrame:
    if frame.empty or ts_col not in frame.columns:
        return pd.DataFrame(columns=["date", name])
    series = (
        pd.to_datetime(frame[ts_col], errors="coerce")
        .dt.date.value_counts()
        .sort_index()
        .rename(name)
    )
    out = series.reset_index()
    out.columns = ["date", name]
    return out


def activity_over_time(
    failed: pd.DataFrame, attempts: pd.DataFrame
) -> pd.DataFrame:
    """Daily failures vs outreach attempts, aligned on one date axis."""
    f = _daily(failed, "created_at", "failures")
    a = _daily(attempts, "timestamp", "attempts")
    if f.empty and a.empty:
        return pd.DataFrame(columns=["date", "failures", "attempts"])
    merged = pd.merge(f, a, on="date", how="outer").fillna(0)
    merged = merged.sort_values("date").tail(30)  # last 30 active days
    return merged


def recovery_funnel(failed: pd.DataFrame, attempts: pd.DataFrame) -> pd.DataFrame:
    """Stage-by-stage conversion: failed -> contacted -> delivered -> recovered."""
    total = len(failed)
    if total == 0 or attempts.empty:
        contacted = 0
        delivered = 0
    else:
        contacted = attempts["failed_payment_id"].nunique()
        delivered = attempts.loc[
            attempts["outcome"] == "delivered", "failed_payment_id"
        ].nunique()
    recovered = len(_recovered_ids(failed, attempts))
    return pd.DataFrame(
        {
            "stage": [STAGE_FAILED, STAGE_CONTACTED, STAGE_DELIVERED, STAGE_RECOVERED],
            "count": [total, contacted, delivered, recovered],
        }
    )


# --------------------------------------------------------------------- #
# Formatting                                                            #
# --------------------------------------------------------------------- #
def amount_inr(paise: pd.Series) -> pd.Series:
    """Convert a paise column to INR rupees."""
    return paise / 100.0


def format_inr(value: float) -> str:
    """Human-friendly rupee formatting: 1299.0 -> '₹1,299'."""
    return f"\u20b9{value:,.0f}"
