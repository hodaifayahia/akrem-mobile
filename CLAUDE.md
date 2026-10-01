# AkremMobile Installment Manager

Windows desktop app in Python for AkremMobile, a phone shop in Oum Thiour, Algeria. It replaces the shop's Excel installment sheet and paper commitment form. It tracks customers, sales (cash, installment, and credit), and monthly payments.

## Stack

- Python 3.12, PySide6 (Qt 6)
- SQLAlchemy 2.x + SQLite for single-PC use; optional PostgreSQL through Psycopg 3 for a configured shared server; Alembic migrations
- pandas + openpyxl for Excel import/export
- bcrypt for passwords
- PDFs with Qt (QTextDocument + QPdfWriter) for Arabic shaping
- pytest for tests; PyInstaller + Inno Setup for packaging
- Do not change the stack without asking.
- The owner says records must be shared by multiple PCs. A SQLite file on a network share is not a supported multi-user database. Keep SQLite local for development until the shared-server database and host are agreed; do not claim multi-PC sharing is ready until it is configured and verified.

## Language and layout

- UI is Arabic and the whole app is right-to-left (`Qt.RightToLeft`). All UI strings live in `app/i18n/ar.py`.
- Money is stored as integer dinars, shown with Western-digit thousands separators and the suffix `دج` (for example, `41,850 دج`). Never store money as float.
- Dates are stored as ISO dates and shown as `DD/MM/YYYY`.
- User data goes in `%APPDATA%/AkremMobile` (database, backups, logs, exports).

## Theme

Dark by default. Tokens: bg `#05070B`, surface `#0E141D`, surface-2 `#121A2C`, border `#1F2A3D`, primary `#0758CD`, primary-glow `#3B92D9`, highlight `#9DBEFF`, silver `#C1C1C3`, text `#EBF0FF`, text-muted `#8A94A6`, pending `#F59E0B`, failed `#EF4444`, paid `#22C55E`. Use Cairo for Arabic and Rajdhani for the wordmark and large numbers. Cards have 12px corners and a 1px primary-glow border. Keep app styling in `app/resources/theme.qss`.

## Domain

- Customer categories: `أساتذة`, `منحة البطالة`, `عسكري`, `أعمال حرة`, `غير مصنف`. The owner can add, rename, or delete categories; deletion is allowed only when unused.
- Customer: full name, phone, category, ID/licence number, ID issue date and place, address, profession, CCP account number, cheque count, notes.
- Sale: customer, product, type (`كاش` cash / `بالتقسيط` installment / `كريدي` credit), wholesale price, cash price, rate %, down payment, months, payment interval (months between payments, 1 = monthly), purchase date. Computed values: total, financed, payment schedule, profit, end date.
- Product: name and selling price are required; wholesale price is optional (0 when unknown). Each product has a default installment plan (months, default 6; payment interval, default 1; optional rate, blank = rate preset for the months). Sale forms start from the product's plan and every sale may use its own plan.
- Stock item: one physical phone of a product, as in the shop's "Gros et Détail" sheet: wholesale price, cash price, battery (percentage for a used phone, or new — written `*`), colour, IMEI, REF (`REF-00001`, assigned automatically when blank), note, status (available/sold) and the sale that took it.
- A sale also stores the phone's colour, battery, IMEI and REF, and links to the catalog product: the owner's sales reuse the product with the same name (case and spacing ignored) or add it to the catalog; picking a stock unit (or typing its IMEI/REF) marks that phone sold.
- Debt book (owner only), separate from sales customers: a person (first name, last name, e-mail, national ID card number, phone) and a debt either owed to the shop (`receivable`, sidebar "ديون لي") or owed by the shop (`payable`, sidebar "ديون عليّ"): amount, reason, date, and repayment in one payment (optional due date) or by facility (months, payment every N months, split like an installment sale). Payments are recorded one by one and applied to the schedule oldest-first; status is overdue / due soon (7 days) / on track / paid, with the same grace days as sales.
- Dashboard net position (owner only): inside = what clients still owe (installments + credit) + receivable debts + unsold stock at wholesale price; outside = payable debts; net = inside − outside (cash in the drawer is not tracked). Month cash flow: sales collections + debt repayments received − payments made on payable debts.
- Installment: sale, index 1..N, due date, amount due, amount paid, paid date, method (`cash`, `CCP`, `BaridiMob`), note.
- Payment: each amount received, linked to an installment; supports partial payments.
- Credit down payment means money already paid. Credit has no monthly schedule; payments reduce the remaining balance and an expected payment date is optional.

## Business rules

- `total = cash_price * (1 + rate / 100)`
- `financed = total - down_payment`
- Installment plans run 1 to 60 months. With payment interval `n`, payments fall at month offsets `n, 2n, …` and the last payment is always at `months` (5 months every 2 → offsets 2, 4, 5); `n` cannot exceed `months`.
- For installments, round each payment down to a whole dinar and add the remainder to the last payment so the schedule totals exactly `financed`.
- `profit = total - wholesale_price`
- `end_date = purchase_date + months` (Excel `EDATE` behavior).
- Cash sale: no schedule, rate 0, total = cash price, profit = cash price - wholesale price.
- Due-date setting `due_mode` defaults to `first_of_month`: installment k is due on the 1st of purchase month + its offset (k × interval, capped at months). `purchase_day` is the alternate mode. `grace_days` defaults to 5.
- Status is evaluated for the selected month (current month by default): PAID when all installments due that month are fully paid; FAILED when any installment due that month or earlier is still unpaid after its due date plus grace days; PENDING when something is due that month and is not late; otherwise NONE. A past overdue installment keeps the customer FAILED even if the current month's installment is paid.
- Rate presets, editable by the owner (1 to 60 months, 0 to 100%): 4->35, 5->35, 6->35, 7->35, 10->40, 12->45.
- Status, alerts and reminders follow the stored due dates, so a client paying every N months is only due (and notified) in their payment months.

## Code conventions

- Folders: `app/main.py`, `app/config.py`, `app/db/` (models, session, migrations), `app/services/` (calculation, schedule, status, auth, importer, backup, PDF, reports), `app/ui/` (main window, pages, dialogs, widgets), `app/resources/` (logo, icon, fonts, theme), `tests/`.
- Business logic belongs in services, never UI classes. Each service gets pytest tests.
- Type hints everywhere, small functions, docstrings in English, UI text in Arabic.
- After each implementation task: run pytest, list changed files, and provide a short manual test checklist.

## Owner decisions still needed

- Multi-PC deployment: the owner confirmed that multiple PCs must share records, but has not yet chosen/provided the shared server location. The blueprint says this needs a shared server and is outside the original SQLite contract. Do not connect clients to a shared SQLite file.
- The owner confirmed the first-of-month / 5-day grace / overdue-status defaults and that a credit down payment is already paid.
- The owner asked for flexible plans: any duration (1, 3, 5, … months), payment every N months, a default plan per product, and a different plan per client when needed.
- The brief's rounding rule (round down each month, remainder in last month) and optional credit expected date are current defaults; confirm if the owner wants different behavior.
- No extra customer categories were specified; start with the five defaults.
- Assume the wholesale dashboard card covers all sales, as stated in the blueprint.
