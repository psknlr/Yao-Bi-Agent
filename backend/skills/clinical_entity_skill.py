"""Polarity-aware clinical entity scanning（否定/不确定语义识别）.

Clinical narratives are dominated by *pertinent negatives*（"否认外伤"、"无发热寒战"、
"大小便正常"、"排除感染"）. Naive substring matching reads every mention as a positive
finding and poisons all downstream safety logic — a patient who explicitly denies red
flags would be routed to the emergency branch. This module is the single shared scanner:
every keyword-driven skill (extraction, normalization, red-flag screening) must resolve a
term's polarity here before treating it as clinical evidence.

Entities carry a uniform, auditable shape::

    {
        "entity": "发热",
        "polarity": "negated",          # affirmed | negated | uncertain
        "temporality": "current",       # current | historical
        "experiencer": "patient",
        "source_span": "无发热寒战",
        "confidence": 0.95,
    }

Invariant: only ``polarity == "affirmed"`` entities may enter clinical reasoning.
``negated`` mentions are recorded as pertinent negatives; ``uncertain`` mentions
(questions, hedges) must trigger follow-up inquiry — never alarms, never reassurance.
"""

from __future__ import annotations

from typing import Any, Iterable

POLARITY_AFFIRMED = "affirmed"
POLARITY_NEGATED = "negated"
POLARITY_UNCERTAIN = "uncertain"

# Clause boundaries: polarity never crosses these (each clause carries its own negation).
_CLAUSE_BOUNDARIES = "，,。;；!！?？\n"

# Pre-negation cues, longest first so 没有/无明显 win before 没/无.
_NEGATION_PREFIXES = ("没有", "否认", "排除", "未见", "无明显", "不伴", "没", "无", "未", "不", "非")

# Post-negation cues: the term is followed by a normality assertion（"大小便正常"、"二便调"）.
_NEGATION_SUFFIXES = ("正常", "无异常", "未见异常", "阴性", "（-）", "(-)", "调", "通畅", "自如", "如常")
# Filler characters allowed between the term and a post-negation cue（"大小便均正常"）.
_SUFFIX_FILLERS = "均尚基本亦也都情况"

# A question about a symptom is not a report of the symptom（"会发热吗？"、"是否外伤"）.
_UNCERTAIN_MARKERS = ("是否", "会不会", "有没有", "要不要", "能不能", "吗", "？", "?", "可能")

# Tokens that end a negation's forward scope inside one clause（"没力气伴腰痛" ⇒ 腰痛 affirmed）.
_SCOPE_BREAKERS = ("伴", "但", "而", "出现", "仍", "转为", "加重", "现")

# Forward reach of a pre-negation cue（covers enumerations like "无发热、寒战、消瘦"）.
_NEGATION_WINDOW = 12

# Historical-context cues: the finding belongs to the past history, not this episode.
_HISTORICAL_MARKERS = ("既往", "曾经", "曾", "以前", "过去", "病史")

# Resolution cues: the finding existed but is explicitly stated as resolved. Looked for
# after the term in the same clause AND in the immediately following clause, since
# Chinese narratives split them with a comma ("一周前感冒发热，现已痊愈").
# NOTE: recency phrases like "一周前" alone do NOT downgrade — a fever one week ago
# without explicit resolution is still clinically relevant for infection screening.
_RESOLVED_MARKERS = ("已痊愈", "痊愈", "已愈", "已缓解", "已消退", "已退", "已恢复", "已好转", "现已无", "已无", "已复位", "复位后")

# Look-ahead binding is STRICT: the next clause counts as resolving THIS term only when
# it is a *pure* resolution assertion ("现已痊愈"), not a clause about another symptom
# ("腰痛已缓解" must not mark a preceding 发热 as resolved — that would be a dangerous
# false negative for infection screening).
import re as _re

_PURE_RESOLUTION_RE = _re.compile(
    r"^(?:现|今|目前|此后)?(?:均|都)?已(?:经)?(?:完全|基本)?(?:痊愈|缓解|好转|恢复|消退|复位|退)(?:了)?$"
)

# Remote-past cues: an event years in the past is history, not this episode ("十年前
# 车祸" must not fire a current major-trauma emergency). Days/weeks stay current —
# a fall three days ago is very much this episode.
_REMOTE_PAST_RE = _re.compile(r"(?:[一二三四五六七八九十两0-9]+|数|多|几)年(?:前|以前)")

# Experiencer cues: the finding belongs to someone else ("父亲昨日车祸后腰痛"),
# and must never enter the *patient's* red flags. Matched before the term in-clause.
_OTHER_EXPERIENCER_MARKERS = (
    "父亲", "母亲", "家父", "家母", "爸爸", "妈妈", "爷爷", "奶奶", "外公", "外婆",
    "哥哥", "姐姐", "弟弟", "妹妹", "丈夫", "妻子", "老伴", "儿子", "女儿", "孩子",
    "家属", "家人", "亲戚", "朋友", "同事", "邻居",
)


def _clauses(text: str) -> list[tuple[int, str]]:
    """Split into (start_offset, clause) pairs on clause boundaries."""

    clauses: list[tuple[int, str]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in _CLAUSE_BOUNDARIES:
            if i > start:
                clauses.append((start, text[start:i]))
            start = i + 1
    if start < len(text):
        clauses.append((start, text[start:]))
    return clauses


def _containing_clause(clauses: list[tuple[int, str]], idx: int) -> tuple[int, str]:
    for start, clause in clauses:
        if start <= idx < start + len(clause):
            return start, clause
    return 0, ""


def _containing_clause_index(clauses: list[tuple[int, str]], idx: int) -> int:
    for i, (start, clause) in enumerate(clauses):
        if start <= idx < start + len(clause):
            return i
    return 0


def _pre_negated(clause: str, term_start: int) -> bool:
    """A negation cue precedes the term in-clause, within scope, with no breaker between."""

    prefix = clause[:term_start]
    best_end = -1
    for marker in _NEGATION_PREFIXES:
        pos = prefix.rfind(marker)
        if pos != -1:
            best_end = max(best_end, pos + len(marker))
    if best_end == -1:
        return False
    between = clause[best_end:term_start]
    if len(between) > _NEGATION_WINDOW:
        return False
    return not any(breaker in between for breaker in _SCOPE_BREAKERS)


def _post_negated(clause: str, term_end: int) -> bool:
    remainder = clause[term_end:].lstrip(_SUFFIX_FILLERS)
    return remainder.startswith(_NEGATION_SUFFIXES)


def _occurrence_polarity(clause: str, term_start: int, term_end: int) -> str:
    if any(marker in clause for marker in _UNCERTAIN_MARKERS):
        return POLARITY_UNCERTAIN
    if _pre_negated(clause, term_start) or _post_negated(clause, term_end):
        return POLARITY_NEGATED
    return POLARITY_AFFIRMED


def _temporality(clauses: list[tuple[int, str]], clause_index: int, clause: str, term_start: int, term_end: int) -> str:
    """current | historical | resolved — the temporal status safety grading consumes."""

    remainder = clause[term_end:]
    if any(m in remainder for m in _RESOLVED_MARKERS):
        return "resolved"
    if clause_index + 1 < len(clauses):
        next_clause = clauses[clause_index + 1][1].strip()
        # Strict binding: only a *pure* resolution clause resolves this term; a clause
        # naming another symptom ("腰痛已缓解") binds to that symptom, not this one.
        if _PURE_RESOLUTION_RE.match(next_clause):
            return "resolved"
    prefix = clause[:term_start]
    if any(m in prefix for m in _HISTORICAL_MARKERS) or _REMOTE_PAST_RE.search(prefix):
        return "historical"
    return "current"


def _experiencer(clause: str, term_start: int) -> str:
    """patient | other — a family member's event must not become the patient's red flag."""

    prefix = clause[:term_start]
    return "other" if any(m in prefix for m in _OTHER_EXPERIENCER_MARKERS) else "patient"


_CONFIDENCE = {POLARITY_AFFIRMED: 0.9, POLARITY_NEGATED: 0.95, POLARITY_UNCERTAIN: 0.7}


def scan_term(text: str, term: str, blocked_spans: list[tuple[int, int]] | None = None) -> dict[str, Any] | None:
    """Aggregate the polarity of every occurrence of ``term`` in ``text``.

    Safety-first aggregation: one affirmed occurrence outweighs any number of denials
    ("无发热。今晨发热" is a positive finding); uncertain outranks negated (a question
    means the fact is unresolved, not absent). Returns ``None`` when the term is absent
    or every occurrence lies inside a longer already-matched term (``blocked_spans``).
    """

    clauses = _clauses(text)
    seen: list[dict[str, Any]] = []
    search_from = 0
    while True:
        idx = text.find(term, search_from)
        if idx == -1:
            break
        search_from = idx + 1
        end = idx + len(term)
        if any(bs <= idx and end <= be for bs, be in blocked_spans or []):
            continue
        clause_start, clause = _containing_clause(clauses, idx)
        clause_index = _containing_clause_index(clauses, idx)
        polarity = _occurrence_polarity(clause, idx - clause_start, end - clause_start)
        seen.append({
            "polarity": polarity,
            "source_span": clause,
            "temporality": _temporality(clauses, clause_index, clause, idx - clause_start, end - clause_start),
            "experiencer": _experiencer(clause, idx - clause_start),
            "span": (idx, end),
        })
    if not seen:
        return None
    # Safety-first aggregation across MULTIPLE events of the same term:
    # 1) a patient-experienced affirmation outranks a family member's
    #    ("父亲车祸后腰痛，我也腰痛" — the patient's own finding wins the record);
    # 2) among affirmed patient events, a CURRENT event outranks a historical or
    #    resolved one ("十年前车祸后腰痛，今天再次车祸后不能站立" — today's crash is
    #    the safety-relevant fact; collapsing to the first occurrence was the
    #    history-masks-current-emergency false negative of the v0.13 review).
    # All events stay visible in ``events`` for the physician record.
    ranked = {POLARITY_AFFIRMED: 2, POLARITY_UNCERTAIN: 1, POLARITY_NEGATED: 0}
    temporal_rank = {"current": 2, "historical": 1, "resolved": 0}
    best = max(seen, key=lambda o: (
        ranked[o["polarity"]],
        o["experiencer"] == "patient",
        temporal_rank.get(o["temporality"], 2),
    ))
    return {
        "entity": term,
        "polarity": best["polarity"],
        "temporality": best["temporality"],
        "experiencer": best["experiencer"],
        "source_span": best["source_span"],
        "confidence": _CONFIDENCE[best["polarity"]],
        "occurrences": len(seen),
        # Event-level record (v0.14): every occurrence with its own polarity /
        # temporality / experiencer, so downstream consumers can audit which event
        # the aggregated verdict came from instead of trusting a collapsed fact.
        "events": [
            {k: o[k] for k in ("polarity", "temporality", "experiencer", "source_span")}
            for o in seen
        ],
        "spans": [o["span"] for o in seen],
    }


def scan_entities(
    text: str,
    terms: Iterable[str],
    category_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Scan ``terms`` in ``text`` with longest-match precedence.

    A shorter term fully inside a longer matched term's span is suppressed, so
    "大小便失禁" does not additionally emit a bare "大小便" entity. When
    ``category_map`` is given each entity carries its ``category``.
    """

    entities: list[dict[str, Any]] = []
    blocked: list[tuple[int, int]] = []
    for term in sorted(set(terms), key=len, reverse=True):
        entity = scan_term(text, term, blocked_spans=blocked)
        if entity is None:
            continue
        if category_map is not None:
            entity["category"] = category_map.get(term)
        blocked.extend(entity.pop("spans"))
        entities.append(entity)
    return entities


def affirmed_terms(text: str, terms: Iterable[str]) -> list[str]:
    """The subset of ``terms`` positively asserted in ``text`` (order preserved)."""

    result: list[str] = []
    for term in terms:
        entity = scan_term(text, term)
        if entity and entity["polarity"] == POLARITY_AFFIRMED:
            result.append(term)
    return result


def is_affirmed(text: str, term: str) -> bool:
    entity = scan_term(text, term)
    return bool(entity and entity["polarity"] == POLARITY_AFFIRMED)
