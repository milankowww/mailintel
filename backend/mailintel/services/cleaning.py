from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", re.IGNORECASE | re.DOTALL)
ANCHOR_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
BR_RE = re.compile(r"<br\b[^>]*?/?>", re.IGNORECASE)
LI_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
BLOCK_TAG_RE = re.compile(
    r"</?(?:address|article|aside|blockquote|div|footer|h[1-6]|header|hr|ol|p|section|table|tbody|td|tfoot|th|thead|tr|ul)\b[^>]*>",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
PUNCTUATION_SPACE_RE = re.compile(r"\s+([.,;:!?])")
NEWLINE_RE = re.compile(r"\n{3,}")


def safe_email_text(text: str) -> str:
    """Remove decoded mail characters that cannot safely be stored as text."""
    return "".join(
        char for char in text
        if char in "\t\n\r" or unicodedata.category(char) not in {"Cc", "Cs"}
    )


def safe_email_value(value: Any) -> Any:
    """Sanitize text recursively, including values destined for JSONB columns."""
    if isinstance(value, str):
        return safe_email_text(value)
    if isinstance(value, list):
        return [safe_email_value(item) for item in value]
    if isinstance(value, dict):
        return {safe_email_value(key): safe_email_value(item) for key, item in value.items()}
    return value


class CleaningConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LineDropRule:
    name: str
    description: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class TokenDropRule:
    name: str
    description: str
    token_pattern: re.Pattern[str]
    max_words: int

    def matches(self, line: str) -> bool:
        return len(line.split()) <= self.max_words and bool(self.token_pattern.search(line))


@dataclass(frozen=True, slots=True)
class CleaningRules:
    line_drop_rules: tuple[LineDropRule, ...]
    token_drop_rules: tuple[TokenDropRule, ...]


@lru_cache(maxsize=1)
def _default_cleaning_rules() -> CleaningRules:
    return load_cleaning_rules()


def load_cleaning_rules(path: str | Path | None = None) -> CleaningRules:
    try:
        if path is None:
            raw_text = files("mailintel").joinpath("resources", "cleaning.yaml").read_text(encoding="utf-8")
            source = "mailintel/resources/cleaning.yaml"
        else:
            raw_text = Path(path).read_text(encoding="utf-8")
            source = str(path)
        payload = yaml.safe_load(raw_text)
    except OSError as exc:
        raise CleaningConfigError(f"Unable to read cleaning config: {path or 'mailintel/resources/cleaning.yaml'}") from exc
    except yaml.YAMLError as exc:
        raise CleaningConfigError(f"Malformed cleaning config: {path or 'mailintel/resources/cleaning.yaml'}") from exc

    if not isinstance(payload, dict):
        raise CleaningConfigError(f"Cleaning config {source} must be a mapping")

    drop_line_patterns = payload.get("drop_line_patterns", [])
    token_rules = payload.get("token_rules", [])
    if not isinstance(drop_line_patterns, list):
        raise CleaningConfigError(f"Cleaning config {source} field 'drop_line_patterns' must be a list")
    if not isinstance(token_rules, list):
        raise CleaningConfigError(f"Cleaning config {source} field 'token_rules' must be a list")

    line_drop_rules = tuple(_build_line_rule(item, source) for item in drop_line_patterns)
    token_drop_rules = tuple(_build_token_rule(item, source) for item in token_rules)
    return CleaningRules(line_drop_rules=line_drop_rules, token_drop_rules=token_drop_rules)


def _build_line_rule(item: object, source: str) -> LineDropRule:
    if not isinstance(item, dict):
        raise CleaningConfigError(f"Cleaning rule in {source} must be a mapping")
    name = str(item.get("name") or "").strip()
    pattern = item.get("pattern")
    if not name or not isinstance(pattern, str) or not pattern.strip():
        raise CleaningConfigError(f"Line-drop rule in {source} must define non-empty name and pattern")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise CleaningConfigError(f"Invalid regex in cleaning rule {name!r}: {exc}") from exc
    return LineDropRule(name=name, description=str(item.get("description") or "").strip(), pattern=compiled)


def _build_token_rule(item: object, source: str) -> TokenDropRule:
    if not isinstance(item, dict):
        raise CleaningConfigError(f"Token rule in {source} must be a mapping")
    name = str(item.get("name") or "").strip()
    tokens = item.get("tokens")
    max_words = item.get("max_words", 8)
    if not name or not isinstance(tokens, list) or not tokens:
        raise CleaningConfigError(f"Token rule in {source} must define a non-empty name and tokens list")
    try:
        max_words_value = int(max_words)
    except (TypeError, ValueError) as exc:
        raise CleaningConfigError(f"Token rule {name!r} has invalid max_words value") from exc
    escaped_tokens = [re.escape(str(token).strip()) for token in tokens if str(token).strip()]
    if not escaped_tokens:
        raise CleaningConfigError(f"Token rule {name!r} in {source} must define at least one non-empty token")
    pattern = re.compile(r"\b(?:%s)\b" % "|".join(escaped_tokens), re.IGNORECASE)
    return TokenDropRule(
        name=name,
        description=str(item.get("description") or "").strip(),
        token_pattern=pattern,
        max_words=max_words_value,
    )


def _compact_inline_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _anchor_to_text(match: re.Match[str]) -> str:
    attrs = match.group("attrs") or ""
    href_match = HREF_RE.search(attrs)
    href = html.unescape(next((group for group in href_match.groups() if group), "") if href_match else "").strip()
    label = html.unescape(TAG_RE.sub(" ", match.group("label") or ""))
    label = _compact_inline_text(label)
    if href and label and label != href:
        return f" {label} ({href}) "
    return f" {label or href} "


def html_to_text(text: str) -> str:
    if not text:
        return ""
    cleaned = COMMENT_RE.sub(" ", text)
    cleaned = SCRIPT_STYLE_RE.sub(" ", cleaned)
    cleaned = ANCHOR_RE.sub(_anchor_to_text, cleaned)
    cleaned = BR_RE.sub("\n", cleaned)
    cleaned = LI_RE.sub("\n- ", cleaned)
    cleaned = BLOCK_TAG_RE.sub("\n", cleaned)
    cleaned = TAG_RE.sub(" ", cleaned)
    return html.unescape(cleaned).replace("\xa0", " ")


def clean_email_text(text: str, rules: CleaningRules | None = None) -> str:
    if not text:
        return ""
    active_rules = rules or _default_cleaning_rules()
    cleaned = html_to_text(text)
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in cleaned.splitlines():
        line = PUNCTUATION_SPACE_RE.sub(r"\1", WHITESPACE_RE.sub(" ", raw_line).strip())
        if not line:
            continue
        lowered = line.lower()
        if any(rule.pattern.search(line) for rule in active_rules.line_drop_rules):
            continue
        if any(rule.matches(line) for rule in active_rules.token_drop_rules):
            continue
        if len(line) <= 2:
            continue
        fingerprint = lowered[:160]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        lines.append(line)
    return NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()


def embedding_input(*parts: str) -> str:
    return clean_email_text("\n".join(part for part in parts if part).strip())
