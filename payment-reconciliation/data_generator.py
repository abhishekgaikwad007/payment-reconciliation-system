"""Deterministic synthetic data generator.

Produces an internal ledger and a bank settlement file for November 2026
(plus December 2026 records) with the four mandatory discrepancies planted,
plus three supporting ones (missing in bank / missing in ledger / amount
mismatch). A manifest of what was planted is returned for TESTS ONLY; the
dashboard never reads it, all findings come from the reconciliation engine.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import config as C


def fee_unrounded(gross: Decimal) -> Decimal:
    """Exact processing fee (may carry sub-cent precision)."""
    return gross * C.FEE_RATE + C.FIXED_FEE


def ledger_fee(gross: Decimal) -> Decimal:
    """Platform fee: rounded to cents PER TRANSACTION."""
    return C.to_cents(fee_unrounded(gross))


def bank_net_amount(gross: Decimal) -> Decimal:
    """Bank net: unrounded; the bank rounds only the aggregate total."""
    return gross - fee_unrounded(gross)


def generate_datasets(seed: int = C.SEED):
    rng = random.Random(seed)
    nov = lambda d: date(2026, 11, d)
    specs = []

    def add(day, gross, flag, lag=None):
        specs.append({"date": day, "gross": gross, "flag": flag, "lag": lag})

    for _ in range(140):  # ordinary November payments (whole dollars -> no sub-cent fees)
        add(nov(rng.randint(1, 28)), Decimal(rng.randint(15, 600)), "normal")
    # Rounding cohort: gross ends in .50 => 3% fee ends in half-a-cent => the
    # ledger's per-transaction half-up rounding differs from the unrounded bank value.
    for _ in range(12):
        add(nov(rng.randint(3, 24)), Decimal(rng.randint(8, 80)) + Decimal("0.50"), "rounding")
    # Timing: November transactions the bank settles in December
    add(nov(29), Decimal(310), "boundary", lag=2)
    add(nov(30), Decimal(95), "boundary", lag=1)
    add(nov(30), Decimal(240), "next_month", lag=2)
    add(nov(10), Decimal(175), "missing_in_bank")
    add(nov(16), Decimal(320), "amount_mismatch")
    add(nov(14), Decimal(129), "duplicate")
    for _ in range(20):  # December records
        add(date(2026, 12, rng.randint(1, 14)), Decimal(rng.randint(15, 600)), "december")

    specs.sort(key=lambda s: (s["date"], s["flag"], s["gross"]))
    meta, ledger = {}, []
    for i, s in enumerate(specs):
        tid = f"TXN-{100001 + i}"
        fee = ledger_fee(s["gross"])
        meta[tid] = s
        ledger.append({
            "transaction_id": tid, "transaction_date": s["date"],
            "transaction_type": C.PAYMENT, "original_transaction_id": "",
            "gross_amount": s["gross"], "processing_fee": fee,
            "expected_settlement_amount": s["gross"] - fee,
            "currency": C.CURRENCY, "transaction_status": "COMPLETED",
        })

    # Refunds of earlier November payments (fees are non-refundable => fee 0)
    payments = list(ledger)
    candidates = [p for p in payments if meta[p["transaction_id"]]["flag"] == "normal"
                  and p["transaction_date"] <= nov(26)]
    refunds = []
    for p in rng.sample(candidates, 24):
        rdate = min(p["transaction_date"] + timedelta(days=rng.randint(1, 3)), nov(28))
        half = Decimal(int(p["gross_amount"]) // 2)
        amount = rng.choice([p["gross_amount"], half])
        if amount < 1:
            amount = p["gross_amount"]
        refunds.append({"date": rdate, "gross": amount, "orig": p["transaction_id"], "flag": "refund"})
    # Orphan refund: original transaction does not exist in the ledger
    refunds.append({"date": nov(18), "gross": Decimal(85), "orig": "TXN-999999", "flag": "orphan_refund"})
    refunds.sort(key=lambda r: (r["date"], r["orig"]))
    for i, r in enumerate(refunds):
        rid = f"RFD-{200001 + i}"
        meta[rid] = r
        ledger.append({
            "transaction_id": rid, "transaction_date": r["date"],
            "transaction_type": C.REFUND, "original_transaction_id": r["orig"],
            "gross_amount": r["gross"], "processing_fee": Decimal("0.00"),
            "expected_settlement_amount": -r["gross"],
            "currency": C.CURRENCY, "transaction_status": "COMPLETED",
        })
    ledger.sort(key=lambda r: (r["transaction_date"], r["transaction_id"]))

    # ---- bank side
    entries = []

    def add_entry(tid, sdate, amount, stype, tag="", reference=None):
        entries.append({
            "transaction_id": tid, "settlement_date": sdate, "settlement_amount": amount,
            "settlement_type": stype, "_tag": tag, "_order": len(entries),
            "bank_reference": reference or f"BNK{rng.randint(10**9, 10**10 - 1)}",
        })
        return entries[-1]

    for row in ledger:
        tid, spec = row["transaction_id"], meta[row["transaction_id"]]
        if row["transaction_type"] == C.PAYMENT:
            if spec["flag"] == "missing_in_bank":
                continue
            lag = spec["lag"] or rng.randint(1, C.MAX_SETTLEMENT_LAG_DAYS)
            amount = bank_net_amount(row["gross_amount"])
            if spec["flag"] == "amount_mismatch":
                amount -= Decimal("4.75")  # bank deducted an extra fee
            e = add_entry(tid, row["transaction_date"] + timedelta(days=lag), amount, "PAYMENT_SETTLEMENT")
            if spec["flag"] == "duplicate":
                add_entry(tid, e["settlement_date"], amount, "PAYMENT_SETTLEMENT",
                          tag="duplicate_extra", reference=e["bank_reference"])
        else:
            lag = rng.randint(1, C.MAX_SETTLEMENT_LAG_DAYS)
            add_entry(tid, row["transaction_date"] + timedelta(days=lag),
                      -row["gross_amount"], "REFUND_DEBIT")
    add_entry("TXN-777001", nov(20), Decimal("62.40"), "PAYMENT_SETTLEMENT", tag="missing_in_ledger")

    entries.sort(key=lambda e: (e["settlement_date"], e["transaction_id"], e["_order"]))
    bank = []
    for i, e in enumerate(entries, start=1):
        e["bank_entry_id"] = f"BE-{i:06d}"
        bank.append({
            "bank_entry_id": e["bank_entry_id"], "transaction_id": e["transaction_id"],
            "settlement_date": e["settlement_date"],
            "settlement_amount": e["settlement_amount"].quantize(C.BANK_PRECISION),
            "settlement_type": e["settlement_type"],
            "batch_id": f"BATCH-{e['settlement_date']:%Y%m%d}",
            "currency": C.CURRENCY, "bank_reference": e["bank_reference"],
        })

    by_flag = lambda f: [t for t, s in meta.items() if s["flag"] == f]
    tag = lambda t: next(e["bank_entry_id"] for e in entries if e["_tag"] == t)
    manifest = {
        "reconciliation_period": "2026-11-01..2026-11-30",
        "next_month_transaction_id": by_flag("next_month")[0],
        "other_boundary_transaction_ids": by_flag("boundary"),
        "rounding_transaction_ids": by_flag("rounding"),
        "duplicate_transaction_id": by_flag("duplicate")[0],
        "duplicate_bank_entry_id": tag("duplicate_extra"),
        "orphan_refund_id": by_flag("orphan_refund")[0],
        "missing_in_bank_id": by_flag("missing_in_bank")[0],
        "amount_mismatch_id": by_flag("amount_mismatch")[0],
        "missing_in_ledger_transaction_id": "TXN-777001",
    }
    return ledger, bank, manifest


def _fmt(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            q = C.BANK_PRECISION if k == "settlement_amount" else C.CENT
            out[k] = f"{v.quantize(q):.{4 if k == 'settlement_amount' else 2}f}"
        else:
            out[k] = v
    return out


def write_datasets(ledger, bank, manifest, data_dir: Path = C.DATA_DIR) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, rows, cols in (("internal_ledger.csv", ledger, C.LEDGER_COLUMNS),
                             ("bank_settlements.csv", bank, C.BANK_COLUMNS)):
        with open(data_dir / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(_fmt(r) for r in rows)
    (data_dir / "planted_discrepancies.json").write_text(json.dumps(manifest, indent=2))


def read_csv_rows(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def ensure_datasets() -> None:
    if not (C.LEDGER_CSV.exists() and C.BANK_CSV.exists()):
        write_datasets(*generate_datasets())


if __name__ == "__main__":
    ledger_rows, bank_rows, man = generate_datasets()
    write_datasets(ledger_rows, bank_rows, man)
    print(f"ledger={len(ledger_rows)} bank={len(bank_rows)} -> {C.DATA_DIR}")
