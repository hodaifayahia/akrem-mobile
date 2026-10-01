"""Test importing the actual shop Excel sheet 'حساب التقسيط (أكرم عبدربه).xlsx'."""

from __future__ import annotations

from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.migrate import upgrade_database
from app.db.models import Category, Customer, Installment, Payment, Sale
from app.services.importer import import_preview, preview_workbook
from app.services.seed import seed_defaults


def test_import_akrem_installment_excel_sheet(tmp_path: Path, monkeypatch) -> None:
    """Validate that 'حساب التقسيط (أكرم عبدربه).xlsx' imports cleanly into the database."""
    excel_path = Path("حساب التقسيط (أكرم عبدربه).xlsx")
    assert excel_path.is_file(), "The Excel file must exist in the project root"

    # Step 1: Preview workbook
    preview = preview_workbook(excel_path)
    assert preview.sheet_name == "التقسيط"
    assert len(preview.missing_columns) == 0, f"Expected 0 missing columns, got: {preview.missing_columns}"
    assert len(preview.rows) >= 80

    ready_rows = [r for r in preview.rows if r.ready]
    assert len(ready_rows) >= 80, f"Expected at least 80 ready rows, got: {len(ready_rows)}"

    # Step 2: Set up fresh test database
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path))
    db_file = tmp_path / "akremmobile.sqlite3"
    db_url = f"sqlite+pysqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("AKREMMOBILE_DATABASE_URL", db_url)

    upgrade_database()
    seed_defaults()

    engine = create_engine(db_url)
    with Session(engine) as session:
        # Find default uncategorized category
        uncategorized = session.scalar(
            select(Category).where(Category.name == "غير مصنف")
        )
        assert uncategorized is not None

        # Step 3: Run import
        result = import_preview(
            session=session,
            preview=preview,
            uncategorized_id=uncategorized.id,
            mark_past_due_paid=True,
        )
        session.commit()

        # Step 4: Verify import results
        assert result.imported >= 80, f"Expected at least 80 imported sales, got: {result.imported}"

        from sqlalchemy import func

        # Verify customers were created and deduplicated
        total_customers = session.scalar(select(func.count(Customer.id)))
        assert total_customers > 0

        # Verify sales exist
        total_sales = session.scalar(select(func.count(Sale.id)))
        assert total_sales == result.imported

        # Verify installment sales generated installments
        total_installments = session.scalar(select(func.count(Installment.id)))
        assert total_installments > 0

        # Verify payments were recorded for past installments
        total_payments = session.scalar(select(func.count(Payment.id)))
        assert total_payments > 0

        print(f"\nImported {result.imported} sales, {total_customers} customers, {total_installments} installments, {total_payments} past payments.")

    engine.dispose()
