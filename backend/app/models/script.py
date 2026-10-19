"""Models for imported scripts (HAR or JMX): filter rules, edits and export options."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Requirements 8.3 defaults, plus the static types JMeter's own recorder template excludes.
DEFAULT_EXTENSIONS = [
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "bmp", "ico",
    "css", "js", "mjs", "map", "woff", "woff2", "ttf", "otf", "eot", "swf", "mp4", "webm",
]
DEFAULT_MIME_TYPES = [
    "image/*", "text/css", "font/*", "application/font-*", "application/x-font-*",
    "text/javascript", "application/javascript", "application/x-javascript",
]
# Analytics, ads, tag managers, session replay and chat widgets. A starting list: edit per project.
DEFAULT_DOMAINS = [
    "*.google-analytics.com", "*.analytics.google.com", "*.googletagmanager.com",
    "*.doubleclick.net", "*.googlesyndication.com", "*.googleadservices.com",
    "*.hotjar.com", "*.hotjar.io", "*.clarity.ms", "*.facebook.net", "bat.bing.com",
    "px.ads.linkedin.com", "*.segment.io", "*.segment.com", "*.mixpanel.com",
    "*.fullstory.com", "*.newrelic.com", "*.nr-data.net", "*.sentry.io",
    "*.intercom.io", "*.intercomcdn.com", "*.zdassets.com", "*.livechatinc.com", "*.optimizely.com",
]


class TransactionRule(BaseModel):
    """Requests whose URL (without the query string) matches the glob go into this business function."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    url_pattern: str = Field(min_length=1, max_length=500)

    @field_validator("name", "url_pattern")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class FilterRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS), max_length=200)
    exclude_mime_types: list[str] = Field(default_factory=lambda: list(DEFAULT_MIME_TYPES), max_length=200)
    exclude_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_DOMAINS), max_length=500)
    exclude_methods: list[str] = Field(default_factory=lambda: ["OPTIONS"], max_length=20)
    keep_only_api: bool = False
    # A new business function starts after this much idle time (HAR only). 0 turns it off.
    # 5 s matches JMeter's recorder default (proxy.pause=5000).
    idle_gap_seconds: float = Field(5, ge=0, le=600)
    transaction_rules: list[TransactionRule] = Field(default_factory=list, max_length=200)

    @field_validator("exclude_extensions")
    @classmethod
    def _extensions(cls, v: list[str]) -> list[str]:
        return [e.strip().lower().lstrip(".") for e in v if e.strip().lstrip(".")]

    @field_validator("exclude_mime_types", "exclude_domains")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [e.strip().lower() for e in v if e.strip()]

    @field_validator("exclude_methods")
    @classmethod
    def _methods(cls, v: list[str]) -> list[str]:
        return [e.strip().upper() for e in v if e.strip()]


class RequestChange(BaseModel):
    """A manual edit to one request. Send `null` to return a field to the automatic choice."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    include: bool | None = None
    transaction: str | None = Field(default=None, max_length=80)


class ScriptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[RequestChange] = Field(default_factory=list, max_length=20_000)
    rules: FilterRules | None = None


class CorrelationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_index: int = Field(ge=0)
    target_indices: list[int] = Field(min_length=1, max_length=10_000)
    variable: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    extractor: Literal["json"] = "json"
    expression: str = Field(min_length=1, max_length=500)
    enabled: bool = True


class CorrelationsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlations: list[CorrelationRule] = Field(default_factory=list, max_length=200)

    @field_validator("correlations")
    @classmethod
    def _unique_variables(cls, rules: list[CorrelationRule]) -> list[CorrelationRule]:
        variables = [rule.variable for rule in rules]
        if len(variables) != len(set(variables)):
            raise ValueError("correlation variable names must be unique")
        return rules


class ParameterRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variable: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    recorded_value: str | None = Field(default=None, min_length=1, max_length=100_000)


class ParameterizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: list[ParameterRule] = Field(default_factory=list, max_length=200)
    recycle: bool = True
    sharing_mode: Literal["all_threads", "per_thread_group", "per_thread"] = "all_threads"

    @field_validator("parameters")
    @classmethod
    def _unique_parameters(cls, rules: list[ParameterRule]) -> list[ParameterRule]:
        variables = [rule.variable for rule in rules]
        if len(variables) != len(set(variables)):
            raise ValueError("parameter variable names must be unique")
        columns = [rule.column for rule in rules]
        if len(columns) != len(set(columns)):
            raise ValueError("each CSV column can be assigned to only one variable")
        return rules


class StatusCodeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["status_code"]
    expected: int | Literal["2xx", "3xx", "4xx", "5xx"]


class TextCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text_contains", "text_not_contains", "text_equals", "text_matches"]
    value: str = Field(min_length=1, max_length=10_000)


class JsonPathCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["json_path"]
    path: str = Field(min_length=1, max_length=500)
    expected: str | None = Field(default=None, max_length=10_000)
    exists: bool = True


class HeaderCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["header_value"]
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=10_000)
    operator: Literal["contains", "equals", "matches"] = "contains"


class DurationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["duration"]
    max_ms: int = Field(gt=0, le=3_600_000)


class ResponseSizeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["response_size"]
    min_bytes: int = Field(default=0, ge=0)
    max_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _valid_range(self):
        if self.max_bytes is not None and self.max_bytes < self.min_bytes:
            raise ValueError("max_bytes must be greater than or equal to min_bytes")
        return self


AssertionCheck = Annotated[
    StatusCodeCheck | TextCheck | JsonPathCheck | HeaderCheck | DurationCheck | ResponseSizeCheck,
    Field(discriminator="type"),
]


class AssertionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["global", "transaction", "request"]
    request_index: int | None = Field(default=None, ge=0)
    transaction: str | None = Field(default=None, min_length=1, max_length=80)
    checks: list[AssertionCheck] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _valid_scope(self):
        if self.scope == "request" and self.request_index is None:
            raise ValueError("request_index is required for request-scoped assertions")
        if self.scope == "transaction" and not self.transaction:
            raise ValueError("transaction is required for transaction-scoped assertions")
        if self.scope == "global" and (self.request_index is not None or self.transaction is not None):
            raise ValueError("global assertions cannot specify a request or transaction")
        if self.scope == "request" and self.transaction is not None:
            raise ValueError("request-scoped assertions cannot specify a transaction")
        return self


class AssertionsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assertions: list[AssertionRule] = Field(default_factory=list, max_length=10_000)


class DebugRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    users: int = Field(default=1, ge=1, le=5)
    loops: int = Field(default=1, ge=1, le=5)
