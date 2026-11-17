"""Report generation: summary table and full reconciliation CSV report."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

FINDING_COLUMNS = [
    "finding_id", "category", "reference_id", "transaction_id", "dataset", "expected_amount",
    "actual_amount", "difference_amount", "period_impact", "counts_in_bridge",
    "transaction_date", "settlement_date", "explanation", "suggested_action",
]


def _v(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.4f}" if value != value.quantize(Decimal("0.01")) else f"{value:.2f}"
    return str(value)


def summary_table(result) -> list[tuple[str, str]]:
    s = result.summary
    cur = s["currency"]
    return [
        ("Reconciliation period", f"{s['period_start']} to {s['period_end']} (cutoff inclusive)"),
        ("Currency", cur),
        ("Ledger transactions (raw rows)", _v(s["ledger_rows_raw"])),
        ("Ledger transactions in period (unique)", _v(s["ledger_unique_in_period"])),
        ("Bank entries (raw rows)", _v(s["bank_rows_raw"])),
        ("Bank entries settled in period", _v(s["bank_entries_in_period"])),
        ("Gross payment value", _v(s["gross_payment_value"])),
        ("Refund value", _v(s["refund_value"])),
        ("Processing fees", _v(s["processing_fees"])),
        ("Net expected settlement (E)", _v(s["net_expected_settlement"])),
        ("Actual bank payment receipts", _v(s["actual_payment_receipts"])),
        ("Actual bank refund debits", _v(s["actual_refund_debits"])),
        ("Actual bank settlement (B)", _v(s["actual_bank_settlement"])),
        ("Reconciliation difference D = E - B", _v(s["reconciliation_difference"])),
        ("Matched transactions", _v(s["matched_count"])),
        ("Matched amount", _v(s["matched_amount"])),
        ("Unmatched ledger amount (E - matched)", _v(s["unmatched_ledger_amount"])),
        ("Unmatched bank amount (duplicates + bank-only)", _v(s["unmatched_bank_amount"])),
        ("Duplicate impact (bank)", _v(s["duplicate_impact"])),
        ("Timing difference", _v(s["timing_difference_amount"])),
        ("Rounding difference (aggregate)", _v(s["rounding_difference_amount"])),
        ("Orphan refund amount", _v(s["orphan_refund_amount"])),
        ("Explained difference", _v(s["explained_difference"])),
        ("Unexplained difference", _v(s["unexplained_difference"])),
        ("Status", s["reconciliation_status"]),
    ]


def findings_rows(result) -> list[dict]:
    return [{c: f.get(c) for c in FINDING_COLUMNS} for f in result.findings]


def build_report_csv(result) -> str:
    s = result.summary
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["PAYMENT RECONCILIATION REPORT"])
    w.writerow(["Reconciliation period", f"{s['period_start']} to {s['period_end']}"])
    w.writerow(["Formula", "D = E - B = timing + missing_in_bank + amount_mismatch + rounding - bank_duplicates - bank_only + unexplained"])
    w.writerow(["Tolerance per transaction", _v(s["tolerance"])])
    w.writerow([])
    w.writerow(["SUMMARY"])
    w.writerow(["metric", "value"])
    for k, v in summary_table(result):
        w.writerow([k, v])
    w.writerow([])
    w.writerow(["RECONCILIATION BRIDGE (effect on D = expected - actual)"])
    w.writerow(["component", "category", "amount"])
    for b in result.bridge:
        w.writerow([b["component"], b["category"], _v(b["amount"])])
    w.writerow([])
    w.writerow(["DISCREPANCY DETAILS"])
    w.writerow(FINDING_COLUMNS)
    for f in findings_rows(result):
        w.writerow([_v(f[c]) for c in FINDING_COLUMNS])
    w.writerow([])
    w.writerow(["DATA VALIDATION ISSUES"])
    w.writerow(["dataset", "row_number", "record_id", "field", "severity", "issue", "action"])
    for i in result.validation_issues:
        w.writerow([i[k] for k in ("dataset", "row_number", "record_id", "field", "severity", "issue", "action")])
    return buf.getvalue()
