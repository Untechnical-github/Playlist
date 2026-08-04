import json
from pathlib import Path
from typing import Dict, Tuple

from .planner import normalize

ALIASES_FILE = Path("artist_groups.json")


def load_artist_groups(playlist_id: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """artist_groups.json から、指定プレイリスト向けのアーティストグループ定義を読み込む。
    - alias_map: 正規化済みのアーティスト名 -> 正規化済みの代表グループ名
    - display: 正規化済みの代表グループ名 -> 表示用の名前（JSONに書いた通りの表記）
    ファイルが無い、または対象プレイリストの定義が無い場合は空の対応表を返す。
    """
    if not ALIASES_FILE.exists():
        return {}, {}

    data = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    groups = data.get(playlist_id, {})

    alias_map: Dict[str, str] = {}
    display: Dict[str, str] = {}
    for canonical, members in groups.items():
        canonical_norm = normalize(canonical)
        display[canonical_norm] = canonical
        alias_map[canonical_norm] = canonical_norm
        for member in members:
            alias_map[normalize(member)] = canonical_norm

    return alias_map, display
