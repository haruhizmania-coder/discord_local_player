import asyncio
import logging
import os
import random
import shutil
import sys
from collections import deque
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
MAX_QUEUE = 100
LOOP_LABELS = {"off": "オフ", "track": "1曲リピート", "queue": "キュー全体リピート"}

volumes: dict[int, float] = {}


class GuildState:
    """サーバーごとの再生状態。キューには sounds/ からの相対パスを積む。"""

    def __init__(self) -> None:
        self.queue: deque[str] = deque()
        self.current: str | None = None
        self.loop = "off"
        self.skipping = False
        # 再生ごとに発行する目印。/stop や切断で無効化し、古い after コールバックを無視する
        self.token: object | None = None

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.token = None


states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    return states.setdefault(guild_id, GuildState())


def drop_state(guild_id: int) -> None:
    state = states.pop(guild_id, None)
    if state:
        state.clear()


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

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        # Bot が切断された(キックなど)ときはキューを破棄する
        if self.user and member.id == self.user.id and after.channel is None:
            drop_state(member.guild.id)


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


def begin(vc: discord.VoiceClient, state: GuildState, name: str, path: Path) -> None:
    token = object()
    state.token = token
    state.current = name
    loop = asyncio.get_running_loop()

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(str(path), executable=FFMPEG_PATH),
        volume=volumes.get(vc.guild.id, DEFAULT_VOLUME),
    )

    def after(error: Exception | None) -> None:
        if error:
            log.error("Playback error: %s", error)
        # after は再生スレッドから呼ばれるので、状態の更新はイベントループ側で行う
        loop.call_soon_threadsafe(on_track_end, vc, state, token, name)

    try:
        vc.play(source, after=after)
    except Exception:
        state.current = None
        state.token = None
        raise


def start_next(vc: discord.VoiceClient, state: GuildState) -> None:
    """キューの先頭から、再生できる音源を探して再生する。なければ待機状態にする。"""
    while state.queue:
        name = state.queue.popleft()
        path = resolve_sound(name)
        if path is None:
            log.warning("Skipping missing file: %s", name)
            continue
        begin(vc, state, name, path)
        return
    state.current = None
    state.token = None


def on_track_end(vc: discord.VoiceClient, state: GuildState, token: object, name: str) -> None:
    if state.token is not token:
        return  # /stop や切断で無効化済み
    skipping, state.skipping = state.skipping, False
    if not vc.is_connected():
        drop_state(vc.guild.id)
        return
    if state.loop == "track" and not skipping:
        state.queue.appendleft(name)
    elif state.loop == "queue":
        state.queue.append(name)
    start_next(vc, state)


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


@tree.command(name="leave", description="ボイスチャンネルから退出します(キューも破棄されます)")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc is None:
        await interaction.response.send_message("ボイスチャンネルに参加していません。", ephemeral=True)
        return
    drop_state(interaction.guild.id)
    await vc.disconnect()
    await interaction.response.send_message("退出しました。")


@tree.command(name="play", description="sounds フォルダの音源を再生します(再生中ならキューに追加)")
@app_commands.describe(name="音源ファイル名")
@app_commands.autocomplete(name=sound_autocomplete)
@app_commands.guild_only()
async def play(interaction: discord.Interaction, name: str) -> None:
    await interaction.response.defer()
    path = resolve_sound(name)
    if path is None:
        await interaction.followup.send(f"音源が見つかりません: `{name}`", ephemeral=True)
        return
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    state = get_state(interaction.guild.id)
    if len(state.queue) >= MAX_QUEUE:
        await interaction.followup.send(f"キューがいっぱいです (最大 {MAX_QUEUE} 件)。", ephemeral=True)
        return
    canonical = path.relative_to(SOUNDS_DIR).as_posix()
    state.queue.append(canonical)
    if state.current is None:
        start_next(vc, state)
        await interaction.followup.send(f"再生中: `{canonical}`")
    else:
        await interaction.followup.send(f"キューに追加しました (#{len(state.queue)}): `{canonical}`")


@tree.command(name="skip", description="再生中の音源をスキップして次へ進みます")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    state = states.get(interaction.guild.id)
    if vc is None or state is None or state.current is None:
        await interaction.response.send_message("再生中の音源はありません。", ephemeral=True)
        return
    skipped = state.current
    state.skipping = True
    vc.stop()  # after コールバック経由で次の曲へ進む
    await interaction.response.send_message(f"スキップしました: `{skipped}`")


@tree.command(name="stop", description="再生を停止し、キューを空にします")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    state = states.get(interaction.guild.id)
    if vc is None or state is None or state.current is None:
        await interaction.response.send_message("再生中の音源はありません。", ephemeral=True)
        return
    state.clear()
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


@tree.command(name="queue", description="再生中の音源とキューを表示します")
@app_commands.guild_only()
async def queue_command(interaction: discord.Interaction) -> None:
    state = states.get(interaction.guild.id)
    if state is None or (state.current is None and not state.queue):
        await interaction.response.send_message("キューは空です。", ephemeral=True)
        return
    lines = [f"再生中: `{state.current}`" if state.current else "再生中: なし"]
    if state.loop != "off":
        lines.append(f"リピート: {LOOP_LABELS[state.loop]}")
    length = sum(len(line) for line in lines)
    for i, name in enumerate(state.queue, 1):
        if length + len(name) + 8 > 1800:
            lines.append(f"…ほか {len(state.queue) - i + 1} 件")
            break
        lines.append(f"{i}. {name}")
        length += len(name) + 8
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="remove", description="キューから指定した番号の音源を削除します")
@app_commands.describe(index="/queue に表示される番号")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, index: app_commands.Range[int, 1, MAX_QUEUE]) -> None:
    state = states.get(interaction.guild.id)
    if state is None or index > len(state.queue):
        await interaction.response.send_message("その番号の音源はキューにありません。", ephemeral=True)
        return
    removed = state.queue[index - 1]
    del state.queue[index - 1]
    await interaction.response.send_message(f"キューから削除しました: `{removed}`")


@tree.command(name="clear", description="キューを空にします(再生中の音源はそのまま)")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction) -> None:
    state = states.get(interaction.guild.id)
    if state is None or not state.queue:
        await interaction.response.send_message("キューは空です。", ephemeral=True)
        return
    count = len(state.queue)
    state.queue.clear()
    await interaction.response.send_message(f"キューを空にしました ({count} 件)。")


@tree.command(name="shuffle", description="キューの順番をシャッフルします")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction) -> None:
    state = states.get(interaction.guild.id)
    if state is None or len(state.queue) < 2:
        await interaction.response.send_message("シャッフルできる音源がキューにありません。", ephemeral=True)
        return
    items = list(state.queue)
    random.shuffle(items)
    state.queue = deque(items)
    await interaction.response.send_message("キューをシャッフルしました。")


@tree.command(name="loop", description="リピート再生の設定を切り替えます")
@app_commands.describe(mode="リピートの種類")
@app_commands.choices(mode=[app_commands.Choice(name=label, value=key) for key, label in LOOP_LABELS.items()])
@app_commands.guild_only()
async def loop_command(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
    get_state(interaction.guild.id).loop = mode.value
    await interaction.response.send_message(f"リピートを「{mode.name}」にしました。")


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
