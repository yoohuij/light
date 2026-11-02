import asyncio


import logging


import os


import sqlite3


import time


from contextlib import closing


from dataclasses import dataclass


from pathlib import Path


import discord


from discord import app_commands


from dotenv import load_dotenv


ENVIRONMENT_PATH = Path(__file__).resolve().with_name(".env")


load_dotenv(ENVIRONMENT_PATH)


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", Path(__file__).resolve().with_name("attendance.sqlite3")))


EMBED_LIMIT = 3_700


MENTION_CHUNK_LIMIT = 900


NOTICE_CONTENT_LIMIT = 1_800


NOTICE_RESULT_LIMIT = 900


NOTICE_RETRY_COUNT = 5


@dataclass(frozen=True)
class CheckTarget:
    guild_id: int
    channel_id: int
    message_id: int
    emoji: str | None


@dataclass(frozen=True)
class MeetingSettings:
    guild_id: int
    meeting_channel_id: int | None
    log_channel_id: int | None
    notice_channel_id: int | None
    reason_channel_id: int | None


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS check_targets (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notice_exemptions (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_administrators (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_settings (
            guild_id INTEGER PRIMARY KEY,
            meeting_channel_id INTEGER,
            log_channel_id INTEGER,
            notice_channel_id INTEGER,
            reason_channel_id INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS absence_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason_channel_id INTEGER,
            message_id INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS approved_absences (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            approved_by INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS meeting_status ("
        "guild_id INTEGER PRIMARY KEY, phase TEXT NOT NULL, end_message_id INTEGER)"
    )
    connection.execute("CREATE TABLE IF NOT EXISTS global_exemptions (user_id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    if connection.execute("SELECT 1 FROM schema_migrations WHERE name = 'global_exemptions'").fetchone() is None:
        connection.execute("INSERT OR IGNORE INTO global_exemptions SELECT DISTINCT user_id FROM notice_exemptions")
        connection.execute("INSERT INTO schema_migrations VALUES ('global_exemptions')")
    connection.execute("CREATE TABLE IF NOT EXISTS global_administrators (user_id INTEGER PRIMARY KEY)")
    if connection.execute("SELECT 1 FROM schema_migrations WHERE name = 'global_administrators'").fetchone() is None:
        connection.execute("INSERT OR IGNORE INTO global_administrators SELECT DISTINCT user_id FROM bot_administrators")
        connection.execute("INSERT INTO schema_migrations VALUES ('global_administrators')")
    connection.commit()
    return connection


def meeting_phase(guild_id: int) -> str:
    with database() as connection:
        row = connection.execute("SELECT phase FROM meeting_status WHERE guild_id = ?", (guild_id,)).fetchone()
    return row[0] if row else "collecting"


def set_meeting_phase(guild_id: int, phase: str, end_message_id: int | None = None) -> None:
    with database() as connection:
        connection.execute(
            "INSERT INTO meeting_status VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "phase = excluded.phase, end_message_id = excluded.end_message_id",
            (guild_id, phase, end_message_id),
        )


async def open_absence_modal(interaction: discord.Interaction) -> None:
    if interaction.guild is None or meeting_phase(interaction.guild.id) != "collecting":
        await interaction.response.send_message("불참 사유 신청이 마감되었습니다.", ephemeral=True)
        return
    await interaction.response.send_modal(AbsenceReasonModal())


def save_target(target: CheckTarget) -> None:
    with database() as connection:
        connection.execute(
            """
            INSERT INTO check_targets (guild_id, channel_id, message_id, emoji)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                emoji = excluded.emoji
            """,
            (target.guild_id, target.channel_id, target.message_id, target.emoji),
        )


def get_target(guild_id: int) -> CheckTarget | None:
    with database() as connection:
        row = connection.execute(
            "SELECT guild_id, channel_id, message_id, emoji FROM check_targets WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return CheckTarget(*row) if row else None


def get_meeting_settings(guild_id: int) -> MeetingSettings:
    with database() as connection:
        row = connection.execute(
            """
            SELECT guild_id, meeting_channel_id, log_channel_id, notice_channel_id, reason_channel_id
            FROM meeting_settings WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        if row is None:
            connection.execute("INSERT INTO meeting_settings (guild_id) VALUES (?)", (guild_id,))
            return MeetingSettings(guild_id, None, None, None, None)
    return MeetingSettings(*row)


def set_meeting_setting(guild_id: int, column: str, channel_id: int) -> None:
    allowed_columns = {
        "meeting_channel_id",
        "log_channel_id",
        "notice_channel_id",
        "reason_channel_id",
    }
    if column not in allowed_columns:
        raise ValueError("허용되지 않은 설정 항목입니다.")
    get_meeting_settings(guild_id)
    with database() as connection:
        connection.execute(f"UPDATE meeting_settings SET {column} = ? WHERE guild_id = ?", (channel_id, guild_id))


def create_absence_request(guild_id: int, user_id: int, reason: str) -> int:
    with database() as connection:
        cursor = connection.execute(
            "INSERT INTO absence_requests (guild_id, user_id, reason) VALUES (?, ?, ?)",
            (guild_id, user_id, reason),
        )
    return int(cursor.lastrowid)


def set_absence_request_message(request_id: int, channel_id: int, message_id: int) -> None:
    with database() as connection:
        connection.execute(
            "UPDATE absence_requests SET reason_channel_id = ?, message_id = ? WHERE request_id = ?",
            (channel_id, message_id, request_id),
        )


def get_absence_request(request_id: int) -> tuple[int, int, str, str] | None:
    with database() as connection:
        row = connection.execute(
            "SELECT guild_id, user_id, reason, status FROM absence_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return row


def approve_absence_request(request_id: int, approver_id: int) -> tuple[int, int, str] | None:
    request = get_absence_request(request_id)
    if request is None or request[3] != "pending":
        return None
    guild_id, user_id, reason, _ = request
    with database() as connection:
        connection.execute("UPDATE absence_requests SET status = 'approved' WHERE request_id = ?", (request_id,))
        connection.execute(
            """
            INSERT INTO approved_absences (guild_id, user_id, reason, approved_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                reason = excluded.reason, approved_by = excluded.approved_by
            """,
            (guild_id, user_id, reason, approver_id),
        )
    return guild_id, user_id, reason


def reject_absence_request(request_id: int) -> tuple[int, int] | None:
    request = get_absence_request(request_id)
    if request is None or request[3] != "pending":
        return None
    with database() as connection:
        connection.execute("DELETE FROM absence_requests WHERE request_id = ?", (request_id,))
    return request[0], request[1]


def get_approved_absence(guild_id: int, user_id: int) -> str | None:
    with database() as connection:
        row = connection.execute(
            "SELECT reason FROM approved_absences WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row[0] if row else None


def get_approved_absences(guild_id: int) -> list[tuple[int, str]]:
    with database() as connection:
        rows = connection.execute(
            "SELECT user_id, reason FROM approved_absences WHERE guild_id = ? ORDER BY user_id",
            (guild_id,),
        ).fetchall()
    return [(int(user_id), str(reason)) for user_id, reason in rows]


def get_absence_roster(guild_id: int) -> list[tuple[int, str]]:
    pass
    roster = dict(get_approved_absences(guild_id))
    for user_id in get_notice_exemptions(guild_id):
        roster.setdefault(user_id, "예외 설정으로 자동 포함된 불참자입니다.")
    return sorted(roster.items())


def reset_absence_data(guild_id: int) -> tuple[int, int]:
    pass
    with database() as connection:
        approved_count = connection.execute(
            "DELETE FROM approved_absences WHERE guild_id = ?", (guild_id,)
        ).rowcount
        request_count = connection.execute(
            "DELETE FROM absence_requests WHERE guild_id = ?", (guild_id,)
        ).rowcount
    return approved_count, request_count


def get_pending_request_message_ids() -> list[tuple[int, int]]:
    with database() as connection:
        rows = connection.execute(
            """
            SELECT request_id, message_id FROM absence_requests
            WHERE status = 'pending' AND message_id IS NOT NULL
            """
        ).fetchall()
    return [(int(request_id), int(message_id)) for request_id, message_id in rows]


def get_exemption_roles(guild_id: int) -> set[int]:
    with closing(database()) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS role_exemptions (guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, PRIMARY KEY(guild_id, role_id))")
        return {row[0] for row in connection.execute("SELECT role_id FROM role_exemptions WHERE guild_id=?", (guild_id,))}


def get_notice_exemptions(guild_id: int) -> set[int]:
    with closing(database()) as connection:
        rows = connection.execute(
            "SELECT user_id FROM global_exemptions ORDER BY user_id"
        ).fetchall()
    ids = {row[0] for row in rows}
    guild = bot.get_guild(guild_id)
    if guild is not None:
        role_ids = get_exemption_roles(guild_id)
        ids.update(member.id for member in guild.members if any(role.id in role_ids for role in member.roles))
    return ids


def set_notice_exemption(guild_id: int, user_id: int, is_exempt: bool) -> None:
    with closing(database()) as connection, connection:
        if is_exempt:
            connection.execute(
                "INSERT OR IGNORE INTO global_exemptions (user_id) VALUES (?)",
                (user_id,),
            )
        else:
            connection.execute(
                "DELETE FROM global_exemptions WHERE user_id = ?",
                (user_id,),
            )


def filter_exception_mentions(chunk: str, guild_id: int) -> str:
    exempt_ids = get_notice_exemptions(guild_id)
    return " ".join(token for token in chunk.split() if int(token.strip("<@!>")) not in exempt_ids)


def set_manual_absence(guild_id: int, user_id: int, owner_id: int, adding: bool) -> None:
    with database() as connection:
        
        connection.execute("DELETE FROM absence_requests WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        if adding:
            connection.execute(
                "INSERT INTO approved_absences VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) "
                "DO UPDATE SET reason = excluded.reason, approved_by = excluded.approved_by",
                (guild_id, user_id, "소유주가 직접 등록한 불참자입니다.", owner_id),
            )
        else:
            connection.execute("DELETE FROM approved_absences WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))


def is_bot_administrator(guild_id: int, user_id: int) -> bool:
    pass
    if is_suspended(user_id):
        return False
    with closing(database()) as connection:
        row = connection.execute(
            "SELECT 1 FROM global_administrators WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def set_bot_administrator(guild_id: int, user_id: int, is_administrator: bool) -> None:
    with closing(database()) as connection, connection:
        if is_administrator:
            connection.execute(
                "INSERT OR IGNORE INTO global_administrators (user_id) VALUES (?)",
                (user_id,),
            )
        else:
            connection.execute(
                "DELETE FROM global_administrators WHERE user_id = ?",
                (user_id,),
            )


def get_bot_administrator_ids(guild_id: int) -> list[int]:
    with closing(database()) as connection:
        rows = connection.execute(
            "SELECT user_id FROM global_administrators ORDER BY user_id",
        ).fetchall()
    return [row[0] for row in rows]


def is_suspended(user_id: int) -> bool:
    return False


def can_manage(interaction: discord.Interaction) -> bool:
    if is_suspended(interaction.user.id):
        return False
    member = interaction.user
    return isinstance(member, discord.Member) and member.guild_permissions.manage_guild


def can_review_meeting(interaction: discord.Interaction) -> bool:
    if is_suspended(interaction.user.id):
        return False
    return can_manage(interaction) or can_use_notice_admin(interaction)


def can_use_notice_admin(interaction: discord.Interaction) -> bool:
    pass
    if is_suspended(interaction.user.id):
        return False
    return (
        interaction.guild is not None
        and (
            interaction.user.id == bot.application_owner_id
            or interaction.user.id == interaction.guild.owner_id
            or is_bot_administrator(interaction.guild.id, interaction.user.id)
        )
    )


def is_owner(interaction: discord.Interaction) -> bool:
    pass
    return (
        interaction.guild is not None
        and (
            interaction.user.id == bot.application_owner_id
            or interaction.user.id == interaction.guild.owner_id
        )
    )


async def fetch_target_message(bot: discord.Client, target: CheckTarget) -> discord.Message:
    channel = bot.get_channel(target.channel_id)
    if channel is None:
        channel = await bot.fetch_channel(target.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        raise ValueError("메시지를 조회할 수 있는 서버 채널이 아닙니다.")
    return await channel.fetch_message(target.message_id)


async def get_non_responders(
    bot: discord.Client, guild: discord.Guild, target: CheckTarget
) -> tuple[discord.Message, list[discord.Member], int]:
    meeting_id = get_meeting_settings(guild.id).meeting_channel_id
    if meeting_id is None or not isinstance(guild.get_channel(meeting_id), (discord.VoiceChannel, discord.StageChannel)):
        raise ValueError("먼저 `/회의방등록`으로 현재 사용할 회의방을 등록해 주세요.")
    message = await fetch_target_message(bot, target)
    responded_ids: set[int] = set()

    for reaction in message.reactions:
        if target.emoji is not None and str(reaction.emoji) != target.emoji:
            continue
        async for user in reaction.users(limit=None):
            if not user.bot:
                responded_ids.add(user.id)

    members = await get_meeting_members(guild, meeting_id)
    non_responders = [member for member in members if member.id not in responded_ids]
    return message, non_responders, len(members)


def safe_name(member: discord.Member) -> str:
    
    return discord.utils.escape_markdown(discord.utils.escape_mentions(member.display_name))


def make_pages(members: list[discord.Member]) -> list[str]:
    if not members:
        return ["모든 일반 유저가 반응했습니다."]

    pages: list[str] = []
    lines: list[str] = []
    size = 0
    for member in members:
        line = f"• {safe_name(member)} (`{member.name}`)"
        if lines and (len(lines) >= 20 or size + len(line) + 1 > EMBED_LIMIT):
            pages.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


def make_mention_pages(members: list[discord.Member]) -> list[str]:
    pass
    if not members:
        return ["없음"]

    pages: list[str] = []
    mentions: list[str] = []
    size = 0
    for member in members:
        mention = member.mention
        if mentions and size + len(mention) + 1 > NOTICE_RESULT_LIMIT:
            pages.append(" ".join(mentions))
            mentions, size = [], 0
        mentions.append(mention)
        size += len(mention) + 1
    if mentions:
        pages.append(" ".join(mentions))
    return pages


async def send_direct_notice(member: discord.Member, notice: str) -> bool | None:
    pass
    for attempt in range(NOTICE_RETRY_COUNT):
        if member.id in get_notice_exemptions(member.guild.id) or any(r.id in get_exemption_roles(member.guild.id) for r in member.roles):
            return None
        try:
            await member.send(
                notice,
                
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
            return True
        except (discord.Forbidden, discord.NotFound):
            
            return False
        except discord.HTTPException as error:
            status = getattr(error, "status", None)
            
            
            should_retry = status == 429 or (isinstance(status, int) and status >= 500)
            if not should_retry or attempt == NOTICE_RETRY_COUNT - 1:
                return False
            await asyncio.sleep(2 ** attempt)
    return False


class ResultView(discord.ui.View):
    def __init__(
        self, bot: discord.Client, owner_id: int, guild: discord.Guild, target: CheckTarget,
        members: list[discord.Member], total_members: int, message: discord.Message,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.owner_id = owner_id
        self.guild = guild
        self.target = target
        self.members = members
        self.total_members = total_members
        self.source_message = message
        self.pages = make_pages(members)
        self.page = 0
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_suspended(interaction.user.id):
            await interaction.response.send_message("봇 권한이 임시 정지되었습니다.", ephemeral=True)
            return False
        if interaction.user.id == self.owner_id or can_manage(interaction):
            return True
        await interaction.response.send_message("명령어를 실행한 사람 또는 서버 관리 권한자만 사용할 수 있습니다.", ephemeral=True)
        return False

    def update_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1
        self.mention.disabled = not self.members

    def embed(self) -> discord.Embed:
        emoji_label = self.target.emoji or "모든 이모지"
        embed = discord.Embed(title="반응 미확인자 조회", colour=discord.Colour.blurple())
        embed.add_field(name="확인 메시지", value=f"[메시지 보기]({self.source_message.jump_url})", inline=False)
        embed.add_field(name="선택 이모지", value=emoji_label, inline=True)
        embed.add_field(name="회의방 대상 인원 (예외 제외)", value=f"{self.total_members}명", inline=True)
        embed.add_field(name="미반응자", value=f"{len(self.members)}명", inline=True)
        
        embed.description = self.pages[self.page]
        embed.set_footer(text=f"페이지 {self.page + 1}/{len(self.pages)} · 목록에서는 멘션되지 않습니다.")
        return embed

    async def redraw(self, interaction: discord.Interaction) -> None:
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        await self.redraw(interaction)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        await self.redraw(interaction)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            self.target = get_target(self.guild.id) or self.target
            self.source_message, self.members, self.total_members = await get_non_responders(self.bot, self.guild, self.target)
        except (discord.HTTPException, ValueError) as error:
            await interaction.followup.send(f"새로고침하지 못했습니다: {error}", ephemeral=True)
            return
        self.pages = make_pages(self.members)
        self.page = min(self.page, len(self.pages) - 1)
        self.update_buttons()
        await interaction.edit_original_response(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="미반응자 멘션", style=discord.ButtonStyle.danger)
    async def mention(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        
        await interaction.response.defer(ephemeral=True)
        try:
            self.target = get_target(self.guild.id) or self.target
            self.source_message, self.members, self.total_members = await get_non_responders(self.bot, self.guild, self.target)
        except (discord.HTTPException, ValueError) as error:
            await interaction.followup.send(f"현재 대상자를 확인하지 못했습니다: {error}", ephemeral=True)
            return
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for member in self.members:
            token = member.mention
            if current and current_size + len(token) + 1 > MENTION_CHUNK_LIMIT:
                chunks.append(" ".join(current))
                current, current_size = [], 0
            current.append(token)
            current_size += len(token) + 1
        if current:
            chunks.append(" ".join(current))

        for index, chunk in enumerate(chunks, start=1):
            chunk = filter_exception_mentions(chunk, interaction.guild.id)
            if not chunk:
                continue
            await interaction.channel.send(
                f"반응 확인 부탁드립니다. ({index}/{len(chunks)})\n{chunk}",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            await asyncio.sleep(0.5)
        await interaction.followup.send(f"미반응자 {len(self.members)}명을 멘션했습니다.", ephemeral=True)


def make_member_pages(members: list[discord.Member], empty_message: str) -> list[str]:
    if not members:
        return [empty_message]
    pages: list[str] = []
    lines: list[str] = []
    size = 0
    for member in members:
        line = f"• {safe_name(member)} (`{member.name}`)"
        if lines and (len(lines) >= 20 or size + len(line) + 1 > EMBED_LIMIT):
            pages.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


class MemberResultView(discord.ui.View):
    pass

    def __init__(
        self,
        owner_id: int,
        members: list[discord.Member],
        total_members: int,
        title: str,
        count_label: str,
        empty_message: str,
        mention_message: str,
        refresh_members=None,
        excluded_count: int | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.members = members
        self.total_members = total_members
        self.title = title
        self.count_label = count_label
        self.mention_message = mention_message
        self.refresh_members = refresh_members
        self.excluded_count = excluded_count
        self.empty_message = empty_message
        self.refresh_lock = asyncio.Lock()
        self.pages = make_member_pages(members, empty_message)
        self.page = 0
        self.mention.label = f"{count_label} 멘션"
        if refresh_members is None:
            self.remove_item(self.refresh)
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_suspended(interaction.user.id):
            await interaction.response.send_message("봇 권한이 임시 정지되었습니다.", ephemeral=True)
            return False
        if interaction.user.id == self.owner_id or can_manage(interaction):
            return True
        await interaction.response.send_message("명령어를 실행한 사람 또는 서버 관리 권한자만 사용할 수 있습니다.", ephemeral=True)
        return False

    def update_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1
        self.mention.disabled = not self.members

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title, colour=discord.Colour.orange())
        embed.add_field(name="전체 인원" if self.excluded_count is not None else "대상 인원", value=f"{self.total_members}명", inline=True)
        if self.excluded_count is not None:
            embed.add_field(name="예외 인원", value=f"{self.excluded_count}명", inline=True)
        embed.add_field(name=self.count_label, value=f"{len(self.members)}명", inline=True)
        embed.description = self.pages[self.page]
        embed.set_footer(text=f"페이지 {self.page + 1}/{len(self.pages)} · 목록에서는 멘션되지 않습니다.")
        if self.excluded_count is not None:
            reacted = self.total_members - self.excluded_count - len(self.members)
            embed.set_footer(text=f"페이지 {self.page + 1}/{len(self.pages)} · 반응자 {reacted}명 · 예외 인원은 예외자·불참자 중복 제외 합계 · 목록에서는 멘션되지 않습니다.")
        return embed

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="대상자 멘션", style=discord.ButtonStyle.danger)
    async def mention(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.refresh_lock.locked():
            await interaction.response.send_message("조회 중입니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.refresh_lock:
            if not await self.reload_members(interaction):
                return
            await self.send_mentions(interaction)

    async def reload_members(self, interaction):
        if self.refresh_members is None:
            return True
        try:
            members, total, excluded = await asyncio.wait_for(self.refresh_members(), timeout=90)
        except (discord.HTTPException, ValueError, asyncio.TimeoutError):
            await interaction.followup.send("최신 반응을 확인하지 못했습니다. 잘못된 멘션을 막기 위해 전송하지 않습니다. 메시지와 봇 권한을 확인해 주세요.", ephemeral=True)
            return False
        self.members, self.total_members = members, total
        self.excluded_count = excluded
        self.pages = make_member_pages(members, self.empty_message)
        self.page = 0
        self.update_buttons()
        if interaction.message is not None:
            await interaction.message.edit(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())
        return True

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.refresh_lock.locked():
            await interaction.response.send_message("조회 중입니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.refresh_lock:
            if await self.reload_members(interaction):
                await interaction.followup.send("현재 반응과 회의방 참여 상태로 갱신했습니다.", ephemeral=True)

    async def send_mentions(self, interaction):
        exempt_ids = get_notice_exemptions(interaction.guild.id)
        self.members = [member for member in self.members if member.id not in exempt_ids]
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for member in self.members:
            mention = member.mention
            if current and current_size + len(mention) + 1 > MENTION_CHUNK_LIMIT:
                chunks.append(" ".join(current))
                current, current_size = [], 0
            current.append(mention)
            current_size += len(mention) + 1
        if current:
            chunks.append(" ".join(current))
        if interaction.channel is not None:
            for chunk in chunks:
                chunk = filter_exception_mentions(chunk, interaction.guild.id)
                if not chunk:
                    continue
                await interaction.channel.send(
                    f"{self.mention_message}\n{chunk}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
                await asyncio.sleep(0.5)
        await interaction.followup.send(f"{self.count_label} {len(self.members)}명을 멘션했습니다.", ephemeral=True)


class AbsenceReasonModal(discord.ui.Modal, title="불참 사유 신청"):
    reason = discord.ui.TextInput(label="불참 사유", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버에서만 신청할 수 있습니다.", ephemeral=True)
            return
        settings = get_meeting_settings(interaction.guild.id)
        if settings.reason_channel_id is None:
            await interaction.response.send_message("사유방이 아직 등록되지 않았습니다.", ephemeral=True)
            return
        reason_channel = bot.get_channel(settings.reason_channel_id)
        if reason_channel is None:
            try:
                reason_channel = await bot.fetch_channel(settings.reason_channel_id)
            except discord.HTTPException:
                reason_channel = None
        if not isinstance(reason_channel, discord.TextChannel):
            await interaction.response.send_message("등록된 사유방을 찾을 수 없습니다. 소유주에게 다시 등록해 달라고 해주세요.", ephemeral=True)
            return

        reason = self.reason.value
        if meeting_phase(interaction.guild.id) != "collecting":
            await interaction.response.send_message("불참 사유 신청이 마감되었습니다.", ephemeral=True)
            return
        request_id = create_absence_request(interaction.guild.id, interaction.user.id, reason)
        embed = discord.Embed(title="불참 사유 신청", colour=discord.Colour.gold())
        embed.add_field(name="신청자", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="사유", value=discord.utils.escape_mentions(reason), inline=False)
        try:
            request_message = await reason_channel.send(
                embed=embed,
                view=AbsenceDecisionView(request_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            reject_absence_request(request_id)
            await interaction.response.send_message("사유방에 신청을 보낼 수 없습니다. 봇 권한을 확인해 주세요.", ephemeral=True)
            return
        set_absence_request_message(request_id, reason_channel.id, request_message.id)
        await interaction.response.send_message("불참 사유 신청을 보냈습니다. 관리자의 검토를 기다려 주세요.", ephemeral=True)


class MeetingAnnouncementModal(discord.ui.Modal, title="회의 공지 작성"):
    time = discord.ui.TextInput(label="회의 시간", placeholder="예: 9월 20일 오후 8시", max_length=100)
    content = discord.ui.TextInput(label="공지 내용", style=discord.TextStyle.paragraph, max_length=1500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_use_notice_admin(interaction):
            await interaction.response.send_message("소유자 또는 등록된 관리자만 회의 공지를 보낼 수 있습니다.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        settings = get_meeting_settings(interaction.guild.id)
        if settings.notice_channel_id is None:
            await interaction.response.send_message("먼저 `/공지방등록`으로 공지방을 지정해 주세요.", ephemeral=True)
            return
        notice_channel = bot.get_channel(settings.notice_channel_id)
        if notice_channel is None:
            try:
                notice_channel = await bot.fetch_channel(settings.notice_channel_id)
            except discord.HTTPException:
                notice_channel = None
        if not isinstance(notice_channel, discord.TextChannel):
            await interaction.response.send_message("등록된 공지방을 찾을 수 없습니다. 다시 등록해 주세요.", ephemeral=True)
            return

        try:
            await notice_channel.send(
                content="|| @everyone ||\n# 회의 공지",
                embed=discord.Embed(
                    description=(
                        f"## 회의시간 : {discord.utils.escape_mentions(self.time.value)}\n\n"
                        f"{discord.utils.escape_mentions(self.content.value)}"
                    ),
                    colour=discord.Colour.blurple(),
                ).set_footer(
                    text="사정으로 불참시 불참 사유 신청을 통해 사유 신청해주십시오."
                ),
                view=AbsenceApplyView(),
                allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=True),
            )
        except discord.Forbidden:
            await interaction.response.send_message("공지방에 메시지를 보내거나 @everyone을 멘션할 권한이 없습니다.", ephemeral=True)
            return
        if meeting_phase(interaction.guild.id) != "active":
            set_meeting_phase(interaction.guild.id, "collecting")
        await interaction.response.send_message(f"회의 공지를 {notice_channel.mention}에 올렸습니다.", ephemeral=True)


class MeetingApplyRow(discord.ui.ActionRow):
    @discord.ui.button(label="불참사유신청", style=discord.ButtonStyle.secondary,
                       custom_id="meeting_absence_apply_box")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_absence_modal(interaction)


class MeetingAnnouncementView(discord.ui.LayoutView):
    def __init__(self, time: str = "회의 시간", content: str = "회의 내용") -> None:
        super().__init__(timeout=None)
        heading = discord.utils.escape_mentions(time).replace("\n", " ").replace("\r", " ")
        self.add_item(discord.ui.TextDisplay("|| @everyone ||"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# {heading} 시작\n\n{discord.utils.escape_mentions(content)}"),
            MeetingApplyRow(),
        ))


class AbsenceApplyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="불참 사유 신청", style=discord.ButtonStyle.secondary, custom_id="meeting_absence_apply")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_absence_modal(interaction)


class AbsenceDecisionButton(discord.ui.Button):
    def __init__(self, request_id: int, approved: bool) -> None:
        super().__init__(
            label="사유 수락" if approved else "사유 거절",
            style=discord.ButtonStyle.success if approved else discord.ButtonStyle.danger,
            custom_id=f"meeting_absence_{'approve' if approved else 'reject'}_{request_id}",
        )
        self.approved = approved

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AbsenceDecisionView)
        await self.view.handle_decision(interaction, self.approved)


class AbsenceDecisionView(discord.ui.View):
    def __init__(self, request_id: int) -> None:
        super().__init__(timeout=None)
        self.request_id = request_id
        self.add_item(AbsenceDecisionButton(request_id, approved=True))
        self.add_item(AbsenceDecisionButton(request_id, approved=False))

    async def handle_decision(self, interaction: discord.Interaction, approved: bool) -> None:
        if not can_use_notice_admin(interaction):
            await interaction.response.send_message("소유자 또는 등록된 관리자만 사유를 처리할 수 있습니다.", ephemeral=True)
            return
        if interaction.message is None or not interaction.message.embeds:
            await interaction.response.send_message("원본 신청 임베드를 찾을 수 없어 처리하지 않았습니다.", ephemeral=True)
            return
        embeds = [embed.copy() for embed in interaction.message.embeds]
        embeds[0].colour = discord.Colour.green() if approved else discord.Colour.red()
        result = approve_absence_request(self.request_id, interaction.user.id) if approved else reject_absence_request(self.request_id)
        if result is None:
            await interaction.response.send_message("이미 처리되었거나 존재하지 않는 신청입니다.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embeds=embeds, view=self, allowed_mentions=discord.AllowedMentions.none())


class AttendanceBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.moderation = False
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.application_owner_id: int | None = None

    async def setup_hook(self) -> None:
        app_info = await self.application_info()
        self.application_owner_id = app_info.owner.id
        self.add_view(AbsenceApplyView())
        self.add_view(MeetingEndView())
        self.add_view(MeetingAnnouncementView())
        for request_id, message_id in get_pending_request_message_ids():
            self.add_view(AbsenceDecisionView(request_id), message_id=message_id)
        await request_safety_feature.sync_commands()


bot = AttendanceBot()


admin_group = app_commands.Group(name="관리자", description="공지용 봇 관리자 권한을 관리합니다.")


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    pass
    if member.bot or member.guild is None:
        return
    if meeting_phase(member.guild.id) != "active":
        return
    settings = get_meeting_settings(member.guild.id)
    if settings.meeting_channel_id is None or settings.log_channel_id is None:
        return
    was_in_meeting = before.channel is not None and before.channel.id == settings.meeting_channel_id
    is_in_meeting = after.channel is not None and after.channel.id == settings.meeting_channel_id
    
    if was_in_meeting == is_in_meeting:
        return
    log_channel = bot.get_channel(settings.log_channel_id)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(settings.log_channel_id)
        except discord.HTTPException:
            return
    if not isinstance(log_channel, discord.TextChannel):
        return
    if is_in_meeting:
        title, description, colour = (
            "회의방 입장",
            f"{member.mention} 님이 회의방에 입장했습니다.",
            discord.Colour.green(),
        )
    else:
        title, description, colour = (
            "회의방 퇴장",
            f"{member.mention} 님이 회의방에서 퇴장했습니다.",
            discord.Colour.red(),
        )
    await log_channel.send(
        embed=discord.Embed(title=title, description=description, colour=colour),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def get_meeting_members(guild: discord.Guild, meeting_channel_id: int, *, include_excluded=False) -> list[discord.Member]:
    exempt_ids = set() if include_excluded else get_notice_exemptions(guild.id)
    return [
        member
        async for member in guild.fetch_members(limit=None)
        if not member.bot
        and member.id not in exempt_ids
        and member.voice is not None
        and member.voice.channel is not None
        and member.voice.channel.id == meeting_channel_id
    ]


async def get_meeting_non_participants(
    guild: discord.Guild, meeting_channel_id: int
) -> tuple[list[discord.Member], int]:
    members = [member async for member in guild.fetch_members(limit=None) if not member.bot]
    approved_absence_ids = {user_id for user_id, _ in get_approved_absences(guild.id)}
    approved_absence_ids |= get_notice_exemptions(guild.id)
    eligible_members = [member for member in members if member.id not in approved_absence_ids]
    non_participants = [
        member
        for member in eligible_members
        if member.voice is None
        or member.voice.channel is None
        or member.voice.channel.id != meeting_channel_id
    ]
    return non_participants, len(eligible_members)


def make_absence_pages(absences: list[tuple[int, str]]) -> list[str]:
    if not absences:
        return ["승인된 불참자가 없습니다."]
    pages: list[str] = []
    lines: list[str] = []
    size = 0
    for user_id, _ in absences:
        line = f"• <@{user_id}> (`{user_id}`)"
        if lines and size + len(line) + 1 > EMBED_LIMIT:
            pages.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


@bot.tree.command(name="회의방등록", description="회의에 사용할 음성 채널을 등록합니다.")
@app_commands.describe(회의방="회의를 진행할 음성 채널")
async def register_meeting_channel(interaction: discord.Interaction, 회의방: discord.VoiceChannel | discord.StageChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 회의방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "meeting_channel_id", 회의방.id)
    await interaction.response.send_message(f"회의방을 {회의방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="로그방등록", description="회의방 입퇴장 로그를 보낼 채널을 등록합니다.")
@app_commands.describe(로그방="회의방 입퇴장 로그를 보낼 텍스트 채널")
async def register_log_channel(interaction: discord.Interaction, 로그방: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 로그방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "log_channel_id", 로그방.id)
    await interaction.response.send_message(f"로그방을 {로그방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="공지방등록", description="회의 공지를 올릴 채널을 등록합니다.")
@app_commands.describe(공지방="회의 공지를 올릴 텍스트 채널")
async def register_meeting_notice_channel(interaction: discord.Interaction, 공지방: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 공지방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "notice_channel_id", 공지방.id)
    await interaction.response.send_message(f"공지방을 {공지방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="사유방등록", description="불참 사유 신청을 받을 채널을 등록합니다.")
@app_commands.describe(사유방="불참 사유 신청을 받을 텍스트 채널")
async def register_reason_channel(interaction: discord.Interaction, 사유방: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 사유방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "reason_channel_id", 사유방.id)
    await interaction.response.send_message(f"사유방을 {사유방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="회의공지", description="시간과 내용을 입력해 회의 공지를 올립니다.")
async def meeting_notice(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("소유자 또는 등록된 관리자만 회의 공지를 보낼 수 있습니다.", ephemeral=True)
        return
    if get_meeting_settings(interaction.guild.id).notice_channel_id is None:
        await interaction.response.send_message("먼저 `/공지방등록`으로 공지방을 지정해 주세요.", ephemeral=True)
        return
    await interaction.response.send_modal(MeetingAnnouncementModal())


@bot.tree.command(name="불참자", description="불참자를 직접 추가하거나 제거합니다.")
@app_commands.guild_only()
@app_commands.describe(동작="추가 또는 제거", 유저="불참 명단을 변경할 유저")
@app_commands.choices(동작=[app_commands.Choice(name="추가", value="add"), app_commands.Choice(name="제거", value="remove")])
async def manual_absence(interaction: discord.Interaction, 동작: app_commands.Choice[str], 유저: discord.Member) -> None:
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("소유자 또는 등록된 관리자만 불참자를 관리할 수 있습니다.", ephemeral=True)
        return
    if 유저.bot:
        await interaction.response.send_message("봇은 불참자 대상이 아닙니다.", ephemeral=True)
        return
    adding = 동작.value == "add"
    set_manual_absence(interaction.guild.id, 유저.id, interaction.user.id, adding)
    action = "추가" if adding else "제거"
    await interaction.response.send_message(f"{유저.mention}님을 불참자 명단에서 {action} 처리했습니다.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="불참자명단", description="승인된 불참자 명단을 확인합니다.")
async def approved_absence_list(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_review_meeting(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    pages = make_absence_pages(get_absence_roster(interaction.guild.id))
    for index, page in enumerate(pages, start=1):
        embed = discord.Embed(title="불참자 명단 · 예외자 포함", description=page, colour=discord.Colour.gold())
        if len(pages) > 1:
            embed.set_footer(text=f"페이지 {index}/{len(pages)}")
        if index == 1:
            await interaction.response.send_message(
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await interaction.followup.send(
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )


@bot.tree.command(name="사유확인", description="승인된 불참자의 사유를 확인합니다.")
@app_commands.describe(유저="불참 사유를 확인할 유저")
async def check_absence_reason(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_review_meeting(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    reason = get_approved_absence(interaction.guild.id, 유저.id)
    if reason is None and (유저.id in get_notice_exemptions(interaction.guild.id)
                           or any(role.id in get_exemption_roles(interaction.guild.id) for role in 유저.roles)):
        reason = "예외 설정으로 자동 포함된 불참자입니다."
    if reason is None:
        await interaction.response.send_message("불참자가 아닙니다.", ephemeral=True)
        return
    embed = discord.Embed(title="불참 사유 확인", colour=discord.Colour.gold())
    embed.add_field(name="유저", value=f"{discord.utils.escape_mentions(유저.display_name)} (`{유저.id}`)", inline=False)
    embed.add_field(name="사유", value=discord.utils.escape_mentions(reason), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="미참여자확인", description="등록된 회의방에 현재 없는 사람을 확인합니다.")
async def check_meeting_non_participants(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_review_meeting(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    meeting_channel_id = get_meeting_settings(interaction.guild.id).meeting_channel_id
    if meeting_channel_id is None:
        await interaction.response.send_message("먼저 `/회의방등록`으로 회의방을 지정해 주세요.", ephemeral=True)
        return
    await interaction.response.defer()
    members, total = await get_meeting_non_participants(interaction.guild, meeting_channel_id)
    view = MemberResultView(
        interaction.user.id,
        members,
        total,
        "회의 미참여자 확인",
        "미참여자",
        "등록된 불참자를 제외한 모든 대상자가 회의방에 참여 중입니다.",
        "회의방 참여 부탁드립니다.",
    )
    if not (can_manage(interaction) or can_use_notice_admin(interaction)):
        view.remove_item(view.mention)
    await interaction.followup.send(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())


async def collect_reacted_ids(message):
    reacted_ids: set[int] = set()
    for reaction in message.reactions:
        for kind in (discord.ReactionType.normal, discord.ReactionType.burst):
            async for user in reaction.users(limit=None, type=kind):
                if not user.bot:
                    reacted_ids.add(user.id)
    return reacted_ids


async def find_guild_message(guild, preferred_channel, message_id):
    pass
    seen = set()

    async def lookup(channel):
        if channel is None or channel.id in seen or not hasattr(channel, "fetch_message"):
            return None
        if getattr(channel, "guild", None) != guild:
            return None
        seen.add(channel.id)
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            return None
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    for candidate in [preferred_channel, *guild.channels, *guild.threads]:
        message = await lookup(candidate)
        if message is not None:
            return message
    
    for parent in guild.channels:
        if not hasattr(parent, "archived_threads"):
            continue
        permissions = parent.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            continue
        modes = [{}]
        if isinstance(parent, discord.TextChannel):
            modes.append({"private": True, "joined": not permissions.manage_threads})
        for mode in modes:
            try:
                async for thread in parent.archived_threads(limit=None, **mode):
                    message = await lookup(thread)
                    if message is not None:
                        return message
            except discord.Forbidden:
                continue
    raise ValueError("같은 서버에서 봇이 읽을 수 있는 메시지를 찾지 못했습니다.")


async def get_idle_members(guild, channel, message_id):
    meeting_channel_id = get_meeting_settings(guild.id).meeting_channel_id
    if meeting_channel_id is None:
        raise ValueError("회의방이 등록되지 않았습니다.")
    
    message = await find_guild_message(guild, channel, message_id)
    reacted_ids = await collect_reacted_ids(message)
    members = await get_meeting_members(guild, meeting_channel_id, include_excluded=True)
    excluded_ids = get_notice_exemptions(guild.id) | {uid for uid, _ in get_approved_absences(guild.id)}
    eligible = [m for m in members if m.id not in excluded_ids]
    return [m for m in eligible if m.id not in reacted_ids], len(members), len(members) - len(eligible)


@bot.tree.command(name="잠수확인", description="회의방 참여자 중 메시지에 일반·슈퍼 반응을 달지 않은 사람을 확인합니다.")
@app_commands.describe(메시지_id="반응을 확인할 메시지 ID")
async def check_idle_members(interaction: discord.Interaction, 메시지_id: str) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_review_meeting(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    meeting_channel_id = get_meeting_settings(interaction.guild.id).meeting_channel_id
    if meeting_channel_id is None:
        await interaction.response.send_message("먼저 `/회의방등록`으로 회의방을 지정해 주세요.", ephemeral=True)
        return
    try:
        message_id = int(메시지_id)
        if message_id <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message("올바른 메시지 ID를 입력해 주세요.", ephemeral=True)
        return
    await interaction.response.defer()

    async def refresh_members():
        return await get_idle_members(interaction.guild, interaction.channel, message_id)

    try:
        idle_members, total, excluded = await asyncio.wait_for(refresh_members(), timeout=90)
    except discord.NotFound:
        await interaction.followup.send("해당 메시지나 반응을 찾지 못했습니다. 메시지 ID와 봇의 채널 읽기 권한을 확인해 주세요.", ephemeral=True)
        return
    except (discord.HTTPException, ValueError, asyncio.TimeoutError):
        await interaction.followup.send("메시지·반응·인원 조회를 완료하지 못했습니다. 일부 결과로 잠수자를 판정하지 않습니다. 권한 확인 후 다시 시도해 주세요.", ephemeral=True)
        return
    view = MemberResultView(
        interaction.user.id,
        idle_members,
        total,
        "회의방 잠수 확인",
        "미반응자",
        "예외자·불참자를 제외한 회의방 인원 중 미반응자가 없습니다.",
        "반응 확인 부탁드립니다.",
        refresh_members=refresh_members,
        excluded_count=excluded,
    )
    if not (can_manage(interaction) or can_use_notice_admin(interaction)):
        view.remove_item(view.mention)
    await interaction.followup.send(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())


async def get_outside_reactors(guild, channel, message_id):
    meeting_id = get_meeting_settings(guild.id).meeting_channel_id
    if meeting_id is None:
        raise ValueError("먼저 `/회의방등록`으로 회의방을 지정해 주세요.")
    message = await find_guild_message(guild, channel, message_id)
    reacted_ids = await collect_reacted_ids(message)
    exempt_ids = get_notice_exemptions(guild.id)
    members = [m async for m in guild.fetch_members(limit=None)
               if not m.bot and m.id in reacted_ids and m.id not in exempt_ids]
    outside = [m for m in members if m.voice is None or m.voice.channel is None
               or m.voice.channel.id != meeting_id]
    return outside, len(members)


@bot.tree.command(name="미참여반응자", description="회의방 밖에서 반응한 사람을 비공개로 확인합니다.")
async def check_outside_reactors(interaction: discord.Interaction, 메시지id: str):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("소유자 또는 등록된 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    try:
        message_id = int(메시지id)
        if message_id <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message("올바른 메시지 ID를 입력해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        members, total = await asyncio.wait_for(
            get_outside_reactors(interaction.guild, interaction.channel, message_id), timeout=90)
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    except (discord.HTTPException, asyncio.TimeoutError):
        await interaction.followup.send("조회를 완료하지 못했습니다. 메시지 ID와 채널 권한을 확인한 뒤 다시 시도해 주세요.", ephemeral=True)
        return
    view = MemberResultView(interaction.user.id, members, total,
                            "회의 미참여 반응자", "미참여 반응자",
                            "회의방 밖에서 반응한 사람이 없습니다.", "")
    view.remove_item(view.mention)
    await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True,
                                   allowed_mentions=discord.AllowedMentions.none())


class NoticeModal(discord.ui.Modal, title="공지 작성"):
    content = discord.ui.TextInput(
        label="내용", style=discord.TextStyle.paragraph,
        placeholder="DM으로 보낼 공지 내용을 입력해 주세요.",
        required=True, max_length=NOTICE_CONTENT_LIMIT,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.content.value.strip():
            await interaction.response.send_message("공지 내용을 입력해 주세요.", ephemeral=True)
            return
        await send_notice(interaction, self.content.value)


@bot.tree.command(name="공지", description="내용 입력창을 열어 공지 DM을 작성합니다.")
@app_commands.guild_only()
async def open_notice(interaction: discord.Interaction) -> None:
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("공지 봇 관리자 권한이 필요합니다.", ephemeral=True)
        return
    await interaction.response.send_modal(NoticeModal())


async def send_notice(interaction: discord.Interaction, 내용: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("공지 봇 관리자 권한이 필요합니다.", ephemeral=True)
        return
    if len(내용) > NOTICE_CONTENT_LIMIT:
        await interaction.response.send_message(
            f"공지 내용은 {NOTICE_CONTENT_LIMIT}자 이하로 입력해 주세요.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    sent_count = 0
    failed_members: list[discord.Member] = []
    total_members = 0
    exempt_count = 0

    
    async for member in interaction.guild.fetch_members(limit=None):
        if member.bot:
            continue
        if member.id in get_notice_exemptions(interaction.guild.id):
            exempt_count += 1
            continue
        total_members += 1
        notice = f"# [light 공지]\n\n{내용}\n\n-# 공지 DM입니다 {member.mention}"
        result = await send_direct_notice(member, notice)
        if result is None:
            total_members -= 1
            exempt_count += 1
        elif result:
            sent_count += 1
        else:
            failed_members.append(member)
        
        await asyncio.sleep(0.1)

    failed_count = len(failed_members)
    mention_pages = make_mention_pages(failed_members)
    for page_number, mention_page in enumerate(mention_pages, start=1):
        embed = discord.Embed(title="공지 DM 전송 결과", colour=discord.Colour.green())
        embed.add_field(name="전체 인원", value=f"{total_members}명", inline=True)
        embed.add_field(name="성공 인원", value=f"{sent_count}명", inline=True)
        embed.add_field(name="실패 인원", value=f"{failed_count}명", inline=True)
        embed.add_field(name="공지 예외 인원", value=f"{exempt_count}명", inline=True)
        embed.add_field(name="실패 인원 멘션", value=mention_page, inline=False)
        if len(mention_pages) > 1:
            embed.set_footer(text=f"실패 목록 {page_number}/{len(mention_pages)}")
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def exception_permission(interaction: discord.Interaction) -> bool:
    if interaction.user.id == bot.application_owner_id:
        return True
    await interaction.response.send_message("봇 소유자만 전체 예외 명단을 관리할 수 있습니다.", ephemeral=True)
    return False


@bot.tree.command(name="예외설정", description="봇 전체 예외 인원을 추가하거나 제거합니다.")
@app_commands.describe(동작="추가 또는 제거를 선택하세요", 유저="예외를 설정할 사용자")
@app_commands.choices(동작=[
    app_commands.Choice(name="추가", value="add"),
    app_commands.Choice(name="제거", value="remove"),
])
async def set_global_exception(
    interaction: discord.Interaction, 동작: app_commands.Choice[str], 유저: discord.User
) -> None:
    if not await exception_permission(interaction):
        return
    adding = 동작.value == "add"
    set_notice_exemption(0, 유저.id, adding)
    result = "예외에 추가했습니다" if adding else "예외에서 제거했습니다"
    await interaction.response.send_message(
        f"{유저.mention}님을 봇 전체 {result}.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="예외설정역할", description="·")
@app_commands.guild_only()
@app_commands.choices(동작=[app_commands.Choice(name="추가", value="add"), app_commands.Choice(name="제거", value="remove")])
async def set_role_exception(interaction: discord.Interaction, 동작: app_commands.Choice[str], 역할: discord.Role) -> None:
    if not await exception_permission(interaction):
        return
    guild = interaction.guild
    adding = 동작.value == "add"
    if 역할.guild.id != guild.id or 역할.is_default():
        await interaction.response.send_message("현재 서버의 @everyone 이외 역할을 선택해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    get_exemption_roles(guild.id)
    with closing(database()) as connection, connection:
        if adding:
            connection.execute("INSERT OR IGNORE INTO role_exemptions VALUES (?,?)", (guild.id, 역할.id))
        else:
            connection.execute("DELETE FROM role_exemptions WHERE guild_id=? AND role_id=?", (guild.id, 역할.id))
    await interaction.followup.send(
        f"{역할.mention} 역할 예외를 {'추가' if adding else '제거'}했습니다. 이 서버에서 적용되며 개인 예외는 유지됩니다.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class ExceptionRosterView(discord.ui.View):
    def __init__(self, owner_id, role_ids, user_ids, in_guild):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.role_ids = role_ids
        self.user_ids = user_ids
        self.in_guild = in_guild
        self.page = 0
        self.pages = max(1, (max(len(role_ids), len(user_ids)) + 19) // 20)
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next_page.disabled = self.page == self.pages - 1

    def embed(self):
        start = self.page * 20
        roles = "\n".join(f"{i}. <@&{rid}>" for i, rid in enumerate(self.role_ids[start:start + 20], start + 1))
        users = "\n".join(f"{i}. <@{uid}>" for i, uid in enumerate(self.user_ids[start:start + 20], start + 1))
        embed = discord.Embed(title="예외명단", colour=discord.Colour.blurple())
        embed.add_field(name=f"지정 역할 · 현재 서버 ({len(self.role_ids)}개)", value=roles or ("이 페이지에 표시할 역할이 없습니다." if self.in_guild else "서버에서 확인해 주세요."), inline=False)
        embed.add_field(name=f"개인 지정 · 봇 전체 ({len(self.user_ids)}명)", value=users or "이 페이지에 표시할 인원이 없습니다.", inline=False)
        embed.set_footer(text=f"페이지 {self.page + 1}/{self.pages}")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or is_suspended(interaction.user.id):
            await interaction.response.send_message("이 목록을 조작할 권한이 없습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.page = min(self.pages - 1, self.page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)


@bot.tree.command(name="예외목록", description="봇 전체 예외 명단을 나에게만 표시합니다.")
async def list_global_exceptions(interaction: discord.Interaction) -> None:
    if not await exception_permission(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    ids = sorted(get_notice_exemptions(0))
    role_ids = sorted(get_exemption_roles(interaction.guild.id)) if interaction.guild is not None else []
    view = ExceptionRosterView(interaction.user.id, role_ids, ids, interaction.guild is not None)
    await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@admin_group.command(name="부여", description="유저에게 공지용 봇 관리자 권한을 부여합니다.")
@app_commands.describe(유저="공지 봇 관리자로 지정할 유저")
async def grant_bot_administrator(interaction: discord.Interaction, 유저: discord.User) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if interaction.user.id != bot.application_owner_id:
        await interaction.response.send_message("봇 소유자만 통합 관리자 권한을 부여할 수 있습니다.", ephemeral=True)
        return
    if 유저.bot:
        await interaction.response.send_message("봇 계정에는 권한을 부여할 수 없습니다.", ephemeral=True)
        return
    set_bot_administrator(interaction.guild.id, 유저.id, True)
    await interaction.response.send_message(
        f"{discord.utils.escape_mentions(유저.display_name)}님에게 모든 서버에서 적용되는 공지 봇 관리자 권한을 부여했습니다.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@admin_group.command(name="해제", description="유저의 공지용 봇 관리자 권한을 해제합니다.")
@app_commands.describe(유저="공지 봇 관리자 권한을 해제할 유저")
async def revoke_bot_administrator(interaction: discord.Interaction, 유저: discord.User) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if interaction.user.id != bot.application_owner_id:
        await interaction.response.send_message("봇 소유자만 통합 관리자 권한을 해제할 수 있습니다.", ephemeral=True)
        return
    set_bot_administrator(interaction.guild.id, 유저.id, False)
    await interaction.response.send_message(
        f"{discord.utils.escape_mentions(유저.display_name)}님의 모든 서버 공통 공지 봇 관리자 권한을 해제했습니다.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@admin_group.command(name="목록", description="공지용 봇 관리자 목록을 확인합니다.")
async def list_bot_administrators(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if interaction.user.id != bot.application_owner_id:
        await interaction.response.send_message("봇 소유자만 통합 관리자 목록을 확인할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ids = get_bot_administrator_ids(interaction.guild.id)
    for offset in range(0, max(1, len(ids)), 20):
        entries = [f"• <@{uid}>" for uid in ids[offset:offset + 20]]
        embed = discord.Embed(
            title="봇 통합 공지 관리자 목록",
            description="\n".join(entries) if entries else "등록된 관리자가 없습니다.",
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"총 {len(ids)}명 · {offset // 20 + 1}페이지 · 모든 서버에 공통 적용")
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())




class MeetingEndView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="사유 초기화", style=discord.ButtonStyle.danger, custom_id="meeting_end_reset")
    async def reset(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not can_use_notice_admin(interaction):
            await interaction.response.send_message("소유자 또는 등록된 관리자만 초기화할 수 있습니다.", ephemeral=True)
            return
        with database() as connection:
            row = connection.execute(
                "SELECT phase, end_message_id FROM meeting_status WHERE guild_id = ?",
                (interaction.guild.id,),
            ).fetchone()
        if not row or row != ("ended", interaction.message.id):
            await interaction.response.send_message("현재 회의의 종료 메시지에서만 초기화할 수 있습니다.", ephemeral=True)
            return
        counts = reset_absence_data(interaction.guild.id)
        set_meeting_phase(interaction.guild.id, "cleared")
        self.reset.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"불참자 {counts[0]}명과 사유 신청 {counts[1]}건을 초기화했습니다. 사유방의 기존 메시지는 남습니다.",
            ephemeral=True,
        )


meeting_group = app_commands.Group(name="회의", description="회의 시작과 종료를 관리합니다.")


@bot.tree.command(name="사유초기화", description="소유자·등록 관리자: 이 서버의 불참자 명단과 사유를 초기화합니다.")
@app_commands.guild_only()
async def reset_meeting_reasons(interaction: discord.Interaction) -> None:
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("소유자 또는 등록된 관리자만 초기화할 수 있습니다.", ephemeral=True)
        return
    counts = reset_absence_data(interaction.guild.id)
    await interaction.response.send_message(
        f"불참자 {counts[0]}명과 사유 신청 {counts[1]}건을 초기화했습니다. 사유방의 기존 메시지는 남습니다. 회의 진행 상태는 유지됩니다.",
        ephemeral=True,
    )


async def change_meeting_state(interaction: discord.Interaction, starting: bool) -> None:
    await interaction.response.defer(ephemeral=True)
    lock = meeting_chat_feature.lock(interaction.guild.id)
    if lock.locked():
        await interaction.followup.send("회의 처리 중입니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return
    async with lock:
        await process_meeting_state(interaction, starting)


async def process_meeting_state(interaction: discord.Interaction, starting: bool) -> None:
    if not can_use_notice_admin(interaction):
        await interaction.followup.send("소유자 또는 등록된 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    phase = meeting_phase(guild_id)
    if (starting and phase == "active") or (not starting and phase != "active"):
        await interaction.followup.send("이미 회의 중입니다." if starting else "진행 중인 회의가 없습니다.", ephemeral=True)
        return
    if starting:
        try:
            meeting_chat_feature.begin(guild_id)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
    settings = get_meeting_settings(guild_id)
    if starting and (settings.meeting_channel_id is None or settings.log_channel_id is None):
        await interaction.followup.send("먼저 회의방과 로그방을 등록해 주세요.", ephemeral=True)
        return
    channel = bot.get_channel(settings.notice_channel_id) if settings.notice_channel_id else None
    try:
        if channel is None and settings.notice_channel_id:
            channel = await bot.fetch_channel(settings.notice_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("공지방을 먼저 등록해 주세요.", ephemeral=True)
            return
        missing_ids = []
        if not starting:
            meeting_channel = interaction.guild.get_channel(settings.meeting_channel_id)
            if not isinstance(meeting_channel, (discord.VoiceChannel, discord.StageChannel)):
                await interaction.followup.send("회의방을 찾을 수 없습니다. 회의방을 다시 등록한 뒤 종료해 주세요.", ephemeral=True)
                return
            missing, _ = await get_meeting_non_participants(interaction.guild, settings.meeting_channel_id)
            missing_ids = [member.id for member in missing]
            try:
                meeting_chat_feature.validate_voice(meeting_channel)
                await meeting_chat_feature.cleanup(interaction.guild, settings)
                await meeting_chat_feature.close_voice(meeting_channel)
            except (ValueError, discord.HTTPException, asyncio.TimeoutError) as exc:
                logging.getLogger(__name__).exception("회의 종료 정리 실패")
                await interaction.followup.send("회의방 퇴장 또는 채팅 청소·잠금을 완료하지 못해 종료를 중단했습니다. 일부 작업은 이미 반영됐을 수 있습니다. 채널 관리·멤버 이동 등 봇 권한을 확인한 뒤 /회의 종료를 다시 실행해 주세요.", ephemeral=True)
                return
        
        set_meeting_phase(guild_id, "active" if starting else "ended")
        message = await channel.send(
            content="|| @everyone ||\n# 회의가 시작되었습니다" if starting else "회의가 종료되었습니다",
            embed=discord.Embed(description="신속히 참여해주세요", colour=discord.Colour.blurple()) if starting else None,
            view=None if starting else MeetingEndView(),
            allowed_mentions=discord.AllowedMentions(everyone=starting, users=False, roles=False),
        )
    except discord.HTTPException:
        set_meeting_phase(guild_id, phase)
        await interaction.followup.send("안내를 전송하지 못했습니다. 공지방 권한을 확인해 주세요.", ephemeral=True)
        return
    if not starting:
        set_meeting_phase(guild_id, "ended", message.id)
    await interaction.followup.send(
        "회의를 시작했습니다. 입퇴장 로그를 켜고 사유 신청을 마감했습니다." if starting
        else f"회의를 종료하고 입퇴장 로그를 껐습니다. 종료 시점 미참여자는 {len(missing_ids)}명입니다. 종료 안내에서 사유 초기화를 할 수 있습니다.",
        ephemeral=True,
    )


@meeting_group.command(name="시작", description="회의를 시작하고 사유 신청을 마감하며 입퇴장 로그를 켭니다.")
@app_commands.guild_only()
async def start_meeting(interaction: discord.Interaction) -> None:
    await change_meeting_state(interaction, True)


@meeting_group.command(name="종료", description="회의를 종료하고 사유 초기화 버튼을 표시합니다.")
@app_commands.guild_only()
async def end_meeting(interaction: discord.Interaction) -> None:
    await change_meeting_state(interaction, False)


bot.tree.add_command(meeting_group)
bot.tree.add_command(admin_group)

import request_safety
request_safety_feature = request_safety.install(bot, database)

import meeting_chat
meeting_chat_feature = meeting_chat.install(bot, database, is_owner, get_meeting_settings)

for command in bot.tree.walk_commands():
    command.description = '·'
    if isinstance(command, app_commands.Command):
        app_commands.describe(**{p.name: '·' for p in command.parameters})(command)

@bot.event
async def on_ready():
    logging.getLogger(__name__).info("light 로그인 완료: %s", bot.user)

@bot.tree.error
async def command_error(interaction, error):
    logging.getLogger(__name__).error("명령어 처리 실패", exc_info=error)
    try:
        message = "처리하지 못했습니다. 봇 권한과 콘솔 로그를 확인해 주세요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

async def run_bot(token):
    async with bot:
        await bot.start(token)

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "붙여넣을_봇_토큰":
        raise RuntimeError(f"{ENVIRONMENT_PATH} 파일에 DISCORD_TOKEN을 설정해 주세요.")
    discord.utils.setup_logging()
    try:
        asyncio.run(run_bot(token))
    except KeyboardInterrupt:
        pass
