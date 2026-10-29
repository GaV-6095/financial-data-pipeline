import csv
import hashlib
import json
import sqlite3
from contextlib import closing
import logging
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent
REQUIRED_COLUMNS = {"date", "description", "income", "expenses"}


class DataValidationError(ValueError):
    """The input CSV does not contain valid financial transactions."""


def configure_logging(log_path):
    """Write UTC timestamps to the terminal and a size-limited log file."""
    formatter = logging.Formatter(
        "%(asctime)s UTC | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    formatter.converter = time.gmtime
    # Open the file first so setup failures do not leave partial handlers behind.
    file_handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    console_handler = logging.StreamHandler()
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    for handler in (console_handler, file_handler):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def parse_amount(value, column, line_number):
    """Require finite, non-negative amounts with at most two decimal places."""
    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise DataValidationError(
            f"Line {line_number}: '{column}' must be a number; use 0.00 for no amount."
        ) from None
    if not amount.is_finite() or amount < 0:
        raise DataValidationError(
            f"Line {line_number}: '{column}' must be a finite, non-negative amount."
        )
    if amount.as_tuple().exponent < -2:
        raise DataValidationError(
            f"Line {line_number}: '{column}' must have at most two decimal places."
        )
    if amount > Decimal("92233720368547758.07"):
        raise DataValidationError(
            f"Line {line_number}: '{column}' exceeds the database storage limit."
        )
    return amount


def read_transactions(csv_path):
    """Validate the entire CSV before any records can be saved."""
    transactions = []
    LOGGER.info("Reading financial data from %s", csv_path)

    # utf-8-sig also accepts CSV files with a UTF-8 byte-order mark.
    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file, strict=True)
        if not reader.fieldnames:
            raise DataValidationError("The CSV file is empty; add a header and transactions.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise DataValidationError("The CSV header contains duplicate column names.")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise DataValidationError("Missing CSV columns: " + ", ".join(sorted(missing)))

        try:
            for row in reader:
                line_number = reader.line_num
                if None in row or any(value is None for value in row.values()):
                    raise DataValidationError(
                        f"Line {line_number}: the number of fields does not match the header."
                    )
                date_text = row["date"].strip()
                try:
                    parsed_date = date.fromisoformat(date_text)
                    if parsed_date.isoformat() != date_text:
                        raise ValueError
                except ValueError:
                    raise DataValidationError(
                        f"Line {line_number}: 'date' must be a valid date in YYYY-MM-DD format."
                    ) from None
                if not row["description"].strip():
                    raise DataValidationError(
                        f"Line {line_number}: 'description' must not be blank."
                    )
                income = parse_amount(row["income"], "income", line_number)
                expenses = parse_amount(row["expenses"], "expenses", line_number)
                transactions.append((
                    date_text, row["description"].strip(),
                    int(income * 100), int(expenses * 100),
                ))
        except csv.Error as error:
            raise DataValidationError(
                f"CSV formatting error near line {reader.line_num}: {error}"
            ) from None

    if not transactions:
        LOGGER.warning("The CSV contains a header but no transactions; totals are zero.")
    LOGGER.info("Validated %d transactions", len(transactions))
    return transactions


def summarize_transactions(transactions):
    # Sum integer cents first; never store or calculate money using floats.
    income_cents = sum(row[2] for row in transactions)
    expense_cents = sum(row[3] for row in transactions)
    income = Decimal(income_cents) / 100
    expenses = Decimal(expense_cents) / 100
    return income, expenses, income - expenses


def calculate_totals(csv_path):
    """Keep the original CSV-summary interface available."""
    return summarize_transactions(read_transactions(csv_path))


def save_transactions(db_path, transactions, source_name):
    """Atomically save a complete batch; return False if already imported.

    The fingerprint covers normalized rows, including their order and repeats.
    Changed or overlapping files are separate batches, not record-level updates.
    """
    if not transactions:
        LOGGER.info("No transactions to save")
        return False
    payload = json.dumps(transactions, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS imports (
                    fingerprint TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    row_count INTEGER NOT NULL CHECK (row_count > 0)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    import_fingerprint TEXT NOT NULL REFERENCES imports(fingerprint),
                    row_number INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    income_cents INTEGER NOT NULL CHECK (income_cents >= 0),
                    expenses_cents INTEGER NOT NULL CHECK (expenses_cents >= 0),
                    PRIMARY KEY (import_fingerprint, row_number)
                )
            """)
            if connection.execute(
                "SELECT 1 FROM imports WHERE fingerprint = ?", (fingerprint,)
            ).fetchone():
                LOGGER.info("This batch is already imported; no duplicate records added")
                return False
            connection.execute(
                "INSERT INTO imports (fingerprint, source_name, row_count) VALUES (?, ?, ?)",
                (fingerprint, source_name, len(transactions)),
            )
            connection.executemany(
                """INSERT INTO transactions
                   (import_fingerprint, row_number, date, description, income_cents, expenses_cents)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ((fingerprint, number, *row) for number, row in enumerate(transactions, 1)),
            )
    LOGGER.info("Saved %d transactions to %s", len(transactions), db_path)
    return True


def main():
    try:
        configure_logging(PROJECT_DIR / "financial_pipeline.log")
    except OSError as error:
        # File logging may fail, for example if the directory is read-only.
        print(f"ERROR: Cannot open the log file: {error}", file=sys.stderr)
        return 1

    LOGGER.info("Financial Data Pipeline started")
    csv_path = PROJECT_DIR / "financial_data.csv"
    try:
        transactions = read_transactions(csv_path)
        total_income, total_expenses, balance = summarize_transactions(transactions)
        save_transactions(PROJECT_DIR / "financial_data.db", transactions, csv_path.name)
    except FileNotFoundError:
        LOGGER.error("Data file not found: %s. Place financial_data.csv beside the script.", csv_path)
        return 1
    except PermissionError:
        LOGGER.error("Permission denied reading %s. Check the file permissions.", csv_path)
        return 1
    except UnicodeError:
        LOGGER.error("Cannot decode the CSV. Save it using UTF-8 encoding.")
        return 1
    except (DataValidationError, csv.Error) as error:
        LOGGER.error("Invalid financial data: %s", error)
        return 1
    except sqlite3.Error as error:
        LOGGER.error("Database import failed; this batch was not saved: %s", error)
        return 1
    except OSError as error:
        LOGGER.error("Cannot read the data file: %s", error)
        return 1
    except Exception:
        LOGGER.exception("Unexpected pipeline failure")
        return 1

    print("\nFinancial summary for current CSV (AUD)")
    print(f"Total income:   ${total_income:,.2f}")
    print(f"Total expenses: ${total_expenses:,.2f}")
    print(f"Balance:        ${balance:,.2f}")
    LOGGER.info("Financial Data Pipeline completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
