import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.apply import compute_position_updates
from core.models import Track
from core.planner import build_plan


def t(video_id, artists, title=None, video_type="MUSIC_VIDEO_TYPE_ATV"):
    return Track(
        video_id=video_id,
        item_id=f"item_{video_id}",
        title=title or video_id,
        artists=artists,
        video_type=video_type,
    )


def test_multi_track_artists_are_merged_into_a_single_block_even_if_split_across_the_playlist():
    tracks = [
        t("1", ["Eve"]),
        t("2", ["sumika"]),
        t("3", ["Bump"]),
        t("4", ["Eve"]),
        t("5", ["sumika"]),
    ]
    result = build_plan(tracks)
    # Eve と sumika は2曲ずつあるので塊としてアルファベット順（Eve, sumika）に前方へ。
    # Bump は1曲だけなので塊にはならず、末尾（元の相対位置のまま）に残る。
    assert [x.video_id for x in result] == ["1", "4", "2", "5", "3"]


def test_single_track_artists_stay_in_original_relative_order_at_the_end():
    tracks = [
        t("1", ["Zard"]),  # single
        t("2", ["Aiko"]),  # block
        t("3", ["Bump"]),  # single
        t("4", ["Aiko"]),  # block
    ]
    result = build_plan(tracks)
    # 塊(Aiko)が前方に、単独曲(Zard, Bump)は再ソートされず元の相対順のまま末尾に残る
    assert [x.video_id for x in result] == ["2", "4", "1", "3"]


def test_block_internal_order_preserves_original_relative_order():
    tracks = [t("1", ["Eve"]), t("2", ["Bump"]), t("3", ["Eve"])]
    result = build_plan(tracks)
    assert [x.video_id for x in result] == ["1", "3", "2"]


def test_collab_track_follows_the_artist_block_it_is_currently_adjacent_to():
    tracks = [
        t("1", ["Aiko"]),
        t("2", ["Aiko", "Bump"]),  # Aiko の隣にあるので Aiko の塊に入る
        t("3", ["Bump"]),
    ]
    result = build_plan(tracks)
    # Aiko+コラボ曲で2曲の塊になるので前方へ、Bump は単独曲として末尾に残る
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_collab_track_with_no_matching_neighbor_falls_back_to_first_listed_artist():
    tracks = [t("1", ["Zard", "Bump"]), t("2", ["Aiko"])]
    result = build_plan(tracks)
    # 隣接する Aiko とは一致しないので先頭アーティスト Zard で確定するが、どちらも単独曲なので
    # 塊は生まれず、元の相対順のまま
    assert [x.video_id for x in result] == ["1", "2"]


def test_unknown_ugc_track_follows_its_current_neighbor_and_can_form_a_block():
    known_a = t("1", ["Aiko"])
    ugc = t("2", [], video_type="MUSIC_VIDEO_TYPE_UGC")
    known_b = t("3", ["Bump"])
    result = build_plan([known_a, ugc, known_b])
    # ugc は Aiko の隣にいたので Aiko の塊に入り2曲になる。Bump は単独曲として末尾。
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_fully_isolated_unknown_track_goes_to_the_end():
    ugc = t("1", [], video_type="MUSIC_VIDEO_TYPE_UGC")
    result = build_plan([ugc])
    assert [x.video_id for x in result] == ["1"]


def test_unknown_track_next_to_a_single_track_does_not_form_a_block():
    tracks = [t("1", ["Bump"]), t("2", [], video_type="MUSIC_VIDEO_TYPE_UGC"), t("3", ["Aiko"])]
    result = build_plan(tracks)
    # ugc(2) は前方の Bump に付くが、Bump+ugc も Aiko も1曲ずつなので塊にはならず、
    # 元の相対順のまま末尾に残る
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_case_insensitive_artist_names_are_merged_into_a_block():
    tracks = [t("1", ["RADWIMPS"]), t("2", ["Bump"]), t("3", ["Radwimps"])]
    result = build_plan(tracks)
    # RADWIMPS/Radwimps は同一アーティストとして2曲の塊になり前方へ、Bump は単独曲として末尾
    assert [x.video_id for x in result] == ["1", "3", "2"]


def test_compute_position_updates_reorders_correctly():
    current = [t("1", ["A"]), t("2", ["B"]), t("3", ["A"])]
    target = build_plan(current)
    updates = compute_position_updates(current, target)

    order = [x.item_id for x in current]
    for track, position in updates:
        order.remove(track.item_id)
        order.insert(position, track.item_id)
    assert order == [x.item_id for x in target]


def test_compute_position_updates_empty_when_already_sorted():
    tracks = [t("1", ["A"]), t("2", ["A"]), t("3", ["B"])]
    target = build_plan(tracks)
    assert compute_position_updates(tracks, target) == []
