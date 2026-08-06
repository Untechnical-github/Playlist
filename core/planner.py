import difflib
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import Track

# 表記ゆれの自動統合に使う類似度の閾値・最短文字数。誤爆を避けるため、ある程度長い名前
# （短い名前は1文字違いでも別アーティストの可能性が高いため対象外）にのみ適用する。
FUZZY_MATCH_THRESHOLD = 0.92
FUZZY_MATCH_MIN_LENGTH = 6


def normalize(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def _canon(name: str, alias_map: Dict[str, str]) -> str:
    """正規化した上で、artist_groups.json のエイリアス定義（表記ゆれの自動統合含む）が
    あれば代表グループ名に読み替える。"""
    key = normalize(name)
    return alias_map.get(key, key)


def _auto_merge_similar_names(tracks: List[Track], alias_map: Dict[str, str]) -> Dict[str, str]:
    """"Macaroni Empitsu" と "macaroni enpitsu" のような、ほぼ同一だが厳密には異なる表記を
    自動で同一アーティストとして統合する。artist_groups.json で明示的に定義済みのキーは
    （意図した振り分けを壊さないよう）対象から除外する。
    """
    raw_keys = set()
    for t in tracks:
        for a in t.artists:
            raw_keys.add(normalize(a))

    candidates = sorted(k for k in raw_keys if k not in alias_map)

    parent = {k: k for k in candidates}

    def find(k: str) -> str:
        while parent[k] != k:
            k = parent[k]
        return k

    for i in range(len(candidates)):
        a = candidates[i]
        if len(a) < FUZZY_MATCH_MIN_LENGTH:
            continue
        for j in range(i + 1, len(candidates)):
            b = candidates[j]
            if len(b) < FUZZY_MATCH_MIN_LENGTH:
                continue
            if difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY_MATCH_THRESHOLD:
                ra, rb = find(a), find(b)
                if ra != rb:
                    if ra < rb:
                        parent[rb] = ra
                    else:
                        parent[ra] = rb

    return {k: find(k) for k in candidates if find(k) != k}


def _is_katakana_only(text: str) -> bool:
    """カタカナ（と長音符・中点・空白・半角/全角の区別を無視した英数字）だけで構成されているか。
    漢字が混じっている場合は False（人名の読みは辞書変換だけでは正しく求められないため対象外）。
    """
    has_katakana = False
    for ch in text:
        if "゠" <= ch <= "ヿ":
            has_katakana = True
            continue
        if ch.isspace() or ch in "・-ー.":
            continue
        if ch.isascii():
            continue
        return False
    return has_katakana


def _to_romaji(text: str) -> str:
    try:
        import pykakasi
    except ImportError:
        return normalize(text)

    kks = pykakasi.kakasi()
    romaji = "".join(item["hepburn"] for item in kks.convert(text))
    return normalize(romaji)


def _auto_merge_transliterations(tracks: List[Track], alias_map: Dict[str, str]) -> Dict[str, str]:
    """"ヨルシカ"（カタカナ）と "Yorushika"（ローマ字）のような、カタカナ表記とその
    ローマ字表記を自動で同一アーティストとして統合する。漢字を含む表記は読みが一意に
    決まらないため対象外。artist_groups.json で明示的に定義済みのキーも対象外。
    """
    raw_by_norm: Dict[str, str] = {}
    for t in tracks:
        for a in t.artists:
            raw_by_norm.setdefault(normalize(a), a)

    candidates = {k: v for k, v in raw_by_norm.items() if k not in alias_map}
    if not candidates:
        return {}

    groups: Dict[str, List[str]] = {}
    for norm_key, raw in candidates.items():
        romaji_key = _to_romaji(raw) if _is_katakana_only(raw) else norm_key
        groups.setdefault(romaji_key, []).append(norm_key)

    merges: Dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        canonical = sorted(members)[0]
        for m in members:
            if m != canonical:
                merges[m] = canonical

    return merges


def _assign_buckets(tracks: List[Track], alias_map: Dict[str, str]) -> List[Optional[str]]:
    """曲ごとに所属させるアーティストのバケツ（正規化済みアーティスト名、またはエイリアス先の
    代表グループ名）を決める。位置（隣にどの曲があるか）は一切見ない。
    - 掲載されているアーティスト（コラボなら複数）のいずれかが、プレイリスト全体で本当に
      2曲以上ある（本物の複数曲アーティスト）なら、そのバケツに所属する。
    - 該当が無ければ、掲載アーティストの先頭（コラボなら1人目）のタグをそのままバケツとする
      （この場合は当然「単独曲」として扱われ、末尾行きになる）。
    - アーティスト情報が全く無い曲（UGC等）はバケツを持たない（None、末尾送り）。
    """
    tag_counts: Dict[str, int] = {}
    for t in tracks:
        for a in t.artists:
            key = _canon(a, alias_map)
            tag_counts[key] = tag_counts.get(key, 0) + 1

    buckets: List[Optional[str]] = [None] * len(tracks)
    for i, t in enumerate(tracks):
        if t.is_unknown:
            continue
        matched = next(
            (
                _canon(a, alias_map)
                for a in t.artists
                if tag_counts.get(_canon(a, alias_map), 0) >= 2
            ),
            None,
        )
        buckets[i] = matched if matched is not None else _canon(t.artists[0], alias_map)

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


def group_tracks(
    tracks: List[Track],
    alias_map: Optional[Dict[str, str]] = None,
    group_display: Optional[Dict[str, str]] = None,
) -> GroupedPlan:
    """曲が2曲以上あるアーティスト（塊）だけをアルファベット順にまとめる。
    塊の中の曲順は元の相対順を維持する（重要視していないため）。
    曲が1曲しかないアーティスト、およびバケツが決まらなかった曲（不明）は、
    塊とは別に tail へ、元の相対順のまま残す（再ソートしない）。

    alias_map / group_display は artist_groups.json（`core.aliases.load_artist_groups`）から
    読み込んだ、複数のアーティスト名を1つのグループとして扱うための対応表。
    """
    alias_map = dict(alias_map or {})
    group_display = dict(group_display or {})

    alias_map.update(_auto_merge_similar_names(tracks, alias_map))
    alias_map.update(_auto_merge_transliterations(tracks, alias_map))

    buckets = _assign_buckets(tracks, alias_map)

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
                block_display[b] = group_display.get(b) or next(
                    (a for a in t.artists if _canon(a, alias_map) == b),
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


def build_plan(
    tracks: List[Track],
    alias_map: Optional[Dict[str, str]] = None,
    group_display: Optional[Dict[str, str]] = None,
) -> List[Track]:
    return group_tracks(tracks, alias_map, group_display).flatten()
