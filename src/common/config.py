"""Configuration loading and path helpers."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: str | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_path(p: str | os.PathLike) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p
