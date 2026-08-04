import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github_task
from core.models import Track
from core.planner import group_tracks


def t(video_id, artists, title=None):
    return Track(video_id=video_id, item_id=f"item_{video_id}", title=title or video_id, artists=artists)


def _all_field_text(embeds):
    return "".join(f["value"] for e in embeds for f in e.get("fields", []))


def test_build_embeds_fits_in_a_single_embed_for_small_playlists():
    tracks = [t("1", ["A"]), t("2", ["A"]), t("3", ["B"]), t("4", ["B"])]
    grouped = group_tracks(tracks)
    embeds = github_task.build_embeds("小さいプレイリスト", grouped)
    assert len(embeds) == 1
    assert embeds[0]["title"] == "並び替えプレビュー: 小さいプレイリスト"


def test_build_embeds_splits_across_multiple_embeds_without_omitting_blocks():
    tracks = []
    for i in range(40):
        tracks.append(t(f"{i}a", [f"Artist{i:02d}"], title=f"Song {i}A"))
        tracks.append(t(f"{i}b", [f"Artist{i:02d}"], title=f"Song {i}B"))
    grouped = group_tracks(tracks)

    embeds = github_task.build_embeds("大きいプレイリスト", grouped)
    assert len(embeds) > 1

    text = _all_field_text(embeds)
    for artist, block_tracks in grouped.blocks:
        for track in block_tracks:
            assert track.title in text


def test_chunk_embeds_respects_the_ten_embed_per_message_limit():
    tracks = []
    for i in range(300):
        tracks.append(t(f"{i}a", [f"Artist{i:03d}"], title=f"Song{i}A"))
        tracks.append(t(f"{i}b", [f"Artist{i:03d}"], title=f"Song{i}B"))
    grouped = group_tracks(tracks)

    embeds = github_task.build_embeds("超大規模プレイリスト", grouped)
    chunks = github_task.chunk_embeds(embeds)

    assert all(len(c) <= github_task.MAX_EMBEDS_PER_MESSAGE for c in chunks)
    assert sum(len(c) for c in chunks) == len(embeds)

    text = _all_field_text(embeds)
    for artist, block_tracks in grouped.blocks:
        for track in block_tracks:
            assert track.title in text


def test_single_field_over_1024_chars_is_split_into_continuation_fields():
    tracks = [
        t(str(i), ["BigArtist"], title=f"とても長くて説明的な曲名のサンプルトラック番号 {i:03d}")
        for i in range(100)
    ]
    grouped = group_tracks(tracks)
    embeds = github_task.build_embeds("単一アーティスト大量曲", grouped)

    fields = [f for e in embeds for f in e.get("fields", [])]
    assert len(fields) > 1
    for f in fields:
        assert len(f["value"]) <= github_task.MAX_FIELD_VALUE

    text = _all_field_text(embeds)
    for track in tracks:
        assert track.title in text
