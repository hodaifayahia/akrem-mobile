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
- Records are shared by several PCs through PostgreSQL on the main shop PC (owner's choice, 2026-10-03). Each PC stores its choice in its own `local_settings.json` (`database_url`); `AKREMMOBILE_DATABASE_URL` still overrides it; without either the PC uses its local SQLite file. Never connect clients to a SQLite file on a network share. Server setup: `scripts/setup_shop_server.ps1` (shipped with the installer); PC connection: Settings or the first-run screen (`app/services/shared_db.py`, `app/ui/dialogs/server_dialog.py`); guide: `docs/SHARED_SERVER.md`. The code is verified against PostgreSQL 16 in tests; the shop's own server is not installed yet, so do not claim it is live there.

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
- A sale has a quantity (default 1): it takes that many available units of the product out of stock (the picked unit first, then the oldest), and its prices are for the whole quantity (one unit's price × quantity). With no unit picked, the oldest available unit is taken; a typed IMEI/REF that matches no unit takes none. Asking for more units than are available is refused.
- A client can take several products in one visit: the New sale page (cash and installment) has a basket. Each product keeps its own type, price, plan and phone, and saving creates one sale per product for that client in one transaction (`sales.create_sales_batch`); two or more products share a `basket_id` (migration 0009). The Clients list shows one row per purchase: a sale bought on its own, or a whole basket on one row (owner's choice 2026-10-04); a client without sales still has one row; ticking a row selects the client.
- Adding a client (Clients page) only creates the client (owner's choice 2026-10-04: no "buy now" section); purchases are made on the New sale page.
- Client history ("ما اشتراه منا"): one row per sale, and under it every transaction for that product (down payment, full cash payment, each payment, amounts marked paid without payment rows) with the balance left after it (`customers.TransactionRecord`).
- Printed documents (`pdf_forms`): the A4 commitment form (تعهد والتزام, laid out like the shop's paper form: shop box, client fields, product, figures, commitment text and numbered terms, schedule, signature boxes) and the A5 sale and payment receipts. Their texts are owner-editable on the owner-only sidebar page "نماذج الطباعة" (`print_templates`, one JSON setting `print_templates`) with a live preview; commitment texts accept placeholders such as `{customer}`, `{financed}`, `{first_due}`. The wholesale price and profit are off the client's copy unless the owner turns them on. The cash sale receipt keeps the owner's earlier rule: logo, then the nine sale facts, no shop line, seller or signature. Latin product names, IMEI and REF are wrapped in direction marks so they keep their reading order; Qt does not mirror table cells, so RTL rows are written in reverse (`pdf_forms._tr`).
- The product form edits the quantity in stock (`stock.set_available_quantity`: adds units at catalog prices, removes the newest unsold ones, those with an IMEI last); the catalog table shows the available quantity. "Receive new goods" on the Products page records phones bought for the shop. Editing a product's prices also updates its unsold phones that still had the old catalog price.
- Debt book (owner only), separate from sales customers: a person (first name, last name, e-mail, national ID card number, phone) and a debt either owed to the shop (`receivable`, sidebar "ديون لي") or owed by the shop (`payable`, sidebar "ديون عليّ"): amount, reason, date, and repayment in one payment (optional due date) or by facility (months, payment every N months, split like an installment sale). Payments are recorded one by one and applied to the schedule oldest-first; status is overdue / due soon (7 days) / on track / paid, with the same grace days as sales.
- Dashboard net position (owner only): the headline is "my net money" = what clients still owe (installments + credit) + receivable debts − payable debts (cash in the drawer and stock not included); the stock at wholesale price is shown beside it, with the total including stock. Month cash flow: sales collections + debt repayments received − payments made on payable debts.
- Installment: sale, index 1..N, due date, amount due, amount paid, paid date, method (`cash`, `CCP`, `BaridiMob`), note.
- Payment: each amount received, linked to an installment; supports partial payments. A client may pay any amount up to the sale's remaining balance (e.g. 20,000 one month, nothing the next, 10,000 after): it is applied to unpaid installments oldest-first, one payment row per installment touched, sharing a `batch_id` so "undo last payment" removes the whole amount.
- Credit down payment means money already paid. Credit has no monthly schedule; payments reduce the remaining balance and an expected payment date is optional.

## Business rules

- `total = cash_price * (1 + rate / 100)`
- `financed = total - down_payment`
- Installment plans run 1 to 60 months. With payment interval `n`, payments fall at month offsets `n, 2n, …` and the last payment is always at `months` (5 months every 2 → offsets 2, 4, 5); `n` cannot exceed `months`.
- For installments, round each payment down to a whole dinar and add the remainder to the last payment so the schedule totals exactly `financed`.
- `profit = total - wholesale_price`
- `end_date = purchase_date + months` (Excel `EDATE` behavior).
- Cash sale: no schedule, rate 0, total = cash price, profit = cash price - wholesale price.
- Due-date setting `due_mode` defaults to `purchase_day`: installment k is due on the purchase day of purchase month + its offset (k × interval, capped at months), EDATE-clamped (bought 02/02/2026, 1 month → due 02/03/2026). `first_of_month` (the 1st of that month) is the alternate mode. Migration 0008 switched stored `first_of_month` settings and moved due dates generated on the 1st to the purchase day. `grace_days` defaults to 5.
- Status is evaluated for the selected month (current month by default): PAID when all installments due that month are fully paid; FAILED when any installment due that month or earlier is still unpaid after its due date plus grace days; PENDING when something is due that month and is not late; otherwise NONE. A past overdue installment keeps the customer FAILED even if the current month's installment is paid.
- Rate presets, editable by the owner (1 to 60 months, 0 to 100%): 4->35, 5->35, 6->35, 7->35, 10->40, 12->45.
- Status, alerts and reminders follow the stored due dates, so a client paying every N months is only due (and notified) in their payment months.
- Seller discount (owner's choice 2026-10-03: a percentage): a seller may type a lower price (or, on installments, a lower rate) down to `seller_max_discount` percent below the catalog total (default 5, set in Settings; installment totals use the product's rate or the preset for the months). Above that, the owner types their username and password (`auth.approve_as_owner`, counts toward the sign-in lockout) and the approval is written to the audit log (`discount_approved`). A blank price means the catalog price; the wholesale price always comes from the phone or catalog. The owner has no limit.
- Weekly WhatsApp reminders (Payments page): one row per client with everything due within 7 days (and, by default, late amounts) in one message. WhatsApp only opens drafted messages, so "send next" opens one client at a time; each opened reminder is logged (`whatsapp_reminder`) and clients reminded today start unticked.
- Barcode/IMEI scanning: the sale forms' scan box finds an unsold phone by IMEI or REF (`stock.find_by_scan`) and selects its product and unit; an unknown IMEI is written in the sale's IMEI field. "Receive goods" takes one scanned IMEI per line and adds one phone per IMEI (`stock.add_scanned_units`).
- Backups are always SQLite files; with the shop server they are copied from PostgreSQL, and restoring replaces every row on the server in one transaction.
- Deleting (owner only; list pages have row checkboxes, "select all" and "delete selected"): a customer with sales is deleted only when the owner explicitly chooses "with sales", which also deletes their sales, installments and payments and returns sold phones to stock. Deleting payments or sales writes an audit-log entry for each, and a `before_delete` safety backup is made first (the newest 5 are kept). Products used by sales and sold stock units are never deleted (archive instead).

## Code conventions

- Folders: `app/main.py`, `app/config.py`, `app/db/` (models, session, migrations), `app/services/` (calculation, schedule, status, auth, importer, backup, PDF, reports), `app/ui/` (main window, pages, dialogs, widgets), `app/resources/` (logo, icon, fonts, theme), `tests/`.
- Business logic belongs in services, never UI classes. Each service gets pytest tests.
- Type hints everywhere, small functions, docstrings in English, UI text in Arabic.
- After each implementation task: run pytest, list changed files, and provide a short manual test checklist.

## Owner decisions still needed

- Multi-PC deployment: on 2026-10-03 the owner chose the main shop PC as the PostgreSQL server (local network, no cloud). Still to do at the shop: run the setup script on the main PC, give it a fixed address, connect each PC.
- The owner confirmed the 5-day grace / overdue-status defaults and that a credit down payment is already paid. On 2026-10-02 the owner asked for due dates on the purchase day instead of the 1st of the month.
- On 2026-10-02 the owner asked to remove the Excel export buttons from the Clients and Payments pages.
- On 2026-10-03 the owner asked to remove every Excel export and the Excel import/template buttons on the Clients and Payments pages. The Reports page now shows profits (month, year, by month), the debt book, "my net money", the month's collection and the overdue list on screen, with no Excel files. The Import page and the Products page template/upload remain (imports, not exports); the export functions remain in the services for the backup/full-data use but have no buttons.
- The owner asked for flexible plans: any duration (1, 3, 5, … months), payment every N months, a default plan per product, and a different plan per client when needed.
- On 2026-10-04 the owner asked for products bought together on one client-list row, every transaction under each product in the client history, no purchase section when adding a client, and a redesigned, editable commitment form and receipt. The default commitment terms are suggested wording; the owner should review them on the "نماذج الطباعة" page.
- The brief's rounding rule (round down each month, remainder in last month) and optional credit expected date are current defaults; confirm if the owner wants different behavior.
- No extra customer categories were specified; start with the five defaults.
- Assume the wholesale dashboard card covers all sales, as stated in the blueprint.
