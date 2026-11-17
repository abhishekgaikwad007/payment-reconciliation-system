"""Discrepancy detection primitives.

Pure functions that detect individual discrepancy types and build "finding"
records. The engine (reconciliation_engine.py) orchestrates them.

Sign convention everywhere:  difference = expected - actual.
`period_impact` is the effect of a finding on the period-level difference
(expected net settlement - bank settlements received inside the period) and
`counts_in_bridge` marks the findings that may be summed into the bridge, so
overlapping findings (e.g. a per-transaction rounding note and the aggregate
rounding finding) are never double counted.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Callable, Iterable

import config as C

ZERO = Decimal("0")


def m(x: Decimal) -> str:
    return f"{x:,.2f}"


# ---------------------------------------------------------------- detection
def find_duplicates(records: Iterable[dict], key_func: Callable, sort_key: Callable):
    """Split records into (unique, extras). The earliest record per key is kept."""
    groups = defaultdict(list)
    for rec in records:
        groups[key_func(rec)].append(rec)
    unique, extras = [], []
    for key, group in groups.items():
        ordered = sorted(group, key=sort_key)
        unique.append(ordered[0])
        for extra in ordered[1:]:
            extras.append({"key": key, "kept": ordered[0], "duplicate": extra})
    return unique, extras


def find_orphan_refunds(ledger_records: Iterable[dict]) -> dict:
    """Refunds whose original_transaction_id is not a PAYMENT in the ledger."""
    records = list(ledger_records)
    payment_ids = {r["transaction_id"] for r in records if r["transaction_type"] == C.PAYMENT}
    return {r["transaction_id"]: r for r in records
            if r["transaction_type"] == C.REFUND and r["original_transaction_id"] not in payment_ids}


def primary_status(findings: list[dict]) -> str:
    cats = {f["category"] for f in findings}
    for cat in C.STATUS_PRIORITY:
        if cat in cats:
            return cat
    return C.MATCHED


# ---------------------------------------------------------------- findings
def _finding(category, reference_id, transaction_id, dataset, expected, actual,
             impact, counts, tdate, sdate, explanation, action):
    return {
        "category": category, "reference_id": reference_id, "transaction_id": transaction_id,
        "dataset": dataset, "expected_amount": expected, "actual_amount": actual,
        "difference_amount": None if actual is None else expected - actual,
        "period_impact": impact, "counts_in_bridge": counts,
        "transaction_date": tdate, "settlement_date": sdate,
        "explanation": explanation, "suggested_action": action,
    }


def timing_finding(L, B, cfg):
    exp, tid = L["expected_settlement_amount"], L["transaction_id"]
    return _finding(
        C.TIMING_DIFFERENCE, tid, tid, "LEDGER+BANK", exp, B["settlement_amount"], exp, True,
        L["transaction_date"], B["settlement_date"],
        f"{tid} ({m(exp)} {cfg.currency}) was recorded in the {cfg.period_label} ledger on "
        f"{L['transaction_date']} but the bank settled it on {B['settlement_date']}, after the "
        f"{cfg.period_end} cutoff, i.e. in the subsequent accounting period. This is a timing "
        f"difference, not a missing payment: the funds exist at the bank and simply arrive in the next period.",
        "No correction needed. Carry as an in-transit settlement at period end and confirm it "
        "appears in the next period's bank statement.")


def missing_in_bank_finding(L, cfg):
    exp, tid = L["expected_settlement_amount"], L["transaction_id"]
    return _finding(
        C.MISSING_IN_BANK, tid, tid, "LEDGER", exp, ZERO, exp, True,
        L["transaction_date"], None,
        f"{tid} dated {L['transaction_date']} ({m(exp)}) has no bank settlement entry at all, "
        f"not even one dated after the {cfg.period_end} cutoff, so it cannot be explained as timing.",
        "Check the bank batch files and payment processor logs; raise a missing-settlement claim with the bank if confirmed.")


def amount_mismatch_finding(L, B, cfg, counts_in_bridge=True):
    exp, tid = L["expected_settlement_amount"], L["transaction_id"]
    diff = exp - B["settlement_amount"]
    return _finding(
        C.AMOUNT_MISMATCH, tid, tid, "LEDGER+BANK", exp, B["settlement_amount"],
        diff if counts_in_bridge else ZERO, counts_in_bridge,
        L["transaction_date"], B["settlement_date"],
        f"Ledger expected {m(exp)} but the bank settled {B['settlement_amount']:,.4f}; the difference of "
        f"{m(diff)} exceeds the {cfg.tolerance} tolerance.",
        "Compare the fee schedule applied by the bank with the platform fee rule; request a bank adjustment or correct the ledger.")


def rounding_finding(L, B, cfg):
    """Per-transaction, sub-tolerance difference. Informational (not summed in bridge)."""
    exp, tid = L["expected_settlement_amount"], L["transaction_id"]
    return _finding(
        C.ROUNDING_DIFFERENCE, tid, tid, "LEDGER+BANK", exp, B["settlement_amount"],
        exp - B["settlement_amount"], False, L["transaction_date"], B["settlement_date"],
        f"Ledger rounded the fee to cents ({m(exp)}); the bank carries {B['settlement_amount']:,.4f}. "
        f"Within tolerance, so the transaction matches, but it contributes to the aggregate rounding difference.",
        "None at transaction level; see the aggregate rounding finding.")


def aggregate_rounding_finding(ledger_total, bank_unrounded, bank_rounded, count, cfg):
    diff = ledger_total - bank_rounded
    return _finding(
        C.ROUNDING_DIFFERENCE, f"AGG-ROUNDING-{cfg.period_end:%Y-%m}", None, "LEDGER+BANK",
        ledger_total, bank_rounded, diff, True, cfg.period_start, cfg.period_end,
        f"Across {count} tolerance-matched entries the platform rounds each transaction to cents before summing "
        f"(ledger total {m(ledger_total)}), while the bank sums unrounded amounts ({bank_unrounded:,.4f}) and rounds "
        f"once ({m(bank_rounded)}). The {m(diff)} gap comes purely from rounding order and is only visible in the aggregate.",
        "Record as a rounding variance (rounding/clearing account) and agree a common rounding policy with the bank.")


def duplicate_finding(extra: dict, dataset: str, in_window: bool, cfg):
    dup, kept = extra["duplicate"], extra["kept"]
    if dataset == "BANK":
        amt, ref, tid = dup["settlement_amount"], dup["bank_entry_id"], dup["transaction_id"]
        txt = (f"Bank entry {ref} repeats transaction {tid} ({dup['settlement_type']}, {amt:,.4f}) already posted as "
               f"{kept['bank_entry_id']}. Counting it would overstate bank receipts by {m(amt)}; it is excluded from matching.")
        action = "Ask the bank to reverse/credit the duplicate posting and confirm funds were not double-paid."
        d_t, d_s = None, dup["settlement_date"]
    else:
        amt, ref, tid = dup["expected_settlement_amount"], dup["transaction_id"], dup["transaction_id"]
        txt = (f"Ledger transaction {tid} appears more than once ({m(amt)}). Only the first is counted so the "
               f"expected total is not inflated.")
        action = "Investigate the platform ingestion job that wrote the duplicate and remove it."
        d_t, d_s = dup["transaction_date"], None
    return _finding(C.DUPLICATE_ENTRY, ref, tid, dataset, ZERO, amt,
                    -amt if in_window else ZERO, in_window and dataset == "BANK", d_t, d_s, txt, action)


def missing_in_ledger_finding(B, in_window: bool, cfg):
    amt = B["settlement_amount"]
    return _finding(
        C.MISSING_IN_LEDGER, B["bank_entry_id"], B["transaction_id"], "BANK", ZERO, amt,
        -amt if in_window else ZERO, in_window, None, B["settlement_date"],
        f"Bank entry {B['bank_entry_id']} for transaction {B['transaction_id']} ({amt:,.4f}) has no matching "
        f"ledger record of the same type.",
        "Identify the payer via the bank reference; either a ledger record was never created or the bank posted a stray entry.")


def orphan_refund_finding(L, B, cfg):
    exp, tid = L["expected_settlement_amount"], L["transaction_id"]
    actual = B["settlement_amount"] if B else None
    return _finding(
        C.ORPHAN_REFUND, tid, tid, "LEDGER", exp, actual, ZERO, False,
        L["transaction_date"], B["settlement_date"] if B else None,
        f"Refund {tid} ({m(-exp)} refunded, reference to original payment {L['original_transaction_id']}) points to a "
        f"payment that does not exist in the internal ledger. Even if the bank amount agrees it is not a valid matched "
        f"refund: money left the company with no payment on record.",
        "Investigate urgently: locate the original payment (wrong ID? other period?), check for fraud or a manual refund, "
        "and recover or re-book as appropriate.")
