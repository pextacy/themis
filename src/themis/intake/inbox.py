"""A private disclosure inbox: a report in a file instead of on a public issue.

Mail headers go no further than this module, apart from the subject. Addresses left in the
text are redacted, so no email reaches Otari or the store. What remains is untrusted
submission text like any other, and HTML stays raw so the Shield can see what it hides.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from pathlib import Path
from typing import cast

from themis.action import PROJECT_BUDGET_KEY
from themis.config import Settings
from themis.intake.github import Target
from themis.models import Submission, SubmissionKind
from themis.pipeline import Outcome, run_url
from themis.store import redact

MAX_REPORT_BYTES = 1024 * 1024
MAX_TITLE = 200
REPO = re.compile(r"[\w.-]+/[\w.-]+")
TEXT_SUFFIXES = (".md", ".txt", ".markdown")
TEXT_TYPES = ("text/plain", "text/html")


@dataclass(frozen=True)
class InboxItem:
    submission: Submission
    attachments_ignored: int = 0


def _part_text(part: Message) -> str:
    """Unknown charsets decode with replacement rather than failing the whole report."""
    try:
        return str(cast(EmailMessage, part).get_content())
    except (LookupError, ValueError, KeyError):
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


def read_mail(raw: bytes) -> tuple[str, str, int]:
    """(subject, text parts, attachments skipped). Every text part is kept for the Shield."""
    message = message_from_bytes(raw, policy=policy.default)
    texts: list[str] = []
    ignored = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() in TEXT_TYPES and part.get_content_disposition() != "attachment":
            texts.append(_part_text(part))
        else:
            ignored += 1
    return str(message.get("subject") or ""), "\n\n".join(texts), ignored


def _title_of(text: str) -> str:
    first = next((line for line in text.splitlines() if line.strip()), "Report")
    return first.lstrip("# ").strip()[:MAX_TITLE]


def read_file(path: Path) -> tuple[str, str, int]:
    raw = path.read_bytes()
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report is larger than 1 MiB")
    suffix = path.suffix.lower()
    if suffix == ".eml":
        return read_mail(raw)
    if suffix in TEXT_SUFFIXES:
        text = raw.decode("utf-8", errors="replace")
        return _title_of(text), text, 0
    raise ValueError(f"unsupported report type {suffix or 'with no extension'}: use .md, .txt or .eml")


def build(path: Path, repo: str, kind: SubmissionKind = SubmissionKind.SECURITY_REPORT) -> InboxItem:
    """The same report always gets the same id, so a resend updates one record."""
    if not REPO.fullmatch(repo):
        raise ValueError(f"not an owner/name repository: {repo!r}")
    title, body, ignored = read_file(path)
    clean_title, clean_body = str(redact(title))[:MAX_TITLE], str(redact(body))
    digest = hashlib.sha256(f"{repo}\0{clean_title}\0{clean_body}".encode()).hexdigest()[:16]
    item = Submission(
        id=f"inbox-{digest}", repo=repo, number=0, kind=kind, title=clean_title or "Report", body=clean_body,
        contributor_key=PROJECT_BUDGET_KEY,
    )  # fmt: skip
    return InboxItem(item, ignored)


async def triage(settings: Settings, item: InboxItem) -> Outcome:
    """Runs the whole pipeline and stores the receipt. A private report is never posted."""
    owner, name = item.submission.repo.split("/", 1)
    return await run_url(settings, Target(owner, name, 0), post=False, submission=item.submission)
