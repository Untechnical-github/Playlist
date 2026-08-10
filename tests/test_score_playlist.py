import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from googleapiclient.errors import HttpError

import score_playlist as sp


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def _http_error(status, content: bytes = b"error body"):
    return HttpError(_FakeResp(status), content)


def _daily_quota_error():
    return _http_error(
        429,
        b"Quota exceeded for quota metric 'Search Queries' and limit 'Search Queries per day' "
        b"of service 'youtube.googleapis.com'",
    )


class _FakeYouTube:
    """search().list().execute() / videos().list().execute() の連鎖を模倣するフェイク。
    view_countsに渡した値を呼び出しごとに1つずつ順番に返す（常に1件ヒットする想定）。
    """

    def __init__(self, view_counts, video_id="vid"):
        self._view_counts = list(view_counts)
        self._video_id = video_id
        self.search_call_count = 0

    def search(self):
        outer = self

        class _List:
            def list(self, **kwargs):
                class _Exec:
                    def execute(self_inner):
                        outer.search_call_count += 1
                        return {"items": [{"id": {"videoId": outer._video_id}}]}

                return _Exec()

        return _List()

    def videos(self):
        outer = self

        class _List:
            def list(self, **kwargs):
                class _Exec:
                    def execute(self_inner):
                        view_count = outer._view_counts.pop(0)
                        return {"items": [{"statistics": {"viewCount": str(view_count)}}]}

                return _Exec()

        return _List()


def test_retry_returns_result_once_underlying_call_succeeds(monkeypatch):
    monkeypatch.setattr(sp.time, "sleep", lambda seconds: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(500)
        return "ok"

    assert sp.retry(flaky) == "ok"
    assert calls["n"] == 3


def test_retry_waits_longer_on_rate_limit_than_on_other_errors(monkeypatch):
    waits = []
    monkeypatch.setattr(sp.time, "sleep", lambda seconds: waits.append(seconds))

    def always_rate_limited():
        raise _http_error(429)

    try:
        sp.retry(always_rate_limited)
    except HttpError:
        pass

    assert all(w == sp.RATE_LIMIT_BACKOFF_SECONDS for w in waits)

    waits.clear()

    def always_server_error():
        raise _http_error(500)

    try:
        sp.retry(always_server_error)
    except HttpError:
        pass

    assert all(w < sp.RATE_LIMIT_BACKOFF_SECONDS for w in waits)


def test_retry_gives_up_immediately_on_daily_quota_exceeded_without_sleeping(monkeypatch):
    monkeypatch.setattr(sp.time, "sleep", lambda seconds: (_ for _ in ()).throw(AssertionError("must not sleep")))
    calls = {"n": 0}

    def always_daily_quota_exceeded():
        calls["n"] += 1
        raise _daily_quota_error()

    try:
        sp.retry(always_daily_quota_exceeded)
    except HttpError:
        pass

    assert calls["n"] == 1  # 1日あたりのクォータ超過は待っても回復しないためリトライしない


def test_throttle_search_calls_sleeps_to_maintain_minimum_interval(monkeypatch):
    monkeypatch.setattr(sp, "_last_search_call_time", 100.0)
    monkeypatch.setattr(sp.time, "monotonic", lambda: 100.2)
    slept = []
    monkeypatch.setattr(sp.time, "sleep", lambda seconds: slept.append(seconds))

    sp._throttle_search_calls()

    assert slept and abs(slept[0] - (sp.MIN_SEARCH_INTERVAL_SECONDS - 0.2)) < 1e-9


def test_view_cache_round_trip_preserves_entries(tmp_path):
    path = str(tmp_path / "cache.json")
    cache = {("Song A", "Artist A"): ("v1", 12345)}
    fetched_at = {}

    sp.save_view_cache(cache, fetched_at, path=path)
    loaded_cache, loaded_fetched_at = sp.load_view_cache(path=path)

    assert loaded_cache == cache
    assert ("Song A", "Artist A") in loaded_fetched_at


def test_view_cache_keeps_very_old_entries_since_cache_has_no_ttl(tmp_path):
    path = str(tmp_path / "cache.json")
    cache = {("Old Song", "Old Artist"): ("v_old", 999)}
    old_fetched_at = {("Old Song", "Old Artist"): time.time() - 365 * 24 * 3600}

    sp.save_view_cache(cache, old_fetched_at, path=path)
    loaded_cache, _ = sp.load_view_cache(path=path)

    assert loaded_cache == cache


def test_view_cache_keeps_original_fetched_at_for_untouched_entries(tmp_path):
    path = str(tmp_path / "cache.json")
    old_time = time.time() - 3600
    cache = {("Song A", "Artist A"): ("v1", 100)}
    fetched_at = {("Song A", "Artist A"): old_time}

    sp.save_view_cache(cache, fetched_at, path=path)
    _, reloaded_fetched_at = sp.load_view_cache(path=path)

    assert abs(reloaded_fetched_at[("Song A", "Artist A")] - old_time) < 1e-6


def test_get_youtube_view_count_uses_cache_without_calling_api_when_not_forced(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[999_999])  # 呼ばれたら明らかにおかしい値
    cache = {("Song A", "Artist A"): ("cached_v", 1_000)}
    fetched_at = {}

    result = sp.get_youtube_view_count(youtube, "Song A", "Artist A", cache, fetched_at)

    assert result == ("cached_v", 1_000)
    assert youtube.search_call_count == 0
    assert fetched_at == {}  # キャッシュを使っただけなので取得日時は更新されない


def test_get_youtube_view_count_force_refresh_updates_when_view_count_increases(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[2_000])
    cache = {("Song A", "Artist A"): ("v1", 1_000)}
    fetched_at = {}

    result = sp.get_youtube_view_count(youtube, "Song A", "Artist A", cache, fetched_at, force_refresh=True)

    assert result == ("v1", 2_000)  # video_idは既知なので維持し、統計だけ更新する
    assert cache[("Song A", "Artist A")] == ("v1", 2_000)
    assert ("Song A", "Artist A") in fetched_at


def test_get_youtube_view_count_force_refresh_with_known_video_id_skips_search(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[3_000])
    cache = {("Song A", "Artist A"): ("v1", 1_000)}
    fetched_at = {}

    sp.get_youtube_view_count(youtube, "Song A", "Artist A", cache, fetched_at, force_refresh=True)

    assert youtube.search_call_count == 0  # video_id既知なのでsearch.listはやり直さない


def test_get_youtube_view_count_force_refresh_falls_back_to_search_when_video_id_unknown(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[3_000])
    cache = {("Song A", "Artist A"): (None, 0)}  # 前回はヒットなし
    fetched_at = {}

    result = sp.get_youtube_view_count(youtube, "Song A", "Artist A", cache, fetched_at, force_refresh=True)

    assert result == ("vid", 3_000)  # video_id不明だったので検索からやり直す
    assert youtube.search_call_count == 1


def test_get_youtube_view_count_force_refresh_keeps_cached_value_when_view_count_decreases(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[500])  # 動画差し替え等による一時的な減少を想定
    cache = {("Song A", "Artist A"): ("v1", 1_000)}
    fetched_at = {("Song A", "Artist A"): 123.0}

    result = sp.get_youtube_view_count(youtube, "Song A", "Artist A", cache, fetched_at, force_refresh=True)

    assert result == ("v1", 1_000)  # 古い（大きい）値を維持
    assert cache[("Song A", "Artist A")] == ("v1", 1_000)
    assert fetched_at[("Song A", "Artist A")] == 123.0  # 更新しなかったので取得日時もそのまま


class _SearchFailsYouTube:
    """search.list().execute() が常にHttpErrorを送出するフェイク（クォータ超過等を想定）。"""

    def __init__(self, error):
        self._error = error

    def search(self):
        outer = self

        class _List:
            def list(self, **kwargs):
                class _Exec:
                    def execute(self_inner):
                        raise outer._error

                return _Exec()

        return _List()


class _VideosFailsYouTube:
    """videos.list().execute()（統計のみ再取得）が常にHttpErrorを送出するフェイク。"""

    def __init__(self, error):
        self._error = error

    def videos(self):
        outer = self

        class _List:
            def list(self, **kwargs):
                class _Exec:
                    def execute(self_inner):
                        raise outer._error

                return _Exec()

        return _List()


def test_get_youtube_view_count_does_not_poison_cache_on_api_error_with_no_previous_value(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _SearchFailsYouTube(_daily_quota_error())
    cache = {}
    fetched_at = {}

    result = sp.get_youtube_view_count(youtube, "Vaundy 不可幸力", "Vaundy", cache, fetched_at)

    assert result == (None, 0)
    assert cache == {}  # 一時的なAPIエラーを「見つからなかった」としてキャッシュに固定しない
    assert fetched_at == {}


def test_get_youtube_view_count_returns_previous_on_api_error_without_touching_cache(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _VideosFailsYouTube(_daily_quota_error())
    cache = {("Song A", "Artist A"): ("v1", 1_000)}
    fetched_at = {("Song A", "Artist A"): 123.0}

    result = sp.get_youtube_view_count(youtube, "Song A", "Artist A", cache, fetched_at, force_refresh=True)

    assert result == ("v1", 1_000)
    assert cache[("Song A", "Artist A")] == ("v1", 1_000)
    assert fetched_at[("Song A", "Artist A")] == 123.0


def test_get_youtube_view_count_uses_known_video_id_to_skip_search_on_first_fetch(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[4_000_000])
    cache = {}
    fetched_at = {}

    result = sp.get_youtube_view_count(
        youtube, "Song A", "Artist A", cache, fetched_at, known_video_id="known_v1"
    )

    assert result == ("known_v1", 4_000_000)
    assert cache[("Song A", "Artist A")] == ("known_v1", 4_000_000)
    assert youtube.search_call_count == 0  # ytmusicapiで既知のvideo_idなのでsearch.listは不要
    assert ("Song A", "Artist A") in fetched_at


def test_get_youtube_view_count_prefers_existing_cache_over_known_video_id(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[999_999])  # 呼ばれたら明らかにおかしい値
    cache = {("Song A", "Artist A"): ("cached_v", 1_000)}
    fetched_at = {}

    result = sp.get_youtube_view_count(
        youtube, "Song A", "Artist A", cache, fetched_at, known_video_id="different_v"
    )

    assert result == ("cached_v", 1_000)
    assert youtube.search_call_count == 0


def test_get_youtube_view_count_known_video_id_does_not_poison_cache_on_api_error(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _VideosFailsYouTube(_daily_quota_error())
    cache = {}
    fetched_at = {}

    result = sp.get_youtube_view_count(
        youtube, "Song A", "Artist A", cache, fetched_at, known_video_id="known_v1"
    )

    assert result == (None, 0)
    assert cache == {}


class _FakeTitleSearchYouTube:
    """search().list(part="id,snippet", ...).execute() が複数件返すフェイク（曲名のみ検索用）。"""

    def __init__(self, items):
        self._items = items
        self.search_call_count = 0

    def search(self):
        outer = self

        class _List:
            def list(self, **kwargs):
                class _Exec:
                    def execute(self_inner):
                        outer.search_call_count += 1
                        return {
                            "items": [
                                {"id": {"videoId": vid}, "snippet": {"title": title, "channelTitle": channel}}
                                for vid, title, channel in outer._items
                            ]
                        }

                return _Exec()

        return _List()


def test_search_videos_by_title_returns_id_title_channel_for_each_result(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeTitleSearchYouTube(
        [("v1", "廻廻奇譚 (Cover)", "Some Channel"), ("v2", "廻廻奇譚 - Eve", "Eve Official")]
    )

    results = sp.search_videos_by_title(youtube, "廻廻奇譚")

    assert results == [("v1", "廻廻奇譚 (Cover)", "Some Channel"), ("v2", "廻廻奇譚 - Eve", "Eve Official")]
    assert youtube.search_call_count == 1


def test_search_videos_by_title_returns_empty_list_on_api_error(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _SearchFailsYouTube(_daily_quota_error())

    assert sp.search_videos_by_title(youtube, "廻廻奇譚") == []


def test_get_view_count_by_video_id_fetches_and_caches_under_synthetic_key(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[5_000_000])
    cache = {}
    fetched_at = {}

    result = sp.get_view_count_by_video_id(youtube, "abc123", cache, fetched_at)

    assert result == 5_000_000
    assert cache[("__video__", "abc123")] == ("abc123", 5_000_000)
    assert ("__video__", "abc123") in fetched_at


def test_get_view_count_by_video_id_uses_cache_without_calling_api_when_not_forced(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[999_999_999])  # 呼ばれたら明らかにおかしい値
    cache = {("__video__", "abc123"): ("abc123", 1_000)}
    fetched_at = {}

    assert sp.get_view_count_by_video_id(youtube, "abc123", cache, fetched_at) == 1_000


def test_get_view_count_by_video_id_force_refresh_keeps_value_when_decreased(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _FakeYouTube(view_counts=[500])
    cache = {("__video__", "abc123"): ("abc123", 1_000)}
    fetched_at = {}

    result = sp.get_view_count_by_video_id(youtube, "abc123", cache, fetched_at, force_refresh=True)

    assert result == 1_000
    assert cache[("__video__", "abc123")] == ("abc123", 1_000)


def test_get_view_count_by_video_id_returns_zero_when_never_cached_and_api_fails(monkeypatch):
    monkeypatch.setattr(sp, "_throttle_search_calls", lambda: None)
    youtube = _VideosFailsYouTube(_daily_quota_error())
    cache = {}
    fetched_at = {}

    result = sp.get_view_count_by_video_id(youtube, "abc123", cache, fetched_at)

    assert result == 0
    assert cache == {}  # 未確定なのでキャッシュに固定しない
