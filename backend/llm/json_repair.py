from __future__ import annotations

import json
import re
from typing import Any


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_SINGLE_QUOTED_KEY_RE = re.compile(r"(?P<prefix>[{,]\s*)'(?P<key>[^'\\]*(?:\\.[^'\\]*)*)'\s*:")
_SINGLE_QUOTED_VALUE_RE = re.compile(r":\s*'(?P<value>[^'\\]*(?:\\.[^'\\]*)*)'(?P<suffix>\s*[,}])")


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas (``,]`` / ``,}``) that appear in JSON *structure* only.

    A naive ``re.sub(r",\\s*([}\\]])", ...)`` also rewrites the same sequence when it
    occurs *inside* a string value (e.g. "示例集合 {a, b, }"), silently corrupting
    clinical text that happens to contain a comma before a brace. This scanner tracks
    string state so only structural trailing commas are dropped.
    """

    out: list[str] = []
    in_string = False
    escape = False
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            i += 1
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
            out.append(char)
            i += 1
            continue
        if char == ",":
            # Look ahead past whitespace: a comma immediately before a closing bracket
            # (outside any string) is a structural trailing comma — skip it.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # drop the comma, keep the whitespace/bracket
                continue
        out.append(char)
        i += 1
    return "".join(out)


class JsonRepairError(ValueError):
    """Raised when a model response cannot be repaired into valid JSON."""


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _extract_balanced_json(text: str) -> str:
    starts = [idx for idx in (text.find("{"), text.find("[")) if idx != -1]
    if not starts:
        return text
    start = min(starts)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    quote = ""
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return text[start:]


def _quote_single_json(text: str) -> str:
    text = _SINGLE_QUOTED_KEY_RE.sub(lambda m: f'{m.group("prefix")}"{m.group("key")}":', text)
    text = _SINGLE_QUOTED_VALUE_RE.sub(lambda m: f': "{m.group("value")}"{m.group("suffix")}', text)
    return text


def repair_json_text(text: str) -> str:
    """Best-effort repair for common LLM JSON errors.

    This intentionally stays conservative: it handles markdown fences, prose around a
    JSON object, trailing commas, smart quotes, and simple single-quoted keys/values.
    It does not invent missing clinical fields.
    """

    candidate = _strip_code_fence(text)
    candidate = _extract_balanced_json(candidate)
    candidate = candidate.replace("\ufeff", "").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    candidate = _strip_trailing_commas(candidate)
    candidate = _quote_single_json(candidate)
    return candidate.strip()


def loads_with_repair(text: str) -> tuple[Any, dict[str, Any]]:
    """Parse JSON, repairing common model-output formatting issues.

    Returns the parsed object and metadata indicating whether repair was needed.
    """

    try:
        return json.loads(text), {"repaired": False, "strategy": "json.loads"}
    except json.JSONDecodeError as first_error:
        repaired = repair_json_text(text)
        try:
            return json.loads(repaired), {"repaired": True, "strategy": "fence_extract_trailing_comma_single_quote", "original_error": str(first_error)}
        except json.JSONDecodeError as second_error:
            raise JsonRepairError(f"Could not parse repaired JSON: {second_error}") from second_error
