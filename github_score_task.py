import csv
import io
import json
import os

import requests

from score_playlist import build_youtube_client, fetch_playlist_tracks, get_youtube_view_count
from scoring import TrackScore, normalize_view_counts

DISCORD_API = "https://discord.com/api/v10"
TOP_N_IN_MESSAGE = 10


def _raise_with_body(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} error from Discord: {resp.text}", response=resp
        )


def post_followup(application_id: str, interaction_token: str, payload: dict) -> None:
    resp = requests.post(f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}", json=payload)
    _raise_with_body(resp)


def post_followup_with_file(
    application_id: str, interaction_token: str, content: str, filename: str, file_bytes: bytes
) -> None:
    payload = {"content": content, "attachments": [{"id": 0, "filename": filename}]}
    resp = requests.post(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}",
        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
        files={"files[0]": (filename, file_bytes, "text/csv")},
    )
    _raise_with_body(resp)


def build_csv_bytes(results: list) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["title", "artist", "video_id", "view_count", "score"])
    writer.writeheader()
    for r in results:
        writer.writerow(
            {
                "title": r.title,
                "artist": r.artist,
                "video_id": r.video_id or "",
                "view_count": r.view_count,
                "score": r.score,
            }
        )
    # Excelで開いたときに文字化けしないようBOM付きUTF-8で書き出す
    return output.getvalue().encode("utf-8-sig")


def run_score(playlist_id: str, application_id: str, interaction_token: str) -> None:
    youtube = build_youtube_client()
    tracks = fetch_playlist_tracks(playlist_id)

    if not tracks:
        post_followup(application_id, interaction_token, {"content": "曲がありませんでした。"})
        return

    cache: dict = {}
    raw = []
    for t in tracks:
        video_id, view_count = get_youtube_view_count(youtube, t["title"], t["artist"], cache)
        raw.append((t, video_id, view_count))

    scores = normalize_view_counts([r[2] for r in raw])
    results = [
        TrackScore(t["title"], t["artist"], video_id, view_count, round(score, 4))
        for (t, video_id, view_count), score in zip(raw, scores)
    ]
    results.sort(key=lambda r: r.score, reverse=True)

    top = results[:TOP_N_IN_MESSAGE]
    lines = [f"{i + 1}. {r.title} - {r.artist}（views: {r.view_count:,}）" for i, r in enumerate(top)]
    no_match = sum(1 for r in results if r.video_id is None)
    content = (
        f"**{len(results)}曲中 上位{len(top)}曲**（見つからなかった曲: {no_match}件、全件は添付CSV参照）\n"
        + "\n".join(lines)
    )

    post_followup_with_file(
        application_id, interaction_token, content, "scores.csv", build_csv_bytes(results)
    )


def main() -> None:
    playlist_id = os.environ["PLAYLIST_ID"]
    application_id = os.environ["APPLICATION_ID"]
    interaction_token = os.environ["INTERACTION_TOKEN"]

    try:
        run_score(playlist_id, application_id, interaction_token)
    except Exception as e:
        try:
            post_followup(application_id, interaction_token, {"content": f"エラーが発生しました: {e}"})
        except requests.RequestException:
            pass
        raise


if __name__ == "__main__":
    main()
