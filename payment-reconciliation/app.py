"""Streamlit dashboard. All numbers come from reconciliation_engine; nothing is hardcoded."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config as C
from config import ReconConfig
from data_generator import ensure_datasets, read_csv_rows
from reconciliation_engine import reconcile
from report_generator import FINDING_COLUMNS, build_report_csv, findings_rows, summary_table

st.set_page_config(page_title="Payment Reconciliation", page_icon="🧾", layout="wide")
st.markdown("""<style>
div[data-testid="stMetric"]{background:#f6f8fb;border:1px solid #e3e8ef;border-radius:10px;padding:12px 16px}
@media (prefers-color-scheme: dark){div[data-testid="stMetric"]{background:#1b2230;border-color:#2c3648}}
</style>""", unsafe_allow_html=True)

CATEGORY_COLORS = {
    "MATCHED": "#2e9e6b", "TIMING_DIFFERENCE": "#3b82c4", "ROUNDING_DIFFERENCE": "#8b6fd1",
    "DUPLICATE_ENTRY": "#e0902b", "ORPHAN_REFUND": "#d64545", "MISSING_IN_BANK": "#b5446e",
    "MISSING_IN_LEDGER": "#7a5c3e", "AMOUNT_MISMATCH": "#c9a227", "OUT_OF_PERIOD": "#9aa4b2",
}


def money(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def to_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in df.columns:  # display only: Decimal -> float (all maths stays in Decimal)
        if df[col].map(lambda v: isinstance(v, Decimal)).any():
            df[col] = df[col].map(lambda v: float(v) if isinstance(v, Decimal) else v)
    return df


@st.cache_data(show_spinner=False)
def load_raw():
    ensure_datasets()
    return read_csv_rows(C.LEDGER_CSV), read_csv_rows(C.BANK_CSV)


@st.cache_data(show_spinner="Reconciling…")
def run_reconciliation(tolerance: str):
    ledger, bank = load_raw()
    return reconcile(ledger, bank, ReconConfig(tolerance=Decimal(tolerance)))


# ------------------------------------------------------------------ sidebar
ledger_raw, bank_raw = load_raw()
with st.sidebar:
    st.title("🧾 Reconciliation")
    tol = st.number_input("Per-transaction tolerance", min_value=0.0, max_value=5.0, value=0.01, step=0.01, format="%.2f",
                          help="Amount differences up to this value count as a match.")
result = run_reconciliation(f"{tol:.2f}")
S = result.summary
with st.sidebar:
    st.caption(f"Period: **{S['period_start']} → {S['period_end']}**  \nCurrency: **{S['currency']}**")
    st.caption("Difference = expected − actual. Positive: bank received less than the ledger expects.")

st.title("Payment Reconciliation & Discrepancy Detection")
st.caption(f"Reconciliation period: {result.config.period_label} (cutoff {S['period_end']}). "
           "Internal ledger vs bank settlement records.")

tab_over, tab_txn, tab_disc, tab_data, tab_rep = st.tabs(
    ["Overview", "Transaction Explorer", "Discrepancy Analysis", "Data Explorer", "Reports"])

ledger_df = to_df(result.ledger_status)
bank_df = to_df(result.bank_status)

# ------------------------------------------------------------------ overview
with tab_over:
    ok = S["reconciliation_status"] == "FULLY EXPLAINED"
    (st.success if ok else st.error)(
        f"**{S['reconciliation_status']}** — difference of {money(S['reconciliation_difference'])} {S['currency']}; "
        f"unexplained remainder {money(S['unexplained_difference'])}.")
    c = st.columns(4)
    c[0].metric("Expected net settlement (ledger)", money(S["net_expected_settlement"]))
    c[1].metric("Actual bank settlement", money(S["actual_bank_settlement"]))
    c[2].metric("Difference (expected − actual)", money(S["reconciliation_difference"]))
    c[3].metric("Unexplained difference", money(S["unexplained_difference"]))
    c = st.columns(4)
    c[0].metric("Matched transactions", f"{S['matched_count']} / {S['ledger_unique_in_period']}")
    c[1].metric("Discrepant records", S["discrepancy_count"])
    c[2].metric("Ledger / bank rows", f"{S['ledger_rows_raw']} / {S['bank_rows_raw']}")
    c[3].metric("Findings", S["finding_count"])

    st.subheader("Reconciliation bridge")
    st.markdown("`D = E − B = timing + missing in bank + amount mismatch + rounding − bank duplicates − bank-only entries + unexplained`")
    bridge_df = to_df(result.bridge)
    left, right = st.columns([3, 2])
    with left:
        fig = px.bar(bridge_df, x="amount", y="component", orientation="h", text="amount",
                     title="What explains the difference?")
        fig.update_traces(texttemplate="%{text:,.2f}", marker_color="#3b82c4")
        fig.update_layout(yaxis={"autorange": "reversed", "title": ""}, xaxis_title=f"Effect on difference ({S['currency']})",
                          height=380, margin=dict(l=0, r=10, t=40, b=0))
        st.plotly_chart(fig)
    with right:
        st.dataframe(bridge_df.rename(columns={"component": "Component", "amount": "Amount"})[["Component", "Amount"]],
                     hide_index=True)
        st.caption(f"E = {money(S['net_expected_settlement'])}, B = {money(S['actual_bank_settlement'])}, "
                   f"D = {money(S['reconciliation_difference'])}")

    a, b = st.columns(2)
    with a:
        counts = ledger_df[ledger_df["in_period"]]["status"].value_counts().reset_index()
        counts.columns = ["status", "records"]
        fig = px.bar(counts, x="status", y="records", color="status", color_discrete_map=CATEGORY_COLORS,
                     title="Ledger records by reconciliation status (in period)", log_y=True)
        fig.update_layout(showlegend=False, height=340, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig)
    with b:
        led = ledger_df[ledger_df["in_period"]].groupby("transaction_date")["expected_amount"].sum().rename("Ledger expected (by transaction date)")
        bk = bank_df[(bank_df["settlement_date"] >= S["period_start"]) & (bank_df["settlement_date"] <= S["period_end"])] \
            .groupby("settlement_date")["settlement_amount"].sum().rename("Bank settled (by settlement date)")
        daily = pd.concat([led, bk], axis=1).fillna(0).reset_index(names="date")
        fig = go.Figure()
        for col in daily.columns[1:]:
            fig.add_trace(go.Scatter(x=daily["date"], y=daily[col], name=col, mode="lines"))
        fig.update_layout(title="Daily flow: ledger vs bank", height=340, margin=dict(l=0, r=0, t=40, b=0),
                          legend=dict(orientation="h", y=-0.2))
        st.plotly_chart(fig)

    st.subheader("Amounts: gross, refunds, fees, net")
    st.dataframe(pd.DataFrame(summary_table(result)[6:13], columns=["Item", "Amount"]), hide_index=True)

# ------------------------------------------------------------------ transaction explorer
with tab_txn:
    st.subheader("Transaction Explorer")
    f1, f2, f3 = st.columns([2, 2, 2])
    query = f1.text_input("Search transaction / refund / bank entry ID").strip().upper()
    dmin, dmax = ledger_df["transaction_date"].min(), ledger_df["transaction_date"].max()
    rng = f2.date_input("Transaction date range", (dmin, dmax), min_value=dmin, max_value=dmax)
    statuses = f3.multiselect("Status", sorted(ledger_df["status"].unique()), default=[])
    view = ledger_df.copy()
    if query:
        bank_hits = bank_df[bank_df["bank_entry_id"].str.contains(query) | bank_df["transaction_id"].str.contains(query)]["transaction_id"]
        view = view[view["transaction_id"].str.contains(query) | view["transaction_id"].isin(bank_hits)]
    if isinstance(rng, tuple) and len(rng) == 2:
        view = view[(view["transaction_date"] >= rng[0]) & (view["transaction_date"] <= rng[1])]
    if statuses:
        view = view[view["status"].isin(statuses)]
    st.caption(f"{len(view)} ledger records")
    st.dataframe(view.drop(columns=["in_period"]), hide_index=True, height=300)
    if len(view):
        pick = st.selectbox("Inspect transaction", view["transaction_id"].unique())
        l_rows = ledger_df[ledger_df["transaction_id"] == pick]
        b_rows = bank_df[bank_df["transaction_id"] == pick]
        x, y = st.columns(2)
        x.markdown("**Ledger record(s)**"); x.dataframe(l_rows, hide_index=True)
        y.markdown("**Bank record(s)**")
        if b_rows.empty:
            y.warning("No bank entry for this transaction.")
        else:
            y.dataframe(b_rows, hide_index=True)
        for f in [f for f in result.findings if pick in (f["transaction_id"], f["reference_id"])]:
            st.info(f"**{f['category']}** — {f['explanation']}\n\n*Suggested action:* {f['suggested_action']}")

# ------------------------------------------------------------------ discrepancy analysis
def show_category(title, category, impact_label, impact_value, intro, extra_filter=None):
    items = [f for f in result.findings if f["category"] == category and (extra_filter is None or extra_filter(f))]
    with st.expander(f"{title}  ·  {len(items)} finding(s)", expanded=True):
        st.metric(impact_label, money(impact_value))
        st.markdown(intro)
        if not items:
            st.success("None detected with the current data and tolerance.")
            return
        st.dataframe(to_df([{k: f.get(k) for k in FINDING_COLUMNS if k not in ("explanation", "suggested_action")}
                            for f in items]), hide_index=True)
        for f in items[:6]:
            st.markdown(f"**{f['reference_id']}** — {f['explanation']}")
        if len(items) > 6:
            st.caption(f"…{len(items) - 6} more findings in the table above / CSV report.")
        st.markdown(f"**Suggested action:** {items[0]['suggested_action']}")


with tab_disc:
    st.subheader("Mandatory discrepancy types")
    show_category("A. Next-month settlement (timing difference)", C.TIMING_DIFFERENCE,
                  "Timing difference (in expected, not yet in bank)", S["timing_difference_amount"],
                  "Transactions recorded in the period but settled by the bank after the cutoff. They are in-transit, **not missing**.")
    show_category("B. Aggregate rounding difference", C.ROUNDING_DIFFERENCE, "Aggregate rounding difference",
                  S["rounding_difference_amount"],
                  "The platform rounds each fee to cents before summing; the bank sums unrounded amounts and rounds once. "
                  "Each transaction is within tolerance; the gap appears only in the aggregate. The aggregate finding is the one "
                  "counted in the bridge; per-transaction rows are informational.",
                  extra_filter=lambda f: str(f["reference_id"]).startswith("AGG-"))
    rounding_rows = [f for f in result.findings if f["category"] == C.ROUNDING_DIFFERENCE and not str(f["reference_id"]).startswith("AGG-")]
    if rounding_rows:
        with st.expander(f"Contributing transactions ({len(rounding_rows)})"):
            st.dataframe(to_df([{k: f[k] for k in ("transaction_id", "expected_amount", "actual_amount", "difference_amount")} for f in rounding_rows]), hide_index=True)
    show_category("C. Duplicate entries", C.DUPLICATE_ENTRY, "Duplicate impact on bank total", S["duplicate_impact"],
                  "Repeated transaction identifiers. Only the first record is matched; duplicates are excluded from matching "
                  "so they cannot inflate the reconciled total, but the extra cash is reported as a bridge item.")
    if result.duplicates:
        st.dataframe(to_df(result.duplicates), hide_index=True)
    show_category("D. Orphan refunds", C.ORPHAN_REFUND, "Orphan refund amount", S["orphan_refund_amount"],
                  "Refunds referencing an original payment that is not in the internal ledger. Never treated as validly matched, "
                  "even when the bank amount agrees.")
    st.subheader("Other findings")
    show_category("Missing in bank", C.MISSING_IN_BANK, "Impact", S["missing_in_bank_amount"], "Ledger transactions with no bank entry and no later settlement.")
    show_category("Amount mismatch", C.AMOUNT_MISMATCH, "Impact", S["amount_mismatch_amount"], "Settled amounts differing beyond tolerance.")
    show_category("Missing in ledger", C.MISSING_IN_LEDGER, "Impact", S["missing_in_ledger_amount"], "Bank entries with no ledger record.")

# ------------------------------------------------------------------ data explorer
with tab_data:
    st.subheader("Internal ledger")
    st.dataframe(pd.DataFrame(ledger_raw), hide_index=True, height=300)
    st.download_button("Download ledger CSV", C.LEDGER_CSV.read_bytes(), "internal_ledger.csv", "text/csv")
    st.subheader("Bank settlement records")
    st.dataframe(pd.DataFrame(bank_raw), hide_index=True, height=300)
    st.download_button("Download bank CSV", C.BANK_CSV.read_bytes(), "bank_settlements.csv", "text/csv")
    if result.validation_issues:
        st.subheader("Validation issues")
        st.dataframe(pd.DataFrame(result.validation_issues), hide_index=True)
    else:
        st.caption("No validation issues: all rows have valid required fields.")

# ------------------------------------------------------------------ reports
with tab_rep:
    st.subheader(f"Reconciliation report — {S['period_start']} to {S['period_end']}")
    st.dataframe(pd.DataFrame(summary_table(result), columns=["Metric", "Value"]), hide_index=True)
    st.download_button("Download full reconciliation report (CSV)", build_report_csv(result),
                       f"reconciliation_report_{S['period_end']:%Y_%m}.csv", "text/csv", type="primary")
    st.download_button("Download discrepancy details only (CSV)", to_df(findings_rows(result)).to_csv(index=False),
                       f"discrepancies_{S['period_end']:%Y_%m}.csv", "text/csv")
    st.subheader("Accounting assumptions")
    st.markdown(f"""
- Period **{result.config.period_label}**; transactions are in scope by *transaction date*, bank cash by *settlement date* (cutoff {S['period_end']}).
- Net expected settlement = gross payments − refunds − processing fees. Refund fees are non-refundable (refund fee = 0).
- Fee = {C.FEE_RATE:.0%} × gross + {C.FIXED_FEE}; ledger rounds per transaction (half-up), bank rounds the aggregate.
- Tolerance per transaction: {S['tolerance']}. Duplicates: same transaction ID (ledger) or same transaction ID + settlement type (bank); first record kept.
- See the README for limitations of what this system cannot detect in production.""")
