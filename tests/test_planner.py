import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.apply import compute_position_updates
from core.models import Track
from core.planner import (
    _auto_merge_similar_names,
    _auto_merge_transliterations,
    _is_katakana_only,
    build_plan,
    group_tracks,
)


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
    # Bump（単独タグ）は sumika と隣接しているため、その塊に取り込まれる。
    assert [x.video_id for x in result] == ["1", "4", "2", "3", "5"]


def test_single_tag_track_adjacent_to_a_block_is_absorbed_into_it():
    tracks = [
        t("1", ["Zard"]),  # 単独タグだが Aiko の隣にいるので Aiko の塊に取り込まれる
        t("2", ["Aiko"]),
        t("3", ["Bump"]),  # こちらも単独タグ、同じく Aiko に隣接して取り込まれる
        t("4", ["Aiko"]),
    ]
    result = build_plan(tracks)
    assert [x.video_id for x in result] == ["1", "2", "3", "4"]


def test_single_tag_track_isolated_from_any_block_stays_in_the_tail():
    tracks = [
        t("1", ["Solo1"]),
        t("2", ["Solo2"]),
    ]
    result = build_plan(tracks)
    # どちらも単独タグで、隣接する塊が無いので末尾（元の相対順）に残る
    assert [x.video_id for x in result] == ["1", "2"]


def test_scattered_same_artist_in_the_tail_still_forms_a_block():
    tracks = [
        t("1", ["ArtistQ"]),
        t("2", ["Solo1"]),
        t("3", ["ArtistQ"]),
        t("4", ["Solo2"]),
    ]
    result = build_plan(tracks)
    # ArtistQ は離れていても2曲あるので必ず1つの塊になる（Solo1も隣接して取り込まれる）
    assert [x.video_id for x in result] == ["1", "2", "3", "4"]


def test_block_internal_order_preserves_original_relative_order():
    tracks = [t("1", ["Eve"]), t("2", ["Bump"]), t("3", ["Eve"])]
    result = build_plan(tracks)
    # Bump（単独タグ）は Eve の間に挟まれているため Eve の塊に取り込まれる
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_collab_track_follows_the_artist_block_it_is_currently_adjacent_to():
    tracks = [
        t("1", ["Aiko"]),
        t("2", ["Aiko", "Bump"]),  # Aiko の隣にあるので Aiko の塊に入る
        t("3", ["Bump"]),
    ]
    result = build_plan(tracks)
    # Aiko+コラボ曲で2曲の塊になるので前方へ、Bump は単独タグかつ隣接する塊が無いので末尾に残る
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_collab_track_with_no_matching_neighbor_falls_back_to_first_listed_artist():
    tracks = [t("1", ["Zard", "Bump"]), t("2", ["Aiko"])]
    result = build_plan(tracks)
    # 隣接する Aiko とは一致しないので先頭アーティスト Zard で確定するが、どちらも単独タグかつ
    # 互いに隣接する塊が無いので、塊は生まれず元の相対順のまま
    assert [x.video_id for x in result] == ["1", "2"]


def test_unknown_ugc_track_follows_its_current_neighbor_and_can_form_a_block():
    known_a = t("1", ["Aiko"])
    ugc = t("2", [], video_type="MUSIC_VIDEO_TYPE_UGC")
    known_b = t("3", ["Bump"])
    result = build_plan([known_a, ugc, known_b])
    # ugc は Aiko の隣にいたので Aiko の塊に入り2曲になる。Bump は単独タグで隣接する塊が無いので末尾。
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_fully_isolated_unknown_track_goes_to_the_end():
    ugc = t("1", [], video_type="MUSIC_VIDEO_TYPE_UGC")
    result = build_plan([ugc])
    assert [x.video_id for x in result] == ["1"]


def test_case_insensitive_artist_names_are_merged_into_a_block():
    tracks = [t("1", ["RADWIMPS"]), t("2", ["Bump"]), t("3", ["Radwimps"])]
    result = build_plan(tracks)
    # RADWIMPS/Radwimps は同一アーティストとして2曲の塊になり、間の Bump（単独タグ）も取り込まれる
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_fuzzy_typo_variant_of_artist_name_is_merged_automatically():
    tracks = [
        t("1", ["Macaroni Empitsu"]),
        t("2", ["macaroni enpitsu"]),
        t("3", ["macaroni enpitsu"]),
    ]
    grouped = group_tracks(tracks)
    assert len(grouped.blocks) == 1
    _, block_tracks = grouped.blocks[0]
    assert [x.video_id for x in block_tracks] == ["1", "2", "3"]
    assert grouped.tail == []


def test_fuzzy_merge_does_not_trigger_on_short_or_dissimilar_names():
    tracks = [t("1", ["Aimyon"]), t("2", ["Aimer"]), t("3", ["Uru"])]
    merges = _auto_merge_similar_names(tracks, {})
    # Aimyon と Aimer は文字列としてそれなりに違う（短めで紛れやすい）ので統合されない
    assert merges == {}


def test_katakana_and_romaji_spelling_of_same_artist_are_merged_automatically():
    tracks = [t("1", ["ヨルシカ"]), t("2", ["Yorushika"])]
    grouped = group_tracks(tracks)
    assert len(grouped.blocks) == 1
    name, block_tracks = grouped.blocks[0]
    assert name in ("ヨルシカ", "Yorushika", "yorushika")
    assert [x.video_id for x in block_tracks] == ["1", "2"]
    assert grouped.tail == []


def test_kanji_names_are_not_auto_merged_by_transliteration():
    # 漢字の読みは辞書変換だけでは一意に決まらないため、対象外（誤爆防止）
    tracks = [t("1", ["米津玄師"]), t("2", ["Kenshi Yonezu"])]
    merges = _auto_merge_transliterations(tracks, {})
    assert merges == {}


def test_is_katakana_only():
    assert _is_katakana_only("ヨルシカ") is True
    assert _is_katakana_only("ヨルシカ・") is True
    assert _is_katakana_only("かぐや") is False
    assert _is_katakana_only("米津玄師") is False
    assert _is_katakana_only("Yorushika") is False


def test_artist_group_alias_merges_unrelated_tags_into_one_block():
    tracks = [
        t("r1", ["ryo (supercell)"]),
        t("r2", ["ryo (supercell)"]),
        t("k1", ["Kaguya(cv. Yuko Natsuyoshi)"]),
        t("k2", ["Kaguya(cv. Yuko Natsuyoshi)"]),
    ]
    alias_map = {
        "ryo (supercell)": "kaguya",
        "kaguya(cv. yuko natsuyoshi)": "kaguya",
        "kaguya": "kaguya",
    }
    group_display = {"kaguya": "Kaguya"}

    grouped = group_tracks(tracks, alias_map, group_display)
    assert len(grouped.blocks) == 1
    name, block_tracks = grouped.blocks[0]
    assert name == "Kaguya"
    assert [x.video_id for x in block_tracks] == ["r1", "r2", "k1", "k2"]
    assert grouped.tail == []


def test_without_alias_map_unrelated_tags_stay_separate_blocks():
    tracks = [
        t("r1", ["ryo (supercell)"]),
        t("r2", ["ryo (supercell)"]),
        t("k1", ["Kaguya(cv. Yuko Natsuyoshi)"]),
        t("k2", ["Kaguya(cv. Yuko Natsuyoshi)"]),
    ]
    grouped = group_tracks(tracks)
    names = [name for name, _ in grouped.blocks]
    assert names == ["Kaguya(cv. Yuko Natsuyoshi)", "ryo (supercell)"]


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
