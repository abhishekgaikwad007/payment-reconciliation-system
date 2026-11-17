"""Tests assert on concrete calculated values, not merely that code runs."""
from decimal import Decimal

import config as C
from config import ReconConfig
from data_generator import generate_datasets, fee_unrounded, ledger_fee
from reconciliation_engine import reconcile, validate_ledger, validate_bank
from report_generator import build_report_csv
from helpers import ledger_row, bank_row, finding, ledger_status

D = Decimal


# 1. correct matching of valid transactions
def test_valid_transactions_match_including_refund():
    ledger = [ledger_row("T1", "2026-11-05"), ledger_row("T2", "2026-11-06"),
              ledger_row("R1", "2026-11-08", gross="40.00", fee="0.00", ttype="REFUND", orig="T1")]
    bank = [bank_row("B1", "T1", "2026-11-06", "96.70"), bank_row("B2", "T2", "2026-11-08", "96.70"),
            bank_row("B3", "R1", "2026-11-09", "-40.00", "REFUND_DEBIT")]
    r = reconcile(ledger, bank)
    assert [s["status"] for s in r.ledger_status] == ["MATCHED"] * 3
    assert r.summary["matched_count"] == 3
    assert r.summary["matched_amount"] == D("153.40")
    assert r.summary["reconciliation_difference"] == D("0.00")
    assert r.findings == []


def test_payment_not_matched_to_refund_entry_of_same_id():
    r = reconcile([ledger_row("T1", "2026-11-05")],
                  [bank_row("B1", "T1", "2026-11-06", "-96.70", "REFUND_DEBIT")])
    assert ledger_status(r, "T1")["status"] == "MISSING_IN_BANK"
    assert r.bank_status[0]["status"] == "MISSING_IN_LEDGER"


# 2. November transaction settled in December
def test_next_month_settlement_is_timing_not_missing(result, manifest):
    tid = manifest["next_month_transaction_id"]
    assert ledger_status(result, tid)["status"] == "TIMING_DIFFERENCE"
    f = finding(result, "TIMING_DIFFERENCE", tid)[0]
    assert str(f["transaction_date"]) == "2026-11-30" and str(f["settlement_date"]) == "2026-12-02"
    assert f["expected_amount"] == D("240") - ledger_fee(D("240"))
    assert "subsequent accounting period" in f["explanation"]
    assert finding(result, "MISSING_IN_BANK", tid) == []
    ids = {s["transaction_id"] for s in result.ledger_status if s["status"] == "TIMING_DIFFERENCE"}
    assert ids == {tid, *manifest["other_boundary_transaction_ids"]}


# 3. aggregate rounding difference
def test_aggregate_rounding_difference_is_reproducible(result, manifest):
    ids = manifest["rounding_transaction_ids"]
    grosses = {s["transaction_id"]: s["gross_amount"] for s in generate_datasets()[0]}
    ledger_total = sum(grosses[i] - ledger_fee(grosses[i]) for i in ids)
    bank_total = sum(grosses[i] - fee_unrounded(grosses[i]) for i in ids)
    assert ledger_total - bank_total == D("-0.060")  # 12 x half a cent; platform rounds the fee up
    assert result.summary["rounding_difference_amount"] == D("-0.06")
    agg = [f for f in finding(result, "ROUNDING_DIFFERENCE") if str(f["reference_id"]).startswith("AGG-")]
    assert len(agg) == 1 and agg[0]["difference_amount"] == D("-0.06")
    for i in ids:  # every transaction individually matches within tolerance
        s = ledger_status(result, i)
        assert s["status"] == "MATCHED" and abs(s["difference"]) == D("0.005")


def test_rounding_becomes_visible_per_transaction_with_zero_tolerance(dataset):
    r = reconcile(dataset[0], dataset[1], ReconConfig(tolerance=D("0")))
    assert r.summary["rounding_difference_amount"] == D("0.00")
    assert sum(1 for s in r.ledger_status if s["status"] == "AMOUNT_MISMATCH") == 13  # 12 rounding + 1 planted
    assert r.summary["unexplained_difference"] == D("0.00")


# 4. duplicates
def test_duplicate_bank_entry_detected(result, manifest):
    f = finding(result, "DUPLICATE_ENTRY")
    assert len(f) == 1 and f[0]["dataset"] == "BANK"
    assert f[0]["reference_id"] == manifest["duplicate_bank_entry_id"]
    assert f[0]["transaction_id"] == manifest["duplicate_transaction_id"]
    assert f[0]["actual_amount"] == D("129") - fee_unrounded(D("129"))
    d = result.duplicates[0]
    assert d["dataset"] == "BANK" and d["duplicate_record"] == manifest["duplicate_bank_entry_id"]


def test_duplicate_detected_in_ledger_dataset_too():
    ledger = [ledger_row("T1", "2026-11-05"), ledger_row("T1", "2026-11-05")]
    r = reconcile(ledger, [bank_row("B1", "T1", "2026-11-06", "96.70")])
    assert r.duplicates[0]["dataset"] == "LEDGER"
    assert r.summary["net_expected_settlement"] == D("96.70")  # not 193.40
    assert r.summary["ledger_duplicate_amount_excluded"] == D("96.70")


# 5. orphan refund
def test_orphan_refund_detected(result, manifest):
    rid = manifest["orphan_refund_id"]
    assert ledger_status(result, rid)["status"] == "ORPHAN_REFUND"
    f = finding(result, "ORPHAN_REFUND", rid)[0]
    assert f["expected_amount"] == D("-85") and "TXN-999999" in f["explanation"]
    assert result.summary["orphan_refund_amount"] == D("-85.00") and result.summary["orphan_refund_count"] == 1


def test_valid_refund_is_not_orphan(result, manifest):
    refunds = [s for s in result.ledger_status if s["transaction_type"] == "REFUND"]
    assert len(refunds) == 25
    assert [s["transaction_id"] for s in refunds if s["status"] == "ORPHAN_REFUND"] == [manifest["orphan_refund_id"]]
    # the orphan's amounts agree with the bank, yet it is excluded from matched totals
    assert ledger_status(result, manifest["orphan_refund_id"])["difference"] == D("0")
    assert result.summary["matched_count"] == len([s for s in result.ledger_status if s["status"] == "MATCHED"])


# 6. duplicate double counting
def test_duplicate_does_not_inflate_matched_total():
    r = reconcile([ledger_row("T1", "2026-11-05")],
                  [bank_row("B1", "T1", "2026-11-06", "96.70"), bank_row("B2", "T1", "2026-11-06", "96.70")])
    s = r.summary
    assert s["matched_count"] == 1 and s["matched_amount"] == D("96.70")
    assert s["actual_bank_settlement"] == D("193.40")        # cash really was posted twice
    assert s["duplicate_impact"] == D("96.70")
    assert s["reconciliation_difference"] == D("-96.70") and s["unexplained_difference"] == D("0.00")
    assert [x["status"] for x in r.bank_status] == ["MATCHED", "DUPLICATE_ENTRY"]


# 7. totals
def test_expected_and_actual_totals(dataset, result):
    ledger, bank, _ = dataset
    exp = sum(r["expected_settlement_amount"] for r in ledger if r["transaction_date"].month == 11)
    act = sum(b["settlement_amount"] for b in bank if b["settlement_date"].month == 11)
    s = result.summary
    assert s["net_expected_settlement"] == exp.quantize(D("0.01"))
    assert s["actual_bank_settlement"] == C.to_cents(act)
    assert s["gross_payment_value"] - s["refund_value"] - s["processing_fees"] == s["net_expected_settlement"]
    assert s["actual_payment_receipts"] + s["actual_refund_debits"] == s["actual_bank_settlement"]
    assert s["reconciliation_difference"] == s["net_expected_settlement"] - s["actual_bank_settlement"]
    assert s["ledger_rows_raw"] >= 200 and s["bank_rows_raw"] >= 200


def test_bridge_fully_explains_difference_without_double_counting(result):
    s = result.summary
    assert sum(b["amount"] for b in result.bridge) == s["reconciliation_difference"]
    assert s["unexplained_difference"] == D("0.00")
    assert s["timing_difference_amount"] == D("624.75")
    assert s["duplicate_impact"] == D("124.83")
    for f in result.findings:  # orphan + per-transaction rounding notes carry no bridge weight
        if f["category"] == "ORPHAN_REFUND" or (f["category"] == "ROUNDING_DIFFERENCE"
                                                 and not str(f["reference_id"]).startswith("AGG-")):
            assert not f["counts_in_bridge"]


# 8. classification
def test_supporting_discrepancies_classified(result, manifest):
    assert ledger_status(result, manifest["missing_in_bank_id"])["status"] == "MISSING_IN_BANK"
    mm = ledger_status(result, manifest["amount_mismatch_id"])
    assert mm["status"] == "AMOUNT_MISMATCH" and mm["difference"] == D("4.75")
    f = finding(result, "MISSING_IN_LEDGER")[0]
    assert f["transaction_id"] == manifest["missing_in_ledger_transaction_id"] and f["actual_amount"] == D("62.40")
    assert {s["status"] for s in result.ledger_status} == {
        "MATCHED", "TIMING_DIFFERENCE", "MISSING_IN_BANK", "AMOUNT_MISMATCH", "ORPHAN_REFUND", "OUT_OF_PERIOD"}
    assert result.summary["matched_count"] == 177


def test_record_can_hold_multiple_findings():
    r = reconcile([ledger_row("R1", "2026-11-08", gross="40.00", fee="0.00", ttype="REFUND", orig="NOPE")],
                  [bank_row("B1", "R1", "2026-11-09", "-35.00", "REFUND_DEBIT")])
    assert {f["category"] for f in r.findings} >= {"ORPHAN_REFUND", "AMOUNT_MISMATCH"}
    assert r.ledger_status[0]["all_findings"] == "AMOUNT_MISMATCH, ORPHAN_REFUND"


# 9. invalid / missing fields
def test_invalid_ledger_fields_reported_and_excluded():
    rows = [ledger_row("T1", "2026-11-05"),
            ledger_row("", "2026-11-05"),
            ledger_row("T3", "not-a-date"),
            {**ledger_row("T4", "2026-11-05"), "gross_amount": "abc"},
            {**ledger_row("T5", "2026-11-05"), "transaction_type": "CHARGE"},
            ledger_row("R6", "2026-11-05", ttype="REFUND", orig=""),
            {**ledger_row("T7", "2026-11-05"), "currency": "EUR"}]
    valid, issues = validate_ledger(rows)
    assert [v["transaction_id"] for v in valid] == ["T1"]
    fields = {(i["record_id"], i["field"]) for i in issues}
    assert {("row 2", "transaction_id"), ("T3", "transaction_date"), ("T4", "gross_amount"),
            ("T5", "transaction_type"), ("R6", "original_transaction_id"), ("T7", "currency")} <= fields


def test_engine_survives_bad_bank_rows():
    bad = [bank_row("B1", "T1", "2026-11-06", "96.70"), bank_row("B2", "T9", "2026-11-06", "x"),
           bank_row("B3", "T9", "", "1.00"), bank_row("B4", "T9", "2026-11-06", "1.00", "WIRE")]
    r = reconcile([ledger_row("T1", "2026-11-05")], bad)
    assert r.summary["invalid_rows"] == 3 and r.summary["matched_count"] == 1
    assert len(validate_bank(bad)[0]) == 1


# 10. reproducibility
def test_generated_data_is_reproducible():
    a, b = generate_datasets(), generate_datasets()
    assert a == b
    assert generate_datasets(seed=1)[1] != a[1]


def test_committed_csvs_match_generator(tmp_path):
    from data_generator import write_datasets
    write_datasets(*generate_datasets(), data_dir=tmp_path)
    for name in ("internal_ledger.csv", "bank_settlements.csv", "planted_discrepancies.json"):
        assert (tmp_path / name).read_bytes() == (C.DATA_DIR / name).read_bytes()


def test_report_contains_period_summary_and_details(result):
    text = build_report_csv(result)
    assert "2026-11-01 to 2026-11-30" in text
    assert "Reconciliation difference D = E - B,611.66" in text
    assert "Unexplained difference,0.00" in text
    assert text.count("\nF-00") == result.summary["finding_count"]
