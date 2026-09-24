# School ERP

A school management system for schools in Bangladesh, built with Django 5.2 and Tailwind CSS.
It is made for English-medium schools teaching Cambridge, Pearson Edexcel and IB programmes, and
works equally for national-curriculum (Bangla-medium and English-version) classes: each class
follows the rules of its own programme.
It covers students, staff, both attendance registers, fees, double-entry accounts, exams and
results, the class routine, notices and downloads, SMS, the school calendar, settings and user
accounts — with a portal for students and guardians.

Every record carries a school foreign key, so a single-school deployment today can become a
hosted multi-school one without reshaping the database.

## What it does

| Module | What you can do |
|---|---|
| Students | One-screen admission (student, guardian and class together), roster with class/section/status filters, enrollment history, promotion, leaving records, CSV import with a preview step, ID cards, fee statements |
| Teachers & staff | Personal file with assignments, attendance, leave balance, documents and payslips; roster filters; salary hidden from roles that do not run payroll |
| Student attendance | Section register with mark-all, per-student history, daily "which registers are missing" summary, monthly grids and exports, optional absence SMS |
| Fees | Fee heads and class structures, concessions, monthly or whole-school invoice runs, one-off invoices, partial payments, receipts with the amount in words, reversals, outstanding-fee chasing, late fees, VAT per fee head (off unless set), online payment through the school's SSLCommerz account with gateway-confirmed receipts |
| Accounts | Multi-line journals, quick expense and income entry, account ledgers, trial balance, income and expenditure, balance sheet, cash book, opening balances, payroll, period lock |
| Staff attendance | Register with check-in and out, optional self check-in, leave requests that mark the register when approved, entitlement and overlap checks |
| Results | Rules per programme: the school's own, Bangladesh national (GPA, groups, 4th subject, combined papers), Cambridge and Edexcel grades, IB MYP criteria, IB Diploma estimates with the core and all eight conditions. Subject plans and per-student choices; papers in parts with weights, tier caps and presets; grid mark entry with absent and exempt; a pre-publish checklist; publication with frozen, versioned, fingerprinted results; comments, effort and approved predicted grades; combined term results; grade distribution, tabulation and merit exports; report cards that say they are the school's assessment; QR verification; promotion from results; a public lookup by result code (off by default) |
| Exam officer | Exam series for Cambridge, Pearson, the IB or a board; candidates, entries, options, tiers and HL/SL; an entries file with checks; access arrangements (managers only); official results imported from the statement of results, amended without losing history, and shown to families only after a second person confirms them; national board registration (eSIF) data with its gaps |
| Certificates | Transfer, leaving and character certificates in English or Bangla, numbered and frozen when issued, revoked or reissued with a trail, with a public QR check |
| Routine | The week as a grid per class or teacher, a bulk week editor with clash detection, free-teacher lookup, room and teacher-load reports |
| Notices & downloads | A noticeboard with audiences and expiry, and files behind the same audience rules |
| SMS | Templates rendered per recipient, preview with recipient count and SMS-part cost, batches with delivery counts, retries, a gateway test, and five optional event notifications |
| Calendar | Holidays and events, a month grid, working-day counts, national-holiday import, iCalendar export |
| Settings | A hub with a readiness check, one-press defaults, school profile, academic setup, notifications, policy and the audit log |
| Users | Create a login straight from a student, guardian or staff record, or for a whole class; temporary passwords that must be changed; role and activity filters |
| Language | Bangla and English: the school switches Bangla on, each person chooses in the header; the family portal, navigation and report card are translated |
| Privacy | Guardian consent per purpose (also given and withdrawn by families in the portal), identity numbers for managers only, logged views of restricted data, and erasure of former students' personal data after the school's retention period; see [docs/PRIVACY.md](docs/PRIVACY.md) |

Temporary passwords are shown once, on the page that creates them, and are not written to the
session or anywhere else on the server. The CSV of a bulk run is assembled in the browser from
that page. An account stores only a hash; the single place a readable password exists is a
credential SMS waiting in the queue, and that message's text is cleared as soon as delivery ends,
leaving the number, the outcome and the time for an audit.

Eight roles: Administrator, Principal, Vice Principal (the same access as a Principal), Accountant,
Teacher, Staff, Student, Guardian.
[docs/access-matrix.md](docs/access-matrix.md) lists every screen with the permission it
declares, and names the further limit where a screen applies one of its own — a teacher holding
`attendance.view_leaverequest` opens the leave list and sees only their own requests. It is
generated by `python manage.py role_matrix` from the URLs and the groups, and a test compares
the committed file with a fresh run, so it cannot drift from the code. A short, fixed list of
pages is reachable without signing in: a Public download, the health probe, the verification
pages for report cards, combined results and certificates, the public result lookup (off unless
the school turns it on), and the payment gateway's return, notification and demonstration pages.
A test asserts that list stays exactly that.

Access is by ownership wherever a blanket permission would be too much. A teacher reaches the
children currently on the roll of a section they currently teach, and their own staff file, but
not the staff roster: that carries every colleague's phone number, NID and qualification, and
belongs to the office. Historical records are authorised against the year they belong to, so a
teacher can still print last year's report cards for the section they taught last year.

## Local setup (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py setup_roles
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py seed_demo --admin-username YOUR_SUPERUSER
.\tools\tailwindcss.exe -i .\static\src\input.css -o .\static\css\app.css --minify
.\.venv\Scripts\python.exe manage.py runserver
```

Sign in at `/login/`. `seed_demo` is repeatable, attaches the named superuser to the demo
school, and sends nothing.

### Starting a real school instead

```powershell
.\.venv\Scripts\python.exe manage.py setup_school          # accounts, fee heads, grading, periods, templates
```

Then open **Basic Settings**. The readiness panel names anything still missing — current
academic year, classes, sections, subjects, fee heads, accounts, periods — and "Create
defaults" fills the rest in. Everything it creates is editable, and running it again changes
nothing already entered.

## Quality checks

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q --cov=. --cov-report=term:skip-covered
```

Browser tests need Chromium (`python -m playwright install chromium`) and are excluded from
the default run; add `-m browser` to include them. They sign in, walk the roster, admit a
student through the real form and check the mobile sidebar, writing screenshots to
`docs/screenshots/`. CI runs lint, the Django checks, migration
drift, the tests with a coverage floor, a check that the committed stylesheet matches a fresh
Tailwind build, and the deployment checklist.

## SMS

Nothing is sent inside a web request. Composing and every automatic notification only queue
messages; a worker delivers them:

```powershell
.\.venv\Scripts\python.exe manage.py process_sms --limit 100        # one pass, for a scheduler
.\.venv\Scripts\python.exe manage.py process_sms --loop --interval 30   # a standing worker
```

A message that keeps failing is retried three times and then left alone rather than texting a
family in a loop, waiting a minute, then five, then fifteen between attempts so a struggling
gateway is given room. Each message is claimed before sending, so two workers never send it
twice; a claim left behind by a worker that died is reclaimed after fifteen minutes. Exactly-once
delivery cannot be guaranteed on the provider's side without an idempotency key, which these
gateways do not offer: a worker killed between the gateway accepting a message and the answer
being recorded will retry it. The default backend prints to the log. For a real gateway set
`SMS_BACKEND=messaging.backends.HttpSMSBackend`, fill in the credentials under **Basic
Settings → SMS gateway**, and use **Gateway test** to send one real message before a batch
depends on it. Delivery means the gateway accepted the request; provider delivery receipts
are not integrated.

Automatic messages (absence, payment received, fee reminder, results published, admission)
are each off until switched on under **Notifications**, and a family can be opted out on
their guardian record.

## Deployment

```bash
cp .env.example .env     # then set DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec web python manage.py setup_roles
docker compose exec web python manage.py createsuperuser
```

The image builds the stylesheet with the standalone Tailwind CLI (no Node), collects static
files at build time and serves them through WhiteNoise. Compose runs PostgreSQL, the web app
and the SMS worker. `/healthz/` reports the database and cache for a load balancer.

Set at minimum: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`. With `DEBUG=0` the login rate limit uses a
shared cache (`createcachetable`, or `CACHE_URL` for Redis) so restarting a worker cannot
reset someone's failed-attempt count. Run `manage.py check --deploy` before release; it is
also asserted by the test suite.

Printed documents (report cards, admit cards, ID cards, receipts) go out on the school's
letterhead and can carry a student's Bangla name: Noto Sans Bengali is bundled under the
OFL in `static/fonts/`. Remove it and documents still print, in Helvetica, with Latin text
only.

Uploaded files stay private: photos, student and staff documents and downloads are all served
through permission-checked views, never from a public media URL.

### Running on SQLite (one school, such as a demonstration)

Leave `DATABASE_URL` unset and the system uses SQLite, set up for a web server: WAL
journalling, write transactions that queue instead of failing, and a 20-second wait when the
database is busy. For a server:

1. Set `SQLITE_PATH` to a file on persistent storage, outside the code folder (and, with
   Docker, on a mounted volume, or rebuilding the container deletes the database).
2. Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` and
   `DJANGO_CSRF_TRUSTED_ORIGINS`. Never run a server with the default `DJANGO_DEBUG=1`: it shows
   error details to anyone.
3. Run `migrate`, `createcachetable` (the login limit's shared cache), `collectstatic` and
   `setup_roles`. After upgrading from a version without analytics, run `rebuild_result_facts`
   once so exams published before then appear in the analytics; publishing keeps them current.
4. Run the SMS worker (`python manage.py process_sms --loop`) beside the web server, if SMS is on.
5. Schedule the backup (below) and keep a copy off the server.
6. For the demonstration gateway, set `ALLOW_DEMO_PAYMENTS=1`.

Behind a reverse proxy (needed for HTTPS), set `TRUSTED_PROXY_COUNT=1` so rate limits see each
visitor's own address rather than the proxy's.

The platform administrator (a superuser, created with `createsuperuser`) has a **Platform** page:
every school, the modules each has been given (homework, online admissions), and which school
to work in. Only a superuser can switch a module on; a school without it never sees it.

### Backups

```powershell
.\.venv\Scripts\python.exe manage.py backup              # database + media, keeps 14 runs
.\.venv\Scripts\python.exe manage.py backup --skip-media
.\.venv\Scripts\python.exe manage.py backup --copy-to E:\SchoolBackups   # and a second copy elsewhere
```

Each run writes a timestamped folder containing the database, a media archive and a
`RESTORE.txt` with the steps. Rehearse a restore on a spare machine before you need one.

On SQLite the database is taken with SQLite's own backup API: one consistent snapshot, safe
while people are working, checked with `PRAGMA integrity_check` before it counts. The folder
holds `database.sqlite3` (put it back by copying it to `SQLITE_PATH` with the application
stopped) and `database.sql`, the same snapshot as SQL.

A backup on the same disk as the database is lost with that disk. `--copy-to` puts a second
copy on a USB drive, a network share, or a folder that OneDrive or Google Drive keeps in sync,
and prunes old copies there as well.

Run it every night. On Windows, with Task Scheduler:

```powershell
schtasks /Create /TN "School ERP backup" /SC DAILY /ST 23:30 /RU SYSTEM `
  /TR "E:\school-erp\.venv\Scripts\python.exe E:\school-erp\manage.py backup --copy-to \\nas\school-backups"
```

On Linux, with cron (`crontab -e`):

```cron
30 23 * * * cd /srv/school-erp && .venv/bin/python manage.py backup --copy-to /mnt/offsite >> /var/log/school-erp-backup.log 2>&1
```

The task needs the same environment variables as the application (`SQLITE_PATH`,
`DJANGO_SECRET_KEY` and so on); set them for the account the task runs as. Check the log or
the backup folder after the first night.

A school given the homework module keeps the photos and files students hand in for a set time
after the year ends (six months unless its managers change it under Homework settings). Delete
the old ones every night, after the backup:

```powershell
schtasks /Create /TN "School ERP homework files" /SC DAILY /ST 23:50 /RU SYSTEM `
  /TR "E:\school-erp\.venv\Scripts\python.exe E:\school-erp\manage.py homework_purge_files"
```

```cron
50 23 * * * cd /srv/school-erp && .venv/bin/python manage.py homework_purge_files >> /var/log/school-erp-homework.log 2>&1
```

`--dry-run` counts the files without deleting them. Uploads are capped at 30 MB a request; set
the proxy in front to refuse larger bodies too (nginx `client_max_body_size 30m;`).

A school given online admissions keeps applications that did not lead to a place for a set time
after the round closes (six months unless its managers change it under Admissions settings), and
an enrolled child's application for 90 days. Clear them every night, after the backup:

```powershell
schtasks /Create /TN "School ERP admissions" /SC DAILY /ST 23:55 /RU SYSTEM `
  /TR "E:\school-erp\.venv\Scripts\python.exe E:\school-erp\manage.py admissions_purge"
```

```cron
55 23 * * * cd /srv/school-erp && .venv/bin/python manage.py admissions_purge >> /var/log/school-erp-admissions.log 2>&1
```

`--dry-run` counts the applications without clearing them. Families apply at
`/apply/<school slug>/`; link to it from the school's website.

A school that switches on the weekly homework digest (Basic Settings, Notifications) texts each
family once a week about homework not handed in. Queue it once a week, before the weekend; the
SMS worker sends it. Running it twice in one week sends nothing new.

```powershell
schtasks /Create /TN "School ERP homework digest" /SC WEEKLY /D THU /ST 16:00 /RU SYSTEM `
  /TR "E:\school-erp\.venv\Scripts\python.exe E:\school-erp\manage.py homework_digest"
```

```cron
0 16 * * 4 cd /srv/school-erp && .venv/bin/python manage.py homework_digest >> /var/log/school-erp-homework.log 2>&1
```

On PostgreSQL the dump runs `pg_dump` with connection arguments and the password in `PGPASSWORD`,
never in a connection URI: a URI puts the password in the process list for every user on the
machine to read, and has to be percent-encoded, so a password containing `@` or `/` would
silently point somewhere else. A `.pgpass` file works too; leave `PASSWORD` out of `DATABASE_URL`
and `pg_dump` will find it.

### Closing the books

Setting a lock date under **Settings → Policy** refuses every write on or before it: a manual
journal, a fee receipt, cancelling one, an opening balance, a salary and a salary reversal. The
rule lives on the model, so it holds whichever screen or script does the writing. Corrections
belong in an open period — reverse and re-post, rather than editing history.

A salary paid by mistake is undone from **Payroll**, not from the ledger. The reversal posts a
mirrored entry, leaves both on the record and returns the row to unpaid so it can be paid
correctly.

## Demonstration

`python manage.py seed_demo` builds a demonstration school with one login per role
(`demo_admin`, `demo_principal`, `demo_vice`, `demo_accountant`, `demo_teacher`, `demo_staff`,
`demo_student`, `demo_guardian`; password `DemoPass!2026` unless `--demo-password` is given).
Besides Class 1 with fees and attendance, it creates:

- **Year 10 (IGCSE)** under Cambridge rules: option subjects, a weighted Physics paper, comments,
  approved predicted grades and a published mock; exam series June 2027 with entries, and an
  official result from November 2025 already confirmed.
- **DP1 (IB Diploma)**: one student who meets the diploma conditions and one who does not.
- **Class 9 (national)** from the national plan: Science, Humanities and Business students,
  religion papers, 4th subjects and a creative/MCQ Bangla paper, with GPA.

`demo_guardian` has a child in Class 1 and one in Year 10, to show a family across classes.
Running the seed again changes nothing. To show online payment without moving money, set
`ALLOW_DEMO_PAYMENTS=1` on the server and choose the demonstration gateway under
Settings → Online payments. [docs/demo-role-walkthrough.md](docs/demo-role-walkthrough.md)
walks each role through the system.

### Before a school uses it for real

1. Print a report card, class sheet, admit card and certificate on the school's own printer.
2. Enter a past term's marks and compare the results with the ones the school published.
3. Replace the grade thresholds with the school's own internal ones.
4. Record guardian consent, set the retention period, and have counsel review hosting
   ([docs/PRIVACY.md](docs/PRIVACY.md)).
5. Never describe the system as certified by Cambridge, Pearson, the IB or a board.
6. For online payment, enter the SSLCommerz sandbox store details, run
   `python manage.py check_gateway`, and make one sandbox payment end to end; repeat the check
   after switching to the live store. Schedule `check_gateway --settle` every hour, so a payment
   whose return and notification were both lost still gets its receipt.

## Not built

Biometric attendance devices are not implemented; a device becomes a log import that feeds the
existing attendance service. A mobile app is likewise absent; the web UI is responsive and works
on a phone. Results do not cover IB PYP narrative reports, the IB Career-related Programme,
Edexcel modular UMS and cash-in, or moderation, and entries are never submitted to an awarding
body from here: the exam officer uploads the checked file to the body's own system.

[STATUS.md](docs/STATUS.md) records what was built and the decisions behind it;
[the original audit](docs/AUDIT_AND_UPDATE_PLAN.md) is the state the work started from.
