import argparse
import json
import sys
from pathlib import Path

from core.aliases import load_artist_groups
from core.apply import apply_updates, compute_position_updates
from core.auth import get_auth_header
from core.fetch import get_playlist_tracks
from core.models import Track
from core.planner import group_tracks

SNAPSHOT_FILE = Path("playlist_snapshot.json")
PLAN_FILE = Path("plan.json")


def cmd_fetch(args):
    auth_header = get_auth_header()
    tracks = get_playlist_tracks(auth_header, args.playlist_id)

    missing = [t for t in tracks if t.item_id is None]
    if missing:
        sys.exit(
            f"{len(missing)} 曲で公式APIとの対応付けができませんでした。もう一度 fetch を試してください。"
        )

    SNAPSHOT_FILE.write_text(
        json.dumps(
            {"playlistId": args.playlist_id, "tracks": [t.to_dict() for t in tracks]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"{len(tracks)} 曲を取得し {SNAPSHOT_FILE} に保存しました。")


def cmd_plan(args):
    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    tracks = [Track.from_dict(d) for d in snapshot["tracks"]]
    alias_map, group_display = load_artist_groups(snapshot["playlistId"])
    grouped = group_tracks(tracks, alias_map, group_display)
    new_order = grouped.flatten()

    print("--- 変更後の並び ---")
    for artist, block_tracks in grouped.blocks:
        print(f"[{artist}]")
        for t in block_tracks:
            print(f"    {t.title}")
    if grouped.tail:
        print("[単独曲・不明（末尾、元の順のまま）]")
        for t in grouped.tail:
            artist = "/".join(t.artists) if t.artists else "(不明)"
            print(f"    {t.title} - {artist}")

    PLAN_FILE.write_text(
        json.dumps(
            {
                "playlistId": snapshot["playlistId"],
                "sourceOrder": [t.item_id for t in tracks],
                "order": [t.to_dict() for t in new_order],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n{PLAN_FILE} に保存しました。内容を確認のうえ `apply` を実行してください。")


def cmd_apply(args):
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    playlist_id = plan["playlistId"]
    source_order = plan["sourceOrder"]
    target_order = [Track.from_dict(d) for d in plan["order"]]

    auth_header = get_auth_header()
    current = get_playlist_tracks(auth_header, playlist_id)

    if [t.item_id for t in current] != source_order:
        sys.exit("プレイリストの状態が plan 作成時から変わっています。`plan` を再実行してください。")

    updates = compute_position_updates(current, target_order)
    if not updates:
        print("変更はありません。")
        return

    print(f"{len(updates)} 件の移動を適用します。")
    if not args.yes:
        answer = input("実行しますか？ [y/N]: ")
        if answer.strip().lower() != "y":
            print("中止しました。")
            return

    apply_updates(auth_header, playlist_id, updates)
    print("反映しました。")


def main():
    parser = argparse.ArgumentParser(
        description="YouTube Music のプレイリストをアーティスト順に半自動で並び替える"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="プレイリストを取得してスナップショット保存")
    p_fetch.add_argument("playlist_id")
    p_fetch.set_defaults(func=cmd_fetch)

    p_plan = sub.add_parser("plan", help="並び替え案を計算して表示・保存")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="並び替え案を実際に反映")
    p_apply.add_argument("--yes", action="store_true", help="確認プロンプトをスキップ")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
