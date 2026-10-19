from __future__ import annotations

import math
import re
from collections import Counter

from ..models import InterestMatch, InterestProfile, MessageSummary, NormalizedMessage, TopicCluster

WORD_RE = re.compile(r"[^\W\d_][\w-]{2,}")
SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv):\s*)+", re.IGNORECASE)
SUBJECT_TAG_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
STOPWORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "this",
    "from",
    "have",
    "your",
    "about",
    "subject",
    "hello",
    "thanks",
    "please",
    "team",
}
GENERIC_TOPIC_WORDS = {
    "action",
    "annual",
    "daily",
    "followup",
    "follow-up",
    "fyi",
    "monthly",
    "next",
    "note",
    "quarterly",
    "required",
    "reminder",
    "steps",
    "status",
    "update",
    "updates",
    "weekly",
}
SUMMARY_DETAIL_STOPWORDS = GENERIC_TOPIC_WORDS | {
    "and",
    "are",
    "can",
    "for",
    "from",
    "has",
    "have",
    "into",
    "its",
    "moved",
    "move",
    "moving",
    "need",
    "needs",
    "needed",
    "please",
    "still",
    "their",
    "there",
    "they",
    "who",
}
CALENDAR_TERMS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in WORD_RE.finditer(text) if match.group(0).lower() not in STOPWORDS]


def _normalize_topic_term(term: str) -> str:
    normalized = term.strip().lower().replace("_", "-")
    if normalized.endswith("ies") and len(normalized) > 4:
        return f"{normalized[:-3]}y"
    if normalized.endswith(("ses", "xes", "zes", "ches", "shes")) and len(normalized) > 5:
        return normalized[:-2]
    if normalized.endswith("s") and len(normalized) > 4 and not normalized.endswith("ss"):
        return normalized[:-1]
    return normalized


def _strip_subject_noise(subject: str) -> str:
    cleaned = subject.strip()
    while cleaned:
        updated = SUBJECT_PREFIX_RE.sub("", cleaned)
        updated = SUBJECT_TAG_RE.sub("", updated).strip()
        if updated == cleaned:
            return cleaned
        cleaned = updated
    return subject.strip()


def _iter_phrases(tokens: list[str], size: int) -> list[tuple[str, ...]]:
    if len(tokens) < size:
        return []
    return [tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]


def _is_meaningful_phrase(phrase: tuple[str, ...]) -> bool:
    if not phrase:
        return False
    if all(token in GENERIC_TOPIC_WORDS for token in phrase):
        return False
    if len(phrase) > 1 and (phrase[0] in GENERIC_TOPIC_WORDS or phrase[-1] in GENERIC_TOPIC_WORDS):
        return False
    return True


def _count_phrase(tokens: list[str], phrase: tuple[str, ...]) -> int:
    return sum(1 for candidate in _iter_phrases(tokens, len(phrase)) if candidate == phrase)


def _top_terms(tokens: list[str], limit: int = 3) -> list[str]:
    counts = Counter(token for token in tokens if token not in GENERIC_TOPIC_WORDS)
    if counts:
        return [term for term, _ in counts.most_common(limit)]
    counts = Counter(tokens)
    return [term for term, _ in counts.most_common(limit)]


def _format_topic_name(tokens: list[str]) -> str:
    return " ".join(token.title() for token in tokens)


def _format_summary_label(label: str) -> str:
    parts = []
    for token in label.split():
        parts.append(token.title() if token in CALENDAR_TERMS else token)
    return " ".join(parts)


def _build_topic_key(topic_terms: list[str], key_terms: list[str]) -> str:
    normalized_terms: list[str] = []
    for term in topic_terms + key_terms:
        normalized = _normalize_topic_term(term)
        if not normalized or normalized in GENERIC_TOPIC_WORDS:
            continue
        normalized_terms.append(normalized)
    if not normalized_terms:
        return "general"
    counts = Counter(normalized_terms)
    first_seen = {term: index for index, term in enumerate(dict.fromkeys(normalized_terms))}
    ranked = sorted(counts, key=lambda term: (-counts[term], first_seen[term]))
    return ":".join(sorted(ranked[: min(2, len(ranked))]))


def _select_topic_phrase(subject: str, source_text: str) -> list[str]:
    subject_tokens = _tokenize(_strip_subject_noise(subject))
    body_tokens = _tokenize(source_text)
    phrase_scores: dict[tuple[str, ...], float] = {}

    for tokens, source_weight in ((subject_tokens, 5.0), (body_tokens, 2.0)):
        for size in range(2, 4):
            for index, phrase in enumerate(_iter_phrases(tokens, size)):
                if not _is_meaningful_phrase(phrase):
                    continue
                phrase_scores[phrase] = phrase_scores.get(phrase, 0.0) + source_weight + (size - 1) - (index * 0.05)

    for phrase in tuple(phrase_scores):
        if _count_phrase(subject_tokens, phrase) and _count_phrase(body_tokens, phrase):
            phrase_scores[phrase] += 6.0

    if phrase_scores:
        best_phrase = max(
            phrase_scores.items(),
            key=lambda item: (
                item[1],
                len(item[0]),
                sum(Counter(subject_tokens + body_tokens)[token] for token in item[0]),
                tuple(reversed(item[0])),
            ),
        )[0]
        return list(best_phrase)

    subject_terms = _top_terms(subject_tokens, limit=4)
    if len(subject_terms) >= 2:
        return subject_terms[:4]
    combined_terms = _top_terms(subject_tokens + body_tokens, limit=4)
    if len(combined_terms) >= 2:
        return combined_terms[:4]
    return combined_terms[:1]


def _build_summary_lead(subject: str, source_text: str) -> tuple[str, list[str]]:
    cleaned_subject = _strip_subject_noise(subject)
    focus_tokens = _select_topic_phrase(cleaned_subject, source_text)
    if focus_tokens:
        return f"Update about {' '.join(focus_tokens)}", focus_tokens
    if cleaned_subject:
        return f"Update about {cleaned_subject}", []
    return "Message update", []


def _detail_phrase_candidates(source_text: str, focus_tokens: list[str]) -> list[str]:
    source_tokens = _tokenize(source_text)
    focus_set = {_normalize_topic_term(token) for token in focus_tokens}
    candidates: list[str] = []
    seen: set[str] = set()
    for phrase in _iter_phrases(source_tokens, 2):
        normalized_phrase = tuple(_normalize_topic_term(token) for token in phrase)
        if not _is_meaningful_phrase(phrase):
            continue
        if any(token in SUMMARY_DETAIL_STOPWORDS for token in normalized_phrase):
            continue
        if any(token in focus_set for token in normalized_phrase):
            continue
        label = " ".join(phrase)
        if label in seen:
            continue
        seen.add(label)
        candidates.append(label)
    return candidates


def _detail_token_candidates(source_text: str, focus_tokens: list[str]) -> list[str]:
    focus_set = {_normalize_topic_term(token) for token in focus_tokens}
    tokens = [_normalize_topic_term(token) for token in _tokenize(source_text)]
    counts = Counter()
    first_seen: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token in focus_set or token in SUMMARY_DETAIL_STOPWORDS:
            continue
        counts[token] += 1
        first_seen.setdefault(token, index)
    ranked = sorted(counts, key=lambda token: (-counts[token], first_seen[token]))
    return ranked


def _build_summary_details(source_text: str, focus_tokens: list[str]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for label in _detail_phrase_candidates(source_text, focus_tokens) + _detail_token_candidates(source_text, focus_tokens):
        normalized = _normalize_topic_term(label.replace(" ", "-"))
        if normalized in seen:
            continue
        seen.add(normalized)
        labels.append(_format_summary_label(label))
        if len(labels) == 3:
            break
    return labels


def build_summary(message: NormalizedMessage, source_text: str, interest_matches: list[InterestMatch]) -> MessageSummary:
    lead, focus_tokens = _build_summary_lead(message.subject, source_text)
    lines = [lead]
    details = []
    summary_labels = _build_summary_details(source_text, focus_tokens)
    if summary_labels:
        details.append(f"Key topics: {', '.join(summary_labels)}")
    if interest_matches:
        strongest = max(interest_matches, key=lambda match: match.score)
        qualifier = "relevant" if details else "Relevant"
        details.append(f"{qualifier} to {strongest.rationale}")
    if details:
        lines.append("; ".join(details))
    importance = score_importance(message, source_text, interest_matches)
    rationale_parts = []
    if message.unread:
        rationale_parts.append("unread")
    if message.has_attachments:
        rationale_parts.append("has attachments")
    if interest_matches:
        rationale_parts.append("matches interest profiles")
    if message.importance_hint:
        rationale_parts.append(f"provider flagged {message.importance_hint}")
    rationale = ", ".join(rationale_parts) or "baseline relevance"
    return MessageSummary(
        message_id=None,
        summary_text=". ".join(part for part in lines if part).strip(". ") + ".",
        importance_score=importance,
        rationale=rationale,
        confidence=0.72 if interest_matches else 0.55,
        model_name="rules/v1",
    )


def score_importance(message: NormalizedMessage, source_text: str, interest_matches: list[InterestMatch]) -> float:
    score = 0.25
    lowered = f"{message.subject} {source_text}".lower()
    if message.unread:
        score += 0.15
    if message.has_attachments:
        score += 0.05
    if any(token in lowered for token in ("urgent", "action required", "deadline", "asap")):
        score += 0.2
    if message.importance_hint and message.importance_hint.lower() in {"high", "important"}:
        score += 0.15
    if interest_matches:
        score += max(match.score for match in interest_matches)
    return min(score, 1.0)


def match_interest_profiles(message: NormalizedMessage, source_text: str, profiles: list[InterestProfile]) -> list[InterestMatch]:
    text = " ".join(
        [
            message.subject,
            source_text,
            message.sender_name,
            message.sender_address,
            " ".join(message.recipients),
            " ".join(message.cc),
        ]
    ).lower()
    text_tokens = set(_tokenize(text))
    matches: list[InterestMatch] = []
    for profile in profiles:
        if not profile.signals.strip() and not profile.collaborators:
            continue
        signals = _profile_signals(profile.signals)
        phrase_hits = [signal for signal in signals if " " in signal and signal in text]
        token_signals = {signal for signal in signals if " " not in signal}
        token_hits = sorted(token_signals & text_tokens)
        collaborator_hits = [collaborator for collaborator in profile.collaborators if collaborator and collaborator.lower() in text]
        hit_count = len(phrase_hits) + len(token_hits) + len(collaborator_hits)
        if not hit_count:
            continue
        if not phrase_hits and not collaborator_hits and len(token_hits) < 2:
            continue
        denominator = max(len(signals) * 0.45, 1.0)
        raw_score = (hit_count / denominator) * min(profile.priority_weight, 1.5)
        score = min(raw_score, 1.0)
        if score >= 0.2:
            hit_labels = phrase_hits + token_hits + collaborator_hits
            matches.append(
                InterestMatch(
                    interest_profile_id=profile.id or 0,
                    score=score,
                    rationale=f"{profile.name} signals: {', '.join(hit_labels[:4])}",
                )
            )
    matches.sort(key=lambda item: item.score, reverse=True)
    if matches:
        matches[0].primary_match = True
    return matches


def _profile_signals(signals: str) -> list[str]:
    raw_parts = re.split(r"[,;\n]+", signals.lower())
    normalized: list[str] = []
    for part in raw_parts:
        tokens = _tokenize(part)
        if not tokens:
            continue
        signal = " ".join(tokens)
        if signal not in normalized:
            normalized.append(signal)
    return normalized


def derive_topic_cluster(message: NormalizedMessage, source_text: str) -> TopicCluster:
    topic_terms = _select_topic_phrase(message.subject, source_text) or ["general"]
    name = _format_topic_name(topic_terms)
    key_terms = list(dict.fromkeys(topic_terms + _top_terms(_tokenize(f"{message.subject} {source_text}"))))[:4]
    topic_key = _build_topic_key(topic_terms, key_terms)
    description = f"Messages about {' '.join(topic_terms)}"
    tokens = _tokenize(f"{message.subject} {source_text}")
    importance = min(0.3 + math.log(len(tokens) + 1, 10), 1.0)
    return TopicCluster(
        id=None,
        topic_key=topic_key,
        name=name,
        description=description,
        key_terms=key_terms or ["general"],
        importance_score=importance,
    )
