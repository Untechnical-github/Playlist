import argparse
import csv
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ytmusicapi import YTMusic

from scoring import TrackScore, normalize_view_counts

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("score_playlist")

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


def retry(func, *args, **kwargs):
    """簡易リトライ：失敗するたびに待ち時間を伸ばしながら再試行し、それでも失敗したら例外を送出する。"""
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 呼び出し先のAPIエラーは種類を問わずリトライ対象にする
            last_error = e
            if attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_SECONDS * attempt
            logger.warning("Attempt %d/%d failed (%s); retrying in %.0fs", attempt, MAX_RETRIES, e, wait)
            time.sleep(wait)
    assert last_error is not None
    raise last_error


def fetch_playlist_tracks(playlist_id: str) -> list:
    """ytmusicapi でプレイリストの曲名・アーティスト名を取得する。
    auth/headers_auth.json があれば使い、無ければ認証なし（限定公開/公開プレイリストのみ）で取得する。
    """
    auth_file = os.environ.get("YTMUSIC_AUTH_FILE", "auth/headers_auth.json")
    yt = YTMusic(auth_file) if os.path.exists(auth_file) else YTMusic()

    data = yt.get_playlist(playlist_id, limit=None)
    tracks = []
    for item in data["tracks"]:
        artists = [a["name"] for a in (item.get("artists") or []) if a.get("name")]
        tracks.append({"title": item.get("title") or "", "artist": artists[0] if artists else ""})
    return tracks


def get_youtube_view_count(
    youtube, title: str, artist: str, cache: Dict[Tuple[str, str], Tuple[Optional[str], int]]
) -> Tuple[Optional[str], int]:
    key = (title, artist)
    if key in cache:
        return cache[key]

    query = f"{artist} {title}".strip()
    try:
        search_resp = retry(
            youtube.search().list(q=query, part="id", type="video", maxResults=1).execute
        )
        items = search_resp.get("items", [])
        if not items:
            logger.info('YouTube: no match for "%s"', query)
            cache[key] = (None, 0)
            return cache[key]

        video_id = items[0]["id"]["videoId"]
        stats_resp = retry(youtube.videos().list(id=video_id, part="statistics").execute)
        stats_items = stats_resp.get("items", [])
        view_count = int(stats_items[0]["statistics"].get("viewCount", 0)) if stats_items else 0
        cache[key] = (video_id, view_count)
    except HttpError as e:
        logger.warning('YouTube API error for "%s": %s', query, e)
        cache[key] = (None, 0)
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

    youtube_cache: Dict[Tuple[str, str], Tuple[Optional[str], int]] = {}

    raw: list = []
    for t in tracks:
        video_id, view_count = get_youtube_view_count(youtube, t["title"], t["artist"], youtube_cache)
        raw.append((t, video_id, view_count))

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
