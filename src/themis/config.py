"""Configuration: environment (and .env) plus the repository's `.github/themis.yml`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from themis.models import Role
from themis.otari.mcp import McpServer, default_servers

DEFAULT_BASE_URL = "https://api.otari.ai/api/v1"
HOSTED_GATEWAYS = ("api.otari.ai", "eu.api.otari.ai")


class ConfigError(Exception):
    """Missing or invalid configuration. CLI exit code 2."""


def _env(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True, populate_by_name=True)

    otari_api_key: SecretStr | None = Field(default=None, validation_alias=_env("OTARI_API_KEY", "otari_api_key"))
    otari_base_url: str = Field(default=DEFAULT_BASE_URL, validation_alias=_env("OTARI_BASE_URL", "otari_base_url"))
    otari_budget_demo_key: SecretStr | None = Field(
        default=None, validation_alias=_env("OTARI_BUDGET_DEMO_KEY", "otari_budget_demo_key")
    )
    otari_service_key: SecretStr | None = Field(
        default=None, validation_alias=_env("OTARI_SERVICE_KEY", "otari_service_key")
    )
    otari_peruser_budget_id: str | None = Field(
        default=None, validation_alias=_env("OTARI_PERUSER_BUDGET_ID", "otari_peruser_budget_id")
    )
    otari_contributor_budget_id: str | None = Field(
        default=None, validation_alias=_env("OTARI_CONTRIBUTOR_BUDGET_ID", "otari_contributor_budget_id")
    )

    model_clerk: str | None = Field(default=None, validation_alias=_env("THEMIS_MODEL_CLERK", "model_clerk"))
    model_prover: str | None = Field(default=None, validation_alias=_env("THEMIS_MODEL_PROVER", "model_prover"))
    model_arbiter: str | None = Field(default=None, validation_alias=_env("THEMIS_MODEL_ARBITER", "model_arbiter"))
    fallback_policy: str = Field(
        default="themis-fallback-demo", validation_alias=_env("THEMIS_FALLBACK_POLICY", "fallback_policy")
    )

    guardrail_profile: str = Field(
        default="prompt-injection", validation_alias=_env("THEMIS_GUARDRAIL_PROFILE", "guardrail_profile")
    )
    guardrail_mode: Literal["block", "monitor"] = Field(
        default="block", validation_alias=_env("THEMIS_GUARDRAIL_MODE", "guardrail_mode")
    )
    mcp_servers: list[McpServer] = Field(
        default_factory=default_servers, validation_alias=_env("THEMIS_MCP_SERVERS", "mcp_servers")
    )
    otari_timeout_s: float = Field(default=60, gt=0, validation_alias=_env("THEMIS_OTARI_TIMEOUT_S", "otari_timeout_s"))
    max_concurrency: int = Field(default=4, ge=1, validation_alias=_env("THEMIS_MAX_CONCURRENCY", "max_concurrency"))

    github_token: SecretStr | None = Field(default=None, validation_alias=_env("GITHUB_TOKEN", "github_token"))
    forgejo_token: SecretStr | None = Field(
        default=None, validation_alias=_env("THEMIS_FORGEJO_TOKEN", "forgejo_token")
    )
    forgejo_hosts: list[str] = Field(
        default_factory=lambda: ["codeberg.org"], validation_alias=_env("THEMIS_FORGEJO_HOSTS", "forgejo_hosts")
    )
    webhook_secret: SecretStr | None = Field(
        default=None, validation_alias=_env("THEMIS_WEBHOOK_SECRET", "webhook_secret")
    )
    dashboard_token: SecretStr | None = Field(
        default=None, validation_alias=_env("THEMIS_DASHBOARD_TOKEN", "dashboard_token")
    )
    db_url: str = Field(default="sqlite:///./themis.db", validation_alias=_env("THEMIS_DB_URL", "db_url"))
    log_level: str = Field(default="INFO", validation_alias=_env("THEMIS_LOG_LEVEL", "log_level"))

    @property
    def base_url(self) -> str:
        return self.otari_base_url.rstrip("/")

    @property
    def is_hosted(self) -> bool:
        return urlparse(self.base_url).hostname in HOSTED_GATEWAYS

    def api_key(self) -> str:
        if self.otari_api_key is None:
            raise ConfigError("OTARI_API_KEY is not set")
        return self.otari_api_key.get_secret_value()

    def model_for(self, role: Role) -> str:
        configured = {
            Role.CLERK: self.model_clerk,
            Role.PROVER: self.model_prover,
            Role.ARBITER: self.model_arbiter,
        }.get(role)
        if not configured:
            raise ConfigError(f"no model configured for role {role.value}: set THEMIS_MODEL_{role.name}")
        return configured

    def secrets(self) -> tuple[str, ...]:
        """Exact values to scrub, on top of the pattern-based redaction."""
        held = (
            self.otari_api_key,
            self.otari_budget_demo_key,
            self.otari_service_key,
            self.github_token,
            self.forgejo_token,
            self.webhook_secret,
            self.dashboard_token,
        )
        return tuple(secret.get_secret_value() for secret in held if secret is not None)


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        names = ", ".join(str(item["loc"][0]) for item in error.errors())
        raise ConfigError(f"invalid configuration: {names}") from error


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Kinds(_Section):
    issues: bool = True
    pull_requests: bool = True
    security_reports: bool = True

    def allows(self, *, pull_request: bool, labels: list[str]) -> bool:
        """Labels are set by people with triage rights, so an author cannot pick a bucket."""
        if pull_request:
            return self.pull_requests
        if any(label.lower() == "security" for label in labels):
            return self.security_reports
        return self.issues


class Checks(_Section):
    symbols: bool = True
    versions: bool = True
    context_mcp: bool = True
    web: bool = True
    reproduction: bool = True
    tests: bool = True
    consistency: bool = True
    dependencies: bool = True
    duplicates: bool = True
    tracebacks: bool = True
    references: bool = True
    ecosystems: bool = True
    advisories: bool = True
    test_impact: bool = True


class SignalChecks(_Section):
    """Facts shown beside the verdict. None of these can change it."""

    secrets: bool = True
    supply_chain: bool = True
    change_quality: bool = True
    completeness: bool = True


class Tests(_Section):
    command: str = "pytest -q --junitxml=themis-junit.xml"


class Budgets(_Section):
    per_contributor_daily_usd: float = Field(default=0.05, ge=0)
    project_monthly_usd: float = Field(default=10.00, ge=0)
    local_enforcement: bool = False
    burst_per_contributor_hour: int = Field(default=0, ge=0)


class Weights(_Section):
    reproduced: float = 1.0
    confirmed: float = 0.7
    unverified: float = 0.3
    soft_contradicted: float = 0.1
    hard_contradicted: float = 0.0


class Scoring(_Section):
    weights: Weights = Field(default_factory=Weights)
    plausible_threshold: float = Field(default=0.6, ge=0, le=1)


class Policy(_Section):
    auto_close: bool = False
    first_time_contributor_fast_track: bool = True
    maintainers: list[str] = Field(default_factory=list)


class Labels(_Section):
    prefix: str = "themis:"


class RepoPolicy(_Section):
    version: Literal[1] = 1
    mode: Literal["advisory", "gate"] = "advisory"
    kinds: Kinds = Field(default_factory=Kinds)
    checks: Checks = Field(default_factory=Checks)
    signals: SignalChecks = Field(default_factory=SignalChecks)
    tests: Tests = Field(default_factory=Tests)
    budgets: Budgets = Field(default_factory=Budgets)
    scoring: Scoring = Field(default_factory=Scoring)
    policy: Policy = Field(default_factory=Policy)
    labels: Labels = Field(default_factory=Labels)


def parse_policy(text: str, source: str) -> RepoPolicy:
    try:
        return RepoPolicy.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValidationError) as error:
        raise ConfigError(f"invalid policy file {source}: {error}") from error


def load_policy(path: Path) -> RepoPolicy:
    """Read a policy file. A missing file means the safe defaults; a broken one is an error."""
    return parse_policy(path.read_text(), str(path)) if path.exists() else RepoPolicy()
