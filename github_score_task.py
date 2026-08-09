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
from score_playlist import (
    build_youtube_client,
    fetch_playlist_tracks,
    get_youtube_view_count,
    load_view_cache,
    save_view_cache,
)

DISCORD_API = "https://discord.com/api/v10"

TARGET_PLAYLIST_NAME = "Playlist"
EXCLUDED_TITLE_PREFIX = "Playlist"  # "Playlist"・"Playlist II" 等、集計先とその仲間は対象外
EXCLUDED_EXACT_TITLES = {"English Songs"}
VIEW_UNIT = 10_000  # しきい値は「万回再生」単位で指定される
MAX_MESSAGE_LENGTH = 1900  # Discordのメッセージ本文上限(2000文字)に余裕を持たせる

# しきい値のこの割合以上・未満（＝境界線付近）の曲だけ、キャッシュに値があっても再取得して
# 最新の再生回数で判定し直す。再生回数は基本的に増加のみで減らないため、しきい値を大きく
# 超えている曲や大きく下回っている曲は次回実行までに判定が変わる可能性が低く、再取得は無駄になる。
BORDERLINE_THRESHOLD_RATIO = 0.8

SCORE_OVERRIDES_FILE = Path("score_overrides.json")


def load_score_overrides() -> dict:
    """しきい値判定のときに、この曲自体のクレジットとは別に追加でチェックしたいアーティストの
    上書き設定。曲名にキーの文字列が含まれていたら、値に列挙されたアーティスト名それぞれで
    検索した再生回数も候補に加え、（曲自体の再生回数も含めた）最大値をしきい値判定に使う。

    想定するケース:
    - カバー曲: 曲自体（カバー動画）の再生回数だけでは元曲の人気度を反映しないため、
      元曲アーティスト名を追加する（例: "少女レイ": ["BUMP OF CHICKEN"]）。
    - 同じ曲に複数の公式動画/コラボ動画がある: 例えば「アカシア」はBUMP OF CHICKEN本人の
      動画とは別にPokémon公式のコラボ動画があるため、そちらも追加でチェックする
      （例: "アカシア": ["Pokémon"]）。どちらか一方でもしきい値を超えていれば対象にする。

    実際にPlaylistに追加するのは、あくまで曲自体（クレジット通り）の video_id。
    """
    if not SCORE_OVERRIDES_FILE.exists():
        return {}
    return json.loads(SCORE_OVERRIDES_FILE.read_text(encoding="utf-8"))


def resolve_override_artists(title: str, overrides: dict) -> list:
    """曲名にマッチした上書き設定から、追加でチェックすべきアーティスト名の一覧を返す。"""
    result = []
    for key, override_artists in overrides.items():
        if key in title:
            if isinstance(override_artists, str):
                result.append(override_artists)
            else:
                result.extend(override_artists)
    return result


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
    cache, fetched_at = load_view_cache()
    matches = []
    try:
        for t in all_tracks:
            override_artists = resolve_override_artists(t["title"], overrides)

            # 追加するのは常にこの曲自体（クレジット通りの動画）の video_id。しきい値の判定は、
            # この曲自体に加えて上書き設定の候補アーティストも調べ、最大の再生回数を採用する
            # （カバー元やコラボ版など、どれか1つでも超えていればよい）
            video_id, own_view_count = get_youtube_view_count(youtube, t["title"], t["artist"], cache, fetched_at)
            alt_view_counts = [
                get_youtube_view_count(youtube, t["title"], a, cache, fetched_at)[1] for a in override_artists
            ]
            view_count = max([own_view_count] + alt_view_counts)

            # 境界線付近（しきい値未満だがその80%以上）ならキャッシュ済みでも再取得し、
            # 取得し直した最新の値で判定する
            if view_count_threshold * BORDERLINE_THRESHOLD_RATIO <= view_count < view_count_threshold:
                video_id, own_view_count = get_youtube_view_count(
                    youtube, t["title"], t["artist"], cache, fetched_at, force_refresh=True
                )
                alt_view_counts = [
                    get_youtube_view_count(youtube, t["title"], a, cache, fetched_at, force_refresh=True)[1]
                    for a in override_artists
                ]
                view_count = max([own_view_count] + alt_view_counts)

            if video_id and view_count >= view_count_threshold:
                matches.append((t, video_id, view_count))
    finally:
        # 曲数が多いと検索だけで長時間かかるため、途中で失敗しても調べ終えた分は次回に持ち越す
        save_view_cache(cache, fetched_at)

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
