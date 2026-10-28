# Runbook — accountant sign-off of the pilot month (AC-23)

- **Last verified:** not yet executed by a named accountant. A dry run of the scripted month was executed by the Build agent on the pilot tenant on 2026-10-27; the review and signature in step 6 are an **owner action**.
- **Estimated duration:** 3–5 hours for a first review, 1–2 hours to repeat
- **Risk:** low — the review writes only to the pilot tenant (never to production data)
- **Reversible:** yes — the reviewer works in a dedicated company; the tenant can be re-provisioned afterwards

## Purpose

Make a named accountant — not the build team — the person who confirms that the
inferred accounting defaults (ADR 0003) are correct for a real business, that a
full month can be completed with the product, and that the numbers agree with
the system the business uses today. The signed record is what closes AC-23.

## Why it cannot be done by the build agent

ADR 0003 records that the chart of accounts, tax treatment and posting defaults
were inferred from the vendor study. Only an accountant can confirm or reject
them; a software test can prove the arithmetic is self-consistent, not that it is
the right arithmetic for the business. The owner must name the reviewer and keep
the signature.

## Preconditions

- [ ] The pilot installation is running (see offline-windows-install.md), and the
      owner has the administrator account and the tenant URL.
- [ ] A company is prepared for the review with the defaults untouched, so the
      reviewer sees exactly what a new customer gets: tenant, company, fiscal
      year, base currency, chart of accounts, tax codes.
- [ ] A read-only or accountant role user exists for the reviewer (their own
      account, never the administrator password shared).
- [ ] The reviewer has their current system available for comparison and knows
      the month they will compare (for example September 2026).
- [ ] The evidence folder exists: docs/10-runbooks/accountant-signoffs/2026-10/.
      It is inside the private repository, not published with the public export.

## The scripted month

Execute in this order in the pilot tenant. Record the document number and date of
every step in the review sheet as you go; the sheet is the evidence.

| # | Step | What the reviewer checks |
|---|---|---|
| 1 | Review Settings → Company, fiscal year, base currency, exchange rates | matches the legal entity and the reporting currency |
| 2 | Review the chart of accounts, account types, and the mapping from document types | account names, numbering and grouping match their practice |
| 3 | Enter opening balances for each account as of the month start | the balance sheet opening agrees with their current system |
| 4 | Create a sales invoice, two lines, one with VAT, one exempt; post it | tax splits into the right accounts, the invoice PDF is Arabic and correct |
| 5 | Create a purchase bill with VAT from a supplier; post it | input VAT lands in the right account, the payable is right |
| 6 | Issue a credit note against the sales invoice; post it | reverses revenue and VAT to the same accounts |
| 7 | Record a customer receipt, fully and partially against invoices | the invoice becomes paid/partly paid, no rounding drift |
| 8 | Record a supplier payment, fully and partially against bills | same, on the payable side |
| 9 | Record a post-dated cheque received and deposit it | the cheque is tracked, then moves to the bank |
| 10 | Transfer between two bank accounts | both sides balance, no revenue is created |
| 11 | Reconcile the bank statement, including one unmatched line | the unreconciled difference is explainable |
| 12 | Post a manual journal entry, then reverse it | the reversal keeps the audit trail intact |
| 13 | Run Trial Balance, P&L, Balance Sheet, AR aging, AP aging, VAT summary | every report opens, the Arabic PDFs print, the totals agree |
| 14 | Close the period, then try to post into it | posting is refused with a clear message |
| 15 | Compare ten figures (see below) with their current system | differences are zero or explained |
| 16 | Print the final trial balance, P&L, balance sheet and the VAT summary to PDF | the files are attached to the record |

## Comparison sheet (ten figures)

Fill this for the chosen month. Differences are in minor units of the base
currency; a difference above one minor unit must be explained in the variance log.

| Figure | Al-Mahmoud | Current system | Difference | Explanation |
|---|---|---|---|---|
| Revenue, month | | | | |
| Cost of sales, month | | | | |
| Gross profit, month | | | | |
| Output VAT, month | | | | |
| Input VAT, month | | | | |
| Net VAT payable, month | | | | |
| Total receivables, month end | | | | |
| Total payables, month end | | | | |
| Bank balance, month end | | | | |
| Retained earnings / equity, month end | | | | |

## Variance log

Every difference or behaviour the reviewer disagrees with goes here with a
classification, so the build team can decide instead of guessing.

| # | Step | Expected | Al-Mahmoud showed | Class | Decision |
|---|---|---|---|---|---|
| 1 | | | | defect / default is wrong / preference / not required | fix before pilot / accept / later |

Classes: **defect** (the product is wrong), **default is wrong** (needs a
different chart/tax default), **preference** (works, but their practice differs),
**not required** (out of scope for this pilot). A defect blocks the sign-off
until fixed and re-checked; the other classes can be accepted with conditions.

## The sign-off

The reviewer signs this statement after the month and the variance log are
complete. Store the filled copy plus the PDFs and the variance log in
docs/10-runbooks/accountant-signoffs/2026-10/.

~~~text
Pilot accounting review — statement

I reviewed the Al-Mahmoud ERP pilot installation for <company>, month <YYYY-MM>.
I executed the scripted month in docs/10-runbooks/accountant-signoff.md and
compared the ten figures with <current system>.

Result: [ ] accepted   [ ] accepted with conditions   [ ] rejected
Conditions / outstanding items: <list or "none">
Variance log entries: <count> (<count> accepted with conditions, <count> fixed)
I confirm the accounting defaults (chart of accounts, tax treatment, posting
rules) are [ ] suitable for this business   [ ] need the changes listed above.

Name: ______________________  Role: ______________________
Signature: __________________  Date: ______________________
~~~

## Evidence to keep

1. This runbook copied and filled in for the month, with every step initialled.
2. The ten-figure comparison sheet and the variance log.
3. The printed PDFs from step 16.
4. The signed statement above.
5. The install report and log from the machine where the pilot runs
   (C:\AlMahmoud\logs\install-report-*.json), so the reviewed build is identified.
6. One new handoff entry in docs/03-handoff.md with the reviewer name, month,
   result and where the evidence lives.

## Verification

The record is complete when the signed statement, the filled script, the
comparison sheet, the variance log and the PDFs exist in the evidence folder,
and the handoff entry names the reviewer. A signature without the filled script
is not evidence; the script without the signature is not a sign-off.

## Rollback

The review writes to a dedicated pilot company only. To redo it, create a new
company in the pilot tenant and run the script again; do not edit the signed
record — supersede it with a new dated record and reference the old one.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| reviewer cannot see a report | role lacks a permission | grant the accountant role, never the administrator account; record the change |
| a figure differs by a rounding cent | currency rounding configuration | check the currency decimals and the rounding account; classify as default-is-wrong if the practice differs |
| month close refuses because of an unposted document | draft documents block the close by design | either post or delete the draft; record which and why |
| reviewer finds the chart does not match their practice | inferred defaults (ADR 0003) | log it as default-is-wrong, agree the mapping, and record whether it is configured per company or needs a code change |
| reviewer is not available before the kill-date | owner scheduling | escalate to the owner; the sign-off cannot be manufactured or delegated to the build team |
