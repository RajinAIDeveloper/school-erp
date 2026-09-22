# School ERP — Validation Audit and Update Plan

Audit date: 22 September 2026 · Working tree at commit `904ad96` ("astra update and fix 1") plus uncommitted changes (14 modified, 12 untracked files — `reports/`, `core/portal_*`, `templates/portal/`, `tests/test_roles_reports.py`, `tests/test_edge_regressions.py`, `README.md`, `.env.example`, `messaging/migrations/0003`).

This document is written for a coding agent (Codex) that will finish the system. Part A is the validation of what exists. Part B is the work plan: ordered work packages, each with exact files, behaviour, acceptance criteria and the tests to write. Part C is the operating contract the agent must follow.

---

## Part A — Validation

### A1. Baseline measurements (reproduced locally)

| Check | Result |
|---|---|
| `manage.py check` | clean |
| `manage.py makemigrations --check --dry-run` | no pending model changes |
| `manage.py check --deploy` (DEBUG=0) | 2 warnings: `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` unset |
| `pytest --ignore=tests/test_browser.py` | 148–149 passed (suite grew during the audit), 0 failed, ~50 s |
| Line coverage (`--cov=.`) | 87 % overall; lowest: `messaging/views.py` 55 %, `fees/views.py` 72 %, `students/views.py` 74 %, `examinations/views.py` 74 %, `finance/views.py` 77 % |
| Browser test (`tests/test_browser.py`) | not run: hard-codes `%LOCALAPPDATA%/ms-playwright/chromium-1243/...` |
| Tailwind | `static/css/app.css` built (41 KB) from `static/src/input.css`; rebuild is manual |
| Python code | 5.8 k lines across 14 apps; templates 1.8 k lines (≈40 % of template files are dead, see A4) |

### A2. Architecture verdict

The foundation is sound and should be kept:

- **Tenant isolation is enforced in four layers**: `SchoolScopedModel.school` FK + `clean()` cross-FK check (`core/models.py`), `SchoolModelForm` queryset narrowing (`core/forms.py`), `SchoolScopedMixin.get_queryset` / `require_permission` (`core/mixins.py`, `core/access.py`), and service-level `assert_school` / `assert_actor_school`. Tests prove cross-school reads/writes fail.
- **Roles** are Django Groups populated by `setup_roles` from `core/roles.py`; views declare `permission_required`; object scoping for teachers/parents goes through `core/access.students_for` / `sections_for`.
- **Money is correct**: fees post balanced double-entry journals (`FeePayment.post_to_ledger`, proportional allocation across income accounts), reversals instead of edits, `select_for_update` on the school row for serialisation, idempotent invoice generation (`one_invoice_per_enrollment_month` constraint).
- **Results are audit-grade**: versioned `ResultSnapshot`, DB triggers that block writes to published marks on SQLite and PostgreSQL (`examinations/migrations/0004`), 48-hour scoped unlock workflow, optimistic `Mark.version`.
- **Private media**: no `static(MEDIA_URL)` route; images go through `core/media.image`, documents/downloads through permission-checked views.
- **Login rate limiting**, CSRF, no superuser escalation from the user form, audit log on sensitive actions.

Structural debts that the plan fixes: a generic form mixin that contains exam-specific locking (`core/generic.py:69-83`), a queryset mixin that special-cases models by label (`core/mixins.py:44`), two coding styles in one repo (PEP-8 files vs. compressed one-liner files with no spaces), no linter/formatter config, no CI, and a large set of dead templates from an earlier iteration.

### A3. Module status (the 13 requested modules)

Legend: **Done** = usable end-to-end with tests · **Partial** = core flow works, listed gaps remain · **Thin** = screens exist but the module does not yet do its job.

| # | Module | Status | What works (verified) | What is missing (verified) |
|---|---|---|---|---|
| 1 | Students | Partial | List/search, create/edit, detail (enrollments, guardians, documents, invoices), guardian link, document upload/download, enrollment CRUD, promotion (history kept), CSV import (atomic), CSV/XLSX export, ID-card PDF, `next_student_id` | Admission is 3 separate screens (student → guardian → enrollment); no class/section/status filters on list; no transfer/withdraw workflow (status change only); no dependent class→section select; ID card and export are bare (3 columns); import ignores guardians/enrollment |
| 2 | Teachers & Staff | Partial | Employee CRUD (generic), departments, designations, subject-teacher assignments, class teacher on section | No employee detail page; no "create login" action; no per-employee attendance/leave/payroll summary; no documents; `basic_salary` visible to Principal |
| 3 | Student attendance | Partial | Section register (assigned sections only), holiday/weekend/future-date rejection, update_or_create, audit rows, monthly matrix + CSV/XLSX, parent portal history | No guardian absence SMS; no staff-side per-student history page; `monthly_matrix` runs one holiday query per cell (days × rows); register uses `<select>` per row with no "mark all present" (JS handler exists in `static/js/app.js` but nothing uses it) |
| 4 | Fees | Partial | Categories→income accounts, structures per year/class, concessions, monthly batch generation (one-time/yearly/quarterly rules), invoice detail, partial payments, receipt PDF, cancellation with ledger reversal, collection report | No ad-hoc/single invoice; `discount`/`late_fee` fields never editable; invoice cancel not exposed; no defaulter/due list; no student fee statement; no fee-due SMS; receipt PDF is a one-row table (no header, items, amount in words); list computes total/paid/balance with 3 queries per row |
| 5 | Accounts | Partial | Chart of accounts (default 21 heads), 2-line journal posting, reversal with reason, account balances report (date range), payroll prepare/pay/payslip | No multi-line journal; no account ledger/statement; no trial balance (Dr/Cr), income statement, balance sheet, cash book; no opening balances; payroll pays from Cash only and has no bulk monthly run; expense entry is only "post journal" |
| 6 | Staff attendance | Partial | Manager register with check-in/out, self view for staff, monthly matrix, leave request/approve/reject | Approved leave does not create `leave` attendance rows; no leave balance vs `LeaveType.days_per_year`; no overlap check; staff cannot self check-in |
| 7 | Results | Partial (strong core) | Grade scales + rule editor with gap/overlap validation, exams, schedules, assigned-only mark entry, publish with completeness check, snapshots, DB lock, unlock workflow, ranks, subject analysis, CSV/XLSX/PDF, report card (HTML/PDF), verification URL, parent/student view of published only | Mark entry is one form + one POST per student (no grid/bulk save); no admit card / exam routine print; report-card PDF has no school header, attendance, signatures; `verify` page unstyled; exams not linked to `Term`; no result-published SMS |
| 8 | Routine | Partial | Periods, rooms, slot CRUD, conflict validation (teacher/room/section/time overlap/break), class & teacher filter, CSV/XLSX/PDF | Routine is a flat table, not a week grid; no bulk slot entry; no printable per-class/per-teacher grid |
| 9 | Downloads | Partial | Categories, upload with audience + class filter, protected download, public audience, download counter | No text **Notices**; `/downloads/item/` (generic list) shows all items — including staff-only titles/descriptions — to students/guardians; no expiry |
| 10 | SMS | Thin | Compose to class/all guardians/staff/teachers/custom, BD number normalisation + dedupe, outbox, `process_sms` worker with claim, retry, console/HTTP backends, template CRUD | Templates are never used by compose (`SMSTemplate.render` unused); no automated triggers (absence, fee due, result published, admission); no batch list with counts; no scheduling story beyond OS cron |
| 11 | Holidays | Partial | CRUD, types, `closes_school`, weekend from school settings, iCal export, attendance exclusion | No month calendar view; no BD public-holiday preset |
| 12 | Basic settings | Partial | School profile, SMS gateway, audit log, academic years/terms/classes/sections/subjects/subject teachers/departments/designations/leave types (generic CRUD) | No delete/deactivate for master data anywhere; `core/views.switch_year` exists but is not routed; no settings landing page; grade scales live under Exams; no first-run bootstrap of default accounts/grade scale/fee categories/periods outside `seed_demo` |
| 13 | User management | Partial | List/search, create (password + roles), edit, profile, password change/reset (email), self-deactivation guard, no superuser escalation, rate-limited login | No "create login" from student/guardian/employee (two-step manual link); no admin-set temporary password + force change; no bulk account creation for a class; no last-login/linked-profile columns |

Cross-cutting: `reports` hub + management overview (new), student/guardian portal (new), dashboard is minimal (3 stat tiles + holidays; the richer `templates/core/dashboard.html` is orphaned).

### A4. Defects and risks found (fix list)

Severity: **H** = wrong behaviour or security/privacy exposure · **M** = broken UX or maintainability trap · **L** = cleanup.

| ID | Sev | Where | Finding | Fix |
|---|---|---|---|---|
| D1 | H | `downloads/urls.py` (generic `item_list`), `core/generic.py` | Students/guardians hold `downloads.view_downloaditem`, so `/downloads/item/` lists every item incl. `audience=staff` titles and descriptions. File itself stays protected. | Require `downloads.change_downloaditem` for the management list, or filter `get_queryset` by `visible_to`. Add test `test_student_cannot_list_staff_only_download_titles`. |
| D2 | M | `core/generic.py:47` (`extra_actions`) | Header buttons are not permission-filtered → Teacher sees "Promote", "Import CSV", "Grading", "Teaching assignments"; Principal sees "Generate invoices"; all lead to 403. | Accept `(label, url_name, permission)` triples and filter with `user.has_perm`. Test each role's list page contains no 403 link. |
| D3 | M | `core/views.py:81` | `switch_year` view is dead code (not in `core/urls.py`). | Route as `settings:year_switch` (POST) and add a "Make current" button, or delete. |
| D4 | M | `templates/portal/index.html` | Mojibake: literal `?` where `·` was intended ("{{ s.full_name }} ? {{ s.student_id }}"). | Replace with `·` and save the file as UTF-8. |
| D5 | M | `fees/views.py` `InvoiceListView`, `reports/views.py` `dues` | `total`/`paid`/`balance` are Python properties → 3 aggregate queries per invoice row; overview sums balances in Python. | Add `FeeInvoiceQuerySet.with_totals()` annotating `subtotal`, `paid`, `balance` via `Sum` subqueries; use in list, overview and portal. Test with `django_assert_num_queries`. |
| D6 | M | `attendance/services.py:monthly_matrix`, `holidays/models.py:is_holiday` | One DB query per (row × day). | Use `holiday_dates_between()` once and a set lookup. Test query count. |
| D7 | M | `core/generic.py:69-83` | Exam publication locking lives inside the generic `ERPFormMixin` via `obj._meta.label_lower == "examinations.exam"`. | Move into `examinations/forms.py` (`ExamForm.clean`, `ExamScheduleForm.clean`) and pass `form=` to `crud()`. |
| D8 | M | `core/mixins.py:44` | Generic mixin special-cases `students.enrollment` / `students.studentdocument` by label. | Give `SchoolScopedMixin` an overridable `scope_queryset(qs)` hook and implement it in the students views. |
| D9 | M | `messaging/views.py` compose | `SMSTemplate` unused; placeholders `{student} {amount} {school}` never rendered. | WP-09. |
| D10 | M | `tests/test_browser.py` | Hard-coded Chromium path; not part of default run. | Use `playwright install` default path; `pytest.importorskip`; mark `@pytest.mark.browser` and run in CI only when the browser is present. |
| D11 | L | dead code | Orphan templates (all extend a legacy `templates/base.html` and reference URL names that do not exist, e.g. `attendance:mark`, `exams:save_mark`): `templates/base.html`, `attendance/{register,monthly_report,student_history}.html`, `attendance/partials/counts.html`, `examination/**`, `fees/{fee_head_list,fee_structure,invoice_detail,invoice_list,receipt}.html`, `fees/partials/single_form.html`, `students/{student_list,student_form,student_detail,enrollment_form}.html`, `core/{dashboard,portal}.html` (superseded by `core/home.html` and `portal/`), `layouts/registration/login.html`. Also `core/templatetags/money.py` (duplicate of `erp.taka`), `core/context_processors.current_school`, `core/generic.ERPDeleteView`, `core/mixins.SuccessUrlNameMixin`. | Delete (or, for `core/dashboard.html`, re-use in WP-13). |
| D12 | L | `static/js/app.js` | `data-depends-on`, `data-mark-all` handlers exist but no template uses them; `EnrollmentForm` shows all sections regardless of class. | Wire `data-depends-on` + a `academics:sections_json` endpoint (WP-01); use `data-mark-all` in the register (WP-03). |
| D13 | L | repo hygiene | Two formatting styles; CRLF warnings; no `pyproject.toml`/ruff; no CI; no `.gitattributes`. | WP-00. |
| D14 | L | `config/settings.py` | HSTS sub-settings unset; rate-limit cache is LocMem (per-process, resets on restart). | WP-15: `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD`, DB/Redis cache when `DEBUG=0`. |
| D15 | L | `timetable/urls.py` | Students/guardians can open `/routine/slot/` (generic list) and see all sections' slots (non-personal data, but inconsistent with `routine()` scoping). | Require `timetable.change_routineslot` for the management list. |

No data-corruption or cross-tenant defects were found. The suite already proves tenant isolation, payment balancing, reversal idempotency, publication locking and role screen access.

### A5. Status by role — what each role can do today and what is missing

Access matrix derived by introspecting every URL's `permission_required` against the groups created by `setup_roles` (Y = allowed, · = 403, o = login-only with object scoping in the view).

| Screen (URL name) | Admin | Principal | Accountant | Teacher | Staff | Student | Guardian |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Dashboard `/` | o | o | o | o | o | o (portal) | o (portal) |
| Students list/detail/export | Y | Y | Y | Y (assigned) | · | o (self) | o (children) |
| Student create/edit/guardian/document/import/promote | Y | Y | · | · | · | · | · |
| Enrollment list / create-edit | Y/Y | Y/Y | Y/· | Y/· | · | · | · |
| Employees list / create-edit | Y/Y | Y/Y | Y/· | Y/· | · | · | · |
| Student attendance register / report | Y | Y | · | Y (assigned) | · | · | · |
| Staff attendance register / report | Y (edit) | Y (edit) | · | Y (view) | Y (own, view) | · | · |
| Leave list / request / review | Y/Y/Y | Y/Y/Y | · | Y/Y/· | Y/Y/· | · | · |
| Fees invoices / detail / collect / cancel | Y | Y (view) | Y | · | · | o (own) | o (children) |
| Fee generate / categories / structures / concessions | Y | · | Y | · | · | · | · |
| Accounts dashboard / journal / reverse / report | Y | Y (view) | Y | · | · | · | · |
| Payroll list/prepare/pay/payslip | Y | · | Y | · | · | · | · |
| Exams list/detail, marks, results, unlocks | Y | Y | · | Y (assigned) | · | · | · |
| Exam create/edit, schedules, scales, publish, unlock review | Y | Y | · | · | · | · | · |
| Report card | Y | Y | · | Y (assigned) | · | o (published) | o (published) |
| Routine view | Y | Y | · | Y | Y | Y | Y |
| Routine slots/periods/rooms manage | Y | Y | · | view | · | · | · |
| Downloads list / upload / manage | Y | Y | list | list+upload | list | list | list |
| SMS log / compose / retry | Y | Y | Y | · | · | · | · |
| SMS templates | Y | Y | · | · | · | · | · |
| Holidays list / manage / iCal | Y | Y | view | view | view | view | view |
| Basic settings: school profile, SMS, audit | Y | · | · | · | · | · | · |
| Academic setup (years, classes, sections, subjects, …) | Y | Y | view years/classes/sections | view | · | · | · |
| Users list / create / edit | Y | view | · | · | · | · | · |
| Reports hub / management overview | Y/Y | Y/Y | Y/Y | Y/· | Y/· | Y/· | Y/· |
| Portal (`/portal/*`) | · | · | · | · | · | Y | Y |

**Administrator** — Done: every module's screens. Missing: settings landing page, master-data delete/deactivate, current-year switch button, first-run bootstrap, notices, automated SMS, create-login shortcuts, rich dashboard.

**Principal** — Done: academic setup, people, attendance, exams and publication, routine, downloads, SMS, holidays, read-only finance/users. Missing (by design, keep): school identity, user creation, finance posting. Missing (to build): approval dashboards (pending leaves, pending unlocks), teacher-wise routine grid, class-wise attendance summary, admit cards, exam routine.

**Accountant** — Done: fee setup, invoicing, collection, reversal, journals, payroll, reports. Missing: ad-hoc invoices, invoice cancel, discount/late-fee editing, defaulter list, student statement, proper receipt, multi-line journal, ledgers/trial balance/P&L/cash book, payroll bulk run + pay method, fee-due SMS.

**Teacher** — Done: assigned sections' students, register, monthly report, assigned subject marks, results, unlock requests, routine, own leave, uploads. Missing: grid mark entry, mark-all-present, per-student attendance history, "my classes today" dashboard, pending-marks indicator. Bug: sees header buttons that 403 (D2).

**Staff** — Done: own attendance view, leave request, routine (empty unless teaching), downloads, holidays. Missing: leave balance view, own payslips, own profile page (employee detail). Note: linking `User`→`Employee` is manual; without it leave request has no employee choice.

**Student** — Done: portal (record, attendance by month + CSV, invoices, published report cards), routine, downloads, holidays. Missing: notices, routine as grid, attendance calendar, downloadable report card link on portal results list (exists via `examinations:report_card`), fee receipt list.

**Guardian** — Same as Student for each linked child, child switcher works, cross-child URL tampering returns 404 (tested). Missing: same as Student plus fee-due summary and SMS opt-in flag.

---

## Part B — Update plan (work packages)

Execute in order. Each WP is independently shippable and must leave `pytest`, `manage.py check` and `makemigrations --check` green. Estimated effort is in agent-hours of focused work, for sequencing only.

### WP-00 — Baseline hygiene (≈2 h) — do first

1. Commit the current working tree as-is (`git add -A && git commit -m "Reports hub, portal, edge tests, docs"`); nothing below starts on a dirty tree.
2. Add `pyproject.toml` with `[tool.ruff]` (`line-length = 120`, `select = ["E","F","I","B","DJ"]`, exclude migrations) and run `ruff format . && ruff check --fix .` in one commit so later diffs are readable. Add `ruff` to `requirements-dev.txt`.
3. Add `.gitattributes` (`* text=auto eol=lf`, `*.png binary`).
4. Delete dead code from D11. Keep `templates/core/dashboard.html` only if WP-13 reuses it.
5. Fix D3 (route `switch_year` as `path("year/<int:pk>/switch/", views.switch_year, name="year_switch")`, `@require_POST`), D4, D1, D2, D15, D7, D8, D12 (endpoint only — wiring is in WP-01).
6. Add `.github/workflows/ci.yml`: `pip install -r requirements-dev.txt`, `ruff check`, `manage.py check`, `makemigrations --check --dry-run`, `pytest -q --ignore=tests/test_browser.py`, and a Tailwind build step that fails if `static/css/app.css` is stale (`tailwindcss -i ... -o /tmp/app.css --minify && cmp`). Use the Linux standalone CLI in CI.
7. Fix D10: `test_browser.py` uses `p.chromium.launch()` without `executable_path`; skip when `playwright` or the browser is unavailable; mark with `@pytest.mark.browser` registered in `pytest.ini`.

Tests to add (`tests/test_hygiene.py`):
- `test_every_template_on_disk_is_referenced` — walk `templates/`, assert each file (except `registration/*`, `403.html`, `404.html`) is referenced by a `.py`/`.html` file.
- `test_list_header_actions_are_permission_filtered` — for each of Teacher, Accountant, Principal: GET `/students/`, `/employees/`, `/fees/`, `/exams/`; every `href` in the header actions block must return 200 for that client.
- `test_student_cannot_list_staff_only_download_titles` — create `audience=staff` item; student client GET `/downloads/item/` → 403 (or item title absent).
- `test_year_switch_sets_current_and_audits` — POST `settings:year_switch` as Admin → `is_current` moved; AuditLog row `academic_year.switched`; Teacher POST → 403.
- `test_portal_index_has_no_mojibake` — parent GET `/portal/` → `b"?"` not in the student card heading (assert `"·"` present).

### WP-01 — Student management completion (≈6 h)

Goal: one-screen admission, list filters, dependent class→section selects, transfer/withdraw workflow, better documents.

Build:
- `academics/views.py`: `sections_json(request)` → `[{id, name}]` for `?class_level=<pk>` limited to `request.school`; URL `academics:sections_json` under `settings/` or `/academics/sections/`. Templates for `EnrollmentForm`, `PromotionForm`, fee generate, mark filter, results filter, compose SMS: give the section `<select>` `data-depends-on="#id_class_level" data-url="/academics/sections/?class_level="`.
- `students/forms.py`: `AdmissionForm` = `StudentForm` fields + guardian block (`guardian_name`, `guardian_phone`, `guardian_relation`, `guardian_nid`, `guardian_email`, optional `existing_guardian` ModelChoiceField by phone search) + enrollment block (`academic_year` default current, `class_level`, `section`, `roll_number` default next free). `students/services.admit(school, user, data)` creates Student + Guardian (or reuses by phone within the school) + StudentGuardian(primary) + Enrollment in one `transaction.atomic`, audits `student.admitted`. View `students:admission` at `/students/admit/`; keep `students:create` as the plain form.
- `StudentListView`: filters `class_level` (via `enrollments__class_level` for current year), `section`, `status`, `gender`; columns add current class/section/roll and primary guardian phone; `select_related`/`prefetch_related` to avoid N+1; export gains the same columns + guardian + address + DOB.
- Transfer/withdraw: `students/views.change_status` (POST, `students.change_student`): sets `Student.status` to graduated/transferred/withdrawn, sets current `Enrollment.status="left"`, optional `reason`, audit `student.status_changed`; blocked if unpaid invoices exist unless `force=1` (Admin only). Button on detail page.
- Detail page tabs: Overview, Attendance (current year: present/absent counts + last 30 days), Fees (invoices with balance annotations), Results (published exams → report card links), Documents.
- ID card PDF: school logo/name, photo, ID, class/section/roll, session, guardian phone, ReportLab canvas at 86×54 mm.
- Import: preview step (parse → show first 20 rows + errors → confirm) using a session-stored token; columns extend to `guardian_name, guardian_phone, guardian_relation, class_level, section, roll_number`.

Tests (`tests/test_students.py`):
- `test_admission_creates_student_guardian_enrollment_atomically` — POST admission; assert 1 Student, 1 Guardian, 1 StudentGuardian(primary), 1 Enrollment; on duplicate roll the POST returns 200 with error and creates nothing.
- `test_admission_reuses_guardian_by_phone_within_school_only` — same phone in same school → reused; same phone in other school → new row.
- `test_sections_json_is_school_scoped` — foreign class → `[]`.
- `test_student_list_filters_by_class_section_status` — 3 students; `?class_level=…&status=active` shows only matching.
- `test_withdraw_sets_enrollment_left_and_blocks_on_dues` — invoice unpaid → 200 with error; `force=1` as Admin → 302 and statuses changed; Teacher POST → 403.
- `test_import_preview_then_confirm` — bad row → preview lists "Row 3"; valid file → confirm creates rows incl. guardian and enrollment.
- `test_id_card_pdf_contains_school_and_student` — response starts `%PDF`; use `pypdf`-free check by asserting Content-Type and length > 1 KB (avoid new deps).
- `test_student_list_query_count` — `django_assert_max_num_queries(12)` for 25 students.

### WP-02 — Teachers & staff completion (≈4 h)

Build:
- `employees/views.py`: `EmployeeListView` (custom, replaces generic) with filters `employee_type`, `department`, `designation`, `status`; hide `basic_salary` column unless `finance.view_payroll`.
- `employees:detail` `/employees/<pk>/`: profile card, this month's attendance summary, leave balance table (WP-06 provides balances), subject/section assignments (`SubjectTeacher`, class-teacher of), routine link, payroll history (if `finance.view_payroll`), documents.
- `EmployeeDocument` model (`employee`, `title`, `file`, `uploaded_by`) + upload/download views mirroring students' (`employees.add_employeedocument`).
- `EmployeeForm`: hide `basic_salary` unless user has `finance.change_payroll`; `user` field replaced by the WP-12 "create login" action.
- Self-service: `/employees/me/` redirects to own detail (Staff/Teacher with linked employee); 404 with helpful message otherwise.

Tests (`tests/test_employees.py`):
- `test_employee_detail_shows_assignments_and_hides_salary_for_principal`.
- `test_employee_document_private_to_school_and_permission` (Teacher of same school 403 on other's document unless manager; other school 404).
- `test_employee_me_redirects_to_own_detail` and `test_employee_me_without_profile_is_404`.
- `test_employee_list_filters`.

### WP-03 — Student attendance completion (≈5 h)

Build:
- Register UX: radio group per row (`present/absent/late/leave/half_day`) with `data-mark-all="present"` / `absent` buttons; keep the same POST contract (`<pk>-status`).
- `attendance:student_history` `/attendance/student/<pk>/`: per-student month grid + totals (reuses `monthly_matrix` for one enrollment); linked from student detail; scoped by `students_for`.
- Daily class summary `attendance:daily_summary` `/attendance/summary/?date=`: per section present/absent/late counts, "not taken" flag; CSV/XLSX.
- Perf (D6): `monthly_matrix` takes `holidays = holiday_dates_between(...)` + weekend set once.
- Absence notification: `School.notify_absence_sms` (bool, default False) + `School.absence_sms_template` FK→`SMSTemplate` (nullable). After `save_register` for students, `attendance/services.queue_absence_alerts(school, day, entries)` queues one SMS per absent student's primary guardian using `SMSTemplate.render(student=, class=, date=, school=)`; idempotent per (enrollment, date) via `SMSMessage.dedupe_key` (new nullable CharField, unique per school when set). Never sends synchronously; `process_sms` delivers.

Tests (`tests/test_attendance.py`):
- `test_mark_all_present_posts_every_row` (client POST with all rows present → count == roster).
- `test_student_history_scoped_to_teacher_sections` (other section → 404).
- `test_daily_summary_counts_and_not_taken_flag`.
- `test_monthly_matrix_query_count_is_constant` — 30 days × 20 students ≤ 6 queries.
- `test_absence_alert_queued_once_per_student_day_and_only_when_enabled` — toggle off → 0 messages; on → 1 queued per absent student, rerun → still 1; message body rendered with student name.

### WP-04 — Fee management completion (≈7 h)

Build:
- `FeeInvoiceQuerySet.with_totals()` (D5) and use everywhere; keep model properties for single objects.
- Ad-hoc invoice: `fees:invoice_create` form (student → enrollment auto, `month` optional, `due_date`, formset of items: category, description, amount; `discount`, `late_fee`, notes). Service `fees/services.create_invoice(...)`. Uniqueness on (`enrollment`,`month`) still applies.
- Invoice edit before any payment: change `discount`, `late_fee`, `due_date`, items; blocked once a non-cancelled payment exists (`ValidationError`). Cancel invoice (`fees:invoice_cancel`, POST with reason): only if no non-cancelled payments; sets `status=cancelled`; audit.
- Bulk generation for **all classes** of a year/month (`class_level` optional in `GenerateInvoicesForm`); returns per-class counts.
- Due/defaulter report `fees:due_list` `/fees/due/?class_level=&section=&as_of=`: rows per student with outstanding total, overdue days, guardian phone; CSV/XLSX/PDF; button "Send reminder SMS" → queues via WP-09 template `fee_reminder` for selected rows (checkbox) or all.
- Student fee statement `fees:statement` `/fees/statement/<student_pk>/?year=`: invoices, payments, running balance; PDF with school header; accessible to Accountant/Admin/Principal and to the student/guardian for their own (`students_for`).
- Receipt PDF redesign in `fees/receipt.py`: school logo/name/address, receipt no/date, student/class/roll, invoice items table, discount/late fee, amount paid, method/reference, balance after payment, amount in words (`core/utils.amount_in_words` for BDT, supports lakh/crore), "Received by", duplicate watermark when `?copy=1`.
- Collection report gains group-by: method, category (from journal lines), day; totals row.
- Late fee policy (simple): `School.late_fee_per_day` Decimal default 0; `fees:apply_late_fees` (POST, Admin/Accountant) sets `late_fee = min(days_overdue × rate, cap)` on unpaid invoices; idempotent; audited.

Tests (`tests/test_fees.py`):
- `test_invoice_list_totals_are_annotated_and_query_count_small` (25 invoices ≤ 8 queries; annotated `balance` equals property).
- `test_adhoc_invoice_create_and_edit_locked_after_payment`.
- `test_invoice_cancel_requires_no_payments_and_audits`.
- `test_generate_all_classes_returns_per_class_counts`.
- `test_due_list_filters_and_reminder_queue` (2 unpaid, 1 paid → 2 rows; POST reminder → 2 queued SMS with amount rendered; rerun same day → no duplicates).
- `test_statement_running_balance_and_parent_scope` (parent sees own child, 404 other).
- `test_receipt_pdf_has_header_and_amount_in_words` — assert `amount_in_words(Decimal("150000.50")) == "One Lakh Fifty Thousand Taka and Fifty Paisa Only"`; PDF starts `%PDF`.
- `test_apply_late_fees_idempotent_and_capped`.

### WP-05 — Accounts completion (≈7 h)

Build:
- Multi-line journal: `finance:journal_create` uses an inline formset (`JournalLine`: account, debit, credit, description; ≥2 lines; Σdebit == Σcredit > 0; one side per line) — service `finance/services.post_journal(school, user, date, narration, reference, lines, source)`; keep the 2-line quick form as `finance:quick_entry` with presets **Expense** (Dr expense / Cr cash-bank) and **Other income** (Dr cash-bank / Cr income).
- Account ledger `finance:ledger` `/finance/ledger/<account_pk>/?start=&end=`: opening balance, rows (date, JE no, narration, Dr, Cr, running balance), closing; CSV/XLSX/PDF.
- Trial balance `finance:trial_balance` (Dr/Cr columns, totals equal), Income statement `finance:income_statement` (income vs expense for a range, net), Balance sheet `finance:balance_sheet` (assets = liabilities + equity + retained net), Cash book `finance:cash_book` (all `is_cash` accounts, day-wise in/out/balance). All in `finance/reports.py` returning plain dicts so views and tests share them.
- Opening balances: `finance:opening_balances` form (per account amount as of a date) → posts one balanced entry against `3010` with `source="opening"` (add to `Source` choices + migration).
- Payroll: `payroll_pay` accepts `method` (cash/bank/mobile → 1010/1020/1030) and `paid_on`; bulk `finance:payroll_generate` for a month creates rows for all active employees from `Employee.basic_salary` (skip existing); payslip PDF gets school header.
- Period lock (simple): `School.books_locked_until` DateField null; `post_journal`/`record_simple_entry`/`reverse` reject dates ≤ lock date with `ValidationError`; Admin sets it in Basic settings; audit.

Tests (`tests/test_finance.py`):
- `test_multiline_journal_must_balance_and_rejects_two_sided_line`.
- `test_ledger_running_balance_and_opening_balance_before_range`.
- `test_trial_balance_totals_equal_after_fees_payroll_and_reversal`.
- `test_income_statement_net_matches_account_balances`.
- `test_balance_sheet_balances_with_opening_entries`.
- `test_cash_book_includes_all_cash_accounts_by_day`.
- `test_payroll_generate_skips_existing_and_uses_basic_salary`; `test_payroll_pay_by_bank_debits_1020`.
- `test_period_lock_blocks_backdated_posting_and_reversal`.

### WP-06 — Staff attendance & leave completion (≈4 h)

Build:
- Leave approval side-effects: on `approved`, create/overwrite `StaffAttendance(status="leave")` for each non-holiday day in range (`attendance/services.apply_leave`); on later rejection/cancel of an approved leave, remove only rows it created (track `LeaveRequest.attendance_rows` M2M or `created_by_leave` FK on `StaffAttendance`).
- Leave balance: `attendance/services.leave_balance(employee, year)` → per `LeaveType`: entitlement (`days_per_year`), used (approved days in year), remaining; `LeaveForm.clean` rejects overlap with existing pending/approved requests and remaining < requested (unless leave type `allow_negative`, new bool).
- Staff self check-in (optional policy): `School.staff_self_checkin` bool; when on, `/attendance/staff/checkin/` POST records today's `present` with `check_in=now` for the caller's employee; second POST sets `check_out`; audited.
- Leave list gains filters (status, employee, month) and a "pending" badge count in nav for managers.

Tests (`tests/test_leave.py`):
- `test_approved_leave_creates_attendance_rows_excluding_holidays_and_rollback_on_reject`.
- `test_leave_overlap_and_balance_enforced`.
- `test_self_checkin_requires_policy_and_records_in_out`.
- `test_leave_balance_math` (entitlement 10, approved 3 → remaining 7).

### WP-07 — Result management completion (≈6 h)

Build:
- Grid mark entry: `examinations:marks` renders **one form** with one row per student (score, absent, hidden `expected_version`, hidden enrollment pk); `examinations:save_marks` (POST) validates every row first, then saves all in one `transaction.atomic` via `save_mark` per row; on any row error, re-render with per-row errors and nothing saved. Keep single-row endpoint for HTMX later.
- Bulk absent toggle and keyboard-friendly tab order; show class average/entered count live from `analyse`.
- Admit card `examinations:admit_cards` `/exams/<pk>/admit-cards/?class_level=&section=` (PDF, one card per student: school header, exam, student, class/roll, schedule table) and exam routine `examinations:exam_routine` `/exams/<pk>/routine/` (HTML + PDF).
- Report card PDF v2 (`examinations/report_card_pdf.py`): school header + logo, student block, subject table (full/pass/obtained/letter/GP), total/percent/GPA/rank/result, attendance % for the exam's year (from `StudentAttendance`), remarks, signature lines, verification URL text; Bangla-safe font: bundle a Unicode TTF (e.g. Noto Sans Bengali, OFL) under `static/fonts/` and register it in ReportLab for `name_bn`.
- Bulk report cards `examinations:report_cards` (one PDF for a section).
- `Exam.term` FK→`Term` (nullable); results filter by term; progress report across exams of a year (`examinations:progress` per student: GPA per exam).
- Styled `verify` page (extends base layout, no login required; shows school, exam name, version, state — still no marks).
- Result-published SMS: on `publish_exam`, if `School.notify_results_sms` on, queue one SMS per guardian using template `result_published` (`{student} {exam} {gpa}`), deduped by (exam, enrollment, version).

Tests (`tests/test_results.py`):
- `test_grid_save_is_atomic_and_reports_row_errors` (one invalid score → 200, 0 marks saved; all valid → all saved, versions = 1).
- `test_grid_save_respects_expected_version_conflict`.
- `test_admit_cards_and_exam_routine_pdf_render`.
- `test_report_card_pdf_v2_includes_attendance_and_bangla_font_registered`.
- `test_bulk_report_cards_only_for_published_or_manager`.
- `test_result_sms_queued_once_per_publication_version`.
- `test_progress_report_lists_published_exams_only_for_parent`.

### WP-08 — Routine completion (≈4 h)

Build:
- Week grid: `timetable:routine` renders days × periods for a section (cells: subject / teacher / room) and a teacher grid (`?teacher=`); breaks rendered as spanning rows; print stylesheet; PDF grid via ReportLab table.
- Bulk editor `timetable:grid_edit` `/routine/edit/?section=&year=`: one form with a select per (day, period) cell for subject, teacher, room; POST validates every slot with `RoutineSlot.full_clean()` and reports conflicts per cell; saves atomically.
- Teacher free-period finder `timetable:free_teachers?weekday=&period=` (JSON + small page) used by the editor.
- Room utilisation report (`timetable:room_report`).

Tests (`tests/test_timetable.py`):
- `test_grid_edit_saves_all_cells_atomically_and_reports_conflict_per_cell`.
- `test_routine_grid_for_student_only_shows_own_section`.
- `test_free_teachers_excludes_busy_and_other_school`.
- `test_routine_pdf_grid_renders`.

### WP-09 — SMS completion (≈5 h)

Build:
- Compose: template picker (`template` ModelChoiceField, JS copies body into textarea), preview of rendered text for the first recipient, recipient count before send, character/segment counter (160 GSM / 70 Unicode for Bangla).
- Placeholder rendering per recipient: `messaging/services.render_for(template_or_body, student=None, guardian=None, employee=None, invoice=None, extra={})`; `queue_batch` accepts `contacts` as `(name, phone, context)` and renders per message.
- Batch list `messaging:batch_list` shows batches (title, kind, total/sent/failed, sent_by, created) with drill-down to messages (`messaging:batch_detail`); existing message log becomes `messaging:message_list` with status filter.
- Event triggers (all opt-in per school; all queue only): absence (WP-03), fee reminder (WP-04), result published (WP-07), admission welcome (`School.notify_admission_sms`), payment receipt (`School.notify_payment_sms`, on `collect_payment`: "{amount} received, receipt {receipt}, balance {balance}"). Settings page "Notifications" under Basic settings with the toggles and template selectors; `setup_school` (WP-11) seeds the five default templates.
- `process_sms`: `--loop --interval 30` option for a simple long-running worker; per-message `attempts` counter (new field) with max 3 and exponential backoff; `SMSMessage.dedupe_key`.
- Gateway test button in SMS settings: sends one message to a number entered by Admin via the configured backend (synchronous, explicit).

Tests (`tests/test_messaging.py`):
- `test_compose_with_template_renders_per_recipient` (two guardians → two bodies containing each student's name).
- `test_segment_counter_helper` (`segments("Hello") == 1`, Bangla 71 chars → 2).
- `test_batch_list_counts_and_detail_scoped_to_school`.
- `test_payment_sms_queued_when_enabled_with_balance`.
- `test_process_sms_retries_up_to_three_then_fails` (backend raising → attempts 3 → status failed).
- `test_dedupe_key_prevents_duplicate_queue`.

### WP-10 — Holiday management completion (≈2 h)

Build:
- Month calendar view `holidays:calendar_view` `/holidays/calendar/?month=YYYY-MM` (grid; weekends shaded; holidays coloured by type; events listed) available to every logged-in role; link from portal.
- Bangladesh public-holiday preset: `holidays/fixtures/bd_public_holidays_2026.json` + "Import national holidays" button (`holidays.add_holiday`), idempotent by (name, start_date).
- Academic-year working-days counter on the year edit page (uses `holiday_dates_between` + weekends).

Tests (`tests/test_holidays.py`):
- `test_calendar_view_marks_weekend_and_holiday_cells`.
- `test_import_preset_is_idempotent_and_school_scoped`.
- `test_working_days_count_excludes_weekends_and_holidays`.

### WP-11 — Basic settings completion (≈3 h)

Build:
- Settings landing `/settings/` → hub page with cards (School profile, Academic setup, People setup, Finance setup, Notifications, Users, Audit); move the profile form to `/settings/school/`.
- `setup_school` management command (and "Initialise defaults" button on the hub, Admin only, idempotent): default accounts (`ensure_default_accounts`), default grade scale, fee categories (Tuition/Admission/Exam/Transport → income accounts), periods (8 periods + tiffin), leave types (Casual 10, Sick 14, Earned 20), download categories, SMS templates, current academic year if none.
- Delete/deactivate for master data: add `is_active` where missing (`ClassLevel`, `Section`, `Subject`, `Department`, `Designation`, `LeaveType`, `Period`, `Room`, `FeeCategory` already has it); generic list shows inactive greyed with filter; `ERPDeleteView` wired (`crud(..., delete=True)`) with `ProtectedError` → friendly message ("used by 12 enrollments — deactivate instead").
- Grade scales and rules reachable from settings hub (link only; views stay in examinations).
- Notifications page (WP-09) and Period lock (WP-05) live here.
- Year switch button (WP-00 D3) on the years list.

Tests (`tests/test_settings.py`):
- `test_setup_school_is_idempotent_and_creates_defaults`.
- `test_delete_protected_master_data_shows_message_and_deactivate_works`.
- `test_settings_hub_cards_filtered_by_permission` (Principal sees Academic setup but not School profile).

### WP-12 — User management completion (≈4 h)

Build:
- "Create login" action on Student, Guardian and Employee detail pages (`users:provision`, POST, `users.add_user`): username = `student_id` / `employee_id` / guardian phone; role auto (Student/Guardian/Teacher-or-Staff by `employee_type`); generates a temporary password shown **once** on the confirmation page and (if `notify_admission_sms`) queued by SMS; sets `User.must_change_password=True` (new field) enforced by middleware that redirects to `password_change` until cleared; links the profile's `user` FK; audit `user.provisioned`.
- Bulk provisioning for a section (`users:provision_bulk`): creates missing student and primary-guardian logins; CSV download of credentials generated in that run (never stored).
- Admin reset: on user edit, "Set temporary password" (forces change) and "Send reset email" (uses Django reset flow) buttons.
- User list columns: roles, linked profile (student/guardian/employee), last login, active; filters by role/active; detail page `users:detail`.
- `User.must_change_password` + `core/middleware.ForcePasswordChangeMiddleware` (exempt logout/password_change URLs).

Tests (`tests/test_users.py`):
- `test_provision_student_login_links_profile_sets_role_and_forces_change`.
- `test_provision_is_idempotent_and_school_scoped` (second POST → 200 with "already has a login"; foreign profile → 404).
- `test_bulk_provision_creates_only_missing_and_returns_csv`.
- `test_force_password_change_redirects_until_changed`.
- `test_admin_temporary_password_reset_audited_and_not_for_superuser`.

### WP-13 — Dashboards & portal (≈4 h)

Build:
- Admin/Principal dashboard (reuse the orphaned `templates/core/dashboard.html` design): tiles (active students, staff, today's student attendance %, fees collected this month, outstanding), class strength bars, quick actions filtered by `perms`, pending approvals (leaves, unlock requests), recent payments/admissions, upcoming holidays, notices. All aggregates via annotated querysets (WP-04 `with_totals`).
- Teacher dashboard: my sections today (routine slots for today), registers not yet taken today, exams with missing marks for my assignments, pending unlock decisions.
- Accountant dashboard: today's/month's collections by method, invoices due this week, top defaulters (WP-04).
- Staff dashboard: my attendance this month, leave balance, payslips.
- Portal: notices (WP-14), routine grid (WP-08), attendance calendar (WP-10 grid reused per student), results list with report-card PDF links, receipts list, fee-due summary; guardian SMS opt-in flag (`Guardian.sms_opt_in`, default True).

Tests (`tests/test_dashboards.py`):
- `test_admin_dashboard_tiles_and_query_budget` (≤ 25 queries with seed data).
- `test_teacher_dashboard_lists_untaken_registers_and_missing_marks`.
- `test_accountant_dashboard_collections_by_method`.
- `test_portal_shows_notices_routine_and_receipts_only_for_linked_child`.

### WP-14 — Notices and reports (≈4 h)

Build:
- `downloads.Notice` (`title`, `body` (Markdown-safe plain text), `audience` as `DownloadItem.Audience`, `class_levels` M2M, `publish_at`, `expires_at`, `is_pinned`, `created_by`); CRUD under `/downloads/notices/manage/`; public list at `/downloads/notices/` respecting `visible_to`; dashboard/portal widget; optional SMS announce (queues via WP-09).
- Reports hub additions with PDF headers: Defaulters (WP-04), Daily attendance summary (WP-03), Class-wise result analysis PDF (subject stats), Trial balance / Income statement / Cash book (WP-05), Payroll register (month), Leave register, Teacher load (periods per week), Student strength by class/gender/religion; every report supports `format=csv|xlsx|pdf` through `core/exports` and a shared PDF header (`core/pdf.py: document(title, school, subtitle)`).

Tests (`tests/test_notices_reports.py`):
- `test_notice_visibility_by_audience_class_and_dates`.
- `test_reports_hub_lists_new_reports_by_permission`.
- `test_every_report_exports_three_formats` (parametrised over report URLs as Admin: 200 and correct Content-Type).

### WP-15 — Production readiness (≈4 h)

Build:
- `Dockerfile` (python:3.12-slim, gunicorn, `collectstatic`, Tailwind built in a stage using the Linux CLI), `docker-compose.yml` (web, db=postgres:16, worker running `process_sms --loop`), `whitenoise` for static, `psycopg[binary]` in requirements, `.env.example` completed (cache, email, SMS, HSTS).
- Settings: when `DEBUG=0` use `django.core.cache.backends.db.DatabaseCache` (or Redis via `CACHE_URL`) so login rate limiting is shared; set `SECURE_HSTS_INCLUDE_SUBDOMAINS=True`, `SECURE_HSTS_PRELOAD` from env; `LOGGING` to stdout with request id; `DATA_UPLOAD_MAX_NUMBER_FIELDS` raised for grid forms (WP-07/08).
- `manage.py backup` command: `pg_dump`/SQLite copy + media tarball to `backups/` with retention; `restore` documented.
- Health endpoint `/healthz/` (DB + cache).
- README: deployment runbook, roles/permissions table (generated by a `manage.py role_matrix` command that prints the A5 table so docs never drift).
- Playwright smoke in CI (optional job) covering login, admission, register, collect fee, enter marks, publish.

Tests: `test_healthz_ok`, `test_backup_command_creates_archive(tmp_path)`, `test_role_matrix_command_output_contains_all_roles`, `test_deploy_check_has_no_warnings` (run `check --deploy` with env set; assert empty).

### Out of scope until credentials exist (document, do not build blind)

Online fee gateways (bKash/Nagad/SSLCommerz) and biometric device import. When credentials arrive: gateway = `fees.PaymentIntent` + signed callback verification + idempotent `collect_payment(method="gateway")`; biometric = `attendance.DeviceLog` import + mapping + exception review. Both plug into existing services without schema changes elsewhere.

---

## Part C — Operating contract for the implementing agent

1. **Never weaken tenant scoping.** Every new model inherits `SchoolScopedModel`; every queryset in a view filters by `request.school` (use `SchoolScopedMixin`, `require_permission`, `students_for`/`sections_for`); every form is a `SchoolModelForm`; every service starts with `assert_actor_school` / `assert_school`.
2. **Business logic lives in `<app>/services.py`**, is wrapped in `transaction.atomic`, raises `ValidationError`/`PermissionDenied`, writes an `AuditLog` row for money, marks, attendance, roles and settings changes. Views only validate forms and call services.
3. **Financial history is append-only**: never edit or delete a posted `JournalEntry`, a non-cancelled `FeePayment`, or a `ResultSnapshot`. Corrections are reversals or new versions.
4. **Permissions**: declare `permission_required` on every view; add new model permissions to `core/roles.py` deliberately (Admin gets all; Principal gets academic; Accountant gets fees/finance; Teacher/Staff/Student/Guardian get explicit view/add codenames). Re-run `setup_roles` in tests via the `erp` fixture (already does).
5. **UI**: extend `layouts/base.html`; use `.card`, `.table`, `.btn-*`, `.input` classes and `{% render_form %}`; rebuild CSS with `tools/tailwindcss.exe -i static/src/input.css -o static/css/app.css --minify` and commit `app.css`; keep pages usable at 390 px width.
6. **Tests**: put new tests in `tests/test_<area>.py`; reuse the `erp`, `admin_client`, `invoice` fixtures in `tests/conftest.py`; add fixtures there rather than in test files; every WP adds the tests listed above and keeps `pytest -q --ignore=tests/test_browser.py` green; coverage must not drop below 87 %.
7. **Migrations**: one migration per WP per app, named; data migrations only for defaults; run `makemigrations --check --dry-run` before finishing.
8. **Style**: `ruff format` + `ruff check` clean (after WP-00); no compressed one-liner style in new code; module docstring on every new file.
9. **Definition of done per WP**: features + tests + `README.md`/`docs/digicampus-comparison.md` status rows updated + one commit per WP with message `WP-XX: <title>`.
10. **Commands** (Windows):
    ```powershell
    .\.venv\Scripts\python.exe manage.py check
    .\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
    .\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_browser.py
    .\.venv\Scripts\python.exe -m ruff format . ; .\.venv\Scripts\python.exe -m ruff check .
    .\tools\tailwindcss.exe -i .\static\src\input.css -o .\static\css\app.css --minify
    ```
