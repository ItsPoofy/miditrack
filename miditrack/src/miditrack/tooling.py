"""外部ツール呼び出しで共有する小さな検証ユーティリティ。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def safe_subprocess_run(argv, **kwargs) -> subprocess.CompletedProcess:
    """Run subprocess with CREATE_NO_WINDOW on Windows to prevent console flashing."""
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, **kwargs)


def resolve_resource_root(module_path: str) -> Path:
    """環境変数またはモジュール位置からリポジトリルートを解決する。"""
    configured_root = os.environ.get("MIDITRACK_RESOURCE_ROOT")
    if configured_root:
        return Path(configured_root)
    return Path(module_path).resolve().parents[3]


def is_executable_file(path: str | Path) -> bool:
    """pathが実行可能な通常ファイルかを返す。"""
    candidate_path = Path(path)
    if not candidate_path.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(candidate_path, os.X_OK)


def has_wave_audio(path: Path) -> bool:
    """pathが44バイトのWAVヘッダーを超えるデータを持つかを返す。"""
    return path.exists() and path.stat().st_size > 44


def stderr_tail(stderr: str, line_count: int = 20) -> str:
    """外部コマンドの標準エラー末尾をユーザー向け表示用に整形する。"""
    return "\n".join(stderr.strip().splitlines()[-line_count:])
