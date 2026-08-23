# Gmail Emigration

Gmail Emigration is a privacy-first terminal tool for moving only useful Gmail history into iCloud Mail. It reads Google Takeout `.mbox` files locally, separates durable records from noise, builds smaller import-ready mailboxes, and creates a ranked checklist of online accounts that may still use the old Gmail address.

The program never signs in to Gmail or iCloud, never uploads message content, and never changes the original Takeout files.

## What it keeps

The default rules are deliberately conservative. They retain precise records such as:

- financial statements, payment confirmations, transfers, and tax documents
- specific job applications, interviews, assessments, offers, and decisions
- education, student-aid, government, and legal records
- travel confirmations, itineraries, tickets, changes, and refunds
- completed order confirmations, receipts, cancellations, returns, and refunds
- durable account, credential, membership, and subscription changes
- gaming purchases and subscriptions

Newsletters, promotions, job alerts, temporary security codes, shipping updates, Spam/Trash, and routine food-delivery receipts are excluded by default. Security and verification emails can still identify an account for the change checklist without being copied into iCloud.

## Requirements

- Python 3.10 or newer
- One or two extracted Gmail `.mbox` files from [Google Takeout](https://takeout.google.com/)
- Apple Mail on a Mac for importing the filtered mailboxes and copying them to iCloud
- Enough free local and iCloud storage for the selected messages

When creating the Takeout archive, select Gmail/Mail. Extract the downloaded archive before running this tool; do not point it at the `.zip` or `.tgz` file.

## Install and run

Clone the repository, then install it in an isolated environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
gmail-emigration
```

Running `gmail-emigration` without arguments starts guided setup. It will:

1. Explain the Google Takeout requirement.
2. Ask whether one or two Gmail accounts are being migrated.
3. Detect likely `.mbox` files or accept their full paths.
4. Ask for the new iCloud address.
5. Preview up to 2,500 messages per source without writing output.
6. Show the durable-record categories and let the user select which to export.
7. Confirm the output folder and run the complete scan with progress.
8. Validate every generated mailbox.
9. Offer to open the ranked account-change note.

No third-party runtime packages are required. The script can also be run directly:

```bash
python3 gmail_mbox_import_filter.py
```

## Non-interactive usage

One account:

```bash
gmail-emigration '/path/to/All mail.mbox' \
  --account former.address@gmail.com \
  --new-email new.address@icloud.com \
  --out gmail_icloud_migration
```

Two accounts with selected categories:

```bash
gmail-emigration \
  --source 'first.address@gmail.com=/path/to/first/All mail.mbox' \
  --source 'second.address@gmail.com=/path/to/second/All mail.mbox' \
  --new-email new.address@icloud.com \
  --categories finance,jobs,school,travel,accounts \
  --out gmail_icloud_migration
```

Useful flags:

- `--categories all` or comma-separated numbers/names such as `finance,travel,accounts`
- `--interactive` to use guided preview while supplying source paths as flags
- `--preview-limit 5000` to change the interactive sample size
- `--include-routine-purchases` to retain routine delivery receipts
- `--include-spam-trash` to allow precise rules to consider Spam/Trash
- `--open-report` to open the Markdown checklist after a flag-based run
- `--no-open-report` to suppress the final prompt in guided mode
- `--overwrite-output` to intentionally reuse a non-empty output folder in flag-based mode
- `--version` to print the installed version

Run `gmail-emigration --help` for the complete interface.

## Generated files

The output directory contains:

- `ACCOUNT_CHANGE_CHECKLIST.md` — readable, ranked checklist with checkboxes and first-party service/account links when known
- `ACCOUNT_CHANGE_CHECKLIST.csv` — the same evidence in sortable spreadsheet form
- `IMPORT_THESE_FILES.txt` — the authoritative list of non-empty mailboxes to import exactly once
- `MBOX_VALIDATION.json` — count, duplicate, date-header, and 20 MB message-limit checks
- `MIGRATION_SUMMARY.json` — run settings and aggregate results
- one folder per source account containing category `.mbox` files
- `MESSAGE_DECISIONS.csv` — an explanation for every import/review/exclusion decision
- `MANUAL_REVIEW.csv` — record-like attachments that were deliberately not auto-imported

All migration outputs and `.mbox` files are ignored by Git because they can contain private information.

## Message dates and integrity

Messages are copied as original RFC bytes rather than reconstructed with Python's email serializer. Original `Date`, `Message-ID`, sender, recipient, MIME, and attachment data remain intact. The mbox envelope separator also uses the original parsed message date.

After export, the program reopens every mailbox and checks:

- expected versus actual message counts
- duplicate Message-IDs across output files
- missing or unparseable original date headers
- individual messages exceeding iCloud Mail's documented 20 MB limit

Import one small category first and visually confirm several dates and attachments before uploading the remainder.

## Import into iCloud Mail

The filtered files are local `.mbox` files, so use Apple Mail rather than iCloud's direct provider-import feature:

1. Confirm that `MBOX_VALIDATION.json` reports `PASS`.
2. Review any rows in `MANUAL_REVIEW.csv`.
3. Open Mail on the Mac and choose **File > Import Mailboxes**.
4. Select **Files in mbox format** and import only the files listed in `IMPORT_THESE_FILES.txt`.
5. Inspect the imported messages under **On My Mac > Import**.
6. Create destination folders under the iCloud account and move or copy the imported mailboxes there.
7. Wait for synchronization and verify the result on iCloud.com or another device.
8. Keep the original Takeout archive until the migration is fully verified.

Apple documentation:

- [Import or export mailboxes in Mail on Mac](https://support.apple.com/guide/mail/mlhlp1030/mac)
- [Move or copy mailboxes in Mail on Mac](https://support.apple.com/guide/mail/mlhlp1231/mac)
- [iCloud Mail storage and message limits](https://support.apple.com/102198)

## Account-change checklist safety

Candidate services are inferred from sender domains and precise account/transaction evidence. They are not proof that an account is still active. `P0` rows should be handled first, `P1` rows have strong account evidence, and `P2` rows are possible accounts that need confirmation.

Known services link to first-party account pages. Unknown services link only to their inferred root domain—the tool never trusts or copies login links from an email body. Verify every domain independently, preferably through a trusted app, password manager, or manually typed address, before entering credentials.

## Development

Run the test suite with the standard library:

```bash
python3 -m unittest discover -s tests -v
```

The project is released under the [MIT License](LICENSE).
