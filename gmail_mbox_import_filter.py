#!/usr/bin/env python3

"""Build a conservative Gmail-to-iCloud migration set from Google Takeout mbox files.

The filter intentionally separates two questions:

1. Is this message a durable record worth importing?
2. Does this message prove that an online account may still use the Gmail address?

Short-lived security codes, login alerts, newsletters, job alerts, and ordinary marketing
can therefore help populate ACCOUNT_CHANGE_CHECKLIST.csv without being copied to iCloud.
Original RFC message bytes are copied to output mboxes; headers, MIME parts, attachments,
Message-ID, and Date are not reserialized.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import mailbox
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import Message
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

try:
    import readline as _readline
except ImportError:  # pragma: no cover - platform-dependent optional support
    _readline = None


VERSION = "4.2.0"

SENT_CATEGORY = "08_SENT_MAIL"
MAX_ICLOUD_MESSAGE_BYTES = 20_000_000
DATA_PART_ORDER = ("essentials", "sent", "accounts")
DATA_PART_DESCRIPTIONS = {
    "essentials": "Essential non-sent records for iCloud",
    "sent": "Sent email in one combined mailbox",
    "accounts": "Account-change checklist",
}

TERMINAL_THEMES = {
    "none": {},
    "dark": {
        "title": "\033[1;96m",
        "heading": "\033[1;94m",
        "accent": "\033[94m",
        "success": "\033[1;92m",
        "warning": "\033[1;93m",
        "muted": "\033[90m",
    },
    "light": {
        "title": "\033[1;34m",
        "heading": "\033[1;35m",
        "accent": "\033[35m",
        "success": "\033[1;32m",
        "warning": "\033[1;31m",
        "muted": "\033[90m",
    },
}
ANSI_RESET = "\033[0m"
_ACTIVE_TERMINAL_THEME: Mapping[str, str] = TERMINAL_THEMES["none"]


def detect_terminal_theme(
    requested: str = "auto",
    *,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
) -> str:
    """Choose a readable theme without putting ANSI codes in redirected output."""
    environment = os.environ if environment is None else environment
    terminal = sys.stdout.isatty() if is_terminal is None else is_terminal
    if not terminal or "NO_COLOR" in environment:
        return "none"

    selected = requested.casefold()
    override = environment.get("GMAIL_EMIGRATION_THEME", "").casefold()
    if selected == "auto" and override in TERMINAL_THEMES:
        selected = override
    if selected in TERMINAL_THEMES:
        return selected

    color_fgbg = environment.get("COLORFGBG", "")
    try:
        background = int(color_fgbg.split(";")[-1]) % 16
    except (TypeError, ValueError):
        background = 0
    return "light" if background in {7, 9, 10, 11, 12, 13, 14, 15} else "dark"


def configure_terminal_theme(
    requested: str = "auto",
    *,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
) -> str:
    global _ACTIVE_TERMINAL_THEME
    selected = detect_terminal_theme(requested, environment=environment, is_terminal=is_terminal)
    _ACTIVE_TERMINAL_THEME = TERMINAL_THEMES[selected]
    return selected


def colorize(value: object, role: str) -> str:
    text = str(value)
    prefix = _ACTIVE_TERMINAL_THEME.get(role, "")
    return f"{prefix}{text}{ANSI_RESET}" if prefix else text


IMPORT_CATEGORIES = (
    "01_FINANCE_TAX_RECORDS",
    "02_JOB_APPLICATION_RECORDS",
    "03_SCHOOL_GOVERNMENT_LEGAL",
    "04_TRAVEL_RESERVATIONS",
    "05_PURCHASES_ORDER_HISTORY",
    "06_ACCOUNT_SUBSCRIPTION_CHANGES",
    "07_GAMING_ACCOUNT_PURCHASES",
)

EXCLUDE_CATEGORIES = (
    "90_TRANSIENT_SECURITY_NOTICES",
    "91_JOB_ALERTS_AND_RECRUITING_MARKETING",
    "92_MARKETING_NEWSLETTERS",
    "93_SPAM_TRASH",
    "94_CATEGORY_NOT_SELECTED",
    "95_SENT_MAIL_NOT_SELECTED",
    "98_NOT_ESSENTIAL",
)

ALL_CATEGORIES = IMPORT_CATEGORIES + ("80_MANUAL_REVIEW",) + EXCLUDE_CATEGORIES


# Domain lists are context, not sufficient reasons to import a message by themselves.
IDENTITY_DOMAINS = {
    "apple.com",
    "github.com",
    "gitlab.com",
    "google.com",
    "icloud.com",
    "live.com",
    "microsoft.com",
    "openai.com",
    "outlook.com",
    "yahoo.com",
}

FINANCE_DOMAINS = {
    "acorns.com",
    "affirm.com",
    "afterpay.com",
    "americanexpress.com",
    "bankofamerica.com",
    "capitalone.com",
    "chase.com",
    "coinbase.com",
    "creditkarma.com",
    "discover.com",
    "equifax.com",
    "experian.com",
    "fidelity.com",
    "intuit.com",
    "klarna.com",
    "paypal.com",
    "paysend.com",
    "revolut.com",
    "robinhood.com",
    "schwab.com",
    "sofi.com",
    "squareup.com",
    "stash.com",
    "stripe.com",
    "transunion.com",
    "venmo.com",
    "wellsfargo.com",
    "wise.com",
}

SCHOOL_GOV_DOMAINS = {
    "aidvantage.com",
    "collegeboard.org",
    "commonapp.org",
    "ed.gov",
    "fafsa.gov",
    "irs.gov",
    "salliemae.com",
    "ssa.gov",
    "studentaid.gov",
}

JOB_PORTAL_DOMAINS = {
    "ashbyhq.com",
    "greenhouse.io",
    "icims.com",
    "indeed.com",
    "jobvite.com",
    "lever.co",
    "linkedin.com",
    "myworkday.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workday.com",
}

JOB_NOISE_DOMAINS = {
    "bebee.com",
    "energyjobline.com",
    "glassdoor.com",
    "lensa.com",
    "neuvoo.com",
    "nexxt.com",
    "talent.com",
    "wayup.com",
    "ziprecruiter.com",
}

TRAVEL_DOMAINS = {
    "aa.com",
    "airbnb.com",
    "alaskaair.com",
    "amtrak.com",
    "booking.com",
    "delta.com",
    "expedia.com",
    "hotels.com",
    "flyfrontier.com",
    "greyhound.com",
    "klm.com",
    "priceline.com",
    "trip.com",
    "tsa.gov",
    "united.com",
}

SHOPPING_DOMAINS = {
    "amazon.com",
    "bestbuy.com",
    "depop.com",
    "ebay.com",
    "etsy.com",
    "grailed.com",
    "mercari.com",
    "newegg.com",
    "stockx.com",
    "target.com",
    "walmart.com",
}

GAMING_DOMAINS = {
    "battle.net",
    "blizzard.com",
    "ea.com",
    "epicgames.com",
    "gog.com",
    "nintendo.com",
    "playstation.com",
    "riotgames.com",
    "steamcommunity.com",
    "steampowered.com",
    "swtor.com",
    "xbox.com",
}

CATEGORY_DESCRIPTIONS = {
    "01_FINANCE_TAX_RECORDS": "Statements, payments, transfers, tax documents, and financial receipts",
    "02_JOB_APPLICATION_RECORDS": "Specific applications, interviews, assessments, offers, and decisions",
    "03_SCHOOL_GOVERNMENT_LEGAL": "Education, student-aid, government, and legal records",
    "04_TRAVEL_RESERVATIONS": "Travel confirmations, itineraries, tickets, changes, and refunds",
    "05_PURCHASES_ORDER_HISTORY": "Completed orders, receipts, cancellations, returns, and refunds",
    "06_ACCOUNT_SUBSCRIPTION_CHANGES": "Durable account, credential, membership, and subscription changes",
    "07_GAMING_ACCOUNT_PURCHASES": "Gaming account purchases and subscriptions",
}

# Known first-party account pages. Unknown services link to their inferred root domain rather
# than guessing a /login path. The generated note warns users to verify every destination.
KNOWN_ACCOUNT_URLS = {
    "amazon.com": "https://www.amazon.com/your-account",
    "apple.com": "https://account.apple.com/",
    "ea.com": "https://myaccount.ea.com/",
    "ebay.com": "https://www.ebay.com/signin/",
    "github.com": "https://github.com/settings/profile",
    "google.com": "https://myaccount.google.com/",
    "linkedin.com": "https://www.linkedin.com/mypreferences/d/manage-email-addresses",
    "microsoft.com": "https://account.microsoft.com/",
    "paypal.com": "https://www.paypal.com/signin",
    "steamcommunity.com": "https://store.steampowered.com/account/",
    "steampowered.com": "https://store.steampowered.com/account/",
    "studentaid.gov": "https://studentaid.gov/fsa-id/sign-in/landing",
}

WORK_GIG_DOMAINS = {
    "dasherdirect.com",
    "doordash.com",
    "payfare.com",
    "uber.com",
}

ROUTINE_PURCHASE_DOMAINS = {
    "doordash.com",
    "grubhub.com",
    "starbucks.com",
    "tapingo.com",
    "tapingo-grubhub.com",
    "uber.com",
    "ubereats.com",
}

# A payment-processor sender can carry a merchant receipt without the recipient having a
# consumer account at the processor itself.
PAYMENT_PROCESSOR_DOMAINS = {"squareup.com", "stripe.com"}

COMMON_SECOND_LEVEL_SUFFIXES = {
    "ac.uk",
    "co.jp",
    "co.nz",
    "co.uk",
    "com.au",
    "com.br",
    "com.cn",
    "com.hk",
    "com.mx",
    "com.sg",
    "com.tr",
    "gov.uk",
    "net.au",
    "org.uk",
}


def rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


SPAM_LABELS = {"spam", "trash"}

MARKETING_PATTERNS = rx(
    r"\b(?:flash |summer |holiday |weekend )?sale\b",
    r"\b\d{1,3}%\s*off\b",
    r"\b(?:special|exclusive|limited[ -]time) offer\b",
    r"\b(?:shop|buy|apply) now\b",
    r"\b(?:new arrivals?|wishlist|recommended for you|you may like)\b",
    r"\b(?:daily|weekly|personalized) (?:digest|newsletter|roundup)\b",
    r"\b(?:top|today'?s) (?:stories|news|picks)\b",
    r"\bprice alert\b",
    r"\b(?:bonus|reward) (?:ends|offer|opportunity)\b",
    r"\bhandpicked\b",
)

JOB_NOISE_PATTERNS = rx(
    r"\bjob alerts?\b",
    r"\bjobs? (?:like|matching|you may like|for you)\b",
    r"\b(?:new|recommended|similar|handpicked) jobs?\b",
    r"\b\d+ more .+ jobs?\b",
    r"\bis hiring\b",
    r"\bbe the first to apply\b",
    r"\bjob leads?\b",
    r"\bsaved search\b",
    r"\bcareer opportunities\b",
    r"\blooking for a new job\b",
    r"\bgreat fit for (?:a|this) position\b",
)

JOB_RECORD_PATTERNS = rx(
    r"\b(?:we (?:have )?received|thanks? you for|thanks for) (?:your )?(?:job )?application\b",
    r"\b(?:job )?application (?:received|submitted|confirmation|status|update)\b",
    r"\byour application (?:to|for|with|has|was|is)\b",
    r"\bupdate (?:on|regarding) your application\b",
    r"\b(?:interview|phone screen|screening call) (?:invitation|confirmation|request|scheduled|availability)\b",
    r"\b(?:coding|technical|candidate|pre-employment) assessment\b",
    r"\bbackground check\b",
    r"\b(?:employment|job) offer\b",
    r"\boffer letter\b",
    r"\b(?:not selected|will not be moving forward|won'?t be moving forward)\b",
    r"\bthank you for interviewing\b",
    r"\bindeed application:\b",
)

FINANCE_RECORD_PATTERNS = rx(
    r"\b(?:monthly|annual|quarterly|tax|billing|bank|credit card|loan|brokerage) statement (?:is )?(?:available|ready|attached|enclosed)\b",
    r"\b(?:statement|e-?statement) (?:is )?(?:available|ready)\b",
    r"\b(?:payment|transfer) (?:confirmation|receipt|received|sent|processed|posted|scheduled|failed|declined|returned|canceled|cancelled)\b",
    r"\byou (?:made|sent|received) (?:a )?(?:payment|transfer)\b",
    r"\b(?:refund|reimbursement) (?:issued|processed|sent|received|approved|completed)\b",
    r"\b(?:transaction|trade) (?:confirmation|receipt)\b",
    r"\b(?:tax document|tax return|tax transcript)\b",
    r"\b(?:form )?(?:1095-[a-z]|1098(?:-[a-z])?|1099(?:-[a-z]+)?|w-?2)\b",
    r"\b(?:deposit|withdrawal) (?:confirmation|received|posted|completed)\b",
    r"\byour .{0,30}receipt\b",
    r"\bautomatic payment (?:confirmation|processed|scheduled)\b",
    r"\bchargeback|dispute (?:opened|resolved|decision)\b",
)

SCHOOL_GOV_RECORD_PATTERNS = rx(
    r"\b(?:fafsa|financial aid) (?:form )?(?:received|processed|submitted|correction|summary|decision|offer|award)\b",
    r"\bwe(?:'|’)ve received your fafsa\b",
    r"\b(?:student|education) loan (?:approved|disbursed|statement|payment|document)\b",
    r"\b(?:tuition|student account) (?:statement|payment|receipt|balance)\b",
    r"\b(?:admission|application) decision\b",
    r"\b(?:official )?transcript (?:order|request|available|sent|received)\b",
    r"\b(?:tax|benefit|government) document (?:available|ready|issued)\b",
    r"\b(?:case|claim|filing) (?:number|status|decision|received|confirmation)\b",
    r"\b(?:visa|passport) (?:application|renewal) (?:received|approved|status|confirmation)\b",
    r"\b(?:court|dmv) (?:notice|confirmation|record|receipt)\b",
)

TRAVEL_RECORD_PATTERNS = rx(
    r"\b(?:flight|hotel|travel|booking|reservation) confirmation\b",
    r"\bbooking (?:has been |is )?(?:confirmed|canceled|cancelled|changed|updated)\b",
    r"\b(?:reservation|trip) (?:has been |is )?(?:confirmed|canceled|cancelled|changed|updated)\b",
    r"\b(?:e-?ticket|ticket) (?:itinerary|receipt|issued|confirmation)\b",
    r"\bitinerary and receipt\b",
    r"\b(?:your )?itinerary\b",
    r"\bboarding pass\b",
    r"\b(?:seat|train) (?:reservation|selection) (?:confirmed|successful)\b",
    r"\b(?:travel|flight|hotel|booking) refund\b",
    r"\b(?:check[ -]?in) (?:confirmation|is open)\b",
)

PURCHASE_RECORD_PATTERNS = rx(
    r"\b(?:your |final )?receipt (?:from|for)\b",
    r"\b(?:purchase|order) (?:confirmation|confirmed|receipt)\b",
    r"\bthanks? for (?:your )?(?:order|purchase)\b",
    r"\byour .{0,30}order (?:number|#)\s*[a-z0-9-]{4,}\s*$",
    r"\byour order (?:has been |was |is )?(?:confirmed|placed|canceled|cancelled|refunded)\b",
    r"\b(?:item|order) (?:has been |was |is )?(?:canceled|cancelled|refunded)\b",
    r"\b(?:return|refund) (?:confirmation|received|accepted|processed|issued|completed)\b",
    r"\byour item (?:has )?sold\b",
    r"\bfunds (?:are|will be) available\b",
    r"\bsubscription (?:purchase|receipt)\b",
    r"\binvoice (?:and )?receipt\b",
)

REDUNDANT_PURCHASE_LIFECYCLE_PATTERNS = rx(
    r"\b(?:order|item|package) (?:has been |was |is )?(?:shipped|delivered|out for delivery)\b",
    r"\b(?:shipped|delivered|out for delivery):?.*\border\b",
    r"\btracking (?:number|update|information)\b",
    r"\border is ready for (?:pickup|collection)\b",
)

ACCOUNT_DURABLE_PATTERNS = rx(
    r"\b(?:your )?account (?:has been |was |is )?(?:created|registered|closed|deleted|suspended|reopened)\b",
    r"\bemail address (?:has been |was )?(?:changed|updated)\b",
    r"\b(?:recovery|contact) (?:email|phone|information) (?:has been |was )?(?:changed|updated|removed|added)\b",
    r"\bpassword (?:has been |was )?changed\b",
    r"\b(?:subscription|membership) (?:is )?(?:confirmed|renewed|canceled|cancelled|expired|activated|deactivated)\b",
    r"\b(?:subscription|membership) (?:change|cancellation|renewal) confirmation\b",
    r"\baccount closure confirmation\b",
)

TRANSIENT_SECURITY_PATTERNS = rx(
    r"\b(?:verification|security|sign[ -]?in|login|authentication|one[ -]?time) code\b",
    r"\b\d{4,8}\s+is your (?:\d+[- ]digit )?(?:verification |security )?code\b",
    r"\byour .{0,30}(?:verification|security) code is\b",
    r"\b(?:otp|2fa)\b",
    r"\bnew (?:sign[ -]?in|login|device)\b",
    r"\baccess from (?:a )?new (?:computer|browser|device|web|mobile)\b",
    r"\b(?:sign[ -]?in|login) attempt\b",
    r"\bpassword reset\b",
    r"\breset your password\b",
    r"\bverify your (?:email|account|identity)\b",
    r"\bconfirm your email\b",
    r"\bactivate your account\b",
    r"\bsecurity alert\b",
    r"\bsuspicious (?:activity|login|sign[ -]?in)\b",
    r"\baccount recovery\b",
    r"\btrusted device\b",
    r"\bdisplay name recovery request\b",
)

GAMING_RECORD_PATTERNS = rx(
    r"\b(?:gaming|game|steam|playstation|xbox|nintendo|epic|battle\.net|ea).*(?:purchase|receipt|subscription)\b",
    r"\bthank you for your (?:steam|playstation|xbox|nintendo|epic|ea) purchase\b",
    r"\b(?:game time|in-game (?:credit|currency)) (?:purchase|receipt|confirmation)\b",
)

ACCOUNT_EXISTENCE_PATTERNS = rx(
    r"\bwelcome to (?:your )?.{0,40}account\b",
    r"\byou (?:already )?have (?:an? )?.{0,30}account\b",
)

DOCUMENT_FILENAME_PATTERNS = rx(
    r"(?:statement|invoice|receipt|tax|1099|w-?2|contract|transcript|itinerary|ticket|order).+\.(?:pdf|docx?|xlsx?|csv)$",
)


@dataclass(frozen=True)
class SourceSpec:
    account: str
    path: Path


@dataclass
class Decision:
    action: str
    category: str
    rule_id: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    account_evidence: list[str] = field(default_factory=list)

    @property
    def should_import(self) -> bool:
        return self.action == "IMPORT"


@dataclass
class AccountCandidate:
    service_key: str
    service_domain: str
    old_accounts: set[str] = field(default_factory=set)
    sender_names: Counter[str] = field(default_factory=Counter)
    sender_emails: Counter[str] = field(default_factory=Counter)
    evidence: Counter[str] = field(default_factory=Counter)
    imported_categories: Counter[str] = field(default_factory=Counter)
    total_relevant_messages: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    samples: list[tuple[datetime | None, str]] = field(default_factory=list)

    def add(
        self,
        *,
        account: str,
        sender_name: str,
        sender_email: str,
        evidence: Iterable[str],
        imported_category: str | None,
        date_value: datetime | None,
        subject: str,
    ) -> None:
        self.old_accounts.add(account)
        if sender_name:
            self.sender_names[sender_name] += 1
        if sender_email:
            self.sender_emails[sender_email] += 1
        for item in evidence:
            self.evidence[item] += 1
        if imported_category:
            self.imported_categories[imported_category] += 1
        self.total_relevant_messages += 1

        if date_value:
            if self.first_seen is None or date_value < self.first_seen:
                self.first_seen = date_value
            if self.last_seen is None or date_value > self.last_seen:
                self.last_seen = date_value

        if subject:
            self.samples.append((date_value, subject))
            self.samples.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            del self.samples[5:]


def clean_text(value: object) -> str:
    if not value:
        return ""
    text = html.unescape(str(value)).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize(value: object) -> str:
    return clean_text(value).casefold()


def parse_name_email(value: object) -> tuple[str, str]:
    name, address = parseaddr(clean_text(value))
    return clean_text(name), clean_text(address).lower()


def sender_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip(". ").lower() if "@" in address else ""


def registrable_domain(domain: str) -> str:
    """Return a practical grouping domain without requiring a public-suffix package."""
    labels = [part for part in domain.lower().strip(".").split(".") if part]
    if len(labels) <= 2:
        return ".".join(labels)
    last_two = ".".join(labels[-2:])
    if last_two in COMMON_SECOND_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def domain_in(domain: str, candidates: set[str]) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in candidates)


def matches(text: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def parse_message_date(value: object) -> datetime | None:
    try:
        result = parsedate_to_datetime(clean_text(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if result is None:
        return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result


def iso_date(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def message_hash(msg: Message, raw_bytes: bytes, account: str, index: int) -> str:
    message_id = clean_text(msg.get("Message-ID", ""))
    if message_id:
        basis = "message-id\0" + message_id.casefold()
    else:
        # The account/index suffix prevents two unrelated malformed messages from collapsing.
        basis = "raw\0" + hashlib.sha256(raw_bytes).hexdigest() + f"\0{account}\0{index}"
    return hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()


def sent_message_hash(msg: Message, raw_bytes: bytes) -> str:
    """Create a cross-account identity for combining sent mail."""
    message_id = clean_text(msg.get("Message-ID", ""))
    basis = "message-id\0" + message_id.casefold() if message_id else "raw\0" + hashlib.sha256(raw_bytes).hexdigest()
    return hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()


def gmail_labels(msg: Message) -> set[str]:
    raw = clean_text(msg.get("X-Gmail-Labels", ""))
    return {item.strip().casefold() for item in raw.split(",") if item.strip()}


def is_sent_message(msg: Message, source_account: str) -> bool:
    """Prefer Gmail's exact Sent label, with a fallback for label-free exports."""
    labels = gmail_labels(msg)
    if "sent" in labels:
        return True
    if labels:
        return False
    _sender_name, sender_email = parse_name_email(msg.get("From", ""))
    return sender_email.casefold() == source_account.casefold()


def attachment_info(msg: Message) -> tuple[int, list[str]]:
    count = 0
    filenames: list[str] = []
    try:
        for part in msg.walk():
            filename = clean_text(part.get_filename())
            disposition = normalize(part.get("Content-Disposition", ""))
            # Do not count ordinary embedded HTML images unless they have attachment disposition.
            if "attachment" in disposition or (filename and "inline" not in disposition):
                count += 1
                if filename and len(filenames) < 20:
                    filenames.append(filename)
    except Exception:
        pass
    return count, filenames


def is_bulk_message(msg: Message) -> bool:
    precedence = normalize(msg.get("Precedence", ""))
    return bool(
        clean_text(msg.get("List-ID", ""))
        or clean_text(msg.get("List-Unsubscribe", ""))
        or precedence in {"bulk", "list", "junk"}
    )


def classify_message(
    msg: Message,
    *,
    include_spam_trash: bool,
    include_routine_purchases: bool = False,
) -> tuple[Decision, dict[str, object]]:
    sender_name, sender_email = parse_name_email(msg.get("From", ""))
    domain = sender_domain(sender_email)
    root_domain = registrable_domain(domain)
    subject = clean_text(msg.get("Subject", ""))
    subject_folded = normalize(subject)
    labels = gmail_labels(msg)
    date_value = parse_message_date(msg.get("Date", ""))
    attachment_count, attachment_names = attachment_info(msg)
    bulk = is_bulk_message(msg)
    promotional = "category promotions" in labels or matches(subject_folded, MARKETING_PATTERNS)
    job_noise = matches(subject_folded, JOB_NOISE_PATTERNS) or domain_in(domain, JOB_NOISE_DOMAINS)

    identity = domain_in(domain, IDENTITY_DOMAINS)
    finance = domain_in(domain, FINANCE_DOMAINS)
    school_gov = domain_in(domain, SCHOOL_GOV_DOMAINS) or domain.endswith(".gov") or domain.endswith(".edu")
    job_portal = domain_in(domain, JOB_PORTAL_DOMAINS)
    travel = domain_in(domain, TRAVEL_DOMAINS)
    shopping = domain_in(domain, SHOPPING_DOMAINS) or domain_in(domain, WORK_GIG_DOMAINS)
    gaming = domain_in(domain, GAMING_DOMAINS)

    job_record = matches(subject_folded, JOB_RECORD_PATTERNS)
    finance_record = matches(subject_folded, FINANCE_RECORD_PATTERNS)
    school_record = matches(subject_folded, SCHOOL_GOV_RECORD_PATTERNS)
    travel_record = matches(subject_folded, TRAVEL_RECORD_PATTERNS)
    purchase_record = matches(subject_folded, PURCHASE_RECORD_PATTERNS)
    redundant_purchase_update = matches(subject_folded, REDUNDANT_PURCHASE_LIFECYCLE_PATTERNS)
    account_durable = matches(subject_folded, ACCOUNT_DURABLE_PATTERNS)
    transient_security = matches(subject_folded, TRANSIENT_SECURITY_PATTERNS)
    account_existence = matches(subject_folded, ACCOUNT_EXISTENCE_PATTERNS)
    gaming_record = matches(subject_folded, GAMING_RECORD_PATTERNS)
    document_attachment = any(matches(normalize(name), DOCUMENT_FILENAME_PATTERNS) for name in attachment_names)

    metadata: dict[str, object] = {
        "date_value": date_value,
        "date": iso_date(date_value),
        "date_header": clean_text(msg.get("Date", "")),
        "from_name": sender_name,
        "from_email": sender_email,
        "domain": domain,
        "root_domain": root_domain,
        "subject": subject,
        "labels": " | ".join(sorted(labels)),
        "list_id": clean_text(msg.get("List-ID", "")),
        "has_unsubscribe": bool(clean_text(msg.get("List-Unsubscribe", ""))),
        "is_bulk": bulk,
        "attachment_count": attachment_count,
        "attachment_names": " | ".join(attachment_names),
    }

    account_evidence: list[str] = []
    if transient_security or account_existence:
        account_evidence.append("security_login_or_verification")
    if account_durable:
        account_evidence.append("account_or_subscription_change")

    if labels & SPAM_LABELS and not include_spam_trash:
        return Decision(
            "EXCLUDE",
            "93_SPAM_TRASH",
            "spam_or_trash_label",
            "high",
            ["Gmail labeled the message Spam or Trash"],
            account_evidence,
        ), metadata

    # School/loan/admissions context must win before generic phrases such as "your application."
    if school_gov and (school_record or finance_record or (job_record and re.search(r"\b(?:admission|application|college|university)\b", subject_folded))):
        account_evidence.append("school_government_or_loan_record")
        return Decision(
            "IMPORT",
            "03_SCHOOL_GOVERNMENT_LEGAL",
            "school_government_record",
            "high",
            ["recognized school/government/loan sender and a specific record subject"],
            account_evidence,
        ), metadata

    # Real application lifecycle messages win only when the subject is targeted. Merely being
    # sent by a job site is never enough, and school/loan applications are not job records.
    if job_record and not school_gov and not (job_noise and not re.search(r"\byour application\b|\bindeed application:\b", subject_folded)):
        account_evidence.append("job_application_profile")
        return Decision(
            "IMPORT",
            "02_JOB_APPLICATION_RECORDS",
            "targeted_job_application_subject",
            "high" if job_portal else "medium",
            ["subject describes a specific application, interview, assessment, offer, or decision"],
            account_evidence,
        ), metadata

    if job_noise:
        return Decision(
            "EXCLUDE",
            "91_JOB_ALERTS_AND_RECRUITING_MARKETING",
            "job_alert_or_listing",
            "high",
            ["job listing, recommendation, alert, or recruiting marketing—not an application record"],
            account_evidence,
        ), metadata

    if travel and travel_record:
        account_evidence.append("travel_booking")
        return Decision(
            "IMPORT",
            "04_TRAVEL_RESERVATIONS",
            "travel_confirmation_or_itinerary",
            "high",
            ["recognized travel sender and a booking, itinerary, ticket, change, or refund subject"],
            account_evidence,
        ), metadata

    if gaming and (gaming_record or purchase_record or account_durable) and not transient_security:
        account_evidence.append("gaming_account_or_purchase")
        return Decision(
            "IMPORT",
            "07_GAMING_ACCOUNT_PURCHASES",
            "gaming_account_or_transaction",
            "high",
            ["gaming account subscription or purchase record"],
            account_evidence,
        ), metadata

    if finance and (finance_record or purchase_record):
        account_evidence.append("financial_or_tax_account")
        return Decision(
            "IMPORT",
            "01_FINANCE_TAX_RECORDS",
            "specific_financial_record",
            "high",
            ["recognized financial sender and a specific statement, payment, receipt, transfer, refund, transaction, or tax record"],
            account_evidence,
        ), metadata

    if redundant_purchase_update:
        account_evidence.append("purchase_or_marketplace_account")
        return Decision(
            "EXCLUDE",
            "98_NOT_ESSENTIAL",
            "redundant_shipping_or_delivery_update",
            "high",
            ["shipping, delivery, tracking, or pickup update is redundant with the retained order record"],
            account_evidence,
        ), metadata

    # Routine food-delivery receipts are useful account evidence but not essential archives by
    # default. --include-routine-purchases is an explicit opt-in.
    if domain_in(domain, ROUTINE_PURCHASE_DOMAINS) and purchase_record and not include_routine_purchases:
        account_evidence.append("purchase_or_marketplace_account")
        return Decision(
            "EXCLUDE",
            "98_NOT_ESSENTIAL",
            "routine_food_or_delivery_receipt",
            "high",
            ["routine food/delivery purchase excluded by the essentials-only default"],
            account_evidence,
        ), metadata

    # Store/marketplace receipts should not be swallowed by the generic finance category.
    if (shopping or domain == "email.apple.com") and purchase_record:
        account_evidence.append("purchase_or_marketplace_account")
        return Decision(
            "IMPORT",
            "05_PURCHASES_ORDER_HISTORY",
            "purchase_order_lifecycle",
            "high",
            ["recognized store/marketplace and a completed order, receipt, shipment, return, or refund subject"],
            account_evidence,
        ), metadata

    if finance_record and (domain_in(domain, WORK_GIG_DOMAINS) or document_attachment):
        account_evidence.append("financial_or_tax_account")
        return Decision(
            "IMPORT",
            "01_FINANCE_TAX_RECORDS",
            "specific_financial_record",
            "high" if finance else "medium",
            ["specific statement, payment, transfer, refund, transaction, or tax record"],
            account_evidence,
        ), metadata

    # A strongly phrased receipt/order confirmation can be retained for an unlisted merchant,
    # provided it is not visibly promotional.
    if purchase_record and not promotional:
        account_evidence.append("purchase_or_marketplace_account")
        return Decision(
            "IMPORT",
            "05_PURCHASES_ORDER_HISTORY",
            "specific_purchase_record_unlisted_sender",
            "medium",
            ["specific receipt or completed order-lifecycle subject from an unlisted sender"],
            account_evidence,
        ), metadata

    if travel_record and not promotional and re.search(
        r"\b(?:flight|hotel|travel|itinerary|e-?ticket|airline|train)\b",
        subject_folded,
    ):
        account_evidence.append("travel_booking")
        return Decision(
            "IMPORT",
            "04_TRAVEL_RESERVATIONS",
            "specific_travel_record_unlisted_sender",
            "medium",
            ["specific booking, itinerary, or ticket subject from an unlisted sender"],
            account_evidence,
        ), metadata

    if account_durable and not promotional:
        return Decision(
            "IMPORT",
            "06_ACCOUNT_SUBSCRIPTION_CHANGES",
            "durable_account_change",
            "high" if identity or finance else "medium",
            ["durable account, credential, recovery, subscription, or membership change"],
            account_evidence,
        ), metadata

    # Codes, reset links, and login alerts prove an account may exist but are poor archival
    # records. They are intentionally checklist-only.
    if transient_security:
        return Decision(
            "EXCLUDE",
            "90_TRANSIENT_SECURITY_NOTICES",
            "short_lived_security_notice",
            "high",
            ["short-lived code, reset link, login alert, or verification notice"],
            account_evidence,
        ), metadata

    # Documents with meaningful filenames get a review row rather than being auto-imported.
    if document_attachment and not bulk and not promotional:
        account_evidence.append("document_attachment_review")
        return Decision(
            "REVIEW",
            "80_MANUAL_REVIEW",
            "document_attachment_needs_review",
            "medium",
            ["non-bulk message has a record-like document attachment but no precise subject rule"],
            account_evidence,
        ), metadata

    if promotional or bulk:
        return Decision(
            "EXCLUDE",
            "92_MARKETING_NEWSLETTERS",
            "bulk_or_marketing",
            "high",
            ["bulk/list/promotional signal without a precise durable-record subject"],
            account_evidence,
        ), metadata

    return Decision(
        "EXCLUDE",
        "98_NOT_ESSENTIAL",
        "no_durable_record_signal",
        "high",
        ["no precise durable-record rule matched"],
        account_evidence,
    ), metadata


def envelope_date(date_value: datetime | None) -> str:
    if date_value is None:
        date_value = datetime.now(timezone.utc)
    return date_value.astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S %Y")


def write_raw_message(out_file: BinaryIO, raw_bytes: bytes, date_value: datetime | None) -> None:
    """Write mboxo bytes while leaving the RFC message and Date header unchanged."""
    out_file.write(f"From MAILER-DAEMON {envelope_date(date_value)}\n".encode("ascii"))
    escaped = raw_bytes.replace(b"\nFrom ", b"\n>From ")
    out_file.write(escaped)
    if not escaped.endswith(b"\n"):
        out_file.write(b"\n")
    out_file.write(b"\n")


def parse_source(value: str) -> SourceSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("source must be OLD_GMAIL=PATH_TO_MBOX")
    account, raw_path = value.split("=", 1)
    account = account.strip().lower()
    raw_path = raw_path.strip()
    if not account or "@" not in account or not raw_path:
        raise argparse.ArgumentTypeError("source must be OLD_GMAIL=PATH_TO_MBOX")
    return SourceSpec(account, Path(raw_path).expanduser())


def account_slug(account: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", account)


def account_candidate_key(root_domain: str, sender_name: str, sender_email: str) -> str:
    # Shared recruiting platforms can represent many unrelated employers.
    if root_domain in {"greenhouse.io", "lever.co", "myworkday.com", "myworkdayjobs.com", "smartrecruiters.com", "workday.com"}:
        identity = normalize(sender_name) or sender_email.split("@", 1)[0]
        identity = re.sub(r"\b(?:workday|careers?|jobs?|recruiting|no-?reply|notifications?)\b", " ", identity)
        identity = re.sub(r"[^a-z0-9]+", "-", identity).strip("-") or sender_email.split("@", 1)[0]
        return f"{root_domain}|{identity}"
    return root_domain or sender_email


def service_display_name(candidate: AccountCandidate) -> str:
    ignored = re.compile(r"^(?:no[ -]?reply|notifications?|support|team|service|account|alert|mail)$", re.I)
    for name, _count in candidate.sender_names.most_common():
        cleaned = clean_text(name)
        if cleaned and "@" not in cleaned and not ignored.match(cleaned):
            return cleaned
    label = candidate.service_domain.split(".", 1)[0] if candidate.service_domain else candidate.service_key
    return label.replace("-", " ").title()


def candidate_priority(candidate: AccountCandidate) -> tuple[str, str]:
    evidence = set(candidate.evidence)
    domain = candidate.service_domain
    critical_context = domain_in(domain, IDENTITY_DOMAINS | FINANCE_DOMAINS | SCHOOL_GOV_DOMAINS)
    explicit = bool(evidence & {"security_login_or_verification", "account_or_subscription_change"})
    if critical_context and (explicit or "financial_or_tax_account" in evidence or "school_government_or_loan_record" in evidence):
        return "P0_CRITICAL", "high"
    if explicit or evidence & {"financial_or_tax_account", "gaming_account_or_purchase"}:
        return "P1_HIGH", "high" if explicit else "medium"
    return "P2_REVIEW", "medium"


def suggested_action(candidate: AccountCandidate) -> str:
    domain = candidate.service_domain
    evidence = set(candidate.evidence)
    if domain == "google.com":
        return "Review Google Account recovery/contact settings and keep access to the old Gmail during migration."
    if domain_in(domain, IDENTITY_DOMAINS):
        return "Update sign-in, contact, and recovery email details; verify two-factor authentication before retiring Gmail."
    if domain_in(domain, FINANCE_DOMAINS):
        return "Update login/contact/e-statement email and verify recovery methods and two-factor authentication."
    if domain_in(domain, SCHOOL_GOV_DOMAINS) or domain.endswith(".gov") or domain.endswith(".edu"):
        return "Update the profile/contact email and confirm continued access to records and recovery options."
    if "job_application_profile" in evidence:
        return "Update the candidate/profile email if this portal or employer application is still active."
    if "travel_booking" in evidence:
        return "Update the travel or loyalty profile email; do not alter an active booking without checking its contact details."
    if "purchase_or_marketplace_account" in evidence:
        return "Check whether an account exists, then update its login, recovery, and receipt email if still used."
    if "gaming_account_or_purchase" in evidence:
        return "Update the account login/contact email and recovery or security settings."
    return "Sign in from a trusted device and update the login, contact, and recovery email if the account is still used."


def open_temp_mboxes(
    account_dir: Path,
    categories: Iterable[str],
) -> tuple[dict[str, BinaryIO], dict[str, Path]]:
    handles: dict[str, BinaryIO] = {}
    temp_paths: dict[str, Path] = {}
    for category in categories:
        temp_path = account_dir / f".{category}.mbox.partial"
        handles[category] = temp_path.open("wb")
        temp_paths[category] = temp_path
    return handles, temp_paths


def close_files(handles: Iterable[BinaryIO]) -> None:
    for handle in handles:
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass


def render_progress(
    account: str,
    current: int,
    total: int,
    imports: int,
    reviews: int,
    *,
    force: bool = False,
) -> None:
    """Render a compact progress bar on terminals and periodic lines when redirected."""
    if total <= 0:
        return
    if not sys.stdout.isatty():
        if force or current % 5_000 == 0:
            print(f"  {account}: {current:,}/{total:,} scanned; {imports:,} import; {reviews:,} review")
        return
    width = 24
    ratio = min(current / total, 1.0)
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    line = (
        f"\r  [{bar}] {ratio:6.1%}  {current:,}/{total:,}  "
        f"keep {imports:,}  review {reviews:,}"
    )
    print(colorize(line, "accent"), end="\n" if force else "", flush=True)


def write_decision_header(writer: csv.writer) -> None:
    writer.writerow(
        [
            "source_account",
            "action",
            "category",
            "rule_id",
            "confidence",
            "reasons",
            "account_evidence",
            "date",
            "original_date_header",
            "from_email",
            "from_name",
            "sender_domain",
            "service_domain",
            "subject",
            "gmail_labels",
            "list_id",
            "has_unsubscribe",
            "is_bulk",
            "attachment_count",
            "attachment_names",
            "message_hash",
        ]
    )


def write_decision_row(
    writer: csv.writer,
    account: str,
    decision: Decision,
    metadata: dict[str, object],
    digest: str,
) -> None:
    writer.writerow(
        [
            account,
            decision.action,
            decision.category,
            decision.rule_id,
            decision.confidence,
            " | ".join(decision.reasons),
            " | ".join(decision.account_evidence),
            metadata["date"],
            metadata["date_header"],
            metadata["from_email"],
            metadata["from_name"],
            metadata["domain"],
            metadata["root_domain"],
            metadata["subject"],
            metadata["labels"],
            metadata["list_id"],
            metadata["has_unsubscribe"],
            metadata["is_bulk"],
            metadata["attachment_count"],
            metadata["attachment_names"],
            digest,
        ]
    )


def process_source(
    source_spec: SourceSpec,
    output_root: Path,
    *,
    include_spam_trash: bool,
    include_routine_purchases: bool,
    selected_categories: set[str],
    seen_messages: set[str],
    candidates: dict[str, AccountCandidate],
    collect_candidates: bool,
) -> dict[str, object]:
    source_path = source_spec.path.resolve()
    account_dir = output_root / account_slug(source_spec.account)
    account_dir.mkdir(parents=True, exist_ok=True)

    decisions_path = account_dir / "MESSAGE_DECISIONS.csv"
    review_path = account_dir / "MANUAL_REVIEW.csv"
    summary_path = account_dir / "CATEGORY_SUMMARY.csv"
    handles, temp_paths = open_temp_mboxes(account_dir, selected_categories)
    counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    imported_bytes: Counter[str] = Counter()
    total = 0
    duplicate_count = 0
    failed_count = 0
    success = False
    source_box: mailbox.mbox | None = None
    review_file = review_path.open("w", encoding="utf-8", newline="")
    review_writer = csv.writer(review_file)
    write_decision_header(review_writer)

    try:
        source_box = mailbox.mbox(source_path, create=False)
        expected_total = len(source_box)
        with decisions_path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.writer(csv_file)
            write_decision_header(writer)

            for key in source_box.iterkeys():
                total += 1
                try:
                    raw_bytes = source_box.get_bytes(key)
                    try:
                        message = message_from_bytes(raw_bytes, policy=default)
                    except Exception:
                        message = message_from_bytes(raw_bytes)

                    decision, metadata = classify_message(
                        message,
                        include_spam_trash=include_spam_trash,
                        include_routine_purchases=include_routine_purchases,
                    )
                    if is_sent_message(message, source_spec.account):
                        decision = Decision(
                            "EXCLUDE",
                            "95_SENT_MAIL_NOT_SELECTED",
                            "sent_mail_separate_part",
                            "high",
                            ["sent mail is exported only when the Sent email data part is selected"],
                            [],
                        )
                    if decision.should_import and decision.category not in selected_categories:
                        original_category = decision.category
                        decision = Decision(
                            "EXCLUDE",
                            "94_CATEGORY_NOT_SELECTED",
                            "category_not_selected",
                            "high",
                            [f"user did not select {original_category}"],
                            decision.account_evidence,
                        )
                    digest = message_hash(message, raw_bytes, source_spec.account, total)

                    if decision.should_import and digest in seen_messages:
                        decision = Decision(
                            "EXCLUDE",
                            "98_NOT_ESSENTIAL",
                            "duplicate_message",
                            "high",
                            ["same Message-ID was already exported from this migration run"],
                            decision.account_evidence,
                        )
                        duplicate_count += 1

                    counts[decision.category] += 1
                    action_counts[decision.action] += 1
                    write_decision_row(writer, source_spec.account, decision, metadata, digest)
                    if decision.action == "REVIEW":
                        write_decision_row(review_writer, source_spec.account, decision, metadata, digest)

                    if decision.should_import:
                        write_raw_message(handles[decision.category], raw_bytes, metadata["date_value"])
                        imported_bytes[decision.category] += len(raw_bytes)
                        seen_messages.add(digest)

                    evidence_set = set(decision.account_evidence)
                    processor_record_only = domain_in(str(metadata["root_domain"]), PAYMENT_PROCESSOR_DOMAINS) and not evidence_set & {
                        "security_login_or_verification",
                        "account_or_subscription_change",
                    }
                    if (
                        collect_candidates
                        and decision.account_evidence
                        and metadata["root_domain"]
                        and decision.category != "93_SPAM_TRASH"
                        and not processor_record_only
                    ):
                        key_name = account_candidate_key(
                            str(metadata["root_domain"]),
                            str(metadata["from_name"]),
                            str(metadata["from_email"]),
                        )
                        candidate = candidates.setdefault(
                            key_name,
                            AccountCandidate(key_name, str(metadata["root_domain"])),
                        )
                        candidate.add(
                            account=source_spec.account,
                            sender_name=str(metadata["from_name"]),
                            sender_email=str(metadata["from_email"]),
                            evidence=decision.account_evidence,
                            imported_category=decision.category if decision.should_import else None,
                            date_value=metadata["date_value"] if isinstance(metadata["date_value"], datetime) else None,
                            subject=str(metadata["subject"]),
                        )
                except Exception as exc:
                    failed_count += 1
                    warning = f"Warning: message {total:,} in {source_path.name} could not be processed: {exc}"
                    print(colorize(warning, "warning"), file=sys.stderr)

                if total % 250 == 0:
                    render_progress(
                        source_spec.account,
                        total,
                        expected_total,
                        action_counts["IMPORT"],
                        action_counts["REVIEW"],
                    )

            render_progress(
                source_spec.account,
                total,
                expected_total,
                action_counts["IMPORT"],
                action_counts["REVIEW"],
                force=True,
            )

        success = True
    finally:
        close_files(handles.values())
        review_file.close()
        if source_box is not None:
            source_box.close()

    if success:
        for category, temp_path in temp_paths.items():
            final_path = account_dir / f"{category}.mbox"
            temp_path.replace(final_path)

    with summary_path.open("w", encoding="utf-8", newline="") as summary_file:
        writer = csv.writer(summary_file)
        writer.writerow(["source_account", "action", "category", "message_count", "approx_source_bytes"])
        for category in ALL_CATEGORIES:
            action = "IMPORT" if category in IMPORT_CATEGORIES else ("REVIEW" if category == "80_MANUAL_REVIEW" else "EXCLUDE")
            writer.writerow([source_spec.account, action, category, counts[category], imported_bytes[category]])

    return {
        "source_account": source_spec.account,
        "input_mbox": str(source_path),
        "messages_scanned": total,
        "messages_import_ready": action_counts["IMPORT"],
        "messages_for_manual_review": action_counts["REVIEW"],
        "messages_excluded": action_counts["EXCLUDE"],
        "duplicates_excluded": duplicate_count,
        "processing_failures": failed_count,
        "category_counts": dict(counts),
        "selected_categories": sorted(selected_categories),
        "account_output_folder": str(account_dir),
    }


def extract_sent_mail(
    sources: list[SourceSpec],
    output_root: Path,
) -> dict[str, object]:
    """Combine Gmail Sent-labeled messages from every source into one mbox."""
    final_path = output_root / f"{SENT_CATEGORY}.mbox"
    temp_path = output_root / f".{SENT_CATEGORY}.mbox.partial"
    skipped_path = output_root / "SENT_MAIL_SKIPPED.csv"
    summary_path = output_root / "SENT_MAIL_SUMMARY.json"
    seen_messages: set[str] = set()
    total_scanned = 0
    sent_matches = 0
    imported = 0
    duplicate_count = 0
    oversized_count = 0
    failed_count = 0
    imported_bytes = 0
    source_stats: list[dict[str, object]] = []
    success = False

    with temp_path.open("wb") as sent_file, skipped_path.open("w", encoding="utf-8", newline="") as skipped_file:
        skipped_writer = csv.writer(skipped_file)
        skipped_writer.writerow(
            ["source_account", "reason", "message_index", "date", "from", "to", "subject", "bytes", "message_hash"]
        )
        try:
            for source_spec in sources:
                source_scanned = 0
                source_matches = 0
                source_imported = 0
                source_failures = 0
                source_box = mailbox.mbox(source_spec.path.resolve(), create=False)
                try:
                    expected_total = len(source_box)
                    for key in source_box.iterkeys():
                        source_scanned += 1
                        total_scanned += 1
                        try:
                            raw_bytes = source_box.get_bytes(key)
                            try:
                                message = message_from_bytes(raw_bytes, policy=default)
                            except Exception:
                                message = message_from_bytes(raw_bytes)
                            if not is_sent_message(message, source_spec.account):
                                continue

                            source_matches += 1
                            sent_matches += 1
                            digest = sent_message_hash(message, raw_bytes)
                            date_header = clean_text(message.get("Date", ""))
                            common_row = [
                                source_spec.account,
                                "",
                                source_scanned,
                                date_header,
                                clean_text(message.get("From", "")),
                                clean_text(message.get("To", "")),
                                clean_text(message.get("Subject", "")),
                                len(raw_bytes),
                                digest,
                            ]
                            if digest in seen_messages:
                                duplicate_count += 1
                                common_row[1] = "duplicate_message"
                                skipped_writer.writerow(common_row)
                                continue
                            if len(raw_bytes) > MAX_ICLOUD_MESSAGE_BYTES:
                                oversized_count += 1
                                common_row[1] = "over_20mb_icloud_limit"
                                skipped_writer.writerow(common_row)
                                continue

                            write_raw_message(sent_file, raw_bytes, parse_message_date(date_header))
                            seen_messages.add(digest)
                            source_imported += 1
                            imported += 1
                            imported_bytes += len(raw_bytes)
                        except Exception as exc:
                            failed_count += 1
                            source_failures += 1
                            warning = (
                                f"Warning: sent message {source_scanned:,} in {source_spec.path.name} "
                                f"could not be processed: {exc}"
                            )
                            print(colorize(warning, "warning"), file=sys.stderr)

                        if source_scanned % 250 == 0:
                            render_progress(
                                source_spec.account,
                                source_scanned,
                                expected_total,
                                source_imported,
                                0,
                            )

                    render_progress(
                        source_spec.account,
                        source_scanned,
                        expected_total,
                        source_imported,
                        0,
                        force=True,
                    )
                finally:
                    source_box.close()

                source_stats.append(
                    {
                        "source_account": source_spec.account,
                        "input_mbox": str(source_spec.path.resolve()),
                        "messages_scanned": source_scanned,
                        "sent_messages_matched": source_matches,
                        "sent_messages_imported": source_imported,
                        "processing_failures": source_failures,
                    }
                )
            success = True
        finally:
            sent_file.flush()

    if success:
        temp_path.replace(final_path)

    result: dict[str, object] = {
        "source_account": "Combined sent mail",
        "input_mbox": [str(item.path.resolve()) for item in sources],
        "messages_scanned": total_scanned,
        "messages_import_ready": imported,
        "messages_for_manual_review": 0,
        "messages_excluded": total_scanned - imported - failed_count,
        "duplicates_excluded": duplicate_count,
        "oversized_messages_skipped": oversized_count,
        "processing_failures": failed_count,
        "sent_messages_matched": sent_matches,
        "category_counts": {SENT_CATEGORY: imported},
        "selected_categories": [SENT_CATEGORY],
        "account_output_folder": str(output_root),
        "sent_mailbox": str(final_path),
        "sent_skipped_report": str(skipped_path),
        "approx_source_bytes": imported_bytes,
        "sources": source_stats,
    }
    summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def sorted_candidate_rows(
    candidates: dict[str, AccountCandidate],
) -> list[tuple[str, datetime | None, AccountCandidate, str]]:
    rows = []
    for candidate in candidates.values():
        priority, confidence = candidate_priority(candidate)
        rows.append((priority, candidate.last_seen, candidate, confidence))
    priority_order = {"P0_CRITICAL": 0, "P1_HIGH": 1, "P2_REVIEW": 2}
    rows.sort(
        key=lambda item: (
            priority_order[item[0]],
            -(item[1].timestamp() if item[1] else 0),
            item[2].service_domain,
        )
    )
    return rows


def service_account_url(candidate: AccountCandidate) -> str:
    if candidate.service_domain in KNOWN_ACCOUNT_URLS:
        return KNOWN_ACCOUNT_URLS[candidate.service_domain]
    if re.fullmatch(r"[a-z0-9.-]+", candidate.service_domain):
        return f"https://{candidate.service_domain}/"
    return ""


def write_account_checklist(
    output_root: Path,
    candidates: dict[str, AccountCandidate],
    new_email: str,
) -> Path:
    path = output_root / "ACCOUNT_CHANGE_CHECKLIST.csv"
    rows = sorted_candidate_rows(candidates)

    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "done",
                "priority",
                "confidence",
                "service",
                "service_domain",
                "official_site_or_account_page",
                "old_gmail_accounts",
                "new_email",
                "suggested_action",
                "evidence_types",
                "relevant_message_count",
                "imported_record_count",
                "first_seen",
                "last_seen",
                "sender_emails",
                "recent_example_subjects",
            ]
        )
        for priority, _last_seen, candidate, confidence in rows:
            writer.writerow(
                [
                    "",
                    priority,
                    confidence,
                    service_display_name(candidate),
                    candidate.service_domain,
                    service_account_url(candidate),
                    " | ".join(sorted(candidate.old_accounts)),
                    new_email,
                    suggested_action(candidate),
                    " | ".join(f"{name}:{count}" for name, count in candidate.evidence.most_common()),
                    candidate.total_relevant_messages,
                    sum(candidate.imported_categories.values()),
                    iso_date(candidate.first_seen),
                    iso_date(candidate.last_seen),
                    " | ".join(name for name, _count in candidate.sender_emails.most_common(8)),
                    " | ".join(subject for _date, subject in candidate.samples),
                ]
            )
    return path


def markdown_escape(value: str) -> str:
    return clean_text(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def write_account_note(
    output_root: Path,
    candidates: dict[str, AccountCandidate],
    new_email: str,
) -> Path:
    path = output_root / "ACCOUNT_CHANGE_CHECKLIST.md"
    rows = sorted_candidate_rows(candidates)
    old_accounts = sorted({account for candidate in candidates.values() for account in candidate.old_accounts})
    lines = [
        "# Account email change checklist",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Old email account(s): {', '.join(old_accounts) if old_accounts else 'Not provided'}",
        f"New email: {new_email or 'Fill in before starting'}",
        "",
        "> Safety: domains are inferred from email evidence. Verify the domain independently, use a trusted app or password manager when possible, and never enter a password on a site you do not recognize.",
        "",
        "For each service, update sign-in, contact, recovery, billing, and notification addresses as applicable. Verify the new address and two-factor recovery before checking the item off.",
        "",
    ]
    labels = {
        "P0_CRITICAL": "P0 — Critical identity, financial, education, or government accounts",
        "P1_HIGH": "P1 — Strong account evidence",
        "P2_REVIEW": "P2 — Possible account; verify before spending time on it",
    }
    for priority in ("P0_CRITICAL", "P1_HIGH", "P2_REVIEW"):
        group = [row for row in rows if row[0] == priority]
        if not group:
            continue
        lines.extend([f"## {labels[priority]}", ""])
        for _priority, _last_seen, candidate, confidence in group:
            name = markdown_escape(service_display_name(candidate))
            url = service_account_url(candidate)
            linked_name = f"[{name}]({url})" if url else name
            old = ", ".join(sorted(candidate.old_accounts))
            last_seen = iso_date(candidate.last_seen)[:10] or "unknown"
            lines.append(
                f"- [ ] {linked_name} — `{candidate.service_domain}` — old: `{old}` — "
                f"last evidence: {last_seen} — confidence: {confidence}"
            )
            lines.append(f"  - {markdown_escape(suggested_action(candidate))}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_import_manifest(output_root: Path, source_results: list[dict[str, object]]) -> Path:
    path = output_root / "IMPORT_THESE_FILES.txt"
    lines = [
        "Gmail to iCloud import manifest",
        "",
        "Import each NON-EMPTY mbox listed below exactly once.",
        "The same message is never intentionally placed in more than one listed file.",
        "Do not import older outputs from previous versions of the script.",
        "",
    ]
    for result in source_results:
        account_dir = Path(str(result["account_output_folder"]))
        counts = result.get("category_counts", {})
        lines.append(f"Source account: {result['source_account']}")
        for category in result.get("selected_categories", IMPORT_CATEGORIES):
            if int(counts.get(category, 0)) > 0:
                lines.append(str((account_dir / f"{category}.mbox").resolve()))
        if int(result.get("messages_for_manual_review", 0)) > 0:
            lines.append(f"Manual-review CSV (do not import directly): {(account_dir / 'MANUAL_REVIEW.csv').resolve()}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def validate_output_mboxes(output_root: Path, source_results: list[dict[str, object]]) -> tuple[Path, dict[str, object]]:
    """Re-open every produced mbox and verify counts, dates, and cross-file Message-IDs."""
    files: list[dict[str, object]] = []
    seen_message_ids: set[str] = set()
    duplicate_message_ids = 0
    count_mismatches = 0
    total_messages = 0
    missing_date_headers = 0
    unparseable_date_headers = 0
    messages_over_20mb = 0
    largest_message_bytes = 0

    for result in source_results:
        account_dir = Path(str(result["account_output_folder"]))
        counts = result.get("category_counts", {})
        for category in result.get("selected_categories", IMPORT_CATEGORIES):
            path = account_dir / f"{category}.mbox"
            expected = int(counts.get(category, 0))
            box = mailbox.mbox(path, create=False)
            actual = 0
            missing_dates = 0
            unparseable_dates = 0
            over_20mb = 0
            file_largest_message_bytes = 0
            try:
                for key in box.iterkeys():
                    raw_bytes = box.get_bytes(key)
                    try:
                        message = message_from_bytes(raw_bytes, policy=default)
                    except Exception:
                        message = message_from_bytes(raw_bytes)
                    actual += 1
                    message_size = len(raw_bytes)
                    file_largest_message_bytes = max(file_largest_message_bytes, message_size)
                    if message_size > MAX_ICLOUD_MESSAGE_BYTES:
                        over_20mb += 1
                    date_header = clean_text(message.get("Date", ""))
                    if not date_header:
                        missing_dates += 1
                    elif parse_message_date(date_header) is None:
                        unparseable_dates += 1
                    message_id = clean_text(message.get("Message-ID", "")).casefold()
                    if message_id:
                        if message_id in seen_message_ids:
                            duplicate_message_ids += 1
                        seen_message_ids.add(message_id)
            finally:
                box.close()

            if actual != expected:
                count_mismatches += 1
            total_messages += actual
            missing_date_headers += missing_dates
            unparseable_date_headers += unparseable_dates
            messages_over_20mb += over_20mb
            largest_message_bytes = max(largest_message_bytes, file_largest_message_bytes)
            files.append(
                {
                    "source_account": result["source_account"],
                    "category": category,
                    "path": str(path),
                    "expected_messages": expected,
                    "actual_messages": actual,
                    "count_matches": actual == expected,
                    "missing_date_headers": missing_dates,
                    "unparseable_date_headers": unparseable_dates,
                    "messages_over_20mb": over_20mb,
                    "largest_message_bytes": file_largest_message_bytes,
                    "size_bytes": path.stat().st_size,
                }
            )

    status = "PASS" if count_mismatches == 0 and duplicate_message_ids == 0 and messages_over_20mb == 0 else "FAIL"
    report: dict[str, object] = {
        "status": status,
        "mbox_files_checked": len(files),
        "messages_checked": total_messages,
        "count_mismatches": count_mismatches,
        "duplicate_message_ids_across_output_files": duplicate_message_ids,
        "messages_missing_original_date_header": missing_date_headers,
        "messages_with_unparseable_date_header": unparseable_date_headers,
        "messages_over_20mb_icloud_limit": messages_over_20mb,
        "largest_message_bytes": largest_message_bytes,
        "files": files,
    }
    path = output_root / "MBOX_VALIDATION.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path, report


def validate_sources(sources: list[SourceSpec]) -> None:
    if not 1 <= len(sources) <= 2:
        raise ValueError("provide one or two Gmail source accounts")
    seen_accounts: set[str] = set()
    for item in sources:
        if "@" not in item.account:
            raise ValueError(f"source account is not a complete email address: {item.account}")
        if item.account in seen_accounts:
            raise ValueError(f"duplicate source account: {item.account}")
        seen_accounts.add(item.account)
        if not item.path.exists() or not item.path.is_file():
            raise ValueError(f"mbox file not found: {item.path}")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def configure_terminal_input() -> None:
    """Enable in-session history and arrow-key editing when readline is available."""
    if _readline is None or not sys.stdin.isatty():
        return
    try:
        binding = "bind -e" if "libedit" in (_readline.__doc__ or "") else "set editing-mode emacs"
        _readline.parse_and_bind(binding)
        _readline.set_history_length(100)
    except (AttributeError, RuntimeError):
        # Basic input still works on Python builds with a limited readline backend.
        pass


def prompt_text(question: str, default_value: str = "") -> str:
    suffix = f" [{default_value}]" if default_value else ""
    try:
        response = input(f"{question}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError("interactive setup canceled") from exc
    return response or default_value


def normalize_interactive_path(value: str) -> Path:
    """Accept plain, quoted, shell-escaped, or file-URL paths pasted at a prompt."""
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
        candidate = candidate[1:-1].strip()

    if candidate.casefold().startswith("file://"):
        parsed = urlparse(candidate)
        if parsed.scheme.casefold() == "file":
            candidate = unquote(parsed.path)

    # Terminal drag-and-drop and shell copying commonly produce paths such as
    # /Users/name/All\ mail.mbox. Preserve already-valid paths containing spaces,
    # but decode shell escapes when the value represents one shell token.
    try:
        shell_parts = shlex.split(candidate, posix=True)
    except ValueError:
        shell_parts = []
    if len(shell_parts) == 1:
        candidate = shell_parts[0]

    return Path(candidate).expanduser().resolve()


def prompt_yes_no(question: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        answer = prompt_text(f"{question} ({marker})").casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print(colorize("Please answer yes or no.", "warning"))


def discover_mbox_files(root: Path) -> list[Path]:
    candidates = []
    for path in root.rglob("*.mbox"):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative_parts = path.relative_to(root).parts
        if any(part.startswith(("gmail_icloud_migration", "gmail_import_filtered_output")) for part in relative_parts):
            continue
        if path.stem in IMPORT_CATEGORIES or path.stem == SENT_CATEGORY or path.name.endswith(".partial"):
            continue
        candidates.append(path.resolve())
    return sorted(candidates, key=lambda path: path.stat().st_size, reverse=True)


def choose_mbox_path(discovered: list[Path], used: set[Path]) -> Path:
    available = [path for path in discovered if path not in used]
    if available:
        print(colorize("\nDetected mbox files (the largest files are usually Google Takeout sources):", "heading"))
        for index, path in enumerate(available[:12], start=1):
            print(f"  {index}. {path} ({human_size(path.stat().st_size)})")
        value = prompt_text("Choose a number or enter the full mbox path", "1")
        if value.isdigit() and 1 <= int(value) <= min(len(available), 12):
            return available[int(value) - 1]
    else:
        value = prompt_text("Full path to the Google Takeout .mbox file")
    return normalize_interactive_path(value)


def preview_sources(
    sources: list[SourceSpec],
    *,
    limit: int,
    include_spam_trash: bool,
    include_routine_purchases: bool,
) -> Counter[str]:
    combined: Counter[str] = Counter()
    preview = f"\nPreviewing up to {limit:,} messages per source. No output mailboxes are written yet."
    print(colorize(preview, "heading"))
    for source_spec in sources:
        box = mailbox.mbox(source_spec.path, create=False)
        scanned = 0
        failures = 0
        try:
            preview_total = min(len(box), limit)
            for key in box.iterkeys():
                if scanned >= limit:
                    break
                scanned += 1
                try:
                    message = box.get_message(key)
                    decision, _metadata = classify_message(
                        message,
                        include_spam_trash=include_spam_trash,
                        include_routine_purchases=include_routine_purchases,
                    )
                    combined[decision.category] += 1
                except Exception:
                    failures += 1
                if scanned % 250 == 0:
                    render_progress(source_spec.account, scanned, preview_total, 0, 0)
            render_progress(source_spec.account, scanned, preview_total, 0, 0, force=True)
        finally:
            box.close()
        if failures:
            print(colorize(f"  Preview warnings for {source_spec.account}: {failures:,}", "warning"))
    return combined


CATEGORY_ALIASES = {
    "finance": "01_FINANCE_TAX_RECORDS",
    "jobs": "02_JOB_APPLICATION_RECORDS",
    "school": "03_SCHOOL_GOVERNMENT_LEGAL",
    "government": "03_SCHOOL_GOVERNMENT_LEGAL",
    "travel": "04_TRAVEL_RESERVATIONS",
    "purchases": "05_PURCHASES_ORDER_HISTORY",
    "orders": "05_PURCHASES_ORDER_HISTORY",
    "accounts": "06_ACCOUNT_SUBSCRIPTION_CHANGES",
    "subscriptions": "06_ACCOUNT_SUBSCRIPTION_CHANGES",
    "gaming": "07_GAMING_ACCOUNT_PURCHASES",
}

DATA_PART_ALIASES = {
    "records": "essentials",
    "essential": "essentials",
    "essentials": "essentials",
    "sent": "sent",
    "sent-mail": "sent",
    "accounts": "accounts",
    "checklist": "accounts",
}


def parse_data_parts(value: str | None) -> set[str]:
    if not value or value.strip().casefold() in {"recommended", "default"}:
        return {"essentials", "accounts"}
    if value.strip().casefold() == "all":
        return set(DATA_PART_ORDER)
    selected: set[str] = set()
    for token in re.split(r"[,\s]+", value.strip()):
        folded = token.casefold()
        if not folded:
            continue
        if folded.isdigit() and 1 <= int(folded) <= len(DATA_PART_ORDER):
            selected.add(DATA_PART_ORDER[int(folded) - 1])
        elif folded in DATA_PART_ALIASES:
            selected.add(DATA_PART_ALIASES[folded])
        else:
            raise ValueError(f"unknown data part: {token}")
    if not selected:
        raise ValueError("select at least one data part")
    return selected


def prompt_data_parts() -> set[str]:
    print(colorize("\nChoose which parts of your Gmail data to extract:", "heading"))
    for index, part in enumerate(DATA_PART_ORDER, start=1):
        print(f"  {index}. {DATA_PART_DESCRIPTIONS[part]}")
    while True:
        value = prompt_text("Data parts: comma-separated numbers, or all", "1,3")
        try:
            return parse_data_parts(value)
        except ValueError as exc:
            print(colorize(f"Please try again: {exc}", "warning"))


def parse_categories(value: str | None) -> set[str]:
    if not value or value.strip().casefold() in {"all", "recommended"}:
        return set(IMPORT_CATEGORIES)
    selected: set[str] = set()
    for token in re.split(r"[,\s]+", value.strip()):
        folded = token.casefold()
        if not folded:
            continue
        if folded.isdigit() and 1 <= int(folded) <= len(IMPORT_CATEGORIES):
            selected.add(IMPORT_CATEGORIES[int(folded) - 1])
        elif folded in CATEGORY_ALIASES:
            selected.add(CATEGORY_ALIASES[folded])
        else:
            exact = next((category for category in IMPORT_CATEGORIES if category.casefold() == folded), None)
            if exact:
                selected.add(exact)
            else:
                raise ValueError(f"unknown category: {token}")
    if not selected:
        raise ValueError("select at least one import category")
    return selected


def prompt_categories(preview_counts: Counter[str]) -> set[str]:
    print(colorize("\nChoose which durable-record categories should be exported:", "heading"))
    for index, category in enumerate(IMPORT_CATEGORIES, start=1):
        observed = preview_counts[category]
        print(f"  {index}. {CATEGORY_DESCRIPTIONS[category]} (preview matched {observed:,})")
    while True:
        value = prompt_text("Categories: all, or comma-separated numbers", "all")
        try:
            return parse_categories(value)
        except ValueError as exc:
            print(colorize(f"Please try again: {exc}", "warning"))


def interactive_configuration(
    args: argparse.Namespace,
    initial_sources: list[SourceSpec],
) -> tuple[list[SourceSpec], str, Path, bool, bool, set[str], set[str], bool]:
    configure_terminal_input()
    print(colorize("\nGmail Emigration — guided setup", "title"))
    print(colorize("=" * 34, "accent"))
    print("This tool works locally with Google Takeout .mbox exports.")
    print("If needed, request Gmail data at https://takeout.google.com and extract the download first.")
    print(colorize("Your email content is never uploaded by this program.\n", "muted"))

    selected_parts = parse_data_parts(args.parts) if args.parts else prompt_data_parts()

    sources = list(initial_sources)
    if not sources:
        while True:
            count_value = prompt_text("How many Gmail accounts are you migrating? (1 or 2)", "1")
            if count_value in {"1", "2"}:
                source_count = int(count_value)
                break
            print(colorize("Please enter 1 or 2.", "warning"))
        discovered = discover_mbox_files(Path.cwd())
        used: set[Path] = set()
        if not discovered:
            print("Tip: paste or drag the .mbox file here; quoted and shell-escaped paths are accepted.\n")
        for index in range(source_count):
            while True:
                account = prompt_text(f"Old Gmail address #{index + 1}").lower()
                if "@" in account:
                    break
                print(colorize("Enter a complete email address.", "warning"))
            while True:
                path = choose_mbox_path(discovered, used)
                if path.is_file():
                    used.add(path)
                    sources.append(SourceSpec(account, path))
                    break
                print(colorize(f"File not found: {path}", "warning"))
                retry_tip = "Try dragging the .mbox file into this window, or paste its path without editing it."
                print(colorize(retry_tip, "muted"))
    else:
        print("Using source files supplied by flags:")
        for item in sources:
            print(f"  - {item.account}: {item.path}")

    new_email = args.new_email.strip()
    if "accounts" in selected_parts:
        while "@" not in new_email:
            new_email = prompt_text("New iCloud email address").lower()
            if "@" not in new_email:
                print(colorize("Enter a complete email address.", "warning"))

    classify_non_sent = bool(selected_parts & {"essentials", "accounts"})
    include_spam_trash = args.include_spam_trash
    if classify_non_sent and not include_spam_trash:
        include_spam_trash = prompt_yes_no(
            "Consider precisely matched messages in Gmail Spam/Trash?",
            False,
        )

    include_routine_purchases = args.include_routine_purchases
    selected_categories: set[str] = set()
    if "essentials" in selected_parts:
        if not include_routine_purchases:
            include_routine_purchases = prompt_yes_no(
                "Import routine food-delivery purchase receipts?",
                False,
            )
        preview_counts = preview_sources(
            sources,
            limit=args.preview_limit,
            include_spam_trash=include_spam_trash,
            include_routine_purchases=include_routine_purchases,
        )
        selected_categories = (
            parse_categories(args.categories) if args.categories else prompt_categories(preview_counts)
        )

    default_out = args.out
    if selected_parts == {"sent"} and default_out == "gmail_icloud_migration":
        default_out = "gmail_icloud_sent_mail"
    default_path = Path(default_out).expanduser()
    if default_path.is_dir() and any(default_path.iterdir()):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_out = f"{default_out}_{stamp}"
        print(f"Existing output detected; the new default is {default_out}")
    output_root = Path(prompt_text("Output folder", default_out)).expanduser().resolve()
    if output_root.exists() and not output_root.is_dir():
        raise RuntimeError(f"output path is not a folder: {output_root}")
    if output_root.is_dir() and any(output_root.iterdir()):
        if not prompt_yes_no("That output folder contains files. Replace generated files in it?", False):
            raise RuntimeError("choose a new or empty output folder")

    print(colorize("\nReady to run:", "heading"))
    for item in sources:
        print(f"  - {item.account}: {item.path}")
    print(f"  - Data parts: {', '.join(part for part in DATA_PART_ORDER if part in selected_parts)}")
    if "accounts" in selected_parts:
        print(f"  - New email: {new_email or '(not provided)'}")
    if "essentials" in selected_parts:
        print(f"  - Categories: {len(selected_categories)} selected")
    if "sent" in selected_parts:
        print(f"  - Sent output: {SENT_CATEGORY}.mbox (combined across sources)")
    print(f"  - Output: {output_root}")
    if not prompt_yes_no("Start the full migration scan?", True):
        raise RuntimeError("migration canceled before writing output")
    return (
        sources,
        new_email,
        output_root,
        include_spam_trash,
        include_routine_purchases,
        selected_categories,
        selected_parts,
        "accounts" in selected_parts,
    )


def open_local_file(path: Path) -> bool:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a high-precision, non-duplicated Gmail-to-iCloud mbox migration set."
    )
    parser.add_argument("mbox_path", nargs="?", help="Single-source compatibility mode: path to one mbox")
    parser.add_argument("--account", help="Single-source compatibility mode: old Gmail address")
    parser.add_argument("--new-email", default="", help="New iCloud address to show in the account-change note")
    parser.add_argument(
        "--source",
        action="append",
        type=parse_source,
        default=[],
        metavar="OLD_GMAIL=PATH",
        help="Input source; repeat for a combined two-account extraction",
    )
    parser.add_argument("--out", default="gmail_icloud_migration", help="Output folder (default: gmail_icloud_migration)")
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Allow a flag-based run to replace generated files in a non-empty output folder",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="Comma-separated category numbers/names, or 'all' (default: all)",
    )
    parser.add_argument(
        "--parts",
        default=None,
        help="Data parts: essentials, sent, accounts, or comma-separated values (default: essentials,accounts)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run guided setup even when source flags are supplied",
    )
    parser.add_argument(
        "--preview-limit",
        type=int,
        default=2_500,
        help="Messages sampled per source during interactive preview (default: 2500)",
    )
    parser.add_argument(
        "--include-spam-trash",
        action="store_true",
        help="Allow precise rules to consider Gmail Spam/Trash messages (off by default)",
    )
    parser.add_argument(
        "--include-routine-purchases",
        action="store_true",
        help="Also import food-delivery and similar routine purchase receipts (off by default)",
    )
    parser.add_argument(
        "--theme",
        choices=("auto", "dark", "light", "none"),
        default="auto",
        help="Terminal color theme (default: auto; also honors NO_COLOR)",
    )
    report_group = parser.add_mutually_exclusive_group()
    report_group.add_argument(
        "--open-report",
        action="store_true",
        help="Open the ranked Markdown account-change note when finished",
    )
    report_group.add_argument(
        "--no-open-report",
        action="store_true",
        help="Do not offer to open the account-change note",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_terminal_theme(args.theme)
    if args.preview_limit < 1:
        parser.error("--preview-limit must be at least 1")

    sources: list[SourceSpec] = list(args.source)
    if args.mbox_path or args.account:
        if sources:
            parser.error("use either positional mbox + --account, or repeated --source—not both")
        if not args.mbox_path or not args.account:
            parser.error("single-source mode requires both mbox_path and --account")
        sources = [SourceSpec(args.account.strip().lower(), Path(args.mbox_path).expanduser())]

    interactive_session = args.interactive or not sources
    if interactive_session:
        try:
            (
                sources,
                new_email,
                output_root,
                include_spam_trash,
                include_routine_purchases,
                selected_categories,
                selected_parts,
                prompt_to_open,
            ) = interactive_configuration(args, sources)
        except (RuntimeError, ValueError) as exc:
            print(colorize(f"\n{exc}", "warning"), file=sys.stderr)
            return 130
    else:
        new_email = args.new_email.strip()
        output_root = Path(args.out).expanduser().resolve()
        include_spam_trash = args.include_spam_trash
        include_routine_purchases = args.include_routine_purchases
        prompt_to_open = False
        try:
            selected_parts = parse_data_parts(args.parts)
            selected_categories = parse_categories(args.categories) if "essentials" in selected_parts else set()
        except ValueError as exc:
            parser.error(str(exc))

    try:
        validate_sources(sources)
    except ValueError as exc:
        parser.error(str(exc))

    if output_root.exists() and not output_root.is_dir():
        parser.error(f"output path is not a folder: {output_root}")

    if (
        not interactive_session
        and output_root.is_dir()
        and any(output_root.iterdir())
        and not args.overwrite_output
    ):
        parser.error("output folder is not empty; choose another --out path or pass --overwrite-output")
    output_root.mkdir(parents=True, exist_ok=True)

    print(colorize(f"Gmail Emigration v{VERSION}", "title"))
    print(f"Output: {colorize(output_root, 'accent')}")
    selected_part_names = ", ".join(part for part in DATA_PART_ORDER if part in selected_parts)
    policy = f"Selected data parts: {selected_part_names}."
    print(colorize(policy, "muted"))
    if selected_parts & {"essentials", "accounts"} and not include_spam_trash:
        print("Spam and Trash are excluded.")
    if "essentials" in selected_parts:
        print(f"Selected durable-record categories: {len(selected_categories)}/{len(IMPORT_CATEGORIES)}")

    seen_messages: set[str] = set()
    candidates: dict[str, AccountCandidate] = {}
    results: list[dict[str, object]] = []

    if selected_parts & {"essentials", "accounts"}:
        for item in sources:
            print(colorize(f"\nScanning non-sent mail for {item.account}: {item.path}", "heading"))
            result = process_source(
                item,
                output_root,
                include_spam_trash=include_spam_trash,
                include_routine_purchases=include_routine_purchases,
                selected_categories=selected_categories,
                seen_messages=seen_messages,
                candidates=candidates,
                collect_candidates="accounts" in selected_parts,
            )
            results.append(result)
            done = (
                f"  Done: {result['messages_scanned']:,} scanned; "
                f"{result['messages_import_ready']:,} import; "
                f"{result['messages_for_manual_review']:,} review; "
                f"{result['messages_excluded']:,} excluded"
            )
            print(colorize(done, "success"))

    sent_result: dict[str, object] | None = None
    if "sent" in selected_parts:
        print(colorize("\nExtracting one combined Sent mailbox:", "heading"))
        sent_result = extract_sent_mail(sources, output_root)
        results.append(sent_result)
        sent_done = (
            f"  Done: {sent_result['sent_messages_matched']:,} sent matched; "
            f"{sent_result['messages_import_ready']:,} imported; "
            f"{sent_result['duplicates_excluded']:,} duplicates; "
            f"{sent_result['oversized_messages_skipped']:,} over 20 MB"
        )
        print(colorize(sent_done, "success"))

    checklist_path: Path | None = None
    account_note_path: Path | None = None
    if "accounts" in selected_parts:
        checklist_path = write_account_checklist(output_root, candidates, new_email)
        account_note_path = write_account_note(output_root, candidates, new_email)
    manifest_path = write_import_manifest(output_root, results)
    validation_path, validation = validate_output_mboxes(output_root, results)
    summary_path = output_root / "MIGRATION_SUMMARY.json"
    summary = {
        "filter_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "include_spam_trash": include_spam_trash,
            "include_routine_purchases": include_routine_purchases,
            "selected_data_parts": sorted(selected_parts),
            "selected_categories": sorted(selected_categories),
            "manual_review_messages_are_imported": False,
            "transient_security_messages_are_imported": False,
            "raw_rfc_message_bytes_preserved": True,
            "original_date_header_preserved": True,
        },
        "sources": results,
        "account_change_candidates": len(candidates),
        "account_change_checklist": str(checklist_path) if checklist_path else None,
        "account_change_note": str(account_note_path) if account_note_path else None,
        "new_email": new_email,
        "import_manifest": str(manifest_path),
        "mbox_validation": str(validation_path),
        "mbox_validation_status": validation["status"],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    total_scanned = sum(int(item["messages_scanned"]) for item in results)
    total_import = sum(int(item["messages_import_ready"]) for item in results)
    total_review = sum(int(item["messages_for_manual_review"]) for item in results)
    total_failed = sum(int(item["processing_failures"]) for item in results)
    print(colorize("\nMigration set complete.", "success"))
    print(f"Scanned: {total_scanned:,}; import-ready: {total_import:,}; manual review: {total_review:,}")
    if "accounts" in selected_parts:
        print(f"Account/service candidates: {len(candidates):,}")
        print(f"Account checklist: {checklist_path}")
        print(f"Readable account note: {account_note_path}")
    if sent_result:
        print(f"Combined sent mailbox: {sent_result['sent_mailbox']}")
        if int(sent_result["oversized_messages_skipped"]) or int(sent_result["duplicates_excluded"]):
            print(f"Sent messages not copied: {sent_result['sent_skipped_report']}")
    print(f"Processing failures: {total_failed:,}")
    print(f"Exact import list: {manifest_path}")
    validation_role = "success" if validation["status"] == "PASS" else "warning"
    print(colorize(f"Mbox validation: {validation_path} ({validation['status']})", validation_role))
    print(f"Run summary: {summary_path}")
    exit_code = 1 if total_failed or validation["status"] != "PASS" else 0
    should_open = bool(args.open_report and account_note_path)
    if prompt_to_open and account_note_path and not args.no_open_report:
        should_open = prompt_yes_no("Open the account-change note now?", True)
    if should_open and account_note_path:
        if open_local_file(account_note_path):
            print("Opened the account-change note.")
        else:
            print(f"Could not open the note automatically. Open it manually: {account_note_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
