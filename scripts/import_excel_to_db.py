#!/usr/bin/env python3
"""Import an Excel installment workbook directly into the AkremMobile database.

Usage:
    python scripts/import_excel_to_db.py ["path/to/excel_file.xlsx"] [--mark-paid]

Example:
    python scripts/import_excel_to_db.py "حساب التقسيط (أكرم عبدربه).xlsx"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

# Ensure app is discoverable on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import database_path, database_url, ensure_data_dirs
from app.db.migrate import upgrade_database
from app.db.models import Customer, Installment, Payment, Sale
from app.db.session import session_scope
from app.services.categories import get_fallback_type
from app.services.importer import import_preview, preview_workbook
from app.services.seed import seed_defaults


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import shop Excel installment sheet directly into the database."
    )
    parser.add_argument(
        "excel_path",
        nargs="?",
        default="حساب التقسيط (أكرم عبدربه).xlsx",
        help="Path to the Excel file (.xlsx or .xls).",
    )
    parser.add_argument(
        "--no-mark-past-paid",
        action="store_true",
        help="Do NOT automatically mark past-due installments as paid.",
    )
    args = parser.parse_args()

    excel_file = Path(args.excel_path)
    if not excel_file.is_file():
        # Try finding in project root
        candidate = PROJECT_ROOT / args.excel_path
        if candidate.is_file():
            excel_file = candidate
        else:
            print(f"❌ Error: Excel file not found: {args.excel_path}")
            return 1

    print("\n" + "=" * 60)
    print(f"  AkremMobile — Excel Database Importer")
    print("=" * 60)
    print(f"  • Source File:     {excel_file.name}")
    print(f"  • Target Database: {database_path()}")
    print("-" * 60)

    # 1. Initialize and migrate database if needed
    ensure_data_dirs()
    print("  [*] Checking database schema...")
    upgrade_database()
    seed_defaults()

    # 2. Preview the workbook
    print(f"  [*] Reading worksheet from '{excel_file.name}'...")
    try:
        preview = preview_workbook(excel_file)
    except Exception as exc:
        print(f"❌ Failed to parse Excel workbook: {exc}")
        return 1

    print(f"  [+] Worksheet:               {preview.sheet_name}")
    print(f"  [+] Total Parsed Rows:       {len(preview.rows)}")
    ready_rows = [r for r in preview.rows if r.ready]
    error_rows = [r for r in preview.rows if r.errors]
    print(f"  [+] Valid / Ready Rows:      {len(ready_rows)}")
    if error_rows:
        print(f"  [!] Incomplete / Skipped:    {len(error_rows)}")

    if not ready_rows:
        print("❌ No valid rows found to import.")
        return 1

    # 3. Perform the database import in a transactional session
    print("  [*] Importing records into database...")
    mark_past_due = not args.no_mark_past_paid
    with session_scope() as session:
        # New customers go to the protected system client type (created if missing).
        uncategorized = get_fallback_type(session)

        result = import_preview(
            session=session,
            preview=preview,
            uncategorized_id=uncategorized.id,
            mark_past_due_paid=mark_past_due,
        )

        total_customers = session.scalar(select(Customer.id).count()) if hasattr(Customer.id, "count") else len(session.scalars(select(Customer.id)).all())
        total_installments = len(session.scalars(select(Installment.id)).all())
        total_payments = len(session.scalars(select(Payment.id)).all())

    print("\n" + "=" * 60)
    print("  Import Complete — Summary")
    print("=" * 60)
    print(f"  ✅ Sales Imported:           {result.imported}")
    print(f"  ⚠️ Rows Skipped / Duplicates: {result.skipped}")
    print(f"  👥 Total Customers in DB:    {total_customers}")
    print(f"  📅 Total Installments in DB: {total_installments}")
    print(f"  💵 Past Payments Recorded:   {total_payments}")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
