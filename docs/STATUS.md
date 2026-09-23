# Build status

Last updated: 23 September 2026. This records what was built against
[the audit and plan](AUDIT_AND_UPDATE_PLAN.md), and what deliberately was not.

## Where it stands

| Measure | Before | Now |
|---|---:|---:|
| Tests passing | 148 | 435 (+1 browser) |
| Python line coverage | 87 % | 92 % |
| Lint / format | none configured | ruff clean, 181 files formatted |
| `check --deploy` | 2 warnings | clean at `--fail-level WARNING` |
| Dead templates | ~30 | 0 (a test now fails if one appears) |
| Screens rendering on a fresh seeded database | — | 37 / 37 |
| End-to-end browser run | hard-coded path, never run | passes; screenshots committed |

Verified by: `manage.py check`, `makemigrations --check --dry-run`, `check --deploy`,
`ruff check`, `ruff format --check`, the full suite with coverage, a fresh-build comparison
of the committed stylesheet, and a smoke pass that signs in and opens every major screen
against a database created from scratch.

## Work packages

All sixteen are complete, each its own commit.

| WP | Title | What it delivered |
|---|---|---|
| 00 | Baseline hygiene | ruff + CI + `.gitattributes`; audit defects D1–D15 fixed; 29 dead templates removed |
| 01 | Students | One-screen admission, roster filters, leaving records, import preview, ID cards, statements |
| 02 | Teachers & staff | Personal file, documents, ownership-based access, salary visibility |
| 03 | Student attendance | Radio register with mark-all, per-student history, daily summary, absence alerts, N+1 fix |
| 04 | Fees | Ad-hoc invoices, cancellation, dues chasing, late fees, payment confirmations, annotated totals |
| 05 | Accounts | Multi-line journals, ledgers, trial balance, income statement, balance sheet, cash book, opening balances, payroll, period lock |
| 06 | Leave | Approval marks the register, entitlement and overlap checks, self check-in |
| 07 | Results | Grid mark entry, report cards v2, admit cards, exam routine, progress reports, verification, result SMS |
| 08 | Routine | Week grid, bulk editor with clash detection, free-teacher lookup, utilisation |
| 09 | SMS | Templates per recipient, preview with cost, batches, retries, gateway test, notification settings |
| 10 | Calendar | Month grid, working days, national-holiday import |
| 11 | Settings | Hub with readiness check, `setup_school` defaults, master-data deactivation |
| 12 | Users | Provision logins from a record or a whole class, forced password change, admin resets |
| 13 | Dashboards | A separate panel per role, answering that role's own question |
| 14 | Notices & reports | Noticeboard with audiences, shared audience rule, five new reports |
| 15 | Production | Docker, compose, WhiteNoise, shared cache, health check, backups, `role_matrix`, runbook |

## Notable decisions

- **Ownership, not blanket permissions.** A staff member reads their own file without being
  given the whole roster; asking for a colleague's answers 403, and a document request from
  an unrelated account answers 404 rather than confirming the file exists.
- **Nothing sends inside a request.** Every notification is queued and delivered by a worker,
  after commit, deduplicated by event. A gateway fault can never undo a saved register or a
  receipt, and a retried request cannot text a family twice.
- **Validate the whole batch before writing any of it.** The mark grid, the timetable week
  and the CSV import all check every row first and report problems in place. A half-saved
  timetable silently double-books a teacher; a half-marked class is worse than an unmarked one.
- **Money and marks are append-only.** Posted journals are reversed, never edited. Published
  results are snapshotted and locked at the database level; a correction needs an approved,
  time-limited unlock and produces a new version rather than overwriting the old one.
- **Off by default where it costs money or reaches a family.** All five SMS notification
  types start switched off, and a guardian can be opted out individually.
- **Generated documentation.** `manage.py role_matrix` prints the access matrix from the URLs
  and the groups, so it cannot drift from the code.

## Deliberately not built

Online fee gateways (bKash, Nagad, SSLCommerz) and biometric attendance devices. Both need
provider contracts and credentials; guessing their callback shapes in advance would produce
code nobody could trust and tests that prove nothing. The seams exist: a gateway becomes a
payment intent plus a signed callback calling the existing `collect_payment`, and a device
becomes a log import feeding the existing attendance service. Neither needs a schema change
elsewhere.

A mobile app is also absent. The web interface is responsive and works on a phone; a scoped
API and an app belong after the web flows have been used by a real school for a term.

## Known limits

- **SMS delivery means the gateway accepted the request.** Provider delivery receipts are not
  integrated, so "sent" is not proof a handset received it. The gateway test screen says so.
- **Bangla PDF typography is wired and verified in code, not on paper.** Noto Sans Bengali
  (OFL) ships in `static/fonts/`; tests assert it registers, that every assigned Bengali
  letter maps to a real glyph, that a report card with a Bangla name embeds the face, and
  that printing still works if the font is removed. What has *not* happened is a human
  looking at a printed card to judge conjuncts and line spacing.
- **The browser test needs Chromium** and is excluded from the default run; CI has a separate
  job for it. It has been run locally: sign-in, the student roster, admitting a student
  through the real form, the mobile sidebar and the results screen all render correctly
  (screenshots in `docs/screenshots/`).
- **Single school in practice.** Every record carries a school foreign key and the queries are
  scoped, but host-based tenant resolution is not implemented; `request.school` comes from the
  signed-in user.
