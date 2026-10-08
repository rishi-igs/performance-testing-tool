"""Models for imported scripts (HAR or JMX): filter rules, edits and export options."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
