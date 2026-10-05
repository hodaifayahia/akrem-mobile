# مشاركة البيانات بين عدة أجهزة / Sharing the records between several PCs

## بالعربية

الجهاز الرئيسي في المحل يحفظ كل البيانات (خادم PostgreSQL)، والأجهزة الأخرى تتصل به عبر شبكة المحل (الراوتر/الويفي). لا حاجة للإنترنت، لكن **يجب أن يكون الجهاز الرئيسي مشغّلاً** حين تعمل الأجهزة الأخرى.

### 1. على الجهاز الرئيسي (مرة واحدة)

1. ثبّت AkremMobile كالعادة.
2. من قائمة ابدأ: **AkremMobile ‹ Make this PC the shop server**، ووافق على طلب صلاحيات المسؤول.
3. السكربت يثبّت PostgreSQL إن لم يكن مثبتاً، وينشئ القاعدة، ويفتح المنفذ 5432 لشبكة المحل فقط، ثم يكتب في النهاية:
   - **عنوان الجهاز الرئيسي** (مثل `192.168.1.10`)
   - **كلمة سر القاعدة** — اكتبها واحفظها في مكان آمن.
4. يُستحسن تثبيت عنوان هذا الجهاز في الراوتر (عنوان ثابت) حتى تجده الأجهزة الأخرى دائماً.
5. إن ظهر تنبيه أن الشبكة «عامة» (Public)، اجعلها «خاصة» (Private) من إعدادات ويندوز.

### 2. أول جهاز فيه بيانات المحل (عادة الجهاز الرئيسي نفسه)

افتح AkremMobile ‹ **الإعدادات ‹ مشاركة البيانات بين عدة أجهزة ‹ إعداد الاتصال بالخادم**:

- العنوان: `127.0.0.1` إذا كان هذا هو الجهاز الرئيسي، وإلا عنوانه.
- كلمة سر القاعدة.
- **اختبار الاتصال** ثم **حفظ واستعمال هذا الخادم**: البرنامج يعرض نقل بيانات هذا الجهاز إلى الخادم → اختر **نعم** (مرة واحدة فقط).
- البرنامج يُغلق؛ افتحه من جديد.

### 3. كل جهاز آخر

- جهاز جديد: في الشاشة الأولى اضغط **«جهاز إضافي في المحل؟ اتصل بخادم المحل»** (لا تنشئ حساب مالك جديد).
- جهاز مستعمل من قبل: الإعدادات ‹ مشاركة البيانات ‹ إعداد الاتصال بالخادم.
- اكتب العنوان وكلمة السر، اختبر، احفظ، وأعد فتح البرنامج. الحسابات (المالك والبائعون) هي نفسها على كل الأجهزة.

### إذا كان الجهاز الرئيسي مطفأً

عند فتح البرنامج على جهاز آخر تظهر رسالة «تعذر الاتصال بخادم المحل» مع: **إعادة المحاولة** / **إعداد الاتصال بالخادم** / **خروج**. البرنامج لا يعمل على بيانات محلية منفصلة من تلقاء نفسه حتى لا تنقسم بيانات المحل.

### النسخ الاحتياطية

النسخ اليومية تعمل كما كانت (ملفات `.sqlite3` في مجلد النسخ) وتُؤخذ من الخادم على كل جهاز، فتوجد نسخ على أكثر من جهاز. الاسترجاع من الإعدادات يستبدل كل بيانات الخادم بالنسخة المختارة (يؤثر على كل الأجهزة).

### العودة إلى جهاز واحد

الإعدادات ‹ مشاركة البيانات ‹ إعداد الاتصال بالخادم ‹ **العودة إلى بيانات هذا الجهاز**.

---

## English

The main shop PC keeps every record (a PostgreSQL server) and the other PCs connect to it over the shop network. No internet is needed, but **the main PC must be switched on** while the others work.

1. **Main PC, once:** install AkremMobile, then Start menu › AkremMobile › **Make this PC the shop server** (accept the administrator prompt). It installs PostgreSQL 16 if needed, creates the `akremmobile` database and `akrem` login, accepts only the shop's local network (`samenet`, password-checked), opens port 5432 for private networks, and prints the **address** and **database password**. Keep the password safe; give the PC a fixed address in the router.
2. **First PC that has the shop's records** (usually the main PC, address `127.0.0.1`): Settings › Share the records between several PCs › Set up the server connection › Test › Save. Answer **Yes** to move this PC's records to the server (once). The app closes; open it again.
3. **Every other PC:** on a new PC use "Another PC of the shop? Connect to the shop server" on the first screen (do not create a new owner); otherwise use Settings. Same address and password.

If the main PC is off, the app offers Retry / Server settings / Quit; it never silently falls back to local records. Daily backups keep working (SQLite files copied from the server, on every PC); restoring replaces every record on the server. "Back to this PC's records" returns a PC to its own local database.

Technical notes: the choice is stored per PC in `%APPDATA%/AkremMobile/local_settings.json` (`database_url`, including the database password); the `AKREMMOBILE_DATABASE_URL` environment variable still overrides it. Copying refuses to merge into a server that already has records. Tested end-to-end against PostgreSQL 16 (`tests/test_reminders_scan_discount_server.py`, set `AKREMMOBILE_TEST_POSTGRES_URL` to an empty test database).
