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

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False):
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
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
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


def test_resolve_override_artists_matches_on_title_substring():
    overrides = {"少女レイ": ["BUMP OF CHICKEN"], "アカシア": ["Pokémon", "映画版"]}
    assert gst.resolve_override_artists("少女レイ / 星街すいせい(Cover)", overrides) == ["BUMP OF CHICKEN"]
    assert gst.resolve_override_artists("アカシア - Acacia", overrides) == ["Pokémon", "映画版"]
    assert gst.resolve_override_artists("全く関係ない曲", overrides) == []


def test_resolve_override_artists_accepts_legacy_single_string_value():
    overrides = {"少女レイ": "BUMP OF CHICKEN"}
    assert gst.resolve_override_artists("少女レイ / 星街すいせい(Cover)", overrides) == ["BUMP OF CHICKEN"]


def test_run_score_uses_override_artist_view_count_but_adds_the_cover_track(monkeypatch):
    # カバー動画自体の再生回数はしきい値未満だが、元曲(BUMP)の再生回数は十分あるので、
    # 上書き設定によりカバー動画がPlaylistに追加されるべきケース
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Suisei"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "少女レイ / 星街すいせい(Cover)", "artist": "Suisei Hoshimachi"}]

    views = {
        ("少女レイ / 星街すいせい(Cover)", "Suisei Hoshimachi"): ("cover_v1", 100_000),
        ("少女レイ / 星街すいせい(Cover)", "BUMP OF CHICKEN"): ("orig_v1", 90_000_000),
    }

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(
        gst,
        "get_youtube_view_count",
        lambda youtube, title, artist, cache, fetched_at, force_refresh=False: views[(title, artist)],
    )
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append(video_id))
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "load_score_overrides", lambda: {"少女レイ": ["BUMP OF CHICKEN"]})

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score(1000, "app", "tok")  # 1000万回再生以上。カバー単体では満たさないがBUMP基準なら満たす

    assert added == ["cover_v1"]  # 追加されるのはあくまでカバー動画自体
    assert "少女レイ" in "".join(sent)


def test_run_score_qualifies_when_any_override_variant_exceeds_threshold(monkeypatch):
    # 「アカシア」はBUMP本人の動画としての再生回数はしきい値未満だが、Pokémon公式の
    # コラボ動画の再生回数がしきい値を超えているため、対象になるべきケース
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "BUMP OF CHICKEN"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "アカシア - Acacia", "artist": "BUMP OF CHICKEN"}]

    views = {
        ("アカシア - Acacia", "BUMP OF CHICKEN"): ("bump_v1", 1_000_000),
        ("アカシア - Acacia", "Pokémon"): ("poke_v1", 200_000_000),
    }

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(
        gst,
        "get_youtube_view_count",
        lambda youtube, title, artist, cache, fetched_at, force_refresh=False: views[(title, artist)],
    )
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append(video_id))
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "load_score_overrides", lambda: {"アカシア": ["Pokémon"]})

    sent = []
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: sent.append(content))

    gst.run_score(5000, "app", "tok")  # 5000万回再生以上。BUMP単体では満たさないがPokémon版なら満たす

    assert added == ["bump_v1"]  # 追加されるのはあくまでクレジット通りの動画（BUMP版）


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

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False):
        if force_refresh:
            force_refresh_calls.append((title, artist))
            return ("v_mid", 1_500_000)
        return initial_views[(title, artist)]

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: None)
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: None)

    gst.run_score(100, "app", "tok")  # しきい値1,000,000（境界線は800,000以上1,000,000未満）

    assert force_refresh_calls == [("Borderline", "Artist Mid")]


def test_run_score_force_refetches_override_artist_when_combined_view_count_is_borderline(monkeypatch):
    # カバー動画自体の再生回数だけでは境界線かどうか判断できないため、上書き設定のアーティストと
    # 合わせた最大値で境界線判定し、境界線ならカバー・元曲どちらも再取得すべきケース
    playlists = [
        {"id": "PL_SRC", "snippet": {"title": "Suisei"}},
        {"id": "PL_TARGET", "snippet": {"title": "Playlist"}},
    ]
    tracks = [{"title": "少女レイ / 星街すいせい(Cover)", "artist": "Suisei Hoshimachi"}]

    initial_views = {
        ("少女レイ / 星街すいせい(Cover)", "Suisei Hoshimachi"): ("cover_v1", 100_000),
        ("少女レイ / 星街すいせい(Cover)", "BUMP OF CHICKEN"): ("orig_v1", 900_000),  # しきい値の90%
    }

    force_refresh_artists = []

    def fake_get_youtube_view_count(youtube, title, artist, cache, fetched_at, force_refresh=False):
        if force_refresh:
            force_refresh_artists.append(artist)
            if artist == "BUMP OF CHICKEN":
                return ("orig_v1", 1_200_000)  # 再取得後はしきい値超え
        return initial_views[(title, artist)]

    added = []

    monkeypatch.setattr(gst, "fetch_playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(gst, "get_youtube_view_count", fake_get_youtube_view_count)
    monkeypatch.setattr(gst, "load_view_cache", lambda: ({}, {}))
    monkeypatch.setattr(gst, "save_view_cache", lambda cache, fetched_at: None)
    monkeypatch.setattr(gst, "build_youtube_client", lambda: object())
    monkeypatch.setattr(gst, "get_auth_header", lambda: "Bearer fake")
    monkeypatch.setattr(gst, "list_my_playlists", lambda auth_header: playlists)
    monkeypatch.setattr(gst, "list_playlist_items", lambda auth_header, playlist_id: [])
    monkeypatch.setattr(gst, "add_playlist_item", lambda auth_header, playlist_id, video_id: added.append(video_id))
    monkeypatch.setattr(gst, "remove_playlist_item", lambda auth_header, item_id: None)
    monkeypatch.setattr(gst, "load_score_overrides", lambda: {"少女レイ": ["BUMP OF CHICKEN"]})
    monkeypatch.setattr(gst, "post_followup", lambda app_id, token, content: None)

    gst.run_score(100, "app", "tok")  # しきい値1,000,000

    assert set(force_refresh_artists) == {"Suisei Hoshimachi", "BUMP OF CHICKEN"}
    assert added == ["cover_v1"]  # 再取得後の元曲再生回数が超えたため対象になる
