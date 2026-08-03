import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import Track


def normalize(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def _assign_buckets(tracks: List[Track]) -> List[Optional[str]]:
    """曲ごとに所属させるアーティストのバケツ（正規化済みアーティスト名）を決める。
    - アーティストが1人だけで明確な曲は、そのアーティスト名で確定。
    - コラボ曲は、現在隣接している曲のバケツが自分のアーティストのいずれかと一致すればそこに所属
      （＝手動で特定アーティストの棚に置いた位置を尊重する）。一致しなければ先頭アーティストで確定。
    - アーティスト情報が無い曲（UGC等）は、隣接している曲のバケツにそのまま所属する。
      どちらの隣にも確定したバケツが無ければ None（＝末尾送りの「不明」扱い）のまま。
    """
    n = len(tracks)
    buckets: List[Optional[str]] = [None] * n
    ambiguous_idx: List[int] = []
    unknown_idx: List[int] = []

    for i, t in enumerate(tracks):
        if len(t.artists) == 1 and not t.is_unknown:
            buckets[i] = normalize(t.artists[0])
        elif t.artists:
            ambiguous_idx.append(i)
        else:
            unknown_idx.append(i)

    changed = True
    while changed:
        changed = False
        for i in ambiguous_idx:
            if buckets[i] is not None:
                continue
            keys = {normalize(a) for a in tracks[i].artists}
            for j in (i - 1, i + 1):
                if 0 <= j < n and buckets[j] in keys:
                    buckets[i] = buckets[j]
                    changed = True
                    break
        for i in unknown_idx:
            if buckets[i] is not None:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < n and buckets[j] is not None:
                    buckets[i] = buckets[j]
                    changed = True
                    break

    for i in ambiguous_idx:
        if buckets[i] is None:
            buckets[i] = normalize(tracks[i].artists[0])

    return buckets


@dataclass
class GroupedPlan:
    blocks: List[Tuple[str, List[Track]]]
    """(表示用アーティスト名, 曲リスト) のアルファベット順の列。塊（2曲以上）のみ含む。"""
    tail: List[Track]
    """単独曲・不明曲。元の相対順のまま。"""

    def flatten(self) -> List[Track]:
        result: List[Track] = []
        for _, tracks in self.blocks:
            result.extend(tracks)
        result.extend(self.tail)
        return result


def group_tracks(tracks: List[Track]) -> GroupedPlan:
    """曲が2曲以上あるアーティスト（塊）だけをアルファベット順にまとめる。
    塊の中の曲順は元の相対順を維持する（重要視していないため）。
    曲が1曲しかないアーティスト、およびバケツが決まらなかった曲（不明）は、
    塊とは別に tail へ、元の相対順のまま残す（再ソートしない）。
    """
    buckets = _assign_buckets(tracks)

    counts: Dict[str, int] = {}
    for b in buckets:
        if b is not None:
            counts[b] = counts.get(b, 0) + 1

    block_groups: Dict[str, List[Track]] = {}
    block_display: Dict[str, str] = {}
    block_order: List[str] = []
    tail: List[Track] = []

    for t, b in zip(tracks, buckets):
        if b is not None and counts[b] >= 2:
            if b not in block_groups:
                block_groups[b] = []
                block_order.append(b)
                block_display[b] = next(
                    (a for a in t.artists if normalize(a) == b),
                    t.artists[0] if t.artists else b,
                )
            block_groups[b].append(t)
        else:
            tail.append(t)

    block_order.sort()

    return GroupedPlan(
        blocks=[(block_display[b], block_groups[b]) for b in block_order],
        tail=tail,
    )


def build_plan(tracks: List[Track]) -> List[Track]:
    return group_tracks(tracks).flatten()
