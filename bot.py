import logging
import os
import shutil
import sys
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("local_player")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
SOUNDS_DIR = Path(os.getenv("SOUNDS_DIR", "sounds")).resolve()
EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
DEFAULT_VOLUME = 0.5

volumes: dict[int, float] = {}
# 再生ごとに発行するトークン。/stop や別の曲の再生で古いループを止めるために使う
playbacks: dict[int, object] = {}


def list_sounds() -> list[str]:
    files = (p for p in SOUNDS_DIR.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
    return sorted(p.relative_to(SOUNDS_DIR).as_posix() for p in files)


def resolve_sound(name: str) -> Path | None:
    """sounds/ 配下の音源だけを許可する(../ などによる外部ファイル参照を防ぐ)。"""
    path = (SOUNDS_DIR / name).resolve()
    if not path.is_relative_to(SOUNDS_DIR) or path.suffix.lower() not in EXTENSIONS:
        return None
    return path if path.is_file() else None


class LocalPlayer(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()  # グローバル同期は反映まで最大1時間かかることがある

    async def on_ready(self) -> None:
        log.info("Logged in as %s (sounds: %s)", self.user, SOUNDS_DIR)


client = LocalPlayer()
tree = client.tree


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    voice = getattr(interaction.user, "voice", None)
    if voice is None or voice.channel is None:
        await interaction.followup.send("先にボイスチャンネルに参加してください。", ephemeral=True)
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        return await voice.channel.connect()
    if vc.channel != voice.channel:
        await vc.move_to(voice.channel)
    return vc


def start_playback(vc: discord.VoiceClient, path: Path, loop: bool) -> None:
    guild_id = vc.guild.id
    token = object()
    playbacks[guild_id] = token

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(str(path), executable=FFMPEG_PATH),
        volume=volumes.get(guild_id, DEFAULT_VOLUME),
    )

    def after(error: Exception | None) -> None:
        if error:
            log.error("Playback error: %s", error)
        if loop and playbacks.get(guild_id) is token and vc.is_connected():
            client.loop.call_soon_threadsafe(start_playback, vc, path, loop)

    if vc.is_playing() or vc.is_paused():
        vc.stop()
    vc.play(source, after=after)


async def sound_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()
    names = [n for n in list_sounds() if current in n.lower() and len(n) <= 100]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@tree.command(name="join", description="あなたのいるボイスチャンネルに参加します")
@app_commands.guild_only()
async def join(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    vc = await ensure_voice(interaction)
    if vc:
        await interaction.followup.send(f"{vc.channel.name} に参加しました。", ephemeral=True)


@tree.command(name="leave", description="ボイスチャンネルから退出します")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc is None:
        await interaction.response.send_message("ボイスチャンネルに参加していません。", ephemeral=True)
        return
    playbacks.pop(interaction.guild.id, None)
    await vc.disconnect()
    await interaction.response.send_message("退出しました。")


@tree.command(name="play", description="sounds フォルダの音源を再生します")
@app_commands.describe(name="音源ファイル名", loop="繰り返し再生する")
@app_commands.autocomplete(name=sound_autocomplete)
@app_commands.guild_only()
async def play(interaction: discord.Interaction, name: str, loop: bool = False) -> None:
    await interaction.response.defer()
    path = resolve_sound(name)
    if path is None:
        await interaction.followup.send(f"音源が見つかりません: `{name}`", ephemeral=True)
        return
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    start_playback(vc, path, loop)
    await interaction.followup.send(f"再生中: `{name}`" + (" (ループ)" if loop else ""))


@tree.command(name="stop", description="再生を停止します")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc is None or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("再生中の音源はありません。", ephemeral=True)
        return
    playbacks.pop(interaction.guild.id, None)
    vc.stop()
    await interaction.response.send_message("停止しました。")


@tree.command(name="pause", description="再生を一時停止します")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_playing():
        await interaction.response.send_message("再生中の音源はありません。", ephemeral=True)
        return
    vc.pause()
    await interaction.response.send_message("一時停止しました。")


@tree.command(name="resume", description="一時停止中の再生を再開します")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_paused():
        await interaction.response.send_message("一時停止中の音源はありません。", ephemeral=True)
        return
    vc.resume()
    await interaction.response.send_message("再開しました。")


@tree.command(name="volume", description="音量を変更します (0-100)")
@app_commands.describe(percent="音量(%)")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]) -> None:
    volumes[interaction.guild.id] = percent / 100
    vc = interaction.guild.voice_client
    if vc is not None and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = percent / 100
    await interaction.response.send_message(f"音量を {percent}% にしました。")


@tree.command(name="list", description="再生できる音源の一覧を表示します")
async def list_command(interaction: discord.Interaction) -> None:
    names = list_sounds()
    if not names:
        await interaction.response.send_message(f"`{SOUNDS_DIR}` に音源がありません。", ephemeral=True)
        return
    lines: list[str] = []
    length = 0
    for n in names:
        if length + len(n) + 4 > 1800:
            lines.append(f"…ほか {len(names) - len(lines)} 件")
            break
        lines.append(f"- {n}")
        length += len(n) + 4
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        sys.exit("DISCORD_TOKEN が設定されていません。.env を確認してください。")
    if shutil.which(FFMPEG_PATH) is None:
        sys.exit("FFmpeg が見つかりません。インストールするか、.env の FFMPEG_PATH を設定してください。")
    SOUNDS_DIR.mkdir(exist_ok=True)
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
