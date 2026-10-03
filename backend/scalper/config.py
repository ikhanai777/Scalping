"""Configuration loading: config/default.yaml < config/local.yaml < SCALPER_* env vars."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("SCALPER_CONFIG_DIR", REPO_ROOT / "config"))


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
    for name in ("default.yaml", "local.yaml"):
        p = CONFIG_DIR / name
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


def data_dir(cfg: Cfg) -> Path:
    p = Path(cfg.get_path("storage.data_dir", "data"))
    if not p.is_absolute():
        p = REPO_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p
