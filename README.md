# AkremMobile Installment Manager

Arabic, right-to-left Windows desktop application for managing customers, phone sales, installment schedules, and collections at AkremMobile in Oum Thiour, Algeria.

## Current features

- Customer records, editable customer categories, customer search, and sale/payment history.
- Cash, installment, and credit sales with whole-dinar calculations and installment schedules.
- Monthly payment tracking, partial payments, credit-balance collections, and owner-only audited payment undo.
- Dashboard and customer status views for paid, pending, and overdue installments; the payments page also lists overdue and near-due balances with a drafted WhatsApp reminder.
- New-sale flow warns the operator when an existing customer has at least two overdue installments.
- Owner editing of customer profiles and sales, with a confirmation step before recalculating a sale that has payment history.
- Excel import with worksheet and column mapping, row validation, duplicate protection, and an import report.
- Excel template creation, monthly collection and overdue exports, and owner-only profit reports.
- Arabic commitment-form and payment-receipt PDFs.
- Owner settings for users, product prices, installment rules, local backups, and restore.
- Arabic (right-to-left), English and French (left-to-right) interface with live switching; the layout, icons and charts mirror with the language. The language is saved per PC.
- Client types: four defaults plus a protected "غير مصنف" type; the owner can add, rename, recolour, reorder and delete types (moving their customers), filter the customer list by type and assign a type to several customers at once.
- Cash vs. installment customers: the customer list shows each customer's payment method (cash / installment / credit) and filters cash-only customers, customers paying little by little, installment or credit customers, and customers who still owe money or are fully paid; search also matches product names. The dashboard counts cash and installment/credit customers and this month's sales by type.
- Excel exports: the filtered customer list, monthly collection, overdue list, profit by month, and (owner only) a full workbook with customers, sales, installments, payments and client types.
- Runs in the Windows system tray: closing the window keeps the app (and its collection monitor and backups) running; a second launch brings the existing window forward.
- Notifications: an in-app notification center (overdue, upcoming, activity) and native Windows toasts for the daily collection digest and newly overdue installments while the window is in the background.

See [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) for the design system, RTL/LTR rules, and the tray/notification architecture.

## Requirements

- Windows 10 or 11, 64-bit
- Python 3.12

## Set up and run from PowerShell

Open PowerShell in the project folder:

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    python -m app.main

On first launch, the application runs database migrations, seeds the default categories and settings, and asks you to create an owner account. Sign in with that account to use the app. To launch it later, activate the environment and run:

    python -m app.main

## Build a Windows distribution

Build on Windows from the project folder after installing the requirements:

    .\scripts\build_windows.ps1

The PyInstaller one-folder application is written to `dist\AkremMobile`, a portable ZIP is written to `dist`, and the per-user setup package is written to `dist\installer\AkremMobile-Setup-<version>.exe`. The setup installs under `%LOCALAPPDATA%\Programs\AkremMobile`, creates Start menu and desktop shortcuts, and leaves the database under `%APPDATA%\AkremMobile` when the app is uninstalled.

## Accounts and access

- **Owner:** manages users, settings, product prices, customer categories, imports, backups, and restores; can see wholesale prices and profit, and undo the most recent payment with an audit reason.
- **Seller:** can use customer and payment workflows, create sales from the owner's active product catalog, and change their own password. Wholesale prices, profit, business settings, imports, and payment undo are restricted.
- Passwords are stored as bcrypt hashes. The initial owner password must be at least six characters. Five failed login attempts trigger a five-minute lockout; an inactivity timeout opens a reauthentication prompt.

## User data and generated files

By default, the application stores its data under %APPDATA%/AkremMobile:

- Database: akremmobile.sqlite3
- Automatic and manual backups: backups/ (timestamped SQLite files; automatic startup backup is limited to once per day and old backups are pruned to 30)
- Logs: logs/akremmobile.log and rotated log files
- Exports: exports/ (import reports and the default save location for report and PDF exports)

Report and PDF save dialogs allow you to choose a different destination. Set AKREMMOBILE_DATA_DIR to use a different per-user data directory.

An owner can create or restore local backups from Settings. Restore validates the selected backup, replaces the configured database, and closes the application; start AkremMobile again to continue.

## Multi-PC use

Local SQLite remains the default. Do not put its file on a network share or have multiple PCs open it. The app includes a PostgreSQL driver and accepts a SQLAlchemy connection URL through `AKREMMOBILE_DATABASE_URL`, but no database server has been selected or configured yet. The database host, database name, credentials, network access, first migration, and server backup plan still need to be set up before multiple PCs can share records. If the target is new and there is no local data to transfer, start one client first to initialize the shared database, then connect the remaining PCs. If moving existing data, use the transfer steps below before launching any client against the target.

Automatic backup and restore in the app currently support local SQLite only. A shared PostgreSQL deployment needs a server-side backup and restore plan; the local backup controls do not back up a remote PostgreSQL database.

To keep existing local records when moving to a newly provisioned PostgreSQL server, update the source PC to the current app version first so its local database has the latest schema. Then make a local SQLite backup and stop AkremMobile on every PC. Set `AKREMMOBILE_SOURCE_DATABASE_URL` to the full SQLite file URL and `AKREMMOBILE_DATABASE_URL` to the empty PostgreSQL database URL in the form `postgresql+psycopg://user:encoded-password@host:5432/database`, then run `python -m app.db.migrate_sqlite_to_postgres` once. Use a database password encoded for a URL and follow the host's TLS requirements. The transfer copies one SQLite database: categories, customers, products, sales, installments, payments, settings, users, and audit history, preserving IDs. It refuses to merge into a PostgreSQL database that already contains application data. If several PCs already have separate local databases, reconcile those histories before transferring one source. After the transfer, set the same `AKREMMOBILE_DATABASE_URL` on each PC before launching the app. Do not launch a client against the target until the transfer is complete.

PostgreSQL clients serialize startup migrations, default-data seeding, and first-owner setup. Payment entry locks its installment or credit sale while checking the balance, so simultaneous clients cannot collect more than the recorded balance. PostgreSQL still requires a server-side backup plan and a restricted, secure connection URL.

## Development & Build Commands

### 1. Activate Virtual Environment (Linux / WSL / Ubuntu)
```bash
source .venv/bin/activate
```
*Once activated, `python` and `pytest` work directly:*
```bash
pytest                                             # Run all tests
python scripts/build_exe.py                        # Build executable
python scripts/import_excel_to_db.py "حساب التقسيط (أكرم عبدربه).xlsx"  # Import Excel
```

### 2. Or Use Direct Shortcut Scripts
- **Build Executable:** `./build.sh` (or `build_exe.bat` on Windows)
- **Run Tests:** `./test.sh`
- **Import Excel into Database:** `./import_excel.sh "حساب التقسيط (أكرم عبدربه).xlsx"`
