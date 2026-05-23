"""SP4e follow-on: choke-point helpers that apply sanitize_external to declared
tool-result field paths. NO idempotency check — always sanitize. Pre-review B1:
a `startswith("<external_content")` check is an attacker bypass because
sanitize_external does NOT escape `<` in body content; a forged prefix in
attacker-controlled input would skip sanitization. Double-wrap is safe because
the OUTER wrap is the boundary the LLM treats as the trust boundary."""
from __future__ import annotations

from dlw.ai.sanitize import sanitize_external


def _sanitize_leaf(node: dict, key: str, *, source: str) -> None:
    val = node.get(key)
    if not isinstance(val, str) or not val:
        return
    node[key] = sanitize_external(val, source=source).text


def _apply_one(node, parts: list[str], *, source: str) -> None:
    if not parts or not isinstance(node, dict):
        return
    head = parts[0]
    rest = parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        items = node.get(key)
        if not isinstance(items, list):
            return
        for item in items:
            _apply_one(item, rest, source=source)
    elif rest:
        nxt = node.get(head)
        if isinstance(nxt, dict):
            _apply_one(nxt, rest, source=source)
    else:
        _sanitize_leaf(node, head, source=source)


def apply_external_fields(
    result: dict, paths: list[str], *, source: str,
) -> None:
    """In-place. Walks each dotted path (with `[]` list-iteration suffix) and
    applies sanitize_external to the leaf string. Path syntax:
      "field"           — top-level string field
      "field[].nested"  — iterate items in `field` (list of dicts), sanitize
                          nested string on each
    Permissive: missing keys, wrong types, empty lists → silent no-op."""
    if not isinstance(result, dict):
        return
    for p in paths:
        _apply_one(result, p.split("."), source=source)


def sanitize_error_key(result: dict, *, source: str) -> None:
    """Pre-review I2: tools like _hf_api_metadata return
    {"error": f"hf_network: {e}"} where `e` carries external content.
    Sanitize the `error` key unconditionally if present."""
    if not isinstance(result, dict):
        return
    _sanitize_leaf(result, "error", source=source)
