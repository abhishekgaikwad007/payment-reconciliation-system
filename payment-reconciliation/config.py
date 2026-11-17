"""Central configuration for the reconciliation system.

Everything that affects financial results (period, tolerance, fee rule,
rounding policy) lives here so it is documented and testable in one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LEDGER_CSV = DATA_DIR / "internal_ledger.csv"
BANK_CSV = DATA_DIR / "bank_settlements.csv"
MANIFEST_JSON = DATA_DIR / "planted_discrepancies.json"

SEED = 2026
CURRENCY = "USD"

# Money precision / rounding policy
CENT = Decimal("0.01")
BANK_PRECISION = Decimal("0.0001")  # bank keeps 4 decimals per entry
ROUNDING_MODE = ROUND_HALF_UP

# Fee rule used by the platform: 3% of gross + 0.30 fixed (payments only)
FEE_RATE = Decimal("0.03")
FIXED_FEE = Decimal("0.30")
MAX_SETTLEMENT_LAG_DAYS = 2

LEDGER_COLUMNS = [
    "transaction_id", "transaction_date", "transaction_type",
    "original_transaction_id", "gross_amount", "processing_fee",
    "expected_settlement_amount", "currency", "transaction_status",
]
BANK_COLUMNS = [
    "bank_entry_id", "transaction_id", "settlement_date", "settlement_amount",
    "settlement_type", "batch_id", "currency", "bank_reference",
]

PAYMENT, REFUND = "PAYMENT", "REFUND"
TRANSACTION_TYPES = (PAYMENT, REFUND)
TRANSACTION_STATUSES = ("COMPLETED", "PENDING", "FAILED")
# bank settlement_type -> ledger transaction_type it must pair with
SETTLEMENT_TYPES = {"PAYMENT_SETTLEMENT": PAYMENT, "REFUND_DEBIT": REFUND}

# Reconciliation statuses / finding categories
MATCHED = "MATCHED"
MISSING_IN_BANK = "MISSING_IN_BANK"
MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
TIMING_DIFFERENCE = "TIMING_DIFFERENCE"
DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
ROUNDING_DIFFERENCE = "ROUNDING_DIFFERENCE"
ORPHAN_REFUND = "ORPHAN_REFUND"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
OUT_OF_PERIOD = "OUT_OF_PERIOD"  # informational: ledger record outside the period

# When a record has several findings, the first match here is its primary status.
STATUS_PRIORITY = [MISSING_IN_BANK, AMOUNT_MISMATCH, ORPHAN_REFUND,
                   TIMING_DIFFERENCE, DUPLICATE_ENTRY, MISSING_IN_LEDGER]


def to_cents(value: Decimal) -> Decimal:
    """Quantize to cents using the platform rounding mode."""
    return value.quantize(CENT, rounding=ROUNDING_MODE)


@dataclass(frozen=True)
class ReconConfig:
    period_start: date = date(2026, 11, 1)
    period_end: date = date(2026, 11, 30)  # month-end cutoff (inclusive)
    tolerance: Decimal = Decimal("0.01")    # per-transaction amount tolerance
    currency: str = CURRENCY

    @property
    def period_label(self) -> str:
        return self.period_end.strftime("%B %Y")


DEFAULT_CONFIG = ReconConfig()
