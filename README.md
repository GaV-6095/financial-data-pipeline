# Financial Data Pipeline

A learning project for financial data automation and future SAP HANA Cloud integration.
Currently runs locally with Python's standard library and SQLite; no extra packages needed.
All included transactions are sample data in AUD.

## Run in Ubuntu

```bash
cd ~/financial-data-pipeline
python3 financial_pipeline.py
```

The program validates the entire CSV, saves a batch to `financial_data.db`, and
prints totals for the **current CSV**, not all historical database records.
Logs appear in the terminal and `financial_pipeline.log` with UTC timestamps.
An unsuccessful run returns exit code 1; success returns 0.
The database and log are located beside the script, regardless of the working directory.

## Input

`financial_data.csv` requires `date,description,income,expenses` columns.
Dates use YYYY-MM-DD. Descriptions must not be blank. Amounts must be finite,
non-negative, with at most two decimal places; use 0.00 rather than a blank cell.
A single amount cannot exceed 92233720368547758.07 AUD (SQLite integer cents limit).
An empty file is invalid; a header-only file produces a warning and saves nothing.

## Database behavior

- `imports` records each batch fingerprint, source filename, UTC import time and row count.
- `transactions` records dates, descriptions, and income/expenses in integer cents.
- SQL parameters handle text safely, including descriptions with apostrophes.
- Each batch is saved in one transaction. A failure rolls back that entire batch.
- Rerunning the same normalized rows in the same order is skipped, even if the CSV is renamed.
- Repeated identical rows inside a CSV are retained: they may represent separate payments.

**Scope of duplicate protection:** it applies to whole batches, not individual payments.
Changing, reordering or appending rows creates a new batch. Importing that changed
file can repeat records from a previous batch. For now, use unchanged files for
reruns and non-overlapping batches for new data. Transaction IDs and update/merge
support are future work. This project is not yet a bank-connected production system.

## Tests

```bash
python3 -B -m unittest -v
```

Tests cover validation, logs, exact money calculations, database persistence,
repeat imports, and rollback after an insertion failure. Database tests use temporary files.

## Inspect saved data without installing a database viewer

Run this in the Ubuntu terminal from the project directory:

```bash
python3 - <<'PY'
import sqlite3
from contextlib import closing

with closing(sqlite3.connect('file:financial_data.db?mode=ro', uri=True)) as db:
    for row in db.execute('SELECT date, description, income_cents, expenses_cents FROM transactions ORDER BY import_fingerprint, row_number'):
        print(row)
PY
```

The final two numbers are cents (for example, 1250 means AUD 12.50).
SQLite files are binary databases, so they cannot be read as plain text in VS Code.

Next milestones: stable transaction IDs for incremental imports, Git/GitHub setup,
and SAP HANA Cloud integration. No cloud connection or deployment is included yet.
