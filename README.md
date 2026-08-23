# Gmail Emigration

<p align="center">
  <img src="gmail_icon.jpg" alt="Gmail app icon" height="140">
  &nbsp;&nbsp;&nbsp;→&nbsp;&nbsp;&nbsp;
  <img src="icloud_icon.jpg" alt="iCloud app icon" height="140">
</p>

Create a small, high-confidence Gmail-to-iCloud migration set from Google Takeout
`.mbox` files. Everything runs locally: the tool never signs in, uploads mail, or
changes the original archive.

It keeps durable records—finance, applications, school/government/legal, travel,
orders, account changes, and gaming purchases—while excluding newsletters, job
alerts, temporary codes, routine delivery mail, Spam, and Trash by default. It also
builds a checklist of accounts that may still use the old Gmail address.

## Quick start

Requires Python 3.10+, an extracted Gmail export from
[Google Takeout](https://takeout.google.com/), and Apple Mail for the final import.

```bash
git clone https://github.com/ASVPATM/gmail-emigration.git
cd gmail-emigration
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
gmail-emigration
```

Guided setup accepts one or two Gmail accounts, previews the filter, lets you choose
categories, and validates every output mailbox. Paste paths normally, shell-escaped,
quoted, or by dragging the `.mbox` file into the terminal. Arrow-key editing and
in-session history are supported.

## Output

- category `.mbox` files containing original RFC message bytes and dates
- `IMPORT_THESE_FILES.txt` with the exact files to import once
- `ACCOUNT_CHANGE_CHECKLIST.md` and `.csv` for email-change work
- `MESSAGE_DECISIONS.csv` and `MANUAL_REVIEW.csv` for audit/review
- `MBOX_VALIDATION.json` and `MIGRATION_SUMMARY.json`

Generated mailboxes and reports are ignored by Git because they can contain private
data.

## Import into iCloud

1. Confirm `MBOX_VALIDATION.json` says `PASS`.
2. In Apple Mail, choose **File > Import Mailboxes > Files in mbox format**.
3. Import only the paths in `IMPORT_THESE_FILES.txt`.
4. Apple Mail creates `Import` mailboxes. Move their messages into iCloud folders—or
   select all messages and use **Message > Move to > iCloud > Inbox**.
5. Wait for sync, verify on iCloud.com, then delete the empty Import mailboxes.
6. Keep the original Takeout archive until everything is verified.

Apple guides: [import mailboxes](https://support.apple.com/guide/mail/mlhlp1030/mac),
[move messages](https://support.apple.com/guide/mail/mlhlp1000/mac), and
[iCloud Mail limits](https://support.apple.com/102198).

## Options

Run `gmail-emigration --help` for flag-based and two-account usage. Common options:

- `--categories finance,travel,accounts`
- `--include-routine-purchases` or `--include-spam-trash`
- `--preview-limit 5000`
- `--overwrite-output` for an intentional output-folder replacement
- `--theme auto|dark|light|none`

Color defaults to `auto`. Terminals exposing `COLORFGBG` get a dark/light theme;
otherwise the dark theme is used. Set `GMAIL_EMIGRATION_THEME=light` to override it,
or `NO_COLOR=1`/`--theme none` for plain output. Redirected output is always plain.

## Safety

The filter is conservative, not infallible. Review `MANUAL_REVIEW.csv`, test one
small mailbox first, and verify dates and attachments. Checklist links use known
first-party account pages or inferred root domains—never login links copied from email
bodies. Confirm every domain before entering credentials.

Run tests with `python3 -m unittest discover -s tests -v`.

MIT licensed. See [LICENSE](LICENSE). Gmail and iCloud are trademarks of their
respective owners; this independent project is not affiliated with Google or Apple.
