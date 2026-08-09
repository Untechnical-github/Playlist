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


def _http_error(status):
    return HttpError(_FakeResp(status), b"error body")


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


def test_view_cache_drops_entries_older_than_ttl(tmp_path):
    path = str(tmp_path / "cache.json")
    cache = {("Old Song", "Old Artist"): ("v_old", 999)}
    stale_fetched_at = {("Old Song", "Old Artist"): time.time() - sp.CACHE_TTL_SECONDS - 3600}

    sp.save_view_cache(cache, stale_fetched_at, path=path)
    loaded_cache, _ = sp.load_view_cache(path=path)

    assert loaded_cache == {}


def test_view_cache_keeps_original_fetched_at_for_untouched_entries(tmp_path):
    path = str(tmp_path / "cache.json")
    old_time = time.time() - 3600
    cache = {("Song A", "Artist A"): ("v1", 100)}
    fetched_at = {("Song A", "Artist A"): old_time}

    sp.save_view_cache(cache, fetched_at, path=path)
    _, reloaded_fetched_at = sp.load_view_cache(path=path)

    assert abs(reloaded_fetched_at[("Song A", "Artist A")] - old_time) < 1e-6
