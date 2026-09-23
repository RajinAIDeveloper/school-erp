# Curriculum-flexible exam, results and school-pilot implementation plan

23 September 2026. This is the implementation companion to
`AUDIT_AND_UPDATE_PLAN.md` and `MARKET_AND_COMPLIANCE_REVIEW.md`. Where the
market review assumes a national-board-first product, **this scope takes
precedence**. It prioritises
the product the school will actually judge: accurate marks, defensible result
publication, useful analysis and report cards in the school's format.

**Product scope:** schools operating in Bangladesh, regardless of whether they
teach the national curriculum. The core must support school-defined assessment
and multiple programmes within one school: for example national curriculum,
English-version, Cambridge, Pearson Edexcel, IB, madrasa or a school's own
programme. These are *examples of distinct needs*, not a claim of certification
or built-in compliance with every programme. An internal school exam and an
external qualification also need distinct, explicitly selected policies. Board
GPA is one optional preset, never the definition of a result.
The commercial goal is to onboard a new school through configuration and a
reviewed policy adapter, without forking the codebase. It does **not** mean
claiming that every external qualification is automatically calculated by the
ERP; external awards may need to be imported with their provenance.

**Current baseline:** commit `60e8cdc` had 548 passing tests (one browser test was
not run in the read-only audit). At the time this plan was written, the working
tree also contained *uncommitted* changes to academic, student and examination
models/services, grading, mark-entry UI and migrations. They look like an attempt
at subjects, groups, components and board rules. Their presence is **not** an
acceptance result. Inspect and reconcile them in EX-00; preserve the author's
work, do not overwrite it or recreate migrations blindly. Re-run all checks on
the exact commit ultimately offered to a pilot school.

## Decision and release gates

| Gate | Minimum outcome | Explicitly not promised |
|---|---|---|
| G0 — safe demo | Existing simple-class flows work; EX-00 and EX-01 pass; no incorrect teacher read access or duplicate publication/SMS | Any unvalidated curriculum-specific calculation |
| G1 — first supported programme | G0 plus EX-02 through EX-06 for **one school's actual programme**, subject plan and card; school signs off worked examples and printed PDFs | Support for other programmes merely because the engine is configurable |
| G2 — second, materially different programme | G1 plus an independently validated second policy (for example numeric GPA versus criterion/rubric reporting), including migration and historical-result tests | Official external examination certificates, accreditation or board integration |
| G3 — repeatable paid offer | G2 plus EX-07/08, supported onboarding/backup/restore, real pilot measurements and written support terms | A native app, biometric devices or online fees unless separately delivered |

Do not market a gate as achieved because tests are green alone. A school must
approve representative input sheets, calculated output, card layout and release
procedure. Use a named school, programme, academic year and policy version in
each approval. A school may have several programmes and assessment policies at
once; support is granted per validated programme, not per school name.

## Policy evidence before implementation

Maintain a small, versioned `docs/policy-examples/` fixture set (redacted or
synthetic data only) with input evidence and expected result/card outputs approved
by a school. Record the programme, policy source, effective year, level, subject,
assessment type, cohort and any school exception. Never silently apply an SSC
formula to Cambridge IGCSE, IB MYP, early years or even a school's own class
test. The [NCTB 2026 notice](https://nctb.gov.bd/site/notices/7ded5854-8663-4704-848d-70031dfa7eec)
and an [official Dhaka board grading order](https://www.dhakaeducationboard.gov.bd/data/20260412192513655447.pdf)
are sources for a **national-board preset only**. Official
[Cambridge qualification guidance](https://www.cambridgeinternational.org/programmes-and-qualifications/cambridge-upper-secondary/cambridge-igcse/qualification/)
describes a different grading and assessment structure, while
[IB assessment guidance](https://ibo.org/about-the-ib/what-it-means-to-be-an-ib-student/recognizing-student-achievement/about-assessment/)
shows that even programmes under one organisation have different criteria. In
all cases, confirm the specific programme's current official rules **and** the
school's internal-reporting practice before implementing a preset. Do not claim
to calculate an external awarding body's official grade when the school has only
entered its internal marks.

## Ordered work packages

### EX-00 — Reconcile the baseline and make decisions explicit

Depends on: none. Do this before editing any of the current uncommitted work.

Build:

- Inventory `git status`, migration graph and the uncommitted code; distinguish
  finished behaviour, incomplete implementation and mere documentation. Compare
  against the seven findings in the 23 September review. Retain user changes.
- Write the supported-programme matrix: programme/curriculum, school level,
  internal versus external assessment, grading mode (number, letter, GPA,
  rubric, narrative or mixed), subject choice, components, language and card
  template. Label unsupported combinations and prevent publishing them.
- Obtain approved examples for simple numeric marks, individual subject choice,
  rubric/criterion or narrative reporting, absent/missing/not-applicable,
  correction and at least one programme-specific exception. Add fourth-subject
  and two-paper examples **only** for the national-board preset.
- Define whether rankings are by section, class, group, shift or version and
  whether unequal subject sets may be ranked together. Ranking and GPA must be
  *optional*; decide rounding and ties where applicable.
- Review the in-progress binary `assessment_system` (`national`/`other`), fixed
  Science/Business/Humanities group choices and fourth-subject fields. They
  must not become the universal schema for every school. Decide whether to
  generalise them before migration or isolate them inside a national preset.
  Record a small architecture decision for programme, policy version,
  assessment evidence, grade calculation and document layout interfaces.
- Record old-data migration rules: existing published snapshots remain
  immutable; unconfigured old classes retain the prior simple behaviour until
  explicitly migrated. No retrospective GPA recalculation without a new version.

Acceptance:

- A short policy matrix and approved expected-output fixtures exist; ambiguities
  are marked `decision required`, not guessed in code.
- Existing database migrates forward on a copy; rollback/recovery procedure is
  documented. `makemigrations --check --dry-run` is clean on the chosen branch.
- Any broad claim in `MARKET_AND_COMPLIANCE_REVIEW.md` that is unsupported by
  primary evidence is labelled as a vendor claim, inference or hypothesis;
  remove implications that Bangla-medium/national-board rules define the
  product's whole market.

### EX-01 — Repair mark-entry authorisation and publication batching

Depends on: EX-00. This is the highest-priority code release.

Build:

- Authorise both subject **and section** before a mark grid GET retrieves pupils
  or marks; constrain selector choices accordingly. Recheck per row on POST.
- Make a blank/un-absent row's meaning explicit: unchanged, clear to unentered,
  or absent. Provide a visible clear action and preserve entered form values on
  validation errors. Never turn an absence into zero by accident.
- Diff submitted rows against persisted marks. Do not rewrite unchanged rows,
  increment their versions or create audit entries for them.
- For an approved correction batch, save changed rows atomically, then create
  **one** new result snapshot/version and at most one announcement per eligible
  student *after commit*. Keep the existing DB-level published-mark lock and
  scoped unlock; a single-row correction follows the same publication rule.
- Make notification semantics explicit: correction notice versus first release,
  with school opt-in and version-based idempotency.

Tests and gate:

- Teacher with Math/A and Science/B cannot GET or POST Math/B, but can use the
  two authorised combinations; managers and guardian/student scopes remain sound.
- Existing absent -> clear -> unentered works; invalid row leaves the batch and
  displayed form intact; stale version rejects the whole batch.
- A 30-row published grid with one changed score creates one changed mark, one
  snapshot version, one audit event per real change and no SMS storm. Repeating
  the same POST does not create a further version.
- Two concurrent corrections cannot create conflicting versions. SQLite and
  PostgreSQL lock behaviour are both exercised where CI provides those engines.

### EX-02 — Programme/year-specific subjects and individual eligibility

Depends on: EX-00/01. Reconcile the in-progress `ClassSubject`, group and
enrolment-field work instead of adding a second model for the same concept.

Build:

- Define programme and year-specific course/subject offerings and each pupil's
  enrolment in them. Choice, pathway/group, elective, fourth and religion are
  **optional metadata/policy roles**, not universal required fields. A subject
  belongs to one school and a valid programme/year/class; cross-school and
  inconsistent selections are rejected by services and forms.
- Use one eligibility service for mark grids, admit cards, completeness checks,
  live result calculation, snapshot publication, exports and analytics. A paper
  that a pupil does not take is **not** missing, absent, zero or a failure.
- Give schools a review screen showing the chosen subjects per pupil and a
  pre-publish exception list. Do not require all students to take all class
  papers. Avoid silently assigning optional papers from a global flag. Support
  pupils moving between sections or programmes without rewriting issued years.
- Data migration: preserve existing classes as an explicit legacy/simple plan;
  require review before enabling group/choice rules for an existing class.

Tests and gate:

- Pupils in the same class can take different subjects, have only their own
  required assessments and publish successfully; one school can run two
  programmes without result or permission leakage.
- National-board group/religion/fourth-subject rules apply only when selected;
  they do not block an international or school-defined programme. Paper/
  pupil/school/year tampering returns a safe error.
- A simple legacy class still calculates and prints exactly as before.
- Import, bulk mark grid, admit card and result PDF agree on the same eligibility.

### EX-03 — Versioned, curriculum-neutral grading policies

Depends on: EX-02. Keep calculations pure and deterministic; no template should
implement its own grade arithmetic.

Build:

- Introduce named, effective-year policy versions that can express: numeric
  marks/percentages, thresholds, letter grades, GPA where appropriate,
  criterion/rubric levels, narrative-only outcomes, weights, optional bonus,
  no-rank/no-pass-fail modes and display rules. Implement bounded validated
  policy definitions or reviewed strategy modules, **not** arbitrary user code.
  Store the selected policy/version and inputs in each published snapshot.
- Keep the existing simple-average behaviour as a legacy preset. Implement
  board-style fourth-subject arithmetic as a separate, explicitly selected
  national preset; it must not change results for other programmes.
- Keep internally calculated/predicted results distinct from officially
  awarded external results. An external awarding body's grade is imported with
  source, date and reviewer; the ERP does not infer it from school-entered
  marks or mislabel an internal estimate as official.
- Make applicable result fields coherent; absent, exempt/not-applicable and
  missing are distinct. Do not fabricate GPA, percentage, pass/fail or rank for
  a policy that does not define them. Lock rounding only per relevant policy.
- Show policy name, version and calculation trace to authorised staff before
  publication; never expose another pupil's trace in a family view.

Tests and gate:

- Golden tests cover numeric/letter, rubric/narrative, no-GPA and no-rank
  policies, boundaries, missing/not-applicable and two different programmes.
  National-preset tests separately cover high/low/failed fourth subject, main
  failure, GPA cap and paper combination.
- Compare each supported policy with manually checked examples from that
  school/programme; a responsible academic lead signs the discrepancy report.
- Recomputing an old publication after a policy change leaves its issued
  snapshot unchanged.

### EX-04 — Configurable assessment components and subject aggregation

Depends on: EX-02/03. Do not hard-code a universal `33% per part`, a fixed
CQ/MCQ/practical split, or an assumption that every school uses exam papers.

Build:

- Each assessment may have zero or more configured components (for example
  written, MCQ, practical, coursework, oral, project or rubric criteria).
  Validate applicable ranges, weights and requirements before marks are entered.
  Some components are observations or rubric levels, not decimal scores.
- Mark grid supports component values with a calculated total, per-row errors,
  absence and optimistic versions. Persist the component breakdown in the
  publication snapshot and audit trail. Lock component edits after publishing.
- Aggregate explicitly linked assessments into a course/subject outcome only
  where the selected policy requires it. A national two-paper subject may
  contribute once to GPA; another programme may use coursework plus an exam or
  no GPA at all. Display source assessments as approved by the school.
- Editing a schedule/component scheme after marks exist needs a controlled
  migration or is blocked; never reinterpret existing marks silently.

Tests and gate:

- Total passes but a required component fails **where that policy requires a
  component pass**; rubric levels; missing component; absence; oversized or
  tampered score; wrong-school component.
- National two-paper combination produces one grade point; an international or
  school-defined weighted coursework/exam example follows its own policy.
  Existing single-score papers are unaffected.
- Printed documents reproduce the signed sample calculations exactly.

### EX-05 — Review, aggregation and controlled release

Depends on: EX-01 through EX-04. Only implement term/annual or other assessment
weighting after a school has supplied its actual formula and exceptions.

Build:

- Pre-publish checklist: eligible-student count, missing/absent marks, unusual
  scores, grade-policy version, sample card, ranking cohort and approver.
- Distinguish `draft`, `ready/reviewed`, `published` and, if required, scheduled
  release. Guard every parent view, export, PDF URL and SMS against early access.
- Store configured period/assessment weights and publish a separate aggregate
  version with its source versions. Support semester, trimester, term or custom
  periods rather than assuming a Bangladeshi half-yearly/annual pair. Do not
  silently mutate source exams. A correction invalidates or deliberately
  supersedes the aggregate according to an explicit policy.
- If a programme uses external awards, provide a separately permissioned,
  validated import of official outcomes, preserving the original issuing body
  and evidence reference. School-issued progress cards must not masquerade as
  official board/awarding-body certificates.
- Record who reviewed and published, when, and what policy/data version was
  approved. Keep one publication transaction and one notification event.

Tests and gate:

- Signed weight and rubric examples, absence/incomplete cases and source
  correction behaviour; no double counting of combined assessments or bonuses.
- Family and public endpoints cannot obtain drafts or scheduled results early.
- Two simultaneous publish attempts produce one version/notification set.

### EX-06 — Stable, authentic, programme-approved report cards

Depends on: EX-03/05 for policy and versioned result payload. Can prototype
layouts earlier, but do not call the final PDF verified until these gates pass.

Build:

- Snapshot **every field printed** at release: student identity, year/class/
  section/roll, subject labels and marks, policy, rank, attendance with an
  explicit cut-off, school identity and approved signatory/remarks. Changes to
  live attendance or master data must not rewrite an issued version.
- Define a canonical card payload or signed document digest and a privacy-safe
  public verification view. A copied valid URL must not make a changed PDF look
  genuine. Old versions must clearly display superseded/current status.
- Provide a small set of school-approved report templates selected by
  programme/level, with configurable labels and Bangla/English presentation
  where needed. Do not force a GPA/rank table onto a rubric or narrative card.
  Defer an open-ended designer. Include teacher remarks only after approval and
  visibility rules are set. QR is useful only once it points to meaningful
  content verification.
- Human print review: long names (including Bangla), many subjects, page
  breaks, logo, signatures, margins, A4/low-cost printer, grayscale and mobile
  PDF download. Compare single and bulk cards byte/content-wise where feasible.

Tests and gate:

- Same snapshot version renders the same content after live identity,
  attendance, school settings or current enrolment changes.
- Forged/altered payload is not reported authentic; unknown and superseded
  versions have correct public responses without exposing unnecessary child data.
- Guardian sees only linked children; teacher historical-card and bulk-card
  access use one documented exam-year policy.
- Two school staff sign off representative printed cards before pilot use.

### EX-07 — Policy-aware result analysis and exports

Depends on: EX-02 through EX-06. Analytics must consume the same snapshot and
eligibility rules as report cards, not recalculate a second interpretation.

Build:

- Principal view: completion funnel, distributions **appropriate to the policy**,
  course/component or rubric-criterion weaknesses, cohort comparisons and
  period trends with denominators and programme labels shown. Teacher view:
  only assigned pupils/courses and actionable missing evidence or follow-up
  lists. Parent view: own child's progress only.
- Define rank/report cohorts explicitly; do not compare raw totals across
  unequal subject sets as if they were equivalent. Suppress or clearly label
  small cohorts where privacy would be compromised.
- Provide programme/school-approved class-result exports in CSV, XLSX and PDF,
  with relevant outcomes and policy/version identifiers. A board-style
  tabulation sheet is an **optional preset**, not the universal export. Offer
  safe filters for programme, class, cohort, section, shift, version and period
  where those exist.
- Profile representative sizes (for example 1,000 pupils and a full exam) and
  avoid per-pupil queries; cap bulk exports if needed.

Tests and gate:

- All report totals reconcile exactly to the published cards; filtering does
  not alter denominators silently; zero pupils and missing data are handled.
- Role, tenant and family access tests cover every export format.
- A principal uses the dashboard with a pilot school's real questions and can
  identify specific follow-up action, not just view charts.

### EX-08 — Pilot operations and the paid-offer decision

Depends on: G1 or G2 as appropriate. This is product work, not an optional
marketing afterthought.

Build:

- Import a school-approved Excel roster, subject choices and historic marks
  with preview, row-level validation, duplicate detection and rollback.
- Prepare onboarding instructions in the school's working language, mark-entry
  training, correction/release runbook, escalation contact, backups and a **tested
  restore**. Specify who signs off policy examples and card templates.
- Pilot with a deliberately small number of schools using explicitly supported
  programmes; include a materially different second programme before claiming
  broad curriculum flexibility. Record onboarding effort, publication time,
  correction rate, print rework, support tickets, guardian usage and willingness
  to pay. Do not call a free
  pilot proof of demand.
- Offer a narrow exam/results package first if schools already use another
  ERP. Keep annual support, SMS charges, migration, policy configuration and
  custom-card work explicit in a written proposal. Price is a hypothesis to
  test, not a figure inferred from competitors' ambiguous advertising.

Gate:

- At least one full real reporting cycle is completed **per supported
  programme** with written school approval, no unresolved correctness/privacy
  defect and a restore rehearsal.
- Document the actual buyer, budget, decision process and a paid commitment or
  clearly recorded reason for refusal before widening the product scope.

## Separate commercial tracks — do not block the exam core

- **Online fees:** the existing manual fee ledger is not an online gateway.
  Treat gateway integration as a separate work package only with a school and a
  licensed bank/gateway partner. Bangladesh Bank's
  [student-banking circular](https://www.bb.org.bd/mediaroom/circulars/finincld/feb092026fid01e.pdf)
  describes fee collection via bank arrangements and digital channels, while
  its [PSO/PSP guidance](https://www.bb.org.bd/en/index.php/financialsystems/paysystems)
  addresses licensing for payment services. Use hosted checkout, authenticated
  callbacks, idempotent intents, reconciliation and refunds; have local counsel
  review the exact funds flow. Do not infer that Bangladesh Bank governs grades.
- **Later discovery:** PWA/native app, biometric import, website, admission,
  certificates, eSIF exports and other regulatory reports need a real buyer and
  validated specification. Do not claim interoperability with a government
  portal or legal compliance until tested and reviewed. Avoid storing extra
  child/parent identity data merely because an unverified market checklist says
  to collect it.

## Operating contract for Claude or another implementing agent

1. Work in order and commit **one accepted WP at a time** as `EX-00: ...`, etc.
   Since the working tree is currently dirty, first coordinate its ownership;
   preserve all existing changes and never reset them to make a package easier.
2. Before each WP, write its decision record: policy source, effective year,
   supported cohort, migration effect, access rule and negative cases. If a
   school choice or regulatory source is missing, stop that WP and complete safe
   independent work; do not invent the rule.
3. Keep calculation in a pure grading service and use one eligibility service
   everywhere. Tenant checks, transactional writes, versioned snapshots,
   audit logs and published-mark DB locks are non-negotiable invariants.
4. Never edit/delete a published `ResultSnapshot`. Changes make a new version;
   notifications are sent on commit and deduplicated. Old result policy and
   displayed card fields remain reproducible.
5. Each WP adds positive, negative, tenant/role, migration and regression tests.
   Keep HTML, PDF, CSV and XLSX outputs consistent. Update `README.md`,
   `docs/STATUS.md`, access matrix and market-status claims when the feature is
   actually accepted, not when scaffolding exists.
6. At each gate run `manage.py check`, `makemigrations --check --dry-run`, the
   full pytest suite, Ruff check/format, and deployment checks. Use the real
   browser and a human PDF print review for G1/G2; verify PostgreSQL-specific
   constraints/triggers in the deployment environment.
7. A failed gate blocks the next dependent WP and external claims. Report exact
   failed scenarios and what remains unsupported; do not hide them behind a
   green aggregate test count.
