import json
import sys
from pathlib import Path

OAUTH_TOKEN_FILE = Path("auth/oauth.json")
OAUTH_CLIENT_FILE = Path("auth/oauth_client.json")


def get_auth_header() -> str:
    """OAuthのアクセストークンを "Bearer ..." の形で返す。必要なら自動でリフレッシュする。"""
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.oauth import OAuthCredentials

    if not OAUTH_TOKEN_FILE.exists() or not OAUTH_CLIENT_FILE.exists():
        sys.exit(
            f"認証ファイルが見つかりません: {OAUTH_TOKEN_FILE} / {OAUTH_CLIENT_FILE}\n"
            "README のセットアップ手順に従って作成してください。"
        )
    client = json.loads(OAUTH_CLIENT_FILE.read_text(encoding="utf-8"))
    credentials = OAuthCredentials(
        client_id=client["client_id"], client_secret=client["client_secret"]
    )
    yt = YTMusic(str(OAUTH_TOKEN_FILE), oauth_credentials=credentials)
    return yt.headers["authorization"]
