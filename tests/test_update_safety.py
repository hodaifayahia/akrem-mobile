"""Tests verifying that updating the application preserves existing user data."""

from __future__ import annotations

from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.migrate import upgrade_database
from app.db.models import Category, Customer, Setting, User
from app.services.auth import hash_password
from app.services.backup import list_backups
from app.services.seed import seed_defaults


def test_update_does_not_touch_existing_data_and_creates_safety_backup(tmp_path: Path, monkeypatch) -> None:
    """Simulate client version 1 having data, then updating to a new version.
    
    Verifies:
    1. Pre-upgrade safety backup is automatically created when database exists.
    2. All existing customer records, credentials, and settings remain untouched.
    3. Seed defaults do not overwrite existing categories or settings.
    """
    # Point data directory to isolated tmp_path
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path))
    db_file = tmp_path / "akremmobile.sqlite3"
    db_url = f"sqlite+pysqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("AKREMMOBILE_DATABASE_URL", db_url)

    # Step 1: Initial startup on empty database (Version 1 install)
    upgrade_database()
    seed_defaults()

    # Step 2: Client uses the app and enters real business data
    engine = create_engine(db_url)
    with Session(engine) as session:
        # Create an owner account
        owner = User(
            username="akrem",
            password_hash=hash_password("clientpassword123"),
            role="owner",
            disabled=False,
        )
        session.add(owner)

        # Add custom category
        custom_cat = Category(name="فئة خاصة جديدة", is_default=False)
        session.add(custom_cat)
        session.flush()

        # Add customer
        client_customer = Customer(
            category_id=custom_cat.id,
            full_name="محمد بن علي",
            phone="0555123456",
            id_number="123456789",
            address="أم الطيور",
        )
        session.add(client_customer)

        # Custom setting
        session.add(Setting(key="store_name", value='"محل أكرم موبايل"'))
        session.commit()

    # Step 3: Now simulate updating the application (.exe update)
    # The new version starts up and calls upgrade_database() and seed_defaults()
    upgrade_database()
    seed_defaults()

    # Step 4: Verify pre-upgrade safety backup was created
    backups = list_backups(tmp_path / "backups")
    assert len(backups) >= 1
    pre_upgrade_backups = [b for b in backups if b.backup_type == "pre_upgrade"]
    assert len(pre_upgrade_backups) == 1, "An automatic pre-upgrade safety backup must be created"
    assert pre_upgrade_backups[0].size_bytes > 0

    # Step 5: Verify all existing user data was preserved and NOT touched
    with Session(engine) as session:
        user = session.scalar(select(User).where(User.username == "akrem"))
        assert user is not None
        assert user.role == "owner"

        cust = session.scalar(select(Customer).where(Customer.phone == "0555123456"))
        assert cust is not None
        assert cust.full_name == "محمد بن علي"
        assert cust.address == "أم الطيور"

        setting = session.scalar(select(Setting).where(Setting.key == "store_name"))
        assert setting is not None
        assert "محل أكرم موبايل" in setting.value

        custom_cat_db = session.scalar(select(Category).where(Category.name == "فئة خاصة جديدة"))
        assert custom_cat_db is not None
        assert custom_cat_db.is_default is False

    engine.dispose()
