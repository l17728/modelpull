"""Resolve effective server + token (flag > env > config > default)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from dlw.sdk.errors import UsageError

_DEFAULT_SERVER = "http://localhost:8000"


def _config_key() -> str | None:
    return os.environ.get("DLW_CONFIG_KEY") or None


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


def load_config(config_path: str | None = None) -> dict:
    """Public read alias of _load_config (so the CLI doesn't use the private name)."""
    return _load_config(config_path)


def _resolve_write_path(config_path: str | None) -> Path:
    if config_path:
        return Path(config_path)
    env = os.environ.get("DLW_CONFIG")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "dlw" / "config.yaml"
    return Path.home() / ".dlw" / "config.yaml"


def save_config(cfg: dict, *, config_path: str | None = None) -> Path:
    p = _resolve_write_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=True), encoding="utf-8")
    try:
        p.chmod(0o600)        # best-effort; no-op on Windows
    except OSError:
        pass
    return p


def set_context(name: str, *, server: str | None = None, token: str | None = None,
                make_current: bool = True, config_path: str | None = None) -> Path:
    cfg = _load_config(config_path) or {}
    cfg.setdefault("contexts", {}).setdefault(name, {})
    if server is not None:
        cfg["contexts"][name]["server"] = server.rstrip("/")
    if token is not None:
        from dlw.sdk._crypto import encrypt_token
        key = _config_key()
        stored = encrypt_token(token, key) if key else token
        cfg.setdefault("auth", {}).setdefault(name, {})["access_token"] = stored
    if make_current:
        cfg["current_context"] = name
    return save_config(cfg, config_path=config_path)


def use_context(name: str, *, config_path: str | None = None) -> Path:
    cfg = _load_config(config_path) or {}
    if name not in (cfg.get("contexts") or {}):
        raise UsageError(f"no such context: {name}")
    cfg["current_context"] = name
    return save_config(cfg, config_path=config_path)


def resolve_server(*, server: str | None = None,
                   config_path: str | None = None) -> str:
    """Server precedence only (no token required): flag > DLW_SERVER > current-context server > default."""
    cfg = _load_config(config_path)
    cur = cfg.get("current_context")
    ctx = ((cfg.get("contexts") or {}).get(cur) or {}) if cur else {}
    srv = (server or os.environ.get("DLW_SERVER")
           or ctx.get("server") or _DEFAULT_SERVER)
    return str(srv).rstrip("/")


def clear_token(name: str, *, config_path: str | None = None) -> Path:
    """Remove auth.<name>.access_token from the config (gracefully if absent)."""
    cfg = _load_config(config_path) or {}
    cfg.get("auth", {}).get(name, {}).pop("access_token", None)
    return save_config(cfg, config_path=config_path)


def _split(key: str) -> list[str]:
    return [p for p in key.split(".") if p]


def get_config_value(key: str, *, config_path: str | None = None):
    cur = _load_config(config_path) or {}
    for p in _split(key):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def set_config_value(key: str, value, *, config_path: str | None = None) -> Path:
    cfg = _load_config(config_path) or {}
    parts = _split(key)
    cur = cfg
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    if parts[-1] == "access_token" and value is not None:
        from dlw.sdk._crypto import encrypt_token
        key = _config_key()
        if key and not str(value).startswith("enc:v1:"):
            value = encrypt_token(str(value), key)
    cur[parts[-1]] = value
    return save_config(cfg, config_path=config_path)


def unset_config_value(key: str, *, config_path: str | None = None) -> bool:
    cfg = _load_config(config_path) or {}
    parts = _split(key)
    cur = cfg
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return False
    existed = isinstance(cur, dict) and parts[-1] in cur
    if existed:
        del cur[parts[-1]]
        save_config(cfg, config_path=config_path)
    return existed


def flatten_config(cfg: dict) -> dict:
    out: dict = {}

    def _walk(prefix: str, node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(f"{prefix}.{k}" if prefix else k, v)
        else:
            out[prefix] = node

    _walk("", cfg)
    return out


def get_default(name: str, *, config_path: str | None = None):
    return get_config_value(f"defaults.{name}", config_path=config_path)


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
    if tok and token is None and os.environ.get("DLW_TOKEN") is None \
            and os.environ.get("DLW_SYSTEM_ADMIN_TOKEN") is None:
        from dlw.sdk._crypto import decrypt_token, is_encrypted
        from dlw.sdk.errors import TokenDecryptError
        if is_encrypted(tok):
            key = _config_key()
            if not key:
                raise UsageError("stored token is encrypted but "
                                 "DLW_CONFIG_KEY is not set")
            try:
                tok = decrypt_token(tok, key)
            except TokenDecryptError as e:
                raise UsageError("cannot decrypt stored token: wrong "
                                 "DLW_CONFIG_KEY?") from e
    if not tok:
        raise UsageError(
            "no API token: pass --token or set DLW_TOKEN / "
            "DLW_SYSTEM_ADMIN_TOKEN (or configure ~/.dlw/config.yaml)")
    return Resolved(server=str(srv).rstrip("/"), token=str(tok))
