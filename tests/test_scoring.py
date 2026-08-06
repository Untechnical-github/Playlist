import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scoring import normalize_view_counts


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
