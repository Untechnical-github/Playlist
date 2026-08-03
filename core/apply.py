from typing import List, Tuple

from .models import Track
from .youtube_api import set_item_position

PositionUpdate = Tuple[Track, int]


def compute_position_updates(current: List[Track], target: List[Track]) -> List[PositionUpdate]:
    """current を target の順序にするための (曲, 目標position) の列を返す。
    先頭から確定させていくことで、すでに正しい位置にある曲への更新は省略される。
    """
    working = [t.item_id for t in current]
    updates: List[PositionUpdate] = []

    for i, t in enumerate(target):
        if working[i] == t.item_id:
            continue
        idx = working.index(t.item_id)
        working.pop(idx)
        working.insert(i, t.item_id)
        updates.append((t, i))

    return updates


def apply_updates(auth_header: str, playlist_id: str, updates: List[PositionUpdate]) -> None:
    for track, position in updates:
        set_item_position(auth_header, playlist_id, track.item_id, track.video_id, position)
