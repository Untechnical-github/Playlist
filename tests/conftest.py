import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import score_playlist


@pytest.fixture(autouse=True)
def _reset_daily_quota_exceeded_flag():
    """score_playlist._daily_quota_exceeded_seen はプロセス全体で共有されるモジュール変数
    （1回のGitHub Actions実行=1プロセスの中で状態を持ち回すための設計）。テストプロセスは
    全テストを1つのPythonプロセス内で実行するため、あるテストが立てたフラグが後続の無関係な
    テストに漏れないよう、テストごとにリセットする。
    """
    score_playlist._daily_quota_exceeded_seen = False
    yield
    score_playlist._daily_quota_exceeded_seen = False
