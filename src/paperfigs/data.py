"""集約済みデータの読み込みヘルパ。原稿(.qmd)はこれ経由でデータを読む。

- 既定では results/aggregated/(最新)を読む。
- 環境変数 SST_DATA_DIR を設定すると、そのディレクトリを読む。
  投稿時凍結(make freeze)後に SST_DATA_DIR=results/frozen/<tag> で
  レンダリングすれば、原稿を凍結データに固定できる(design-brief 要件8)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = REPO_ROOT / "results" / "aggregated"


def data_dir() -> Path:
    override = os.environ.get("SST_DATA_DIR")
    d = (REPO_ROOT / override).resolve() if override else DEFAULT_DIR
    if not d.is_dir():
        raise FileNotFoundError(
            f"集約データがない: {d} — make aggregate を先に実行する")
    return d


def load_iterations() -> pd.DataFrame:
    return pd.read_parquet(data_dir() / "iterations.parquet")


def load_progress() -> pd.DataFrame:
    return pd.read_parquet(data_dir() / "progress.parquet")


def load_runs() -> pd.DataFrame:
    return pd.read_csv(data_dir() / "runs.csv")


def load_meta() -> dict:
    return json.loads((data_dir() / "meta.json").read_text(encoding="utf-8"))
