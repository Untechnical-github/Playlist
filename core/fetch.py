from typing import List

from .models import Track
from .youtube_api import list_playlist_items


def get_playlist_tracks(auth_header: str, playlist_id: str) -> List[Track]:
    """アーティスト情報は認証なしの ytmusicapi（限定公開/公開プレイリストで動作）、
    並び替えに使う item_id は公式 YouTube Data API v3 から取得する。
    両方とも同じプレイリストを現在の並び順で取得するため、位置（インデックス）でマージする。
    videoId は稀に YouTube Music 側と YouTube 本体側で異なることがあるため使わない。
    """
    from ytmusicapi import YTMusic

    yt = YTMusic()
    music_data = yt.get_playlist(playlist_id, limit=None)
    music_tracks = music_data["tracks"]

    official_items = list_playlist_items(auth_header, playlist_id)

    if len(music_tracks) != len(official_items):
        raise RuntimeError(
            f"取得結果の曲数が一致しません（ytmusicapi: {len(music_tracks)}, "
            f"公式API: {len(official_items)}）。プレイリストが取得中に変更された可能性があります。"
            "もう一度 fetch を試してください。"
        )

    tracks: List[Track] = []
    for entry, official in zip(music_tracks, official_items):
        artists = [a["name"] for a in (entry.get("artists") or []) if a.get("name")]
        tracks.append(
            Track(
                video_id=official["contentDetails"]["videoId"],
                item_id=official["id"],
                title=entry.get("title") or "",
                artists=artists,
                video_type=entry.get("videoType"),
            )
        )
    return tracks
