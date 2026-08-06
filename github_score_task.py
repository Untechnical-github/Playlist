import json
import os
from pathlib import Path

import requests

from core.auth import get_auth_header
from core.youtube_api import (
    add_playlist_item,
    list_my_playlists,
    list_playlist_items,
    remove_playlist_item,
)
from score_playlist import build_youtube_client, fetch_playlist_tracks, get_youtube_view_count

DISCORD_API = "https://discord.com/api/v10"

TARGET_PLAYLIST_NAME = "Playlist"
EXCLUDED_TITLE_PREFIX = "Playlist"  # "Playlist"・"Playlist II" 等、集計先とその仲間は対象外
EXCLUDED_EXACT_TITLES = {"English Songs"}
VIEW_UNIT = 10_000  # しきい値は「万回再生」単位で指定される
MAX_MESSAGE_LENGTH = 1900  # Discordのメッセージ本文上限(2000文字)に余裕を持たせる

SCORE_OVERRIDES_FILE = Path("score_overrides.json")


def load_score_overrides() -> dict:
    """カバー曲などで、しきい値判定には元曲のアーティストの再生回数を使いたい場合の上書き設定。
    曲名にキーの文字列が含まれていたら、そのアーティスト名で検索した再生回数をしきい値判定に使う
    （実際にPlaylistに追加するのは、あくまで元の曲=カバー動画自体）。
    """
    if not SCORE_OVERRIDES_FILE.exists():
        return {}
    return json.loads(SCORE_OVERRIDES_FILE.read_text(encoding="utf-8"))


def resolve_override_artist(title: str, overrides: dict):
    for key, override_artist in overrides.items():
        if key in title:
            return override_artist
    return None


def _raise_with_body(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} error from Discord: {resp.text}", response=resp
        )


def post_followup(application_id: str, interaction_token: str, content: str) -> None:
    resp = requests.post(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}",
        json={"content": content},
    )
    _raise_with_body(resp)


def send_paginated_message(application_id: str, interaction_token: str, header: str, lines: list) -> None:
    """本文が長すぎる場合は複数メッセージに分割して送る（何も省略しない）。"""
    chunks = []
    current = header
    for line in lines:
        candidate = f"{current}{line}\n"
        if len(candidate) > MAX_MESSAGE_LENGTH and current != header:
            chunks.append(current)
            current = f"{line}\n"
        else:
            current = candidate
    chunks.append(current)

    for chunk in chunks:
        post_followup(application_id, interaction_token, chunk)


def find_playlist_id_by_title(auth_header: str, title: str):
    for p in list_my_playlists(auth_header):
        if p["snippet"]["title"] == title:
            return p["id"]
    return None


def get_source_playlists(auth_header: str) -> list:
    """集計対象のプレイリスト一覧を返す。「Playlist」で始まる名前（集計先とその仲間）と
    「English Songs」は対象外にする。
    """
    return [
        p
        for p in list_my_playlists(auth_header)
        if not p["snippet"]["title"].startswith(EXCLUDED_TITLE_PREFIX)
        and p["snippet"]["title"] not in EXCLUDED_EXACT_TITLES
    ]


def run_score(threshold: int, application_id: str, interaction_token: str) -> None:
    auth_header = get_auth_header()

    source_playlists = get_source_playlists(auth_header)
    if not source_playlists:
        post_followup(application_id, interaction_token, "集計対象になるプレイリストがありませんでした。")
        return

    all_tracks = []
    for p in source_playlists:
        all_tracks.extend(fetch_playlist_tracks(p["id"]))

    if not all_tracks:
        post_followup(application_id, interaction_token, "曲がありませんでした。")
        return

    youtube = build_youtube_client()
    overrides = load_score_overrides()
    view_count_threshold = threshold * VIEW_UNIT
    cache: dict = {}
    matches = []
    for t in all_tracks:
        # 追加するのは常にこの曲自体（カバー動画等）の video_id
        video_id, own_view_count = get_youtube_view_count(youtube, t["title"], t["artist"], cache)

        # しきい値の判定だけは、上書き設定があれば元曲アーティストの再生回数を使う
        override_artist = resolve_override_artist(t["title"], overrides)
        if override_artist:
            _, view_count = get_youtube_view_count(youtube, t["title"], override_artist, cache)
        else:
            view_count = own_view_count

        if video_id and view_count >= view_count_threshold:
            matches.append((t, video_id, view_count))

    target_playlist_id = find_playlist_id_by_title(auth_header, TARGET_PLAYLIST_NAME)
    if target_playlist_id is None:
        post_followup(
            application_id,
            interaction_token,
            f"「{TARGET_PLAYLIST_NAME}」という名前のプレイリストが見つかりませんでした。",
        )
        return

    # 削除判定のため、追加・削除どちらも行う前の「今の Playlist の中身」を確定させておく
    original_items = list_playlist_items(auth_header, target_playlist_id)
    original_by_video_id = {it["contentDetails"]["videoId"]: it for it in original_items}
    qualifying_video_ids = {video_id for _, video_id, _ in matches}

    newly_added = []
    for t, video_id, view_count in matches:
        if video_id in original_by_video_id:
            continue
        add_playlist_item(auth_header, target_playlist_id, video_id)
        newly_added.append((t, view_count))

    removed = []
    for video_id, item in original_by_video_id.items():
        if video_id in qualifying_video_ids:
            continue
        remove_playlist_item(auth_header, item["id"])
        removed.append(item["snippet"].get("title", video_id))

    if not newly_added and not removed:
        post_followup(
            application_id,
            interaction_token,
            f"変更はありませんでした（しきい値: {threshold}万回再生以上、"
            f"集計対象プレイリスト{len(source_playlists)}件、対象{len(matches)}曲）。",
        )
        return

    header = (
        f"**「{TARGET_PLAYLIST_NAME}」を更新しました**"
        f"（しきい値: {threshold}万回再生以上、集計対象プレイリスト{len(source_playlists)}件、"
        f"追加{len(newly_added)}曲・削除{len(removed)}曲）\n"
    )
    lines = [f"・追加: {t['title']} - {t['artist']}（views: {view_count:,}）" for t, view_count in newly_added]
    lines += [f"・削除: {title}（しきい値未満になったため）" for title in removed]
    send_paginated_message(application_id, interaction_token, header, lines)


def main() -> None:
    threshold = int(os.environ["THRESHOLD"])
    application_id = os.environ["APPLICATION_ID"]
    interaction_token = os.environ["INTERACTION_TOKEN"]

    try:
        run_score(threshold, application_id, interaction_token)
    except Exception as e:
        try:
            post_followup(application_id, interaction_token, f"エラーが発生しました: {e}")
        except requests.RequestException:
            pass
        raise


if __name__ == "__main__":
    main()
