import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import sqlite3
import os
from datetime import datetime
import logging
import secrets
import hashlib

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("SleepBot")

# BOTの設定
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

DB_PATH = 'blacklist.db'

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# データベース初期化
def init_db():
    with get_db_connection() as conn:
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

@bot.event
async def on_ready():
    logger.info(f'BOTにログインしました: {bot.user.name}')
    init_db()
    try:
        await bot.tree.sync()
        logger.info("Slashコマンドの同期に成功しました。")
    except Exception as e:
        logger.error(f"Slashコマンドの同期に失敗: {e}")


    # DB関連ーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーー
    # 管理者ログ機能
def add_admin_log(action, user_id, target_id=None, details=""):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO admin_logs (action, user_id, target_id, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (action, user_id, target_id, details, datetime.now())
        )
        conn.commit()
    logger.info(f"管理者ログ: {action} - ユーザー: {user_id} - 対象: {target_id} - 詳細: {details}")


# ブラックリスト機能
def add_to_blacklist(owner_id, blocked_user_id, reason=""):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO user_blacklists (owner_id, blocked_user_id, reason, added_at) VALUES (?, ?, ?, ?)",
            (owner_id, blocked_user_id, reason, datetime.now())
        )
        conn.commit()
    logger.info(f"ブラックリスト追加: ユーザー {owner_id} が {blocked_user_id} をブロック - 理由: {reason}")

def remove_from_blacklist(owner_id, blocked_user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_blacklists WHERE owner_id = ? AND blocked_user_id = ?", 
                      (owner_id, blocked_user_id))
        result = cursor.rowcount > 0
        conn.commit()
    if result:
        logger.info(f"ブラックリスト削除: ユーザー {owner_id} が {blocked_user_id} のブロックを解除")
    return result

def get_blacklist(owner_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT blocked_user_id FROM user_blacklists WHERE owner_id = ?", (owner_id,))
        blacklist = [row[0] for row in cursor.fetchall()]
    return blacklist

#スラッシュコマンド
@bot.tree.command(name="bl-add", description="ユーザーをブラックリストに追加")
@app_commands.describe(
    user="ブラックリストに追加するユーザー",
    reason="理由（省略可）"
)
async def blacklist_add(interaction: discord.Interaction, user: discord.Member, reason: str = "理由なし"):
    """ユーザーをブラックリストに追加"""
    if user.id == interaction.user.id:
        await interaction.response.send_message(" 自分自身をブラックリストに追加することはできません。", ephemeral=True)
        return
    add_to_blacklist(interaction.user.id, user.id, reason)
    add_admin_log("ブラックリスト追加", interaction.user.id, user.id, reason)
    await interaction.response.send_message(f"✅ {user.mention} をあなたのブラックリストに追加しました。", ephemeral=True)

@bot.tree.command(name="bl-remove", description="ユーザーをブラックリストから削除")
@app_commands.describe(
    user="ブラックリストから削除するユーザー"
)
async def blacklist_remove(interaction: discord.Interaction, user: discord.Member):
    """ユーザーをブラックリストから削除"""
    if remove_from_blacklist(interaction.user.id, user.id):
        add_admin_log("ブラックリスト削除", interaction.user.id, user.id)
        await interaction.response.send_message(f"✅ {user.mention} をあなたのブラックリストから削除しました。", ephemeral=True)
    else:
        await interaction.response.send_message(f" {user.mention} はあなたのブラックリストに登録されていません。", ephemeral=True)

@bot.tree.command(name="bl-list", description="自分のブラックリストに登録されているユーザー一覧を表示")
async def blacklist_list(interaction: discord.Interaction):
    """自分のブラックリストに登録されているユーザー一覧を表示"""
    blacklist = get_blacklist(interaction.user.id)
    if not blacklist:
        await interaction.response.send_message("あなたのブラックリストは空です。", ephemeral=True)
        return
    embed = discord.Embed(title="あなたのブラックリスト", color=discord.Color.red())
    for user_id in blacklist:
        member = interaction.guild.get_member(user_id)
        user_name = member.display_name if member else f"ID: {user_id}"
        embed.add_field(name=user_name, value=f"ID: {user_id}", inline=False)
    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("✅ DMでブラックリストを送信しました。", ephemeral=True)
    except:
        await interaction.response.send_message(" DMを送信できませんでした。DMが許可されているか確認してください。", ephemeral=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

#　部屋管理機能ーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーー
# 部屋管理機能
# ④ GenderRoomView 内の各ボタン処理を変更し、Modal を表示するようにする
class GenderRoomView(discord.ui.View):
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)

    @discord.ui.button(label="男性のみ", style=discord.ButtonStyle.primary)
    async def male_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RoomCreationModal(gender="male")
        await interaction.response.send_modal(modal)
        # ※ ここで「self.disable_all_items()」や「edit_message」は行わない。
        #    なぜなら、モーダルを送信した時点でInteractionは応答済みになるため。

    @discord.ui.button(label="女性のみ", style=discord.ButtonStyle.danger)
    async def female_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RoomCreationModal(gender="female")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="どちらでもOK", style=discord.ButtonStyle.secondary)
    async def both_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RoomCreationModal(gender="all")
        await interaction.response.send_modal(modal)

def add_room(text_channel_id, voice_channel_id, creator_id, role_id, gender: str, details: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO rooms (text_channel_id, voice_channel_id, creator_id, created_at, role_id, gender, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text_channel_id, voice_channel_id, creator_id, datetime.now(), role_id, gender, details)
        )
        room_id = cursor.lastrowid
        conn.commit()
    logger.info(f"部屋作成: ユーザー {creator_id} がテキスト:{text_channel_id} ボイス:{voice_channel_id} を作成")
    return room_id

def get_rooms_by_creator(creator_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT text_channel_id, voice_channel_id FROM rooms WHERE creator_id = ?", (creator_id,))
        rooms = cursor.fetchall()
    return rooms

def remove_room(text_channel_id=None, voice_channel_id=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if text_channel_id:
            cursor.execute("SELECT role_id, creator_id, voice_channel_id FROM rooms WHERE text_channel_id = ?", (text_channel_id,))
        elif voice_channel_id:
            cursor.execute("SELECT role_id, creator_id, text_channel_id FROM rooms WHERE voice_channel_id = ?", (voice_channel_id,))
        else:
            return None, None, None
        result = cursor.fetchone()
        if not result:
            return None, None, None
        role_id, creator_id, other_channel_id = result
        if text_channel_id:
            cursor.execute("DELETE FROM rooms WHERE text_channel_id = ?", (text_channel_id,))
            logger.info(f"部屋削除: テキストチャンネル {text_channel_id} を削除")
        elif voice_channel_id:
            cursor.execute("DELETE FROM rooms WHERE voice_channel_id = ?", (voice_channel_id,))
            logger.info(f"部屋削除: ボイスチャンネル {voice_channel_id} を削除")
        conn.commit()
    return role_id, creator_id, other_channel_id

def get_room_info(channel_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT creator_id, role_id, text_channel_id, voice_channel_id FROM rooms WHERE text_channel_id = ? OR voice_channel_id = ?", 
                      (channel_id, channel_id))
        result = cursor.fetchone()
    if not result:
        return None, None, None, None
    return result

# ユーザー入力用の Modal クラス
class RoomCreationModal(discord.ui.Modal, title="募集メッセージ入力"):
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
           # ここで「ボタンクリック時の interaction」を使わず、
        # モーダルの submit 用 interaction をそのまま渡す
        await create_room_with_gender(
            interaction,
            self.gender,
            room_message=self.room_message.value
        )
#部屋作成関数
# 部屋作成コマンド``
async def create_room_with_gender(interaction: discord.Interaction, gender: str, capacity: int = 2,room_message: str = ""):
    """
    ボタンが押された際に実行される部屋作成ロジック。
    gender: 'male', 'female', 'all'
    """
    # 1. 既に部屋があるかチェック
    existing_rooms = get_rooms_by_creator(interaction.user.id)
    if existing_rooms:
        await interaction.response.send_message(
            "❌ すでに部屋を作成しています。新しい部屋を作成する前に、既存の部屋を削除してください。",
            ephemeral=True
        )
        return

    # 2. 部屋名の設定
    room_name = f"{interaction.user.display_name}の通話募集"

    # 3. カテゴリの取得 or 作成
    category_name = f"{interaction.user.display_name}の通話募集-{interaction.user.id}"
    category = discord.utils.get(interaction.guild.categories, name=category_name)
    if not category:
        category = await interaction.guild.create_category(category_name)
        logger.info(f"カテゴリー '{category_name}' を作成しました")

# 4. チャンネル権限設定 (男性向け/女性向け/両方)
    male_role = discord.utils.get(interaction.guild.roles, name="男性")
    female_role = discord.utils.get(interaction.guild.roles, name="女性")

    # デフォルト: 全員が見えない
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False, connect=False),
        interaction.guild.me: discord.PermissionOverwrite(read_messages=False, send_messages=False, connect=False)
    }
    if gender == "male":
        # 男性だけ可視
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        # 女性は不可視
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(read_messages=False, connect=False)
        # @everyone も不可視
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(read_messages=False, connect=False)

    elif gender == "female":
        # 女性だけ可視
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(read_messages=False, connect=False)
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(read_messages=False, connect=False)

    elif gender == "all":
        # 男性だけ可視
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        # 女性も可視
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        # @everyone も不可視
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(read_messages=False, connect=False)

       # ▼▼▼ ここがポイント：ロール名の生成をハッシュ方式に変更 ▼▼▼
    # 衝突を防ぎつつ、誰のロールかわからないように匿名性を担保
    random_salt = secrets.token_hex(8)  # 乱数生成
    raw_string = f"{random_salt}:{interaction.user.id}"
    hashed = hashlib.sha256(raw_string.encode()).hexdigest()[:12]  # 先頭12文字にするなどお好みで
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
        await interaction.response.send_message(f"❌ ロールの作成に失敗しました: {str(e)}", ephemeral=True)
        return
    
    blacklisted_users = get_blacklist(interaction.user.id)
    for user_id in blacklisted_users:
        member = interaction.guild.get_member(user_id)
        if member:
            try:
                await member.add_roles(hidden_role)
                logger.info(f"ユーザー {user_id} に非表示ロール '{role_name}' を付与しました")
            except Exception as e:
                logger.error(f"ロール付与に失敗: {str(e)}")
    
    overwrites = {
        hidden_role: discord.PermissionOverwrite(read_messages=False, view_channel=False, connect=False),
         }
    

    try:
        text_channel = await interaction.guild.create_text_channel(
            name=f"{room_name}-通話交渉",
            category=category,
            overwrites=overwrites
        )
        
        voice_channel = await interaction.guild.create_voice_channel(
            name=f"{room_name}-お部屋",
            category=category,
            overwrites=overwrites
        )
        
        add_room(text_channel.id, voice_channel.id, interaction.user.id, hidden_role.id, gender, room_message)
        add_admin_log("部屋作成", interaction.user.id, None, f"テキスト:{text_channel.id} ボイス:{voice_channel.id}")
        
        await interaction.response.send_message(
            f"✅ 通話募集部屋を作成しました！\nテキスト: {text_channel.mention}\nボイス: {voice_channel.mention}",
            ephemeral=True
        )

        await text_channel.send(
            f"{interaction.user.mention} さんが通話を募集中です！\n部屋の作成者は`/delete-room` コマンドでこの部屋を削除できます。"
        )
        male_notice_role = discord.utils.get(interaction.guild.roles, name="男性募集通知")
        female_notice_role = discord.utils.get(interaction.guild.roles, name="女性募集通知")

        if gender == "male":
            if male_notice_role:
                await text_channel.send(f"{male_notice_role.mention} ")
        elif gender == "female":
            if female_notice_role:
                await text_channel.send(f"{female_notice_role.mention} ")
        elif gender == "all":
            # 両方メンション
            if male_notice_role:
                await text_channel.send(f"{male_notice_role.mention} ")
            if female_notice_role:
                await text_channel.send(f"{female_notice_role.mention} ")
       
        if room_message:
            await text_channel.send(f"📝 募集の詳細\n {room_message}")

    except Exception as e:
        logger.error(f"部屋の作成に失敗: {str(e)}")
        await interaction.response.send_message(f" 部屋の作成に失敗しました: {str(e)}", ephemeral=True)
        try:
            await hidden_role.delete()
            logger.info(f"エラーのためロール '{role_name}' を削除しました")
        except Exception as e_del:
            logger.error(f"エラー後のロール削除に失敗: {str(e_del)}")

#満室時に見えなくなる機能
#満室チェック
@bot.event
async def on_voice_state_update(member, before, after):
    """
    ボイスチャンネルへの入退出が発生するたびに呼び出される。
    before.channel: 退出元のVC
    after.channel:  入室先のVC
    """
    # 1. 退出先( before.channel ) と 入室先( after.channel ) の両方をチェック
    #    どちらも rooms テーブルにある可能性があるため

    channels_to_check = []
    if before.channel is not None:
        channels_to_check.append(before.channel)
    if after.channel is not None:
        channels_to_check.append(after.channel)

    for ch in channels_to_check:
        await check_room_capacity(ch)
async def check_room_capacity(voice_channel: discord.VoiceChannel):
    # DBで検索
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT text_channel_id, creator_id, role_id, gender, details
            FROM rooms
            WHERE voice_channel_id = ?
        """, (voice_channel.id,))
        row = cursor.fetchone()

    if not row:
        return  # このボイスチャンネルは「部屋」ではないので無視

    text_channel_id, creator_id, role_id, gender, details = row
    

    # 現在の人数を取得
    member_count = len(voice_channel.members)

    # チャンネルオブジェクト取得
    text_channel = voice_channel.guild.get_channel(text_channel_id)
    hidden_role = voice_channel.guild.get_role(role_id) if role_id else None

    if member_count >= 2:
        # 満室 → 全員から見えないようにする
        await hide_room(voice_channel, text_channel, hidden_role, creator_id)
    else:
        # 空きあり → 元の公開条件を復元
        await show_room(voice_channel, text_channel, hidden_role, creator_id, gender)

#部屋を隠す        
async def hide_room(
    voice_channel: discord.VoiceChannel,
    text_channel: discord.TextChannel,
    hidden_role: discord.Role,
    creator_id: int
):
    guild = voice_channel.guild
    overwrites = {}

    # 1. まずはデフォルトを「見えない」に
    overwrites[guild.default_role] = discord.PermissionOverwrite(read_messages=False, connect=False)
    # BOTには操作権限が必要
    overwrites[guild.me] = discord.PermissionOverwrite(read_messages=True, send_messages=True, connect=True)

    # 2. hidden_role (ブラックリスト) は引き続き見えない
    if hidden_role:
        overwrites[hidden_role] = discord.PermissionOverwrite(read_messages=False, connect=False)

    # 3. 部屋作成者は常に見えるように
    creator = guild.get_member(creator_id)
    if creator:
        overwrites[creator] = discord.PermissionOverwrite(read_messages=True, connect=True)

    # 4. 現在ボイスチャンネルにいる人たちも見えるように
    for member in voice_channel.members:
        # すでに creator と同じ人かもしれないので重複は上書き
        overwrites[member] = discord.PermissionOverwrite(read_messages=True, connect=True)

    try:
        await text_channel.edit(overwrites=overwrites)
        await voice_channel.edit(overwrites=overwrites)
        logger.info(f"[hide_room] {text_channel.id} / {voice_channel.id} を満室モードにしました")
    except Exception as e:
        logger.error(f"[hide_room] チャンネルの上書きに失敗: {e}")


#部屋を再び見せる
async def show_room(voice_channel: discord.VoiceChannel, text_channel: discord.TextChannel,
                    hidden_role: discord.Role, creator_id: int, gender: str):
    """
    部屋を再び公開するときの処理。
    genderに応じて、男性ロール・女性ロールへのPermissionをONにする。
    hidden_role（ブラックリスト用ロール）は見えないままにする。
    """
    guild = voice_channel.guild
    male_role = discord.utils.get(guild.roles, name="男性")
    female_role = discord.utils.get(guild.roles, name="女性")

    # 基本のOverwrites
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False, connect=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, connect=True),
    }

    if hidden_role:
        # ブラックリストロールには見せない
        overwrites[hidden_role] = discord.PermissionOverwrite(read_messages=False, connect=False)

    # genderに応じてON
    if gender == "male":
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(read_messages=False, connect=False)
    elif gender == "female":
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(read_messages=False, connect=False)
    elif gender == "all":
        if male_role:
            overwrites[male_role] = discord.PermissionOverwrite(read_messages=True, connect=True)
        if female_role:
            overwrites[female_role] = discord.PermissionOverwrite(read_messages=True, connect=True)

    try:
        await text_channel.edit(overwrites=overwrites)
        await voice_channel.edit(overwrites=overwrites)
        logger.info(f"[show_room] {text_channel.id} / {voice_channel.id} を再公開しました (gender={gender})")
    except Exception as e:
        logger.error(f"[show_room] チャンネルの上書きに失敗: {e}")


#部屋削除スラッシュコマンド
@bot.tree.command(name="delete-room", description="通話募集部屋を削除")
async def delete_room(interaction: discord.Interaction):
    creator_id, role_id, text_channel_id, voice_channel_id = get_room_info(interaction.channel.id)
    if creator_id is None:
        await interaction.response.send_message("このコマンドは通話募集部屋でのみ使用できます。", ephemeral=True)
        return
    if creator_id != interaction.user.id and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("部屋の作成者または管理者のみが部屋を削除できます。", ephemeral=True)
        return

    await interaction.response.send_message("部屋を削除しています...", ephemeral=True)

    # ---- テキストチャンネル削除 ----
    if text_channel_id:
        text_channel = interaction.guild.get_channel(text_channel_id)
        if text_channel:
            # 現在のチャンネルと同じ場合は後で削除
            if text_channel.id != interaction.channel.id:
                try:
                    await text_channel.delete()
                    logger.info(f"テキストチャンネル {text_channel_id} を削除しました")
                except Exception as e:
                    logger.error(f"テキストチャンネル {text_channel_id} の削除に失敗: {e}")

    # ---- ボイスチャンネル削除 ----
    if voice_channel_id:
        voice_channel = interaction.guild.get_channel(voice_channel_id)
        if voice_channel:
            try:
                await voice_channel.delete()
                logger.info(f"ボイスチャンネル {voice_channel_id} を削除しました")
            except Exception as e:
                logger.error(f"ボイスチャンネル {voice_channel_id} の削除に失敗: {e}")

    # ---- ロール削除 ----
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            try:
                await role.delete()
                logger.info(f"ロール {role_id} を削除しました")
            except Exception as e:
                logger.error(f"ロール {role_id} の削除に失敗: {e}")

    # ---- 最後に「現在のチャンネル」だった場合の削除 ----
    if interaction.channel.id == text_channel_id:
        try:
            await interaction.channel.delete()
            logger.info(f"現在のチャンネル {interaction.channel.id} を削除しました")
        except Exception as e:
            logger.error(f"現在のチャンネル {interaction.channel.id} の削除に失敗: {e}")

    # ---- roomsテーブルからも削除（remove_room） ----
    remove_room(text_channel_id=text_channel_id)  # or voice_channel_id=... whichever
    add_admin_log("部屋削除", interaction.user.id, creator_id, f"テキスト:{text_channel_id} ボイス:{voice_channel_id}")

@bot.event
async def on_guild_channel_delete(channel):
    """チャンネルが削除された時の処理"""
    if isinstance(channel, discord.VoiceChannel) or isinstance(channel, discord.TextChannel):
        role_id, creator_id, other_channel_id = remove_room(
            text_channel_id=channel.id if isinstance(channel, discord.TextChannel) else None,
            voice_channel_id=channel.id if isinstance(channel, discord.VoiceChannel) else None
        )
        if role_id:
            role = channel.guild.get_role(role_id)
            if role:
                try:
                    await role.delete()
                    logger.info(f"チャンネル削除に伴いロール {role_id} を削除しました")
                except Exception as e:
                    logger.error(f"ロール {role_id} の削除に失敗しました: {str(e)}")
            if other_channel_id:
                other_channel = channel.guild.get_channel(other_channel_id)
                if other_channel:
                    try:
                        await other_channel.delete()
                        logger.info(f"関連チャンネル {other_channel_id} を削除しました")
                    except Exception as e:
                        logger.error(f"関連チャンネル {other_channel_id} の削除に失敗しました: {str(e)}")
            add_admin_log("自動部屋削除", None, creator_id, f"チャンネル:{channel.id}")

#募集一覧機能ーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーー
class ShowRoomsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="募集を見る", style=discord.ButtonStyle.blurple)
    async def show_rooms_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_show_rooms(interaction)

def get_user_genders(member: discord.Member) -> set[str]:
    """
    ユーザーが閲覧できる gender のセットを返す。
    例: 男性ロールがあれば {"male", "all"}、女性ロールがあれば {"female", "all"}、両方あれば {"male","female","all"}。
    何もなければ空集合。
    """
    roleset = set()
    male_role = discord.utils.get(member.roles, name="男性")
    female_role = discord.utils.get(member.roles, name="女性")

    if male_role:
        roleset.add("male")
    if female_role:
        roleset.add("female")

    # "all" は、いずれかのロールがある人は閲覧可能とする場合
    if roleset:
        roleset.add("all")

    return roleset


async def handle_show_rooms(interaction: discord.Interaction):
    """押した人が閲覧できる募集一覧を表示する (ロール＋ブラックリスト制御)"""
    member = interaction.user
    viewable_genders = get_user_genders(member)
    if not viewable_genders:
        await interaction.response.send_message("現在、募集はありません。", ephemeral=True)
        return

    # ① DBから性別(gender)に合致する部屋一覧を取得
    with get_db_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(viewable_genders))
        query = f"""
            SELECT creator_id, text_channel_id, details, gender
            FROM rooms
            WHERE gender IN ({placeholders})
        """
        cursor.execute(query, tuple(viewable_genders))
        rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message("現在、募集はありません。", ephemeral=True)
        return

    embed = discord.Embed(
        title="募集一覧",
        description="募集部屋の一覧を表示します。",
        color=discord.Color.green()
    )

    count = 0
    for (creator_id, text_channel_id, details, gender) in rows:
        # ② 作成者のブラックリストを取得
        creator_blacklist = get_blacklist(creator_id)
        # ③ ボタンを押したユーザーがブラックリストに含まれているか？
        if member.id in creator_blacklist:
            # 含まれていれば「この部屋は非表示」にする
            continue

        # ④ 通常の表示処理
        creator = interaction.guild.get_member(creator_id)
        creator_name = creator.display_name if creator else f"UserID: {creator_id}"
        channel = interaction.guild.get_channel(text_channel_id)
        channel_mention = channel.mention if channel else f"#{text_channel_id} (削除済み)"

        male_role = discord.utils.get(interaction.guild.roles, name="男性")
        female_role = discord.utils.get(interaction.guild.roles, name="女性")

        # 省略
            # ③ 作成者のロールを見て性別を判定
        if creator:
            # 両方持っているケースもあるかもしれないので一応分岐
            if male_role in creator.roles and female_role in creator.roles:
                creator_gender_jp = "両方！？" 
            elif male_role in creator.roles:
                creator_gender_jp = "男性"
            elif female_role in creator.roles:
                creator_gender_jp = "女性"
            else:
                creator_gender_jp = "不明"
        else:
            creator_gender_jp = "不明"


        embed.add_field(
            name=f"募集者: {creator_name} / 性別: {creator_gender_jp}",
            value=f"詳細: {details}\n交渉チャンネル: {channel_mention}",
            inline=False
        )
        count += 1

    if count == 0:
        # ブラックリストチェックで全部スキップされた場合など
        await interaction.response.send_message("現在、募集はありません。", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


#管理者用コマンドーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーーー
@bot.tree.command(name="setup-lobby", description="部屋作成ボタン付きメッセージを送信（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_lobby(interaction: discord.Interaction):
    """管理者向け: ボタン付きメッセージをチャンネルに設置"""
    view = GenderRoomView(timeout=None)
    text = (
        "**【募集開始ボタン】**\n"
        "男性のみ・女性のみ・どちらでもOK、いずれかのボタンを押すと募集が開始されます。\n募集を見せたい性別を選んでください！"
    )
    await interaction.channel.send(text, view=view)
    await interaction.response.send_message("部屋作成ボタン付きメッセージを設置しました！", ephemeral=True)

@bot.tree.command(name="setup-room-list-button", description="募集一覧を表示するボタンを設置（管理者用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_room_list_button(interaction: discord.Interaction):
    """管理者向け: 募集一覧を表示するボタンを設置する"""
    view = ShowRoomsView()
    await interaction.channel.send("募集一覧を表示したい場合は、こちらのボタンを押してください。", view=view)
    await interaction.response.send_message("募集一覧ボタンを設置しました！", ephemeral=True)


@bot.tree.command(name="admin-logs", description="管理者ログを表示（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(limit="表示する件数")
async def admin_logs(interaction: discord.Interaction, limit: int = 10):
    """管理者ログを表示（管理者専用）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT action, user_id, target_id, details, timestamp 
            FROM admin_logs 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (limit,))
        logs = cursor.fetchall()
    if not logs:
        await interaction.response.send_message("ログはありません。", ephemeral=True)
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
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear-rooms", description="全ての通話募集部屋を削除（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def clear_rooms(interaction: discord.Interaction):
    """全ての通話募集部屋を削除（管理者専用）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT text_channel_id, voice_channel_id, role_id FROM rooms")
        rooms = cursor.fetchall()
    if not rooms:
        await interaction.response.send_message("削除する部屋はありません。", ephemeral=True)
        return
    count = 0
    for text_channel_id, voice_channel_id, role_id in rooms:
        try:
            text_channel = interaction.guild.get_channel(text_channel_id)
            if text_channel:
                await text_channel.delete()
                logger.info(f"テキストチャンネル {text_channel_id} を削除しました")
            voice_channel = interaction.guild.get_channel(voice_channel_id)
            if voice_channel:
                await voice_channel.delete()
                logger.info(f"ボイスチャンネル {voice_channel_id} を削除しました")
            if role_id:
                role = interaction.guild.get_role(role_id)
                if role:
                    await role.delete()
                    logger.info(f"ロール {role_id} を削除しました")
            count += 1
        except Exception as e:
            logger.error(f"部屋の削除に失敗: {str(e)}")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rooms")
        conn.commit()
    add_admin_log("全部屋削除", interaction.user.id, None, f"{count}個の部屋を削除")
    await interaction.response.send_message(f"✅ {count}個の部屋を削除しました。", ephemeral=True)

#@bot.tree.command(name="bot-help", description="BOTのヘルプを表示")
#async def bot_help(interaction: discord.Interaction):
    """BOTのヘルプを表示"""
    embed = discord.Embed(title="通話募集BOT ヘルプ", color=discord.Color.blue())
    embed.add_field(
        name="🔒 ブラックリスト管理",
        value=(
            "`/bl-add @ユーザー [理由]` - ユーザーをブラックリストに追加\n"
            "`/bl-remove @ユーザー` - ユーザーをブラックリストから削除\n"
            "`/bl-list` - あなたのブラックリストを表示（DMで送信）"
        ),
        inline=False
    )
    embed.add_field(
        name="🏠 部屋管理",
        value=(
            "`/create-room` - 通話募集部屋を作成\n"
            "`/delete-room` - 通話募集部屋を削除（部屋作成者のみ）"
        ),
        inline=False
    )
    embed.set_footer(text="ブラックリストに登録されたユーザーには、あなたの部屋が見えなくなります。")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="sync", description="スラッシュコマンドを手動で同期")
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message("✅ コマンドを手動で同期しました！", ephemeral=True)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.errors.MissingRequiredArgument):
        await ctx.send(" コマンドの引数が不足しています。", ephemeral=True)
    elif isinstance(error, commands.errors.MissingPermissions):
        await ctx.send(" このコマンドを実行する権限がありません。", ephemeral=True)
    elif isinstance(error, commands.errors.CommandNotFound):
        pass
    else:
        logger.error(f"コマンドエラー: {str(error)}")
        await ctx.send(f" エラーが発生しました: {str(error)}", ephemeral=True)

#カテゴリ自動削除機能
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    """
    チャンネルが手動/管理者操作などで消されたとき
    カテゴリが空なら削除。データベース管理のロールも消す
    """
    if isinstance(channel, discord.TextChannel) or isinstance(channel, discord.VoiceChannel):
        r_id, c_id, other_id = remove_room(
            text_channel_id=channel.id if isinstance(channel, discord.TextChannel) else None,
            voice_channel_id=channel.id if isinstance(channel, discord.VoiceChannel) else None
        )
        # データベースから取れた role_id があれば削除
        if r_id:
            role = channel.guild.get_role(r_id)
            if role:
                try:
                    await role.delete()
                    logger.info(f"ロール {role.id} を削除しました")
                except Exception as e:
                    logger.warning(f"ロール {role.id} の削除に失敗: {e}")


        # カテゴリの空判定と削除
        cat = channel.category  # ここでとれる Category オブジェクト
        if cat:
            # まだギルドに存在しているかどうかを念のため再取得か、あるいは try/except で囲む
            # いったん len(cat.channels) == 0 で空なら削除を試す
            if len(cat.channels) == 0:
                try:
                    await cat.delete()
                    logger.info(f"[DeleteCategory] {cat.name}")
                except discord.NotFound:
                    # 既に削除されているか、存在しなくなっている場合
                    logger.warning(f"カテゴリ {cat.name} は既に削除されているようです")
                except Exception as e:
                    logger.warning(f"カテゴリ {cat.name} の削除に失敗: {e}")

        add_admin_log("自動部屋削除", None, c_id, f"channel={channel.id}")

# トークン付与
# .envファイルの読み込み
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)

# デバッグ出力
# print(f"TOKEN: {TOKEN}")
