"""Streamlit operations dashboard for the Razorpay Recovery Agent.

Run from the project root:

    streamlit run dashboard/app.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st

from dashboard import utils

st.set_page_config(
    page_title="Razorpay Recovery Agent",
    page_icon="\U0001f4b8",
    layout="wide",
)

# --------------------------------------------------------------------- #
# Styling                                                               #
# --------------------------------------------------------------------- #
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.4rem; padding-bottom: 2rem; }
      div[data-testid="stMetric"] {
          background: #f7f9fc; border: 1px solid #e6eaf0;
          border-radius: 12px; padding: 14px 18px 10px 18px;
      }
      div[data-testid="stMetricLabel"] p { font-size: 0.85rem; color: #555; }
      .kpi-card {
          background: #f7f9fc; border: 1px solid #e6eaf0; border-left: 5px solid #2e7df6;
          border-radius: 12px; padding: 14px 18px; text-align: center;
      }
      .kpi-card.green { border-left-color: #21a366; }
      .kpi-card.amber { border-left-color: #f5a623; }
      .kpi-card.red   { border-left-color: #e05252; }
      .kpi-card.purple{ border-left-color: #8250df; }
      .kpi-card .kpi-value { font-size: 1.65rem; font-weight: 700; line-height: 1.15; }
      .kpi-card .kpi-sub   { font-size: 0.85rem; color: #667; margin-top: 2px; }
    </style>
    """,
    unsafe_allow_html=True,
)

STATUS_BADGE = {
    "pending": "\u26aa pending",
    "in_progress": "\U0001f501 in_progress",
    "recovered": "\u2705 recovered",
    "unrecoverable": "\u26d4 unrecoverable",
}
OUTCOME_BADGE = {
    "delivered": "\u2705 delivered",
    "failed": "\u274c failed",
    "paid": "\U0001f4b0 paid",
    "clicked": "\U0001f446 clicked",
    "escalated": "\U0001f6a8 escalated",
}


def kpi_card(label: str, value: str, sub: str, accent: str = "") -> None:
    st.markdown(
        f"""
        <div class="kpi-card {accent}">
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{label}</div>
          <div class="kpi-sub"><b>{sub}</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------- #
# Data                                                                  #
# --------------------------------------------------------------------- #
@st.cache_data(ttl=10)
def load_data():
    engine = utils.get_engine()
    return utils.load_failed_payments(engine), utils.load_attempts(engine)


failed_all, attempts_all = load_data()

# --------------------------------------------------------------------- #
# Sidebar filters                                                       #
# --------------------------------------------------------------------- #
with st.sidebar:
    st.header("\U0001f527 Filters")
    statuses = sorted(failed_all["recovery_status"].dropna().unique()) if not failed_all.empty else []
    reasons = sorted(
        failed_all["failure_reason"].fillna("unclassified").unique()
    ) if not failed_all.empty else []
    channels = sorted(attempts_all["channel"].dropna().unique()) if not attempts_all.empty else []

    sel_status = st.multiselect("Recovery status", statuses, default=statuses)
    sel_reason = st.multiselect("Failure reason", reasons, default=reasons)
    sel_channel = st.multiselect("Attempt channel", channels, default=channels)

    st.divider()
    if st.button("\U0001f504 Refresh now", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    auto = st.checkbox("Auto-refresh", value=False)
    interval = st.slider("Interval (s)", 10, 120, 30, disabled=not auto)

    st.divider()
    st.caption(
        f"\U0001f4c2 DB: `recovery_agent.db`  \n\U0001f550 All times UTC  \n"
        f"Trigger test failures:\n"
        f"`python scripts/simulate_failure.py`"
    )

failed = failed_all.copy() if not failed_all.empty else failed_all
if not failed.empty:
    failed = failed[
        failed["recovery_status"].isin(sel_status)
        & failed["failure_reason"].fillna("unclassified").isin(sel_reason)
    ]
attempts = attempts_all.copy() if not attempts_all.empty else attempts_all
if not attempts.empty and sel_channel:
    attempts = attempts[attempts["channel"].isin(sel_channel)]
# Keep only attempts belonging to the filtered payments (when filtering by status/reason).
if not attempts.empty and not failed.empty and len(failed) != len(failed_all):
    attempts = attempts[attempts["failed_payment_id"].isin(failed["id"])]

metrics = utils.compute_metrics(failed, attempts)
money = utils.amount_kpis(failed, attempts)

# --------------------------------------------------------------------- #
# Header                                                                #
# --------------------------------------------------------------------- #
st.title("\U0001f4b8 Revenue Recovery Ops")
st.caption(
    "Autonomous agent recovering failed Razorpay payments via Telegram \u2014 "
    "live view of the agent's audit trail."
)
st.divider()

# --------------------------------------------------------------------- #
# KPI row                                                               #
# --------------------------------------------------------------------- #
rate = metrics["recovery_rate"]
c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    kpi_card(
        "Failed payments", f"{metrics['total_failed']}",
        utils.format_inr(money["failed_amount_inr"]),
    )
with c2:
    kpi_card(
        "Recovered", f"{metrics['recovered']}",
        utils.format_inr(money["recovered_amount_inr"]),
        accent="green",
    )
with c3:
    kpi_card(
        "Recovery rate", f"{rate:.1f}%",
        f"{utils.format_inr(money['recoverable_amount_inr'])} recoverable",
        accent="green",
    )
with c4:
    kpi_card(
        "In progress", f"{metrics['in_progress']}",
        f"{utils.format_inr(money['at_risk_amount_inr'])} awaiting payment",
        accent="amber",
    )
with c5:
    kpi_card(
        "Unrecoverable", f"{metrics['unrecoverable']}",
        f"{money['avg_attempt_per_payment']:.1f} attempts/payment avg",
        accent="red" if metrics["unrecoverable"] else "purple",
    )

if failed_all.empty:
    st.info(
        "No failed payments captured yet. Start the API (`python main.py`), "
        "then trigger one with `python scripts/simulate_failure.py`."
    )
    st.stop()

empty_filtered = failed.empty

# --------------------------------------------------------------------- #
# Charts row 1: activity trend + funnel                                 #
# --------------------------------------------------------------------- #
left, right = st.columns([3, 2])

with left:
    st.subheader("\U0001f4c9 Failures vs \U0001f4e8 Outreach (daily)")
    trend = utils.activity_over_time(failed, attempts)
    if trend.empty or (trend["failures"].sum() == 0 and trend["attempts"].sum() == 0):
        st.info("No dated activity yet.")
    else:
        melted = trend.melt("date", var_name="series", value_name="count")
        chart = (
            alt.Chart(melted)
            .mark_line(point=True)
            .encode(
                x="date:O",
                y=alt.Y("count:Q", title=None),
                color=alt.Color(
                    "series:N",
                    scale=alt.Scale(
                        domain=["failures", "attempts"],
                        range=["#e05252", "#2e7df6"],
                    ),
                    title=None,
                ),
            )
            .properties(height=260)
        )
        st.altair_chart(chart, width="stretch")

with right:
    st.subheader("\U0001faa9 Recovery funnel")
    funnel = utils.recovery_funnel(failed, attempts)
    funnel["label"] = funnel.apply(
        lambda r: f"{r['stage']} ({r['count']})", axis=1
    )
    bar = (
        alt.Chart(funnel)
        .mark_bar(cornerRadius=4)
        .encode(
            x=alt.X("count:Q", title=None),
            y=alt.Y(
                "stage:N",
                sort=["Failed", "Contacted", "Delivered", "Recovered"],
                title=None,
            ),
            color=alt.Color(
                "stage:N",
                legend=None,
                scale=alt.Scale(range=["#e05252", "#f5a623", "#2e7df6", "#21a366"]),
            ),
        )
        .properties(height=260)
    )
    text = alt.Chart(funnel).mark_text(align="left", dx=4).encode(
        x="count:Q", y=alt.Y("stage:N", sort=["Failed", "Contacted", "Delivered", "Recovered"]),
        text="label:N",
    )
    st.altair_chart(bar + text, width="stretch")

# --------------------------------------------------------------------- #
# Charts row 2: reasons / money + channel / outcomes                    #
# --------------------------------------------------------------------- #
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("\U0001f50d Top failure reasons")
    reasons_df = utils.failure_reason_breakdown(failed)
    if empty_filtered or reasons_df.empty:
        st.info("Nothing to show for the current filter.")
    else:
        rbar = (
            alt.Chart(reasons_df)
            .mark_bar(cornerRadius=4, color="#8250df")
            .encode(
                x=alt.X("count:Q", title=None),
                y=alt.Y(
                    "failure_reason:N",
                    sort="-x",
                    title=None,
                ),
            )
            .properties(height=max(180, 40 * len(reasons_df)))
        )
        rtext = (
            alt.Chart(reasons_df)
            .mark_text(align="left", dx=4)
            .encode(x="count:Q", y=alt.Y("failure_reason:N", sort="-x"),
                    text="count:Q")
        )
        st.altair_chart(rbar + rtext, width="stretch")
        reasons_show = reasons_df.copy()
        reasons_show["amount_inr"] = reasons_show["amount_inr"].map(utils.format_inr)
        reasons_show.columns = ["Reason", "Count", "Exposure"]
        st.dataframe(reasons_show, width="stretch", hide_index=True)

with col_b:
    st.subheader("\U0001f4e3 Outreach by channel")
    ch = utils.channel_breakdown(attempts)
    if ch.empty:
        st.info("No attempts yet.")
    else:
        st.altair_chart(
            alt.Chart(ch).mark_bar(cornerRadius=4, color="#2e7df6").encode(
                x=alt.X("attempts:Q", title=None),
                y=alt.Y("channel:N", sort="-x", title=None),
            ).properties(height=140),
            width="stretch",
        )
        oc = utils.outcomes_breakdown(attempts)
        donut = (
            alt.Chart(oc)
            .mark_arc(innerRadius=45)
            .encode(theta="count:Q", color=alt.Color("outcome:N", title=None))
            .properties(height=200)
        )
        st.altair_chart(donut, width="stretch")

st.divider()

# --------------------------------------------------------------------- #
# Failed payments table                                                 #
# --------------------------------------------------------------------- #
st.subheader("\U0001f4cb Failed payments")
display = failed.copy()
display.insert(
    1,
    "status",
    display["recovery_status"].map(lambda s: STATUS_BADGE.get(s, s)),
)
display["amount (\u20b9)"] = utils.amount_inr(display["amount"])
cols = [
    ("created_at", st.column_config.DatetimeColumn("Created (UTC)", format="DD MMM HH:mm")),
    ("payment_id", "Payment ID"),
    ("customer_name", "Customer"),
    ("customer_contact", "Contact"),
    ("payment_method", "Method"),
    ("amount (\u20b9)", st.column_config.NumberColumn(format="\u20b9 %.0f")),
    ("error_code", "Error code"),
    ("error_description", st.column_config.TextColumn(width="medium")),
    ("failure_reason", st.column_config.TextColumn("Diagnosis")),
    ("status", st.column_config.TextColumn("Status")),
]
ordered = [c for c, _ in cols if c in display.columns]
config = {c: cfg for c, cfg in cols if c in display.columns}
st.dataframe(display[ordered], column_config=config, width="stretch", height=280)

# --------------------------------------------------------------------- #
# Recent attempts with drill-down                                       #
# --------------------------------------------------------------------- #
st.subheader("\U0001f4ac Recent outreach attempts")
if attempts.empty:
    st.info("No outreach performed yet.")
else:
    for _, row in attempts.head(20).iterrows():
        badge = OUTCOME_BADGE.get(row["outcome"], row["outcome"])
        when = pd.to_datetime(row["timestamp"]).strftime("%d %b %H:%M") if row["timestamp"] else "?"
        header = f"{badge}  \u00b7  {row['channel']}  \u00b7  {row['action']}  \u00b7  {when}"
        with st.expander(header):
            m1, m2, m3 = st.columns(3)
            m1.metric("Link ID", row["payment_link_id"] or "\u2014")
            m2.metric("Attempt #", row["id"])
            rec = row["recovery_amount"]
            m3.metric("Recovered", utils.format_inr(rec / 100) if rec else "\u2014")
            st.text_area("Message sent", str(row["message"] or ""), height=120, disabled=True)

st.divider()
st.caption(
    "Read-only view over the agent's SQLite audit trail \u00b7 refreshes every "
    f"{interval}s when auto-refresh is on."
)

if auto:
    time.sleep(interval)
    st.rerun()
