# Demo accounts and end-to-end test walkthrough

The demo dataset is created with:

```powershell
.venv\Scripts\python.exe manage.py seed_demo --reset-demo-password
```

The command is safe to run again. It does not send SMS or email. Existing passwords are preserved unless
`--reset-demo-password` is supplied. The default demo password is `DemoPass!2026`.

Open `http://127.0.0.1:8000/login/` and use one account at a time. Log out before switching roles.

| Role | Username | Password | Linked record |
| --- | --- | --- | --- |
| Administrator | `demo_admin` | `DemoPass!2026` | ERP administrator |
| Principal | `demo_principal` | `DemoPass!2026` | Senior academic manager |
| Accountant | `demo_accountant` | `DemoPass!2026` | Finance and fee operator |
| Teacher | `demo_teacher` | `DemoPass!2026` | Employee `DEMO-T1`, Mathematics teacher |
| Staff | `demo_staff` | `DemoPass!2026` | Employee `DEMO-S1` |
| Student | `demo_student` | `DemoPass!2026` | Student `DEMO-001`, Class 1-A, roll 1 |
| Guardian | `demo_guardian` | `DemoPass!2026` | Guardian of `DEMO-001` |

These are regular ERP accounts, not Django superusers. The Django `/admin/` site is intentionally reserved for a
platform superuser. The seven accounts use the school-scoped screens under `/`.

## Seeded records to use during testing

- School: **School ERP Demo** (`demo-school`), academic year **2026**, class **Class 1**, section **A**.
- Students: `DEMO-001` through `DEMO-005`, with one primary guardian each.
- Employee records: `DEMO-T1` and `DEMO-S1`.
- Student attendance on **2026-09-21**: absent, late, present, present, present.
- Staff attendance on **2026-09-21** for both demo employees.
- Fee structure: **Tuition 1,000** for Class 1 and September invoices for all five students.
- Exam: **First Term**, Mathematics schedule, five entered marks (70-75), still in **Draft** status.
- One pending leave request for `DEMO-S1` on **2026-09-28** to **2026-09-29**.
- One student-visible download: **Demo mathematics study note**.
- One holiday and one fee-reminder SMS template.

## Administrator walkthrough

1. Sign in as `demo_admin` and confirm the dashboard at `/` shows students, employees, and fee totals.
2. Open `/settings/` and check the school profile, current academic year, and SMS switches. Save a harmless label
   change and restore it.
3. Open `/users/`; create a temporary user, assign one existing role, edit it, then deactivate it. Delete or leave
   it inactive after the test.
4. Open `/students/` and `/students/DEMO-001/`; inspect enrollment, guardian, attendance, fees, and documents.
   Use `/students/admit/` to admit a temporary student, then use `/students/<id>/edit/` and the leaving workflow.
5. Open `/employees/` and inspect `DEMO-T1` and `DEMO-S1`. Use `/employees/me/` to confirm the linked profile.
6. Open `/attendance/` and `/attendance/report/`; filter **2026-09-21** and confirm the five student statuses. Open
   `/attendance/staff/` and `/attendance/staff/report/` for employee attendance.
7. Open `/attendance/leave/`; review the pending `DEMO-S1` request. Approve it, verify the generated leave attendance,
   then withdraw it if you want to test the reverse workflow.
8. Open `/fees/` and `/fees/due/`; open a September invoice, record a partial cash payment, download its receipt,
   then reverse the payment. Test cancellation only on an unpaid invoice.
9. Open `/finance/`; inspect the dashboard and reports. Create a small journal entry, view the trial balance and
   income statement, then reverse the test entry.
10. Open `/exams/`, select **First Term**, and confirm the marks. Publish the exam only after checking the draft
    result. Then open the report card/result sheet and download a report card. Leave it published for student testing,
    or keep the exam in draft when repeating the seed data in a disposable database.
11. Open `/routine/`, `/downloads/`, `/sms/`, `/holidays/`, and `/reports/overview/` to verify administration screens.
    Compose an SMS only with a test recipient; the seed command itself never queues a message.

## Principal walkthrough

Sign in as `demo_principal`.

1. Review `/`, `/students/`, `/employees/`, `/attendance/report/`, and `/reports/overview/` for school-wide oversight.
2. Open `/exams/`, review the First Term marks, publish the exam, and inspect `/exams/results/` and the report-card
   links.
3. Open `/attendance/leave/`, approve or reject the pending staff request, and confirm the status and attendance row.
4. Check `/routine/`, `/downloads/`, `/holidays/`, and `/users/` (user visibility). Principal does not have finance
   posting, fee collection, school settings, or user-management write access.

## Accountant walkthrough

Sign in as `demo_accountant`.

1. Open `/fees/`, `/fees/due/`, and `/fees/reports/`; filter Class 1 and September.
2. Open a `DEMO-00x` invoice, collect a cash payment, download the receipt, then reverse that payment.
3. Run `/fees/generate/` for another month to verify idempotent invoice generation; running it twice should report
   skipped existing invoices rather than duplicate them.
4. Open `/finance/`, `/finance/reports/trial-balance/`, `/finance/reports/income/`, `/finance/reports/balance-sheet/`,
   and `/finance/reports/cash-book/`. Create and reverse a clearly labelled test journal entry.
5. Use `/students/`, `/employees/`, `/downloads/`, `/holidays/`, and `/sms/` for the read/collection support workflows.

## Teacher walkthrough

Sign in as `demo_teacher`.

1. Open `/students/`; confirm the Class 1-A roster and guardian details.
2. Open `/attendance/`, select Class 1-A and **2026-09-21**, then correct one attendance value and save. Reopen the
   report to verify it persisted.
3. Open `/exams/marks/`; select the First Term Mathematics schedule, edit a mark, save it, and verify the value in the
   result view. Use the unlock-request workflow if the exam has already been published.
4. Open `/routine/`, `/downloads/`, `/holidays/`, and `/employees/me/`.
5. Create a personal leave request at `/attendance/leave/new/`; sign in as Principal and approve/reject it, then sign
   back in as Teacher to verify the result.

## Staff walkthrough

Sign in as `demo_staff`.

1. Open `/employees/me/` and confirm the `DEMO-S1` profile.
2. Open `/attendance/staff/` and `/attendance/staff/report/` to view the staff attendance permitted to this role.
3. Submit a leave request at `/attendance/leave/new/`, then use Principal to review it.
4. Check `/routine/`, `/downloads/`, and `/holidays/`. Staff cannot edit students, fees, finance, marks, SMS, or
   school settings.

## Student walkthrough

Sign in as `demo_student`.

1. Open `/portal/` and confirm the Class 1-A profile for `DEMO-001`.
2. Open `/portal/attendance/?month=2026-09` and check the seeded 2026-09-21 attendance.
3. Open `/portal/fees/` and confirm the September tuition invoice and outstanding balance.
4. After the Administrator or Principal publishes First Term, open `/portal/results/` and view the result.
5. Open `/routine/`, `/downloads/`, and `/holidays/`; download **Demo mathematics study note** and verify it opens.

## Guardian walkthrough

Sign in as `demo_guardian`.

1. Open `/portal/` and confirm the linked child is `DEMO-001`.
2. Repeat `/portal/attendance/?month=2026-09`, `/portal/fees/`, and (after publication) `/portal/results/`.
3. Use `/routine/`, `/downloads/`, and `/holidays/`. Confirm the student-visible download is available.
4. Verify that student records, finance posting, staff data, SMS composition, and school settings return a permission
   response for this role.

## Regression pass after the manual walkthrough

From the repository root run:

```powershell
.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_browser.py
```

Use a disposable database or restore the draft exam before repeating the walkthrough. Re-running the seed command is
idempotent for the seeded records; it will not undo a payment, publication, approval, or other action you make while
testing.
