import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

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
        # YouTube Musicが自動生成する高評価動画プレイリスト。他の集計対象と曲が重複しやすいため除外
        {"id": "PL6", "snippet": {"title": "高く評価した音楽"}},
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

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        result = views_by_track[(title, artist)]
        cache[(title, artist)] = result  # 実際のget_youtube_view_countと同様、確認できた曲はキャッシュに書く
        return result

    added = []
    removed = []

    # 「Playlist」には既に v3 (Song C、今回も条件を満たす) と、
    # v_old (集計対象のどのプレイリストにも出てこない＝今回は検証しようがない曲) が入っている
    existing_playlist_items = [
        {"id": "item_v3", "contentDetails": {"videoId": "v3"}, "snippet": {"title": "Song C - Artist C"}},
        {"id": "item_old", "contentDetails": {"videoId": "v_old"}, "snippet": {"title": "Old Song - Old Artist"}},
    ]

    monkeypatch.setattr(gst, "fetch_playlist_tracks", fake_fetch_playlist_tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
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
    # v_oldはどの集計対象プレイリストにも出てこず今回検証できないため、削除せず保留する
    assert removed == []

    combined = "".join(sent)
    assert "Song A" in combined
    assert "Song B" not in combined  # しきい値未満
    assert "Song C" not in combined  # 既に追加済みで今回も条件を満たすので追加/削除どちらの報告にも出ない
    assert "Old Song" in combined  # 保留された曲として報告される
    assert "対象4曲中4曲を確認済み" in combined  # 4曲すべて確認できたことが分かる
    assert "クォータ" not in combined  # クォータ超過は起きていないので警告は出ない


def _setup_common_run_score_mocks(monkeypatch, playlists, tracks, fake_get_youtube_view_count):
    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: None)
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)


def test_run_score_includes_quota_warning_when_daily_quota_exceeded(monkeypatch):
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Eve"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [
        {"title": "Song A", "artist": "Artist A"},  # 確認できる
        {"title": "Song B", "artist": "Artist B"},  # クォータ切れで確認できない
    ]

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        if title == "Song A":
            result = ("v1", 100)
            cache[(title, artist)] = result
            return result
        return (None, 0)  # Song Bはクォータ切れ相当で未確認のまま（cacheに書き込まない）

    _setup_common_run_score_mocks(monkeypatch, playlists, tracks, fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "was_daily_quota_exceeded", lambda: True)

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score(100, "app", "tok")

    combined = "".join(sent)
    assert "対象2曲中1曲を確認済み" in combined
    assert "本日のYouTube検索クォータを使い切った" in combined


def test_confirmed_cover_video_ids_returns_only_yes_status_candidates():
    cover_candidates = {
        gst._track_key("少女レイ / 星街すいせい(Cover)", "Suisei Hoshimachi"): {
            "orig_v1": {"status": "yes"},
            "other_v2": {"status": "no"},
            "pending_v3": {"status": "pending"},
        }
    }

    result = gst.confirmed_cover_video_ids(cover_candidates, "少女レイ / 星街すいせい(Cover)", "Suisei Hoshimachi")

    assert result == ["orig_v1"]


def test_confirmed_cover_video_ids_returns_empty_list_for_unknown_track():
    assert gst.confirmed_cover_video_ids({}, "Unknown Song", "Unknown Artist") == []


def test_run_score_adds_duplicate_video_id_only_once_when_track_is_in_multiple_playlists(monkeypatch):
    # 同じ曲が複数の集計対象プレイリストに入っていると matches に同じ video_id が複数回入る。
    # 2回目もadd_playlist_itemを呼んでしまうと、YouTube側で「たった今追加した重複」として
    # 409 Conflictになる実際の不具合の再現ケース
    playlists = [
        {"id": "PL_SRC_1", "snippet": {"title": "Eve"}},
        {"id": "PL_SRC_2", "snippet": {"title": "お気に入り"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks_by_playlist = {
        "PL_SRC_1": [{"title": "廻廻奇譚", "artist": "Eve"}],
        "PL_SRC_2": [{"title": "廻廻奇譚", "artist": "Eve"}],  # 別プレイリストに同じ曲が重複して入っている
    }

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        result = ("v1", 2_000_000)
        cache[(title, artist)] = result
        return result

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks_by_playlist[playlist_id])
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append(video_id))
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: None)
    monkeypatch.setattr(gst, "send_paginated_message", lambda app_id, token, header, lines: None)

    gst.run_score(100, "app", "tok")  # 100万回再生以上

    assert added == ["v1"]  # 2回登場しても追加は1回だけ


def test_run_score_uses_confirmed_cover_candidate_view_count_but_adds_the_cover_track(monkeypatch):
    # カバー動画自体の再生回数はしきい値未満だが、確定済みの元曲候補の再生回数は十分あるので、
    # カバー動画がPlaylistに追加されるべきケース
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Suisei"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "少女レイ / 星街すいせい(Cover)", "artist": "Suisei Hoshimachi"}]

    cover_candidates = {
        gst._track_key("少女レイ / 星街すいせい(Cover)", "Suisei Hoshimachi"): {"orig_v1": {"status": "yes"}}
    }

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        result = ("cover_v1", 100_000)
        cache[(title, artist)] = result
        return result

    def fake_get_view_count_by_video_id(youtube, video_id, cache, fetched_at, force_refresh=False):
        assert video_id == "orig_v1"
        return 90_000_000

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "get_view_count_by_video_id", fake_get_view_count_by_video_id)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: cover_candidates)
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append(video_id))
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score(1000, "app", "tok")  # 1000万回再生以上。カバー単体では満たさないが確定済み候補基準なら満たす

    assert added == ["cover_v1"]  # 追加されるのはあくまでカバー動画自体
    assert "少女レイ" in "".join(sent)


def test_run_score_force_refetches_confirmed_cover_candidate_when_combined_view_count_is_borderline(monkeypatch):
    # カバー動画自体の再生回数だけでは境界線かどうか判断できないため、確定済み候補と合わせた
    # 最大値で境界線判定し、境界線ならカバー・候補どちらも再取得すべきケース
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Suisei"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "少女レイ / 星街すいせい(Cover)", "artist": "Suisei Hoshimachi"}]

    cover_candidates = {
        gst._track_key("少女レイ / 星街すいせい(Cover)", "Suisei Hoshimachi"): {"orig_v1": {"status": "yes"}}
    }

    force_refresh_video_ids = []

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        result = ("cover_v1", 100_000)
        cache[(title, artist)] = result
        return result

    def fake_get_view_count_by_video_id(youtube, video_id, cache, fetched_at, force_refresh=False):
        if force_refresh:
            force_refresh_video_ids.append(video_id)
            return 1_200_000  # 再取得後はしきい値超え
        return 900_000  # しきい値の90%

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "get_view_count_by_video_id", fake_get_view_count_by_video_id)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: cover_candidates)
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append(video_id))
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: None)

    gst.run_score(100, "app", "tok")  # しきい値1,000,000

    assert force_refresh_video_ids == ["orig_v1"]
    assert added == ["cover_v1"]


def test_run_score_only_force_refetches_tracks_within_80_percent_of_threshold(monkeypatch):
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Eve"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [
        {"title": "Far Below", "artist": "Artist Low"},  # しきい値の50% → 再取得しない
        {"title": "Borderline", "artist": "Artist Mid"},  # しきい値の90% → 再取得する
        {"title": "Already Qualifies", "artist": "Artist High"},  # 既にしきい値超え → 再取得しない
    ]

    initial_views = {
        ("Far Below", "Artist Low"): ("v_low", 500_000),
        ("Borderline", "Artist Mid"): ("v_mid", 900_000),
        ("Already Qualifies", "Artist High"): ("v_high", 5_000_000),
    }

    force_refresh_calls = []

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        if force_refresh:
            force_refresh_calls.append((title, artist))
            result = ("v_mid", 1_500_000)
        else:
            result = initial_views[(title, artist)]
        cache[(title, artist)] = result
        return result

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: None)
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: None)

    gst.run_score(100, "app", "tok")  # しきい値1,000,000（境界線は800,000以上1,000,000未満）

    assert force_refresh_calls == [("Borderline", "Artist Mid")]


def test_run_score_does_not_remove_item_when_lookup_could_not_be_verified_this_run(monkeypatch):
    # クォータ超過等でAPI呼び出しが失敗し、かつ以前にキャッシュされたこともない曲は
    # 「しきい値未満」と確定できないため、既にPlaylistに入っていても削除してはいけない
    # （人気の高いEveの曲が誤って削除された実際の不具合の再現ケース）
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Eve"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        # 実際のAPIエラー時と同様、結果を確定できないのでcacheには書き込まない
        return (None, 0)

    existing_playlist_items = [
        {"id": "item_kaikai", "contentDetails": {"videoId": "kaikai_v1"}, "snippet": {"title": "廻廻奇譚 - Eve MV"}},
    ]

    removed = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: existing_playlist_items)
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: None)
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: removed.append(item_id))

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score(5000, "app", "tok")  # 5000万回再生以上

    assert removed == []  # 確認できなかったので削除しない
    combined = "".join(sent)
    assert "廻廻奇譚" in combined
    assert "保留" in combined


# --- コラボ・カバー候補の自動探索（discover_cover_candidates） ---


def _no_commit(monkeypatch):
    """discoveryのGitコミットをテストごとに個別無効化しなくて済むようにするヘルパー。"""
    monkeypatch.setattr(gst, "save_cover_candidates", lambda cc: None)
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: None)


def test_discover_cover_candidates_skips_tracks_without_cached_own_view_count_or_above_threshold(monkeypatch):
    def _fail_search(*args, **kwargs):
        raise AssertionError("対象外の曲で検索が呼ばれるべきではない")

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", _fail_search)
    monkeypatch.setattr(gst, "search_videos_by_title", _fail_search)
    _no_commit(monkeypatch)

    tracks = [
        {"title": "Not Cached Yet", "artist": "Artist A"},  # 自身の再生回数が未確定
        {"title": "Already Qualifies", "artist": "Artist B"},  # 既にしきい値超え
    ]
    cache = {("Already Qualifies", "Artist B"): ("v1", 5_000_000)}
    cover_candidates = {}

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert result == []
    assert cover_candidates == {}


def test_discover_cover_candidates_finds_candidate_via_ytmusic_without_youtube_fallback(monkeypatch):
    # ytmusicapiの検索だけで候補が見つかれば、クォータを消費するYouTube検索は行わない
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]
    cache = {("廻廻奇譚", "Eve"): ("own_v1", 500_000)}  # しきい値未満
    cover_candidates = {}

    def fake_ytmusic_search(yt, title, max_results=5):
        assert title == "廻廻奇譚"
        return [
            ("own_v1", "廻廻奇譚 - Eve MV", "Eve"),  # 自分自身は候補から除外される
            ("cover_v1", "廻廻奇譚 (Cover)", "Someone"),
            ("cover_v2", "廻廻奇譚 (Piano ver.)", "Someone Else"),
            ("unrelated_v1", "全然関係ない動画", "Unrelated"),  # 曲名を含まないので除外される
        ]

    def fake_stats(youtube, video_ids):
        assert set(video_ids) == {"cover_v1", "cover_v2"}
        return {"cover_v1": 3_000_000, "cover_v2": 9_000_000}

    def _fail_youtube_search(*args, **kwargs):
        raise AssertionError("ytmusicapiで見つかったのでYouTube検索は不要なはず")

    saved = []
    committed = []
    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", fake_ytmusic_search)
    monkeypatch.setattr(gst, "search_videos_by_title", _fail_youtube_search)
    monkeypatch.setattr(gst, "get_view_counts_for_video_ids", fake_stats)
    monkeypatch.setattr(gst, "save_cover_candidates", lambda cc: saved.append(json.loads(json.dumps(cc))))
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: committed.append((paths, msg)))

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert len(result) == 1
    assert result[0]["video_id"] == "cover_v2"  # 再生回数が最大の候補を採用
    assert result[0]["track_title"] == "廻廻奇譚"
    assert result[0]["track_artist"] == "Eve"

    track_data = cover_candidates[gst._track_key("廻廻奇譚", "Eve")]
    entry = track_data["cover_v2"]
    assert entry["status"] == "pending"
    assert entry["view_count"] == 9_000_000
    assert "cover_v1" not in track_data  # 採用されなかった候補は保存しない
    assert track_data["_meta"] == {"ytmusic_checked": True, "youtube_checked": False}

    assert saved and committed


def test_looks_like_same_artist_matches_exact_and_substring_variants():
    assert gst._looks_like_same_artist("Eve", "Eve") is True
    assert gst._looks_like_same_artist("Eve Official", "Eve") is True  # 公式チャンネルの別名義
    assert gst._looks_like_same_artist("Eve", "Eve Official") is True
    assert gst._looks_like_same_artist("Someone Else", "Eve") is False
    assert gst._looks_like_same_artist("", "Eve") is False


def test_discover_cover_candidates_excludes_candidates_from_the_same_artist(monkeypatch):
    # 曲名だけの検索では、別人によるカバー・コラボではなく本人の別アップロード
    # （MV・リリックビデオ等）まで拾ってしまうことがあるため、それらは候補から除外する
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]
    cache = {("廻廻奇譚", "Eve"): ("own_v1", 500_000)}
    cover_candidates = {}

    def fake_ytmusic_search(yt, title, max_results=5):
        return [
            ("lyric_v1", "廻廻奇譚 (Lyric Video)", "Eve"),  # 本人の別アップロード → 除外
            ("official_v1", "廻廻奇譚 Official Audio", "Eve Official"),  # 同様に除外
            ("cover_v1", "廻廻奇譚 (Cover)", "Someone Else"),  # 別人のカバー → 候補になる
        ]

    def fake_stats(youtube, video_ids):
        assert set(video_ids) == {"cover_v1"}  # 本人名義の2件は統計取得すら行わない
        return {"cover_v1": 5_000_000}

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", fake_ytmusic_search)
    monkeypatch.setattr(gst, "get_view_counts_for_video_ids", fake_stats)
    _no_commit(monkeypatch)

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert len(result) == 1
    assert result[0]["video_id"] == "cover_v1"


def test_discover_cover_candidates_falls_back_to_youtube_when_ytmusic_finds_nothing(monkeypatch):
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]
    cache = {("廻廻奇譚", "Eve"): ("own_v1", 500_000)}
    cover_candidates = {}

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", lambda yt, title, max_results=5: [])

    def fake_youtube_search(youtube, title, max_results=5):
        return [("cover_v1", "廻廻奇譚 (Cover)", "Someone")]

    monkeypatch.setattr(gst, "search_videos_by_title", fake_youtube_search)
    monkeypatch.setattr(gst, "get_view_counts_for_video_ids", lambda youtube, ids: {"cover_v1": 1_000_000})
    _no_commit(monkeypatch)

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert len(result) == 1
    assert result[0]["video_id"] == "cover_v1"
    track_data = cover_candidates[gst._track_key("廻廻奇譚", "Eve")]
    assert track_data["_meta"] == {"ytmusic_checked": True, "youtube_checked": True}


def test_discover_cover_candidates_does_not_reprocess_track_exhausted_by_both_searches(monkeypatch):
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]
    cache = {("廻廻奇譚", "Eve"): ("own_v1", 500_000)}
    cover_candidates = {
        gst._track_key("廻廻奇譚", "Eve"): {"_meta": {"ytmusic_checked": True, "youtube_checked": True}}
    }

    def _fail_search(*args, **kwargs):
        raise AssertionError("両方の方法で調べ尽くした曲を再検索するべきではない")

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", _fail_search)
    monkeypatch.setattr(gst, "search_videos_by_title", _fail_search)

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert result == []


def test_discover_cover_candidates_skips_track_with_pending_candidate_already(monkeypatch):
    def _fail_search(*args, **kwargs):
        raise AssertionError("回答待ちの候補がある曲で検索が呼ばれるべきではない")

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", _fail_search)
    monkeypatch.setattr(gst, "search_videos_by_title", _fail_search)

    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]
    cache = {("廻廻奇譚", "Eve"): ("own_v1", 500_000)}
    cover_candidates = {gst._track_key("廻廻奇譚", "Eve"): {"existing_v1": {"status": "pending"}}}

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert result == []


def test_discover_cover_candidates_does_not_repropose_already_known_candidate(monkeypatch):
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]
    cache = {("廻廻奇譚", "Eve"): ("own_v1", 500_000)}
    # 既に「いいえ」判定済みの候補は二度と提案しない
    cover_candidates = {gst._track_key("廻廻奇譚", "Eve"): {"known_v1": {"status": "no"}}}

    def fake_ytmusic_search(yt, title, max_results=5):
        return [("known_v1", "廻廻奇譚 関連動画", "Someone")]

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", fake_ytmusic_search)
    monkeypatch.setattr(gst, "search_videos_by_title", lambda *a, **k: [])  # ytmusicで見つからず後段にフォールバックする
    monkeypatch.setattr(gst, "get_view_counts_for_video_ids", lambda youtube, ids: {})
    _no_commit(monkeypatch)

    result = gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, cover_candidates)

    assert result == []


def test_discover_cover_candidates_stops_after_max_tracks_per_run(monkeypatch):
    tracks = [
        {"title": f"Song {i}", "artist": "Artist"} for i in range(gst.COVER_DISCOVERY_MAX_TRACKS_PER_RUN + 3)
    ]
    cache = {(t["title"], t["artist"]): (f"v{i}", 100) for i, t in enumerate(tracks)}

    search_calls = []

    def fake_ytmusic_search(yt, title, max_results=5):
        search_calls.append(title)
        return []

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", fake_ytmusic_search)
    monkeypatch.setattr(gst, "search_videos_by_title", lambda *a, **k: [])  # YouTubeフォールバックも空扱いにする
    _no_commit(monkeypatch)

    gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, {})

    assert len(search_calls) == gst.COVER_DISCOVERY_MAX_TRACKS_PER_RUN


def test_discover_cover_candidates_caps_youtube_fallback_searches_but_keeps_using_ytmusic(monkeypatch):
    # ytmusicapiでは何も見つからない状況で、YouTube検索フォールバックの上限に達したら
    # それ以上YouTube検索は行わないが、ytmusicapiでの探索（クォータ制限なし）は続ける
    limit = 3
    monkeypatch.setattr(gst, "COVER_DISCOVERY_YOUTUBE_SEARCH_MAX_PER_RUN", limit)

    tracks = [{"title": f"Song {i}", "artist": "Artist"} for i in range(limit + 2)]
    cache = {(t["title"], t["artist"]): (f"v{i}", 100) for i, t in enumerate(tracks)}

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", lambda yt, title, max_results=5: [])

    youtube_search_calls = []

    def fake_youtube_search(youtube, title, max_results=5):
        youtube_search_calls.append(title)
        return []

    monkeypatch.setattr(gst, "search_videos_by_title", fake_youtube_search)
    _no_commit(monkeypatch)

    gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, {})

    assert len(youtube_search_calls) == limit


def test_discover_cover_candidates_commits_in_batches_not_per_track(monkeypatch):
    batch_size = 3
    monkeypatch.setattr(gst, "COVER_DISCOVERY_COMMIT_BATCH_SIZE", batch_size)

    tracks = [{"title": f"Song {i}", "artist": "Artist"} for i in range(batch_size * 2 + 1)]
    cache = {(t["title"], t["artist"]): (f"v{i}", 100) for i, t in enumerate(tracks)}

    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", lambda yt, title, max_results=5: [])
    monkeypatch.setattr(gst, "search_videos_by_title", lambda *a, **k: [])
    monkeypatch.setattr(gst, "save_cover_candidates", lambda cc: None)
    commits = []
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: commits.append(1))

    gst.discover_cover_candidates(object(), object(), tracks, cache, 1_000_000, {})

    # batch_size*2+1曲 → batch_size件ずつ2回コミット + 最後の端数1件で1回、計3回
    assert len(commits) == 3


# --- コラボ・カバー候補の判定確定（run_cover_decide） ---


def test_run_cover_decide_marks_pending_candidate_as_yes_and_commits(monkeypatch):
    cover_candidates = {
        gst._track_key("廻廻奇譚", "Eve"): {
            "cand1": {"status": "pending", "candidate_title": "x", "candidate_channel": "y", "view_count": 100}
        }
    }
    saved = []
    committed = []
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: cover_candidates)
    monkeypatch.setattr(gst, "save_cover_candidates", lambda cc: saved.append(cc))
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: committed.append((paths, msg)))

    gst.run_cover_decide("cand1", "yes")

    entry = cover_candidates[gst._track_key("廻廻奇譚", "Eve")]["cand1"]
    assert entry["status"] == "yes"
    assert "fetched_at" in entry
    assert saved and committed
    assert committed[0][0] == [str(gst.COVER_CANDIDATES_FILE)]


def test_run_cover_decide_marks_pending_candidate_as_no(monkeypatch):
    cover_candidates = {gst._track_key("廻廻奇譚", "Eve"): {"cand1": {"status": "pending"}}}
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: cover_candidates)
    monkeypatch.setattr(gst, "save_cover_candidates", lambda cc: None)
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: None)

    gst.run_cover_decide("cand1", "no")

    assert cover_candidates[gst._track_key("廻廻奇譚", "Eve")]["cand1"]["status"] == "no"


def test_run_cover_decide_does_nothing_when_candidate_is_not_pending(monkeypatch):
    cover_candidates = {gst._track_key("廻廻奇譚", "Eve"): {"cand1": {"status": "yes"}}}
    committed = []
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: cover_candidates)
    monkeypatch.setattr(
        gst, "save_cover_candidates", lambda cc: (_ for _ in ()).throw(AssertionError("保存されるべきではない"))
    )
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: committed.append(1))

    gst.run_cover_decide("cand1", "no")  # 既にyes確定済みなので無視される

    assert cover_candidates[gst._track_key("廻廻奇譚", "Eve")]["cand1"]["status"] == "yes"
    assert committed == []


def test_run_cover_decide_does_nothing_when_video_id_unknown(monkeypatch):
    committed = []
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(
        gst, "save_cover_candidates", lambda cc: (_ for _ in ()).throw(AssertionError("保存されるべきではない"))
    )
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: committed.append(1))

    gst.run_cover_decide("unknown_video", "yes")

    assert committed == []


# --- cover_decideモードのエラー通知（post_channel_message / main） ---


def test_post_channel_message_sends_via_bot_token(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-bot-token")
    calls = []

    class _FakeResponse:
        status_code = 200
        text = ""

    def fake_post(url, headers=None, json=None):
        calls.append((url, headers, json))
        return _FakeResponse()

    monkeypatch.setattr(gst.requests, "post", fake_post)

    gst.post_channel_message("channel123", "エラーが発生しました")

    assert len(calls) == 1
    url, headers, payload = calls[0]
    assert url == f"{gst.DISCORD_API}/channels/channel123/messages"
    assert headers == {"Authorization": "Bot secret-bot-token"}
    assert payload == {"content": "エラーが発生しました"}


def test_main_cover_decide_reports_error_to_channel_on_failure(monkeypatch):
    monkeypatch.setenv("TASK_MODE", "cover_decide")
    monkeypatch.setenv("VIDEO_ID", "vid123")
    monkeypatch.setenv("DECISION", "yes")
    monkeypatch.setenv("CHANNEL_ID", "chan456")

    def fail_run_cover_decide(video_id, decision):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(gst, "run_cover_decide", fail_run_cover_decide)

    posted = []
    monkeypatch.setattr(gst, "post_channel_message", lambda channel_id, content: posted.append((channel_id, content)))

    with pytest.raises(RuntimeError):
        gst.main()

    assert posted == [("chan456", "コラボ・カバー候補の判定記録に失敗しました（video_id: vid123）: commit failed")]


def test_main_cover_decide_skips_notification_without_channel_id(monkeypatch):
    # channel_idが渡っていない（Worker側が古い等）場合は通知しようがないので静かに諦める
    monkeypatch.setenv("TASK_MODE", "cover_decide")
    monkeypatch.setenv("VIDEO_ID", "vid123")
    monkeypatch.setenv("DECISION", "yes")
    monkeypatch.delenv("CHANNEL_ID", raising=False)

    monkeypatch.setattr(
        gst, "run_cover_decide", lambda video_id, decision: (_ for _ in ()).throw(RuntimeError("commit failed"))
    )
    monkeypatch.setattr(
        gst, "post_channel_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("通知されるべきではない"))
    )

    with pytest.raises(RuntimeError):
        gst.main()


def test_main_cover_decide_does_not_post_when_successful(monkeypatch):
    monkeypatch.setenv("TASK_MODE", "cover_decide")
    monkeypatch.setenv("VIDEO_ID", "vid123")
    monkeypatch.setenv("DECISION", "yes")
    monkeypatch.setenv("CHANNEL_ID", "chan456")

    monkeypatch.setattr(gst, "run_cover_decide", lambda video_id, decision: None)
    monkeypatch.setattr(
        gst, "post_channel_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("通知されるべきではない"))
    )

    gst.main()  # 例外を投げなければOK


# --- Discordへの通知（はい/いいえボタン） ---


def test_build_cover_candidate_components_creates_numbered_yes_no_buttons():
    batch = [
        {"video_id": "v1", "track_title": "A", "track_artist": "a", "candidate_title": "c", "candidate_channel": "ch", "view_count": 1},
        {"video_id": "v2", "track_title": "B", "track_artist": "b", "candidate_title": "c", "candidate_channel": "ch", "view_count": 2},
    ]

    components = gst.build_cover_candidate_components(batch)

    assert len(components) == 2
    assert components[0]["components"][0]["custom_id"] == "covyes:v1"
    assert components[0]["components"][1]["custom_id"] == "covno:v1"
    assert components[0]["components"][0]["label"].startswith("1:")
    assert components[1]["components"][0]["custom_id"] == "covyes:v2"
    assert components[1]["components"][0]["label"].startswith("2:")


def test_send_cover_candidate_messages_batches_by_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(gst, "post_followup_payload", lambda app_id, token, payload: sent.append(payload))

    newly_found = [
        {
            "track_title": f"Song {i}",
            "track_artist": "Artist",
            "video_id": f"v{i}",
            "candidate_title": "c",
            "candidate_channel": "ch",
            "view_count": i,
        }
        for i in range(gst.COVER_CANDIDATES_PER_MESSAGE + 2)
    ]

    gst.send_cover_candidate_messages("app", "tok", newly_found)

    assert len(sent) == 2  # 件数がCOVER_CANDIDATES_PER_MESSAGEを超えるので2メッセージに分かれる
    assert len(sent[0]["components"]) == gst.COVER_CANDIDATES_PER_MESSAGE
    assert len(sent[1]["components"]) == 2
    assert "Song 0" in sent[0]["content"]


def test_run_score_notifies_discord_when_discovery_finds_a_new_cover_candidate(monkeypatch):
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Eve"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "廻廻奇譚", "artist": "Eve"}]

    # 前回までに自身の再生回数はキャッシュ済み（しきい値未満）という状況を再現
    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False, known_video_id=None):
        return cache[(title, artist)]

    def fake_youtube_search(youtube, title, max_results=5):
        return [("cover_v1", "廻廻奇譚 (Cover)", "Someone")]

    def fake_stats(youtube, video_ids):
        return {"cover_v1": 2_000_000}

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    # ytmusicapiでは見つからない状況を再現し、YouTube検索フォールバックで見つかることを確認する
    monkeypatch.setattr(gst, "search_videos_by_title_ytmusic", lambda yt, title, max_results=5: [])
    monkeypatch.setattr(gst, "search_videos_by_title", fake_youtube_search)
    monkeypatch.setattr(gst, "get_view_counts_for_video_ids", fake_stats)
    monkeypatch.setattr(gst, "save_cover_candidates", lambda cc: None)
    monkeypatch.setattr(gst, "commit_and_push", lambda paths, msg: None)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({("廻廻奇譚", "Eve"): ("own_v1", 500_000)}, {}))
    monkeypatch.setattr(gst, "load_cover_candidates", lambda: {})
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "build_ytmusic_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: None)
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)

    sent_payloads = []
    monkeypatch.setattr(gst, "post_followup_payload", lambda app_id, token, payload: sent_payloads.append(payload))
    monkeypatch.setattr(
        gst, "post_followup", lambda app_id, token, content: sent_payloads.append({"content": content})
    )

    gst.run_score(1000, "app", "tok")  # 1000万回再生以上（発見された候補は判定確定していないのでこの回では未反映）

    cover_messages = [p for p in sent_payloads if "covyes:cover_v1" in json.dumps(p.get("components", []))]
    assert len(cover_messages) == 1
    assert "廻廻奇譚" in cover_messages[0]["content"]
