import os
import sys

import requests

from core.aliases import load_artist_groups
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


# Discord embed の実際の上限（フィールド数25、フィールド値1024文字、埋め込み合計6000文字、
# 1メッセージあたりembed最大10個）に収まるよう、複数のembed・複数のメッセージに分割する。
# 何も省略しない（切り詰めるのは1フィールド内の曲名一覧が1024文字を超える場合のみ）。
MAX_FIELDS = 25
MAX_FIELD_VALUE = 1024
MAX_EMBED_TOTAL = 5500
MAX_EMBEDS_PER_MESSAGE = 10


def _split_text(text: str, sep: str, limit: int) -> list:
    """text を sep の境目でできるだけ壊さずに、limit 文字以内のチャンクへ分割する。"""
    parts = text.split(sep)
    chunks: list = []
    current = ""
    for part in parts:
        if len(part) > limit:
            part = part[: limit - 1] + "…"
        candidate = f"{current}{sep}{part}" if current else part
        if len(candidate) > limit and current:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def _fields_for(name: str, text: str, sep: str) -> list:
    """1件分の (見出し, 本文) を、1024文字を超える場合は複数フィールドに分割して返す。
    何も省略しない。
    """
    chunks = _split_text(text, sep, MAX_FIELD_VALUE - 1)
    fields = []
    for i, chunk in enumerate(chunks):
        field_name = name if i == 0 else f"{name}（続き）"
        fields.append({"name": field_name[:256], "value": chunk, "inline": False})
    return fields


def build_embeds(title: str, grouped: GroupedPlan) -> list:
    embed_title = f"並び替えプレビュー: {title}"
    embeds: list = []
    fields: list = []
    total_len = len(embed_title)

    def flush():
        nonlocal fields, total_len
        if fields:
            embeds.append({"color": 0x5865F2, "fields": fields})
        fields = []
        total_len = len(embed_title)

    entries = [
        (f"{artist}（{len(tracks)}曲）", "\n".join(f"・{t.title}" for t in tracks), "\n")
        for artist, tracks in grouped.blocks
    ]
    if grouped.tail:
        entries.append(
            (
                f"単独曲・不明（{len(grouped.tail)}曲、末尾のまま）",
                "、".join(t.title for t in grouped.tail),
                "、",
            )
        )

    for name, text, sep in entries:
        for field in _fields_for(name, text, sep):
            entry_len = len(field["name"]) + len(field["value"])
            if len(fields) >= MAX_FIELDS or total_len + entry_len > MAX_EMBED_TOTAL:
                flush()
            fields.append(field)
            total_len += entry_len
    flush()

    if not embeds:
        embeds = [{"color": 0x5865F2, "description": "曲がありません。"}]

    embeds[0]["title"] = embed_title
    if len(embeds) > 1:
        for i, e in enumerate(embeds):
            e["footer"] = {"text": f"{i + 1}/{len(embeds)}"}

    return embeds


def chunk_embeds(embeds: list) -> list:
    return [
        embeds[i : i + MAX_EMBEDS_PER_MESSAGE] for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE)
    ]


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

    alias_map, group_display = load_artist_groups(playlist_id)
    grouped = group_tracks(tracks, alias_map, group_display)
    target = grouped.flatten()

    if [t.item_id for t in target] == [t.item_id for t in tracks]:
        post_followup(
            application_id, interaction_token, {"content": f"「{title}」はすでに並び替え済みです。"}
        )
        return

    embeds = build_embeds(title, grouped)
    chunks = chunk_embeds(embeds)
    for i, chunk in enumerate(chunks):
        payload = {"embeds": chunk}
        if i == len(chunks) - 1:
            payload["components"] = build_buttons(playlist_id)
        post_followup(application_id, interaction_token, payload)


def run_apply(playlist_id: str, application_id: str, interaction_token: str) -> None:
    auth_header = get_auth_header()
    current = get_playlist_tracks(auth_header, playlist_id)
    alias_map, group_display = load_artist_groups(playlist_id)
    target = group_tracks(current, alias_map, group_display).flatten()

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
