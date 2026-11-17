from decimal import Decimal


def ledger_row(tid, day, gross="100.00", fee="3.30", ttype="PAYMENT", orig="", status="COMPLETED"):
    g, f = Decimal(gross), Decimal(fee)
    exp = g - f if ttype == "PAYMENT" else -g - f
    return {"transaction_id": tid, "transaction_date": day, "transaction_type": ttype,
            "original_transaction_id": orig, "gross_amount": str(g), "processing_fee": str(f),
            "expected_settlement_amount": str(exp), "currency": "USD", "transaction_status": status}


def bank_row(entry, tid, day, amount, stype="PAYMENT_SETTLEMENT"):
    return {"bank_entry_id": entry, "transaction_id": tid, "settlement_date": day,
            "settlement_amount": str(amount), "settlement_type": stype,
            "batch_id": "B1", "currency": "USD", "bank_reference": "R-" + entry}


def finding(result, category, reference=None):
    return [f for f in result.findings if f["category"] == category
            and (reference is None or reference in (f["reference_id"], f["transaction_id"]))]


def ledger_status(result, tid):
    return next(s for s in result.ledger_status if s["transaction_id"] == tid)
