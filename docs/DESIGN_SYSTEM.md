# AkremMobile design system and desktop architecture

This document covers the visual language, right-to-left/left-to-right (RTL/LTR) handling, the application shell, the dashboard, sign-in, client types, background mode and notifications. Code references are relative to the repository root.

## 1. Principles

1. **Arabic first, never Arabic only.** Arabic is the default and runs right-to-left. English and French run left-to-right. No screen assumes a side.
2. **One source of truth per concern.** Colours live in `app/ui/theme.py` (`TOKENS`), styling lives in `app/resources/theme.qss`, text lives in `app/i18n/{ar,en,fr}.py`, and business rules live in `app/services/`.
3. **Act on what matters first.** Screens open on what the shop acts on most: what's late, what's due, and what came in today.
4. **Quiet confidence.** The interface is dark with one brand blue and status colours used only for status. A screen has one primary button.

## 2. Tokens

### Colour

| Token | Value | Use |
|---|---|---|
| `bg` | `#05070B` | Window background |
| `bg-raised` | `#080B12` | Sidebar, table headers, group boxes |
| `surface` / `surface-2` / `surface-3` | `#0E141D` / `#121A2C` / `#17223A` | Cards and inputs, hover, pressed |
| `border` / `border-strong` | `#1F2A3D` / `#2A3954` | Dividers, hover outlines |
| `primary` / `primary-hover` / `primary-pressed` | `#0758CD` / `#1667DD` / `#0549AD` | Primary actions |
| `primary-glow` | `#3B92D9` | Focus rings, active indicator, chart line |
| `primary-soft` | 18% `primary` | Selected nav item, selected rows, checked chips |
| `highlight` | `#9DBEFF` | Accents on dark (group titles, latest data point) |
| `silver` | `#C1C1C3` | Secondary body text, "expected" series |
| `text` / `text-muted` / `text-disabled` | `#EBF0FF` / `#8A94A6` / `#4A5568` | Text hierarchy |
| `paid` / `pending` / `failed` | `#22C55E` / `#F59E0B` / `#EF4444` | Status only. Each has a `*-soft` 14% tint for pills. |

Client-type swatches (`TYPE_COLOR_HEX`): blue, teal, green, amber, orange, rose, violet and slate. The database stores the *key*, not the hex value, so the palette can be retuned without a migration.

### Typography

- **Cairo** (bundled) for all UI text in Arabic and Latin scripts.
- **Rajdhani** (bundled) for the wordmark and large numbers (`kpiValue`, `moneyValue`, the clock, chart axes).
- Type scale: 24 (auth headline), 22 (page title), 16 (top-bar title), 15 (section title), 13 (body), 12 (labels, captions), 11 (meta), 10 (badges).
- Numbers always use Western digits and thousands separators, with the currency suffix from `ar.CURRENCY_SUFFIX`.

### Spacing, shape and elevation

- **Spacing scale (`SPACE`):** 4, 8, 12, 16, 20, 24 and 32 px. Pages use 28 px horizontal padding, and cards use 16–20 px inner padding.
- **Radii:**
  - 8 px: inputs and buttons
  - 12 px: cards (the brand rule)
  - 16 px: the auth card and palette
  - pill: chips and badges
- **Elevation:** a background and border step for most surfaces. Real shadows (`add_shadow`) are reserved for floating surfaces such as the auth card, because graphics effects are expensive on large widgets.

### Components (object names and properties in `theme.qss`)

- **Buttons:**
  - variants via `variant`: none for primary, plus `secondary`, `ghost`, `success`, `danger`, `warning` and `whatsapp`
  - `compact=true` for in-table actions
  - `QToolButton#iconButton` for square icon buttons
- **Chips:** a checkable `QPushButton` with `chip=true`, used for filter rows (client types, notification tabs).
- **Pills:** a `QLabel` with `pill = paid | pending | failed | info`.
- **Cards:**
  - `QFrame#card` and `#kpiCard`
  - `StatCard` (`app/ui/widgets/stat_card.py`) is the KPI card: icon chip, title, value, caption, and an accent bar painted on the reading-start edge.
- **Icons:** `app/ui/icons.py` holds 24×24 line icons authored for this project, rendered from SVG in any token colour, with no emoji in the shell.

## 3. RTL/LTR architecture

### Rules

1. **Direction is set once, on `QApplication`.** `app/ui/locale.py:apply_language()` sets it from the language. Widgets must not call `setLayoutDirection()`: it pins them and they stop following language changes. All the old hard-coded `RightToLeft` calls were removed.
2. **Layouts mirror themselves.** Place the sidebar first in a horizontal layout and it lands on the right in Arabic and on the left in English. Use `AlignLeading`/`AlignTrailing` in layouts.
3. **Labels align by interface direction, not content direction.** By default Qt aligns a label by its *text*, so `41,850` or a Latin username drifts left in Arabic. `LabelDirectionFilter` turns leading/trailing alignment into an absolute side for the current direction and re-applies it on direction change. Centred labels are untouched.
4. **Stylesheet facts (measured on Qt 6.11):**

   | Rule | Mirrored by Qt in RTL? | What we do |
   |---|---|---|
   | `padding-left/right`, `text-align` (buttons, labels, inputs) | Yes | Write in logical LTR terms |
   | `subcontrol-position` (combo arrow, group-box title) | Yes | Write in logical LTR terms |
   | `QComboBox` padding | **No** | Keep it symmetric |
   | `border-left/right` | **No** | Paint the edge in code, or use `@start`/`@end` |

   The shipped stylesheet is now direction-neutral: the only one-sided edge, the sidebar divider, is painted by `Sidebar.paintEvent`. Both directions therefore render the *same* stylesheet, and switching language never forces Qt to re-style every widget. This took a language switch from 4–10 s down to about 0.4 s.
5. **Painted things ask `isRightToLeft()`.** The nav active bar, KPI accent bar and toast stripe sit on the reading-start edge. The trend chart lays time out from the start edge, with the oldest month on the right in Arabic and the Y-axis on the start side. The gauge ring fills in the reading direction.
6. **Directional icons mirror.** `icons.MIRRORED` (`chevron-forward`, `chevron-back`, `exit`) are flipped horizontally in RTL. Non-directional icons (bell, search, clock) never flip.
7. **Text is looked up at use time.** `from app.i18n import ar` is a proxy for the active language. Never cache translated strings in module-level constants; use small functions such as `_headers()` instead.

### Switching language at runtime

1. The language is a **per-PC** preference stored in `QSettings`. The shop default in the database only seeds a new PC. One seller switching to French never changes another PC.
2. `change_language()` saves it, then `apply_language()` applies direction, theme and icon cache, then `set_language()`, which emits `events.language_changed`.
3. `MainWindow` re-translates the shell (sidebar, top bar, tray menu) and rebuilds the **visible page** immediately. Other pages are marked stale and rebuilt on first visit, so a switch stays well under half a second.

```python
# app/ui/locale.py (excerpt)
def apply_language(code: str) -> None:
    app = QApplication.instance()
    install_label_direction_filter(app)
    icons.clear_cache()
    apply_theme(app, get_layout_direction(code))   # direction + (unchanged) stylesheet
    set_language(code)                              # emits events.language_changed

# app/ui/main_window.py (excerpt)
def _rebuild_pages(self) -> None:
    current = max(self.pages.currentIndex(), 0)
    self._stale_pages = set(range(self.pages.count())) - {current}
    self._replace_page(current)
```

## 4. Application shell

```
Arabic (RTL)                                            English / French (LTR)
┌──────────────────────────────────────┬──────────┐    ┌──────────┬──────────────────────────────────────┐
│ lang · clock · 🔔 · ⌨ · [+ بيع] · search  title │ brand    │    │ brand    │ title  search · [+ Sale] · ⌨ · 🔔 · clock · lang │
├──────────────────────────────────────┤ ──────── │    │ ──────── ├──────────────────────────────────────┤
│                                      │ العمليات │    │Operations│                                      │
│              page                    │ ▌لوحة…   │    │▐Dashboard│              page                    │
│                                      │ الإدارة  │    │Management│                                      │
│                                      │ user card│    │user card │                                      │
└──────────────────────────────────────┴──────────┘    └──────────┴──────────────────────────────────────┘
```

**Sidebar (`app/ui/widgets/sidebar.py`):**

- Brand header, then two labelled groups: *Operations* (Dashboard, Customers, New sale, Payments) and *Management* (Import, Reports, Settings).
- Owner-only items are hidden for sellers.
- `NavButton` paints an active bar on the start edge and can show a count badge. The Payments item shows the number of late accounts.
- It collapses to a 72 px icon rail (remembered per PC) with tooltips.
- A user card shows initials, the real role and a lock button.

**Top bar (`app/ui/widgets/topbar.py`):** page title and description, quick search (Ctrl+K), *New sale*, shortcuts, notification bell with badge, clock with localized day and month names, and language picker.

## 5. Dashboard

The dashboard (`app/ui/pages/dashboard_page.py`) is ordered by how often the shop acts on each part:

1. **Header:** a greeting by time of day and the month picker.
2. **Operations status:** Paid, Pending and Failed (plus wholesale for the owner). Each card opens the customer list filtered to that status.
3. **Collection trend** (six months, expected vs collected) beside the **collection-rate ring**.
4. **Financial overview** (owner only): expected, collected, profit and remaining.
5. **Needs attention:** up to six of the most overdue accounts (installments *and* late credit). Clicking one opens the customer, and *View all* goes to Payments. Next to it is **Today's register**: collected today, sales today and, for the owner, profit today.

KPI rows use `ResponsiveGrid`: four columns on a wide window, 2×2 on a laptop, and one column when narrow. Every number comes from `services.dashboard` or `services.alerts`. The old hard-coded "+14.8%" and "yesterday 126,500" figures were removed.

## 6. Sign-in, first-run setup and lock

**Sign-in and setup (`app/ui/dialogs/auth_dialogs.py`)** share a two-panel window. A brand panel (logo, headline, three benefits) sits on the reading-start side and a form card on the other.

Sign-in flow:

1. **Idle:** the username field has focus. A language picker in the corner switches the language live, before signing in.
2. **Submit:** the button shows "Signing in…" and is disabled while the account is checked.
3. **Missing field:** the field is outlined red and a message appears inline.
4. **Wrong password:** an inline error banner appears, the card shakes slightly, the password is cleared and refocused, and the banner clears as soon as the user types.
5. **Locked out or disabled:** the specific inline message from the auth service is shown, including the lockout counters.
6. **Success:** the main window opens.

Inputs:

- The password field has a trailing show/hide eye. Qt places it on the correct side in each direction.
- A Caps Lock warning appears while the field has focus (on Windows).

**Lock screen (`ReauthenticationDialog`):**

- It shows "Session locked for {user}" with the user's avatar.
- Page content is hidden while locked, so customer data no longer shows behind the dialog.
- Esc and the window close button are ignored.
- A **Quit application** button means a user who is temporarily locked out is never stuck.
- If the idle timer fires while the app is hidden in the tray, the lock is deferred and shown on restore.

## 7. Client types

The data model extends `categories` (migration `0003_client_types`) with:

- `color`: a palette key
- `sort_order`
- `is_system`

There are four default types (أساتذة, منحة البطالة, عسكري, أعمال حرة) plus the protected system type **غير مصنف**, which receives unclassified and imported customers. The system type can be recoloured but not renamed or deleted.

Service API (`app/services/categories.py`):

- `list_client_types`
- `create_client_type`
- `update_client_type`
- `delete_client_type(..., reassign_to_id=)`
- `assign_client_type` (bulk)
- `reorder_client_types`
- `get_fallback_type`

Every write calls `auth.require_owner`. Errors are `ClientTypeError(code)` values that the UI translates. Names are unique after Arabic normalization, so "اساتذه" and "أساتذة" collide.

```python
with session_scope() as session:
    teachers = categories.create_client_type(session, owner_id, name="طلبة", color="teal")
    categories.assign_client_type(session, owner_id, [12, 15, 31], teachers.id)
    categories.delete_client_type(session, owner_id, old_type_id, reassign_to_id=teachers.id)
```

### Cash vs installment customers

A customer's *payment profile* is **cash** when every purchase was paid in full, and **facilities** when any purchase is paid over time (installment or credit). See `CustomerSummary.sale_types` and `CustomerSummary.payment_profile`.

- **Customers page:**
  - a *Payment* column shows one coloured tag per sale type (كاش green, بالتقسيط blue, كريدي amber)
  - the *Payment* filter: All / Cash (paid in full) / Installments or credit / Installments / Credit
  - the *Balance* filter: All / Still owes money / Fully paid
  - search also matches product names
  - a totals line shows the count, the amount due this month and the amount remaining for the filtered list
  - **Export list to Excel** saves exactly what is on screen
- **Dashboard:** a *Payment method* row shows cash customers and installment/credit customers (each opens the filtered list) and this month's sales split by type.
- **Reports page (owner):** **Export all data** writes one workbook with sheets for customers, sales, installments, payments and client types. It uses real Excel dates (`dd/mm/yyyy`) and grouped dinar amounts.

UI:

- **Customers page:**
  - a chip row (*All N* plus one coloured chip per type with its count) filters the list
  - the owner can multi-select rows (Ctrl/Shift) and use **Assign type**
  - the type column shows a coloured tag
- **Manage types** dialog: create, rename, recolour, reorder and delete. Deleting a type that is in use asks which type its customers move to.

## 8. Background mode, system tray and notifications

### Lifecycle

```
launch ─▶ single-instance check ──(already running)──▶ ask it to show itself, exit
            │
            ▼
        sign in ─▶ MainWindow.show() ─▶ CollectionMonitor.start()  (scan after 4 s, then every 10 min)
            │
   close ✕ ─┼─▶ minimize-to-tray on? ─yes─▶ hide window, keep timers running, first-time hint toast
            │                         └no──▶ backup checkpoint, quit
   tray ─────▶ Open · New sale · Notifications · Lock · [x] Minimize to tray · [x] Windows notifications · Quit
```

**Parts:**

- `app/ui/tray.py`: `TrayController` owns the `QSystemTrayIcon`, its menu, and the per-PC preferences. Activating the icon (click or double-click) restores the window.
- `app/ui/single_instance.py`: a per-user `QLocalServer` (a named pipe on Windows). A second launch activates the hidden window instead of opening a second copy against the same database.
- `app/ui/background.py`: `CollectionMonitor` runs `services.alerts.collection_alerts` on a `QThreadPool` worker, so the UI never blocks, and posts:
  - a **daily digest** once per day ("5 overdue installments totalling 120,000 DZD; 3 due within 7 days")
  - an alert whenever an installment **becomes** overdue while the app is running, de-duplicated by key

### Dual notification routing

Everything goes through `app/ui/notifications.py:NotificationHub.post()`:

| Source | In-app toast | Activity history (bell) | Native Windows toast |
|---|---|---|---|
| User action confirmations (`events.notify` success/info) | ✓ | – | never |
| User action warnings/errors | ✓ | ✓ | never |
| Daily digest / newly overdue | ✓ | ✓ | when the window is hidden, minimized or in the background |
| Automatic backup | ✓ | ✓ | never (routine) |

```python
hub.post("warning", ar.NOTIF_NEW_OVERDUE_TITLE, body,
         native=None,                 # None = OS toast only if the window isn't in front
         key="overdue:installment:42:overdue")   # never repeat the same event
```

**Notification center (bell):**

- The badge counts open collection alerts plus unread activity.
- Tabs: *All*, *Overdue*, *Upcoming* and *Activity*.
- Each alert card has **View customer** and **WhatsApp** (a drafted reminder via `services.reminders.whatsapp_link`).
- Opening the panel marks the activity history as read.

### Windows integration

- `QSystemTrayIcon.showMessage` is delivered by Windows 10/11 as a standard toast in the Action Center. No extra dependency is needed.
- `set_windows_app_id()` sets the process AppUserModelID `AkremMobile.InstallmentManager` before the first window, so toasts and the taskbar button say *AkremMobile* rather than *python.exe*.
- The Inno Setup Start-menu, desktop and optional *Start when I sign in* shortcuts carry the same AppUserModelID.
- Windows Focus Assist / Do Not Disturb can still hold toasts back. They then wait in the Action Center, and the in-app history keeps them either way.
- Toast text is summary-level (counts and totals, no customer names) because toasts can appear on a locked Windows screen.

## 9. Testing

- `tests/test_ui_shell.py` covers:
  - theme rendering, icon mirroring and label direction
  - sidebar states, notification routing and de-duplication
  - tray preferences, the digest and new-overdue logic, and single-instance activation
  - language switching (direction, lazy rebuild, per-PC storage), close-to-tray and the deferred lock
  - the client-type editor and filters
- `tests/test_client_types.py` and `tests/test_alerts.py` cover the services.
- `tests/test_owner_requirements.py` checks the owner's requirements end to end on a migrated SQLite file. It has one test per requirement:
  - the categories, plus 12 extra types
  - list columns
  - orange, red and green monthly status
  - the 7-field price table after clicking a name
  - dashboard wholesale total and operation counts
  - cash vs facilities
  - Excel upload and template
  - every Excel export
  - backup and restore, and the automatic backup
  - notification center and background monitor

### Manual checks on Windows

1. Close the window: it goes to the tray and the first-time hint appears. Leave it hidden past a grace deadline and a native toast appears. Click the toast and the notification center opens.
2. Launch the app again while it is in the tray: the existing window comes forward.
3. Switch language in the top bar: everything mirrors, numbers and names stay on the reading-start side, and the chart's time axis flips.
4. As owner, create, recolour, reorder and delete a type that is in use (with reassignment), then bulk-assign three customers.
5. As seller, *Manage types* and *Assign type* are not shown.

## 10. Known gaps and follow-ups

- **Older pages still carry inline styles and emoji labels:** New sale, Payments, Import, Reports, Settings, and the customer-details dialog. They follow the new direction and tokens through the global stylesheet. A follow-up should move their inline styles into `theme.qss`.
- **Not verified on real Windows yet:** native toasts and tray behaviour were checked only under Qt's headless platform. They need a run on Windows 10/11 before release.
- **CLAUDE.md is out of date:** it still says "UI is Arabic and the whole app is right-to-left" and lists five default categories. Update it to match this document once the owner confirms.
