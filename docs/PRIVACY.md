# Privacy baseline

How the system handles children's and families' data under Bangladesh's Personal Data
Protection Act 2026. This describes what the software does. It is not legal advice: a school,
or the company hosting the system, should have Bangladesh counsel review the actual
deployment before real data goes in.

Source: the Act as published on bdlaws.minlaw.gov.bd (Act 63 of 2026), read on 23 September
2026. By section 1(3) the Act is deemed in force from 6 November 2025, except section 23
(chief data officer) and sections 31–35 (complaints, penalties and compensation), which start
on a date the government sets by gazette after 18 months.

## 1. What is stored, by class

Section 29(1) lets the government classify personal data as public, internal, confidential
or restricted, by the characteristics in the Act's schedule. Until the government publishes
its classification, the system uses the working classes below.

| Class | Data | Who sees it |
|---|---|---|
| Internal | Names, class, section, roll, attendance, marks, results, fees | Staff by role; the family for their own child |
| Confidential | Addresses, phone numbers, emails, parents' names, photos, guardian relationships, comments on results | Staff by role; the family for their own child |
| Restricted | Birth registration numbers, NIDs (student, father, mother, guardian), religion, blood group, access arrangements (health and disability information) | Managers only (Administrator, Principal); every view or export of registration data and access arrangements is written to the audit log |

## 2. Consent

Section 9: a child's data is collected and processed with the consent of a parent or legal
guardian. Section 29(3): data may go abroad with the person's consent, or where it concerns
their education, with consent.

The system records consent per purpose, dated, with how it was given (signed form, family
portal, or other). The latest record for a purpose stands; earlier ones are kept.

| Purpose | Where it matters |
|---|---|
| Keeping and using the child's school records | The basis for everything else |
| Text messages about the child | SMS to guardians (each guardian can also opt out of SMS) |
| Photos in school publications | Photos beyond the school's own records |
| Sharing entry and result data with awarding bodies outside Bangladesh | Cambridge, Pearson and IB entries; the series check list names every candidate without it |
| Sharing health or access-arrangement information with an awarding body | An access arrangement cannot be marked applied without the date the guardian consented |

Guardians give or withdraw consent themselves in the family portal ("Privacy and consent").
Staff record it from signed forms on the student's page.

## 3. Retention and erasure

Each school sets a retention period (default 7 years after the student's last school year).
The Data retention screen lists former students past it. Erasing one, which needs a manager
and the student ID typed as confirmation:

- removes the name, contact details, identity numbers, religion, blood group, photo,
  documents, result code and consent records;
- keeps the year of birth only;
- removes the name and personal details from published results, combined results and
  certificates (their numbers and fingerprints no longer match the printed paper, which
  is expected after erasure);
- clears the candidate name and UCI from exam entries, and deletes access arrangements;
- replaces the child's name with the student ID in sent SMS text and in ledger narrations;
- erases each guardian unless another child at the school still links to them, and removes
  the name and number from messages sent to an erased guardian;
- deactivates the student's and erased guardians' sign-ins.

Marks, attendance, invoices, receipts and the ledger are kept, tied to the student ID, because
the school needs them for its accounts and records.

## 4. Hosting and transfers

Section 29(3)–(6): transfer abroad is conditional, not banned. It is allowed with consent, under
a contract, or for the person's education with consent; only to countries with adequate
protection as regulations will set out; and large transfers of sensitive identifiable data must
be notified to the Authority. It is not a blanket requirement to host in Bangladesh.

Before a deployment, counsel should confirm:

- where the database, backups and uploaded files are stored, and which countries the hosting
  provider may move them to;
- whether the hosting arrangement is a transfer abroad, and which condition covers it;
- whether the volume of restricted data (NIDs, birth registration numbers) triggers the
  notification in section 29(6);
- the contract between the school and the hosting company (who is the controller and who the
  processor, breach notice, deletion at the end of the contract).

## 5. Not covered by the software

- Appointing a chief data officer (section 23, when in force) is the school's decision.
- A breach response plan: who is told, by when, and how. The audit log helps reconstruct what
  was seen or changed; it does not replace the plan.
- Paper records and devices outside the system.
