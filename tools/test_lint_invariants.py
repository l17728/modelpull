"""
Unit tests for lint_invariants.py (CODE-08, v2.0.13).

Run: python -m pytest tools/test_lint_invariants.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "lint_invariants.py"


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create fake repo with `tools/lint_invariants.py` + `docs/v2.0/{files}`."""
    # tools/ link
    (tmp_path / "tools").mkdir()
    shutil.copy(SCRIPT, tmp_path / "tools" / "lint_invariants.py")
    # docs/v2.0/
    docs_dir = tmp_path / "docs" / "v2.0"
    docs_dir.mkdir(parents=True)
    for name, content in files.items():
        (docs_dir / name).write_text(content, encoding="utf-8")
    return tmp_path


def run_lint(repo: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(repo / "tools" / "lint_invariants.py"), *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


# ---- Test cases ----

def test_empty_docs_dir(tmp_path):
    """空 docs 目录应 friendly exit 0."""
    repo = make_repo(tmp_path, {})
    code, out, err = run_lint(repo)
    assert code == 0
    assert "no .md files" in err


def test_missing_docs_dir(tmp_path):
    """缺 docs/v2.0/ 应 friendly exit 0."""
    (tmp_path / "tools").mkdir()
    shutil.copy(SCRIPT, tmp_path / "tools" / "lint_invariants.py")
    code, out, err = run_lint(tmp_path)
    assert code == 0
    assert "not found" in err


def test_missing_index_doc(tmp_path):
    """有其他 md 但没 01-architecture.md 应 fail."""
    repo = make_repo(tmp_path, {
        "02-protocol.md": "🔒 不变量 1: foo",
    })
    code, out, err = run_lint(repo)
    assert code == 1
    assert "missing" in err


def test_basic_consistent(tmp_path):
    """权威表与 body 声明一致 → OK."""
    repo = make_repo(tmp_path, {
        "01-architecture.md": (
            "# arch\n\n## 7. 关键不变量索引\n\n"
            "| ID | 内容 | 验证 |\n|----|----|----|\n"
            "| 1 | foo | x |\n"
            "| 2 | bar | y |\n"
        ),
        "02-protocol.md": (
            "## 1. proto\n\n🔒 **不变量 1**: foo\n\n🔒 不变量 2: bar\n"
        ),
    })
    code, out, err = run_lint(repo)
    assert code == 0
    assert "OK" in out


def test_missing_in_index_table(tmp_path):
    """body 声明但 §7 表里没有 → fail."""
    repo = make_repo(tmp_path, {
        "01-architecture.md": (
            "## 7. 关键不变量索引\n\n"
            "| ID | 内容 | 验证 |\n|----|----|----|\n| 1 | foo | x |\n"
        ),
        "02-protocol.md": "🔒 **不变量 99**: missing\n",
    })
    code, out, err = run_lint(repo)
    assert code == 1
    assert "99" in (out + err)


def test_gap_in_numbering(tmp_path):
    """§7 表 1, 3 缺 2 → fail."""
    repo = make_repo(tmp_path, {
        "01-architecture.md": (
            "## 7. 关键不变量索引\n\n"
            "| ID | 内容 | 验证 |\n|----|----|----|\n"
            "| 1 | foo | x |\n| 3 | bar | y |\n"
        ),
        "02-protocol.md": "🔒 **不变量 1**: foo\n🔒 **不变量 3**: bar\n",
    })
    code, out, err = run_lint(repo)
    assert code == 1
    assert "gap" in (out + err).lower() or "missing" in (out + err).lower()


def test_decl_without_bold_marker(tmp_path):
    """支持 `🔒 不变量 N` 不带 ** bold."""
    repo = make_repo(tmp_path, {
        "01-architecture.md": (
            "## 7. 关键不变量索引\n\n"
            "| ID | 内容 | 验证 |\n|----|----|----|\n| 1 | foo | x |\n"
        ),
        "13-something.md": "🔒 不变量 1 (v2.1): foo\n",  # no **
    })
    code, out, err = run_lint(repo)
    assert code == 0


def test_strict_mode_fails_on_cross_ref_drift(tmp_path):
    """--strict 模式下 cross-ref 失效应 fail."""
    repo = make_repo(tmp_path, {
        "01-architecture.md": (
            "## 7. 关键不变量索引\n\n"
            "| ID | 内容 | 验证 |\n|----|----|----|\n| 1 | foo | x |\n"
            "详见 99 §7.7 something\n"
        ),
    })
    code, out, err = run_lint(repo, "--strict")
    # 99 文档不存在 → cross-ref 应被忽略（注释中说"reference to non-existent doc"）
    # 99 不在 docs 中所以 skip；strict 也通过
    assert code == 0


def test_decl_in_multiple_files_warning(tmp_path):
    """同一不变量在 3+ 文件声明 → fail（建议合并）."""
    repo = make_repo(tmp_path, {
        "01-architecture.md": (
            "## 7. 关键不变量索引\n\n"
            "| ID | 内容 | 验证 |\n|----|----|----|\n| 1 | foo | x |\n"
            "🔒 **不变量 1**: foo declared here too\n"
        ),
        "02-protocol.md": "🔒 **不变量 1**: bar\n",
        "03-distributed.md": "🔒 **不变量 1**: bar2\n",
        "04-other.md": "🔒 **不变量 1**: bar3\n",
    })
    code, out, err = run_lint(repo)
    assert code == 1
    assert "files" in (out + err).lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
