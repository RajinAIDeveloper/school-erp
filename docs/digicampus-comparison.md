# School ERP comparison and workflow audit

Audit date: 22 September 2026. The comparison source is [DigiCampus BD's public feature and pricing page](https://digicampusbd.com/). Its public page names 13 ERP modules, biometric attendance integration, online fee collection through bKash/Rocket/Nagad/Cellfin/SSL, and describes online learning integration and performance analytics. It mentions a parent portal and mobile app in a testimonial. [The login page](https://digicampusbd.com/login) is public, but the ERP screens and permission matrix require an account. Therefore the role workflows below are **proposed school ERP workflows** checked against this repository, not a claim about DigiCampus's private screens.

## Capability score

The score estimates usable workflow completion, including a screen, access control, business logic, exports where relevant, and meaningful tests. Each of the 13 requested modules has equal weight. It is a planning estimate rather than measured source code coverage. DigiCampus's two separately advertised integrations are scored as additional equal capabilities for the comparison total.

| Capability | App/screen | Status | Estimate | Implemented and key gap |
|---|---|---|---:|---|
| Students | `students`, `/students/`, `/portal/` | Partial | 75% | Admission, guardian links, enrollments, promotion, document upload, CSV import, ID card, linked family portal. Missing integrated admission wizard, transfer workflow, admit cards, previewable import error report. |
| Teachers and staff | `employees`, `/employees/` | Partial | 65% | Profiles, departments, designations, subject assignments and leave. Missing staff detail screen and salary history, qualifications/document history, automated account creation. |
| Student attendance | `attendance`, `/attendance/` | Partial | 65% | Assigned section register, holiday-aware validation, monthly CSV/Excel, family history. Missing absent alerts, approval/change window and device import. |
| Fees | `fees`, `/fees/` | Partial | 70% | Structures, concessions, idempotent invoicing, partial payments, PDF receipts, reversals and collection report. Missing online checkout, gateway callback reconciliation, due reminders and fine policies. |
| Accounts | `finance`, `/finance/` | Partial | 65% | Chart of accounts, balanced journals, fee posting, reversal, basic payroll and overview. Missing bank reconciliation, period close, detailed cash book and payroll policy. |
| Staff attendance | `attendance`, `/attendance/staff/` | Partial | 55% | Manager entry, own staff view, monthly sheet and leave requests. Missing biometric import, self check-in policy, approvals for edits and payroll deduction rules. |
| Results | `examinations`, `/exams/` | Partial | 75% | Exam schedules, assigned mark entry, complete-mark publication, immutable data snapshots, lock, scoped correction, ranks, subject analysis, CSV/Excel/PDF report cards. Missing stored PDF versions, bulk card generation, multi-term progression and verified Bangla PDF typography. |
| Routine | `timetable`, `/routine/` | Partial | 65% | Periods, rooms, teacher/room/section conflict validation, class/teacher filters, exports. Missing timetable editor grid and separate exam routine presentation. |
| Downloads | `downloads`, `/downloads/` | Partial | 65% | Categories, protected uploads, role/class filtering, public download endpoint, download count. Missing published notices, expiry and stronger file scanning. |
| SMS | `messaging`, `/sms/` | Partial | 50% | Templates, recipient groups, persistent outbox, console/HTTP backend and retry. Missing automated event triggers, provider balance and delivery receipts; real gateway not verified. |
| Holidays | `holidays`, `/holidays/` | Partial | 75% | Holiday CRUD, weekend exclusion and iCalendar export. Missing calendar grid and separate student/staff calendars. |
| Basic settings | `core`, `academics`, `/settings/` | Partial | 75% | School identity, academic years, terms, classes, sections, subjects, departments, designations, weekend and SMS settings. Missing shifts and tenant self-service onboarding. |
| Users and roles | `users`, `/users/` | Partial | 65% | Seven groups, login, rate limit, profile, reset, activate/deactivate, assigned permissions, role-scoped navigation. Missing automated account provisioning, login audit and MFA. |
| Biometric device integration | None | Missing | 0% | No device adapter, enrollment of device identifiers, log import, deduplication or exception review. |
| Online fee gateways | None | Missing | 0% | A payment *method* field records manual mobile transfers; it does not initiate or verify bKash/Rocket/Nagad/Cellfin/SSL payments. |

Requested 13-module estimate: **865 ÷ 13 = 67%** (rounded). DigiCampus's 15 advertised module/integration capabilities: **865 ÷ 15 = 58%** (rounded). These figures must not be read as a production acceptance score. The separate Reports hub is partially implemented and is not listed as a separate DigiCampus pricing module.

Other DigiCampus public descriptions to assess separately: a mobile application is absent (the web layout is responsive); online learning integration is absent; performance analytics exists only in basic results subject analysis and a management overview. The public site does not establish which pricing plan receives a distinct internal feature set: the same named list appears in all three plans.

## Role access and actual screens

Roles are global Django Groups attached to school-scoped accounts. Management roles have broader access; every record query must still be limited to the account's school. The matrix describes this codebase. `View` means the screen opens; `Edit` means it can submit the business action. Student and guardian screens require an actual linked profile. School admins use the ERP, while Django admin is reserved for platform superusers.

| Role | Main usable screens and actions | Current limits |
|---|---|---|
| Administrator | School setup, people, both attendance registers, fees, accounts/payroll, results, routine, downloads, SMS, holidays, users, reports | No live gateway/device integration; no staff/parent account automation. |
| Principal | Academic setup, student/staff records, attendance, exams and publication, routine, downloads, SMS, leave approvals, overview, read-only users/finance | School identity and SMS credentials are administrator-only; user creation and finance posting are not granted. |
| Accountant | Student/staff lookup, fee structure and collection, reversals, accounts/payroll, financial reports, SMS compose/log, downloads, holidays | No academic editing; student and family financial visibility is school-wide. |
| Teacher | Assigned students, assigned student attendance, assigned subject marks, routine, staff attendance view, own leave, downloads and holidays | Cannot collect fees or edit school settings. Student detail hides fees. |
| Staff | Own staff attendance view, leave request, routine, downloads, holidays and reports hub | Cannot mark own attendance; no device integration. Routine will be empty without an assigned teaching/class relationship. |
| Student | Linked record portal, own attendance, fees, published result cards, class routine, downloads and holidays | Account must be linked manually; no online payment or mobile app. |
| Guardian | Linked children portal, each child's attendance, fees, published report cards, routine, downloads and holidays | Cannot access another family by changing a URL; no online checkout. |

The role matrix has automated GET tests for all 7 roles across 16 high-level screens. That proves the menu pages return the expected access status. It does not prove every possible organization policy or every POST action. Permission enforcement uses [roles](../core/roles.py), [school-scoped mixins](../core/mixins.py), [object access helpers](../core/access.py), and business service checks. A hidden menu is only navigation, not the permission boundary.

## Walkthroughs a user should be able to complete

1. **Administrator — initialize school.** Create a superuser, run migrations and role setup, create or seed a school, attach the account, then enter school profile, academic year, class, section, subject and teacher assignment. Open Reports to verify counts. Current workflow works through ERP screens and the demo command. A branded onboarding wizard and shift setup remain open.
2. **Administrator — create access.** Open User Management, create a username and password, select a Group, then link that account from a student, guardian or employee form. The person logs in and changes the password. Create, edit, profile and password reset screens exist. Automatic provision on admission/hire and login audit remain open.
3. **Administrator/Principal — admit and promote.** Create a student, link a guardian, create an enrollment for the current year and section, upload documents, optionally import a UTF-8 CSV, then run Promote for the next year. Old enrollment history remains. The flow currently spans separate screens and has no transfer wizard or import preview.
4. **Administrator/Principal — hire and assign staff.** Create department/designation and employee, link a user, create subject/section/year assignments, set a class teacher, then open leave and attendance screens. Staff detail history is incomplete. Principal can edit academics and staff but cannot change the school identity.
5. **Teacher — take class attendance.** Open Student Attendance, pick an assigned section and date, set each student's status, save and view the monthly report. A holiday or future date is rejected and other sections are excluded. Guardian absence alerts and device logs are absent.
6. **Staff and Principal — leave.** Staff requests leave for their own profile; Principal/Admin reviews pending requests; staff sees the decision in My Leave. This works for basic requests. Leave quotas/overlap policies are not implemented.
7. **Accountant — set up and collect fees.** Set fee categories and class/year structures, add concessions, generate a month once, open an invoice, collect partial or final cash/bank/mobile payment, print a receipt, and reverse a duplicate with a reason. Cash and income journals post atomically. Manual mobile recording works; an online gateway checkout, webhook verification, defaulter reminders and automatic late fines do not.
8. **Accountant — post expenses and payroll.** Create account heads, post a balanced manual journal, review account balances, prepare a salary, pay it from cash and download a payslip. Posted manual journals can be reversed with a reason. Detailed payroll components, bank reconciliation and closing controls remain open.
9. **Teacher/Principal — exam and results.** Define grading rules, create an exam and paper schedule, select an assigned paper/section, enter marks or an explicit absence, then review section/class results and subject statistics. Principal publishes only when all marks are complete. Publishing creates versioned data snapshots and locks mark writes at the application and database layers. A teacher may request a 48-hour scoped correction; an approved edit creates the next snapshot. Parent/student can view only published linked results. PDF cards are generated on request and have not been validated for Bangla typography or stored as versioned files.
10. **Administrator/Teacher/Student — routine.** Set periods and rooms, assign subjects and teachers to weekday slots, review class or teacher timetable, then export CSV/Excel/PDF. Overlapping teacher, room and section slots are rejected. A drag-and-drop week grid and exam routine view are open.
11. **Administrator/Teacher/Parent — files and messages.** Add a category and upload a file; the authorized role downloads it through a protected endpoint. For SMS, compose a batch for a class, guardians or staff; the database outbox is processed by `process_sms`; review statuses and manually retry failures. Automated absence/payment/result triggers and provider delivery receipt verification are missing.
12. **Administrator/Principal — calendar.** Enter a holiday or open-school event, export iCalendar and observe school closure exclusion in attendance. Student/staff-specific holidays and a month grid remain open.
13. **Student/Guardian — daily portal.** Log in with a linked account, choose a child, open attendance by month, inspect invoices/receipts, download published report cards, review routine and permitted downloads. Cross-child URL changes fail. Online fee payment and an app are absent.
14. **Principal/Accountant — reports.** Open the Reports hub for links permitted by the role. The management overview gives range-filtered attendance counts, collections, outstanding invoices and ledger totals with CSV/Excel/PDF. Source reports provide attendance grids, collection lists, account balances, result analysis, and routine exports. A configurable report builder, saved filters, reconciliation report and scheduled delivery remain open.

### Proposed DigiCampus parity workflows without public internal evidence

- **Biometric:** register device and school identifier → map device user ID to student/employee → import time punches → deduplicate and flag exceptions → review missing/late cases → post approved attendance → feed reports and optional payroll policy. No matching code exists here.
- **Online fees:** create a gateway checkout for an outstanding invoice → redirect or present provider form → validate signed callback server-side → record an idempotent fee payment → post the journal and receipt → reconcile settlement → reverse/refund with an audit trail. No provider integration exists here.
- **Mobile app:** authenticate the student/guardian or teacher with a scoped API, sync only permitted data, show push notifications and handle offline/retry cases. The current repository has a responsive web UI and no mobile app/API contract.

## Reports module and regression tests

There **is now a `reports` app** with a [report hub](../reports/views.py) and school overview. Individual reports still live in `attendance`, `fees`, `finance`, `examinations`, `timetable`, `students` and `holidays`; this avoids duplicating their business queries. Parent/student reports live in [portal views](../core/portal_views.py). The hub is a navigation and consolidated-metrics layer, not a complete report designer.

The test suite includes [role/report tests](../tests/test_roles_reports.py), [financial, results and attendance workflows](../tests/test_workflows.py), [screen smoke tests](../tests/test_pages.py), [edge cases](../tests/test_edge_regressions.py), and a [real browser test](../tests/test_browser.py). The final run passed **149 tests** and measured **87% Python line coverage**. Aggregate app coverage was: academics 93%, attendance 87%, core 85%, downloads 88%, employees 92%, examinations 84%, fees 86%, finance 87%, holidays 94%, messaging 80%, reports 100%, students 84%, timetable 88%, users 94%. The machine-readable report is [coverage.json](coverage.json). Tests cover important business invariants (duplicates, permissions, payment balancing/reversal, publication locking, snapshots, tenant isolation), but **they do not fully cover every module workflow or prove external integrations**. Priority test gaps are live SMS provider and queue recovery, payroll/reconciliation edge cases, upload threat cases, PDF typography, and full cross-role POST permission matrices.

## Priority implementation order

1. Establish production fundamentals: PostgreSQL test deployment, email/SMS provider contracts, secure static/media hosting, backup and restore rehearsal, and user acceptance checks with school staff.
2. Implement online fee gateways with server-verified, idempotent callbacks, settlement reconciliation, reversal and test sandbox credentials.
3. Integrate biometric devices with configurable mappings, log import, exception review and payroll/attendance policy.
4. Complete automated notifications (absent, fee due/payment, published result, holiday) with consent, outbox monitoring, idempotency and delivery receipts.
5. Finish role workflows: account provisioning, student admission and transfer, leave entitlement, school calendar, staff detail/history, timetable grid, stored PDF cards and Bangla font validation.
6. Expand reporting: detailed ledger/cashbook, defaulters, payroll, attendance trends, comparative results, school-year filtering, saved filters and scheduled export.
7. Add a scoped API and mobile app only after the web flows and permissions are stable. Design online learning features from a specific school requirement, since the public DigiCampus page gives no concrete internal workflow for it.
