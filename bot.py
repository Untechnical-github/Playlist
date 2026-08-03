import asyncio
import os

import discord
from discord import app_commands

from core.apply import apply_updates, compute_position_updates
from core.auth import get_auth_header
from core.fetch import get_playlist_tracks
from core.planner import GroupedPlan, group_tracks
from core.youtube_api import get_playlist_title, list_my_playlists

GUILD_ID = os.environ.get("DISCORD_GUILD_ID")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _build_preview_embed(playlist_title: str, grouped: GroupedPlan) -> discord.Embed:
    embed = discord.Embed(
        title=f"並び替えプレビュー: {playlist_title}",
        color=discord.Color.blurple(),
    )
    for artist, tracks in grouped.blocks:
        value = "\n".join(f"・{t.title}" for t in tracks)
        if len(value) > 1000:
            value = value[:1000] + "\n…"
        embed.add_field(name=f"{artist}（{len(tracks)}曲）", value=value, inline=False)
    if grouped.tail:
        value = "、".join(t.title for t in grouped.tail)
        if len(value) > 1000:
            value = value[:1000] + "…"
        embed.add_field(
            name=f"単独曲・不明（{len(grouped.tail)}曲、末尾のまま）", value=value, inline=False
        )
    if not grouped.blocks and not grouped.tail:
        embed.description = "曲がありません。"
    return embed


class ConfirmView(discord.ui.View):
    def __init__(self, *, playlist_id, source_tracks, target_tracks, author_id):
        super().__init__(timeout=600)
        self.playlist_id = playlist_id
        self.source_tracks = source_tracks
        self.target_tracks = target_tracks
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "このコマンドを実行した本人のみ操作できます。", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="反映する", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)

        try:
            auth_header = await asyncio.to_thread(get_auth_header)
            current = await asyncio.to_thread(get_playlist_tracks, auth_header, self.playlist_id)
        except Exception as e:
            await interaction.followup.send(f"取得に失敗しました: {e}")
            return

        if [t.item_id for t in current] != [t.item_id for t in self.source_tracks]:
            await interaction.followup.send(
                "プレイリストの状態が変わっています。もう一度 /sort を実行してください。"
            )
            self.stop()
            return

        updates = compute_position_updates(current, self.target_tracks)
        if not updates:
            await interaction.followup.send("変更はありません。")
            self.stop()
            return

        try:
            await asyncio.to_thread(apply_updates, auth_header, self.playlist_id, updates)
        except Exception as e:
            await interaction.followup.send(f"反映に失敗しました: {e}")
            return

        await interaction.followup.send(f"{len(updates)} 件反映しました。")
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("キャンセルしました。")
        self.stop()


async def playlist_autocomplete(interaction: discord.Interaction, current: str):
    try:
        auth_header = await asyncio.to_thread(get_auth_header)
        playlists = await asyncio.to_thread(list_my_playlists, auth_header)
    except Exception:
        return []
    matches = [
        p
        for p in playlists
        if current.lower() in p["snippet"]["title"].lower()
    ]
    return [
        app_commands.Choice(name=p["snippet"]["title"][:100], value=p["id"])
        for p in matches[:25]
    ]


@tree.command(name="sort", description="プレイリストをアーティスト順に並び替える案を作る")
@app_commands.describe(playlist="対象のプレイリスト")
@app_commands.autocomplete(playlist=playlist_autocomplete)
async def sort(interaction: discord.Interaction, playlist: str):
    await interaction.response.defer(thinking=True)

    try:
        auth_header = await asyncio.to_thread(get_auth_header)
        tracks = await asyncio.to_thread(get_playlist_tracks, auth_header, playlist)
        title = await asyncio.to_thread(get_playlist_title, auth_header, playlist) or playlist
    except Exception as e:
        await interaction.followup.send(f"取得に失敗しました: {e}")
        return

    if not tracks:
        await interaction.followup.send("曲がありませんでした。")
        return

    grouped = group_tracks(tracks)
    target = grouped.flatten()

    if [t.item_id for t in target] == [t.item_id for t in tracks]:
        await interaction.followup.send(f"「{title}」はすでに並び替え済みです。")
        return

    embed = _build_preview_embed(title, grouped)
    view = ConfirmView(
        playlist_id=playlist,
        source_tracks=tracks,
        target_tracks=target,
        author_id=interaction.user.id,
    )
    await interaction.followup.send(embed=embed, view=view)


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    print(f"Logged in as {client.user} (commands synced)")


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN を設定してください。")
    client.run(token)


if __name__ == "__main__":
    main()
