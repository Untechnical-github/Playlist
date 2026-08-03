import os
import sys

import requests

from core.apply import apply_updates, compute_position_updates
from core.auth import get_auth_header
from core.fetch import get_playlist_tracks
from core.planner import GroupedPlan, group_tracks
from core.youtube_api import ManualSortRequiredError, get_playlist_title

DISCORD_API = "https://discord.com/api/v10"


def post_followup(application_id: str, interaction_token: str, payload: dict) -> None:
    resp = requests.post(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}", json=payload
    )
    resp.raise_for_status()


def edit_original(application_id: str, interaction_token: str, payload: dict) -> None:
    resp = requests.patch(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}/messages/@original",
        json=payload,
    )
    resp.raise_for_status()


def build_embed(title: str, grouped: GroupedPlan) -> dict:
    fields = []
    for artist, tracks in grouped.blocks:
        value = "\n".join(f"・{t.title}" for t in tracks)
        if len(value) > 1000:
            value = value[:1000] + "\n…"
        fields.append({"name": f"{artist}（{len(tracks)}曲）", "value": value, "inline": False})
    if grouped.tail:
        value = "、".join(t.title for t in grouped.tail)
        if len(value) > 1000:
            value = value[:1000] + "…"
        fields.append(
            {
                "name": f"単独曲・不明（{len(grouped.tail)}曲、末尾のまま）",
                "value": value,
                "inline": False,
            }
        )
    embed = {"title": f"並び替えプレビュー: {title}", "color": 0x5865F2, "fields": fields}
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
