import pyodbc
from datetime import date
from decimal import Decimal
from collections import defaultdict

# =========================
# CONFIG
# =========================

SQL_CONNECTION_STRING = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=km-dw-server.database.windows.net;"
    "DATABASE=KM_DW_DB;"
    "UID=kmadmin;"
    "PWD=Kmbc.1432*;"
    "TrustServerCertificate=yes;"
)

START_MONTH = date(2026, 2, 1)

TARGET_TABLE = "sfcc.SalonRewardsMonthlyBalanceReport"


# =========================
# HELPERS
# =========================

def to_decimal(value):
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def first_day_of_month(dt):
    return date(dt.year, dt.month, 1)


def add_month(dt):
    if dt.month == 12:
        return date(dt.year + 1, 1, 1)
    return date(dt.year, dt.month + 1, 1)


def month_range(start_month, end_month):
    months = []
    current = start_month

    while current <= end_month:
        months.append(current)
        current = add_month(current)

    return months


# =========================
# SQL SETUP
# =========================

def create_target_table(cursor):
    cursor.execute(f"""
        IF OBJECT_ID('{TARGET_TABLE}', 'U') IS NULL
        BEGIN
            CREATE TABLE {TARGET_TABLE} (
                LedgerMonth date NOT NULL,
                CustomerNo varchar(50) NOT NULL,

                Care decimal(18,2) NOT NULL,
                Colour decimal(18,2) NOT NULL,
                Total decimal(18,2) NOT NULL,

                BeginningBalance decimal(18,2) NOT NULL,
                Accrued decimal(18,2) NOT NULL,
                Redeemed decimal(18,2) NOT NULL,

                ExpectedEndingBalance decimal(18,2) NOT NULL,
                Adjustment decimal(18,2) NOT NULL,
                EndingBalance decimal(18,2) NOT NULL,

                CreatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
                UpdatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),

                CONSTRAINT PK_SalonRewardsMonthlyBalanceReport
                    PRIMARY KEY (LedgerMonth, CustomerNo)
            );
        END
    """)


def clear_target_months(cursor, start_month, end_month):
    cursor.execute(f"""
        DELETE FROM {TARGET_TABLE}
        WHERE LedgerMonth >= ?
          AND LedgerMonth <= ?
    """, start_month, end_month)


# =========================
# DATA LOAD
# =========================

def load_stores(cursor):
    cursor.execute("""
        SELECT
            s.store_id,
            ISNULL(s.reward_points, 0) AS reward_points
        FROM sfcc.stores s
        WHERE s.store_id IS NOT NULL
    """)

    stores = {}

    for row in cursor.fetchall():
        stores[row.store_id] = to_decimal(row.reward_points)

    return stores


def load_history(cursor, start_month, end_month):
    cursor.execute("""
        SELECT
            h.customerNo,
            DATEFROMPARTS(YEAR(h.ledgerMonth), MONTH(h.ledgerMonth), 1) AS ledgerMonth,
            MAX(h.balance) AS Balance,
            SUM(ISNULL(h.accrued, 0)) AS Accrued,
            SUM(ISNULL(h.redeemed, 0)) AS Redeemed
        FROM sfcc.SalonRewardsHistoryLedger h
        WHERE h.ledgerMonth >= ?
          AND h.ledgerMonth <= ?
        GROUP BY
            h.customerNo,
            DATEFROMPARTS(YEAR(h.ledgerMonth), MONTH(h.ledgerMonth), 1)
    """, start_month, end_month)

    history = {}

    for row in cursor.fetchall():
        key = (row.customerNo, row.ledgerMonth)

        history[key] = {
            "balance": to_decimal(row.Balance) if row.Balance is not None else None,
            "accrued": to_decimal(row.Accrued),
            "redeemed": to_decimal(row.Redeemed),
        }

    return history


def load_uploads(cursor, start_month, end_month):
    cursor.execute("""
        SELECT
            u.customerNo,
            DATEFROMPARTS(YEAR(u.ledgerMonth), MONTH(u.ledgerMonth), 1) AS ledgerMonth,
            SUM(ISNULL(u.haircare, 0)) AS Care,
            SUM(ISNULL(u.colour, 0)) AS Colour
        FROM sfcc.SalonRewardsUploadLedger u
        WHERE u.ledgerMonth >= ?
          AND u.ledgerMonth <= ?
        GROUP BY
            u.customerNo,
            DATEFROMPARTS(YEAR(u.ledgerMonth), MONTH(u.ledgerMonth), 1)
    """, start_month, end_month)

    uploads = {}

    for row in cursor.fetchall():
        key = (row.customerNo, row.ledgerMonth)

        care = to_decimal(row.Care)
        colour = to_decimal(row.Colour)

        uploads[key] = {
            "care": care,
            "colour": colour,
            "total": care + colour,
        }

    return uploads


# =========================
# RECONCILIATION
# =========================

def build_report_rows(stores, history, uploads, months):
    rows = []

    for customer_no, store_starting_balance in stores.items():
        previous_ending_balance = None

        for i, ledger_month in enumerate(months):
            key = (customer_no, ledger_month)

            history_row = history.get(key, {})
            upload_row = uploads.get(key, {})

            care = upload_row.get("care", Decimal("0.00"))
            colour = upload_row.get("colour", Decimal("0.00"))
            total = upload_row.get("total", Decimal("0.00"))

            accrued = history_row.get("accrued", Decimal("0.00"))
            redeemed = history_row.get("redeemed", Decimal("0.00"))

            current_known_balance = history_row.get("balance")

            next_month = add_month(ledger_month)
            next_history_row = history.get((customer_no, next_month), {})
            next_known_beginning_balance = next_history_row.get("balance")

            if i == 0:
                beginning_balance = (
                    current_known_balance
                    if current_known_balance is not None
                    else store_starting_balance
                )
            else:
                beginning_balance = previous_ending_balance

            expected_ending_balance = beginning_balance + accrued - redeemed

            if next_known_beginning_balance is not None:
                ending_balance = next_known_beginning_balance
            else:
                ending_balance = expected_ending_balance

            adjustment = ending_balance - expected_ending_balance

            rows.append((
                ledger_month,
                customer_no,
                care,
                colour,
                total,
                beginning_balance,
                accrued,
                redeemed,
                expected_ending_balance,
                adjustment,
                ending_balance,
            ))

            previous_ending_balance = ending_balance

    return rows


# =========================
# INSERT
# =========================

def insert_rows(cursor, rows):
    cursor.fast_executemany = True

    cursor.executemany(f"""
        INSERT INTO {TARGET_TABLE} (
            LedgerMonth,
            CustomerNo,
            Care,
            Colour,
            Total,
            BeginningBalance,
            Accrued,
            Redeemed,
            ExpectedEndingBalance,
            Adjustment,
            EndingBalance
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)


# =========================
# MAIN
# =========================

def main():
    today = date.today()
    end_month = first_day_of_month(today)

    months = month_range(START_MONTH, end_month)

    conn = pyodbc.connect(SQL_CONNECTION_STRING)
    cursor = conn.cursor()

    try:
        print("Creating target table...")
        create_target_table(cursor)

        print("Loading stores...")
        stores = load_stores(cursor)

        print("Loading history ledger...")
        history = load_history(cursor, START_MONTH, end_month)

        print("Loading upload ledger...")
        uploads = load_uploads(cursor, START_MONTH, end_month)

        print("Building reconciled monthly ledger...")
        rows = build_report_rows(stores, history, uploads, months)

        print("Clearing existing report rows...")
        clear_target_months(cursor, START_MONTH, end_month)

        print(f"Inserting {len(rows)} rows...")
        insert_rows(cursor, rows)

        conn.commit()

        print("Done.")
        print(f"Inserted rows into {TARGET_TABLE}")

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()