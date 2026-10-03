"""Configuration loading.

Order (later wins): config/default.yaml < config/<profile>.yaml (SCALPER_PROFILE, e.g. "mobile")
< local.yaml < SCALPER_* environment variables. local.yaml lives in SCALPER_LOCAL_CONFIG_DIR when
set (a writable location, e.g. the Android app's files dir), otherwise next to default.yaml.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("SCALPER_CONFIG_DIR", REPO_ROOT / "config"))


def local_config_path() -> Path:
    return Path(os.environ.get("SCALPER_LOCAL_CONFIG_DIR", CONFIG_DIR)) / "local.yaml"


class Cfg(dict):
    """dict with attribute access for nested config sections."""

    def __getattr__(self, name: str) -> Any:
        try:
            v = self[name]
        except KeyError as e:
            raise AttributeError(name) from e
        return Cfg(v) if isinstance(v, dict) and not isinstance(v, Cfg) else v

    def get_path(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def load_config(extra: dict | None = None) -> Cfg:
    data: dict = {}
    files = [CONFIG_DIR / "default.yaml"]
    profile = os.environ.get("SCALPER_PROFILE")
    if profile:
        files.append(CONFIG_DIR / f"{profile}.yaml")
    files.append(local_config_path())
    for p in files:
        if p.exists():
            data = _merge(data, yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {})
    for key, raw in os.environ.items():
        if not key.startswith("SCALPER_") or "__" not in key:
            continue
        path = key[len("SCALPER_"):].lower().split("__")
        cur = data
        for part in path[:-1]:
            cur = cur.setdefault(part, {})
        cur[path[-1]] = _coerce(raw)
    if extra:
        data = _merge(data, extra)
    return Cfg(data)


def save_local_settings(updates: dict[str, Any]) -> Path:
    """Merge dotted-key updates (e.g. {"risk.equity": 5000}) into local.yaml and return its path."""
    p = local_config_path()
    data = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {} if p.exists() else {}
    for dotted, value in updates.items():
        cur = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = cur[part] = {}
            cur = nxt
        cur[parts[-1]] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p


def data_dir(cfg: Cfg) -> Path:
    p = Path(cfg.get_path("storage.data_dir", "data"))
    if not p.is_absolute():
        p = REPO_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p
