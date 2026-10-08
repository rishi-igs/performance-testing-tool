"""Models for imported scripts (HAR or JMX): filter rules, edits, design and export options."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VarName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
FileName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\.(csv|txt)$")]

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


# ---- Script design: what VuGen calls correlation, parameters, checks and run-time settings ----

class CorrelationRule(BaseModel):
    """Extract a value from one response into ${variable} (like LoadRunner's web_reg_save_param)."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    variable: VarName
    source: int = Field(ge=0, description="index of the request whose response holds the value")
    extractor: Literal["json", "regex", "boundary"]
    expression: str = Field(min_length=1, max_length=2000, description="JSON path, regular expression or left boundary")
    right: str | None = Field(default=None, max_length=500, description="right boundary (boundary extractor)")
    scope: Literal["body", "headers"] = "body"
    match: int = Field(1, ge=0, le=1000, description="1 = first match, 0 = random")
    replace: str | None = Field(default=None, max_length=10_000,
                                description="recorded text to replace with ${variable} in later requests")
    origin: Literal["auto", "manual"] = "manual"
    preview: str | None = None
    used_in: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "CorrelationRule":
        if self.extractor == "boundary" and not self.right:
            raise ValueError("a boundary extractor needs a right boundary")
        if self.extractor == "regex" and "(" not in self.expression:
            raise ValueError("the regular expression needs a group, for example token=(\\w+)")
        if self.extractor == "json" and not self.expression.startswith("$"):
            raise ValueError("a JSON path starts with $, for example $.token")
        if self.extractor == "json" and self.scope != "body":
            raise ValueError("JSON paths read the response body")
        return self


class Parameter(BaseModel):
    """Test data generated per user or per use (like LoadRunner parameter types)."""

    model_config = ConfigDict(extra="forbid")

    name: VarName
    type: Literal["list", "random_number", "unique_number", "uuid", "date", "random_string"]
    values: list[str] = Field(default_factory=list, max_length=100_000)
    selection: Literal["sequential", "random"] = "sequential"
    minimum: int = 1
    maximum: int = 1000
    start: int = 1
    increment: int = 1
    per_user: bool = False
    format: str = Field("", max_length=100, description="Java date format, e.g. yyyy-MM-dd")
    offset_days: int = Field(0, ge=-36_500, le=36_500)
    length: int = Field(10, ge=1, le=1000)
    update: Literal["iteration", "occurrence"] = "iteration"

    @model_validator(mode="after")
    def _check(self) -> "Parameter":
        if self.type == "list" and not self.values:
            raise ValueError(f"parameter {self.name}: a list needs at least one value")
        if self.type == "list" and any("\n" in v or "\r" in v for v in self.values):
            raise ValueError(f"parameter {self.name}: list values must be single lines")
        if self.type == "random_number" and self.minimum > self.maximum:
            raise ValueError(f"parameter {self.name}: minimum is larger than maximum")
        if self.type == "date" and not self.format:
            self.format = "yyyy-MM-dd"
        return self


class DataFile(BaseModel):
    """A CSV file of test data; each column becomes a variable (CSV Data Set Config)."""

    model_config = ConfigDict(extra="forbid")

    name: FileName
    columns: list[VarName] = Field(min_length=1, max_length=100)
    sharing: Literal["all", "group", "user"] = "all"
    recycle: bool = True
    stop_at_end: bool = False
    first_line_header: bool = True
    rows: int = 0


class Replacement(BaseModel):
    """Replace recorded text with ${variable} in every request (LoadRunner's "replace with parameter")."""

    model_config = ConfigDict(extra="forbid")

    find: str = Field(min_length=1, max_length=10_000)
    variable: VarName


class Check(BaseModel):
    """A content check on one request or on every request (like LoadRunner's web_reg_find)."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    item: int | None = Field(default=None, ge=0, description="request index; empty = every request")
    type: Literal["text", "not_text", "regex", "json_path", "header", "duration", "size_max"]
    value: str | None = Field(default=None, max_length=5000)
    path: str | None = Field(default=None, max_length=500)
    expected: str | None = Field(default=None, max_length=5000)
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "Check":
        needs = {"text": "value", "not_text": "value", "regex": "value", "header": "value",
                 "json_path": "path", "duration": "limit", "size_max": "limit"}[self.type]
        if getattr(self, needs) in (None, ""):
            raise ValueError(f"a {self.type} check needs {needs}")
        if self.type == "json_path" and not (self.path or "").startswith("$"):
            raise ValueError("a JSON path starts with $")
        return self


class ThinkTime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["recorded", "ignore", "fixed", "random"] = "recorded"
    multiplier: float = Field(1.0, ge=0, le=10)
    seconds: float = Field(1.0, ge=0, le=3600)
    minimum: float = Field(1.0, ge=0, le=3600)
    maximum: float = Field(3.0, ge=0, le=3600)
    cap_seconds: float = Field(30.0, ge=0, le=3600, description="longest recorded pause replayed")

    @model_validator(mode="after")
    def _check(self) -> "ThinkTime":
        if self.minimum > self.maximum:
            raise ValueError("think time minimum is larger than maximum")
        return self


class Pacing(BaseModel):
    """When each new iteration starts (LoadRunner run-time setting "Pacing")."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "interval", "after_fixed", "after_random"] = "none"
    seconds: float = Field(10.0, gt=0, le=86_400)
    minimum: float = Field(5.0, ge=0, le=86_400)
    maximum: float = Field(15.0, ge=0, le=86_400)

    @model_validator(mode="after")
    def _check(self) -> "Pacing":
        if self.minimum > self.maximum:
            raise ValueError("pacing minimum is larger than maximum")
        return self


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    think_time: ThinkTime = Field(default_factory=ThinkTime)
    pacing: Pacing = Field(default_factory=Pacing)
    on_error: Literal["continue", "next_iteration", "stop_user"] = "continue"
    new_user_each_iteration: bool = True
    timeout_seconds: int = Field(30, ge=1, le=600)
    status_checks: bool = True


class ScriptDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlations: list[CorrelationRule] = Field(default_factory=list, max_length=2000)
    parameters: list[Parameter] = Field(default_factory=list, max_length=500)
    data_files: list[DataFile] = Field(default_factory=list, max_length=50)
    replacements: list[Replacement] = Field(default_factory=list, max_length=2000)
    checks: list[Check] = Field(default_factory=list, max_length=5000)
    settings: RuntimeSettings = Field(default_factory=RuntimeSettings)

    @model_validator(mode="after")
    def _check(self) -> "ScriptDesign":
        names: dict[str, str] = {}
        for owner, variables in ([(f"parameter {p.name}", [p.name]) for p in self.parameters]
                                 + [(f"file {f.name}", f.columns) for f in self.data_files]):
            for v in variables:
                if v.lower() in names:
                    raise ValueError(f"variable {v} is defined twice ({names[v.lower()]} and {owner})")
                names[v.lower()] = owner
        for rule in self.correlations:
            if rule.variable.lower() in names:
                raise ValueError(f"variable {rule.variable} is both a correlation and {names[rule.variable.lower()]}")
        if len({f.name.lower() for f in self.data_files}) != len(self.data_files):
            raise ValueError("data file names must be unique")
        for prefix, entries in (("c", self.correlations), ("k", self.checks)):
            used = {e.id for e in entries if e.id}
            n = 1
            for entry in entries:
                if not entry.id:
                    while f"{prefix}{n}" in used:
                        n += 1
                    entry.id = f"{prefix}{n}"
                    used.add(entry.id)
        return self


class DesignUpdate(BaseModel):
    """Replace one or more parts of a script's design; parts left out are unchanged."""

    model_config = ConfigDict(extra="forbid")

    correlations: list[CorrelationRule] | None = None
    parameters: list[Parameter] | None = None
    data_files: list[DataFile] | None = None
    replacements: list[Replacement] | None = None
    checks: list[Check] | None = None
    settings: RuntimeSettings | None = None
