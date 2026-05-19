"""Resolve effective server + token (flag > env > config > default)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from dlw.sdk.errors import UsageError

_DEFAULT_SERVER = "http://localhost:8000"


@dataclass(frozen=True)
class Resolved:
    server: str
    token: str


def _load_config(config_path: str | None) -> dict:
    # An explicit empty string means "do not read any config file".
    if config_path == "":
        return {}
    candidates: list[Path] = []
    explicit = config_path or os.environ.get("DLW_CONFIG")
    if explicit:
        candidates.append(Path(explicit))
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            candidates.append(Path(xdg) / "dlw" / "config.yaml")
        candidates.append(Path.home() / ".dlw" / "config.yaml")
    for c in candidates:
        try:
            if c.is_file():
                return yaml.safe_load(c.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
    return {}


def resolve(*, server: str | None, token: str | None,
            config_path: str | None = None) -> Resolved:
    cfg = _load_config(config_path)
    cur = cfg.get("current_context")
    ctx = ((cfg.get("contexts") or {}).get(cur) or {}) if cur else {}
    auth = ((cfg.get("auth") or {}).get(cur) or {}) if cur else {}

    srv = (server or os.environ.get("DLW_SERVER")
           or ctx.get("server") or _DEFAULT_SERVER)
    tok = (token or os.environ.get("DLW_TOKEN")
           or os.environ.get("DLW_SYSTEM_ADMIN_TOKEN")
           or auth.get("access_token"))
    if not tok:
        raise UsageError(
            "no API token: pass --token or set DLW_TOKEN / "
            "DLW_SYSTEM_ADMIN_TOKEN (or configure ~/.dlw/config.yaml)")
    return Resolved(server=str(srv).rstrip("/"), token=str(tok))
