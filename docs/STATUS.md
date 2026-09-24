# Build status

Last updated: 24 September 2026. This records what was built against
[the audit and plan](AUDIT_AND_UPDATE_PLAN.md), what a later re-audit found and fixed, and what
deliberately was not built.

## Where it stands

| Measure | Before | Now |
|---|---:|---:|
| Tests passing | 148 | 858 (+1 browser) |
| Python line coverage | 87 % | 92 % |
| Lint / format | none configured | ruff clean, 186 files |
| `check --deploy` | 2 warnings | clean at `--fail-level WARNING` |
| Dead templates | ~30 | 0 (a test now fails if one appears) |
| Screens rendering on a fresh seeded database | — | 37 / 37 |
| End-to-end browser run | hard-coded path, never run | passes; screenshots committed |

Verified by: `manage.py check`, `makemigrations --check --dry-run`, `check --deploy`,
`ruff check`, `ruff format --check`, the full suite with coverage, a fresh-build comparison
of the committed stylesheet, and a smoke pass that signs in and opens every major screen
against a database created from scratch.

## Exam, results and English-medium programme (September 2026)

Built in stages on the branch `feature/exam-results`, each with its own tests and a full gate
(check, migrations, ruff, the whole suite, `check --deploy`, a regenerated access matrix).
The plan, its evaluation of Codex's plans and reviews, and every decision are in
[IMPLEMENTATION_STAGES.md](IMPLEMENTATION_STAGES.md).

| Stage | What it delivered |
|---|---|
| S0–S1 | Rulebook schema; safe mark entry (section checks, one version per batch, change-only notifications); frozen, fingerprinted results |
| S2 | Rulebooks for Cambridge, Edexcel, IB MYP and IB Diploma beside the school's own and the national rules; grade scale presets |
| S3 | Subject plans and per-student choices used everywhere |
| S4 | Paper parts with presets, weights, tier caps and locks |
| S5 | Comments, effort or ATL, and approved predicted, forecast and target grades |
| S6 | Grade distribution, tabulation and merit exports |
| S6b | Exam series, entries and the official results register |
| S7 | Transfer, leaving and character certificates |
| S8 | Pre-publish checklist, exempt papers, combined results, promotion from results |
| S9 | Bangla and English toggle |
| S10 | Entries file, access arrangements, national board registration data |
| S11 | Public result lookup by result code |
| S12 | VAT per fee head; online payments through SSLCommerz |
| S12c | Privacy baseline: consent, restricted identity numbers, retention and erasure |
| S13 | Demonstration classes for each programme; documentation |

Three independent reviews by Codex were checked against the code; every confirmed finding was
fixed with a regression test (see sections 1, 1b and 1c of the stages document). Worked
examples still need a school's sign-off before the results are relied on (section 4 there).

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

## What a re-audit found

The build above passed its own tests. A second pass over it, looking for the cases those tests
did not describe, found twenty-odd defects; they are fixed, and `tests/test_audit_fixes.py`
names each one as a way the software was wrong rather than a feature it has. The ones worth
knowing about:

- **A leave approval destroyed manual attendance.** Approving leave over a day already marked
  present took ownership of that row and rewrote it, then deleted it outright when the approval
  was withdrawn. Approval now creates rows only for days with no entry, reports the days it left
  alone, and withdrawal removes only what it created.
- **A closed accounting period had three ways round it.** Cancelling a fee receipt, reversing a
  journal, and the two-line entry helper all wrote without checking. Worse, a refused
  cancellation still marked the receipt cancelled, leaving a receipt with no reversal behind it.
  The lock is now one function on the model layer, checked before anything is written.
- **There was no way to undo a salary.** The ledger refused to reverse a salary entry and said
  to do it from payroll, where no such workflow existed. There is one now.
- **A teacher held the whole staff roster**, including every colleague's phone, NID and
  qualification, and could export it. Removed; their own file is still one click away.
- **A teacher's student list included anyone ever enrolled in a section they are ever assigned
  to**, including pupils who left years ago. It is now this year's active roll of the sections
  they currently teach, with historical exam access authorised against the exam's own year.
- **Families could open the teacher-load report** and the free-teacher helper, both of which
  list staff names and workloads. Both are now for the people who plan the timetable, and the
  reports hub uses the same predicate as the destination, so it never offers a 403.
- **Temporary passwords were written to the session table**, which is signed but not encrypted.
  They now live in one response; the bulk CSV is built in the browser.
- **The SMS worker could strand a message for ever.** A crash after claiming left it
  "processing" with nothing to reclaim it. Claims now expire, retries back off, and the attempt
  is counted at claim time so a message that kills the worker still runs out of attempts.
- **The import preview and the import disagreed.** The preview checked the student and little
  else, so confirmation could drop an enrollment, replace a roll the operator supplied, accept
  a relation that is not one, or fail on a duplicate roll. Everything is validated up front and
  the write consumes what the preview checked.
- **"Register taken" meant one row existed**, and a class of thirty with one child marked showed
  100% attendance. Registers are now complete, partial or missing against the actual roll.
- **The iCalendar export replaced a newline with a newline**, so a multi-line holiday
  description produced a file real calendar clients reject. Escaping and folding now follow
  RFC 5545.
- **15 August was imported as a public holiday** after the government cancelled it. The
  fixed-date list is versioned by year and cites its notices, so importing 2023 still reproduces
  the calendar that year was kept to.
- **The health probe returned raw exception text** on an endpoint with no sign-in, and
  `pg_dump` took the password on the command line.

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
  and the groups, and a test compares the committed file with a fresh run, so it cannot drift
  from the code. Where a view narrows access beyond its declared permission, it says so in the
  decorator and the matrix repeats it, rather than the table quietly overstating what a role
  can reach.
- **A guardian with several children is the ordinary case, not an edge case.** One login
  covers the family, home totals the fees across children, and a class notice names each
  child rather than the first one alphabetically. Messages are deduplicated on what the
  person would actually receive, so a personalised notice arrives per child while a generic
  one still arrives once.

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
  Nor can delivery be exactly-once: these gateways offer no idempotency key, so a worker killed
  between the gateway accepting a message and the answer being recorded will retry it. The claim
  and backoff machinery narrows that window; it cannot close it.
- **A credential SMS holds a readable password until it is delivered.** It has to, to be sent.
  The body is cleared the moment delivery ends, but a school that queues credential messages and
  never runs the worker is storing passwords in its outbox.
- **Guardian phone numbers are canonicalised, not merged.** Existing records were rewritten to
  one spelling by a migration, which reports any two guardians of a school that then share a
  number. Whether those are one family entered twice or two people sharing a handset is a
  question only the office can answer, so nothing is merged automatically.
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
