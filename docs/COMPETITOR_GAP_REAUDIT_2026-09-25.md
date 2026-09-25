# Competitor gap re-audit — 25 September 2026

## How to use this audit

This replaces the **missing-feature conclusions** in `COMPETITOR_GAP_REAUDIT_2026-09-24.md`. That older list now wrongly calls homework, online admissions, transcripts, exam seating, mark import, Bangla teacher screens and early-warning reports missing. The 22 September `AUDIT_AND_UPDATE_PLAN.md` is also a historical baseline.

`STATUS.md` also has an older “Deliberately not built” sentence saying online gateways are absent. `fees/online.py`, `fees/management/commands/check_gateway.py` and the current `README.md` show that SSLCommerz invoice payments have since been built. Check code before repeating any old status claim.

The comparison is against the current repository and public descriptions from ManageBac+, Toddle, OpenApply, Fedena, PRS and OpenEduCat. Vendor pages show what each vendor advertises; this audit did not test their products. A missing feature is a market difference, **not** an instruction to build it. The owner has explicitly parked several learning-platform features. Confirm the first pilot school's needs before choosing work.

## Already built — do not ask Claude to rebuild

- National, Cambridge, Edexcel, IB MYP and IB DP **internal** results; subject plans and student subject choices; weighted components; grade forecasts; comments; published versioned cards; official-result register (`academics/`, `examinations/`). Internal grades are not official awarding-body awards.
- Daily attendance with recorder identity, teacher/section restrictions, absence SMS; timetable grid with clash checks (`attendance/`, `timetable/`).
- Exam seating, admit cards, blank mark sheets, component registers, CSV/Excel mark import with preview, result-day lists and Bangla PDF shaping (`examinations/`, `core/pdf.py`).
- Transcripts, verifiable certificates, and progress reporting (`examinations/transcripts.py`, `students/certificates.py`, `analytics/`).
- **Gated** online admissions: public applications, family status, office fee receipt, assessment, offers, waitlist and enrollment. **Gated** homework: tasks, hand-in, feedback, family view and analytics (`admissions/`, `homework/`). The platform operator controls school access to those modules.
- Fees, SSLCommerz payment for **student fee invoices**, accounting, payroll, one-way notices and SMS, and a phone-friendly family web portal (`fees/`, `finance/`, `messaging/`, `downloads/`, `core/portal_views.py`).

## Gaps worth discussing with a pilot school first

| # | What is still missing, in plain language | Current code evidence | Competitor example | Smallest useful next step |
|---|---|---|---|---|
| 1 | **Teaching plans:** record what each class will learn by unit, lesson and outcome. A list of class subjects is present, but it does not show planned or covered content. | `academics/models.py` has `ClassSubject`, subjects and teachers, but no unit, lesson or outcome model. | [Toddle curriculum planning](https://www.toddleapp.com/product/product-overview/); [ManageBac+](https://www.managebac.com/) | One school-year, class and subject plan with units, dates, outcomes and a coverage view. **Previously parked by the owner; build only if requested.** |
| 2 | **School–family conversations:** a parent can ask a teacher or office a question and receive a tracked reply. | `messaging/models.py` stores outbound SMS; `downloads/` has one-way notices; no conversation or reply workflow. | [Toddle Connect](https://www.toddleapp.com/product/connect/) | School-scoped message threads with assigned staff, read state and access rules. **Previously parked.** |
| 3 | **School-designed report cards:** administrators can choose the layout and sections for different programmes. Cards and transcripts exist, but their layouts are coded. | `examinations/documents.py` builds fixed card flowables; no report-template model/editor. | [ManageBac+ report templates](https://help.managebac.com/hc/en-us/articles/360045275492-Multi-Curricula-Non-IB-Reports-Editing-Report-Card-Templates-Publishing) | Start with a few approved layout choices and school branding, keeping published results frozen. A full designer is **previously parked**. |
| 4 | **Parent absence explanations:** families submit a reason for an absence for staff to accept or reject. | `attendance/models.py` has daily attendance and staff leave, but no guardian excusal request. | [ManageBac+ attendance excusals](https://help.managebac.com/hc/en-us/articles/51164383846937-Receiving-Attendance-Excusals) | Parent submits dates and reason; the school reviews; keep the original register and audit the decision. |
| 5 | **Admission fees paid online:** an applicant can pay the application fee from home. Online payment currently applies to an enrolled student's fee invoice. | `admissions/models.py` has an office `ApplicationPayment`; `fees/online.py` settles fee invoices. | [OpenApply welcome kit](https://www.openapply.com/files/OpenApply-WelcomeKit.pdf) | A separate, verified application payment flow using the school's gateway, with one receipt and safe retry handling. Do not turn an applicant into an enrolled student just to charge the fee. |
| 6 | **Behaviour and wellbeing follow-up:** restricted staff can record a concern, assign follow-up and see what happened. The early-warning report flags risks but stores no case. | `reports/` computes warnings; `students/models.py` has no incident/intervention case. | [Toddle behaviour and pastoral care](https://www.toddleapp.com/product/product-overview/) | Private case notes, owner, status, follow-up date and tight permissions; do not expose sensitive notes to every teacher. |

## Other confirmed gaps — only where the school actually needs them

| # | Gap | Repository evidence | Competitor example |
|---|---|---|---|
| 7 | **Period-by-period attendance** and reminders when a lesson register is missing. Daily attendance is present. | `attendance/models.py` stores one row per student and day, with no timetable period. | [ManageBac+ attendance](https://www.managebac.com/mobile) |
| 8 | **Student portfolio and IB CAS / Extended Essay / Service as Action tracking:** ongoing evidence, reflections, milestones and supervisor approval. IB result fields and homework files do not provide this workflow. **Previously parked.** | `academics/models.py` has IB core subject labels; `homework/models.py` stores tasks/submissions, not project milestones or portfolios. | [ManageBac+ DP/CP workflow](https://www.managebac.com/career-programme/feature/learning-management); [Toddle portfolios](https://www.toddleapp.com/product/student-portfolios/) |
| 9 | **Library lending:** books, borrowers, due dates and returns. | No library app or loan model in `config/settings.py`. | [Fedena library](https://fedena.com/feature-tour/library-management-system) |
| 10 | **School transport:** routes, stops, riders, vehicles and fees. A transport fee category alone does not manage buses. | `finance/models.py` has transport fee/expense heads; no route, stop or vehicle model. | [Fedena transport](https://fedena.com/feature-tour/school-bus-management-system) |
| 11 | **Inventory and purchases:** school assets, stock, suppliers and purchasing. | No inventory/procurement app in `config/settings.py`. | [OpenEduCat school ERP](https://openeducat.org/solutions/school-erp-software/) |
| 12 | **Hostel or boarding:** rooms, beds, allocation and boarding charges. | No hostel app or bed allocation model in `config/settings.py`. | [Fedena hostel](https://fedena.com/feature-tour/hostel-management-system) |
| 13 | **Automatic timetable creation:** staff currently place lessons on the grid themselves. | `timetable/services.py` validates saved slots; no timetable solver or generation workflow. **Previously parked.** | [Fedena timetable generator](https://fedena.com/feature-tour/ultimate-modules) |
| 14 | **Flexible fee payment plans:** agree future instalment dates for a charge. Monthly/quarterly billing and partial payments are already present. | `fees/models.py` has fee frequencies, invoices and partial payments, but no agreed instalment schedule. | [OpenEduCat fees](https://openeducat.org/solutions/education-erp/) |
| 15 | **Bank and gateway payout matching:** match the gateway settlement and bank deposit to receipts and charges. A gateway check/recovery command is already present; it is not bank reconciliation. | `fees/online.py`, `fees/management/commands/check_gateway.py` and `finance/` have no settlement-import/match workflow. | [OpenEduCat finance](https://www.openeducat.org/glossary/what-is-education-erp/) |
| 16 | **A public school website:** pages for the school, staff, news and admissions information. The public application and result pages are narrower. **Previously parked.** | `config/urls.py` has public application/results/verification, but no school-site CMS. | [PRS](https://perfectnresults.com/) |
| 17 | **Online quizzes and a question bank:** students answer a test inside the platform. Exam marks are entered by staff; homework submissions are not timed quizzes. **Previously parked.** | `examinations/models.py` has schedules and marks, no question/answer/test-session records. | [Fedena feature tour](https://fedena.com/feature-tour) |
| 18 | **Biometric/device attendance import:** reconcile device events with people and ask staff to review exceptions. | `attendance/` uses manual registers; no raw device-event model or importer. | [Fedena teacher login](https://fedena.com/feature-tour/employee-teacher-login) |
| 19 | **A native mobile app and push notifications:** the web portal is responsive but has no device app or push token. | `config/settings.py` has no mobile API/push service; `README.md` says the app is absent. | [ManageBac+ mobile](https://www.managebac.com/mobile) |
| 20 | **General integrations and single sign-on:** a school can connect a named identity provider or other system without manual file exchange. | `config/urls.py` has application screens and specific payment/public endpoints, not a general scoped API; no SSO configuration. **Previously parked.** | [Fedena integrations/SSO](https://fedena.com/feature-tour) |
| 21 | **Hosted multi-school onboarding:** create and operate separate schools by their own web addresses, with subscriptions and tenant operations. Data is school-scoped, but the host does not select the school. | `core/middleware.py` gets the school from the signed-in user; no host-to-school map or subscription workflow. | [Fedena multi-school](https://fedena.com/feature-tour); [PRS signup](https://perfectnresults.com/) |

## Programme-specific gaps; verify with a school before building

- **IB PYP narrative/standards reports and IB Career-related Programme workflows:** neither is part of the current results workflow. [ManageBac+ PYP report setup](https://help.managebac.com/hc/en-us/articles/360045275472-IB-PYP-Reports-Editing-Report-Card-Templates-Publishing); [ManageBac+ CP](https://www.managebac.com/career-programme/feature/learning-management).
- **Pearson Edexcel International A Level UMS/cash-in/A\* rule:** an official UMS value can be recorded, but the platform does not calculate unit aggregation or cash-in. `README.md` names this limit.
- **Second marking/moderation:** the app has marks, publication and unlocks, but no independent second-marker sample and agreed adjustment process. `README.md` names this limit.
- **A named programme/curriculum record:** `ClassLevel.assessment_system` selects grading rules and `ClassSubject` lists year/class subjects, but there is no programme entity. Add one only if a school runs parallel pathways needing programme-wide planning or reporting.

## Instructions for Claude

1. Treat 24 September gap labels and `AUDIT_AND_UPDATE_PLAN.md` as historical. Recheck the current code immediately before changing it.
2. Ask which **one pilot school workflow** the owner wants next. Keep previously parked items parked unless the owner changes that decision. Do not implement this entire list.
3. For the chosen feature, write the exact user flow, school/role access, data lifecycle, migration, audit events and acceptance tests. Protect student data and preserve existing published results, payments and uncommitted work.
4. The real-use gates in `IMPLEMENTATION_STAGES.md` still require school examples, print checks and operational sign-off. This audit does not certify official Cambridge, Pearson or IB calculations.
