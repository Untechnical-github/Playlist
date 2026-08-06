import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TrackScore:
    title: str
    artist: str
    video_id: Optional[str]
    view_count: int
    spotify_id: Optional[str]
    popularity: int
    youtube_score: float
    spotify_score: float
    composite_score: float


def normalize_view_counts(view_counts: List[int]) -> List[float]:
    """再生回数を0-1に正規化する。再生回数は桁違いに差が出る（対数正規分布に近い）ため、
    素の値でmin-max正規化すると最上位の1曲以外がほぼ0になってしまう。
    log(views + 1) を取ってからmin-max正規化することで、より滑らかな分布にする。
    """
    if not view_counts:
        return []

    logs = [math.log1p(max(v, 0)) for v in view_counts]
    lo, hi = min(logs), max(logs)
    if hi == lo:
        return [1.0 if hi > 0 else 0.0 for _ in logs]
    return [(x - lo) / (hi - lo) for x in logs]


def normalize_popularity(popularity: int) -> float:
    """Spotifyのpopularityは元々0-100のスコアなので、そのまま0-1にスケールするだけでよい。"""
    return max(0, min(popularity, 100)) / 100.0


def composite_score(youtube_score: float, spotify_score: float, youtube_weight: float, spotify_weight: float) -> float:
    total_weight = youtube_weight + spotify_weight
    if total_weight <= 0:
        return 0.0
    return (youtube_weight * youtube_score + spotify_weight * spotify_score) / total_weight
