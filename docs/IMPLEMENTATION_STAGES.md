# Staged implementation plan

23 September 2026. This plan turns the [market and compliance review](MARKET_AND_COMPLIANCE_REVIEW.md)
and Codex's [exam and results plan](EXAM_RESULTS_IMPLEMENTATION_PLAN.md) into controlled
stages. Each stage has:

- a fixed scope;
- tests that must pass before it closes;
- a full quality gate.

A stage that fails its gate blocks the stages that depend on it.

**Target market, as set by the owner:** English-medium schools in Bangladesh following
Cambridge, Pearson Edexcel and the International Baccalaureate, with the Bangladesh national
curriculum as one more configuration, not the product's definition. Everything is configured per
school, per class and per exam. No school's rules are forced on another.

## 1. Evaluation of Codex's plan

Codex's plan was read in full and checked against the code and the owner's decisions. Parts
are adopted, parts adapted, and parts set aside, each with the reason.

### Adopted

- **A curriculum-neutral results engine with named rulebooks.** With English-medium schools as
  the primary market, this is the core of the product, not an extra. A class's results follow
  the rulebook chosen for it. The Bangladesh board rules (GPA, 4th subject, combined papers) are
  one preset, and they never touch a Cambridge, Edexcel or IB class.
- **Stages with gates, one at a time, each committed separately**, on the branch
  `feature/exam-results`.
- **Mark-entry safety first (its EX-01).** Every one of the review's findings was confirmed in
  the code before being fixed:
  - teachers could read another section's marks;
  - one correction created a version and a round of SMS per row;
  - clearing an absence did nothing;
  - cards read live data;
  - historical cards had two access rules.
- **One eligibility service everywhere.** One rule decides which papers a student sits, and it
  is used for mark entry, completeness, results, exports, admit cards, dashboards and
  analytics. Students in one class taking different subjects (IGCSE options, A Level choices,
  national groups) is normal. A paper a student does not sit is never "missing", "absent" or a
  zero.
- **Missing, absent and not-applicable are distinct.**
- **No invented numbers.** A rulebook that has no GPA, rank or pass/fail shows none. Positions
  are off by default except where a rulebook calls for them.
- **An internal estimate is never shown as an official award.** A school's mock grade or
  predicted grade is labelled as the school's; an official Cambridge, Edexcel or IB result is
  recorded as imported, with its source.
- **Everything a card prints is frozen at publication**, and old published results are never
  recalculated. A change makes a new version.
- **Keep what the teacher typed when a grid submission fails.**
- **No compliance claims until a school signs off.** Claims wait for sign-off; code does not.
- **Label competitor claims as vendor claims** in the market review.
- **Existing classes keep exactly their old behaviour** until someone chooses a rulebook. The
  default rulebook is the school's own rules, which is how the system has always calculated.

### Adapted

- **"School-approved fixtures before any code" (its EX-00).** There is no school yet; the
  owner's mentor will introduce one after a demonstration. So each rulebook is built from the
  awarding body's published rules, with worked examples as tests, and marked *unverified*.
  School sign-off is the gate for real use (its G1), not for writing code.
- **Open-ended rubric and narrative reporting.** Criterion scores (IB MYP) and grade-only
  reporting (Cambridge, Edexcel) are in scope now. Free narrative-only reporting, such as IB PYP
  and early years, is left for when a school needs it; teacher comments are supported
  throughout.
- **PostgreSQL concurrency tests.** Corrections are serialised by a row lock on the exam. CI has
  no PostgreSQL service yet; adding one is follow-up work.

### Set aside

- **Deferring certificates, candidate data exports, online fees and the Bangla interface to
  "later discovery".** The owner asked for these in the next build; only the parent app is
  deferred. They are ordered after the results core. Where they collect data, every new field is
  optional, so a school collects only what it uses.

## 1b. Evaluation of Codex's second review (23 September 2026)

Codex reviewed the plan against Cambridge, Pearson and IB guidance. It read an earlier state
of the code: several findings were already fixed by S2–S6, committed before the review.
Every finding was checked against the committed code or the published source.

| Finding | Verdict | Action |
|---|---|---|
| The numeric pass/fail and GPA path is used for English-medium and IB | **Out of date.** Since S2, Cambridge, Edexcel, MYP and DP classes use their own rulebooks with no pass mark, GPA or overall pass/fail, and the card prints GPA only where the rulebook has one. The `grading.py` docstring still said otherwise | Docstring corrected |
| Configure the programme per school, class and exam, not one school-wide switch | **Already done** (exam, then class, then school); per-subject qualification only through a paper's own scale | Series and entries modelled in S6b |
| Keep internal assessment, school predictions and official awards apart | **Valid, partly done.** Estimates are separate, dated and approved (S5). But a Cambridge card printed "Worked out by: Cambridge", which can read as official, and there is no record of official results | Fixed now: relabelled as school assessments, and every card except the school's own rules states that official results come only from the awarding body. S6b adds an official results register |
| QR verification must not imply it authenticates an awarding-body certificate | **Valid** | The verification page now says so; S7 certificates will too |
| Grade presets must be qualified as internal grading | **Already stated** on the presets screen; now also on every card | None |
| Evidence models: grade-only, MYP criteria, narrative, exempt and not-applicable, optional GPA and rank | Grade-only, criteria, not-applicable and optional GPA and rank **done**; narrative is per-subject comments (S5); **exempt** is missing; moderation is missing | Exempt added to S8; moderation left for a school that asks |
| Exam-officer workflow: centre identifiers, candidates, entries, options, tiers, HL/SL, series, clashes, access arrangements, release dates, post-results cases | **Valid.** This shapes the data design, so it must not wait for S10 | New S6b (series, entries, official results) before certificates; access arrangements in S10 with restricted access |
| Edexcel modular: units, UMS, cash-in | **Valid, already deferred** until a school runs modular International A Levels | Stays deferred; the official register records unit and overall awards as imported |
| IB: name PYP, MYP, DP and CP separately; a diploma is not "24 points" | DP applies all eight conditions and is labelled a school estimate (S2b). **PYP and CP are not supported** | Stated as unsupported below |
| Privacy is a launch requirement | **Valid** | New S12b privacy baseline gates any real school data |
| Data protection law dates and hosting | **Codex is right; our review was wrong.** Checked against the Act on bdlaws.minlaw.gov.bd: in force from 6 November 2025 except sections 23 and 31–35; foreign transfer is conditional, not banned | Market review corrected |
| Public lookup by student ID and date of birth | **Valid.** Dates of birth are guessable | S11 redesigned: a random lookup code printed on the card, rate limited, off by default |
| Never advertise as certified by Cambridge, Pearson or the IB | **Agreed**; already in section 4 | None |

**Not supported, stated plainly:** IB PYP narrative reporting, the IB Career-related
Programme, Edexcel modular UMS and cash-in, moderation workflows, and submitting entries to an
awarding body. The system keeps the records; the exam officer submits through the board's own
portal.

## 1c. Codex's third review (23 September 2026)

All four findings were confirmed in the code and fixed, each with a regression test that fails
on the old code (`tests/test_review_fixes.py`).

| Finding | Fix |
|---|---|
| PDF cards printed the student's live name and section in the header | Single and bulk PDFs take both from the published result |
| An IB Diploma estimate did not check the programme's shape: five strong subjects, or no HL subjects, could read as "conditions met" | Exactly six subjects, each HL or SL, three or four at HL; otherwise the conditions are not met and the trace says why |
| Results did not record which rules and scales made them | Every result stores its rulebook, rules version, the exam scale's rules and any paper's own scale. Papers on their own scale are now frozen at first publication like the exam's scale. Before this, editing such a scale and then publishing an unrelated correction silently regraded that paper for everyone |
| A 9-1 subject inside a Cambridge exam was ordered by whichever grade appeared first | Each subject in the grade distribution is ordered by its own scale; a grade from another scale is left blank on that line |

The official-results import Codex also points to is stage S6b, next in order.

## 2. Decisions record

| Decision | Choice | Status |
|---|---|---|
| Which rulebook a result uses | The exam's setting if given, else the class's, else the school's | Decided |
| Default rulebook | The school's own rules: percentage and grade per paper, the system's original behaviour | Decided |
| Rulebooks planned | `own`; `national` (Bangladesh SSC board); `cambridge` and `edexcel` (a grade per subject from the board's scale, no GPA); `ib_myp` (criteria to 1–7); `ib_dp` (1–7 per subject plus core points, as a school estimate) | Decided; built in S2 |
| Which papers a student sits | The class's subject plan for the year: compulsory rows (for the student's group, where groups exist), chosen subjects, a 4th subject (national only), religion papers by religion. A class with no plan: every paper | Decided |
| National GPA | Main-subject average; 4th subject adds points above 2.00, outside the divisor, never fails; any failed main subject gives FAIL; capped at 5.00 | Decided; **confirm with a school** |
| National GPA rounding | Two decimals, half up | **Unverified** |
| National two-paper subjects | Graded once on combined marks; each part against the summed pass marks | **Unverified** |
| Positions (rank) | Shown for `own` and `national`; off for the other rulebooks unless an exam turns them on | Decided |
| Correction publishing | One new version per saved batch; unchanged rows not saved; only families whose own result changed are notified | Decided |
| Clearing a mark | A blank row clears a mark before publication; after publication it is refused | Decided |
| What verification proves | This school published this result in this version, and whether it is current. Shows initials, class, roll, overall result and fingerprint; never the full name or subject marks | Decided |
| Historical card access | Managers: all. Families: own children. Teachers: sections they taught in the exam's year | Decided |
| Old snapshots | Never rewritten; older payloads still render | Decided |

## 3. Stages

Each stage closes only when its tests pass and the full gate passes:

- `manage.py check`
- `makemigrations --check --dry-run`
- `ruff check` and `ruff format --check`
- the full test suite
- `check --deploy` with production settings
- a regenerated access matrix

| Stage | Goal | Scope | Must be proven by tests |
|---|---|---|---|
| **S0 · Baseline and schema** · *done* | Reconcile in-progress work; record decisions | Review uncommitted models and migrations; migrate a copy of the development database; this document | Migration runs on a copy of real data with the marks lock restored; old classes unchanged |
| **S1 · Safe mark entry and publishing** (Codex EX-01) · *done* | No wrong reads, no version or SMS storms, stable cards | Section-level authorisation; clear-to-unentered; unchanged rows skipped; one snapshot per batch; change-only notifications; typed values kept on error; frozen card fields; fingerprint; defined verification; one historical-access rule | The review's four scenarios; repeated POST makes no new version; stale version rejects the batch; two prints identical |
| **S2 · Rulebooks** (Codex EX-03) · *done: S2a Cambridge, Edexcel, MYP; S2b IB Diploma* | Results correct for each programme | `cambridge`, `edexcel`, `ib_myp`, `ib_dp` beside `own` and `national`; grade scale presets (Cambridge IGCSE A*–G, O Level A*–E, AS/A Level A*–E, Edexcel International GCSE 9–1, International A Level A*–E, IB 1–7, MYP criterion boundaries); per-paper scale override; weighted paper parts; rank optional; staff-only calculation trace | Worked examples per rulebook; board rules never applied outside `national`; `own` unchanged |
| **S3 · Subjects per student everywhere** (Codex EX-02) · *done* | Mixed subject choices work end to end | Eligibility used by grid, completeness, results, exports, admit cards, exam progress, teacher dashboard; subject-plan setup with presets (IGCSE option blocks, national Class 9–10); choices screen with an exception list | Mixed choices publish; not-sat papers never missing; tampering refused |
| **S4 · Paper parts setup** (Codex EX-04) · *done* | Parts configured without hand-editing | Parts editor with presets (Cambridge weighted papers, MYP criteria A–D, national creative/MCQ/practical); locked once marks exist or results publish | Parts validate; weights reproduce the published weighting; editing after marks blocked |
| **S5 · Report cards per rulebook** (Codex EX-06) · *done* | Cards a school would hand out | Templates for grade-only, IB and national cards; teacher comment per subject; effort grade; predicted grade recorded separately and labelled as the school's; attendance with a stated cut-off | Each template renders from the snapshot; no GPA or rank where the rulebook has none; comments frozen |
| **S6 · Class result exports** (Codex EX-07) · *done* | What heads and exam officers print | Grade distribution per subject; class results sheet; national tabulation sheet; optional merit lists; every export carries exam, version and rulebook | Totals reconcile to published cards; filters keep denominators honest |
| **S6b · Exam series, entries and official results** (from Codex's second review) · *done* | Exam officers' records; official results kept apart | Exam series (board and session, such as Cambridge June 2027); per-candidate entries with syllabus or unit code, option, tier or HL/SL, candidate number; an official results register imported from the board's statement of results, with source, date and amendment history; cards never mix official and school results | An imported result is shown as official with its source; an amendment keeps the old value; school grades never appear as official |
| **S7 · Certificates** · *done* | Leaving and character certificates, clearly the school's own and never an awarding body's | Transfer or leaving certificate and character certificate (testimonial); issue, freeze, revoke, reissue; QR verification; English and Bangla | Frozen after issue; revoked shows revoked; access by role |
| **S8 · Release control and term results** (Codex EX-05) · *done* | Review before publishing; term weights; promotion | Pre-publish checklist; an exempt state for a paper a student is excused from; combined results with configured weights, published as their own version; promotion from results with override | Weighted examples; a source correction marks the combined result out of date |
| **S9 · Bangla and English** · *done: family screens, navigation and report card; staff screens next* | Language toggle for families who want Bangla | School switch; per-person toggle; translated screens | Toggle persists; English unchanged |
| **S10 · Candidate and registration data** · *done* | Exports exam officers need | Optional identity fields; Cambridge and Edexcel candidate list; national eSIF and unique-ID export; data-check list | Format checks; nothing required that a school does not use |
| **S11 · Public result lookup** · *done* | Families check without signing in | Off by default; a random lookup code printed on the card (never student ID plus date of birth, which is guessable); rate limited; published only; shows no more than the card | Drafts never visible; wrong code refused; repeated guesses throttled |
| **S12 · Online fees and VAT** (separate track) | Demonstrable online payment; correct VAT | Demonstration gateway (off in production unless allowed); SSLCommerz sandbox; verified callbacks; idempotency; 5% VAT on English-medium tuition as a fee-head setting | Replayed callback posts once; tampered amount refused; VAT computed and posted |
| **S12b · Privacy baseline** (gate for real school data) | Lawful handling of children's data | Guardian consent records (Act section 9); classification of stored fields; restricted access and access logs for sensitive records (access arrangements, health, identity numbers); retention schedule and deletion; hosting and transfer review with counsel (section 29) | Consent recorded before sensitive data; restricted records hidden from other roles; deletion leaves no orphaned personal data |
| **S13 · Demonstration and hand-over** | Ready for the mentor | Seed a Cambridge IGCSE class with options and weighted papers, an IB class, and a national Class 9; documentation; print checklist | Seed repeatable; full gate on the final commit |

**Deferred by the owner:** the parent app.

### Scope changes from the English-medium research

[The research](ENGLISH_MEDIUM_RESEARCH.md) verified or corrected several points. The stages
absorb them as follows:

- **S2 (done):**
  - Cambridge Physics 0625's 30/50/20 weighting from raw 40/80/40 is the weighted-paper test.
  - The MYP boundaries and the DP core matrix and eight conditions are verified and tested.
  - AS Level has its own scale, a–e in lower case with no a*.
- **S4 (done):**
  - Tiers cap the grade a paper can earn (Cambridge Core C–G, 9–1 Core 5–1), so a paper gets an
    optional maximum grade.
  - Internal thresholds can be set per exam (a scale per mock) to mirror per-series thresholds.
- **S5 (done):** predicted, forecast and target grades are separate records, dated and approved, never
  calculated from averages. Effort and approaches-to-learning comments sit beside attainment.
- **S10:** the Cambridge and Pearson candidate export carries:
  - the name as it should print on the certificate, up to 60 characters;
  - date of birth as dd/mm/yyyy;
  - a 4-digit candidate number and a UCI;
  - syllabus, option or entry codes.
- **S12:** VAT is a per-fee-head rate that defaults to none. The 5% rate could not be verified,
  and the courts have ruled on it before, so the school sets it on its tax adviser's advice.
- **Later, when a school needs them:**
  - Edexcel International A Level UMS cash-in and its A* rule;
  - PYP narrative reports.

## 4. What only a school can sign off

These are release gates for real use, not for the demonstration:

1. Worked examples from the school's own past internal results, per programme it runs:
   - a weighted Cambridge or Edexcel subject;
   - an MYP criterion total;
   - a DP points total with core points;
   - for a national stream, GPA with a 4th subject.
2. The grade thresholds the school uses for internal exams. Cambridge and Edexcel set official
   thresholds per exam series, so internal exams use the school's own.
3. Report card, class sheet and certificate layouts, printed on the school's printer.
4. Whether the school shows positions, and who approves publication.
5. The items marked unverified in the decisions record.

Until those are signed, describe the results module as "configurable for Cambridge, Edexcel,
IB and national-curriculum reporting, pending verification with your school". Never describe
it as compliant or certified by any awarding body.
