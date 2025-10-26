import discord
import sqlite3
import os
import zipfile
import logging
import secrets
import hashlib
import shutil
import glob
import datetime
import asyncio

from discord.ext import commands
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from contextlib import contextmanager

# =====================================================
# ロギング設定
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("SleepBot")

# =====================================================
# BOT設定
# =====================================================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# =====================================================
# 定数設定
# =====================================================
DB_PATH = 'blacklist.db'
DB_FILE = "blacklist.db"
ZIP_DIR = "./"
BACKUP_PREFIX = "backup_"
ZIP_KEEP_DAYS = 3
LOG_KEEP_DAYS = 14
BACKUP_FOLDER = "backups"
BACKUP_FLAG_FILE = os.path.join("backups", ".backup_flag")
KEEPALIVE_CHANNEL_ID = 1353622624860766308
BACKUP_CHANNEL_ID = 1370282144181784616

# =====================================================
# コマンド連打防止設定
# =====================================================
COMMAND_COOLDOWN_SECONDS = 5  # 同一ユーザーが同じコマンドを再実行するまでの待機秒数
recent_interactions = {}

# =====================================================
# データベース関連
# =====================================================
from contextlib import contextmanager

@contextmanager
def safe_db_context():
    """安全なデータベース接続のコンテキストマネージャー"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        yield conn
        conn.commit()  # ← ここを追加
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"データベースエラー: {e}")
        raise
    finally:
        if conn:
            conn.close()


def get_db_connection():
    """データベース接続を取得"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    """データベース初期化"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        
        # ユーザーごとのブラックリスト
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_blacklists (
            owner_id INTEGER,
            blocked_user_id INTEGER,
            reason TEXT,
            added_at TIMESTAMP,
            PRIMARY KEY (owner_id, blocked_user_id)
        )
        ''')
        
        # 部屋情報
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS rooms (
            room_id INTEGER PRIMARY KEY,
            text_channel_id INTEGER,
            voice_channel_id INTEGER,
            creator_id INTEGER,
            created_at TIMESTAMP,
            role_id INTEGER,
            gender TEXT,
            details TEXT
        )
        ''')
        
        # 管理者ログ
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_logs (
            log_id INTEGER PRIMARY KEY,
            action TEXT,
            user_id INTEGER,
            target_id INTEGER,
            details TEXT,
            timestamp TIMESTAMP
        )
        ''')
        
        conn.commit()
    logger.info("データベース初期化完了")

# =====================================================
# ログ管理機能
# =====================================================
def add_admin_log(action, user_id, target_id=None, details=""):
    """管理者ログを追加"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO admin_logs (action, user_id, target_id, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (action, user_id, target_id, details, datetime.datetime.now())
        )
    logger.info(f"管理者ログ: {action} - ユーザー: {user_id} - 対象: {target_id} - 詳細: {details}")

# =====================================================
# ブラックリスト機能
# =====================================================
def add_to_blacklist(owner_id, blocked_user_id, reason=""):
    """ブラックリストに追加"""
    try:
        with safe_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO user_blacklists (owner_id, blocked_user_id, reason, added_at) VALUES (?, ?, ?, ?)",
                (owner_id, blocked_user_id, reason, datetime.datetime.now())
            )
            if cursor.rowcount == 0:
                logger.warning(f"ブラックリスト追加試行（変更なし）: {owner_id} -> {blocked_user_id}")
            else:
                logger.info(f"ブラックリスト追加: ユーザー {owner_id} が {blocked_user_id} をブロック - 理由: {reason}")
    except Exception as e:
        logger.error(f"ブラックリスト追加失敗: {owner_id} -> {blocked_user_id} 理由: {reason} エラー: {e}")

def remove_from_blacklist(owner_id, blocked_user_id):
    """ブラックリストから削除"""
    try:
        with safe_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_blacklists WHERE owner_id = ? AND blocked_user_id = ?",
                (owner_id, blocked_user_id)
            )
            result = cursor.rowcount > 0

        if result:
            logger.info(f"ブラックリスト削除: ユーザー {owner_id} が {blocked_user_id} のブロックを解除")
        else:
            logger.warning(f"ブラックリスト削除: ユーザー {owner_id} -> {blocked_user_id} は元から登録されていなかった")
        return result
    except Exception as e:
        logger.error(f"ブラックリスト削除失敗: {owner_id} -> {blocked_user_id} エラー: {e}")
        return False


def get_blacklist(owner_id):
    """ブラックリストを取得"""
    try:
        with safe_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT blocked_user_id FROM user_blacklists WHERE owner_id = ?", (owner_id,))
            blacklist = [row[0] for row in cursor.fetchall()]
        return blacklist
    except Exception as e:
        logger.error(f"ブラックリスト取得失敗: {owner_id} エラー: {e}")
        return []

        return False


# =====================================================
# 汎用ヘルパー関数
# =====================================================
async def send_interaction_message(
    interaction: discord.Interaction,
    content: str = None,
    embed: discord.Embed = None,
    view=None,
    ephemeral: bool = True,
    already_deferred: bool = False,
):
    """インタラクションメッセージ送信のヘルパー関数"""
    kwargs = {
        "content": content,
        "embed": embed,
        "ephemeral": ephemeral,
    }
    if view is not None:
        kwargs["view"] = view

    if not already_deferred and not interaction.response.is_done():
        await interaction.response.send_message(**kwargs)
    else:
        await interaction.followup.send(**kwargs)

def get_user_genders(member: discord.Member) -> set[str]:
    """ユーザーが閲覧できるgenderのセットを返す"""
    roleset = set()
    male_role = discord.utils.get(member.roles, name="男性")
    female_role = discord.utils.get(member.roles, name="女性")

    if male_role:
        roleset.add("male")
    if female_role:
        roleset.add("female")

    # "all" は、いずれかのロールがある人は閲覧可能
    if roleset:
        roleset.add("all")

    return roleset

# =====================================================
# 部屋管理機能
# =====================================================
def add_room(text_channel_id, voice_channel_id, creator_id, role_id, gender: str, details: str):
    """部屋をデータベースに追加"""
    logger.info(f"[add_room] パラメータ: text={text_channel_id}, voice={voice_channel_id}, creator={creator_id}, role={role_id}")
    
    try:
        with safe_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO rooms (text_channel_id, voice_channel_id, creator_id, created_at, role_id, gender, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (text_channel_id, voice_channel_id, creator_id, datetime.datetime.now(), role_id, gender, details)
            )
            conn.commit()  # 明示的にコミットを追加
            room_id = cursor.lastrowid
            
            # 登録確認
            cursor.execute("SELECT * FROM rooms WHERE text_channel_id = ? AND voice_channel_id = ?", 
                          (text_channel_id, voice_channel_id))
            check = cursor.fetchone()
            logger.info(f"[add_room] 登録確認: {check}")

    except Exception as e:
        logger.error(f"部屋の登録に失敗: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        # エラー時は0または-1を返す
        return -1

def get_rooms_by_creator(creator_id):
    """作成者IDで部屋を取得"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT text_channel_id, voice_channel_id FROM rooms WHERE creator_id = ?", (creator_id,))
        rooms = cursor.fetchall()
    return rooms

def remove_room(text_channel_id=None, voice_channel_id=None):
    """部屋をデータベースから削除"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        
        # まずは部屋情報を取得
        if text_channel_id:
            cursor.execute("SELECT role_id, creator_id, voice_channel_id FROM rooms WHERE text_channel_id = ?", (text_channel_id,))
        elif voice_channel_id:
            cursor.execute("SELECT role_id, creator_id, text_channel_id FROM rooms WHERE voice_channel_id = ?", (voice_channel_id,))
        else:
            return None, None, None
        
        result = cursor.fetchone()
        if not result:
            logger.warning(f"部屋が見つかりませんでした: text_channel_id={text_channel_id}, voice_channel_id={voice_channel_id}")
            return None, None, None
        
        role_id, creator_id, other_channel_id = result
        
        # 削除処理
        if text_channel_id:
            cursor.execute("DELETE FROM rooms WHERE text_channel_id = ?", (text_channel_id,))
            logger.info(f"部屋削除: テキストチャンネル {text_channel_id} を削除")
        elif voice_channel_id:
            cursor.execute("DELETE FROM rooms WHERE voice_channel_id = ?", (voice_channel_id,))
            logger.info(f"部屋削除: ボイスチャンネル {voice_channel_id} を削除")
        
        # 削除されたかどうかを確認
        if cursor.rowcount == 0:
            logger.warning(f"データベースから部屋を削除できませんでした: text_channel_id={text_channel_id}, voice_channel_id={voice_channel_id}")
        else:
            logger.info(f"データベースから部屋を削除しました: 削除行数={cursor.rowcount}")
    
    return role_id, creator_id, other_channel_id

def get_room_info(channel_id):
    """チャンネルIDから部屋情報を取得"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT creator_id, role_id, text_channel_id, voice_channel_id FROM rooms WHERE text_channel_id = ? OR voice_channel_id = ?", 
                      (channel_id, channel_id))
        result = cursor.fetchone()
    
    if not result:
        return None, None, None, None
    return result

# =====================================================
# 部屋作成UI
# =====================================================
class RoomCreationModal(discord.ui.Modal, title="募集メッセージ入力"):
    """部屋作成用モーダル"""
    def __init__(self, gender: str):
        super().__init__()
        self.gender = gender
    
    room_message = discord.ui.TextInput(
        label="募集の詳細 (任意, 最大200文字)",
        style=discord.TextStyle.paragraph,
        max_length=200,
        required=False,
        default="【いつから】\n【いつまで】\n【目的】\n【NG】\n【一言】",
        placeholder="ここに募集の詳細を入力してください (省略可)"
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=True)
        
        await create_room_with_gender(
            interaction,
            self.gender,
            room_message=self.room_message.value
        )

class GenderRoomView(discord.ui.View):
    """性別選択ボタンView"""
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)

    @discord.ui.button(label="男性のみ", style=discord.ButtonStyle.primary)
    async def male_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RoomCreationModal(gender="male")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="女性のみ", style=discord.ButtonStyle.danger)
    async def female_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RoomCreationModal(gender="female")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="どちらでもOK", style=discord.ButtonStyle.secondary)
    async def both_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RoomCreationModal(gender="all")
        await interaction.response.send_modal(modal)

class CancelRequestView(discord.ui.View):
    """入室希望取り消しボタン用View"""

    def __init__(self, requester_id: int):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.message: discord.Message | None = None

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ この操作は行えません。", ephemeral=True)
            return

        await interaction.response.send_message("✅ 取り消しました。", ephemeral=True)
        if self.message:
            try:
                await self.message.delete()
            except Exception as e:
                logger.warning(f"入室希望メッセージ削除失敗: {e}")



class TalkRequestView(discord.ui.View):
    """入室希望ボタン設置用View"""

    def __init__(self, creator: discord.Member):
        super().__init__(timeout=None)
        self.creator = creator
        self.requested_user_ids: set[int] = set()

    @discord.ui.button(label="話したい", style=discord.ButtonStyle.danger)
    async def request(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in self.requested_user_ids:
            await interaction.response.send_message(
                "❌ 既に入室希望を送信しています。取り消す場合はメッセージの\"取り消す\"ボタンを押してください。",
                ephemeral=True,
            )
            return

        cancel_view = CancelRequestView(interaction.user.id)
        message = f"{self.creator.mention}さん、{interaction.user.mention}さんがお話してみたいそうです！"
        msg = await interaction.channel.send(content=message, view=cancel_view)
        cancel_view.message = msg
        self.requested_user_ids.add(interaction.user.id)
        await interaction.response.send_message("入室希望を送信しました。", ephemeral=True)

async def create_room_with_gender(interaction: discord.Interaction, gender: str, capacity: int = 2, room_message: str = ""):
    """部屋作成のメイン処理"""
    # 既存部屋チェック
    existing_rooms = get_rooms_by_creator(interaction.user.id)
    if existing_rooms:
        await send_interaction_message(interaction, 
            "❌ すでに部屋を作成しています。新しい部屋を作成する前に、既存の部屋を削除してください。",
            ephemeral=True
        )
        return

    # 部屋名とカテゴリ設定
    room_name = f"{interaction.user.display_name}の通話募集"
    category_name = f"{interaction.user.display_name}の通話募集-{interaction.user.id}"
    category = discord.utils.get(interaction.guild.categories, name=category_name)
    
    if not category:
        category = await interaction.guild.create_category(category_name)
        logger.info(f"カテゴリー '{category_name}' を作成しました")

    # 権限設定
    male_role = discord.utils.get(interaction.guild.roles, name="男性")
    female_role = discord.utils.get(interaction.guild.roles, name="女性")

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
        interaction.user: discord.PermissionOverwrite(view_channel=True),
    }

    # 性別に応じた権限設定
    if gender == "male" and male_role:
        overwrites[male_role] = discord.PermissionOverwrite(view_channel=True)
    elif gender == "female" and female_role:
        overwrites[female_role] = discord.PermissionOverwrite(view_channel=True)
    elif gender == "all":
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(view_channel=True)
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(view_channel=True)

    # ハッシュ化された非表示ロール作成
    random_salt = secrets.token_hex(8)
    raw_string = f"{random_salt}:{interaction.user.id}"
    hashed = hashlib.sha256(raw_string.encode()).hexdigest()[:12]
    role_name = f"{hashed}"
    
    try:
        hidden_role = await interaction.guild.create_role(
            name=role_name,
            permissions=discord.Permissions.none(),
            hoist=False,
            mentionable=False
        )
        logger.info(f"非表示ロール '{role_name}' を作成しました")
    except Exception as e:
        logger.error(f"非表示ロールの作成に失敗: {str(e)}")
        await send_interaction_message(interaction, f"❌ ロールの作成に失敗しました: {str(e)}", ephemeral=True)
        return

    # ブラックリストユーザーに対する権限設定
    blacklisted_users = get_blacklist(interaction.user.id)
    for user_id in blacklisted_users:
        member = interaction.guild.get_member(user_id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=False)
            logger.info(f"ID'{user_id}'をブロックしました")

    # チャンネル作成
    text_channel = None
    voice_channel = None
    
    try:
        text_channel = await interaction.guild.create_text_channel(
            name=f"{room_name}-通話交渉",
            category=category,
            overwrites=overwrites
        )
        logger.info(f"テキストチャンネル '{text_channel.name}' (ID: {text_channel.id}) を作成しました")
        
        voice_channel = await interaction.guild.create_voice_channel(
            name=f"{room_name}-お部屋",
            category=category,
            overwrites=overwrites
        )
        logger.info(f"ボイスチャンネル '{voice_channel.name}' (ID: {voice_channel.id}) を作成しました")
        
        # ★ 重要: チャンネル作成成功後、すぐにデータベースに登録
        room_id = add_room(text_channel.id, voice_channel.id, interaction.user.id, hidden_role.id, gender, room_message)
        logger.info(f"データベースに部屋を登録しました: room_id={room_id}")
        
        # 管理者ログ記録
        add_admin_log("部屋作成", interaction.user.id, None, f"テキスト:{text_channel.id} ボイス:{voice_channel.id}")

        # 成功メッセージ
        await send_interaction_message(interaction, 
            f"✅ 通話募集部屋を作成しました！\nテキスト: {text_channel.mention}\nボイス: {voice_channel.mention}",
            ephemeral=True
        )

        # 作成者の性別判定
        creator_gender_jp = "不明"
        if male_role in interaction.user.roles and female_role in interaction.user.roles:
            creator_gender_jp = "両方!?"
        elif male_role in interaction.user.roles:
            creator_gender_jp = "男性"
        elif female_role in interaction.user.roles:
            creator_gender_jp = "女性"

        # 募集メッセージ作成
        notice_role = discord.utils.get(interaction.guild.roles, name="募集通知")
        role_mention_str = notice_role.mention if notice_role else ""

        message_text = f"{interaction.user.mention} さん（{creator_gender_jp}）が通話を募集中です！\n\n"
        
        if room_message:
            message_text += f"📝 募集の詳細\n{room_message}\n\n"

        # 自己紹介チャンネルから情報取得
        intro_channel_name = None
        if female_role in interaction.user.roles:
            intro_channel_name = "🚺自己紹介（女性）"
        elif male_role in interaction.user.roles:
            intro_channel_name = "🚹自己紹介（男性）"

        intro_text = "自己紹介は記入されていません。"
        if intro_channel_name:
            intro_channel = discord.utils.get(interaction.guild.text_channels, name=intro_channel_name)
            if intro_channel:
                async for msg in intro_channel.history(limit=None):
                    if msg.author.id == interaction.user.id:
                        intro_text = f"自己紹介はこちら → {msg.jump_url}"
                        break

        message_text += f"\n{intro_text}"
        message_text += f"\n\n{role_mention_str}\n部屋の作成者は `/delete-room` コマンドでこの部屋を削除できます。\n\nこの部屋は「通話」を前提とした募集用です。\nDMでのやり取りのみが目的の方は利用をご遠慮ください。\nそのような行為を繰り返していると判断された場合、利用制限などの措置対象となります。\n"
        
        await text_channel.send(message_text, allowed_mentions=discord.AllowedMentions(roles=True))

        # 入室希望ボタンを配置
        request_view = TalkRequestView(interaction.user)
        await text_channel.send(
            "------------\n\n🔔話してみたい人はボタンを押してください",
            view=request_view,
        )

    except Exception as e:
        logger.error(f"部屋の作成に失敗: {str(e)}")
        
        # エラー発生時のクリーンアップ
        if text_channel:
            try:
                await text_channel.delete()
                logger.info(f"エラーのためテキストチャンネル {text_channel.id} を削除しました")
            except:
                pass
                
        if voice_channel:
            try:
                await voice_channel.delete()
                logger.info(f"エラーのためボイスチャンネル {voice_channel.id} を削除しました")
            except:
                pass
                
        if hidden_role:
            try:
                await hidden_role.delete()
                logger.info(f"エラーのためロール '{role_name}' を削除しました")
            except:
                pass
        
        await send_interaction_message(interaction, f"❌ 部屋の作成に失敗しました: {str(e)}", ephemeral=True)

# =====================================================
# 満室管理機能
# =====================================================
@bot.event
async def on_voice_state_update(member, before, after):
    # チャンネルが変化した場合だけチェック
    if before.channel != after.channel:
        channels_to_check = []
        if before.channel is not None:
            channels_to_check.append(before.channel)
        if after.channel is not None:
            channels_to_check.append(after.channel)

        for ch in channels_to_check:
            await check_room_capacity(ch)


async def check_room_capacity(voice_channel: discord.VoiceChannel):
    """部屋の人数チェックと満室処理"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT text_channel_id, creator_id, role_id, gender, details
            FROM rooms
            WHERE voice_channel_id = ?
        """, (voice_channel.id,))
        row = cursor.fetchone()

    if not row:
        return

    text_channel_id, creator_id, role_id, gender, details = row

    # 人間だけカウント
    human_members = [m for m in voice_channel.members if not m.bot]
    human_count = len(human_members)
    
    # Botカウント
    bot_members = [m for m in voice_channel.members if m.bot]
    bot_count = len(bot_members)

    # 人間2人以上なら満室として隠す
    if human_count >= 2:
        await hide_room(voice_channel, text_channel_id, role_id, creator_id)
    else:
        await show_room(voice_channel, text_channel_id, role_id, creator_id, gender)
    
    # 人数上限を設定
    total_count = human_count + bot_count
    new_limit = total_count + 1
    try:
        await voice_channel.edit(user_limit=new_limit)
        logger.info(f"ボイスチャンネル {voice_channel.id} の上限を {new_limit} に設定しました (現在 人間:{human_count}, Bot:{bot_count})")
    except Exception as e:
        logger.error(f"ボイスチャンネルの上限設定に失敗: {e}")

async def hide_room(voice_channel: discord.VoiceChannel, text_channel_id: int, role_id: int, creator_id: int):

    logger.info(
        f"HIDE関数呼び出し: VC={voice_channel.name} 人間数={len(voice_channel.members)}"
    )

    """部屋を満室状態として隠す処理"""
    text_channel = voice_channel.guild.get_channel(text_channel_id)
    if not text_channel:
        return

    guild = voice_channel.guild
    hidden_role = guild.get_role(role_id) if role_id else None

    base_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
    }

    if hidden_role:
        base_overwrites[hidden_role] = discord.PermissionOverwrite(view_channel=False)

    text_overwrites = base_overwrites.copy()
    voice_overwrites = base_overwrites.copy()

    current_members = [m for m in voice_channel.members if not m.bot]
    for member in current_members:
        text_overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, read_messages=True, send_messages=True
        )
        voice_overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, connect=True
        )

    # ブラックリストユーザーも明示的にブロック
    for user_id in set(get_blacklist(creator_id)):
        obj = discord.Object(id=user_id)
        text_overwrites[obj] = discord.PermissionOverwrite(
            view_channel=False, read_messages=False, send_messages=False
        )
        voice_overwrites[obj] = discord.PermissionOverwrite(
            view_channel=False, connect=False
        )

    try:
        await text_channel.edit(overwrites=text_overwrites)
        await voice_channel.edit(overwrites=voice_overwrites)
        logger.info(

            f"[hide_room] {text_channel.id} / {voice_channel.id} を満室非公開状態に設定"
        )
    except Exception as e:
        logger.error(f"[hide_room] チャンネルの上書きに失敗: {e}")

async def show_room(voice_channel: discord.VoiceChannel, text_channel_id: int, role_id: int, creator_id: int, gender: str):
    """部屋を再び公開する処理"""
    text_channel = voice_channel.guild.get_channel(text_channel_id)
    hidden_role = voice_channel.guild.get_role(role_id) if role_id else None
    guild = voice_channel.guild
    male_role = discord.utils.get(guild.roles, name="男性")
    female_role = discord.utils.get(guild.roles, name="女性")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
    }
    
    if hidden_role:
        overwrites[hidden_role] = discord.PermissionOverwrite(view_channel=False)

    # 性別に応じた可視設定
    if gender == "male":
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(view_channel=True)
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(view_channel=False)
    elif gender == "female":
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(view_channel=True)
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(view_channel=False)
    elif gender == "all":
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(view_channel=True)
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(view_channel=True)

    # 作成者に対する権限を明示的に追加
    creator = guild.get_member(creator_id)
    if creator:
        overwrites[creator] = discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            connect=True
        )

    try:
        if text_channel:
            await text_channel.edit(overwrites=overwrites)
        await voice_channel.edit(overwrites=overwrites)
        logger.info(
            f"[show_room] {text_channel_id} / {voice_channel.id} を再公開しました (gender={gender})"
        )
    except Exception as e:
        logger.error(f"[show_room] チャンネルの上書きに失敗: {e}")

    # ブラックリスト再拒否（重要！）
    blacklisted_users = set(get_blacklist(creator_id))
    call_count = 0
    for user_id in blacklisted_users:
        user = guild.get_member(user_id) or discord.Object(id=user_id)
        for channel in filter(None, [text_channel, voice_channel]):
            try:
                await channel.set_permissions(
                    user,
                    view_channel=False,
                    read_messages=False,
                    send_messages=False,
                    connect=False,
                    reason="ブラックリスト拒否"
                )
                logger.info(f"ID'{user_id}'を再ブロックしました")
            except Exception as e:
                name = getattr(user, "display_name", str(user_id))
                logger.error(f"個別拒否失敗 {name}: {e}")
            call_count += 1
            if call_count % 4 == 0:
                await asyncio.sleep(1)

# =====================================================
# 部屋削除機能
# =====================================================
@bot.tree.command(name="delete-room", description="通話募集部屋を削除")
async def delete_room(interaction: discord.Interaction):
    """
    通話募集部屋削除コマンド
    
    【実行条件】
    - 通話募集のテキストチャンネルまたはデバッグ部屋でのみ実行可能
    - 通常部屋: 部屋の作成者またはサーバー管理者のみ実行可能
    - デバッグ部屋: 管理者のみ実行可能
    """
    
    # ========== 1. 初期化と権限確認 ==========
    logger.info(f"[DELETE-ROOM] 実行開始: チャンネル={interaction.channel.id}, ユーザー={interaction.user.id}")
    
    # 元のget_room_info関数を使用
    creator_id, role_id, text_channel_id, voice_channel_id = get_room_info(interaction.channel.id)
    
    logger.info(f"[DELETE-ROOM] 部屋情報取得: creator_id={creator_id}, role_id={role_id}, text_channel_id={text_channel_id}, voice_channel_id={voice_channel_id}")
    
    # 部屋として認識されているかチェック
    if creator_id is None:
        logger.warning(f"[DELETE-ROOM] 部屋情報なし: チャンネル={interaction.channel.id}")
        await send_interaction_message(
            interaction, 
            "❌ このコマンドは通話募集部屋またはデバッグ部屋でのみ使用できます。\n💡 `/quick-db-check` で部屋情報を確認できます。", 
            ephemeral=True
        )
        return
    
    # 部屋タイプの判定（genderを別途取得）
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT gender, details FROM rooms WHERE text_channel_id = ? OR voice_channel_id = ?", 
                      (interaction.channel.id, interaction.channel.id))
        result = cursor.fetchone()
        gender = result[0] if result else "all"
        details = result[1] if result else ""
    
    is_debug_room = (gender == "debug")
    room_type = "デバッグ部屋" if is_debug_room else "通話募集部屋"
    
    # 実行権限チェック
    is_creator = creator_id == interaction.user.id
    is_admin = interaction.user.guild_permissions.administrator
    
    if is_debug_room:
        # デバッグ部屋は管理者のみ削除可能
        if not is_admin:
            await send_interaction_message(
                interaction, 
                "❌ デバッグ部屋は管理者のみが削除できます。", 
                ephemeral=True
            )
            return
        logger.info(f"[DELETE-ROOM] デバッグ部屋削除: 管理者={interaction.user.id}")
    else:
        # 通常部屋は作成者または管理者が削除可能
        if not (is_creator or is_admin):
            await send_interaction_message(
                interaction, 
                "❌ 通話募集部屋は作成者または管理者のみが削除できます。", 
                ephemeral=True
            )
            return
        logger.info(f"[DELETE-ROOM] 通話募集部屋削除: 権限={'作成者' if is_creator else '管理者'}")
    
    # ========== 2. 削除処理開始 ==========
    await send_interaction_message(interaction, f"🗑️ {room_type}を削除しています...", ephemeral=True)
    
    # カテゴリを取得（後で空かどうかチェック用）
    category = interaction.channel.category
    deletion_results = {
        "voice_channel": False,
        "text_channel": False,
        "role": False,
        "database": False,
        "current_channel": False,
        "category": False
    }
    
    # ========== 3. ボイスチャンネル削除 ==========
    if voice_channel_id:
        voice_channel = interaction.guild.get_channel(voice_channel_id)
        if voice_channel:
            try:
                await voice_channel.delete()
                deletion_results["voice_channel"] = True
                logger.info(f"[DELETE-ROOM] ボイスチャンネル削除成功: {voice_channel_id}")
            except Exception as e:
                logger.error(f"[DELETE-ROOM] ボイスチャンネル削除失敗: {voice_channel_id} - {e}")
        else:
            logger.warning(f"[DELETE-ROOM] ボイスチャンネル見つからず: {voice_channel_id}")
    
    # ========== 4. テキストチャンネル削除（現在のチャンネル以外） ==========
    if text_channel_id and text_channel_id != interaction.channel.id:
        text_channel = interaction.guild.get_channel(text_channel_id)
        if text_channel:
            try:
                await text_channel.delete()
                deletion_results["text_channel"] = True
                logger.info(f"[DELETE-ROOM] テキストチャンネル削除成功: {text_channel_id}")
            except Exception as e:
                logger.error(f"[DELETE-ROOM] テキストチャンネル削除失敗: {text_channel_id} - {e}")
        else:
            logger.warning(f"[DELETE-ROOM] テキストチャンネル見つからず: {text_channel_id}")
    
    # ========== 5. ロール削除 ==========
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            try:
                await role.delete()
                deletion_results["role"] = True
                logger.info(f"[DELETE-ROOM] ロール削除成功: {role_id}")
            except Exception as e:
                logger.error(f"[DELETE-ROOM] ロール削除失敗: {role_id} - {e}")
        else:
            logger.warning(f"[DELETE-ROOM] ロール見つからず: {role_id}")
    
    # ========== 6. データベース削除 ==========
    try:
        remove_room(text_channel_id=text_channel_id, voice_channel_id=voice_channel_id)
        deletion_results["database"] = True
        logger.info(f"[DELETE-ROOM] データベース削除成功")
    except Exception as e:
        logger.error(f"[DELETE-ROOM] データベース削除失敗: {e}")
    
    # ========== 7. 管理者ログ記録 ==========
    log_action = "デバッグ部屋削除" if is_debug_room else "部屋削除"
    permission_type = "管理者" if is_admin else "作成者"
    
    add_admin_log(
        log_action, 
        interaction.user.id, 
        creator_id, 
        f"種別:{room_type} テキスト:{text_channel_id} ボイス:{voice_channel_id} 権限:{permission_type} 用途:{details or '未設定'}"
    )
    
    # ========== 8. 現在のチャンネル削除（最後） ==========
    if interaction.channel.id == text_channel_id:
        try:
            # 少し待機してから削除（他の処理が完了するまで）
            await asyncio.sleep(1)
            await interaction.channel.delete()
            deletion_results["current_channel"] = True
            logger.info(f"[DELETE-ROOM] 現在のチャンネル削除成功: {interaction.channel.id}")
        except Exception as e:
            logger.error(f"[DELETE-ROOM] 現在のチャンネル削除失敗: {interaction.channel.id} - {e}")
    
    # ========== 9. 空カテゴリ削除 ==========
    if category:
        try:
            # カテゴリの状態を再取得して確認
            updated_category = interaction.guild.get_channel(category.id)
            if updated_category and len(updated_category.channels) == 0:
                await updated_category.delete()
                deletion_results["category"] = True
                logger.info(f"[DELETE-ROOM] 空カテゴリ削除成功: {category.name}")
            else:
                logger.info(f"[DELETE-ROOM] カテゴリ削除スキップ: {category.name} (チャンネル数: {len(updated_category.channels) if updated_category else 'None'})")
        except Exception as e:
            logger.error(f"[DELETE-ROOM] カテゴリ削除失敗: {category.name} - {e}")
    
    # ========== 10. 削除結果サマリー ==========
    success_count = sum(1 for result in deletion_results.values() if result)
    total_count = len([k for k, v in deletion_results.items() if k != "current_channel" or interaction.channel.id == text_channel_id])
    
    logger.info(f"[DELETE-ROOM] 削除完了: 種別={room_type}, 成功={success_count}/{total_count}, 詳細={deletion_results}")

@bot.event
async def on_guild_channel_delete(channel):
    """チャンネル削除時の処理とカテゴリ自動削除"""
    if isinstance(channel, (discord.VoiceChannel, discord.TextChannel)):
        # データベースから部屋情報を削除
        r_id, c_id, other_id = remove_room(
            text_channel_id=channel.id if isinstance(channel, discord.TextChannel) else None,
            voice_channel_id=channel.id if isinstance(channel, discord.VoiceChannel) else None
        )
        
        # 関連ロール削除
        if r_id:
            role = channel.guild.get_role(r_id)
            if role:
                try:
                    await role.delete()
                    logger.info(f"ロール {role.id} を削除しました")
                except Exception as e:
                    logger.warning(f"ロール {role.id} の削除に失敗: {e}")

        # 関連チャンネル削除
        if other_id:
            other_channel = channel.guild.get_channel(other_id)
            if other_channel:
                try:
                    await other_channel.delete()
                    logger.info(f"関連チャンネル {other_id} を削除しました")
                except Exception as e:
                    logger.error(f"関連チャンネル {other_id} の削除に失敗: {e}")

        # カテゴリの空判定と削除
        category = channel.category
        if category and len(category.channels) == 0:
            try:
                await category.delete()
                logger.info(f"[DeleteCategory] {category.name}")
            except discord.NotFound:
                logger.warning(f"カテゴリ {category.name} は既に削除されているようです")
            except Exception as e:
                logger.warning(f"カテゴリ {category.name} の削除に失敗: {e}")

        add_admin_log("自動部屋削除", None, c_id, f"channel={channel.id}")

# =====================================================
# 管理者用デバッグ部屋機能
# =====================================================
@bot.tree.command(name="create-debug-room", description="管理者専用のデバッグ部屋を作成（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    room_name="部屋名（省略時は自動生成）",
    purpose="用途・目的（省略可）"
)
async def create_debug_room(interaction: discord.Interaction, room_name: str = None, purpose: str = None):
    """
    管理者専用デバッグ部屋作成コマンド
    
    【特徴】
    - 管理者のみが作成・参加可能
    - 通常ユーザーには見えない
    - /delete-room で削除可能
    - ブラックリスト機能は適用されない
    """
    
    logger.info(f"[CREATE-DEBUG-ROOM] 実行開始: 管理者={interaction.user.id}, 部屋名={room_name}")
    
    # 既存のデバッグ部屋チェック
    existing_rooms = get_rooms_by_creator(interaction.user.id)
    if existing_rooms:
        await send_interaction_message(interaction, 
            "❌ すでに部屋を作成しています。新しい部屋を作成する前に、既存の部屋を削除してください。",
            ephemeral=True
        )
        return
    
    # 部屋名の設定
    if not room_name:
        timestamp = datetime.datetime.now().strftime("%m%d_%H%M")
        room_name = f"DEBUG_{interaction.user.display_name}_{timestamp}"
    else:
        room_name = f"DEBUG_{room_name}"
    
    # 用途の設定
    if not purpose:
        purpose = "管理者用デバッグ・テスト"
    
    await send_interaction_message(interaction, "🔧 デバッグ部屋を作成しています...", ephemeral=True)
    
    try:
        # ========== 1. カテゴリ作成または取得 ==========
        category_name = "🔧 管理者専用デバッグ"
        category = discord.utils.get(interaction.guild.categories, name=category_name)
        
        if not category:
            # 管理者のみ見えるカテゴリを作成
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
            }
            
            # 管理者ロールがある場合は追加
            for role in interaction.guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True)
            
            category = await interaction.guild.create_category(category_name, overwrites=overwrites)
            logger.info(f"[CREATE-DEBUG-ROOM] 管理者専用カテゴリ作成: {category.id}")
        
        # ========== 2. 権限設定 ==========
        # 管理者のみアクセス可能な権限設定
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
        }
        
        # 管理者ロールを持つ全ユーザーに権限付与
        for role in interaction.guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    connect=True,
                    speak=True,
                    manage_channels=True
                )
        
        # ========== 3. チャンネル作成 ==========
        # テキストチャンネル作成
        text_channel = await interaction.guild.create_text_channel(
            name=f"{room_name}-chat",
            category=category,
            overwrites=overwrites
        )
        
        # ボイスチャンネル作成
        voice_channel = await interaction.guild.create_voice_channel(
            name=f"{room_name}-voice",
            category=category,
            overwrites=overwrites
        )
        
        # ========== 4. 非表示ロール作成（削除機能との互換性） ==========
        random_salt = secrets.token_hex(8)
        hashed = hashlib.sha256(f"{random_salt}:{interaction.user.id}".encode()).hexdigest()[:12]
        role_name = f"debug_{hashed}"
        
        debug_role = await interaction.guild.create_role(
            name=role_name,
            permissions=discord.Permissions.none(),
            hoist=False,
            mentionable=False
        )
        
        # ========== 5. データベース登録 ==========
        room_id = add_room(
            text_channel.id, 
            voice_channel.id, 
            interaction.user.id, 
            debug_role.id, 
            "debug",  # 特別な性別設定
            purpose
        )
        logger.info(f"[DEBUG] チャンネルID: text={text_channel.id if text_channel else 'None'}, voice={voice_channel.id if voice_channel else 'None'}")

        # ========== 6. 初期メッセージ送信 ==========
        embed = discord.Embed(
            title="🔧 デバッグ部屋作成完了",
            description="管理者専用のデバッグ部屋が作成されました",
            color=discord.Color.orange()
        )
        
        embed.add_field(name="👑 作成者", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎯 用途", value=purpose, inline=True)
        embed.add_field(name="📅 作成日時", value=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        
        embed.add_field(name="🔐 アクセス権限", value="管理者のみ", inline=False)
        embed.add_field(name="🗑️ 削除方法", value="`/delete-room` コマンドで削除できます", inline=False)
        
        embed.add_field(name="📋 注意事項", value="""
        • この部屋は管理者のみが参加できます
        • 通常ユーザーには見えません
        • ブラックリスト機能は適用されません
        • デバッグ・テスト用途での利用を想定しています
        """, inline=False)
        
        await text_channel.send(embed=embed)
        
        # ========== 7. 管理者ログ記録 ==========
        add_admin_log(
            "デバッグ部屋作成", 
            interaction.user.id, 
            None, 
            f"部屋名:{room_name} テキスト:{text_channel.id} ボイス:{voice_channel.id} 用途:{purpose}"
        )
        
        # ========== 8. 完了通知 ==========
        await send_interaction_message(
            interaction, 
            f"✅ デバッグ部屋を作成しました！\n"
            f"📝 テキスト: {text_channel.mention}\n"
            f"🎤 ボイス: {voice_channel.mention}\n"
            f"🎯 用途: {purpose}", 
            ephemeral=True
        )
        
        logger.info(f"[CREATE-DEBUG-ROOM] 作成完了: room_id={room_id}, text={text_channel.id}, voice={voice_channel.id}")
        
    except Exception as e:
        logger.error(f"[CREATE-DEBUG-ROOM] 作成失敗: {e}")
        await send_interaction_message(
            interaction, 
            f"❌ デバッグ部屋の作成に失敗しました: {str(e)}", 
            ephemeral=True
        )

# =====================================================
# デバッグ・テスト用コマンド
# =====================================================
@bot.tree.command(name="test-room-creation", description="部屋作成をテスト（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def test_room_creation(interaction: discord.Interaction):
    """部屋作成のテスト"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        await create_room_with_gender(interaction, "all", room_message="テスト部屋")
        await interaction.followup.send("✅ テスト部屋作成完了", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ テスト部屋作成失敗: {str(e)}", ephemeral=True)

@bot.tree.command(name="test-get-room-info", description="get_room_info関数をテスト（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def test_get_room_info(interaction: discord.Interaction):
    """get_room_info関数の動作テスト"""
    
    logger.info(f"[TEST-GET-ROOM-INFO] 実行: チャンネル={interaction.channel.id}")
    
    # 元の関数を使用
    creator_id, role_id, text_channel_id, voice_channel_id = get_room_info(interaction.channel.id)
    
    embed = discord.Embed(
        title="🧪 get_room_info テスト結果",
        color=discord.Color.yellow()
    )
    
    embed.add_field(name="📋 結果", value=f"""
    **creator_id**: `{creator_id}`
    **role_id**: `{role_id}`
    **text_channel_id**: `{text_channel_id}`
    **voice_channel_id**: `{voice_channel_id}`
    **現在のチャンネル**: `{interaction.channel.id}`
    """, inline=False)
    
    # 結果の判定
    if creator_id is None:
        embed.add_field(name="❌ 判定", value="部屋として認識されていません", inline=False)
    else:
        embed.add_field(name="✅ 判定", value="部屋として認識されています", inline=False)
    
    await send_interaction_message(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="quick-db-check", description="データベースの簡単な確認（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def quick_db_check(interaction: discord.Interaction):
    """
    データベースの簡単な確認（高速レスポンス）
    """
    await interaction.response.defer(ephemeral=True)
    
    logger.info(f"[QUICK-DB-CHECK] 実行: 管理者={interaction.user.id}")
    
    try:
        with safe_db_context() as conn:
            cursor = conn.cursor()
            
            # 現在のチャンネルが登録されているかチェック
            cursor.execute("""
                SELECT creator_id, gender, details FROM rooms 
                WHERE text_channel_id = ? OR voice_channel_id = ?
            """, (interaction.channel.id, interaction.channel.id))
            current_room = cursor.fetchone()
            
            # 全部屋数を取得
            cursor.execute("SELECT COUNT(*) FROM rooms")
            total_rooms = cursor.fetchone()[0]
            
            # 部屋タイプ別の数を取得
            cursor.execute("SELECT gender, COUNT(*) FROM rooms GROUP BY gender")
            room_types = cursor.fetchall()
            
            embed = discord.Embed(
                title="🔍 データベース簡単確認",
                color=discord.Color.green()
            )
            
            # 現在のチャンネル情報
            if current_room:
                creator_id, gender, details = current_room
                creator = interaction.guild.get_member(creator_id)
                creator_name = creator.display_name if creator else f"ID:{creator_id}"
                room_type = "🔧 デバッグ部屋" if gender == "debug" else f"💬 {gender}部屋"
                
                embed.add_field(
                    name="✅ 現在のチャンネル",
                    value=f"""
                    **状態**: 部屋として登録済み
                    **種別**: {room_type}
                    **作成者**: {creator_name}
                    **詳細**: {details or "なし"}
                    """,
                    inline=False
                )
            else:
                embed.add_field(
                    name="❌ 現在のチャンネル",
                    value="部屋として登録されていません",
                    inline=False
                )
            
            # 全体統計
            type_summary = []
            for gender, count in room_types:
                if gender == "debug":
                    type_summary.append(f"🔧 デバッグ部屋: {count}件")
                else:
                    type_summary.append(f"💬 {gender}部屋: {count}件")
            
            embed.add_field(
                name="📊 全体統計",
                value=f"""
                **総部屋数**: {total_rooms}件
                {chr(10).join(type_summary) if type_summary else "部屋なし"}
                """,
                inline=False
            )
            
            # 推奨アクション
            actions = []
            if not current_room:
                actions.append("💡 `/force-register-room` で部屋を登録")
            if current_room:
                actions.append("🗑️ `/delete-room` で部屋を削除")
            actions.append("🔍 `/debug-database` で詳細確認")
            
            embed.add_field(
                name="🔧 推奨アクション",
                value="\n".join(actions),
                inline=False
            )
            
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"[QUICK-DB-CHECK] エラー: {e}")
        await interaction.followup.send(f"❌ データベース確認に失敗: {str(e)}", ephemeral=True)

# =====================================================
# ブラックリスト管理UI
# =====================================================
@bot.tree.command(name="bl-manage", description="ブラックリスト管理のボタン設置 (管理者専用)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(action="addで追加、removeで解除")
async def bl_manage_setup(interaction: discord.Interaction, action: str):
    """ブラックリスト管理UI設置"""
    if action not in ("add", "remove"):
        await send_interaction_message(interaction, "action は 'add' または 'remove' を指定してください。", ephemeral=True)
        return

    view = GlobalBlacklistButtonView(action)
    msg = "## 💔ブラックリスト追加ボタン" if action == "add" else "## 💙ブラックリスト解除ボタン"
    await send_interaction_message(interaction, msg, view=view, ephemeral=False)

class GlobalBlacklistButtonView(discord.ui.View):
    """グローバルブラックリストボタンView"""
    def __init__(self, action: str):
        super().__init__(timeout=None)
        self.action = action

        button_style = discord.ButtonStyle.danger if action == "add" else discord.ButtonStyle.blurple
        button = discord.ui.Button(label="はじめる", style=button_style)
        button.callback = self.manage_button_callback
        self.add_item(button)

    async def manage_button_callback(self, interaction: discord.Interaction):
        view = PersonalBlacklistManageView(self.action)
        await send_interaction_message(interaction, "対象ユーザーを選択して、確認を押してください。", view=view, ephemeral=True)

class PersonalBlacklistManageView(discord.ui.View):
    """個人ブラックリスト管理View"""
    def __init__(self, action: str):
        super().__init__(timeout=60)
        self.action = action
        self.selected_users: list[discord.Member] = []

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="対象ユーザーを検索して選択（複数可）",
        min_values=1,
        max_values=25
    )
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        self.selected_users = select.values
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        
    @discord.ui.button(label="確認", style=discord.ButtonStyle.primary, row=2)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_users:
            await send_interaction_message(interaction, "⚠️ ユーザーが選択されていません。", ephemeral=True)
            return

        title = "🛑 以下のユーザーをブラックリストに **追加** しますか？" if self.action == "add" else "🛑 以下のユーザーをブラックリストから **解除** しますか？"
        embed = discord.Embed(title=title, color=discord.Color.red())
        
        for member in self.selected_users:
            embed.add_field(name=member.display_name, value=f"`{member.name}` (ID: {member.id})", inline=False)

        confirm_view = PersonalBlacklistConfirmView(self.action, self.selected_users)
        await send_interaction_message(interaction, embed=embed, view=confirm_view, ephemeral=True)

class PersonalBlacklistConfirmView(discord.ui.View):
    """ブラックリスト確認View"""
    def __init__(self, action: str, users: list[discord.Member]):
        super().__init__(timeout=30)
        self.action = action
        self.users = users

    @discord.ui.button(label="はい", style=discord.ButtonStyle.danger, row=0)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        already_in_list = []
        already_not_in_list = []
        user_id = interaction.user.id
        bl = get_blacklist(user_id)

        for member in self.users:
            target_id = member.id
            if self.action == "add":
                if target_id in bl:
                    already_in_list.append(member)
                else:
                    add_to_blacklist(user_id, target_id)
            else:  # remove
                if target_id not in bl:
                    already_not_in_list.append(member)
                else:
                    remove_from_blacklist(user_id, target_id)

        base_msg = "✅ ブラックリストに追加しました。" if self.action == "add" else "✅ ブラックリストから解除しました。"
        msg = base_msg

        if self.action == "add" and already_in_list:
            names = ', '.join([m.display_name for m in already_in_list])
            msg += f"\n⚠️ すでに登録済みのユーザー: {names}"
        if self.action == "remove" and already_not_in_list:
            names = ', '.join([m.display_name for m in already_not_in_list])
            msg += f"\n⚠️ 登録されていないユーザー: {names}"

        await send_interaction_message(interaction, msg, ephemeral=True)

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary, row=1)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_interaction_message(interaction, "キャンセルしました。", ephemeral=True)

# =====================================================
# ブラックリスト一覧機能
# =====================================================
@bot.tree.command(name="setup-bl-list-button", description="ブラックリスト一覧ボタンを設置（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_bl_list_button(interaction: discord.Interaction):
    """ブラックリスト一覧ボタンを設置"""
    view = ShowBlacklistButtonView()
    await interaction.channel.send(
        "\n\n📌苦手な人はいませんか？\n📌ブラックリスト設定は部屋作成前を推奨しています◎\n\n## 📕ブラックリスト一覧ボタン",
        view=view
    )
    await send_interaction_message(interaction, "ブラックリスト一覧ボタンを設置しました。", ephemeral=True)

class ShowBlacklistButtonView(discord.ui.View):
    """ブラックリスト一覧表示ボタンView"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ブラックリストを見る", style=discord.ButtonStyle.success)
    async def show_bl_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        blacklist = get_blacklist(interaction.user.id)
        if not blacklist:
            await send_interaction_message(interaction, "あなたのブラックリストは空です。", ephemeral=True)
            return

        embed = discord.Embed(title="あなたのブラックリスト", color=discord.Color.red())
        for user_id in blacklist:
            member = interaction.guild.get_member(user_id)
            user_name = member.display_name if member else f"ID: {user_id}"
            embed.add_field(name=user_name, value=f"ID: {user_id}", inline=False)

        try:
            await interaction.user.send(embed=embed)
            await send_interaction_message(interaction, "✅ DMでブラックリストを送信しました。", ephemeral=True)
        except:
            await send_interaction_message(interaction, 
                "⚠️ DMを送信できませんでした。DMが許可されているか確認してください。",
                ephemeral=True
            )

# =====================================================
# 募集一覧機能
# =====================================================
class ShowRoomsView(discord.ui.View):
    """募集一覧表示ボタンView"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="募集を見る", style=discord.ButtonStyle.success)
    async def show_rooms_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_show_rooms(interaction)

async def handle_show_rooms(interaction: discord.Interaction):
    """募集一覧を表示する処理"""
    member = interaction.user
    viewable_genders = get_user_genders(member)
    
    if not viewable_genders:
        await send_interaction_message(interaction, "現在、募集はありません。", ephemeral=True)
        return

    # DBから性別に合致する部屋一覧を取得
    with safe_db_context() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(viewable_genders))
        query = f"""
            SELECT creator_id, text_channel_id, voice_channel_id, details, gender
            FROM rooms
            WHERE gender IN ({placeholders})
        """
        cursor.execute(query, tuple(viewable_genders))
        rows = cursor.fetchall()

    if not rows:
        await send_interaction_message(interaction, "現在、募集はありません。", ephemeral=True)
        return

    embed = discord.Embed(
        title="募集一覧",
        description="募集部屋の一覧を表示します。",
        color=discord.Color.green()
    )

    count = 0
    for (creator_id, text_channel_id, voice_channel_id, details, gender) in rows:
        # 作成者のブラックリストチェック
        creator_blacklist = get_blacklist(creator_id)
        if member.id in creator_blacklist:
            continue

        # 満室チェック
        voice_channel = interaction.guild.get_channel(voice_channel_id)
        if voice_channel:
            human_members = [m for m in voice_channel.members if not m.bot]
            if len(human_members) >= 2:
                continue

        # 表示処理
        creator = interaction.guild.get_member(creator_id)
        creator_name = creator.display_name if creator else f"UserID: {creator_id}"
        channel = interaction.guild.get_channel(text_channel_id)
        channel_mention = channel.mention if channel else f"#{text_channel_id} (削除済み)"

        # 作成者の性別判定
        male_role = discord.utils.get(interaction.guild.roles, name="男性")
        female_role = discord.utils.get(interaction.guild.roles, name="女性")
        
        creator_gender_jp = "不明"
        if creator:
            if male_role in creator.roles and female_role in creator.roles:
                creator_gender_jp = "両方！？"
            elif male_role in creator.roles:
                creator_gender_jp = "男性"
            elif female_role in creator.roles:
                creator_gender_jp = "女性"

        embed.add_field(
            name=f"募集者: {creator_name} / {creator_gender_jp}",
            value=f"詳細: \n{details}\n交渉チャンネル: {channel_mention}",
            inline=False
        )
        count += 1

    if count == 0:
        await send_interaction_message(interaction, "現在、募集はありません。", ephemeral=True)
    else:
        await send_interaction_message(interaction, embed=embed, ephemeral=True)

# =====================================================
# 管理者用コマンド
# =====================================================
@bot.tree.command(name="setup-lobby", description="部屋作成ボタン付きメッセージを送信（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_lobby(interaction: discord.Interaction):
    """部屋作成ボタン付きメッセージを設置"""
    view = GenderRoomView(timeout=None)
    text = (
        "## 📢募集開始ボタン\n"
        "募集を見せたい性別を選んでください！\n"
    )
    await interaction.channel.send(text, view=view)
    await send_interaction_message(interaction, "部屋作成ボタン付きメッセージを設置しました！", ephemeral=True)

@bot.tree.command(name="setup-room-list-button", description="募集一覧を表示するボタンを設置（管理者用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_room_list_button(interaction: discord.Interaction):
    """募集一覧ボタンを設置"""
    view = ShowRoomsView()
    await interaction.channel.send("## 👀募集一覧ボタン\n現在の募集の一覧はこちらからどうぞ！\n", view=view)
    await send_interaction_message(interaction, "募集一覧ボタンを設置しました！", ephemeral=True)

@bot.tree.command(name="setup-blacklist-help", description="ブラックリスト関連のコマンド一覧を設置（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_blacklist_help(interaction: discord.Interaction):
    """ブラックリストヘルプを設置"""
    embed = discord.Embed(
        title="ブラックリスト機能 コマンド一覧",
        description=(
            "🚫ブラックリストは部屋を作るときに参照されます！\n"
            "🚫部屋の作成前に、ブラックリストの追加・確認をお忘れなく！\n\n"
            "以下のコマンドを使用すると、ブラックリストの管理ができます。"
        ),
        color=discord.Color.red()
    )
    embed.add_field(
        name="/bl-add",
        value="指定したユーザーをブラックリストに追加します。\n例: `/bl-add @ユーザー [理由]`",
        inline=False
    )
    embed.add_field(
        name="/bl-remove",
        value="指定したユーザーをブラックリストから削除します。\n例: `/bl-remove @ユーザー`",
        inline=False
    )
    embed.add_field(
        name="/bl-list",
        value="あなたのブラックリストに登録されているユーザー一覧を表示します。\n例: `/bl-list`",
        inline=False
    )
    
    await interaction.channel.send(embed=embed)
    await send_interaction_message(interaction, "ブラックリストコマンド一覧を設置しました。", ephemeral=True)

@bot.tree.command(name="admin-logs", description="管理者ログを表示（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(limit="表示する件数")
async def admin_logs(interaction: discord.Interaction, limit: int = 10):
    """管理者ログを表示"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT action, user_id, target_id, details, timestamp 
            FROM admin_logs 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (limit,))
        logs = cursor.fetchall()
    
    if not logs:
        await send_interaction_message(interaction, "ログはありません。", ephemeral=True)
        return
    
    embed = discord.Embed(title="管理者ログ", color=discord.Color.blue())
    for i, (action, user_id, target_id, details, timestamp) in enumerate(logs):
        user = interaction.guild.get_member(user_id) if user_id else None
        target = interaction.guild.get_member(target_id) if target_id else None
        user_name = user.display_name if user else f"ID: {user_id}" if user_id else "システム"
        target_name = target.display_name if target else f"ID: {target_id}" if target_id else "なし"
        
        embed.add_field(
            name=f"{i+1}. {action} ({timestamp})",
            value=f"実行者: {user_name}\n対象: {target_name}\n詳細: {details}",
            inline=False
        )
    
    await send_interaction_message(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="clear-rooms", description="全ての通話募集部屋を削除（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def clear_rooms(interaction: discord.Interaction):
    """全ての通話募集部屋を削除"""
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT text_channel_id, voice_channel_id, role_id FROM rooms")
        rooms = cursor.fetchall()
    
    if not rooms:
        await send_interaction_message(interaction, "削除する部屋はありません。", ephemeral=True)
        return
    
    count = 0
    for text_channel_id, voice_channel_id, role_id in rooms:
        try:
            # チャンネル削除
            text_channel = interaction.guild.get_channel(text_channel_id)
            if text_channel:
                await text_channel.delete()
                logger.info(f"テキストチャンネル {text_channel_id} を削除しました")
            
            voice_channel = interaction.guild.get_channel(voice_channel_id)
            if voice_channel:
                await voice_channel.delete()
                logger.info(f"ボイスチャンネル {voice_channel_id} を削除しました")
            
            # ロール削除
            if role_id:
                role = interaction.guild.get_role(role_id)
                if role:
                    await role.delete()
                    logger.info(f"ロール {role_id} を削除しました")
            
            count += 1
        except Exception as e:
            logger.error(f"部屋の削除に失敗: {str(e)}")
    
    # データベースクリア
    with safe_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rooms")
    
    add_admin_log("全部屋削除", interaction.user.id, None, f"{count}個の部屋を削除")
    await send_interaction_message(interaction, f"✅ {count}個の部屋を削除しました。", ephemeral=True)

@bot.tree.command(name="sync", description="スラッシュコマンドを手動で同期")
async def sync(interaction: discord.Interaction):
    """コマンド同期"""
    await bot.tree.sync()
    await send_interaction_message(interaction, "✅ コマンドを手動で同期しました！", ephemeral=True)

@bot.tree.command(name="backup-now", description="バックアップを手動で実行（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def backup_now(interaction: discord.Interaction):
    """手動でバックアップを実行"""
    await perform_backup()
    add_admin_log("手動バックアップ", interaction.user.id)
    await send_interaction_message(interaction, "✅ バックアップを実行しました。", ephemeral=True)

# =====================================================
# バックアップ機能
# =====================================================

async def perform_backup():
    """バックアップ処理を実行"""
    now = datetime.datetime.now()
    logger.info(f"[DEBUG] backup_task 呼び出し {now}")
    
    # ログファイルの古いエントリを削除
    cutoff_date = (now - datetime.timedelta(days=LOG_KEEP_DAYS)).isoformat()
    try:
        with safe_db_context() as conn:
            cursor = conn.cursor()
            logger.info(f"[DEBUG] DELETE条件: timestamp < {cutoff_date}")
            cursor.execute("DELETE FROM admin_logs WHERE timestamp < ?", (cutoff_date,))
            logger.info(f"[DEBUG] 削除件数: {cursor.rowcount}")
    except Exception as e:
        logger.error(f"[ERROR] ログ削除失敗: {e}")

    # バックアップファイルの作成
    os.makedirs(BACKUP_FOLDER, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    log_file = "bot.log"
    db_file = "blacklist.db"
    
    backup_log_name = f"botlog_{timestamp}.log"
    backup_db_name = f"blacklist_{timestamp}.db"
    
    try:
        if os.path.exists(log_file):
            shutil.copy2(log_file, os.path.join(BACKUP_FOLDER, backup_log_name))
        if os.path.exists(db_file):
            shutil.copy2(db_file, os.path.join(BACKUP_FOLDER, backup_db_name))
    except Exception as e:
        logger.error(f"[BackupError] バックアップ中にエラー: {e}")
        return
    
    # 古いバックアップファイルを削除
    seven_days_ago = now - datetime.timedelta(days=7)
    for file_path in glob.glob(os.path.join(BACKUP_FOLDER, "*")):
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
            if mtime < seven_days_ago:
                os.remove(file_path)
        except Exception as e:
            logger.error(f"[CleanupError] 古いバックアップ削除時にエラー: {e}")

    # Discordの特定チャンネルへバックアップファイルを送信
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if channel is None:
        logger.warning(f"[BackupWarn] 指定チャンネル (ID={BACKUP_CHANNEL_ID}) が見つかりません。送信をスキップします。")
        return

    files_to_send = []
    backup_log_path = os.path.join(BACKUP_FOLDER, backup_log_name)
    backup_db_path = os.path.join(BACKUP_FOLDER, backup_db_name)

    if os.path.exists(backup_log_path):
        files_to_send.append(discord.File(backup_log_path))
    if os.path.exists(backup_db_path):
        files_to_send.append(discord.File(backup_db_path))

    if files_to_send:
        await channel.send(
            content=f"バックアップ完了: {timestamp}\n古いバックアップ(7日以上)は自動削除しています。",
            files=files_to_send
        )

@tasks.loop(time=datetime.time(hour=12, minute=0, second=0))
async def daily_backup_task():
    """毎日12:00に実行されるバックアップタスク"""
    await perform_backup()

@daily_backup_task.before_loop
async def before_daily_backup_task():
    """バックアップタスク開始前の準備"""
    await bot.wait_until_ready()

# =====================================================
# イベントハンドラー
# =====================================================
@bot.event
async def on_ready():
    """Bot起動時の処理"""
    logger.info(f'BOTにログインしました: {bot.user.name}')
    print(f'BOTにログインしました: {bot.user.name}')
    
    # 初期化
    init_db()
    if not daily_backup_task.is_running():
        daily_backup_task.start()
        logger.info(f"[DEBUG] backup_task 開始 {datetime.datetime.now()}")
    
    try:
        await bot.tree.sync()
        logger.info("Slashコマンドの同期に成功しました。")
    except Exception as e:
        logger.error(f"Slashコマンドの同期に失敗: {e}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    """全てのインタラクションをログに記録し、連続実行を制限"""
    user_id = interaction.user.id
    custom_id = interaction.data.get("custom_id", "unknown")
    command_name = interaction.command.name if interaction.command else "unknown"

    # --- ログ記録 ---
    if interaction.type == discord.InteractionType.application_command:
        logger.info(f"[CommandExecuted] {interaction.user.display_name}({user_id}) ran /{command_name}")
        add_admin_log("Slashコマンド実行", user_id, details=f"/{command_name}")
    elif interaction.type == discord.InteractionType.component and interaction.data.get("component_type") == 2:
        logger.info(f"[ButtonClicked] {interaction.user.display_name}({user_id}) pressed button custom_id={custom_id}")
        add_admin_log("ボタンクリック", user_id, details=f"button_id={custom_id}")


@bot.event
async def on_command_error(ctx, error):
    """コマンドエラーハンドリング"""
    if isinstance(error, commands.errors.MissingRequiredArgument):
        await ctx.send("❌ コマンドの引数が不足しています。", ephemeral=True)
    elif isinstance(error, commands.errors.MissingPermissions):
        await ctx.send("❌ このコマンドを実行する権限がありません。", ephemeral=True)
    elif isinstance(error, commands.errors.CommandNotFound):
        pass  # コマンドが見つからない場合は無視
    else:
        logger.error(f"コマンドエラー: {str(error)}")
        await ctx.send(f"❌ エラーが発生しました: {str(error)}", ephemeral=True)

# =====================================================
# メイン実行部分
# =====================================================
if __name__ == "__main__":
    # 環境変数の読み込み
    load_dotenv()
    
    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if not TOKEN:
        logger.error("DISCORD_TOKENが設定されていません。.envファイルを確認してください。")
        exit(1)
    
    # Bot実行
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Botの起動に失敗しました: {e}")
        exit(1)
