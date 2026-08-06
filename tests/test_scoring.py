import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scoring import composite_score, normalize_popularity, normalize_view_counts


def test_normalize_view_counts_maps_min_and_max_to_0_and_1():
    scores = normalize_view_counts([100, 1_000_000, 10])
    assert scores[2] == 0.0  # 最小 (10)
    assert scores[1] == 1.0  # 最大 (1,000,000)
    assert 0.0 < scores[0] < 1.0


def test_normalize_view_counts_log_scale_gives_meaningful_spread_for_skewed_data():
    # 素の値でmin-max正規化すると 1,000,000 以外はほぼ0になってしまうが、
    # log scaleなら中間の値にもある程度の差がつく
    scores = normalize_view_counts([1, 1_000, 1_000_000])
    assert scores[1] > 0.3


def test_normalize_view_counts_handles_empty_list():
    assert normalize_view_counts([]) == []


def test_normalize_view_counts_handles_all_same_value():
    assert normalize_view_counts([500, 500, 500]) == [1.0, 1.0, 1.0]
    assert normalize_view_counts([0, 0, 0]) == [0.0, 0.0, 0.0]


def test_normalize_popularity_scales_0_100_to_0_1():
    assert normalize_popularity(0) == 0.0
    assert normalize_popularity(100) == 1.0
    assert normalize_popularity(50) == 0.5


def test_normalize_popularity_clamps_out_of_range_values():
    assert normalize_popularity(-10) == 0.0
    assert normalize_popularity(150) == 1.0


def test_composite_score_is_weighted_average():
    assert composite_score(1.0, 0.0, youtube_weight=1.0, spotify_weight=1.0) == 0.5
    assert composite_score(1.0, 0.0, youtube_weight=3.0, spotify_weight=1.0) == 0.75
    assert composite_score(0.0, 1.0, youtube_weight=0.0, spotify_weight=1.0) == 1.0


def test_composite_score_handles_zero_total_weight():
    assert composite_score(1.0, 1.0, youtube_weight=0.0, spotify_weight=0.0) == 0.0
