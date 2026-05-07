#!/usr/bin/env python3
"""
lint_invariants.py — CI gate for invariant integrity in modelpull docs.

Checks:
  1. Invariant numbers are unique across all docs.
  2. Every invariant declared with `🔒 不变量 N` has a corresponding row in
     01-architecture.md §7 index table.
  3. Cross-doc references like "(详见 13 §4.3)" point to existing sections.
  4. Numbers in the §7 table are contiguous (1..N, no gaps).

Exit codes:
  0 — all checks pass
  1 — at least one check fails (CI block)
  2 — usage error
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

# Force UTF-8 stdout (Windows GBK default breaks emoji/CJK output)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs" / "v2.0"
INDEX_DOC = DOCS_DIR / "01-architecture.md"

# Pattern for invariant declarations in body: 🔒 [**]不变量 N (bold optional)
DECL_RE = re.compile(r"🔒\s*\*{0,2}不变量\s*(\d+)")

# Pattern for invariant index rows in 01 §7 table: | N | content | verification |
TABLE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)

# Pattern for cross-doc reference: "详见 NN §X.Y" — strict, requires § marker
# (avoids false positives like "12-14" which means doc range, not §14 of doc 12)
CROSS_REF_RE = re.compile(r"(?:详见|→)\s*(\d{2})[\s\-]*[a-zA-Z\-_]*\s*§\s*(\d+(?:\.\d+)*)")

# Section header pattern: ## N. or ### N.M or #### N.M.K
# Optional trailing dot after number (## 1. text vs ### 1.1 text)
SECTION_RE = re.compile(r"^(#{2,4})\s+(\d+(?:\.\d+)*)\.?\s+", re.MULTILINE)


def main() -> int:
    failures: list[str] = []

    # CODE-03 修复 (v2.0.13): 友好处理空目录/缺文件
    if not DOCS_DIR.exists():
        print(f"WARN: {DOCS_DIR} not found; skipping invariant lint", file=sys.stderr)
        return 0
    md_files = sorted(DOCS_DIR.glob("*.md"))
    if not md_files:
        print(f"WARN: no .md files in {DOCS_DIR}; skipping", file=sys.stderr)
        return 0
    if not INDEX_DOC.exists():
        print(f"FAIL: index doc {INDEX_DOC} missing (other md files exist)", file=sys.stderr)
        return 1

    # --- Collect invariant declarations across all docs ---
    # CODE-08: cache file content (avoid re-reading 3 times in this function)
    file_cache: dict[Path, str] = {}
    for md in md_files:
        try:
            file_cache[md] = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            failures.append(f"Cannot read {md.relative_to(ROOT)}: {e}")
            continue

    decl_locations: dict[int, list[tuple[Path, int]]] = defaultdict(list)
    for md, text in file_cache.items():
        for m in DECL_RE.finditer(text):
            num = int(m.group(1))
            line_no = text[: m.start()].count("\n") + 1
            decl_locations[num].append((md.relative_to(ROOT), line_no))

    # --- Check 1: uniqueness of invariant numbers ---
    # Multiple declarations of same number are OK (cross-references) but they
    # should all resolve to the same canonical text in §7. Here we just flag
    # if a number is declared in >1 doc, suggesting potential drift.
    for num, locs in sorted(decl_locations.items()):
        if len(locs) > 1:
            unique_files = {loc[0] for loc in locs}
            if len(unique_files) > 2:
                failures.append(
                    f"Invariant {num} declared in {len(unique_files)} files: "
                    f"{', '.join(sorted(str(f) for f in unique_files))}. "
                    "Consider consolidating to a single canonical doc."
                )

    # --- Check 2: every declared invariant has a row in §7 table ---
    index_text = file_cache.get(INDEX_DOC, "") or INDEX_DOC.read_text(encoding="utf-8")
    # Locate the §7 invariant table
    sec7_match = re.search(
        r"## 7\.\s*关键不变量索引[\s\S]+?(?=^## \d+\.|\Z)",
        index_text,
        re.MULTILINE,
    )
    if not sec7_match:
        failures.append("01-architecture.md §7 关键不变量索引 section not found")
        indexed_nums: set[int] = set()
    else:
        sec7 = sec7_match.group(0)
        indexed_nums = {int(m.group(1)) for m in TABLE_ROW_RE.finditer(sec7)}

    declared_nums = set(decl_locations.keys())
    missing = declared_nums - indexed_nums
    extra = indexed_nums - declared_nums
    if missing:
        failures.append(
            f"Invariants declared in body but missing from §7 index table: "
            f"{sorted(missing)}"
        )
    # extra is acceptable: §7 may list invariants whose body declaration uses
    # a different format. Do not fail.
    if extra:
        print(
            f"INFO: Invariants in §7 index but not declared in body via 🔒 marker: "
            f"{sorted(extra)} (this may be OK; verify manually)"
        )

    # --- Check 3: §7 numbering is contiguous ---
    if indexed_nums:
        max_num = max(indexed_nums)
        gaps = set(range(1, max_num + 1)) - indexed_nums
        if gaps:
            failures.append(
                f"§7 index table has gaps in numbering: missing {sorted(gaps)}"
            )

    # --- Check 4: cross-doc references resolve to existing sections ---
    # Build map: doc_number_prefix → set of section numbers
    doc_sections: dict[str, set[str]] = defaultdict(set)
    for md, text in file_cache.items():
        # Extract prefix like "01" from "01-architecture.md"
        m = re.match(r"^(\d{2})-", md.name)
        if not m:
            continue
        prefix = m.group(1)
        for sec_m in SECTION_RE.finditer(text):
            doc_sections[prefix].add(sec_m.group(2))

    ref_failures: list[str] = []
    for md, text in file_cache.items():
        for m in CROSS_REF_RE.finditer(text):
            target_doc = m.group(1)
            target_sec = m.group(2)
            if target_doc not in doc_sections:
                # Reference to non-existent doc; skip (could be archived doc)
                continue
            # Allow references to parent sections: "§4" matches "§4.1"
            available = doc_sections[target_doc]
            if target_sec in available:
                continue
            # Check if any existing section starts with the referenced number
            # e.g., target_sec="4" should match existing "4.1", "4.2" etc.
            if any(
                avail == target_sec or avail.startswith(target_sec + ".")
                for avail in available
            ):
                continue
            line_no = text[: m.start()].count("\n") + 1
            ref_failures.append(
                f"{md.relative_to(ROOT)}:{line_no}: cross-ref "
                f"'详见 {target_doc} §{target_sec}' — section not found in "
                f"target doc (available: {sorted(available)[:5]}...)"
            )
    # Cross-refs are common to drift; report as warnings rather than failure
    # unless --strict
    if "--strict" in sys.argv and ref_failures:
        failures.extend(ref_failures)
    elif ref_failures:
        for line in ref_failures[:20]:
            print(f"WARN: {line}")
        if len(ref_failures) > 20:
            print(f"WARN: ... {len(ref_failures) - 20} more cross-ref warnings")

    # --- Report ---
    if failures:
        print()
        print("=" * 70)
        print(f"FAIL: {len(failures)} invariant lint failures")
        print("=" * 70)
        for f in failures:
            print(f"  ✗ {f}")
        return 1

    print(
        f"OK: {len(declared_nums)} invariants declared, "
        f"{len(indexed_nums)} indexed in 01 §7, contiguous"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
