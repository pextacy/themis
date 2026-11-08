"""`/themis` comment commands. The comment is untrusted; only the parsed fields are used."""

from __future__ import annotations

import re
from dataclasses import dataclass

from themis.config import RepoPolicy
from themis.models import VerdictValue

MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
COMMAND = re.compile(r"^/themis[ \t]+(recheck|override|explain)\b[ \t]*(.*)$", re.MULTILINE)
MAX_REASON = 300


class CommandRefused(Exception):
    """A well-formed command the author may not run, or one with bad arguments."""


@dataclass(frozen=True)
class Command:
    name: str
    approve: bool = False
    verdict: VerdictValue | None = None
    reason: str = ""


def parse(body: str) -> Command | None:
    """The first command at the start of a line. Text in code or quotes is not one."""
    match = COMMAND.search(body)
    if match is None:
        return None
    name, rest = match[1], match[2].strip()
    if name == "recheck":
        return Command("recheck", approve=rest.split()[:1] == ["--approve"])
    if name == "explain":
        return Command("explain")
    verdict, _, reason = rest.partition(" ")
    if verdict not in VerdictValue.__members__.values() or not reason.strip():
        raise CommandRefused("usage: /themis override <verdict> <reason>")
    return Command("override", verdict=VerdictValue(verdict), reason=reason.strip()[:MAX_REASON])


def is_maintainer(association: str | None, login: str, policy: RepoPolicy) -> bool:
    named = {name.lstrip("@").lower() for name in policy.policy.maintainers}
    return association in MAINTAINER_ASSOCIATIONS or login.lower() in named


def authorize(command: Command, *, maintainer: bool, is_author: bool) -> None:
    if command.name == "override" or command.approve:
        if not maintainer:
            raise CommandRefused(f"only maintainers may run {command.name}")
    elif command.name == "recheck" and not (maintainer or is_author):
        raise CommandRefused("only the author or a maintainer may recheck this")
