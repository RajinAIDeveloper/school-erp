# Market, compliance and product review

> **Scope update, 23 September 2026.** The owner's primary market is English-medium schools
> following Cambridge, Pearson Edexcel and the IB; the Bangladesh national curriculum is one
> configuration among several, chosen per school, class or exam. This review was written for a
> national-board-first product. Its competitor list covers national-board ERPs only. Its
> national-curriculum rules still apply wherever that rulebook is chosen. The build order now
> follows [the staged plan](IMPLEMENTATION_STAGES.md). Competitor features and prices below are
> **the vendors' own published claims**, not verified behaviour.

23 September 2026. What the system is, how its examination and results side compares with
systems used worldwide and with Bangladeshi competitors, which Bangladeshi rules it must
follow, what to build next, and whether it is ready to market.

Everything about this codebase was checked in the code. Everything about competitors and
regulations comes from web research on the date above; each claim carries its source, and
the confidence is stated where a source was secondary or could not be opened.

---

## 1. The short answer

**The system is technically strong and commercially not yet ready.** It is better built than
anything its competitors show publicly, particularly in results integrity, access control,
accounting and privacy. It is missing several things that Bangladeshi schools treat as basic,
and it gets one board grading rule wrong.

**One correctness defect matters more than every missing feature.** GPA is averaged over every
subject, including the optional 4th subject. The board rule excludes the 4th subject from the
divisor, adds only its grade points above 2.00, and never lets it fail a student. On any class
with a 4th subject — which is every Class 9 and 10 student — the system currently prints the
wrong GPA, and a failed 4th subject wrongly makes the whole result a fail. A school would find
this on its first annual result.

**One design limit blocks secondary schools entirely.** Publication requires every enrolled
student to have a mark for every paper scheduled for the class. Since September 2024 Classes
9–10 are split into Science, Business Studies and Humanities with per-student electives
([Daily Star](https://www.thedailystar.net/news/bangladesh/education/news/secondary-edn-science-arts-commerce-groups-return-3692371)).
A Humanities student never sits Physics, so a Class 9 exam can never be published.

**Readiness:** not ready for a public launch; ready for a closed pilot with Play–Class 8
schools now, and with secondary schools once the grading engine is fixed. Section 8 has the
plan. The timing is good: Bangladeshi schools choose software between October and December
for a January session, and annual results are processed in December.

---

## 2. The examination and results module today

### What it does (verified in code)

| Area | What exists |
|---|---|
| Setup | Exams per academic year and optional term; papers per class and subject with date, time, room, full and pass marks; configurable grade scales with the Bangladesh A+–F scale as default |
| Mark entry | A whole section on one screen; every row validated before any is saved; optimistic locking so two teachers cannot overwrite each other; absent recorded explicitly; teachers limited to their assigned subject and section |
| Results | Totals, percentage, GPA, pass/fail, section rank and class rank with ties shared, subject analysis (average, highest, lowest, pass rate) |
| Publication | Publishing requires complete marks, then takes a **versioned snapshot** of every student's result. Marks are then locked **at the database level** by triggers on SQLite and PostgreSQL, not only in the application |
| Corrections | A teacher requests an unlock with a reason; a head approves it for 48 hours; the correction produces a new published version; the old version is kept and marked superseded; every step is audited |
| Verification | Each published card carries a link to a public page that confirms the school, the examination, the version and whether it is still current, while naming no student and showing no marks |
| Documents | Report cards on the school letterhead with attendance and the Bangla name (bundled Noto Sans Bengali), bulk cards for a section, admit cards, examination routine, progress report across exams; exports to CSV, Excel and PDF |
| Families | Guardians and students see only published results of their own children; optional SMS on publication, deduplicated per student and version |

### What it does not do (verified in code)

- **The 4th-subject GPA rule** (the defect above). `Subject.is_optional` exists but the results
  engine ignores it.
- **Groups and per-student subject choice.** There is no record of which subjects a student
  takes, so every student is expected to sit every paper of the class.
- **Subject components.** One mark per paper. There is no creative / MCQ / practical split
  and no separate pass mark per component.
- **Combined subjects.** Bangla 1st and 2nd paper, and English 1st and 2nd paper, cannot be
  combined into one subject for GPA.
- **Combined term results.** No weighted half-yearly plus annual result, and no continuous
  assessment such as class tests.
- **A board-format tabulation sheet**, and merit lists by group, shift or version. The result
  sheet export is close to a tabulation sheet but not in board layout.
- **Remarks and co-curricular ratings on the card.** `Mark.remarks` is stored but never
  printed; there is no class-teacher comment or conduct rating.
- **Testimonials and transfer certificates.**
- **Promotion from results.** Promotion moves a whole section regardless of pass or fail.
- **A QR code on the card.** The verification link is printed as text.
- **Bangla report cards.** Only the student's name prints in Bangla; everything else is English.

---

## 3. Against systems used worldwide

Compared with PowerSchool, Infinite Campus, Blackbaud, Veracross, Arbor, iSAMS, Fedena,
OpenEduCat, Gibbon, Classe365 and Entab (vendor documentation; full list of sources at the end).

| Capability | Typical of mature systems | This system |
|---|---|---|
| Weighted gradebook rolling class tests and terms into annual results | Table stakes (PowerSchool, Infinite Campus, Fedena "derived exams") | **Missing** |
| Multi-component subjects with separate pass rules | South Asian systems (OpenEduCat for CBSE; Fedena partly) | **Missing** |
| Configurable grade scales | Table stakes | **Yes** |
| Board-specific rules (best-of, optional subject) | Fedena, OpenEduCat for Indian boards. The Bangladeshi 4th-subject rule was found in **no** global system | **Missing, and currently wrong** |
| Report card layout options or designer | Blackbaud drag-and-drop; Arbor, Fedena, Classe365 configurable | Fixed layout |
| Teacher comments, comment banks, co-curricular ratings | Table stakes (Arbor, iSAMS, OpenEduCat) | **Missing** |
| Approval, locking and corrections | Arbor approve-and-lock (reversible by any approver, no version history); Gibbon overwrites regenerated PDFs; PowerSchool logs changes only optionally | **Stronger than all of them** — locked in the database, versioned, audited, superseded versions kept |
| Verification of issued documents | Only OpenEduCat claims QR verification with versioning, on a marketing page its documentation does not support | **Yes**, and documented — a genuine differentiator |
| Transcripts, transfer certificates | Table stakes (PowerSchool, Blackbaud, Fedena's TC generator) | **Missing** |
| Analytics and early warning | Subject and class reports are table stakes; Infinite Campus has predictive early warning | Subject analysis and progress report only |
| Parent access and notification | Portal plus mobile app is table stakes | Web portal and SMS; no app |
| Promotion from results | No system found does this automatically; Fedena's is manual with an undo | Manual, like everyone else |
| Online exams, question banks | Fedena, Entab | Missing (not expected of a Bangladeshi ERP) |
| AI assistance | Shipped only where a human reviews it: Microsoft Teams feedback, PowerSchool PowerBuddy | None |
| Interoperability (OneRoster, LMS grade passback) | PowerSchool, Infinite Campus | CSV and Excel only; no API |
| Bilingual report cards | Infinite Campus (paid add-on) | Name only |

**Where it stands:** in integrity and verification it is ahead of the best-known systems in the
world. In flexibility — weighting, components, layouts, comments, transcripts — it is behind
even the South Asian mid-market (Fedena, OpenEduCat). The research found "versioned, locked,
verifiable published results" to be rare everywhere and especially valuable in South Asia, where
forged marksheets and certificates are a known problem.

---

## 4. Against Bangladeshi competitors

About twenty products were found. The ones that publish enough to compare:

| Product | Strength | Result depth (as published) | Price (as published) |
|---|---|---|---|
| **Eduman** (Netizen) | Market leader: claims 5,000+ institutions, 10+ years, money-back guarantee | "Result processing within 1 day" | Tk 7–10 per student per month, Tk 100 per student per year, setup Tk 5,000–10,000 |
| **DigiCampus BD** | 13 modules, biometric, bKash/Nagad/Rocket, Android app | Merit list; details not public | Tk 10 / 12 / 14 per month (unit presumed per student) |
| **Bidyaan** | Library, transport, hostel; bundles biometric and face devices | Not detailed | Tk 10–20 per student per month |
| **Edufy** | Separate guardian, teacher and admin apps | "Auto GPA" | Tk 2,000–40,000 per month by size, plus setup and 5% VAT |
| **EduZone** | Cheapest published | Not detailed | Tk 5 per student per month, or Tk 12,000–49,999 per year |
| **SmartSchool.bd** | Branded app, marksheets, certificates | Marksheets, admit cards | Tk 6,000–24,000 per year |
| **PRS Result Software** | Deepest result processing found: NCTB 4th subject, merit lists, tabulation, combined half-yearly and annual, bilingual, TC and testimonial, seat plans, public result portal | Board-accurate | Monthly; figure not public |
| **Gradrix** | Result specialist: CQ/MCQ/practical/continuous components, groups, merit by shift/group/version, tabulation | Board-accurate | Quoted per exam-registered student |

Sources: [Eduman](https://edumanbd.com/features/), [DigiCampus](https://digicampusbd.com/),
[Bidyaan](https://www.bidyaan.com/pricing), [Edufy](https://edufy.com.bd/pricing),
[EduZone](https://eduzone.com.bd/price), [SmartSchool.bd](https://smartschool.bd/),
[PRS](https://perfectnresults.com/), [Gradrix](https://gradrix.com/features/).

**Table stakes in Bangladesh** (nearly every competitor): Bangla interface, bKash/Nagad/Rocket
payment, an Android app (often white-labelled), SMS to guardians, a parent portal, a bundled
school website, admit cards, merit lists, and usually biometric attendance.

**What leaders differentiate on:** scale and trust (Eduman), board-accurate results (PRS,
Gradrix), bundled hardware (Bidyaan), madrasa departments (eSchool), payment partnerships.

**Observed weaknesses across the market:** about half publish no price; most describe results
only as "result processing"; public reviews are almost nonexistent; some client figures are
identical across two vendors; on-site support is regional. The government's own assessment app,
Noipunno, went down in January 2024 and was dropped after the curriculum reversal. **There is
no free government ERP to compete against**, but schools must still use the boards' eSIF portal
for Class 9 registration alongside any ERP.

**Where this system stands locally:**

- **Ahead of everyone** on verifiable, versioned results; audit trail; real double-entry
  accounting with reports, period lock and payroll reversal; role scoping and privacy; SMS that
  cannot double-text a family; sibling handling.
- **Behind the result specialists** (PRS, Gradrix) on board accuracy: 4th subject, components,
  groups, tabulation, combined results, certificates.
- **Behind the full ERPs** (Eduman, DigiCampus, Bidyaan) on table stakes: no Bangla interface,
  no online payment, no app, no biometric, no website, no shifts or versions.

---

## 5. Bangladeshi rules the system must follow

| Rule | What it requires of the software | Status here | Source |
|---|---|---|---|
| **Curriculum 2026**: Classes 6–10 back on the 2012 curriculum with marks and GPA; primary stays on the newer curriculum; new subjects in 2027, a new curriculum expected in 2028 | Marks-based assessment for 6–10; curriculum and subject set configurable per class and year | Marks-based: yes. Configurable subject sets per year: partly (subjects are per class, not per year) | [Daily Star](https://www.thedailystar.net/news/bangladesh/education/news/secondary-edn-science-arts-commerce-groups-return-3692371), [Bangladesh Post](https://bangladeshpost.net/posts/govt-to-introduce-new-school-curriculum-in-2028-169804) |
| **GPA**: A+ 80–100 (5.00) to F 0–32; 4th subject adds GP above 2.00, excluded from divisor, cannot fail; one F in a compulsory subject = fail | The exact formula; 1st and 2nd papers combined | **Scale correct; 4th-subject rule wrong; paper combining missing** | [Education Board grading](http://www.educationboard.gov.bd/computer/grading_system.php) |
| **Components**: without practical, 70 written (50 creative + 20 short) + 30 MCQ; with practical, 75 theory + 25 practical; 33% needed in each part (reported, regulation text not found) | Component table per subject; pass check per component | **Missing** | [Prothom Alo](https://en.prothomalo.com/youth/education/e4ln52af2b) |
| **Groups** from Class 9: Science, Business Studies, Humanities; 4th subject and religion per student | Per-student group and subject choice | **Missing** | Daily Star (above) |
| **Board registration (eSIF)**: birth registration number, names in Bangla and English matching the birth record, father's and mother's names, parents' NIDs, 300×300 photo, group, subject codes | Capture and check these at admission; export in portal order | Birth registration number and Bangla name: yes. **Separate father's and mother's names, parents' NIDs, English-name matching, subject codes, export: missing** | [Chittagong board eSIF](https://esifssc.bise-ctg.gov.bd/) |
| **Admission policy**: Class 1 lottery; Classes 2–9 back to admission tests from 2027; quotas (catchment 40%, freedom fighters 5%, special needs 2%, twins 2%, siblings 3%, ministry 1%); age limits; 55 per section | Quota category, lottery import, admission-test marks, age check | **Missing** | [Views Bangladesh](https://viewsbangladesh.com/new-school-admission-policy-released-63-quota/), [Daily Star](https://www.thedailystar.net/news/bangladesh/education/news/no-admission-test-class-1-2027-lottery-system-continue-4241566) |
| **Fee caps** by area and MPO status for admission and annual fees; no re-admission fee; scholarship holders exempt from tuition (DSHE, May 2026); fees deposited in a scheduled bank | Fee heads tagged by area and MPO status with cap checks; exemption flag | **Missing** (fees are free-form) | [Prothom Alo](https://en.prothomalo.com/youth/education/phpwt9j9sd), [BSS](https://www.bssnews.net/news/387108) |
| **Payments**: the Payment and Settlement Systems Act 2024 forbids holding public funds without a Bangladesh Bank licence | Integrate licensed gateways only; money settles straight to the school's bank; **the vendor never holds fees** | Manual recording only; the design is compatible | [Daily Star](https://www.thedailystar.net/news/bangladesh/politics/news/js-passes-payment-and-settlement-system-bill-3647316) |
| **Personal Data Protection Act 2026** (Act 63 of 2026, published 15 April 2026). *Corrected 23 September 2026 from the published text:* by section 1(3) the Act is deemed in force from **6 November 2025**, except section 23 (chief data officer) and sections 31–35 (complaints, penalties, compensation), which start on a date the government sets by gazette after 18 months. Section 9: a child's data only with a parent's or guardian's consent. Section 29: data is classed public, internal, confidential or restricted; transfer abroad is **conditional** (consent, a contract, or the person's education with consent), only to countries meeting standards set by regulation, and large transfers of sensitive identifiable data must be notified to the Authority. It is not a blanket requirement to host in Bangladesh. Have Bangladesh counsel review the actual deployment | Guardian consent records, data classification, a hosting and transfer review, retention and deletion | Access control, audit logs, private media, no credentials stored readably: **yes**. Consent records, retention schedule, Bangladesh hosting: **missing** | [Daily Star](https://www.thedailystar.net/tech-startup/news/bangladeshs-personal-data-protection-ordinance-2025-key-takeaways-4015401), [TBS](https://www.tbsnews.net/bangladesh/govt-issues-gazettes-2-landmark-ordinances-data-protection-governance-1281356) |
| **BANBEIS annual survey**: every EIIN institution must file; 158 had their EIIN suspended in February 2026 for not filing | Enrolment by class, sex and age; staff; results summary | EIIN stored; **survey report missing** | [TBS](https://www.tbsnews.net/bangladesh/eiin-158-educational-institutions-temporarily-suspended-1372531) |
| **MPO staff**: MPO index number, NTRCA registration, pay scale; government and institution pay shares | Staff fields and a payroll split | **Missing** | [DSHE MPO policy](https://dshe.gov.bd/site/notices/635005b8-a231-477f-a21e-5992b0e9713d) |
| **Stipends** (HSP, PMEAT): historically at least 75% attendance and a minimum exam score (current thresholds not confirmed) | Attendance percentage per student, eligibility export | Attendance percentage: yes. **Eligibility report: missing** | [DSHE](https://dshe.gov.bd/site/notices/3d035280-c7ff-416b-a739-c47355f552bf) |
| **School holiday list**: DSHE 2026 list of 64 days, revised 22 February 2026; lunar dates move | Editable annual calendar | **Yes**; the 15 August change was fixed last round | [DSHE notices](https://dshe.gov.bd/pages/notices/) |
| **Transfer certificates**: boards publish sample forms | TC in board format | **Missing** | [Dinajpur board TC form](https://www.dinajpureducationboard.gov.bd/) |
| **VAT**: 5% on English-medium tuition, Bangla-medium exempt. **Vendor tax**: software and SaaS income tax-exempt until 30 June 2027 with BASIS membership and an annual certificate | VAT toggle on fee heads; vendor registers with BASIS | **Missing** | [CPD](https://cpd.org.bd/fiscal-policy-for-incentivising-education/), [Legalseba](https://legalseba.com/bd-resources/tax-exemption-for-information-technology-enabled-services-ites-in-bangladesh/) |

**Confidence notes.** The per-component 33% rule and the exact 4th-subject wording were found
only in secondary sources and match long-standing board practice. Current stipend thresholds
could not be confirmed. Whether the 2026 Act keeps mandatory breach notification is disputed
between sources. Check these with a board official or a lawyer before relying on them.

---

## 6. The whole system: strengths and weaknesses

### Strengths

- **Results you can prove.** Database-level lock, versioned snapshots, audited corrections,
  public verification. Rare worldwide and unmatched locally.
- **Real accounting.** Double-entry, trial balance, income statement, balance sheet, cash book,
  ledgers, opening balances, payroll, a period lock that holds on every path. Competitors say
  "accounting"; this one would satisfy an auditor.
- **Access control that follows ownership.** Teachers see this year's pupils in their own
  sections; families see only their children; a test fails if any screen becomes public by
  accident; the access matrix is generated and checked against the code.
- **Care with families.** Sibling discounts, one login per family, notices that name each
  child, SMS that cannot text twice, opt-out per guardian, credential messages wiped after
  delivery.
- **Engineering quality.** 548 automated tests, 92% coverage, a real browser test, linting and
  deployment checks in CI, backups with restore notes, Docker deployment.
- **Configurable where Bangladesh changes often.** Grade scales, holidays per year, weekends
  per school.

### Weaknesses

- **Wrong GPA for classes with a 4th subject**, and no groups — secondary schools cannot use
  the results module yet.
- **English-only interface.** Every competitor offers Bangla.
- **No online payment, no app, no biometric, no school website** — the four features most
  often advertised locally.
- **No shifts or versions** (morning/day, Bangla medium/English version), common in larger
  schools.
- **Not yet a hosted service.** Every record is school-scoped, but there is no self-service
  sign-up, per-school domain or central billing, and no Bangladesh hosting chosen.
- **Never used by a real school.** Every workflow is tested in code; none has survived a real
  headmaster, a real annual result or a real fee day.

---

## 7. What to build next

Ordered by what blocks sales and compliance. Estimates assume the current pace and quality
bar, including tests.

### Stage 1 — make secondary results correct (about 3–4 weeks). Blocks any secondary school.

1. **Board grading engine**: the 4th-subject rule; combining 1st and 2nd papers into one
   subject; per-component marks (creative, MCQ, practical) with a pass mark per component and
   overall; board subject codes. Keep it configurable per class and year, so 2027's new
   subjects and the 2028 curriculum are data changes, not code changes.
2. **Groups and per-student subjects**: group, elective, 4th subject and religion subject per
   student per year, with valid combinations per group. Publication then expects marks only
   for the papers a student actually takes.
3. **Combined results and promotion**: weighted half-yearly plus annual results; promotion
   driven by pass/fail with a manual override and an audit trail.
4. **Board-format documents**: tabulation sheet, merit lists by class, section, group, shift
   and version; testimonial and transfer certificate in the boards' sample format; a QR code
   on every card and certificate pointing at the existing verification page.

### Stage 2 — reach Bangladeshi table stakes (about 6–8 weeks). Blocks competitive sales.

5. **Bangla interface** (Django translation) and bilingual report cards and certificates.
6. **Board identity fields and exports**: separate father's and mother's names in Bangla and
   English, parents' NIDs, photo size check, English name matching the birth record; exports
   for eSIF registration and the Unique ID.
7. **Online fees through a licensed gateway** (SSLCommerz or bKash payment gateway), settling
   to the school's bank. The seam already exists: a verified callback calls `collect_payment`.
8. **Shifts and versions** across sections, merit lists and reports.
9. **Parent app**: start with an installable web app (PWA) on the existing portal, then a
   branded Android app on a small scoped API.
10. **Public result portal** by roll and registration number, with SMS on publication (the SMS
    exists).

### Stage 3 — compliance and administration (about 4–6 weeks).

11. **Data protection**: guardian consent records, data classification, retention and deletion,
    Bangladesh hosting, an incident procedure. Needed before about May 2027.
12. **Admissions**: quota categories, lottery import for Class 1, admission-test marks for
    Classes 2–9 from 2027, age checks.
13. **Fee policy**: fee heads tagged by area and MPO status with cap warnings, scholarship
    tuition exemption, VAT toggle for English-medium tuition.
14. **Government reports**: BANBEIS survey summary, stipend eligibility list, MPO staff fields
    and a government/institution pay split.
15. **Hosting as a service**: per-school subdomain, self-service trial, central billing.

### Stage 4 — differentiate (ongoing).

16. Remarks, co-curricular and conduct ratings, and a choice of report card layouts.
17. Teacher and subject performance analytics, at-risk flags from attendance and marks.
18. Biometric device import (the seam exists: a log import feeding the attendance service).
19. School website builder, library, transport.
20. Human-reviewed AI drafting of report-card comments, the only AI pattern the research found
    genuinely shipped in education.

---

## 8. Marketing plan

### Readiness

| Segment | Ready? | Why |
|---|---|---|
| Kindergartens and primary (Play–Class 5) | **Pilot now** | No 4th subject or groups; attendance, fees, accounts, SMS and report cards already work |
| Junior secondary (Classes 6–8) | **Pilot after item 1** | Needs component marks and combined papers |
| Secondary (Classes 9–10) | **After Stage 1** | Needs the 4th-subject rule and groups |
| General public launch | **After Stage 2** | Bangla interface and online payment are expected by every buyer |

### Positioning

**"Results you can prove, accounts you can trust."** Every competitor sells a list of modules.
This system can sell something none of them can show: a marksheet any college or employer can
verify, that cannot be quietly altered, with a full history of every correction; and books that
balance. Supporting messages:

- **Board-accurate grading**, including the 4th subject and components (once Stage 1 ships).
- **Your data stays in Bangladesh and is handled lawfully** under the 2026 data protection law.
- **Fees go straight to your bank.** We never hold your money.
- **Honest, published pricing** — half the market hides its prices.
- **No hardware lock-in** — use any biometric device, or none.

### Pricing (a starting hypothesis to test with pilots)

The market's published band is Tk 5–20 per student per month, with Eduman at Tk 7–10 and
DigiCampus at Tk 10–14.

- **Core ERP**: about Tk 8–10 per student per month, billed yearly with a discount for prepaying;
  no setup fee in the first year.
- **Results-only plan** for schools that already have an ERP: priced per student per
  examination, competing with PRS and Gradrix. This is the easiest door into a school.
- **SMS**: at cost, stated openly.
- **Pilot schools**: free until the end of the first term of 2027, in exchange for weekly
  feedback, a written testimonial and permission to publish a case study.

### Phases

**Phase A — foundations (now to mid-October, alongside Stage 1)**

- Register the business and join **BASIS** (required for the software income-tax exemption to
  June 2027).
- Choose a product name and a `.com.bd` domain.
- Stand up a demo school with realistic Bangla data, a landing page with published prices, a
  Facebook page and a WhatsApp Business number.
- Choose Bangladesh hosting.
- Prepare a Bangla brochure, a demo script and a comparison sheet against Eduman and DigiCampus.

**Phase B — pilot (mid-October to December)**

- Recruit **3 to 5 pilot schools**: one kindergarten, one Bangla-medium private school in Dhaka,
  one MPO school in a district town, one English-version school. Mixed segments expose
  different problems early.
- Offer **free annual-result processing for December 2026** to schools that are not pilots.
  Annual results are the moment a school feels its current system's pain, and the verifiable
  marksheet is the product's strongest demonstration. Do this only once Stage 1 is finished and
  checked against a real board result.
- Migrate each pilot's student list from Excel or the old system, and train staff on site.
- Track time to onboard, results published, fees recorded, and every support request.

**Phase C — launch for the January 2027 session (December to March)**

- Publish pilot case studies with numbers: hours saved on results, marksheets verified,
  collection rate.
- **Facebook and YouTube** in Bangla: short videos showing a result being published and
  verified, and a GPA calculator page including the 4th subject as a free tool that ranks in
  search.
- **District resellers**: local IT firms and computer shops on commission for the first year,
  since competitors win through field visits and regional presence.
- **Referral offer**: a free month for each school a customer refers (Eduman runs one).
- Headmasters' associations and teacher-training centres: short workshops on the 2027
  admission-test rules and the data protection law, with the product as the worked example.

**Phase D — scale (from April 2027)**

- The app and online payment launch as upgrades for existing schools.
- Apply to be listed as an education biller with bKash and Nagad.
- A data-protection readiness message ahead of the law taking effect in about May 2027.

### What to measure

Pilots signed and retained; days from contract to first result published; share of fees
recorded in the system; support requests per school per month; conversion from free result
processing to a paid plan; churn at renewal.

### Risks

- **Incumbent scale and price.** Eduman claims 5,000+ institutions at Tk 7–10. Compete on trust
  and accuracy, not on being cheapest.
- **Support capacity.** A school that cannot publish results on time will leave. Limit the
  number of pilots to what can be supported in person.
- **Curriculum churn.** New subjects in 2027 and a new curriculum in 2028. The configurable
  grading engine turns this into a selling point.
- **Payment licensing.** Never route fees through the vendor's account; use licensed gateways.
- **Correctness.** A wrong GPA on a real marksheet would end the product's reputation for
  exactly the thing it sells. Verify the Stage 1 engine against real published board results
  before any school uses it for secondary classes.

---

## Sources

**Global systems:** [PowerSchool grade calculation](https://ps.powerschool-docs.com/powerteacher-pro/latest/grade-calculation-types),
[PowerSchool stored grades](https://ps.powerschool-docs.com/pssis-admin/latest/historical-grades),
[Infinite Campus composite grading](https://content.infinitecampus.com/sis/latest/documentation/establish-composite-grading-rules/),
[Infinite Campus early warning](https://www.infinitecampus.com/products/campus-analytics-suite),
[Infinite Campus report translation](https://www.infinitecampus.com/products/premium-products/report-translation-module),
[Blackbaud report card builder](https://webfiles-sc1.blackbaud.com/files/support/helpfiles/education/teachers/content/sis-report-cards.html),
[Arbor approving and locking marks](https://support.arbor-education.com/hc/en-us/articles/4404467268497-Approving-marks-in-Arbor-Report-Cards-and-locking-summative-assessment-marksheets),
[Arbor AI features](https://support.arbor-education.com/hc/en-us/articles/35404957784989-Arbor-s-AI-Features),
[iSAMS reporting](https://www.isams.com/platform/modules/school-reporting/),
[Gibbon publishing](https://docs.gibbonedu.org/guides/modules/reports/publishing),
[Fedena exam types](https://support.fedena.com/support/solutions/articles/242080-what-are-different-types-of-exams-in-fedena),
[Fedena transfer certificates](https://support.fedena.com/support/solutions/articles/223895-fedena-transfer-certificate-generator),
[OpenEduCat CBSE marksheet](https://openeducat.org/gradebook/k12/india-cbse/marksheet/),
[OpenEduCat marksheet generator](https://openeducat.org/tools/marksheet-generator/),
[Microsoft Teams AI feedback](https://support.microsoft.com/en-us/topic/ai-feedback-suggestions-responsible-ai-faq-b67b6c78-a2fb-4f99-8ba5-0475150e4c89),
[PowerBuddy for Assessment](https://www.powerschool.com/solutions/powerschool-ai/powerbuddy/powerbuddy-for-assessment/).

**Bangladeshi competitors:** [Eduman](https://edumanbd.com/features/), [DigiCampus](https://digicampusbd.com/),
[Bidyaan](https://www.bidyaan.com/pricing), [Pathshala Soft](https://pathshalasoft.com/),
[Smart Software](https://www.smartsoftware.com.bd/school-management-software),
[BDSchool Systems](http://bdschoolsystems.com.bd/), [Edufy](https://edufy.com.bd/pricing),
[EduZone](https://eduzone.com.bd/price), [SmartSchool.bd](https://smartschool.bd/),
[PRS Result Software](https://perfectnresults.com/), [Gradrix](https://gradrix.com/features/),
[eSchool](https://eschool.software/), [Cloudcampus24](https://cloudcampus24.com/school-college-fees-payment-through-bkash),
[DSHE EMIS](https://www.emis.gov.bd/EMIS),
[Noipunno outage](https://www.dhakatribune.com/bangladesh/education/343140/teachers-in-trouble-as-assessment-app-noipunno).

**Regulations:** [curriculum reversal](https://www.thedailystar.net/news/bangladesh/education/news/secondary-edn-science-arts-commerce-groups-return-3692371),
[new curriculum 2028](https://bangladeshpost.net/posts/govt-to-introduce-new-school-curriculum-in-2028-169804),
[Education Board grading](http://www.educationboard.gov.bd/computer/grading_system.php),
[SSC question structure](https://en.prothomalo.com/youth/education/e4ln52af2b),
[eSIF Chittagong](https://esifssc.bise-ctg.gov.bd/),
[admission policy 2026](https://viewsbangladesh.com/new-school-admission-policy-released-63-quota/),
[admission 2027](https://www.thedailystar.net/news/bangladesh/education/news/no-admission-test-class-1-2027-lottery-system-continue-4241566),
[fee order](https://en.prothomalo.com/youth/education/phpwt9j9sd),
[scholarship tuition exemption](https://www.bssnews.net/news/387108),
[stipend notice](https://dshe.gov.bd/site/notices/3d035280-c7ff-416b-a739-c47355f552bf),
[BANBEIS EIIN suspensions](https://www.tbsnews.net/bangladesh/eiin-158-educational-institutions-temporarily-suspended-1372531),
[MPO policy](https://dshe.gov.bd/site/notices/635005b8-a231-477f-a21e-5992b0e9713d),
[data protection ordinance](https://www.thedailystar.net/tech-startup/news/bangladeshs-personal-data-protection-ordinance-2025-key-takeaways-4015401),
[data protection gazette](https://www.tbsnews.net/bangladesh/govt-issues-gazettes-2-landmark-ordinances-data-protection-governance-1281356),
[data protection Act critique](https://www.thedailystar.net/slow-reads/big-picture/news/why-bangladeshs-new-data-protection-law-may-fail-protect-your-data-4217396),
[Payment and Settlement Systems Act](https://www.thedailystar.net/news/bangladesh/politics/news/js-passes-payment-and-settlement-system-bill-3647316),
[VAT on tuition](https://cpd.org.bd/fiscal-policy-for-incentivising-education/),
[ITES tax exemption](https://legalseba.com/bd-resources/tax-exemption-for-information-technology-enabled-services-ites-in-bangladesh/),
[public holidays 2026](https://www.thedailystar.net/news/bangladesh/news/28-public-holidays-2026-approved-4028556).
