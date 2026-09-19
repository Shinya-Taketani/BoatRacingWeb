"""Laravel側(.env)のPostgreSQL接続情報を再利用するための薄い接続ヘルパー。

ml側に接続情報を複製したくないため、リポジトリルートの .env を読み、
DB_* を環境変数として利用する（すでにエクスポート済みの環境変数を優先する）。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg


def _find_repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists() and (parent / "artisan").exists():
            return parent
    return None


def _load_root_env() -> dict[str, str]:
    root = _find_repo_root()
    if root is None:
        return {}

    values: dict[str, str] = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_connection() -> psycopg.Connection:
    """Laravelの.envと同じDB_*を使ってPostgreSQLに接続する。"""
    env = _load_root_env()

    def var(name: str, default: str | None = None) -> str:
        value = os.environ.get(name, env.get(name, default))
        if value is None:
            raise RuntimeError(f"{name} is not set (checked env vars and repo root .env)")
        return value

    return psycopg.connect(
        host=var("DB_HOST", "127.0.0.1"),
        port=var("DB_PORT", "5432"),
        dbname=var("DB_DATABASE"),
        user=var("DB_USERNAME"),
        password=var("DB_PASSWORD"),
    )
