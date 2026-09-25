# School ERP manual test guide

Follow this guide from top to bottom. It starts with a new school, then tests what each account does. Every task says **who signs in**, **what to click and enter**, and **what you should see**. Tick a box when it works. Record any failure in the table at the end.

**Use a disposable test installation.** The steps create records, publish results, issue receipts and may queue SMS. Do not use real children's details or a live payment/SMS account for this walkthrough.

## 1. Start the site and sign in

1. In PowerShell, from the project folder, run `.\.venv\Scripts\python.exe manage.py migrate`.
2. Run `.\.venv\Scripts\python.exe manage.py setup_roles`.
3. If you do not already have a **platform operator** account, run `.\.venv\Scripts\python.exe manage.py createsuperuser`. Choose and record its username and password yourself. The project cannot show you an existing password.
4. Run `.\.venv\Scripts\python.exe manage.py runserver` and open `http://127.0.0.1:8000/login/`.
5. At sign-in, enter the **username**, not the email address. Click **Sign in**.

### Ready-made demonstration accounts

To make these accounts on a disposable local database, run:

```powershell
.\.venv\Scripts\python.exe manage.py seed_demo --admin-username YOUR_SUPERUSER_USERNAME
```

The seeded school is **School ERP Demo** and its public admission address is `http://127.0.0.1:8000/apply/demo-school/`. The command creates these eight accounts. It sets the shown password **only when creating an account for the first time**. Running it again does not reset a changed password. The email is a sample contact field; sign-in uses the username.

| Role | Sign-in username | Sample email | Default password |
|---|---|---|---|
| Administrator | `demo_admin` | `demo_admin@demo.school` | `DemoPass!2026` |
| Principal | `demo_principal` | `demo_principal@demo.school` | `DemoPass!2026` |
| Vice Principal | `demo_vice` | `demo_vice@demo.school` | `DemoPass!2026` |
| Accountant | `demo_accountant` | `demo_accountant@demo.school` | `DemoPass!2026` |
| Teacher | `demo_teacher` | `demo_teacher@demo.school` | `DemoPass!2026` |
| Staff | `demo_staff` | `demo_staff@demo.school` | `DemoPass!2026` |
| Student | `demo_student` | `demo_student@demo.school` | `DemoPass!2026` |
| Guardian | `demo_guardian` | `demo_guardian@demo.school` | `DemoPass!2026` |

**Important: `demo_admin` cannot open Django Admin (`/admin/`) or Platform (`/platform/`).** It manages only its own school inside the ERP. To create a new school in section 3, sign out of `demo_admin` and sign in with the separate **platform operator (superuser)** account you made with `createsuperuser` in step 3 above. That account uses the username and password **you chose**; there is no default operator password. After creating the school, sign out and use its new `test_admin` account for ordinary school work.

If a disposable demo password no longer works and you want to reset **only the `demo_*` accounts**, rerun `seed_demo --reset-demo-password --admin-username YOUR_SUPERUSER_USERNAME`.

**Quick tour of seeded data:** Class 1/A has five students (`DEMO-001` to `DEMO-005`), Mathematics, a teacher, attendance, marks, fees and homework. Other seeded classes show Cambridge IGCSE, IB Diploma and national results. Admissions has a 2027 round with example applicants. These sample records make it easy to look around; the next section tests setup from zero.

## 2. What each role is meant to do

| Account | Main jobs to test | Should not do |
|---|---|---|
| **Platform operator** (superuser) | Create schools in Django Admin; choose the school under **Platform**; switch Homework and Admissions on or off. | There is no default operator password. |
| **Administrator** | Set up its own school's classes, people and users; work across attendance, exams, finance, messages and reports. | Cannot open Django Admin, create schools or grant platform modules unless also a superuser. |
| **Principal** | Manage teaching, admissions, exam publication, attendance, homework and whole-school analytics; view finance summaries. | Should not post school fee payments or manage platform modules. |
| **Vice Principal** | Same school access as Principal. | Same limits as Principal. |
| **Accountant** | Make invoices, collect fees, pay payroll, post accounts and see fee/admission receipts. | Should not enter student marks or decide admission offers. |
| **Teacher** | See assigned students; take their section's register; mark their subjects; add comments; set/check homework; view their analytics. | Should not see other sections' marks or whole-school private reports. |
| **Staff** | See own staff file, request leave and use the admissions office screens when the module is enabled. | Should not publish results or see student marks. |
| **Student** | See own attendance, fees, results, progress, transcript and homework; hand in work. | Should not see another child's records. |
| **Guardian** | See linked children's records; tick or hand in homework for a child; see invoices and pay online if a test gateway is set. | Should not see an unlinked child. |

The [access matrix](access-matrix.md) lists every route and role. Use it if an expected permission is unclear.

## 3. Make a fresh test school (platform operator)

Use this section to test actual onboarding. Keep the demo school for comparison. A new school has **no demo accounts**; you create its accounts below.

**Already created your school? Start here.** Sign in with your platform superuser, click **Platform**, find your school's name, and click **Work in this school**. Check that the sidebar now shows your school's name. Then go straight to **3.3 Create the school Administrator and other logins**. Every user made through **User Management** while this school is selected belongs to this school. The `demo_*` users stay in the demo school.

### 3.1 Create and select the school

- [ ] Sign out of any `demo_*` account. Sign in with the **superuser username and password you chose** when you ran `createsuperuser`. Click **Django Admin** in the left menu (or open `/admin/`), then **Schools → Add**. Enter **Name:** `Manual Test Academy`, **Slug:** `manual-test-academy`, **Short name:** `Test Academy`; leave other fields at their defaults and click **Save**. **Expected:** the school appears in the Schools list. If you are signed in as `demo_admin`, this menu is absent by design.
- [ ] Click **Platform** in the ERP sidebar (or open `/platform/`). Find `Manual Test Academy` and click **Work in this school**. **Expected:** it says **This school**. The sidebar now names Test Academy.
- [ ] On the same page, click **Turn on** under **Homework** and **Admissions** for this school. **Expected:** both say **On**. Sign out later as the school admin to confirm those menus appear. Only the platform operator can change these switches.

### 3.2 Set the basics

- [ ] While still working in Test Academy, click **Basic Settings → Overview → Create defaults**. **Expected:** accounts, fee heads, grading scale, rooms, periods and message templates are created. Clicking it again should say everything is already set up; it should not make duplicates.
- [ ] Click **Basic Settings → School profile**. Enter an address such as `123 Test Road, Dhaka`, phone `01700000099`, email `office@example.test`, principal name `Test Principal`, and leave currency as BDT. Click **Save settings**. **Expected:** the school name/address appears on later PDFs.
- [ ] Click **Basic Settings → Academic years**. Make sure the current year is `2026`. If it is missing, click **Add**, enter **Name** `2026`, **Start** `2026-01-01`, **End** `2026-12-31`, tick **Current**, and save. **Expected:** the sidebar shows 2026. If another year is current, click **Make current** on the 2026 row.
- [ ] Click **Basic Settings → Terms → Add**. Enter `Term 3`, year `2026`, dates inside that year. **Expected:** the term appears in the list.
- [ ] Click **Basic Settings → Classes → Add**. Enter **Name** `Class 1`, **Order** `1`, and choose the school's own assessment rules for the first test. Save. Return to **Basic Settings → Sections → + New**; select Class 1, enter **Name** `A`, **Capacity** `30`, leave **Class teacher** blank until the teacher exists, and click **Save**. **Expected:** `Class 1 - A` appears in the Sections list, and the Sections readiness item on the Basic Settings overview turns green. A class without a saved section still shows an amber Sections warning.
- [ ] Click **Basic Settings → Subjects → Add** twice. Add `Mathematics` with code `MATH`, then `English` with code `ENG`. Select Class 1 for both if offered. **Expected:** both appear in Subjects.
- [ ] Open **Basic Settings → Subject plans** from the Overview card, or `/academics/subject-plan/`. Add Mathematics and English for **2026 / Class 1** as compulsory subjects. **Expected:** both subjects appear in the plan for the class. The plan applies to all Class 1 sections in that year.

### 3.3 Create the school Administrator and other logins

- [ ] While signed in as the **platform superuser**, open **Platform** and click **Work in this school** for your new school if it does not already say **This school**. **Expected:** the sidebar shows your school's name. This choice determines which school receives the new accounts.
- [ ] Click **User Management → Create user** (or open `/users/new/`). Enter **Username** `test_admin`, **First name** `Test`, **Last name** `Admin`, **Email** `test_admin@example.test`, a password you choose, tick **Active**, choose **Administrator** in **Roles**, then click **Save**. **Expected:** `test_admin` appears in the user list with role Administrator. **Record the password privately**; this guide cannot know it. If Roles is a multiple-choice list, select Administrator and leave the other roles unselected.
- [ ] Sign out and sign in as `test_admin` using the password you chose. Open **User Management**. **Expected:** you see accounts for this school; **Platform** and **Django Admin** are absent. This proves you created the account in the intended school.
- [ ] As `test_admin`, click **User Management → Create user** for `test_principal` (**Principal**), `test_vice` (**Vice Principal**) and `test_accountant` (**Accountant**). Give each a different password and a sample `@example.test` email. **Expected:** each appears in the user list with the chosen role and can sign in. The Roles field can hold more than one role, but use one role per test account so permission checks are clear.
- [ ] To change an existing school user's role, open **User Management**, click that username, click **Edit**, change **Roles**, leave **Password** empty unless you want to change it, then **Save**. **Expected:** the user's detail page shows the new role after refresh. Do not assign a role with no job to do; roles grant access to school data.
- [ ] For **Teacher** and **Staff**, use **Teachers & Staff → + New** as one onboarding step. Choose an account in **Existing login** if you already created it; the saved name, email and phone fill in automatically. Otherwise leave Existing login blank and keep **Create a login when I save this employee** checked. Fill the remaining employee details and click **Save**. **Expected:** the person appears in the staff list with a linked login. For a new login, the username and temporary password appear **once**; copy them before leaving. For **Student** and **Guardian**, first admit and link the child and guardian, then click **Create login** on their records. Giving someone the Teacher role alone does not create an employee record or put them in the **Class teacher** list.

## 4. Put people into the school (Administrator)

### 4.1 Employee, section teacher and timetable

**If Class teacher shows only dashes, or Teachers & Staff says no records found:** click **Teachers & Staff → + New**. Choose **Type: Teacher** and **Status: Active**. If the teacher already has a user account, select it under **Existing login**; first name, last name, email and phone fill in from that account. Enter what User Management did not ask for, especially **Gender** and **Joining date**, then save. If there is no account yet, leave Existing login blank, keep **Create a login when I save this employee** checked, enter the person's details and save. The employee appears in the staff list and the Class teacher dropdown. The linked user can then take the class register and use teacher homework screens.

- [ ] Sign in as `test_admin`. Click **Teachers & Staff → + New**. Leave **Existing login** blank, keep **Create a login when I save this employee** checked, then enter employee ID `T-001`, name `Test Teacher`, a safe example phone, gender, joining date `2026-01-01`, type **Teacher**, department **Academics** and designation **Assistant Teacher** (create those two under Basic Settings if needed). Click **Save**. **Expected:** the employee and Teacher login are created together, and a username and temporary password appear once. Save them privately, click **Done**, and sign in as that teacher to change the password.
- [ ] Click **Basic Settings → Sections**, edit `Class 1 - A`, set **Class teacher** to Test Teacher, and save. Then **Subject teachers → Add**: choose 2026, Class 1 - A, Mathematics, Test Teacher. Repeat for English if you want the same teacher to mark both. **Expected:** the teacher sees only these assigned sections/subjects.
- [ ] Click **Teachers & Staff → + New** again. Make employee `S-001`, name `Test Staff`, type **Staff**, and keep **Create a login when I save this employee** checked. Click **Save** and copy the one-time credentials. **Expected:** the Staff account can see **My staff file** and **Leave**, but not teacher mark entry.
- [ ] Click **Routine → Edit week**. Pick year 2026 and section Class 1 - A. Put Mathematics with Test Teacher in one teaching period and a room; save the week. **Expected:** the class and teacher routine show the lesson. Try assigning the same teacher to a second class at the same time; **Expected:** a clash is refused.

### 4.2 Admit a child and link a guardian

- [ ] Click **Students → Admit student** (or open `/students/admit/`). Enter student ID `TEST-001`, first name `Amina`, gender **Female**, birth date `2018-04-10`, admission date today, guardian name `Test Guardian`, relation **Mother**, guardian mobile `01700000123`, year **2026**, class **Class 1**, section **A**. Leave roll blank so the next free roll is chosen. Click the save/admit button. **Expected:** Amina appears in Students and in the Class 1/A enrollment list with a roll number; the guardian is linked.
- [ ] Open Amina's student file. Use **Create login** for the student and **Create login** for the guardian. Copy the one-time usernames and passwords. **Expected:** Student and Guardian roles are assigned and they can sign in. If no button appears, use **User Management** after confirming the people are linked.
- [ ] Admit a second child `TEST-002` in Class 1/A with a different student ID. Enter the **same guardian mobile number** `01700000123` in the admission form. **Expected:** the system links the existing guardian to both children, and the guardian's portal has a child switcher.
- [ ] Open **Students → Subject choices**. For an optional-subject class, check that only the chosen students sit that subject. For this simple Class 1 test, both Mathematics and English are compulsory, so both children should appear on their exam mark grids.
- [ ] Open **Students → ID card** for Amina. **Expected:** a PDF downloads with her current details. Open **Students → Export** and confirm the new student appears.

## 5. Take attendance and manage leave

### 5.1 Student register (Teacher, then Administrator)

- [ ] Sign in as Test Teacher. Click **Student Attendance**. Choose a **school day that is today or earlier**, choose **Class 1 - A**, and click **Load register**. Click **All present**. Change Amina to **Absent**, put `Test absence` in Remarks, and click **Save attendance**. **Expected:** both students have saved statuses; the row for Amina is absent.
- [ ] Open **Today's summary** and **Monthly report**. **Expected:** the section is marked complete for that date; the counts show one absent. Open Amina's **student history** from her file. **Expected:** the saved date, status, remarks and who recorded it are shown.
- [ ] Sign in as Administrator and open the same register. Change Amina to Present, then save. **Expected:** the correction is saved and the recorder/audit trail identifies who made it. Use a separate test date if you want to preserve the absent example for early-warning reports.
- [ ] Try a future date or a closed holiday. **Expected:** the register refuses the entry. If the school allows only class teachers or has an edit window, test those policy choices under **Basic Settings → Policy**.

### 5.2 Staff register and leave (Staff, Principal)

- [ ] As Administrator or Principal, click **Staff Attendance**. Load today's register, mark the teacher and staff member Present, enter a check-in time, and save. **Expected:** the staff monthly report shows them.
- [ ] As Test Staff, click **Leave → New**. Choose **Casual**, two future school days, enter a reason, and submit. **Expected:** it appears as Pending in that person's list, with their remaining entitlement.
- [ ] As Principal or Vice Principal, open **Leave** and approve the request. **Expected:** it becomes Approved and its days appear as leave in the staff attendance register. Try an overlapping request as Staff; **Expected:** the overlap is refused. A Staff account should see its own request, not another employee's.
- [ ] If you enable **Staff self check-in** in **Basic Settings → Policy**, have Staff click **Check in / out** on Staff Attendance. **Expected:** their own time is recorded; they cannot check in for someone else.

## 6. Run an exam from start to report cards

Use the fresh school's **Class 1/A**. Choose a test date that is within the 2026 academic year and does not clash with a holiday. The school must confirm its own grading rules before using a real result.

### 6.1 Set up the exam (Administrator or Principal)

- [ ] Click **Results → Exams** (`/exams/`), then **Add exam**. Enter **Year:** 2026, **Term:** Term 3, **Name:** `Manual Test Term Exam`, start/end dates, the default grade scale, and the chosen assessment system. Save. **Expected:** an exam page opens showing Draft.
- [ ] On that page click **Add paper**. Set exam `Manual Test Term Exam`, class `Class 1`, subject `Mathematics`, date and start/end time, full marks `100`, pass marks `33`, and a room. Save. Repeat for English at a different time. **Expected:** both papers show on the exam page.
- [ ] For an optional component test, click **Parts** beside Mathematics. Enter two parts, e.g. `written` out of 70 with pass 23 and `oral` out of 30 with pass 10; leave weights blank if the parts simply add to 100. Save. **Expected:** the mark grid shows two input columns and totals them. If you need weights instead, give every part a weight adding to 100.
- [ ] Click **Routine** and **Seat plans** on the exam page. Choose a sitting, select rooms, then **Seat the students**. Download door list, stickers and invigilator sheet; click **Lock after printing**. **Expected:** students have one seat each and later PDFs agree with the locked plan. Do not reseat unless you deliberately unlock it.
- [ ] In **Print for a section**, choose Class 1/A and click **Admit cards**. **Expected:** a PDF contains the two eligible students and their papers.

### 6.2 Enter marks (Teacher)

- [ ] Sign in as Test Teacher. Click **Results → Enter marks**. Select Mathematics paper and Class 1/A, then **Load class**. Enter Amina `72` and the second student `65` if the paper has one score. If you added parts, enter `50` and `22` for Amina, `45` and `20` for the second student. Click **Save all marks**. **Expected:** the saved totals appear and the entered count increases. Repeat for English.
- [ ] Open **Blank mark sheet (PDF)** and **Marks register (PDF)**. **Expected:** the blank sheet has spaces to collect marks; the register has the saved component scores.
- [ ] Open **Import from Excel**. Download the template. In a copy, change one student's mark, keeping **Roll** and **Student ID** intact. Upload it and click **Check the file**. **Expected:** it says what would change but saves nothing yet. Click **Save marks** only if the preview is correct. **Expected:** the mark grid now shows the new value. Test a wrong student ID or a mark over the maximum; **Expected:** it refuses the file and changes no marks.
- [ ] Open **Comments** for the same paper and section, enter a short subject comment and save. Open **Grade estimates**, enter a predicted grade if that paper's scale supports it. **Expected:** a manager can review/approve the estimate; it is kept separate from actual marks.
- [ ] Before saving a second change, type a mark and reload the mark page. **Expected:** the page offers to restore unsaved numbers. Save or discard the draft; then sign out and confirm another user does not see that draft.

### 6.3 Publish and print (Principal or Vice Principal)

- [ ] Open `Manual Test Term Exam`. Look at **Before publishing**. **Expected:** missing marks or blocking problems are listed. Do not click Publish until the checklist is clear and both students have a mark or explicit absence for every paper they take.
- [ ] Click **Publish results**. **Expected:** status becomes Published, a publication version appears, and the mark grid is locked. Open **Result sheets**; choose this exam and Class 1, then **View results**. **Expected:** totals, grades and subject analysis agree with the marks entered.
- [ ] On the exam page choose Class 1/A and click **Report cards**. Open Amina's individual card from the result sheet, then download its PDF. **Expected:** school name, marks, grading, publication state and verification information are correct. The bulk PDF contains all students in the section.
- [ ] On **Result sheets**, try CSV, Excel and PDF exports. **Expected:** the same students and values appear. On a national-curriculum test class, also check tabulation, GPA, failed-subject and near-pass lists. These national-only items are not expected for an unrelated rulebook.
- [ ] As Teacher, open the published paper's mark grid and click **Request unlock**, giving `Answer script re-totalled`. As Principal, open **Unlock requests** and approve. As Teacher, correct one mark and save. **Expected:** the result is republished as a newer version; the old published version remains in the audit trail. After the unlock expires or is refused, editing is blocked.

### 6.4 Family view and deeper exam checks

- [ ] Sign in as Amina's Student, then Guardian. Click **My results** and **Progress**. **Expected:** published results appear; draft exam results do not. Download the report card. The guardian can switch children but cannot change marks.
- [ ] As Principal, open **Results → Combined results**. Create a combined term/year result using published source exams if you have at least two; publish it and check its card. **Expected:** it uses the chosen weights and only published source results. With just one test exam, use the seeded demo's existing combined results instead.
- [ ] Open **Results → Exam series** for a seeded Cambridge/IB/board series. Check candidates, entries file and confirmed official results. **Expected:** an imported awarding-body result is clearly separate from the school's internal mock grade; families only see official results after release/confirmation.
- [ ] As Student or Guardian, request a **Transcript** in the portal. As Principal, open **Results → Transcripts**, review and issue it. **Expected:** a numbered PDF is created and its public verification link works; correcting a source result should flag that transcript as out of date.

## 7. Give and check homework

Homework is visible only after the platform operator enables it for the school.

- [ ] As Test Teacher, click **Homework → Set homework**. Choose **Class 1 / Mathematics**, then enter **Title:** `Fractions practice`, **What it practises:** `Adding fractions`, **Instructions:** `Complete questions 1–5`, **Time needed:** `20` minutes, **How it is handed in:** online, **Marking:** marks out of `10`. Tick Class 1/A, choose a future due date/time, and publish. **Expected:** the task appears in Homework and in the Student/Guardian to-do list. The daily load warning should appear if the school's limit is exceeded; it warns but does not block the teacher.
- [ ] As Student, click **Homework → Fractions practice → Choose photos or files**. Upload a small photo or PDF, optionally type a note, and click **Hand it in**. **Expected:** the page shows it as handed in and the teacher sees a submission. A photo should preview; a protected file link should require an authorised account.
- [ ] As Guardian, switch to the right child and open Homework. **Expected:** the same due date and hand-in status are visible. For a paper task, click **Mark as done at home**; **Expected:** it says Ticked done until the teacher checks it. This is not the teacher's final mark.
- [ ] As Teacher, open the task. For paper work use **Check work**; for online work open the submission, enter feedback such as `Good work`, enter a mark such as `8`, and return it. **Expected:** Student/Guardian sees returned feedback and the mark if the teacher chose to show marks. Test **redo** or an extension on a separate task.
- [ ] As the class teacher, open **Homework → Missing work**. **Expected:** only work known to be missing is listed; unchecked paper work is not falsely called missing. Open **Homework analytics** to compare due/handed-in/returned counts.

## 8. Run online admissions

The demo school already has **Admission 2027**. For the fresh school, first create year 2027 under Basic Settings. Use dummy applicants and a test phone number only.

- [ ] As Principal or Administrator, click **Admissions → New round**. Choose year `2027`, name `Admission 2027`, opening date today or earlier, closing date later, optional application fee `500`, and publish the round. Open that round and click **Add class**; choose Class 1, set seats `2`, a suitable birth-date range, and interview or test assessment. **Expected:** the public admissions link shown on the Admissions page lists that class.
- [ ] Sign out and open `http://127.0.0.1:8000/apply/manual-test-academy/`. Click **Apply**. Enter a dummy child's name and valid birth date, guardian name, `01700000124`, address, and tick consent. Submit. **Expected:** an application number and private family link/slip appear. Save that link for this test. A new private browser window should not know another family's application without its link.
- [ ] As Staff, click **Admissions**, open the new application and check its status. Upload a dummy PDF/photo where requested, mark documents accepted/rejected with a reason, enter a staff-only note, and record an office application-fee payment if this round charges one. **Expected:** a receipt is available; the fee status changes; the applicant sees only family-facing information, not private office notes. Application fees are paid at the **school office**, not through the student invoice gateway.
- [ ] As Principal, open **Tests and interviews**. Schedule a sitting, book the applicant, mark attendance and score. Open **Merit list → Fix the merit list → Offer places**. **Expected:** ranking follows the scores and configured sibling rule; offers do not exceed seats. Add more dummy applicants if you want to test the waiting list.
- [ ] Open the saved private family link, accept an offer if one was made, then as Principal open **Enrol** and convert that accepted application. **Expected:** one Student and guardian are created or linked, the child is on the target year's roll, and there is no duplicate child after a retry. Open **Admissions → Round report** to see application and enrolment counts.
- [ ] Have the platform operator turn **Admissions off** for a moment in `/platform/`. **Expected:** the staff menu and public application address disappear/return not found. Turn it back on before continuing. Existing data should still be there.

## 9. Fees, receipts, accounts and payroll (Accountant)

- [ ] Sign in as Accountant. Click **Fees → Fee categories** and confirm Tuition exists. Open **Fee structures → Add**. Choose 2026/Class 1/Tuition, amount `1000`, monthly; save. **Expected:** it is listed. If the demo or defaults already created an identical structure, use its existing row instead of duplicating it.
- [ ] Click **Fees → Generate invoices**. Choose year 2026, Class 1, a month not already invoiced for your test students, issue date today, due date later, then submit the form. **Expected:** one invoice per eligible enrollment for that month. Running it again should not create duplicates.
- [ ] Open Amina's invoice. Enter payment amount `400`, method **Cash**, date today, and click **Collect and issue receipt**. **Expected:** a receipt PDF appears, invoice status becomes Partial, and balance falls by 400. Open **Statement**; it should show the same payment. Collect the remainder separately and confirm Paid.
- [ ] For a reversal test, use a **separate test receipt**. Click **Reverse**, enter `Test correction`, and confirm. **Expected:** the original receipt is marked reversed; its account entry is reversed, and the invoice balance rises again. Do not reverse a real payment.
- [ ] Open **Fees → Due**. Set an as-of date after an unpaid invoice's due date. **Expected:** the unpaid student is listed. Use **Remind** only with a console/test SMS gateway and dummy numbers.
- [ ] Click **Accounts**. Open **Trial balance**, **Income & expenditure**, **Cash book** and the Tuition income account ledger. **Expected:** the fee payment and any reversal agree across reports, and the trial balance balances. Use **Expense / income** to post a small test expense `100` with a reason; check it appears in the account ledger. Never post fee cash through a freehand journal.
- [ ] Open **Accounts → Payroll**. Prepare a test salary for Test Teacher for a month that has not been paid, then pay it. **Expected:** a payslip PDF, payroll status Paid and a matching ledger entry. As Teacher, open **My staff file** and confirm only their own payslip is visible.
- [ ] For an **online fee payment** test, ask the Administrator to open **Basic Settings → Payment settings** and select the demonstration gateway only on a disposable installation where it is allowed, or configure the school's SSLCommerz sandbox account. As Guardian, open an unpaid invoice and click **Pay online**. **Expected:** a successful demo/sandbox return produces one receipt. Do not use live credentials or money for this test.

## 10. Notices, SMS, calendar and reports

- [ ] As Administrator, click **Notices → New notice**. Enter `Test sports day`, choose the student/family audience, publish date now and an expiry date later. **Expected:** Student and Guardian see it. Make a separate staff-only notice; **Expected:** the family does not see its title or file.
- [ ] Click **Downloads → Add**. Upload a small test PDF or text file for Students. **Expected:** Student can download it; a user outside the audience cannot. Repeat with a staff-only file.
- [ ] Click **SMS → Compose**. Choose a template, a recipient group and preview. **Expected:** the preview shows recipient count and message parts before queuing. With the console backend, run `.\.venv\Scripts\python.exe manage.py process_sms --limit 100` in PowerShell and inspect **SMS → Outbox**. **Expected:** statuses update; no real text goes to a handset.
- [ ] Click **Calendar**. Add a future school-closed holiday and an event that does not close school. **Expected:** both appear on the month grid; only the closed day blocks attendance. Export iCalendar and confirm it downloads.
- [ ] As Principal, open **Reports → Early warning**, **Student strength**, **Teacher load** and **Management overview**. **Expected:** figures reflect saved students, attendance, teaching assignments and results. Early warning needs enough attendance/result history; with only one date or exam, an empty or limited result is normal.
- [ ] Click **Analytics**. Choose a published exam, open a paper, a section and **School analytics**. **Expected:** teacher sees only assigned subjects/sections; Principal and Vice Principal see whole-school comparisons. Open **Homework analytics** and the Student's **Progress** page to compare the same student's records.

## 11. Student records and year-end tasks

Do these after the main exam test. Use a separate test student where a step closes an enrollment.

- [ ] As Administrator, open **Students → Import**. Make a UTF-8 CSV with the header and one row shown below. Replace `2026-09-25` with today's date if needed. Click **Check file**. **Expected:** the preview shows one valid child and nothing has been saved yet. Click **Import 1 student**. **Expected:** the child now appears in Students. Repeat with the same ID or an unknown section; **Expected:** the preview reports errors and saves nothing.

  ```csv
  student_id,first_name,gender,date_of_birth,admission_date,guardian_name,guardian_phone,guardian_relation,class_level,section
  TEST-CSV-001,Csv Child,F,2018-05-11,2026-09-25,Test Guardian,01700000123,mother,Class 1,A
  ```
- [ ] Open Amina's student file and click **Certificates**. Choose **Studentship certificate**, English, and reason `Passport application`, then issue it. **Expected:** a numbered printable certificate appears. Open its verification link in a private window; **Expected:** it shows the same valid certificate. Revoke this test certificate with a reason and check that the verification page says it was revoked. If Bangla is enabled, issue a separate Bangla test copy and check that the text renders correctly.
- [ ] On Amina's file, upload one harmless test document. **Expected:** a permitted school worker can download it; another school's account cannot. Open **Privacy and consent** as her Guardian to review the family's consent information. Avoid real identity documents in this test.
- [ ] As Administrator, make a new academic year `2027` and a target class/section such as `Class 2/A` under **Basic Settings**, if they do not already exist. Click **Students → Promote**. Choose source 2026/Class 1/A, target 2027/Class 2/A, and the published test exam as **Advise from**. Click **List students**. **Expected:** each child has a promotion recommendation. Promote one child; if you choose against the recommendation, enter a reason. Click **Promote**. **Expected:** the child has a 2027 enrollment and the 2026 history is still visible.
- [ ] On a separate test student's file, click **Record leaving** and enter a date and reason. **Expected:** the student's current enrollment closes and the student disappears from later active class registers while past attendance and results remain. Issue a leaving certificate only when its required details are present.
- [ ] As Administrator, open **Basic Settings → Audit log** after these changes. **Expected:** it identifies the user, time and action for admissions, attendance edits, mark changes, payments and certificate changes made during this walkthrough.

## 12. Final checks for every role

Sign out between roles, or use separate private browser windows. This catches accidental sharing of a session or cached screen.

| Role | Open and check | Expected result |
|---|---|---|
| Administrator | Basic Settings, User Management, Students, Results, Fees, Accounts, Reports | School-wide screens work; **Platform** is absent unless this account is also a superuser. |
| Principal | Results, Admissions, Student Attendance, Analytics, Accounts | Can publish and approve school workflows; Accounts is for viewing, not posting money. |
| Vice Principal | The same pages as Principal | Same access and limits. |
| Accountant | Fees, Accounts, Admissions fee receipt | Can handle money; cannot edit marks or decide offers. |
| Teacher | Student Attendance, assigned mark grid, Homework, Analytics | Only assigned section/subject data; no other teacher's private marks. |
| Staff | My staff file, Leave, Admissions office | Own employee record and leave; admissions office work only when enabled. |
| Student | My school records, My attendance, My fees, My results, Progress, Homework | Only that student's records; can hand in own work. |
| Guardian | Portal child switcher, fees, results, homework | Only linked children. A changed child ID in the address must not reveal an unlinked child. |

### Checks that should **fail safely**

- [ ] Before publication, try opening a draft report card as a Student. **Expected:** no unpublished marks are shown.
- [ ] As Teacher, open a different section's mark page or a different child's file. **Expected:** access is denied or the record is not found.
- [ ] As Staff, open a payroll or exam mark page. **Expected:** access is denied.
- [ ] As Accountant, try to publish an exam. **Expected:** access is denied.
- [ ] As a Guardian, manually change the child number in a portal URL to one they are not linked to. **Expected:** no other child's information is shown.
- [ ] Turn Homework off in Platform. **Expected:** its menu, portal cards, notifications and pages disappear for every role; turn it on again after the check.
- [ ] Try saving marks above a paper's maximum or uploading a mark file with the wrong student ID. **Expected:** a clear error and no partial save.
- [ ] Try taking attendance on a future day, generating the same invoice twice, double-booking a teacher, and editing a published paper. **Expected:** each attempt is refused or leaves the original record unchanged.

## 13. Record what you found

Copy this row for each problem. Include the exact account, screen and input so a developer can reproduce it.

| Date | Role | Task/step | What you entered or clicked | Expected | What actually happened | Screenshot/file |
|---|---|---|---|---|---|---|
| | | | | | | |

For a PDF problem, keep the PDF and name the document and student/test data. For a permission problem, note whether the result was **403**, **404**, an empty list, or someone else's data. If an icon is blank, note the page, browser and device.
