import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TrackScore:
    title: str
    artist: str
    video_id: Optional[str]
    view_count: int
    score: float


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
