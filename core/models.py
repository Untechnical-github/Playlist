from dataclasses import dataclass, field
from typing import List, Optional

UGC_VIDEO_TYPE = "MUSIC_VIDEO_TYPE_UGC"


@dataclass
class Track:
    video_id: str
    item_id: Optional[str]
    """YouTube Data API v3 の playlistItem id（並び替えの反映に使う）"""
    title: str
    artists: List[str] = field(default_factory=list)
    video_type: Optional[str] = None

    @property
    def is_unknown(self) -> bool:
        return not self.artists or self.video_type == UGC_VIDEO_TYPE

    @property
    def primary_artist(self) -> Optional[str]:
        return self.artists[0] if self.artists else None

    def to_dict(self) -> dict:
        return {
            "videoId": self.video_id,
            "itemId": self.item_id,
            "title": self.title,
            "artists": self.artists,
            "videoType": self.video_type,
        }

    @staticmethod
    def from_dict(d: dict) -> "Track":
        return Track(
            video_id=d["videoId"],
            item_id=d.get("itemId"),
            title=d["title"],
            artists=d.get("artists", []),
            video_type=d.get("videoType"),
        )
