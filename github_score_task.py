import os

import requests

from core.auth import get_auth_header
from core.youtube_api import add_playlist_item, list_my_playlists, list_playlist_items
from score_playlist import build_youtube_client, fetch_playlist_tracks, get_youtube_view_count

DISCORD_API = "https://discord.com/api/v10"

TARGET_PLAYLIST_NAME = "Playlist"
VIEW_UNIT = 10_000  # しきい値は「万回再生」単位で指定される
MAX_MESSAGE_LENGTH = 1900  # Discordのメッセージ本文上限(2000文字)に余裕を持たせる


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


def run_score(playlist_id: str, threshold: int, application_id: str, interaction_token: str) -> None:
    youtube = build_youtube_client()
    tracks = fetch_playlist_tracks(playlist_id)

    if not tracks:
        post_followup(application_id, interaction_token, "曲がありませんでした。")
        return

    view_count_threshold = threshold * VIEW_UNIT
    cache: dict = {}
    matches = []
    for t in tracks:
        video_id, view_count = get_youtube_view_count(youtube, t["title"], t["artist"], cache)
        if video_id and view_count >= view_count_threshold:
            matches.append((t, video_id, view_count))

    auth_header = get_auth_header()
    target_playlist_id = find_playlist_id_by_title(auth_header, TARGET_PLAYLIST_NAME)
    if target_playlist_id is None:
        post_followup(
            application_id,
            interaction_token,
            f"「{TARGET_PLAYLIST_NAME}」という名前のプレイリストが見つかりませんでした。",
        )
        return

    existing_video_ids = {
        it["contentDetails"]["videoId"] for it in list_playlist_items(auth_header, target_playlist_id)
    }

    newly_added = []
    for t, video_id, view_count in matches:
        if video_id in existing_video_ids:
            continue
        add_playlist_item(auth_header, target_playlist_id, video_id)
        existing_video_ids.add(video_id)
        newly_added.append((t, view_count))

    if not newly_added:
        post_followup(
            application_id,
            interaction_token,
            f"しきい値（{threshold}万回再生以上）を満たす新しい曲はありませんでした"
            f"（対象{len(matches)}曲は既に追加済みです）。",
        )
        return

    header = (
        f"**「{TARGET_PLAYLIST_NAME}」に{len(newly_added)}曲を新規追加しました**"
        f"（しきい値: {threshold}万回再生以上）\n"
    )
    lines = [f"・{t['title']} - {t['artist']}（views: {view_count:,}）" for t, view_count in newly_added]
    send_paginated_message(application_id, interaction_token, header, lines)


def main() -> None:
    playlist_id = os.environ["PLAYLIST_ID"]
    threshold = int(os.environ["THRESHOLD"])
    application_id = os.environ["APPLICATION_ID"]
    interaction_token = os.environ["INTERACTION_TOKEN"]

    try:
        run_score(playlist_id, threshold, application_id, interaction_token)
    except Exception as e:
        try:
            post_followup(application_id, interaction_token, f"エラーが発生しました: {e}")
        except requests.RequestException:
            pass
        raise


if __name__ == "__main__":
    main()
