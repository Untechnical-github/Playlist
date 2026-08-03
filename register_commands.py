import os
import sys

import requests

DISCORD_API = "https://discord.com/api/v10"

COMMANDS = [
    {
        "name": "sort",
        "description": "プレイリストをアーティスト順に並び替える案を作る",
        "options": [
            {
                "type": 3,
                "name": "playlist",
                "description": "対象のプレイリスト",
                "required": True,
                "autocomplete": True,
            }
        ],
    }
]


def main() -> None:
    application_id = os.environ.get("DISCORD_APPLICATION_ID")
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not application_id or not bot_token:
        sys.exit("環境変数 DISCORD_APPLICATION_ID / DISCORD_BOT_TOKEN を設定してください。")

    resp = requests.put(
        f"{DISCORD_API}/applications/{application_id}/commands",
        headers={"Authorization": f"Bot {bot_token}"},
        json=COMMANDS,
    )
    resp.raise_for_status()
    print("登録したコマンド:", [c["name"] for c in resp.json()])


if __name__ == "__main__":
    main()
