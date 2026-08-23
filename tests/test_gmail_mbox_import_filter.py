import csv
import contextlib
import io
import mailbox
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import gmail_mbox_import_filter as migration


def make_message(
    subject: str,
    sender: str = "Service <notice@example.com>",
    labels: str = "Inbox,Important",
    message_id: str = "<unique@example.com>",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "former.address@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 03 Feb 2020 10:11:12 -0800"
    message["Message-ID"] = message_id
    message["X-Gmail-Labels"] = labels
    message.set_content("Test body.\nFrom lines in bodies must remain safe.")
    return message


def classify(subject: str, sender: str, labels: str = "Inbox,Important") -> migration.Decision:
    decision, _metadata = migration.classify_message(
        make_message(subject, sender, labels), include_spam_trash=False
    )
    return decision


class ClassificationTests(unittest.TestCase):
    def test_job_alert_is_not_an_application_record(self):
        decision = classify(
            'Jobs like "Software Engineer" in Portland',
            "Jobs beBee <alert@notification.bebee.com>",
        )
        self.assertEqual(decision.action, "EXCLUDE")
        self.assertEqual(decision.category, "91_JOB_ALERTS_AND_RECRUITING_MARKETING")

    def test_specific_application_is_imported(self):
        decision = classify(
            "Your application to Frontend Engineer at Axiom",
            "LinkedIn <jobs-noreply@linkedin.com>",
        )
        self.assertEqual(decision.action, "IMPORT")
        self.assertEqual(decision.category, "02_JOB_APPLICATION_RECORDS")

    def test_marketing_from_important_domains_is_excluded(self):
        examples = [
            ("Price alert: Bitcoin is down 4%", "Coinbase <no-reply@mail.coinbase.com>"),
            ("Lunch Lady from your Steam wishlist is now on sale!", "Steam <noreply@steampowered.com>"),
            ("This Week's Featured Deals", "Trip.com <trip.com@newsletter.trip.com>"),
        ]
        for subject, sender in examples:
            with self.subTest(subject=subject):
                self.assertEqual(classify(subject, sender).action, "EXCLUDE")

    def test_redundant_delivery_and_routine_food_receipts_are_excluded(self):
        examples = [
            ("Your Amazon.com order #112-1234567-1234567 has shipped", "Amazon <shipment-tracking@amazon.com>"),
            ("Grubhub - Order Receipt #387003562", "Grubhub <no-reply@tapingo-grubhub.com>"),
            ("Order Confirmation for Customer from Little Big Burger", "DoorDash <no-reply@doordash.com>"),
        ]
        for subject, sender in examples:
            with self.subTest(subject=subject):
                self.assertEqual(classify(subject, sender).action, "EXCLUDE")

    def test_precise_records_are_imported(self):
        examples = [
            ("Your receipt from Apple.", "Apple <no_reply@email.apple.com>", "05_PURCHASES_ORDER_HISTORY"),
            ("Thank you for your Steam purchase!", "Steam <noreply@steampowered.com>", "07_GAMING_ACCOUNT_PURCHASES"),
            ("Payment received", "PayPal <service@paypal.com>", "01_FINANCE_TAX_RECORDS"),
            ("eTicket Itinerary and Receipt for Confirmation ABC123", "United <receipts@united.com>", "04_TRAVEL_RESERVATIONS"),
        ]
        for subject, sender, category in examples:
            with self.subTest(subject=subject):
                decision = classify(subject, sender)
                self.assertEqual(decision.action, "IMPORT")
                self.assertEqual(decision.category, category)

    def test_transient_security_is_checklist_only(self):
        examples = [
            ("Security alert", "Google <no-reply@accounts.google.com>"),
            ("919853 is your 6-digit code", "Klarna <noreply-us@klarna.com>"),
        ]
        for subject, sender in examples:
            with self.subTest(subject=subject):
                decision = classify(subject, sender)
                self.assertEqual(decision.action, "EXCLUDE")
                self.assertEqual(decision.category, "90_TRANSIENT_SECURITY_NOTICES")
                self.assertIn("security_login_or_verification", decision.account_evidence)

    def test_spam_or_trash_wins_over_receipt(self):
        decision = classify(
            "Your receipt from Apple.",
            "Apple <no_reply@email.apple.com>",
            "Trash,Category Purchases",
        )
        self.assertEqual(decision.category, "93_SPAM_TRASH")


class MboxTests(unittest.TestCase):
    def test_raw_write_preserves_original_date_and_uses_it_for_envelope(self):
        message = make_message(
            "Payment received",
            "PayPal <service@paypal.com>",
        )
        raw = message.as_bytes()
        parsed_date = migration.parse_message_date(message["Date"])
        output = io.BytesIO()
        migration.write_raw_message(output, raw, parsed_date)
        saved = output.getvalue()

        self.assertTrue(saved.startswith(b"From MAILER-DAEMON Mon Feb 03 18:11:12 2020\n"))
        self.assertIn(b"Date: Mon, 03 Feb 2020 10:11:12 -0800", saved)
        self.assertIn(b"\n>From lines in bodies", saved)

    def test_end_to_end_deduplicates_across_sources_and_writes_checklist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.mbox"
            second = root / "second.mbox"
            out = root / "output"

            receipt = make_message(
                "Payment received",
                "PayPal <service@paypal.com>",
                message_id="<same@paypal.com>",
            )
            security = make_message(
                "Security alert",
                "Google <no-reply@accounts.google.com>",
                message_id="<security@google.com>",
            )
            for path, messages in ((first, [receipt, security]), (second, [receipt])):
                with path.open("wb") as output:
                    for message in messages:
                        migration.write_raw_message(
                            output,
                            message.as_bytes(),
                            migration.parse_message_date(message["Date"]),
                        )

            result = migration.main(
                [
                    "--source",
                    f"one@gmail.com={first}",
                    "--source",
                    f"two@gmail.com={second}",
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(result, 0)

            first_box = mailbox.mbox(out / "one_gmail.com" / "01_FINANCE_TAX_RECORDS.mbox")
            second_box = mailbox.mbox(out / "two_gmail.com" / "01_FINANCE_TAX_RECORDS.mbox")
            try:
                self.assertEqual(len(first_box), 1)
                self.assertEqual(len(second_box), 0)
                self.assertEqual(first_box[0]["Date"], "Mon, 03 Feb 2020 10:11:12 -0800")
            finally:
                first_box.close()
                second_box.close()

            with (out / "ACCOUNT_CHANGE_CHECKLIST.csv").open(encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
            self.assertEqual({row["service_domain"] for row in rows}, {"google.com", "paypal.com"})
            self.assertTrue((out / "ACCOUNT_CHANGE_CHECKLIST.md").is_file())


class ConfigurationTests(unittest.TestCase):
    def test_category_names_and_numbers_can_be_selected(self):
        self.assertEqual(
            migration.parse_categories("finance, 4, gaming"),
            {
                "01_FINANCE_TAX_RECORDS",
                "04_TRAVEL_RESERVATIONS",
                "07_GAMING_ACCOUNT_PURCHASES",
            },
        )

    def test_all_category_selection_is_default(self):
        self.assertEqual(migration.parse_categories(None), set(migration.IMPORT_CATEGORIES))

    def test_terminal_theme_auto_detects_dark_and_light_backgrounds(self):
        self.assertEqual(
            migration.detect_terminal_theme("auto", environment={"COLORFGBG": "15;0"}, is_terminal=True),
            "dark",
        )
        self.assertEqual(
            migration.detect_terminal_theme("auto", environment={"COLORFGBG": "0;15"}, is_terminal=True),
            "light",
        )

    def test_terminal_theme_respects_overrides_and_plain_output(self):
        self.assertEqual(
            migration.detect_terminal_theme(
                "auto",
                environment={"GMAIL_EMIGRATION_THEME": "light"},
                is_terminal=True,
            ),
            "light",
        )
        self.assertEqual(
            migration.detect_terminal_theme("dark", environment={"NO_COLOR": ""}, is_terminal=True),
            "none",
        )
        self.assertEqual(migration.detect_terminal_theme("dark", environment={}, is_terminal=False), "none")

    def test_terminal_theme_emits_ansi_only_for_a_terminal(self):
        try:
            migration.configure_terminal_theme("dark", environment={}, is_terminal=True)
            self.assertIn("\033[", migration.colorize("Complete", "success"))
            migration.configure_terminal_theme("dark", environment={}, is_terminal=False)
            self.assertNotIn("\033[", migration.colorize("Complete", "success"))
        finally:
            migration.configure_terminal_theme("none", environment={}, is_terminal=True)

    def test_interactive_path_accepts_plain_spaces_and_shell_escapes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(temp_dir) / "All mail Including Spam and Trash-002.mbox"
            expected.touch()

            self.assertEqual(migration.normalize_interactive_path(str(expected)), expected.resolve())
            escaped = str(expected).replace(" ", r"\ ")
            self.assertEqual(migration.normalize_interactive_path(escaped), expected.resolve())

    def test_interactive_path_accepts_quotes_and_file_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(temp_dir) / "All mail.mbox"
            expected.touch()
            escaped = str(expected).replace(" ", r"\ ")

            self.assertEqual(migration.normalize_interactive_path(f'"{escaped}"'), expected.resolve())
            self.assertEqual(migration.normalize_interactive_path(expected.as_uri()), expected.resolve())

    def test_guided_mode_accepts_one_source_and_selected_categories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "takeout.mbox"
            output_dir = root / "migration"
            message = make_message("Payment received", "PayPal <service@paypal.com>")
            with source.open("wb") as output:
                migration.write_raw_message(
                    output,
                    message.as_bytes(),
                    migration.parse_message_date(message["Date"]),
                )

            answers = iter(
                [
                    "1",
                    "former.address@gmail.com",
                    "1",
                    "new.address@icloud.com",
                    "",
                    "",
                    "1,6",
                    "",
                    "",
                ]
            )
            with patch("gmail_mbox_import_filter.discover_mbox_files", return_value=[source.resolve()]):
                with patch("builtins.input", side_effect=lambda _prompt: next(answers)):
                    result = migration.main(
                        [
                            "--out",
                            str(output_dir),
                            "--preview-limit",
                            "10",
                            "--no-open-report",
                        ]
                    )

            self.assertEqual(result, 0)
            note = (output_dir / "ACCOUNT_CHANGE_CHECKLIST.md").read_text(encoding="utf-8")
            self.assertIn("new.address@icloud.com", note)
            self.assertTrue((output_dir / "former.address_gmail.com" / "01_FINANCE_TAX_RECORDS.mbox").is_file())
            self.assertFalse((output_dir / "former.address_gmail.com" / "07_GAMING_ACCOUNT_PURCHASES.mbox").exists())

    def test_flag_mode_refuses_nonempty_output_without_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "takeout.mbox"
            output_dir = root / "existing-output"
            output_dir.mkdir()
            marker = output_dir / "keep.txt"
            marker.write_text("do not overwrite", encoding="utf-8")
            message = make_message("Payment received", "PayPal <service@paypal.com>")
            with source.open("wb") as output:
                migration.write_raw_message(
                    output,
                    message.as_bytes(),
                    migration.parse_message_date(message["Date"]),
                )

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    migration.main(
                        [
                            "--source",
                            f"former.address@gmail.com={source}",
                            "--out",
                            str(output_dir),
                        ]
                    )
            self.assertEqual(marker.read_text(encoding="utf-8"), "do not overwrite")


if __name__ == "__main__":
    unittest.main()
