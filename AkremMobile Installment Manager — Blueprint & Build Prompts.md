# AkremMobile Installment Manager — Blueprint & Build Prompts

Sep 30, 2026 · @oasis

## Overview

Build a Windows desktop app in Python (PySide6 + SQLite) that replaces Akrem Abderrabou's Excel installment sheet and paper "تعهد و التزام" form for AkremMobile – Oum Thiour. The app tracks every customer, every sale (cash, installment, credit) and every monthly deduction, and shows at a glance who paid, who is pending and who failed this month.

The uploaded Excel sheet (التقسيط) holds about 90 sales rows with 17 columns. It is the source for the business rules in section 3 and the import feature in the prompt pack. Two things in it shape the design:

- The «التصنيف» column in Excel is the **sale type** (كاش / بالتقسيط / كريدي). The client's requested «تصنيفات» (teachers, unemployment grant, military, freelance) are **customer categories**. The app needs both, as separate fields.
- Excel has one row per sale and no record of individual monthly payments, so it cannot know who paid this month. The app adds a payment schedule (one row per due month), which is what makes the orange / red / green status possible.

The paper form adds fields Excel lacks: ID or driving-licence number and issue date, address, profession, CCP (postal account) number, and number of cheques. Its note says the deduction is always due at the start of the month.

## Client requirements (from the 8/9/2026 conversation)

These five features are the contract scope (20,000 DZD). Everything in section 4 is extra.

| # | Client said | Feature in the app |
| --- | --- | --- |
| R1 | قسم لاساتذة، قسم منحة البطالة، قسم العسكر، قسم اعمال حرة والكثير | Customer categories shown as tabs or a filter: Teachers, Unemployment grant, Military, Freelance, plus categories the owner can add, rename or delete. |
| R2 | الاسم اللقب، رقم الهاتف، تاريخ الشراء وتاريخ انتهاء الاقتطاع | Customer list columns: full name, phone, purchase date, end date of the monthly deduction. |
| R3 | الحالة هذا الشهر: قيد الانتظار (برتقالي)، فشلت (أحمر)، مكتمل (أخضر) | A status dot per customer for the current month: orange = due, not yet paid; red = due date passed and unpaid; green = paid. The user marks a month as paid with one click. |
| R4 | عند الضغط على الاسم تظهر نافذة مصغرة | Clicking a name opens a popup with: wholesale price, retail (cash) price, installment rate, total after installment, monthly deduction, number of months, total profit. |
| R5 | في الصفحة الأولى فوق | Dashboard cards at the top: total wholesale value of all goods sold, number of failed, completed and pending operations. |

To make R1–R5 usable, the scope also needs a form to add and edit a customer and a sale, and the "mark as paid" action. The contract counts these as part of R2 and R3.

## Business rules from the Excel formulas

The app must reproduce the sheet's math exactly, so imported numbers match what Akrem already sees. All amounts are in DZD.

| Field (Excel column) | Formula in the sheet | Meaning |
| --- | --- | --- |
| Total after installment (Q) | cash × rate / 100 + cash | Full price the customer pays |
| Sale price (G) | total − down payment | Amount left to deduct monthly |
| Monthly deduction (J) | G / months (empty for cash) | One month's payment |
| Profit (K) | G + down payment − wholesale | Same as total − wholesale |
| End date (M) | EDATE(purchase date, months) | Last deduction month |
| Total wholesale (N2) | SUM of wholesale column | Dashboard card R5 |
| Active monthly deductions (N14) | SUMIFS(J, end date ≥ today) | Money expected per month from live contracts |

```latex
\text{total} = \text{cash} \times \left(1 + \frac{\text{rate}}{100}\right), \qquad \text{monthly} = \frac{\text{total} - \text{down payment}}{\text{months}}, \qquad \text{profit} = \text{total} - \text{wholesale}
```

Rates and durations used in the sheet (installment rows): 6 months at 35% is the most common (22 sales), then 10 months at 40% (14 sales). 12 months uses 45–50%. The dropdowns allow rates 0–50% and durations 2–12 months. The app should offer these as a preset table the owner can edit (months → default rate).

Sale types found: 48 installment, 24 cash, 14 credit (كريدي), 2 blank. Credit rows use «الدفعة الأولية» for an amount still owed or already paid; this must be confirmed (section 8).

Some monthly values are not whole numbers (for example 23,166.67). The app should round each month to a whole dinar and put the difference on the last month, so the schedule always sums to the exact total.

## Recommended improvements and extra features

Login and Excel import are the two you asked for; build them first. The rest are ordered by value to the shop. None are in the 20,000 DZD contract, so quote them as add-ons.

| Priority | Feature | Why it matters for AkremMobile |
| --- | --- | --- |
| Must | Login with roles (owner, seller) | Hashed passwords, first-run owner setup, auto-lock after inactivity. Sellers cannot see wholesale prices or profit. |
| Must | Excel import | Upload the existing sheet, preview, map columns, validate, skip duplicates, then generate payment schedules. Moves 90 rows in minutes. |
| Must | Payment schedule per sale | One row per month with due date, amount, paid date, method (cash, CCP, BaridiMob). Enables R3 and history. |
| Must | Automatic daily backup | SQLite file copied to a backups folder, keep the last 30, one-click restore. Protects the shop's money records. |
| High | Print the «تعهد و التزام» form as PDF | Pre-filled with customer data, item, price, down payment and the 12-line deduction table, with the logo. Ends handwriting. |
| High | Full customer profile | ID or licence number, issue date and place, address, profession, CCP number, cheques count, guarantor, scanned ID photo. |
| High | Partial and late payments | Record 5,000 of a 7,000 month; show remaining balance and days late. |
| High | Reminders list | Customers due in the next 3 days and overdue ones, with a WhatsApp button that opens a pre-written message. |
| Medium | Excel and PDF export | Monthly collection report, overdue list, profit by month. |
| Medium | Product catalog | Saved wholesale and cash prices per model, so a sale form fills itself. |
| Medium | Charts | Profit per month, sales by category, collection rate. |
| Medium | Customer risk flag | Count of failed months; warn before selling again to a customer with 2 or more. |
| Low | Audit log | Who changed what and when, for disputes. |
| Low | Light theme and French UI | Arabic RTL stays the default. |

## Visual identity

The app follows the new logo: a dark, premium, "tech" look with chrome silver and electric-blue glow. Dark theme is the default; colors below were sampled from the logo file.

| Token | Hex | Use |
| --- | --- | --- |
| bg | #05070B | Window background (logo black) |
| surface | #0E141D | Cards, tables, sidebar |
| surface-2 | #121A2C | Hover rows, inputs |
| border | #1F2A3D | Card and table borders |
| primary | #0758CD | Buttons, active tab, "MOBILE" blue |
| primary-glow | #3B92D9 | Focus rings, 1px glowing card borders, selected row |
| highlight | #9DBEFF | Links, small accents |
| silver | #C1C1C3 | Headings, icons ("AKREM" silver) |
| text | #EBF0FF | Body text |
| text-muted | #8A94A6 | Labels, secondary text |
| status-pending | #F59E0B | Orange dot |
| status-failed | #EF4444 | Red dot |
| status-paid | #22C55E | Green dot |

Typography: **Cairo** (or Tajawal) for all Arabic text, bundled with the app; **Rajdhani** or **Orbitron** only for the "AKREM MOBILE" wordmark and big dashboard numbers. Numbers use Western digits with thousands separators (41,850 DZD).

Layout: right-to-left for the whole window. Sidebar on the right with the logo on top, then Dashboard, Customers, New sale, Payments, Import, Reports, Settings. Cards have 12px rounded corners and a thin blue glow border. The login screen is the full logo centered on black, with the login box below it.

The old logo on the paper form (grey infinity with "AkremMobile") should be replaced by the new one on printed forms and receipts.

## Tech stack and architecture

Use PySide6 (Qt 6) with SQLite. Qt has the best right-to-left and Arabic text support of the Python desktop toolkits, looks professional with a stylesheet, and prints Arabic PDFs correctly. CustomTkinter is simpler but handles Arabic alignment poorly; Flet looks modern but is heavier and less mature for printing.

| Need | Choice |
| --- | --- |
| UI | PySide6 + QSS theme, QtCharts |
| Database | SQLite via SQLAlchemy 2, Alembic migrations |
| Excel | pandas + openpyxl |
| Passwords | bcrypt |
| PDF (commitment form, receipts) | Qt QTextDocument + QPdfWriter |
| Tests | pytest |
| Installer | PyInstaller + Inno Setup (.exe setup) |

&#91;embedded content: App architecture · 3 layers\]

Each screen calls a service; only services talk to the database, so the pricing and status rules are written once and tested once.

Data model: Category 1—n Customer 1—n Sale 1—n Installment 1—n Payment, plus Setting, User and AuditLog tables. The Installment table is the key addition over the Excel sheet: it stores each month's due date and what was paid.

## Prompt pack

Run these prompts in order, one per session, in an AI coding assistant working inside the project folder (Claude Code is the best fit). Prompt 0 creates CLAUDE.md, which every later prompt relies on. Test each step before moving to the next. Tags show which prompts are in the 20,000 DZD contract \[CONTRACT\] and which are add-ons \[ADD-ON\].

### Prompt 0 — Project brief (CLAUDE.md) \[CONTRACT\]

```text
Create a file CLAUDE.md at the project root with the brief below, then reply only "Brief saved". Follow this brief in every future task.

# AkremMobile Installment Manager
Windows desktop app in Python for AkremMobile, a phone shop in Oum Thiour, Algeria (owner: Akrem Abderrabou). It tracks customers, sales (cash / installment / credit) and monthly installment payments, replacing an Excel sheet and a paper contract form.

## Stack (do not change without asking)
- Python 3.12, PySide6 (Qt 6) for the UI
- SQLAlchemy 2.x + SQLite, Alembic migrations
- pandas + openpyxl for Excel import/export
- bcrypt for passwords
- PDFs with Qt (QTextDocument + QPdfWriter) so Arabic shaping works
- pytest for tests, PyInstaller + Inno Setup for packaging

## Language and layout
- UI in Arabic, whole app right-to-left (Qt.RightToLeft). All UI strings in app/i18n/ar.py.
- Money stored as integers in dinars; shown as "41,850 دج". Never use float for stored money.
- Dates stored ISO, shown DD/MM/YYYY.
- Data folder: %APPDATA%/AkremMobile (database, backups, logs, exports).

## Theme
Dark by default. Tokens: bg #05070B, surface #0E141D, surface-2 #121A2C, border #1F2A3D, primary #0758CD, primary-glow #3B92D9, highlight #9DBEFF, silver #C1C1C3, text #EBF0FF, text-muted #8A94A6, pending #F59E0B, failed #EF4444, paid #22C55E. Font Cairo for Arabic (bundled), Rajdhani for the wordmark and big numbers. Cards: radius 12px, 1px primary-glow border. All styling in app/resources/theme.qss.

## Domain
- Category (customer category): أساتذة, منحة البطالة, عسكر, أعمال حرة, غير مصنف. Owner can add, rename, delete (delete only if unused).
- Customer: full name, phone, category, ID/licence number, ID issue date and place, address, profession, CCP account number, cheques count, notes.
- Sale: customer, product, sale type (كاش cash / بالتقسيط installment / كريدي credit), wholesale price, cash price, rate %, down payment, months, purchase date. Computed: total, financed, monthly, profit, end date.
- Installment (schedule row): sale, index 1..N, due date, amount due, amount paid, paid date, method (cash, CCP, BaridiMob), note.
- Payment: one record per money received, linked to an installment (supports partial payments).

## Business rules (must match the owner's Excel sheet)
- total = cash_price * (1 + rate / 100)
- financed = total - down_payment
- monthly = financed / months; each month rounded down to a whole dinar, remainder added to the last month, so the schedule sums exactly to financed
- profit = total - wholesale_price
- end_date = purchase_date + months (like Excel EDATE)
- Cash sale: no schedule, rate 0, total = cash_price, profit = cash_price - wholesale_price
- Due dates: setting due_mode = "first_of_month" (default: installment k due on the 1st of month purchase_month + k) or "purchase_day" (same day of month as purchase). Setting grace_days (default 5).
- Rate presets (editable setting, months -> rate %): 4->35, 5->35, 6->35, 7->35, 10->40, 12->45.

## Status rules (orange / red / green dots)
For a selected month (default: current month):
- PAID (green #22C55E): every installment due that month is fully paid.
- FAILED (red #EF4444): an installment due that month or earlier is not fully paid and today > due_date + grace_days.
- PENDING (orange #F59E0B): something is due that month, not paid, not yet late.
- NONE (grey, no dot): nothing due that month.
Dashboard counts operations = installments due in the selected month, by the same rules.

## Code conventions
- Folders: app/main.py, app/config.py, app/db/ (models, session, migrations), app/services/ (calc, schedule, status, auth, importer, backup, pdf, reports), app/ui/ (main_window, pages/, dialogs/, widgets/), app/resources/ (logo.png, icon.ico, fonts/, theme.qss), tests/.
- Business logic only in app/services, never inside UI classes. Every service gets pytest tests.
- Type hints everywhere, small functions, docstrings in English, UI text in Arabic.
- After each task: run pytest, list changed files, and give me a short manual test checklist.
```

### Prompt 1 — Project skeleton and theme \[CONTRACT\]

```text
Read CLAUDE.md. Create the project skeleton exactly as described there:
- requirements.txt with pinned versions, .gitignore, README.md with setup steps (venv, pip install, python -m app.main).
- app/main.py opens a main window: right-to-left, dark theme from theme.qss, Cairo font loaded from app/resources/fonts with QFontDatabase.
- Right-side sidebar: logo (app/resources/logo.png, I will add the file) on top, then buttons لوحة التحكم، الزبائن، بيع جديد، المدفوعات، استيراد Excel، التقارير، الإعدادات. Each switches a placeholder page in a QStackedWidget; active button highlighted in primary blue.
- Window title "AkremMobile — تسيير التقسيط", minimum size 1280x760, remembers size and position.
- Logging to %APPDATA%/AkremMobile/logs with rotation; uncaught exceptions shown in an Arabic error dialog and logged.
Acceptance: app starts with no errors, looks dark and RTL, Arabic text is correctly shaped, sidebar navigation works.
```

### Prompt 2 — Database and models \[CONTRACT\]

```text
Read CLAUDE.md. Implement the data layer:
- SQLAlchemy 2.x models: Category, Customer, Sale, Installment, Payment, Setting (key/value), User (for later). Money columns Integer. Foreign keys with ON DELETE rules, indexes on customer name, phone, due_date.
- Alembic set up with an initial migration; the app runs migrations automatically on start.
- Seed on first run: the 5 default categories, default settings (due_mode, grace_days, rate presets from CLAUDE.md).
- A session helper (context manager) used by all services.
- Repository functions for CRUD on categories, customers and sales.
Tests: create an in-memory database, seed, create a customer with 2 sales, delete rules behave as expected.
```

### Prompt 3 — Calculation, schedule and status engine \[CONTRACT\]

```text
Read CLAUDE.md. Implement app/services/calc.py, schedule.py and status.py as pure functions with full tests.
- calc.compute_sale(cash_price, rate, down_payment, months, wholesale, sale_type) -> total, financed, monthly_list, profit, end_date.
- schedule.build(sale, settings) -> list of installments with due dates (both due_mode options).
- status.for_month(installments, year, month, today, grace_days) -> PAID / FAILED / PENDING / NONE, and a per-customer status combining all active sales.
Use these real rows from the owner's Excel as test cases:
1) cash 62,800, rate 40, down 10,500, 10 months, wholesale 56,500 -> total 87,920, financed 77,420, monthly 7,742, profit 31,420.
2) cash 47,000, rate 35, down 12,300, 6 months, wholesale 41,500 -> total 63,450, financed 51,150, monthly 8,525, profit 21,950.
3) cash 145,000, rate 10, down 90,000, 3 months, wholesale 130,000 -> total 159,500, financed 69,500, months 23,166 / 23,166 / 23,168, profit 29,500.
4) cash sale 32,000, wholesale 29,900 -> profit 2,100, no schedule.
Also test: status on the due date, one day after grace, partial payment, and a customer with one paid and one late sale (must be FAILED).
```

### Prompt 4 — Login and users \[ADD-ON\]

```text
Read CLAUDE.md. Add authentication:
- On first run, a setup dialog creates the owner account (username, password twice, min 6 characters).
- Login window before the main window: full logo centered on black, username, password, show/hide password, دخول button. Passwords hashed with bcrypt.
- 5 wrong attempts lock login for 5 minutes. Auto-lock after N minutes of inactivity (setting, default 15) back to the login screen without losing unsaved forms.
- Roles: owner (everything) and seller (cannot see wholesale price, profit, dashboard wholesale card, settings, users, or undo payments). Enforce in services, not just by hiding widgets.
- Settings page section "المستخدمون" for the owner: add, disable, reset password. Every user can change their own password.
Tests for hashing, lockout and role checks.
```

### Prompt 5 — Dashboard (R5) \[CONTRACT\]

```text
Read CLAUDE.md. Build the dashboard page:
- Top row of 4 cards: إجمالي السلعة بسعر الجملة (sum of wholesale price of all sales ever recorded), العمليات الفاشلة (red), العمليات المكتملة (green), العمليات قيد الانتظار (orange). The three counts use status.py for the selected month.
- Each card: big number in Rajdhani, label in Cairo, colored dot, 1px glow border. Clicking a status card opens the Customers page filtered on that status.
- Month selector (default current month) above the cards.
- Second row (owner only, small cards): expected collections this month, collected this month, total profit of all sales, remaining balance to collect.
- Cards refresh automatically after any sale or payment change (use a Qt signal bus in app/ui/events.py).
```

### Prompt 6 — Customers list with categories and status dots (R1, R2, R3) \[CONTRACT\]

```text
Read CLAUDE.md. Build the Customers page:
- Category tabs at the top: الكل + one per category, each with a count. A small "+" to add a category; right-click a tab to rename or delete it.
- Table (QAbstractTableModel + QSortFilterProxyModel), one row per customer: status dot, full name, phone, category, product(s), purchase date, end date of deduction, monthly amount, remaining balance.
- Status dot painted by a custom delegate with the exact colors from CLAUDE.md, tooltip in Arabic (قيد الانتظار / فشلت / مكتمل).
- Search box (name or phone, Arabic-aware: ignore hamza and taa marbuta differences), status filter, sort by any column.
- Clicking a customer name opens the details popup (next prompt). Right-click menu: edit, change category, delete (with confirmation, owner only).
- Must stay fast with 5,000 customers.
```

### Prompt 7 — Customer details popup (R4) \[CONTRACT\]

```text
Read CLAUDE.md. Build the details dialog opened by clicking a name:
- Header: name, phone, category, status dot.
- If the customer has several sales, a selector at the top to switch between them.
- A clean 2-column table: سعر الجملة، سعر التجزئة (cash price)، نسبة التقسيط، الدفعة الأولى، المبلغ الإجمالي بعد التقسيط، الاقتطاع الشهري، عدد الأشهر، الربح الإجمالي، تاريخ الشراء، تاريخ انتهاء الاقتطاع. Wholesale and profit hidden for sellers.
- Below: the payment schedule, one row per month (month, due date, amount, paid, status dot) with a تسجيل الدفع button per unpaid row.
- Buttons: تعديل, إغلاق. Size about 720x620, centered, closes with Esc.
```

### Prompt 8 — Customer and sale forms \[CONTRACT\]

```text
Read CLAUDE.md. Build the "بيع جديد" page:
- Step 1, customer: search an existing customer or create one. Fields from CLAUDE.md; name and phone required. Phone must be 10 digits starting with 05, 06 or 07 (Algerian mobile); warn if the phone already exists.
- Step 2, sale: product, sale type (كاش / بالتقسيط / كريدي), wholesale price, cash price, down payment, months, rate, purchase date (default today).
- Choosing months auto-fills the rate from the presets; the user can still change it.
- Live preview panel updates on every keystroke using calc.py: total, financed, monthly, profit, end date, and the full schedule table.
- Cash hides months, rate and schedule. Credit shows amount owed and an optional expected pay date.
- Save creates the sale and its schedule in one transaction, then opens the details popup.
- Editing a sale later regenerates only unpaid installments; if payments exist, show a warning and require confirmation.
```

### Prompt 9 — Payments and monthly status \[CONTRACT\]

```text
Read CLAUDE.md. Build the "المدفوعات" page and the payment flow:
- Month selector; list of all installments due in that month grouped in three sections: قيد الانتظار (orange), فشلت (red), مكتمل (green), each with count and total amount.
- "تسجيل الدفع" dialog: amount (default = remaining due), date (default today), method (نقدا، CCP، BaridiMob), note. Partial amounts allowed; overpayment is refused.
- Undo last payment (owner only, asks for a reason, logged).
- Credit sales: record free payments until the balance reaches 0.
- After any payment: statuses, dashboard cards and customer list update instantly through the signal bus.
- A startup check recomputes statuses, so a customer turns red on its own when the grace period ends.
```

### Prompt 10 — Excel import \[ADD-ON\]

```text
Read CLAUDE.md. Build "استيراد Excel" to load the owner's existing sheet.
Source format (sheet التقسيط, header in row 1): الاسم واللقب, المنتج, التصنيف (this is SALE TYPE: كاش / بالتقسيط / كريدي), سعر الجملة, سعر الكاش, نسبة التقسيط, سعر البيع (note: header has a trailing space), الدفعة الأولية, عدد أشهر التقسيط, الاقتطاع الشهري, الفائدة, تاريخ شراء المنتج, تاريخ انتهاء الاقتطاع. Columns after that (N to Q) are summary formulas: ignore them.
Requirements:
1. File picker for .xlsx/.xls; read with openpyxl data_only=True so formula results are used. Match headers after trimming spaces; if a header is missing, show a column-mapping screen.
2. Clean data: trim names, normalize Arabic letters for matching, skip fully empty rows and rows without a name, flag placeholder names like "X".
3. The same customer name appearing on several rows = one customer with several sales. The sheet has no phone numbers: import with empty phone and flag them.
4. All imported customers get category غير مصنف; offer a bulk "assign category" screen after import.
5. Recompute every row with calc.py and compare with the sheet's سعر البيع, الاقتطاع الشهري and الفائدة; list mismatches and let the user keep the sheet values or the recomputed ones.
6. Preview table before import with a status per row: OK, warning, error (with the reason in Arabic). Rows with blank sale type need a choice.
7. Option (default ON): "mark installments due before today as paid", because the sheet does not track payments.
8. Duplicate protection: fingerprint = name + product + purchase date + cash price; re-importing the same file adds nothing.
9. Import runs in one transaction with a progress bar; at the end, a summary (imported / skipped / warnings) and an import report .xlsx saved in the exports folder.
10. A "download template" button that creates an empty .xlsx with the right headers and dropdowns.
Test with a fixture copy of the real sheet: 88 non-empty rows, 48 installment, 24 cash, 14 credit, 2 blank types.
```

### Prompt 11 — Print the commitment form (تعهد و التزام) \[ADD-ON\]

```text
Read CLAUDE.md. From the details popup, add "طباعة التعهد" which produces an A4 PDF that reproduces the shop's paper form, filled in:
- Title تعهد و التزام, the new logo, shop box: AkremMobile - Oum Thiour, بيع وتصليح الهواتف والحواسيب, كاش أو بالتقسيط, أكرم عبدربه.
- Fields: الهاتف, تاريخ الاستلام, عدد الصكوك, أنا الممضي أسفله, الحامل لـ ب.ت.و أو رخصة السياقة رقم, الصادرة بتاريخ / عن, الساكن بـ, المهنة, صاحب الحساب البريدي رقم, نوع السلعة, الثمن بالتقسيط, الدفعة الأولى.
- Table الاقتطاعات الشهرية with 12 rows (الرقم / الأشهر / الدفعات الشهرية): real months and amounts filled, unused rows struck through.
- Note: نعلم زبائننا الكرام أن الاقتطاع الشهري لأي زبون يكون دائما في أول الشهر و شكرا.
- Signature boxes: بصمة و إمضاء المعني, مصادقة البلدية, إمضاء و ختم البائع.
Print-friendly (white background, black text, logo in grayscale option). Build it with QTextDocument HTML + QPdfWriter so Arabic is shaped correctly. Buttons: open PDF, print directly. Also add a small payment receipt PDF.
```

### Prompt 12 — Reminders, WhatsApp and reports \[ADD-ON\]

```text
Read CLAUDE.md. Add:
- Reminders panel on the dashboard: customers due in the next 3 days and all overdue ones, with days late.
- WhatsApp button per row: opens https://wa.me/213XXXXXXXXX with a pre-written Arabic message (name, amount, due date). Message template editable in settings.
- Reports page: monthly collection report, overdue list, profit by month, sales by category, with filters by date range and category. Export each to .xlsx and PDF.
- Two simple charts (QtCharts): profit per month (bars), collection rate per month (line).
```

### Prompt 13 — Backup, restore and settings \[ADD-ON\]

```text
Read CLAUDE.md. Add:
- Automatic backup of the SQLite file on app start and close (safe copy with the sqlite3 backup API), keep the last 30, stored in %APPDATA%/AkremMobile/backups. Manual "نسخة احتياطية الآن" and "استرجاع" (owner only, with confirmation, restarts the app).
- Option to copy backups to a second folder (USB drive or Google Drive folder).
- Settings page: shop info (name, address, phone, logo), due_mode, grace_days, rate presets table, categories, auto-lock minutes, WhatsApp template.
- Audit log table: user, action, entity, before/after, timestamp; viewer for the owner.
```

### Prompt 14 — Packaging for Windows \[CONTRACT\]

```text
Read CLAUDE.md. Package the app:
- Convert app/resources/logo.png to icon.ico (16 to 256 px).
- PyInstaller spec, one-folder build, windowed, includes fonts, theme.qss, logo, Alembic migrations; version number in app/config.py shown in Settings > حول.
- Inno Setup script: Arabic installer, desktop shortcut, install to Program Files, keeps user data in %APPDATA% on uninstall.
- build.ps1 that runs tests, builds and produces AkremMobile-Setup-x.y.z.exe.
- Check on a clean Windows 10 and 11 machine without Python: install, first run, create data, reboot, data still there.
```

### Prompt 15 — Final QA pass \[CONTRACT\]

```text
Read CLAUDE.md. Do a full review before delivery:
- Run all tests; add missing tests for services with less than 80% coverage.
- Test with 5,000 fake customers (Faker, Arabic names): list loads under 1 second, search under 200 ms.
- Check every screen in RTL for clipped Arabic text, wrong alignment, untranslated strings.
- Edge cases: 0 down payment, 100% down payment, 1-month plan, sale on the 31st, leap year, deleting a category in use, two users editing (show a clear error).
- Produce a short Arabic user guide (docs/guide_ar.md) with screenshots placeholders.
List every issue found and fix them.
```

### Bug-fix prompt template

```text
Read CLAUDE.md. Bug: [what happened]. Steps: [1, 2, 3]. Expected: [what should happen]. Error/log: [paste]. Find the root cause, fix it, add a test that fails before the fix and passes after, and tell me what you changed.
```

## Open questions for Akrem

Ask these before Prompt 3; the answers go into CLAUDE.md.

- [ ] Due date: always the 1st of the month (as the paper form says) or the same day as the purchase (as the Excel end date suggests)? How many grace days before a customer turns red?
- [ ] If a customer missed last month but paid this month, should the dot be red or green?
- [ ] Credit (كريدي) sales: does «الدفعة الأولية» mean the amount already paid or the amount still owed? Is there a due date?
- [ ] Should monthly amounts be rounded (for example to the nearest 50 or 100 DZD)?
- [ ] Wholesale total card: all sales ever, or only sales still being paid?
- [ ] Which other categories after the four named (for example: retired, merchants)?
- [ ] Will more than one PC use the app at the same time? If yes, the database must move to a shared server (not in scope).
- [ ] Windows version on the shop PC (Windows 10 or 11, 64-bit)?
