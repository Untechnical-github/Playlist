import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.apply import compute_position_updates
from core.models import Track
from core.planner import (
    _auto_merge_similar_names,
    _auto_merge_transliterations,
    _is_kana_only,
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
    # Bump は1曲だけなので、隣に何があっても関係なく単独曲として末尾に残る。
    assert [x.video_id for x in result] == ["1", "4", "2", "5", "3"]


def test_single_tag_track_next_to_a_block_is_not_absorbed_by_it():
    tracks = [
        t("1", ["Zard"]),  # 単独タグ。Aiko の隣にいても取り込まれない（位置は見ない）
        t("2", ["Aiko"]),
        t("3", ["Bump"]),  # こちらも単独タグ
        t("4", ["Aiko"]),
    ]
    result = build_plan(tracks)
    assert [x.video_id for x in result] == ["2", "4", "1", "3"]


def test_single_tag_track_isolated_from_any_block_stays_in_the_tail():
    tracks = [
        t("1", ["Solo1"]),
        t("2", ["Solo2"]),
    ]
    result = build_plan(tracks)
    # どちらも単独タグなので末尾（元の相対順）に残る
    assert [x.video_id for x in result] == ["1", "2"]


def test_scattered_same_artist_forms_a_block_while_unrelated_singles_stay_separate():
    tracks = [
        t("1", ["ArtistQ"]),
        t("2", ["Solo1"]),
        t("3", ["ArtistQ"]),
        t("4", ["Solo2"]),
    ]
    result = build_plan(tracks)
    # ArtistQ は離れていても2曲あるので必ず1つの塊になる。Solo1/Solo2 は隣にいても
    # 無関係なので取り込まれず、それぞれ単独曲として元の相対順のまま末尾に残る。
    assert [x.video_id for x in result] == ["1", "3", "2", "4"]


def test_block_internal_order_preserves_original_relative_order():
    tracks = [t("1", ["Eve"]), t("2", ["Bump"]), t("3", ["Eve"])]
    result = build_plan(tracks)
    # Bump（単独タグ）は Eve に挟まれていても無関係なので取り込まれず、末尾に残る
    assert [x.video_id for x in result] == ["1", "3", "2"]


def test_collab_track_joins_a_genuine_multi_song_artist_among_its_own_tags():
    tracks = [
        t("1", ["Aiko"]),
        t("2", ["Aiko", "Bump"]),
        t("3", ["Bump"]),
    ]
    result = build_plan(tracks)
    # Aiko はこのコラボ曲を含めて2曲あるので塊になり前方へ。Bump は1曲だけなので末尾に残る。
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_collab_track_falls_back_to_first_listed_artist_when_neither_tag_qualifies():
    tracks = [t("1", ["Zard", "Bump"]), t("2", ["Aiko"])]
    result = build_plan(tracks)
    # Zard・Bump・Aiko のどれも2曲に達しないので、塊は生まれず元の相対順のまま
    assert [x.video_id for x in result] == ["1", "2"]


def test_unknown_ugc_track_never_forms_a_block_since_it_has_no_artist_tag():
    known_a = t("1", ["Aiko"])
    ugc = t("2", [], video_type="MUSIC_VIDEO_TYPE_UGC")
    known_b = t("3", ["Bump"])
    result = build_plan([known_a, ugc, known_b])
    # アーティスト情報がゼロなので、隣に何があっても塊にはなり得ず、常に末尾（元の相対順）
    assert [x.video_id for x in result] == ["1", "2", "3"]


def test_fully_isolated_unknown_track_goes_to_the_end():
    ugc = t("1", [], video_type="MUSIC_VIDEO_TYPE_UGC")
    result = build_plan([ugc])
    assert [x.video_id for x in result] == ["1"]


def test_case_insensitive_artist_names_are_merged_into_a_block():
    tracks = [t("1", ["RADWIMPS"]), t("2", ["Bump"]), t("3", ["Radwimps"])]
    result = build_plan(tracks)
    # RADWIMPS/Radwimps は同一アーティストとして2曲の塊になり前方へ。Bump は単独曲として末尾。
    assert [x.video_id for x in result] == ["1", "3", "2"]


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


def test_hiragana_and_romaji_spelling_of_same_artist_are_merged_automatically():
    tracks = [t("1", ["natori"]), t("2", ["なとり"])]
    grouped = group_tracks(tracks)
    assert len(grouped.blocks) == 1
    name, block_tracks = grouped.blocks[0]
    assert name in ("natori", "なとり")
    assert [x.video_id for x in block_tracks] == ["1", "2"]
    assert grouped.tail == []


def test_kanji_names_are_not_auto_merged_by_transliteration():
    # 漢字の読みは辞書変換だけでは一意に決まらないため、対象外（誤爆防止）
    tracks = [t("1", ["米津玄師"]), t("2", ["Kenshi Yonezu"])]
    merges = _auto_merge_transliterations(tracks, {})
    assert merges == {}


def test_is_kana_only():
    assert _is_kana_only("ヨルシカ") is True
    assert _is_kana_only("ヨルシカ・") is True
    assert _is_kana_only("なとり") is True
    assert _is_kana_only("かぐや") is True
    assert _is_kana_only("米津玄師") is False
    assert _is_kana_only("Yorushika") is False


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
