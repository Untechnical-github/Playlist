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


def test_run_score_filters_by_threshold_skips_existing_and_reports_only_new_adds(monkeypatch):
    views_by_track = {
        ("Song A", "Artist A"): ("v1", 2_000_000),  # しきい値超え、新規
        ("Song B", "Artist B"): ("v2", 500_000),  # しきい値未満
        ("Song C", "Artist C"): ("v3", 60_000_000),  # しきい値超えだが既に追加済み
        ("Song D", "Artist D"): (None, 0),  # YouTubeでヒットしない
    }

    def fake_fetch_playlist_tracks(playlist_id):
        return [{"title": t, "artist": a} for (t, a) in views_by_track]

    def fake_get_youtube_view_count(youtube, title, artist, cache):
        return views_by_track[(title, artist)]

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", fake_fetch_playlist_tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(
        gst, "list_my_playlists", lambda auth_header: [{"id": "PL_TARGET", "snippet": {"title": "Playlist"}}]
    )
    monkeypatch.setattr(
        gst,
        "list_playlist_items",
        lambda auth_header, playlist_id: [{"contentDetails": {"videoId": "v3"}}],
    )
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append((playlist_id, video_id)))

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score("src_playlist", 100, "app", "tok")  # 100万回再生以上

    assert added == [("PL_TARGET", "v1")]
    combined = "".join(sent)
    assert "Song A" in combined
    assert "Song B" not in combined  # しきい値未満
    assert "Song C" not in combined  # 既に追加済みなので新規追加分には出ない
