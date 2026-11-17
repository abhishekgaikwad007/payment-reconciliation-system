# Payment Reconciliation & Discrepancy Detection System

Reconciles an internal transaction ledger against bank settlement records for **November 2026**
(with December 2026 records to demonstrate subsequent-period settlements), explains every difference,
and presents results in a Streamlit dashboard. All findings are **derived by the reconciliation engine** from the
CSV data; nothing in the dashboard is hardcoded.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py          # dashboard (data/ CSVs are regenerated automatically if missing)
python data_generator.py      # optional: regenerate data/ (deterministic, seed 2026)
python -m pytest -q           # 19 tests
```

## Layout
| File | Responsibility |
|---|---|
| `config.py` | period, cutoff, tolerance, fee rule, rounding policy, status names |
| `data_generator.py` | deterministic synthetic ledger + bank data; writes `data/` incl. a manifest used **only by tests** |
| `discrepancy_detector.py` | duplicate / orphan-refund / timing / rounding detection and finding builders |
| `reconciliation_engine.py` | validation, dedupe, matching, aggregate totals, bridge, summary |
| `report_generator.py` | summary table and full CSV report |
| `app.py` | Streamlit + Plotly dashboard (Overview, Transaction Explorer, Discrepancy Analysis, Data Explorer, Reports) |
| `tests/` | pytest suite asserting concrete calculated values |

## Reconciliation method
1. Validate both datasets (missing/invalid fields, types, dates, currency, arithmetic). Bad rows are reported and excluded; the engine never crashes on them.
2. Detect duplicates **before** matching (ledger: same `transaction_id`; bank: same `transaction_id` + settlement type). The earliest record is kept; extras are excluded from matching.
3. Match ledger ↔ bank on `(transaction_id, PAYMENT|REFUND)`. Refunds are additionally checked against their `original_transaction_id`; a refund whose original payment is not in the ledger is an **orphan refund**.
4. Compare amounts with a per-transaction tolerance (default **0.01**, adjustable in the sidebar).
5. Bank entries settled after the cutoff for in-period ledger rows → **TIMING_DIFFERENCE** (never "missing").
6. Aggregate totals are computed independently of matching, then bridged.

**Formula** (difference = expected − actual):

```
D = E − B
E = net expected settlement of unique in-period ledger rows  (gross payments − refunds − fees)
B = bank entries with settlement_date inside the period (duplicates included: the cash was really posted)
D = timing + missing_in_bank + amount_mismatch + aggregate_rounding
    − bank_duplicates − bank_only_entries + unexplained
```
Each finding has `counts_in_bridge`; only flagged findings are summed, so overlapping findings (per-transaction rounding notes, orphan refunds whose bank amount agrees) are never double counted. A record can carry several findings; its *primary* status follows a fixed priority.

Result on the sample data: E = 41,997.90, B = 41,386.24, D = 611.66, fully explained (unexplained 0.00).

## Assumptions
- **Period**: 1–30 Nov 2026. Ledger rows are in scope by transaction date; bank cash by settlement date (cutoff 30 Nov, inclusive).
- **Settlement lag**: 1–2 calendar days (weekends/holidays ignored). Dec settlements can belong to Nov transactions; Dec ledger rows are `OUT_OF_PERIOD` and informational.
- **Fees**: payments 3% of gross + 0.30. Refunds carry no fee (fees are non-refundable). Net expected = gross − fee for payments, −gross for refunds. Currency: USD only.
- **Rounding**: the platform rounds the fee to cents **per transaction** (ROUND_HALF_UP) before summing. The bank carries 4-decimal amounts per entry and rounds **once on the aggregate**. The planted cohort is 12 payments with gross ending in `.50`: the 3% fee then ends in half a cent, so each ledger net is 0.005 lower than the bank's: 12 × 0.005 = **0.06** in total, visible only in aggregate (each entry is within the 0.01 tolerance). The figure is derived, not typed in; a test recomputes it independently from the fee rule.
- **Duplicates**: the sample plants one duplicate **bank** entry (same transaction ID/type/amount, new entry ID, same bank reference). A duplicated ledger row is ignored for E (counted once) and reported; a duplicated bank row stays in B because the bank did post it.
- **Orphan refunds**: refund whose `original_transaction_id` is not a PAYMENT in the ledger. Excluded from "matched" even if the bank agrees, flagged for investigation.
- **Statuses**: only `COMPLETED` ledger rows are reconcilable; others are listed as excluded.
- Opening in-transit items from October are assumed nil (no October data supplied).

## What this cannot reliably detect in production
- Duplicates with *different* IDs (e.g. a customer charged twice, or a bank re-posting under a new reference).
- Wrong-but-plausible matches: a bank entry carrying the wrong transaction ID that happens to match another record.
- Offsetting errors that cancel in the aggregate, and differences smaller than the tolerance.
- Refunds that exceed the original payment, partial/multiple refunds against one payment, chargebacks and FX effects (not modelled).
- Prior-period in-transit items, weekend/holiday calendars, bank cutoff times, fraud, and settlement of reversed or failed payments.
- Refund-orphan root cause: the system flags it, but a human must decide whether it is a keying error, fraud, or a cross-period refund.

Differences that need manual investigation: orphan refunds, missing-in-bank, amount mismatches, bank-only entries and bank duplicates. Timing and rounding items are explainable and normally need only confirmation.

## Deploy to Streamlit Community Cloud
1. Push this folder to a GitHub repository (include `data/`, or let the app regenerate it).
2. Go to https://share.streamlit.io, sign in with GitHub, choose **New app**.
3. Select the repo and branch, set **Main file path** to `app.py`, and deploy.
4. Dependencies are installed from `requirements.txt` automatically.

No deployment has been performed for this project, so no public URL is claimed.
