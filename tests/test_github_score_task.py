import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github_score_task as gst


def test_send_paginated_message_splits_long_content_without_omitting_lines():
    sent = []
    gst.post_followup = lambda app_id, token, content: sent.append(content)

    header = "header\n"
    lines = [f"line{i}" * 20 for i in range(50)]
    gst.send_paginated_message("app", "tok", header, lines)

    assert len(sent) > 1
    assert all(len(m) <= gst.MAX_MESSAGE_LENGTH + 200 for m in sent)
    all_text = "".join(sent)
    for line in lines:
        assert line in all_text


def test_get_source_playlists_excludes_playlist_prefixed_and_english_songs(monkeypatch):
    all_playlists = [
        {"id": "PL1", "snippet": {"title": "Eve"}},
        {"id": "PL2", "snippet": {"title": "Playlist"}},
        {"id": "PL3", "snippet": {"title": "Playlist II"}},
        {"id": "PL4", "snippet": {"title": "English Songs"}},
        {"id": "PL5", "snippet": {"title": "BUMP OF CHICKEN"}},
    ]
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: all_playlists)

    filtered = gst.get_source_playlists("Bearer fake")
    assert [p["id"] for p in filtered] == ["PL1", "PL5"]


def test_run_score_aggregates_multiple_source_playlists_and_reports_only_new_adds(monkeypatch):
    playlists = [
        {"id": "PL_SRC_1", "snippet": {"title": "Eve"}},
        {"id": "PL_SRC_2", "snippet": {"title": "BUMP OF CHICKEN"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
        {"id": "PL_EXCLUDED", "snippet": {"title": "English Songs"}},
    ]

    tracks_by_playlist = {
        "PL_SRC_1": [{"title": "Song A", "artist": "Artist A"}, {"title": "Song B", "artist": "Artist B"}],
        "PL_SRC_2": [{"title": "Song C", "artist": "Artist C"}, {"title": "Song D", "artist": "Artist D"}],
    }

    views_by_track = {
        ("Song A", "Artist A"): ("v1", 2_000_000),  # しきい値超え、新規
        ("Song B", "Artist B"): ("v2", 500_000),  # しきい値未満
        ("Song C", "Artist C"): ("v3", 60_000_000),  # しきい値超えだが既に追加済み
        ("Song D", "Artist D"): (None, 0),  # YouTubeでヒットしない
    }

    fetched_playlist_ids = []

    def fake_fetch_playlist_tracks(playlist_id):
        fetched_playlist_ids.append(playlist_id)
        return tracks_by_playlist[playlist_id]

    def fake_get_youtube_view_count(youtube, title, artist, cache):
        return views_by_track[(title, artist)]

    added = []
    removed = []

    # 「Playlist」には既に v3 (Song C、今回も条件を満たす) と、
    # v_old (今回の集計対象には出てこない＝しきい値を満たさなくなった曲) が入っている
    existing_playlist_items = [
        {"id": "item_v3", "contentDetails": {"videoId": "v3"}, "snippet": {"title": "Song C - Artist C"}},
        {"id": "item_old", "contentDetails": {"videoId": "v_old"}, "snippet": {"title": "Old Song - Old Artist"}},
    ]

    monkeypatch.setattr(gst, "fetch_playlist_tracks", fake_fetch_playlist_tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(
        gst, "list_playlist_items", lambda auth_header, playlist_id: existing_playlist_items
    )
    monkeypatch.setattr(
        gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append((playlist_id, video_id))
    )
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: removed.append(item_id))

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score(100, "app", "tok")  # 100万回再生以上

    # 「Playlist」「English Songs」は集計対象から除外され、fetchすら呼ばれない
    assert set(fetched_playlist_ids) == {"PL_SRC_1", "PL_SRC_2"}

    assert added == [("PL_TARGET", "v1")]  # Song A のみ新規追加（Song Cは既存なので追加しない）
    assert removed == ["item_old"]  # 今回の対象に無い v_old だけ削除、v3(既存かつ条件を満たす)は削除しない

    combined = "".join(sent)
    assert "Song A" in combined
    assert "Song B" not in combined  # しきい値未満
    assert "Song C" not in combined  # 既に追加済みで今回も条件を満たすので追加/削除どちらの報告にも出ない
    assert "Old Song" in combined  # 削除された曲として報告される
