"""Reconciliation engine: validate -> dedupe -> match -> aggregate -> bridge.

Reconciliation formula (all Decimal):
    D = E - B
    E = net expected settlement of in-period ledger transactions (deduplicated)
    B = all bank entries with settlement_date inside the period (duplicates included,
        because cash really was posted), rounded once at the aggregate level
    D = timing + missing_in_bank + amount_mismatch + rounding
        - bank_duplicates - bank_only_entries + unexplained
Each term is computed from findings flagged `counts_in_bridge`, so no record is counted twice.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import config as C
import discrepancy_detector as dd
from config import ReconConfig, DEFAULT_CONFIG

ZERO = Decimal("0")


# ------------------------------------------------------------- parsing/validation
def parse_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    else:
        if value is None or str(value).strip() == "":
            raise ValueError("missing value")
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError(f"not a number: {value!r}")
    if not d.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return d


def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or str(value).strip() == "":
        raise ValueError("missing value")
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"not an ISO date: {value!r}")


def _validate(rows, dataset, id_field, spec, cfg):
    """spec: {field: parser}. Returns (valid_records, issues)."""
    valid, issues = [], []
    for n, raw in enumerate(rows, start=1):
        rec, errors = {"_row": n}, []
        rid = str(raw.get(id_field) or "").strip()
        label = rid or f"row {n}"
        for fname, parser in spec.items():
            try:
                rec[fname] = parser(raw.get(fname), raw)
            except ValueError as exc:
                errors.append((fname, str(exc)))
        for fname, msg in errors:
            issues.append({"dataset": dataset, "row_number": n, "record_id": label,
                           "field": fname, "severity": "ERROR", "issue": msg, "action": "row excluded"})
        if not errors:
            valid.append(rec)
    return valid, issues


def _req_text(v, raw):
    if v is None or str(v).strip() == "":
        raise ValueError("missing value")
    return str(v).strip()


def _choice(options):
    def parse(v, raw):
        s = _req_text(v, raw).upper()
        if s not in options:
            raise ValueError(f"invalid value {s!r}; expected one of {sorted(options)}")
        return s
    return parse


def _currency(cfg):
    def parse(v, raw):
        s = _req_text(v, raw).upper()
        if s != cfg.currency:
            raise ValueError(f"currency {s} differs from reconciliation currency {cfg.currency}")
        return s
    return parse


def _original_id(v, raw):
    if str(raw.get("transaction_type") or "").strip().upper() == C.REFUND:
        return _req_text(v, raw)  # refunds MUST reference an original transaction
    return str(v or "").strip()


def validate_ledger(rows, cfg: ReconConfig = DEFAULT_CONFIG):
    spec = {
        "transaction_id": _req_text,
        "transaction_date": lambda v, r: parse_date(v),
        "transaction_type": _choice(C.TRANSACTION_TYPES),
        "original_transaction_id": _original_id,
        "gross_amount": lambda v, r: parse_decimal(v),
        "processing_fee": lambda v, r: parse_decimal(v),
        "expected_settlement_amount": lambda v, r: parse_decimal(v),
        "currency": _currency(cfg),
        "transaction_status": _choice(C.TRANSACTION_STATUSES),
    }
    valid, issues = _validate(rows, "LEDGER", "transaction_id", spec, cfg)
    for rec in valid:  # arithmetic sanity check (warning only)
        sign = 1 if rec["transaction_type"] == C.PAYMENT else -1
        calc = sign * rec["gross_amount"] - rec["processing_fee"]
        if calc != rec["expected_settlement_amount"]:
            issues.append({"dataset": "LEDGER", "row_number": rec["_row"], "record_id": rec["transaction_id"],
                           "field": "expected_settlement_amount", "severity": "WARNING",
                           "issue": f"expected {rec['expected_settlement_amount']} != gross/fee arithmetic {calc}",
                           "action": "kept; review ledger arithmetic"})
    return valid, issues


def validate_bank(rows, cfg: ReconConfig = DEFAULT_CONFIG):
    spec = {
        "bank_entry_id": _req_text, "transaction_id": _req_text,
        "settlement_date": lambda v, r: parse_date(v),
        "settlement_amount": lambda v, r: parse_decimal(v),
        "settlement_type": _choice(C.SETTLEMENT_TYPES),
        "batch_id": _req_text, "currency": _currency(cfg), "bank_reference": _req_text,
    }
    return _validate(rows, "BANK", "bank_entry_id", spec, cfg)


# ------------------------------------------------------------- result container
@dataclass
class ReconciliationResult:
    config: ReconConfig
    validation_issues: list = field(default_factory=list)
    ledger_status: list = field(default_factory=list)
    bank_status: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    bridge: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _bank_key(b):
    return (b["transaction_id"], C.SETTLEMENT_TYPES[b["settlement_type"]])


def reconcile(ledger_rows, bank_rows, cfg: ReconConfig = DEFAULT_CONFIG) -> ReconciliationResult:
    start, cutoff, tol = cfg.period_start, cfg.period_end, cfg.tolerance
    ledger_valid, issues = validate_ledger(ledger_rows, cfg)
    bank_valid, b_issues = validate_bank(bank_rows, cfg)
    issues += b_issues

    ledger_active = []
    for r in ledger_valid:
        if r["transaction_status"] == "COMPLETED":
            ledger_active.append(r)
        else:
            issues.append({"dataset": "LEDGER", "row_number": r["_row"], "record_id": r["transaction_id"],
                           "field": "transaction_status", "severity": "INFO",
                           "issue": f"status {r['transaction_status']} is not settleable",
                           "action": "excluded from reconciliation"})

    # 1) duplicates BEFORE matching
    ledger_u, ledger_dups = dd.find_duplicates(
        ledger_active, lambda r: r["transaction_id"], lambda r: (r["transaction_date"], r["_row"]))
    bank_u, bank_dups = dd.find_duplicates(
        bank_valid, _bank_key, lambda r: (r["settlement_date"], r["bank_entry_id"], r["_row"]))
    orphans = dd.find_orphan_refunds(ledger_u)
    bank_idx = {_bank_key(b): b for b in bank_u}

    findings, ledger_status = [], []
    used_bank, primary_by_key = set(), {}
    tol_group = []                 # (ledger_amt, bank_amt) of in-window, within-tolerance rows
    matched_count, matched_amount = 0, ZERO
    in_period = lambda d: start <= d <= cutoff

    def lrow(L, B, status, row_findings, in_per):
        act = B["settlement_amount"] if B else None
        return {"transaction_id": L["transaction_id"], "transaction_date": L["transaction_date"],
                "transaction_type": L["transaction_type"], "original_transaction_id": L["original_transaction_id"],
                "expected_amount": L["expected_settlement_amount"], "actual_amount": act,
                "difference": None if act is None else L["expected_settlement_amount"] - act,
                "settlement_date": B["settlement_date"] if B else None,
                "bank_entry_id": B["bank_entry_id"] if B else "", "status": status,
                "all_findings": ", ".join(sorted({f["category"] for f in row_findings})),
                "in_period": in_per}

    for L in sorted(ledger_u, key=lambda r: (r["transaction_date"], r["transaction_id"])):
        key = (L["transaction_id"], L["transaction_type"])
        B = bank_idx.get(key)
        if B:
            used_bank.add(B["bank_entry_id"])
        if not in_period(L["transaction_date"]):
            ledger_status.append(lrow(L, B, C.OUT_OF_PERIOD, [], False))
            primary_by_key[key] = C.OUT_OF_PERIOD
            continue
        exp, row_f = L["expected_settlement_amount"], []
        if B is None:
            row_f.append(dd.missing_in_bank_finding(L, cfg))
        else:
            diff = exp - B["settlement_amount"]
            in_window = in_period(B["settlement_date"])
            if B["settlement_date"] > cutoff:
                row_f.append(dd.timing_finding(L, B, cfg))
                if abs(diff) > tol:
                    row_f.append(dd.amount_mismatch_finding(L, B, cfg, counts_in_bridge=False))
            elif abs(diff) > tol:
                row_f.append(dd.amount_mismatch_finding(L, B, cfg, counts_in_bridge=in_window))
            else:
                if in_window:
                    tol_group.append((exp, B["settlement_amount"]))
                if diff != 0:
                    row_f.append(dd.rounding_finding(L, B, cfg))
        if L["transaction_id"] in orphans:
            row_f.append(dd.orphan_refund_finding(L, B, cfg))
        status = dd.primary_status(row_f)
        if status == C.MATCHED:
            matched_count += 1
            matched_amount += exp
        primary_by_key[key] = status
        findings += row_f
        ledger_status.append(lrow(L, B, status, row_f, True))

    # duplicates
    duplicates = []
    for x in ledger_dups:
        f = dd.duplicate_finding(x, "LEDGER", False, cfg)
        findings.append(f)
        d = x["duplicate"]
        ledger_status.append({**lrow(d, None, C.DUPLICATE_ENTRY, [f], in_period(d["transaction_date"])),
                              "actual_amount": None, "difference": None})
        duplicates.append({"dataset": "LEDGER", "kept_record": x["kept"]["transaction_id"],
                           "duplicate_record": d["transaction_id"], "key": str(x["key"]),
                           "amount": d["expected_settlement_amount"],
                           "date": d["transaction_date"]})
    for x in bank_dups:
        d = x["duplicate"]
        f = dd.duplicate_finding(x, "BANK", in_period(d["settlement_date"]), cfg)
        findings.append(f)
        duplicates.append({"dataset": "BANK", "kept_record": x["kept"]["bank_entry_id"],
                           "duplicate_record": d["bank_entry_id"], "key": " / ".join(x["key"]),
                           "amount": d["settlement_amount"], "date": d["settlement_date"]})

    # bank-side status rows
    bank_status = []
    dup_ids = {x["duplicate"]["bank_entry_id"] for x in bank_dups}
    for B in sorted(bank_valid, key=lambda r: (r["settlement_date"], r["bank_entry_id"])):
        if B["bank_entry_id"] in dup_ids:
            status = C.DUPLICATE_ENTRY
        elif B["bank_entry_id"] in used_bank:
            status = primary_by_key[_bank_key(B)]
        else:
            status = C.MISSING_IN_LEDGER
            findings.append(dd.missing_in_ledger_finding(B, in_period(B["settlement_date"]), cfg))
        bank_status.append({"bank_entry_id": B["bank_entry_id"], "transaction_id": B["transaction_id"],
                            "settlement_date": B["settlement_date"], "settlement_amount": B["settlement_amount"],
                            "settlement_type": B["settlement_type"], "batch_id": B["batch_id"],
                            "bank_reference": B["bank_reference"], "status": status})

    # 2) aggregate-level rounding, computed independently of per-transaction results
    ledger_tol = sum((a for a, _ in tol_group), ZERO)
    bank_tol_unrounded = sum((b for _, b in tol_group), ZERO)
    bank_tol_rounded = C.to_cents(bank_tol_unrounded)
    rounding_agg = ledger_tol - bank_tol_rounded
    n_rounding_txn = sum(1 for f in findings if f["category"] == C.ROUNDING_DIFFERENCE)
    if rounding_agg != 0 or n_rounding_txn:
        findings.append(dd.aggregate_rounding_finding(ledger_tol, bank_tol_unrounded, bank_tol_rounded,
                                                      len(tol_group), cfg))

    # 3) period totals, independent of matching
    period_ledger = [L for L in ledger_u if in_period(L["transaction_date"])]
    pays = [L for L in period_ledger if L["transaction_type"] == C.PAYMENT]
    refs = [L for L in period_ledger if L["transaction_type"] == C.REFUND]
    gross_pay = sum((L["gross_amount"] for L in pays), ZERO)
    refund_val = sum((L["gross_amount"] for L in refs), ZERO)
    fees = sum((L["processing_fee"] for L in period_ledger), ZERO)
    E = sum((L["expected_settlement_amount"] for L in period_ledger), ZERO)
    window = [B for B in bank_valid if in_period(B["settlement_date"])]
    B_pay = C.to_cents(sum((b["settlement_amount"] for b in window if b["settlement_type"] == "PAYMENT_SETTLEMENT"), ZERO))
    B_ref = C.to_cents(sum((b["settlement_amount"] for b in window if b["settlement_type"] == "REFUND_DEBIT"), ZERO))
    B_total = C.to_cents(sum((b["settlement_amount"] for b in window), ZERO))
    D = E - B_total

    comp = defaultdict(lambda: ZERO)
    for f in findings:
        if f["counts_in_bridge"]:
            comp[f["category"]] += f["period_impact"]
    explained = sum(comp.values(), ZERO)
    unexplained = D - explained

    for i, f in enumerate(sorted(findings, key=lambda f: (f["category"], str(f["reference_id"]))), start=1):
        f["finding_id"] = f"F-{i:04d}"
    findings.sort(key=lambda f: f["finding_id"])

    bridge = [
        {"component": "Timing differences (settled after cutoff)", "category": C.TIMING_DIFFERENCE, "amount": comp[C.TIMING_DIFFERENCE]},
        {"component": "Ledger transactions missing in bank", "category": C.MISSING_IN_BANK, "amount": comp[C.MISSING_IN_BANK]},
        {"component": "Amount mismatches beyond tolerance", "category": C.AMOUNT_MISMATCH, "amount": comp[C.AMOUNT_MISMATCH]},
        {"component": "Aggregate rounding difference", "category": C.ROUNDING_DIFFERENCE, "amount": comp[C.ROUNDING_DIFFERENCE]},
        {"component": "Bank duplicate entries (bank received more)", "category": C.DUPLICATE_ENTRY, "amount": comp[C.DUPLICATE_ENTRY]},
        {"component": "Bank entries missing in ledger", "category": C.MISSING_IN_LEDGER, "amount": comp[C.MISSING_IN_LEDGER]},
        {"component": "Unexplained difference", "category": "UNEXPLAINED", "amount": unexplained},
    ]

    for row in bridge:
        row["amount"] = C.to_cents(row["amount"])

    ledger_dup_amt = sum((d["amount"] for d in duplicates if d["dataset"] == "LEDGER"), ZERO)
    bank_dup_amt = sum((d["amount"] for d in duplicates if d["dataset"] == "BANK" and in_period(d["date"])), ZERO)
    orphan_rows = [L for L in period_ledger if L["transaction_id"] in orphans]
    unmatched_ledger = [s for s in ledger_status if s["in_period"] and s["status"] not in (C.MATCHED,)]
    q = C.to_cents
    summary = {
        "period_start": start, "period_end": cutoff, "currency": cfg.currency, "tolerance": tol,
        "ledger_rows_raw": len(ledger_rows), "ledger_rows_valid": len(ledger_valid),
        "ledger_unique_in_period": len(period_ledger),
        "ledger_out_of_period": len([L for L in ledger_u if not in_period(L["transaction_date"])]),
        "bank_rows_raw": len(bank_rows), "bank_rows_valid": len(bank_valid), "bank_entries_in_period": len(window),
        "invalid_rows": len([i for i in issues if i["severity"] == "ERROR"]),
        "gross_payment_value": q(gross_pay), "refund_value": q(refund_val), "processing_fees": q(fees),
        "net_expected_settlement": q(E),
        "actual_payment_receipts": B_pay, "actual_refund_debits": B_ref, "actual_bank_settlement": B_total,
        "reconciliation_difference": q(D),
        "matched_count": matched_count, "matched_amount": q(matched_amount),
        "unmatched_ledger_count": len(unmatched_ledger),
        "unmatched_ledger_amount": q(E - matched_amount),
        "unmatched_bank_amount": q(bank_dup_amt + sum((-f["period_impact"] for f in findings
                                    if f["category"] == C.MISSING_IN_LEDGER and f["counts_in_bridge"]), ZERO)),
        "duplicate_impact": q(bank_dup_amt), "ledger_duplicate_amount_excluded": q(ledger_dup_amt),
        "timing_difference_amount": q(comp[C.TIMING_DIFFERENCE]),
        "timing_difference_count": sum(1 for s in ledger_status if s["status"] == C.TIMING_DIFFERENCE),
        "rounding_difference_amount": q(rounding_agg), "rounding_transaction_count": n_rounding_txn,
        "orphan_refund_amount": q(sum((L["expected_settlement_amount"] for L in orphan_rows), ZERO)),
        "orphan_refund_count": len(orphan_rows),
        "missing_in_bank_amount": q(comp[C.MISSING_IN_BANK]),
        "amount_mismatch_amount": q(comp[C.AMOUNT_MISMATCH]),
        "missing_in_ledger_amount": q(comp[C.MISSING_IN_LEDGER]),
        "explained_difference": q(explained), "unexplained_difference": q(unexplained),
        "discrepancy_count": len(unmatched_ledger)
        + sum(1 for s in bank_status if s["status"] in (C.DUPLICATE_ENTRY, C.MISSING_IN_LEDGER)),
        "finding_count": len(findings),
        "reconciliation_status": "FULLY EXPLAINED" if abs(unexplained) <= tol else "UNEXPLAINED DIFFERENCE",
    }
    return ReconciliationResult(cfg, issues, ledger_status, bank_status, findings, duplicates, bridge, summary)
