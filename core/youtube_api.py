from typing import Any, Dict, List, Optional

import requests

BASE = "https://www.googleapis.com/youtube/v3"


def list_playlist_items(auth_header: str, playlist_id: str) -> List[Dict[str, Any]]:
    """公式 YouTube Data API v3 でプレイリストの中身を position 順に取得する。"""
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(
            f"{BASE}/playlistItems",
            params=params,
            headers={"Authorization": auth_header},
        )
        resp.raise_for_status()
        body = resp.json()
        items.extend(body.get("items", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    items.sort(key=lambda it: it["snippet"]["position"])
    return items


def list_my_playlists(auth_header: str) -> List[Dict[str, Any]]:
    """自分が所有するプレイリストの一覧（id・タイトル）を取得する。"""
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(
            f"{BASE}/playlists",
            params=params,
            headers={"Authorization": auth_header},
        )
        resp.raise_for_status()
        body = resp.json()
        items.extend(body.get("items", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return items


def get_playlist_title(auth_header: str, playlist_id: str) -> Optional[str]:
    resp = requests.get(
        f"{BASE}/playlists",
        params={"part": "snippet", "id": playlist_id},
        headers={"Authorization": auth_header},
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[0]["snippet"]["title"] if items else None


def set_item_position(
    auth_header: str, playlist_id: str, item_id: str, video_id: str, position: int
) -> None:
    body = {
        "id": item_id,
        "snippet": {
            "playlistId": playlist_id,
            "position": position,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        },
    }
    resp = requests.put(
        f"{BASE}/playlistItems",
        params={"part": "snippet"},
        headers={"Authorization": auth_header, "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
