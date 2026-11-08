"""What every pipeline stage receives."""

from __future__ import annotations

from dataclasses import dataclass

from themis.config import RepoPolicy, Settings
from themis.models import ShieldResult, Submission
from themis.otari.client import OtariClient


@dataclass
class StageContext:
    settings: Settings
    policy: RepoPolicy
    client: OtariClient
    submission: Submission
    shield: ShieldResult | None = None

    @property
    def conversation_id(self) -> str:
        return f"themis-{self.submission.id}"
