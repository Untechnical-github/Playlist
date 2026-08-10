import argparse
import csv
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ytmusicapi import YTMusic

from scoring import TrackScore, normalize_view_counts

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("score_playlist")

MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 2.0
RATE_LIMIT_BACKOFF_SECONDS = 30.0  # 「Search Queries per minute」超過は数秒待っても解消しないため長めに待つ

# YouTube Data API の search.list は「1分あたりの検索回数」が厳しく制限されているため、
# 失敗してからリトライするのではなく、そもそもこの間隔を空けて呼び出しペースを落とす。
MIN_SEARCH_INTERVAL_SECONDS = float(os.environ.get("YOUTUBE_SEARCH_MIN_INTERVAL", "1.5"))
_last_search_call_time = 0.0

VIEW_CACHE_FILE = os.environ.get("YOUTUBE_VIEW_CACHE_FILE", "youtube_view_cache.json")
# 再生回数は基本的に増加のみで減らないため、一度調べた曲は期限を設けず恒久的にキャッシュから
# 再利用する（呼び出し元がしきい値付近の曲だけ force_refresh で明示的に再取得する）


def _throttle_search_calls() -> None:
    global _last_search_call_time
    now = time.monotonic()
    wait = MIN_SEARCH_INTERVAL_SECONDS - (now - _last_search_call_time)
    if wait > 0:
        time.sleep(wait)
    _last_search_call_time = time.monotonic()


def _is_daily_quota_exceeded(e: Exception) -> bool:
    """「Search Queries per minute」等は待てば回復するが、「...per day」の1日あたりクォータ超過は
    リセット（太平洋時間の深夜）まで絶対に回復しないため、区別してリトライを打ち切る。
    """
    return isinstance(e, HttpError) and e.resp is not None and e.resp.status == 429 and "per day" in str(e)


# このプロセス（＝GitHub Actionsの1回の実行）内で一度でも1日あたりのクォータ超過を検知したか。
# 実行が完走したように見えても、実は途中でクォータが尽きて一部の曲を確認できていない、という
# ことをDiscordのメッセージで利用者に伝えるために使う。プロセスごとにリセットされるので
# モジュールレベルのフラグで十分（呼び出し階層をまたいで持ち回す必要がない）。
_daily_quota_exceeded_seen = False


def was_daily_quota_exceeded() -> bool:
    """このプロセス内で1日あたりのクォータ超過を一度でも検知していればTrue。"""
    return _daily_quota_exceeded_seen


def retry(func, *args, **kwargs):
    """簡易リトライ：失敗するたびに待ち時間を伸ばしながら再試行し、それでも失敗したら例外を送出する。
    「Search Queries per minute」のようなレート制限（429）は数秒の待機では解消しないことが多いため、
    通常のAPIエラーより長めに待ってから再試行する。1日あたりのクォータ超過はいくら待っても
    回復しないため、即座に諦める（無駄な待機・API呼び出しを避けるため）。
    """
    global _daily_quota_exceeded_seen
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 呼び出し先のAPIエラーは種類を問わずリトライ対象にする
            last_error = e
            if _is_daily_quota_exceeded(e):
                _daily_quota_exceeded_seen = True
                logger.warning("Daily quota exceeded (%s); giving up without retrying", e)
                break
            if attempt == MAX_RETRIES:
                break
            if isinstance(e, HttpError) and e.resp is not None and e.resp.status == 429:
                wait = RATE_LIMIT_BACKOFF_SECONDS
            else:
                wait = RETRY_BACKOFF_SECONDS * attempt
            logger.warning("Attempt %d/%d failed (%s); retrying in %.0fs", attempt, MAX_RETRIES, e, wait)
            time.sleep(wait)
    assert last_error is not None
    raise last_error


def load_view_cache(path: str = VIEW_CACHE_FILE):
    """前回までに調べた再生回数をディスクから読み込み、同じ曲をAPIで調べ直すのを避ける。
    戻り値は (get_youtube_view_count にそのまま渡せるcache辞書, キーごとの取得日時) のタプル。
    """
    if not os.path.exists(path):
        return {}, {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cache: Dict[Tuple[str, str], Tuple[Optional[str], int]] = {}
    fetched_at: Dict[Tuple[str, str], float] = {}
    for key, entry in raw.items():
        title, artist = key.split("\x1f", 1)
        cache[(title, artist)] = (entry["video_id"], entry["view_count"])
        fetched_at[(title, artist)] = entry["fetched_at"]
    return cache, fetched_at


def save_view_cache(
    cache: Dict[Tuple[str, str], Tuple[Optional[str], int]],
    fetched_at: Dict[Tuple[str, str], float],
    path: str = VIEW_CACHE_FILE,
) -> None:
    """cacheの内容をディスクに保存する。load_view_cacheで読み込み済み(=今回APIを叩いていない)
    エントリは元のfetched_atを維持し、今回新たにAPIで取得したエントリだけ現在時刻にする。
    """
    now = time.time()
    raw = {
        f"{title}\x1f{artist}": {
            "video_id": video_id,
            "view_count": view_count,
            "fetched_at": fetched_at.get((title, artist), now),
        }
        for (title, artist), (video_id, view_count) in cache.items()
    }
    Path(path).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def build_ytmusic_client() -> YTMusic:
    """auth/headers_auth.json があれば使い、無ければ認証なし（限定公開/公開プレイリストのみ・
    検索は認証不要）でYTMusicクライアントを作る。"""
    auth_file = os.environ.get("YTMUSIC_AUTH_FILE", "auth/headers_auth.json")
    return YTMusic(auth_file) if os.path.exists(auth_file) else YTMusic()


def search_videos_by_title_ytmusic(yt: YTMusic, title: str, max_results: int = 5) -> list:
    """YouTube Data APIのクォータを消費せず、ytmusicapi経由でYouTube Musicのカタログ
    （"songs"）と動画（"videos"）の両方を曲名で検索する。カバー曲は公式カタログに乗っている
    ことも多いため"songs"も対象にする。[(video_id, title, artist_or_channel), ...] を返す。
    失敗しても例外を投げず、その検索区分の結果を空として続行する（ytmusicapiは非公式APIで
    エラーの型が一定しないため、個別の失敗で探索全体を止めないようbroadにキャッチする）。
    """
    results = []
    for search_filter in ("songs", "videos"):
        try:
            items = yt.search(title, filter=search_filter, limit=max_results)
        except Exception as e:  # noqa: BLE001 - 非公式APIのため例外の型を限定できない
            logger.warning('ytmusicapi search error for "%s" (filter=%s): %s', title, search_filter, e)
            continue
        for item in items or []:
            video_id = item.get("videoId")
            if not video_id:
                continue
            artists = [a.get("name", "") for a in (item.get("artists") or []) if a.get("name")]
            results.append((video_id, item.get("title") or "", artists[0] if artists else ""))
    return results


def fetch_playlist_tracks(playlist_id: str) -> list:
    """ytmusicapi でプレイリストの曲名・アーティスト名・video_id を取得する。

    ytmusicapiが返す各trackには、YouTube Music側で既に紐付いている`videoId`が含まれている。
    これは自分のプレイリストに入っている曲そのものなので、`get_youtube_view_count`に渡せば
    `search.list`での検索を省略でき、初回取得のクォータ消費を100分の1（`videos.list`の1のみ）に
    減らせる。
    """
    yt = build_ytmusic_client()

    data = yt.get_playlist(playlist_id, limit=None)
    tracks = []
    for item in data["tracks"]:
        artists = [a["name"] for a in (item.get("artists") or []) if a.get("name")]
        tracks.append(
            {
                "title": item.get("title") or "",
                "artist": artists[0] if artists else "",
                "video_id": item.get("videoId"),
            }
        )
    return tracks


def _fetch_stats_only(youtube, video_id: str, label: str) -> Optional[int]:
    """video_idが既知の動画について、search.listをやり直さずvideos.list（1クォータ）だけで
    再生回数を取得する。失敗したらNoneを返す（呼び出し元は既存の値を維持すること）。
    """
    try:
        stats_resp = retry(youtube.videos().list(id=video_id, part="statistics").execute)
        stats_items = stats_resp.get("items", [])
        return int(stats_items[0]["statistics"].get("viewCount", 0)) if stats_items else 0
    except HttpError as e:
        logger.warning('YouTube API error refreshing stats for "%s": %s', label, e)
        return None


def get_view_count_by_video_id(
    youtube,
    video_id: str,
    cache: Dict[Tuple[str, str], Tuple[Optional[str], int]],
    fetched_at: Optional[Dict[Tuple[str, str], float]] = None,
    force_refresh: bool = False,
) -> int:
    """video_idが既知の動画（コラボ・カバー候補として確定済みのもの等）の再生回数を返す。
    search.listは使わず、youtube_view_cache.json と同じcache辞書に間借りする合成キー
    ("__video__", video_id) で結果をキャッシュする（曲名・アーティスト名の組み合わせキーとは
    衝突しない）。減少時に既存値を維持する挙動は get_youtube_view_count と同じ。
    """
    key = ("__video__", video_id)
    previous = cache.get(key)
    if previous is not None and not force_refresh:
        return previous[1]

    view_count = _fetch_stats_only(youtube, video_id, video_id)
    if view_count is None:
        return previous[1] if previous is not None else 0

    if previous is not None and view_count < previous[1]:
        return previous[1]

    cache[key] = (video_id, view_count)
    if fetched_at is not None:
        fetched_at[key] = time.time()
    return view_count


def get_view_counts_for_video_ids(youtube, video_ids: list) -> Dict[str, int]:
    """複数のvideo_idの再生回数を1回のvideos.list呼び出し（カンマ区切り、最大50件、1クォータ）で
    まとめて取得する。キャッシュを経由せず一時的に必要なとき（コラボ・カバー候補の絞り込み等）に使う。
    """
    if not video_ids:
        return {}
    try:
        resp = retry(youtube.videos().list(id=",".join(video_ids), part="statistics").execute)
    except HttpError as e:
        logger.warning("YouTube API error fetching stats for %d video ids: %s", len(video_ids), e)
        return {}
    result: Dict[str, int] = {}
    for item in resp.get("items", []):
        video_id = item.get("id")
        if not video_id:
            continue
        result[video_id] = int(item.get("statistics", {}).get("viewCount", 0))
    return result


def search_videos_by_title(youtube, title: str, max_results: int = 5) -> list:
    """曲名だけ（アーティスト名を含めず）で検索し、[(video_id, video_title, channel_title), ...]を
    返す。get_youtube_view_countの「アーティスト名+曲名」検索より幅広い候補がヒットするため、
    コラボ・カバー候補の自動探索に使う。失敗したら空リストを返す。
    """
    def _search():
        _throttle_search_calls()
        return youtube.search().list(q=title, part="id,snippet", type="video", maxResults=max_results).execute()

    try:
        search_resp = retry(_search)
    except HttpError as e:
        logger.warning('YouTube API error searching by title "%s": %s', title, e)
        return []

    results = []
    for item in search_resp.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        results.append((video_id, snippet.get("title", ""), snippet.get("channelTitle", "")))
    return results


def get_youtube_view_count(
    youtube,
    title: str,
    artist: str,
    cache: Dict[Tuple[str, str], Tuple[Optional[str], int]],
    fetched_at: Optional[Dict[Tuple[str, str], float]] = None,
    force_refresh: bool = False,
    known_video_id: Optional[str] = None,
) -> Tuple[Optional[str], int]:
    """曲の再生回数を返す。既にcacheにあり force_refresh でなければAPIを呼ばずそれを返す。

    force_refresh=True で再取得した場合でも、新しく取得した値が既存のキャッシュ値より
    少なければ更新しない（動画差し替え等による一時的な減少でキャッシュを退行させないため。
    再生回数は基本的に増加のみで減らない前提）。

    force_refresh=True かつキャッシュに video_id が既知の場合は、search.list
    （100クォータ消費・呼び出し間隔の制限あり）をやり直さず、判明済みの video_id で
    videos.list（1クォータ）だけ叩いて統計情報を更新する。

    キャッシュに無い（初回取得の）場合でも、known_video_id（ytmusicapiが返す、自分の
    プレイリストの曲に既に紐付いているvideoId等）が分かっていれば同様にsearch.listを
    省略する。search.listが必要になるのは、キャッシュも known_video_id も無い場合だけ。
    """
    key = (title, artist)
    previous = cache.get(key)
    if previous is not None and not force_refresh:
        return previous

    query = f"{artist} {title}".strip()

    if force_refresh and previous is not None and previous[0] is not None:
        video_id = previous[0]
        view_count = _fetch_stats_only(youtube, video_id, query)
        if view_count is None:
            return previous
    elif previous is None and known_video_id:
        video_id = known_video_id
        view_count = _fetch_stats_only(youtube, video_id, query)
        if view_count is None:
            return (None, 0)
    else:
        def _search():
            _throttle_search_calls()
            return youtube.search().list(q=query, part="id", type="video", maxResults=1).execute()

        try:
            search_resp = retry(_search)
            items = search_resp.get("items", [])
            if not items:
                logger.info('YouTube: no match for "%s"', query)
                video_id, view_count = None, 0
            else:
                video_id = items[0]["id"]["videoId"]
                stats_resp = retry(youtube.videos().list(id=video_id, part="statistics").execute)
                stats_items = stats_resp.get("items", [])
                view_count = int(stats_items[0]["statistics"].get("viewCount", 0)) if stats_items else 0
        except HttpError as e:
            # APIエラー（クォータ超過等）は「見つからなかった」とは区別し、キャッシュには書き込まない。
            # ここで(None, 0)を保存してしまうと、恒久キャッシュの下では次回以降も再取得されず、
            # 一時的な障害のせいでその曲が永久に対象外扱いになってしまう。
            logger.warning('YouTube API error for "%s": %s', query, e)
            return previous if previous is not None else (None, 0)

    if previous is not None and view_count < previous[1]:
        logger.info(
            'YouTube: view count decreased for "%s" (%d -> %d); keeping cached value', query, previous[1], view_count
        )
        return previous

    cache[key] = (video_id, view_count)
    if fetched_at is not None:
        fetched_at[key] = time.time()
    return cache[key]


def build_youtube_client():
    api_key = os.environ["YOUTUBE_API_KEY"]
    return build("youtube", "v3", developerKey=api_key)


def write_results(results: list, output_path: str) -> None:
    rows = [asdict(r) for r in results]
    if output_path.endswith(".json"):
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        return

    fieldnames = list(rows[0].keys()) if rows else list(asdict(TrackScore("", "", None, 0, 0)).keys())
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="YouTube再生回数を基準にプレイリストの曲をスコアリング・ソートする"
    )
    parser.add_argument("playlist_id", help="YouTube Musicのプレイリストid")
    parser.add_argument("--output", default="scores.csv", help="出力ファイル（.csv または .json）")
    args = parser.parse_args()

    youtube = build_youtube_client()

    tracks = fetch_playlist_tracks(args.playlist_id)
    logger.info("Fetched %d tracks from playlist %s", len(tracks), args.playlist_id)

    youtube_cache, fetched_at = load_view_cache()

    raw: list = []
    for t in tracks:
        video_id, view_count = get_youtube_view_count(
            youtube, t["title"], t["artist"], youtube_cache, fetched_at, known_video_id=t.get("video_id")
        )
        raw.append((t, video_id, view_count))

    save_view_cache(youtube_cache, fetched_at)

    scores = normalize_view_counts([r[2] for r in raw])

    results = [
        TrackScore(
            title=t["title"],
            artist=t["artist"],
            video_id=video_id,
            view_count=view_count,
            score=round(score, 4),
        )
        for (t, video_id, view_count), score in zip(raw, scores)
    ]

    results.sort(key=lambda r: r.score, reverse=True)
    write_results(results, args.output)

    no_match = [r for r in results if r.video_id is None]
    logger.info("Wrote %d scored tracks to %s (%d with no YouTube match)", len(results), args.output, len(no_match))


if __name__ == "__main__":
    main()
