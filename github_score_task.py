import json
import os
import time
from pathlib import Path

import requests

from core.auth import get_auth_header
from core.repo_commit import commit_and_push
from core.youtube_api import (
    add_playlist_item,
    list_my_playlists,
    list_playlist_items,
    remove_playlist_item,
)
from score_playlist import (
    build_youtube_client,
    build_ytmusic_client,
    fetch_playlist_tracks,
    get_view_count_by_video_id,
    get_view_counts_for_video_ids,
    get_youtube_view_count,
    load_view_cache,
    save_view_cache,
    search_videos_by_title,
    search_videos_by_title_ytmusic,
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

COVER_CANDIDATES_FILE = Path("cover_candidates.json")
# ytmusicapiの検索はGoogleのクォータを消費しないため、1回のdiscoveryフェーズで曲名検索を
# 試みる曲数の上限は、クォータではなく単純に処理時間・非公式APIへの配慮で決めている。
# プレイリスト全体（数百〜数千曲規模）を1回のdiscoveryで処理しきれるよう大きめの値にしてある。
COVER_DISCOVERY_MAX_TRACKS_PER_RUN = 2000
# ytmusicapiの検索で候補が見つからなかった曲だけ、フォールバックとしてYouTube Data APIの
# search.list（1回100クォータ）で再度探す。デフォルトの1日10,000クォータ枠では約100回が
# 上限の目安になるため、少し余裕を持たせた値にしている（通常のスコアリングは`known_video_id`
# によりsearch.listをほぼ使わないため、そちらとの取り合いは気にしなくてよい）。
COVER_DISCOVERY_YOUTUBE_SEARCH_MAX_PER_RUN = 90
COVER_DISCOVERY_SEARCH_RESULTS = 5
COVER_CANDIDATES_PER_MESSAGE = 5  # Discordの1メッセージに入れる候補数（ボタン行の上限を考慮）
COVER_DISCOVERY_COMMIT_BATCH_SIZE = 20  # この件数の曲を処理するごとにまとめてコミットする


def _track_key(title: str, artist: str) -> str:
    return f"{title}\x1f{artist}"


def load_cover_candidates() -> dict:
    """自動探索したコラボ・カバー候補動画と、その判定状況（pending/yes/no）を読み込む。
    `score_overrides.json`（人力メンテの上書き設定）を置き換えるもので、内容は
    `discover_cover_candidates`が自動で追記し、Discordのボタン操作で人間が確定させる。
    """
    if not COVER_CANDIDATES_FILE.exists():
        return {}
    return json.loads(COVER_CANDIDATES_FILE.read_text(encoding="utf-8"))


def save_cover_candidates(cover_candidates: dict) -> None:
    COVER_CANDIDATES_FILE.write_text(
        json.dumps(cover_candidates, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def confirmed_cover_video_ids(cover_candidates: dict, title: str, artist: str) -> list:
    """曲名・アーティスト名から、コラボ・カバー候補として確定済み（status: "yes"）の
    video_id一覧を返す。"""
    candidates = cover_candidates.get(_track_key(title, artist), {})
    return [video_id for video_id, entry in candidates.items() if entry.get("status") == "yes"]


def _pick_best_new_candidate(youtube, results: list, own_video_id, known_candidate_ids: set, title_lower: str):
    """検索結果から、自分自身・既知の候補を除いた新規候補のうち再生回数最大の1件を選ぶ。
    見つからなければNoneを返す。"""
    new_results = [
        (video_id, video_title, channel)
        for video_id, video_title, channel in results
        if video_id != own_video_id and video_id not in known_candidate_ids and title_lower in video_title.lower()
    ]
    if not new_results:
        return None

    stats = get_view_counts_for_video_ids(youtube, [video_id for video_id, _, _ in new_results])
    best_video_id, best_title, best_channel = max(new_results, key=lambda r: stats.get(r[0], 0))
    return best_video_id, best_title, best_channel, stats.get(best_video_id, 0)


def discover_cover_candidates(
    youtube,
    ytmusic,
    all_tracks: list,
    cache: dict,
    view_count_threshold: int,
    cover_candidates: dict,
) -> list:
    """しきい値未満と分かっている曲（＝自身の再生回数が既にキャッシュ済みのもの）について、
    まだ見たことのないコラボ・カバー候補動画を探し、`cover_candidates`に status: "pending"
    として追記する。1曲につき今回新たに見つける候補は最大1件（複数見つかった場合は再生回数が
    最大のものだけを採用し、残りは次回以降のdiscoveryに回す）。

    探索は2段階: まずYouTube Data APIのクォータを消費しない`ytmusicapi`の検索（"songs"・
    "videos"）を試し、そこで見つからなかった曲だけYouTube Data APIの`search.list`（クォータ
    消費・1日あたりの回数に上限あり）でフォールバック検索する。曲ごとにどちらを試したかは
    `cover_candidates[track_key]["_meta"]`に記録し、両方試して見つからなかった曲を毎回
    無駄に検索し直さないようにする（video_idと違い11文字固定のキーにはならないため、
    実際のvideo_idと衝突しない予約キーとして"_meta"を使う）。

    既にしきい値を満たしている曲は、候補を追加しても判定結果が変わらないため対象外にする
    （再生回数は基本的に増加のみで減らないため、一度満たした曲がその後下回ることも無い）。
    自身の再生回数がまだキャッシュに無い曲（新曲等）も、今回のスコアリングで判明してから
    次回以降のdiscoveryで扱う。

    見つけた新規候補のリスト（Discord通知用の辞書のリスト）を返す。ファイル保存・Gitコミットは
    `COVER_DISCOVERY_COMMIT_BATCH_SIZE`件ごとにまとめて行う（クォータ超過等で途中終了しても、
    それまでに進んだ分は失われない。1曲ごとにコミットするとGitの履歴が膨大になるため）。
    """
    newly_found = []
    checked = 0
    youtube_searches_used = 0
    unsaved = 0

    for t in all_tracks:
        if checked >= COVER_DISCOVERY_MAX_TRACKS_PER_RUN:
            break

        cached = cache.get((t["title"], t["artist"]))
        if cached is None or cached[1] >= view_count_threshold:
            continue

        track_key = _track_key(t["title"], t["artist"])
        track_data = cover_candidates.setdefault(track_key, {})
        meta = track_data.setdefault("_meta", {"ytmusic_checked": False, "youtube_checked": False})
        known_candidate_ids = {vid for vid in track_data if vid != "_meta"}

        if any(track_data[vid].get("status") == "pending" for vid in known_candidate_ids):
            continue  # 回答待ちの候補があるので、答えが出るまで新しい候補は提案しない
        if meta["ytmusic_checked"] and meta["youtube_checked"]:
            continue  # 両方の検索方法で調べ尽くしていて、これ以上探しても見つからない

        own_video_id = cached[0]
        title_lower = t["title"].lower()
        found = None
        touched = False

        if not meta["ytmusic_checked"]:
            results = search_videos_by_title_ytmusic(ytmusic, t["title"], max_results=COVER_DISCOVERY_SEARCH_RESULTS)
            meta["ytmusic_checked"] = True
            touched = True
            found = _pick_best_new_candidate(youtube, results, own_video_id, known_candidate_ids, title_lower)

        if (
            found is None
            and not meta["youtube_checked"]
            and youtube_searches_used < COVER_DISCOVERY_YOUTUBE_SEARCH_MAX_PER_RUN
        ):
            results = search_videos_by_title(youtube, t["title"], max_results=COVER_DISCOVERY_SEARCH_RESULTS)
            youtube_searches_used += 1
            meta["youtube_checked"] = True
            touched = True
            found = _pick_best_new_candidate(youtube, results, own_video_id, known_candidate_ids, title_lower)

        if not touched:
            continue  # ytmusic検索済み・YouTube検索は今回の上限に達していて試せなかった
        checked += 1

        if found is not None:
            best_video_id, best_title, best_channel, best_view_count = found
            track_data[best_video_id] = {
                "status": "pending",
                "candidate_title": best_title,
                "candidate_channel": best_channel,
                "view_count": best_view_count,
            }
            newly_found.append(
                {
                    "track_title": t["title"],
                    "track_artist": t["artist"],
                    "video_id": best_video_id,
                    "candidate_title": best_title,
                    "candidate_channel": best_channel,
                    "view_count": best_view_count,
                }
            )

        unsaved += 1
        if unsaved >= COVER_DISCOVERY_COMMIT_BATCH_SIZE:
            save_cover_candidates(cover_candidates)
            commit_and_push([str(COVER_CANDIDATES_FILE)], "Update cover/collab candidate discovery progress")
            unsaved = 0

    if unsaved:
        save_cover_candidates(cover_candidates)
        commit_and_push([str(COVER_CANDIDATES_FILE)], "Update cover/collab candidate discovery progress")

    return newly_found


def run_cover_decide(video_id: str, decision: str) -> None:
    """Discordのボタン操作で確定したコラボ・カバー候補の判定（yes/no）を記録する。"""
    cover_candidates = load_cover_candidates()
    updated = False
    for candidates in cover_candidates.values():
        entry = candidates.get(video_id)
        if entry is None or entry.get("status") != "pending":
            continue
        entry["status"] = decision
        if decision == "yes":
            entry["fetched_at"] = time.time()
        updated = True

    if not updated:
        return

    save_cover_candidates(cover_candidates)
    commit_and_push(
        [str(COVER_CANDIDATES_FILE)],
        f"Record cover/collab decision for {video_id}: {decision}",
    )


def _raise_with_body(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} error from Discord: {resp.text}", response=resp
        )


def post_followup_payload(application_id: str, interaction_token: str, payload: dict) -> None:
    resp = requests.post(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}",
        json=payload,
    )
    _raise_with_body(resp)


def post_followup(application_id: str, interaction_token: str, content: str) -> None:
    post_followup_payload(application_id, interaction_token, {"content": content})


def build_cover_candidate_components(batch: list) -> list:
    """コラボ・カバー候補1件につき「はい」「いいえ」ボタン1行を作る。custom_idは
    `covyes:{video_id}` / `covno:{video_id}`（video_idは11文字固定なのでDiscordの
    custom_id上限100文字に余裕で収まり、曲名・アーティスト名を含める必要が無い）。
    """
    rows = []
    for i, item in enumerate(batch, start=1):
        rows.append(
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": f"{i}: はい", "custom_id": f"covyes:{item['video_id']}"},
                    {"type": 2, "style": 4, "label": f"{i}: いいえ", "custom_id": f"covno:{item['video_id']}"},
                ],
            }
        )
    return rows


def send_cover_candidate_messages(application_id: str, interaction_token: str, newly_found: list) -> None:
    """discover_cover_candidatesで見つかった候補を、最大COVER_CANDIDATES_PER_MESSAGE件ずつ
    まとめてDiscordに通知する（候補数が多いときにメッセージが大量に届かないようにするため）。
    """
    for start in range(0, len(newly_found), COVER_CANDIDATES_PER_MESSAGE):
        batch = newly_found[start : start + COVER_CANDIDATES_PER_MESSAGE]
        lines = [
            f"{i}. 「{item['track_title']} - {item['track_artist']}」の候補: "
            f"「{item['candidate_title']}」（チャンネル: {item['candidate_channel']}、"
            f"再生回数: {item['view_count']:,}）"
            for i, item in enumerate(batch, start=1)
        ]
        content = (
            "**コラボ・カバー候補が見つかりました**\n"
            "曲の再生回数としてカウントしてよければ「はい」、関係なければ「いいえ」を押してください。\n\n"
            + "\n".join(lines)
        )
        post_followup_payload(
            application_id,
            interaction_token,
            {"content": content, "components": build_cover_candidate_components(batch)},
        )


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
    ytmusic = build_ytmusic_client()
    view_count_threshold = threshold * VIEW_UNIT
    cache, fetched_at = load_view_cache()
    cover_candidates = load_cover_candidates()

    # 既存のスコアリングより先に、しきい値未満と分かっている曲のコラボ・カバー候補を探す
    # （候補が見つかったらDiscordのボタンで確認してもらう。曲自体のスコアリングは待たない）
    newly_found_candidates = discover_cover_candidates(
        youtube, ytmusic, all_tracks, cache, view_count_threshold, cover_candidates
    )
    if newly_found_candidates:
        send_cover_candidate_messages(application_id, interaction_token, newly_found_candidates)

    matches = []
    # この曲自体（クレジット通りの動画）の再生回数が確定した（キャッシュ由来の値が得られた）
    # video_id の集合。クォータ超過等で今回検証できなかった曲はここに入らないため、
    # 「対象から漏れた」＝「しきい値未満」と誤って削除しないようにする（下記の削除判定で使う）
    verified_video_ids = set()
    try:
        for t in all_tracks:
            cover_video_ids = confirmed_cover_video_ids(cover_candidates, t["title"], t["artist"])
            own_key = (t["title"], t["artist"])

            # 追加するのは常にこの曲自体（クレジット通りの動画）の video_id。しきい値の判定は、
            # この曲自体に加えて確定済みのコラボ・カバー候補も調べ、最大の再生回数を採用する
            # （カバー元やコラボ版など、どれか1つでも超えていればよい）
            video_id, own_view_count = get_youtube_view_count(
                youtube, t["title"], t["artist"], cache, fetched_at, known_video_id=t.get("video_id")
            )
            alt_view_counts = [
                get_view_count_by_video_id(youtube, vid, cache, fetched_at) for vid in cover_video_ids
            ]
            view_count = max([own_view_count] + alt_view_counts)

            # 境界線付近（しきい値未満だがその80%以上）ならキャッシュ済みでも再取得し、
            # 取得し直した最新の値で判定する
            if view_count_threshold * BORDERLINE_THRESHOLD_RATIO <= view_count < view_count_threshold:
                video_id, own_view_count = get_youtube_view_count(
                    youtube, t["title"], t["artist"], cache, fetched_at, force_refresh=True
                )
                alt_view_counts = [
                    get_view_count_by_video_id(youtube, vid, cache, fetched_at, force_refresh=True)
                    for vid in cover_video_ids
                ]
                view_count = max([own_view_count] + alt_view_counts)

            if own_key in cache:
                # 再生回数がキャッシュ由来（今回新規取得できた、または以前からの既知の値）で確定している
                verified_video_ids.add(cache[own_key][0])

            if video_id and view_count >= view_count_threshold:
                matches.append((t, video_id, view_count))

            # 曲数が多いと検索だけで長時間かかり、クォータ超過やジョブのキャンセル・タイムアウトで
            # 途中終了することもある。1曲ごとに保存しておけば、そこまで調べた分は必ず次回に持ち越せる
            # （finallyでの保存だけだと、プロセスが強制終了された場合に未保存のまま失われる）
            save_view_cache(cache, fetched_at)
    finally:
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
    skipped = []
    for video_id, item in original_by_video_id.items():
        if video_id in qualifying_video_ids:
            continue
        if video_id not in verified_video_ids:
            # クォータ超過等で今回は再生回数を確認できなかった曲。しきい値未満と確定していないので
            # 誤って削除しない（次回、確認できたときに改めて判定する）
            skipped.append(item["snippet"].get("title", video_id))
            continue
        remove_playlist_item(auth_header, item["id"])
        removed.append(item["snippet"].get("title", video_id))

    if not newly_added and not removed and not skipped:
        post_followup(
            application_id,
            interaction_token,
            f"変更はありませんでした（しきい値: {threshold}万回再生以上、"
            f"集計対象プレイリスト{len(source_playlists)}件、対象{len(matches)}曲）。",
        )
        return

    if newly_added or removed:
        header = (
            f"**「{TARGET_PLAYLIST_NAME}」を更新しました**"
            f"（しきい値: {threshold}万回再生以上、集計対象プレイリスト{len(source_playlists)}件、"
            f"追加{len(newly_added)}曲・削除{len(removed)}曲"
            + (f"・確認できず保留{len(skipped)}曲" if skipped else "")
            + "）\n"
        )
    else:
        # 追加・削除は無いが、確認できず保留にした曲だけはある場合
        header = (
            f"**「{TARGET_PLAYLIST_NAME}」に追加・削除の変更はありませんでした**"
            f"（しきい値: {threshold}万回再生以上、集計対象プレイリスト{len(source_playlists)}件、"
            f"確認できず保留{len(skipped)}曲）\n"
        )
    lines = [f"・追加: {t['title']} - {t['artist']}（views: {view_count:,}）" for t, view_count in newly_added]
    lines += [f"・削除: {title}（しきい値未満になったため）" for title in removed]
    lines += [f"・保留: {title}（再生回数を確認できなかったため削除せず維持）" for title in skipped]
    send_paginated_message(application_id, interaction_token, header, lines)


def main() -> None:
    mode = os.environ.get("TASK_MODE", "score")

    if mode == "cover_decide":
        run_cover_decide(os.environ["VIDEO_ID"], os.environ["DECISION"])
        return

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
