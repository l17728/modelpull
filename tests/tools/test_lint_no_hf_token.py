"""Self-test for tools/lint_invariants.py::check_no_hf_token_in_executor (W3b)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_LINT_PATH = Path(__file__).resolve().parents[2] / "tools" / "lint_invariants.py"


def _load_lint():
    spec = importlib.util.spec_from_file_location("lint_invariants", _LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lint_flags_hf_token_in_executor(tmp_path, monkeypatch) -> None:
    lint = _load_lint()
    exec_dir = tmp_path / "src" / "dlw" / "executor"
    exec_dir.mkdir(parents=True)
    (exec_dir / "bad.py").write_text(
        "hf_token = 'leaked-secret'\n", encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)
    errors = lint.check_no_hf_token_in_executor()
    assert any("bad.py" in e for e in errors), errors


def test_lint_passes_clean_executor(tmp_path, monkeypatch) -> None:
    lint = _load_lint()
    exec_dir = tmp_path / "src" / "dlw" / "executor"
    exec_dir.mkdir(parents=True)
    (exec_dir / "ok.py").write_text(
        "# hf_token mentioned in a comment is fine\nx = 1\n", encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)
    assert lint.check_no_hf_token_in_executor() == []
