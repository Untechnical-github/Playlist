import os
import sys

import requests

from core.apply import apply_updates, compute_position_updates
from core.auth import get_auth_header
from core.fetch import get_playlist_tracks
from core.planner import GroupedPlan, group_tracks
from core.youtube_api import ManualSortRequiredError, get_playlist_title

DISCORD_API = "https://discord.com/api/v10"


def _raise_with_body(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} error from Discord: {resp.text}", response=resp
        )


def post_followup(application_id: str, interaction_token: str, payload: dict) -> None:
    resp = requests.post(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}", json=payload
    )
    _raise_with_body(resp)


def edit_original(application_id: str, interaction_token: str, payload: dict) -> None:
    resp = requests.patch(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}/messages/@original",
        json=payload,
    )
    _raise_with_body(resp)


# Discord embed の実際の上限（フィールド数25、フィールド値1024文字、埋め込み合計6000文字）に
# 収まるよう、余裕を持たせた予算で切り詰める。
MAX_FIELDS = 25
MAX_FIELD_VALUE = 1024
MAX_EMBED_TOTAL = 5500


def build_embed(title: str, grouped: GroupedPlan) -> dict:
    embed_title = f"並び替えプレビュー: {title}"
    fields = []
    total_len = len(embed_title)
    omitted_blocks = 0

    for artist, tracks in grouped.blocks:
        name = f"{artist}（{len(tracks)}曲）"
        value = "\n".join(f"・{t.title}" for t in tracks)
        if len(value) > MAX_FIELD_VALUE - 10:
            value = value[: MAX_FIELD_VALUE - 10] + "\n…"
        entry_len = len(name) + len(value)
        # 末尾用の枠を1つ残しておく
        if len(fields) >= MAX_FIELDS - 1 or total_len + entry_len > MAX_EMBED_TOTAL:
            omitted_blocks += 1
            continue
        fields.append({"name": name, "value": value, "inline": False})
        total_len += entry_len

    if omitted_blocks:
        fields.append(
            {
                "name": "…ほか",
                "value": f"表示しきれなかった塊が {omitted_blocks} 件あります",
                "inline": False,
            }
        )

    if grouped.tail:
        name = f"単独曲・不明（{len(grouped.tail)}曲、末尾のまま）"
        value = "、".join(t.title for t in grouped.tail)
        if len(value) > MAX_FIELD_VALUE - 10:
            value = value[: MAX_FIELD_VALUE - 10] + "…"
        if len(fields) < MAX_FIELDS and total_len + len(name) + len(value) <= MAX_EMBED_TOTAL:
            fields.append({"name": name, "value": value, "inline": False})

    embed = {"title": embed_title, "color": 0x5865F2, "fields": fields}
    if not fields:
        embed["description"] = "曲がありません。"
    return embed


def build_buttons(playlist_id: str) -> list:
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 1, "label": "反映する", "custom_id": f"confirm:{playlist_id}"},
                {"type": 2, "style": 2, "label": "キャンセル", "custom_id": f"cancel:{playlist_id}"},
            ],
        }
    ]


def run_preview(playlist_id: str, application_id: str, interaction_token: str) -> None:
    auth_header = get_auth_header()
    tracks = get_playlist_tracks(auth_header, playlist_id)
    title = get_playlist_title(auth_header, playlist_id) or playlist_id

    if not tracks:
        post_followup(application_id, interaction_token, {"content": "曲がありませんでした。"})
        return

    grouped = group_tracks(tracks)
    target = grouped.flatten()

    if [t.item_id for t in target] == [t.item_id for t in tracks]:
        post_followup(
            application_id, interaction_token, {"content": f"「{title}」はすでに並び替え済みです。"}
        )
        return

    post_followup(
        application_id,
        interaction_token,
        {"embeds": [build_embed(title, grouped)], "components": build_buttons(playlist_id)},
    )


def run_apply(playlist_id: str, application_id: str, interaction_token: str) -> None:
    auth_header = get_auth_header()
    current = get_playlist_tracks(auth_header, playlist_id)
    target = group_tracks(current).flatten()

    updates = compute_position_updates(current, target)
    if not updates:
        edit_original(
            application_id,
            interaction_token,
            {"content": "変更はありません。", "embeds": [], "components": []},
        )
        return

    apply_updates(auth_header, playlist_id, updates)
    edit_original(
        application_id,
        interaction_token,
        {"content": f"{len(updates)} 件反映しました。", "embeds": [], "components": []},
    )


def report_error(application_id: str, interaction_token: str, mode: str, message: str) -> None:
    payload = {"content": message, "embeds": [], "components": []}
    try:
        if mode == "apply":
            edit_original(application_id, interaction_token, payload)
        else:
            post_followup(application_id, interaction_token, payload)
    except requests.RequestException:
        pass


def main() -> None:
    mode = os.environ["TASK_MODE"]
    playlist_id = os.environ["PLAYLIST_ID"]
    application_id = os.environ["APPLICATION_ID"]
    interaction_token = os.environ["INTERACTION_TOKEN"]

    try:
        if mode == "preview":
            run_preview(playlist_id, application_id, interaction_token)
        elif mode == "apply":
            run_apply(playlist_id, application_id, interaction_token)
        else:
            sys.exit(f"unknown mode: {mode}")
    except ManualSortRequiredError as e:
        report_error(application_id, interaction_token, mode, str(e))
    except Exception as e:
        report_error(application_id, interaction_token, mode, f"エラーが発生しました: {e}")
        raise


if __name__ == "__main__":
    main()
