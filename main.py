# coding: UTF-8
import os
import json
import random
import asyncio
from datetime import datetime, timezone, timedelta, time
import discord
from discord import app_commands
from discord.ext import tasks
from discord.ui import Select, View
from dotenv import load_dotenv

load_dotenv()

# --- 定数定義 ---
JST = timezone(timedelta(hours=+9), "JST")
SETTINGS_FILE = "settings.json"
DATA_DIR = "data"
SAMPLE_SOUND_PATH = os.path.abspath("./data/sample.mp3")

intents = discord.Intents.all()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# 毎時00分00秒（JST）の実行トリガーリスト
HOURLY_TIMES = [time(hour=h, minute=0, second=0, tzinfo=JST) for h in range(24)]


# --- 音源選択ロジック ---
def select_random_sound(filename: str) -> str:
    """登録されているキャラクターフォルダからランダムに対象時刻の音源を選択する"""
    if not os.path.exists(DATA_DIR):
        return SAMPLE_SOUND_PATH

    folders = [d for d in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, d))]
    if not folders:
        return SAMPLE_SOUND_PATH

    candidates = list(folders)
    while candidates:
        selected_folder = random.choice(candidates)
        selected_path = os.path.join(DATA_DIR, selected_folder, f"{filename}.mp3")
        if os.path.isfile(selected_path):
            return os.path.abspath(selected_path)
        candidates.remove(selected_folder)

    # 該当音源が存在しない場合は sample.mp3
    return SAMPLE_SOUND_PATH


def select_target_sound(character: str, filename: str) -> str:
    """指定されたキャラクターフォルダから対象時刻の音源を選択する"""
    selected_path = os.path.join(DATA_DIR, character, f"{filename}.mp3")
    if os.path.isfile(selected_path):
        return os.path.abspath(selected_path)

    # 該当音源が存在しない場合は sample.mp3
    return SAMPLE_SOUND_PATH


# --- 設定ファイル管理ヘルパー ---
def load_settings() -> dict:
    default_config = {
        "default_character": "random",
        "default_volume": 0.5,
        "guilds": {}
    }
    if not os.path.exists(SETTINGS_FILE):
        return default_config
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"設定ファイルの読み込みに失敗しました: {e}")
        return default_config


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"設定ファイルの保存に失敗しました: {e}")


def get_guild_config(settings: dict, guild_id: int | str) -> dict:
    """指定サーバーの設定を取得（未設定項目はデフォルト値で補完）"""
    g_id = str(guild_id)
    cfg = settings.get("guilds", {}).get(g_id, {})
    return {
        "character": cfg.get("character", settings.get("default_character", "random")),
        "volume": float(cfg.get("volume", settings.get("default_volume", 0.5))),
        "text_channel_id": cfg.get("text_channel_id"),
        "voice_channels": cfg.get("voice_channels", [])
    }


# --- 再生ロジック ---
async def play_chime_in_vc(
    vc_channel: discord.VoiceChannel,
    text_channel: discord.TextChannel | None,
    now_str: str,
    character: str,
    volume: float,
    custom_sound_path: str | None = None
) -> None:
    """指定されたVCに入室し、音源を再生して切断する"""
    try:
        vc = await vc_channel.connect(timeout=10.0, reconnect=True)
    except Exception as e:
        print(f"[{vc_channel.guild.name}] {vc_channel.name} への接続に失敗しました: {e}")
        return

    try:
        # UDP/暗号化ハンドシェイク完了待機
        await asyncio.sleep(2.0)

        # 1. 音源パスの解決
        if custom_sound_path:
            source_path = custom_sound_path
        else:
            if character == "random":
                source_path = select_random_sound(now_str)
            else:
                source_path = select_target_sound(character, now_str)

            if source_path == SAMPLE_SOUND_PATH:
                print(f"[{vc_channel.guild.name}] 指定音源が見つからないため代替音源({SAMPLE_SOUND_PATH})を使用します。")

        # 再生対象ファイルが存在するか最終チェック
        if not source_path or not os.path.exists(source_path):
            raise FileNotFoundError(f"再生対象の音源ファイル '{source_path}' が存在しません。")

        # 2. 音声ストリームの生成
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(source=source_path),
            volume=volume
        )

        play_error = None
        def after_playing(error):
            nonlocal play_error
            if error:
                play_error = error
                print(f"[{vc_channel.guild.name}] 再生エラー: {error}")

        vc.play(source, after=after_playing)

        # レースコンディション対策: is_playing が True になるまで最大2秒待機
        wait_count = 0
        while not vc.is_playing() and wait_count < 20:
            if play_error:
                break
            await asyncio.sleep(0.1)
            wait_count += 1

        timer = 0
        while vc.is_playing():
            timer += 1
            if timer >= 30:
                if text_channel:
                    await text_channel.send("自動切断の待機時間が上限(30秒)に達しました。")
                break
            await asyncio.sleep(1)

    except Exception as e:
        print(f"[{vc_channel.guild.name}] 再生処理中にエラーが発生しました: {e}")
    finally:
        if vc.is_connected():
            await vc.disconnect()


async def process_guild(
    guild_id_str: str,
    guild_config: dict,
    now_str: str,
    custom_sound_path: str | None = None
) -> int:
    """単一サーバー内の全VCを優先度順に走査し、人がいるVCを巡回再生する"""
    guild = bot.get_guild(int(guild_id_str))
    if not guild:
        return 0

    character = guild_config["character"]
    volume = guild_config["volume"]
    text_channel_id = guild_config.get("text_channel_id")
    text_channel = bot.get_channel(text_channel_id) if text_channel_id else None
    vc_ids = guild_config.get("voice_channels", [])

    played_count = 0
    for vc_id in vc_ids:
        vc_channel = bot.get_channel(vc_id)
        if not vc_channel or not isinstance(vc_channel, discord.VoiceChannel):
            continue

        human_members = [m for m in vc_channel.members if not m.bot]
        if len(human_members) > 0:
            await play_chime_in_vc(
                vc_channel=vc_channel,
                text_channel=text_channel,
                now_str=now_str,
                character=character,
                volume=volume,
                custom_sound_path=custom_sound_path
            )
            played_count += 1
            # 次のVC接続前にDiscord側のセッション解放完了を待機
            await asyncio.sleep(2.0)

    return played_count


# --- 定期タスク ---
@tasks.loop(time=HOURLY_TIMES)
async def taskloop():
    now_str = datetime.now(JST).strftime("%H%M")
    settings = load_settings()
    guilds_dict = settings.get("guilds", {})

    coros = [
        process_guild(
            guild_id_str=g_id,
            guild_config=get_guild_config(settings, g_id),
            now_str=now_str
        )
        for g_id in guilds_dict.keys()
    ]
    if coros:
        await asyncio.gather(*coros)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("時報")
    )
    await tree.sync()
    if not taskloop.is_running():
        taskloop.start()


# --- スラッシュコマンド ---
class SelectCharacter(Select):
    def __init__(self, current_char: str):
        options_data = [{"label": "random"}]
        if os.path.exists(DATA_DIR):
            folders = [d for d in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, d))]
            for folder in folders:
                options_data.append({"label": folder})

        options = [discord.SelectOption(label=item["label"]) for item in options_data]
        super().__init__(
            placeholder=f"キャラクターを選択 (現在: {current_char})",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        settings = load_settings()
        selected_char = self.values[0]

        settings.setdefault("guilds", {}).setdefault(guild_id, {})["character"] = selected_char
        save_settings(settings)

        await interaction.response.send_message(
            f"このサーバーの時報キャラを {selected_char} に変更しました！"
        )


@tree.command(name="jh_character", description="このサーバーの時報キャラクターを変更します")
async def cmd_character(interaction: discord.Interaction):
    settings = load_settings()
    guild_cfg = get_guild_config(settings, interaction.guild_id)
    view = View()
    view.add_item(SelectCharacter(guild_cfg["character"]))
    await interaction.response.send_message(view=view)


@tree.command(name="jh_volume", description="このサーバーの時報の音量を設定します (0.0〜1.0)")
@app_commands.describe(volume="音量 (例: 0.5 で 50%)")
async def cmd_volume(interaction: discord.Interaction, volume: float):
    if not (0.0 <= volume <= 1.0):
        await interaction.response.send_message("音量は 0.0~1.0 の範囲で指定してください。(例: 0.5)", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    settings = load_settings()
    settings.setdefault("guilds", {}).setdefault(guild_id, {})["volume"] = round(volume, 2)
    save_settings(settings)

    await interaction.response.send_message(
        f"このサーバーの音量を {int(volume * 100)}% (`{volume:.2f}`) に設定しました。"
    )


@tree.command(name="jh_vc_add", description="時報対象のボイスチャンネルを優先度の末尾に追加します")
async def cmd_vc_add(interaction: discord.Interaction, channel: discord.VoiceChannel):
    settings = load_settings()
    guild_id = str(interaction.guild_id)

    guild_dict = settings.setdefault("guilds", {}).setdefault(guild_id, {})
    vc_list = guild_dict.setdefault("voice_channels", [])

    if channel.id in vc_list:
        await interaction.response.send_message(f"{channel.mention} は既に対象リストに登録されています。", ephemeral=True)
        return

    vc_list.append(channel.id)
    save_settings(settings)
    await interaction.response.send_message(f"{channel.mention} を優先度 {len(vc_list)} として登録しました。")


@tree.command(name="jh_vc_remove", description="時報対象のボイスチャンネルを削除します")
async def cmd_vc_remove(interaction: discord.Interaction, channel: discord.VoiceChannel):
    settings = load_settings()
    guild_id = str(interaction.guild_id)

    vc_list = settings.get("guilds", {}).get(guild_id, {}).get("voice_channels", [])
    if channel.id not in vc_list:
        await interaction.response.send_message(f"{channel.mention} は対象リストに登録されていません。", ephemeral=True)
        return

    vc_list.remove(channel.id)
    save_settings(settings)
    await interaction.response.send_message(f"{channel.mention} を対象リストから削除しました。")


@tree.command(name="jh_set_text", description="時報のエラー・通知先テキストチャンネルを設定します")
async def cmd_set_text(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = load_settings()
    guild_id = str(interaction.guild_id)

    settings.setdefault("guilds", {}).setdefault(guild_id, {})["text_channel_id"] = channel.id
    save_settings(settings)
    await interaction.response.send_message(f"通知先テキストチャンネルを {channel.mention} に設定しました。")


@tree.command(name="jh_info", description="このサーバーの時報設定（キャラ、音量、対象VC一覧）を表示します")
async def cmd_info(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return

    settings = load_settings()
    cfg = get_guild_config(settings, guild.id)

    embed = discord.Embed(
        title=f"🕒 時報設定ステータス - {guild.name}",
        color=discord.Color.blue(),
        timestamp=datetime.now(JST)
    )
    embed.add_field(name="🎙️ キャラクター", value=f"`{cfg['character']}`", inline=True)
    embed.add_field(name="🔊 音量", value=f"`{int(cfg['volume'] * 100)}%` ({cfg['volume']})", inline=True)

    text_ch = guild.get_channel(cfg["text_channel_id"]) if cfg["text_channel_id"] else None
    embed.add_field(name="💬 通知先チャンネル", value=text_ch.mention if text_ch else "未設定", inline=False)

    vc_ids = cfg.get("voice_channels", [])
    if vc_ids:
        lines = []
        for idx, vc_id in enumerate(vc_ids, start=1):
            vc = guild.get_channel(vc_id)
            ch_name = vc.mention if vc else f"不明なチャンネル (ID: {vc_id})"
            lines.append(f"**{idx}.** {ch_name}")
        vc_field_value = "\n".join(lines)
    else:
        vc_field_value = "対象VCは登録されていません。"

    embed.add_field(name="📋 対象VC (優先度順)", value=vc_field_value, inline=False)
    embed.set_footer(text=f"Bot: {bot.user.name}")

    await interaction.response.send_message(embed=embed)


@tree.command(name="jh_test", description="【デバッグ用】登録された全サーバーのVCでテスト再生を一斉実行します")
async def cmd_test(interaction: discord.Interaction):
    if not os.path.exists(SAMPLE_SOUND_PATH):
        await interaction.response.send_message(
            f"❌ テスト音源ファイル `{SAMPLE_SOUND_PATH}` が見つかりません。ファイルを配置してください。",
            ephemeral=True
        )
        return

    settings = load_settings()
    guilds_dict = settings.get("guilds", {})

    active_guild_ids = [
        g_id for g_id, g_cfg in guilds_dict.items()
        if g_cfg.get("voice_channels")
    ]

    if not active_guild_ids:
        await interaction.response.send_message(
            "⚠️ 登録されているVCが存在しません。先に `/jh_vc_add` でVCを登録してください。",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    now_str = datetime.now(JST).strftime("%H%M")
    coros = [
        process_guild(
            guild_id_str=g_id,
            guild_config=get_guild_config(settings, g_id),
            now_str=now_str,
            custom_sound_path=SAMPLE_SOUND_PATH
        )
        for g_id in active_guild_ids
    ]

    results = await asyncio.gather(*coros)
    total_played = sum(results)

    if total_played > 0:
        await interaction.followup.send(
            f"✅ 全サーバーのテスト再生が完了しました。（対象サーバー数: {len(active_guild_ids)} / 再生VC総数: {total_played}）"
        )
    else:
        await interaction.followup.send(
            f"⚠️ テスト走査が完了しましたが、VCにユーザーがいなかったため再生されませんでした。（対象サーバー数: {len(active_guild_ids)}）"
        )


bot.run(os.getenv('BOT_TOKEN'))