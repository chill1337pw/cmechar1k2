import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import discord
from discord.ext import commands
from discord.ui import Select, View
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from dateutil import parser as dateparser

# --------- Конфиг из окружения ----------
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # канал для постов/реакций
DB_FILE = "reminders.db"

# --------- Интенты ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler()

# --------- База данных ----------
def db_init() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            kind TEXT NOT NULL,              -- 'role' | 'dm'
            role_id INTEGER,                 -- если kind=role
            target_user_id INTEGER,          -- если kind=dm
            message TEXT NOT NULL,
            mode TEXT NOT NULL,              -- 'one' | 'weekly'
            run_at TEXT,                     -- ISO (для one)
            weekly_days TEXT,                -- 'mon,wed,fri' (для weekly)
            weekly_time TEXT,                -- 'HH:MM' (для weekly)
            ack_required INTEGER NOT NULL,   -- 0/1
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL,
            dm_sent INTEGER NOT NULL,
            details TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS allowed_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(guild_id, user_id)
        )"""
    )
    conn.commit()
    conn.close()

def db_execute(query: str, params: Tuple = ()) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    conn.close()

def db_fetchall(query: str, params: Tuple = ()) -> List[sqlite3.Row]:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

def db_fetchone(query: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(query, params)
    row = c.fetchone()
    conn.close()
    return row

# --------- Утилиты ----------
DAY_ALIASES = {
    "mon": "mon", "monday": "mon", "пн": "mon",
    "tue": "tue", "tuesday": "tue", "вт": "tue",
    "wed": "wed", "wednesday": "wed", "ср": "wed",
    "thu": "thu", "thursday": "thu", "чт": "thu",
    "fri": "fri", "friday": "fri", "пт": "fri",
    "sat": "sat", "saturday": "sat", "сб": "sat",
    "sun": "sun", "sunday": "sun", "вс": "sun",
}
DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def normalize_days(text: str) -> List[str]:
    parts = [p.strip().lower() for p in text.split(",")]
    days: List[str] = []
    for p in parts:
        if p in DAY_ALIASES:
            days.append(DAY_ALIASES[p])
    # уникальные, по порядку недели
    uniq = []
    for d in DAY_ORDER:
        if d in days and d not in uniq:
            uniq.append(d)
    return uniq

def parse_hhmm(text: str) -> Tuple[int, int]:
    t = text.strip()
    if ":" not in t:
        raise ValueError("Ожидалось HH:MM")
    hh, mm = t.split(":")
    h = int(hh)
    m = int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Часы 0..23, минуты 0..59")
    return h, m

def adjust_time_minus_minutes(h: int, m: int, minus_min: int) -> Tuple[int, int, int]:
    """Возвращает (day_shift, new_h, new_m). day_shift = -1 если ушли на предыдущий день."""
    total = h * 60 + m
    total -= minus_min
    if total >= 0:
        return (0, total // 60, total % 60)
    total += 24 * 60
    return (-1, total // 60, total % 60)

def prev_day(day: str) -> str:
    idx = DAY_ORDER.index(day)
    return DAY_ORDER[(idx - 1) % 7]

def can_create(ctx: commands.Context) -> bool:
    if ctx.author == ctx.guild.owner:
        return True
    row = db_fetchone(
        "SELECT 1 FROM allowed_users WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, ctx.author.id),
    )
    return row is not None

# --------- Планирование ----------
async def post_ack_and_wait(channel: discord.TextChannel, rid: int, role: Optional[discord.Role], message: str) -> Tuple[List[int], Optional[int]]:
    """Публикует ACK-сообщение с ✅, ждёт 5 минут, возвращает список user_id с реакцией и id сообщения."""
    try:
        mention = role.mention if role else ""
        ack_msg = await channel.send(f"[ACK {rid}] {mention} {message}\nНажмите ✅, если НЕ хотите получать ЛС по этому напоминанию.")
        await ack_msg.add_reaction("✅")
        await asyncio.sleep(300)  # 5 минут
        # Собираем IDs отреагировавших
        reacted_ids: List[int] = []
        for reaction in ack_msg.reactions:
            if str(reaction.emoji) == "✅":
                async for u in reaction.users():
                    if not u.bot:
                        reacted_ids.append(u.id)
        return reacted_ids, ack_msg.id
    except Exception:
        return [], None

async def do_send(reminder_id: int) -> None:
    """Основная отправка напоминания (однократная или еженедельная)."""
    row = db_fetchone("SELECT * FROM reminders WHERE id=? AND active=1", (reminder_id,))
    if not row:
        return

    guild = bot.get_guild(row["guild_id"])
    if not guild:
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    kind = row["kind"]           # 'role' | 'dm'
    role_id = row["role_id"]
    target_user_id = row["target_user_id"]
    message = row["message"]
    ack_required = bool(row["ack_required"])

    role: Optional[discord.Role] = guild.get_role(role_id) if role_id else None

    reacted_ids: List[int] = []
    ack_message_id: Optional[int] = None

    # Если включена проверка реакции — постим ACK заранее в том же джобе, ждём 5 минут.
    if ack_required:
        reacted_ids, ack_message_id = await post_ack_and_wait(channel, row["id"], role if kind == "role" else None, message)

    dm_sent = 0

    if kind == "role":
        # Публикуем основное сообщение в канал (с тегом роли)
        if role is not None:
            await channel.send(f"{role.mention} {message}")
        else:
            await channel.send(message)

        # Рассылаем ЛС тем, кто НЕ поставил ✅ (и кто видит канал)
        if role is not None:
            for member in role.members:
                if member.bot:
                    continue
                perms = channel.permissions_for(member)
                sees_channel = perms.read_messages
                if ack_required and sees_channel and member.id in reacted_ids:
                    continue
                try:
                    await member.send(message)
                    dm_sent += 1
                except Exception:
                    # игнорируем закрытые ЛС
                    pass
    else:
        # kind == 'dm' — одно целевое лицо
        if not target_user_id:
            return
        member = guild.get_member(target_user_id)
        if member and not member.bot:
            # Если ack включён, проверяем реакцию пользователя (если у него есть доступ к каналу)
            if ack_required:
                perms = channel.permissions_for(member)
                if perms.read_messages and member.id in reacted_ids:
                    pass  # пропускаем ЛС
                else:
                    try:
                        await member.send(message)
                        dm_sent += 1
                    except Exception:
                        pass
            else:
                try:
                    await member.send(message)
                    dm_sent += 1
                except Exception:
                    pass

    # История
    db_execute(
        "INSERT INTO history(reminder_id, sent_at, dm_sent, details) VALUES (?,?,?,?)",
        (row["id"], datetime.utcnow().isoformat(), dm_sent, f"ack_msg_id={ack_message_id} reacted={len(reacted_ids)}"),
    )

    # Если одноразовое — деактивируем
    if row["mode"] == "one":
        db_execute("UPDATE reminders SET active=0 WHERE id=?", (row["id"],))

def schedule_reminder(row: sqlite3.Row) -> None:
    """Ставит задачи в APScheduler в зависимости от режима и ack."""
    rid = row["id"]
    mode = row["mode"]
    ack_required = bool(row["ack_required"])

    if mode == "one":
        # Если ack включён — запускаем джоб за 5 мин до времени (внутри он подождёт 5 мин и отправит)
        run_at = dateparser.parse(row["run_at"])
        if ack_required:
            run_at = run_at - timedelta(minutes=5)
        scheduler.add_job(do_send, DateTrigger(run_date=run_at), args=[rid], id=f"rem_{rid}", replace_existing=True)
    else:
        # weekly: считаем время старта джоба (если ack — минус 5 минут + сдвиг дня)
        days = [d.strip() for d in row["weekly_days"].split(",") if d.strip()]
        days = [d for d in days if d in DAY_ORDER]
        hh, mm = parse_hhmm(row["weekly_time"])
        if ack_required:
            # вычисляем смещение
            day_shift, nh, nm = adjust_time_minus_minutes(hh, mm, 5)
            if day_shift == 0:
                cron_days = days
            else:
                # сдвиг на предыдущий день
                cron_days = [prev_day(d) for d in days]
            scheduler.add_job(
                do_send,
                CronTrigger(day_of_week=",".join(cron_days), hour=nh, minute=nm),
                args=[rid],
                id=f"rem_{rid}",
                replace_existing=True,
            )
        else:
            scheduler.add_job(
                do_send,
                CronTrigger(day_of_week=",".join(days), hour=hh, minute=mm),
                args=[rid],
                id=f"rem_{rid}",
                replace_existing=True,
            )

async def load_all_reminders() -> None:
    rows = db_fetchall("SELECT * FROM reminders WHERE active=1")
    for r in rows:
        schedule_reminder(r)

# --------- Команды ----------
@bot.event
async def on_ready():
    db_init()
    if CHANNEL_ID == 0:
        print("⚠️ Переменная окружения CHANNEL_ID не задана!")
    scheduler.start()
    await load_all_reminders()
    print(f"✅ Бот запущен как {bot.user} (готов)")

def ensure_allowed():
    async def predicate(ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("Эту команду можно использовать только на сервере.")
            return False
        if can_create(ctx):
            return True
        await ctx.send("⛔ У вас нет прав для этой команды.")
        return False
    return commands.check(predicate)

@bot.command(name="add_allowed_user")
async def add_allowed_user_cmd(ctx: commands.Context, user_id: int):
    if ctx.author != ctx.guild.owner:
        await ctx.send("⛔ Только владелец сервера может добавлять разрешённых.")
        return
    db_execute("INSERT OR IGNORE INTO allowed_users(guild_id, user_id) VALUES (?,?)", (ctx.guild.id, user_id))
    await ctx.send(f"✅ Пользователь `{user_id}` добавлен в белый список.")

@bot.command(name="remove_allowed_user")
async def remove_allowed_user_cmd(ctx: commands.Context, user_id: int):
    if ctx.author != ctx.guild.owner:
        await ctx.send("⛔ Только владелец сервера может убирать разрешённых.")
        return
    db_execute("DELETE FROM allowed_users WHERE guild_id=? AND user_id=?", (ctx.guild.id, user_id))
    await ctx.send(f"✅ Пользователь `{user_id}` удалён из белого списка.")

@bot.command(name="list_reminders")
@ensure_allowed()
async def list_reminders_cmd(ctx: commands.Context):
    rows = db_fetchall("SELECT * FROM reminders WHERE guild_id=? AND active=1 ORDER BY id DESC LIMIT 20", (ctx.guild.id,))
    if not rows:
        await ctx.send("📭 Активных напоминаний нет.")
        return
    lines = []
    for r in rows:
        if r["mode"] == "one":
            when = r["run_at"]
        else:
            when = f"{r['weekly_days']} @ {r['weekly_time']}"
        target = f"role:{r['role_id']}" if r["kind"] == "role" else f"user:{r['target_user_id']}"
        lines.append(f"ID {r['id']} | {target} | {r['mode']} | {when} | ack={r['ack_required']}")
    await ctx.send("**Напоминания:**\n" + "\n".join(lines))

@bot.command(name="history")
@ensure_allowed()
async def history_cmd(ctx: commands.Context):
    rows = db_fetchall(
        """SELECT h.reminder_id, h.sent_at, h.dm_sent
           FROM history h
           JOIN reminders r ON r.id=h.reminder_id
           WHERE r.guild_id=?
           ORDER BY h.sent_at DESC LIMIT 20""",
        (ctx.guild.id,),
    )
    if not rows:
        await ctx.send("📜 История пуста.")
        return
    text = "\n".join([f"RID {r['reminder_id']} — {r['sent_at']} — DM: {r['dm_sent']}" for r in rows])
    await ctx.send("**История отправок (последние 20):**\n" + text)

# --------- Мастер создания напоминания ---------
@bot.command(name="reminder")
@ensure_allowed()
async def reminder_cmd(ctx: commands.Context):
    await ctx.send("Какой тип напоминания? Введите `role` или `dm`.")

    def author_check(m: discord.Message) -> bool:
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg_kind = await bot.wait_for("message", timeout=120.0, check=author_check)
    except asyncio.TimeoutError:
        await ctx.send("⏰ Время ожидания истекло.")
        return

    kind = msg_kind.content.strip().lower()
    if kind not in ("role", "dm"):
        await ctx.send("Нужно ввести `role` или `dm`.")
        return

    role_id = None
    target_user_id = None

    if kind == "role":
        roles = [r for r in ctx.guild.roles if r != ctx.guild.default_role]
        if not roles:
            await ctx.send("На сервере нет ролей, кроме @everyone.")
            return

        options = [discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in roles[:25]]  # Discord ограничение 25
        select = Select(placeholder="Выберите роль для напоминания", min_values=1, max_values=1, options=options)

        async def on_select(interaction: discord.Interaction):
            nonlocal role_id
            if interaction.user != ctx.author:
                await interaction.response.send_message("Только инициатор может выбирать.", ephemeral=True)
                return
            role_id = int(select.values[0])
            await interaction.response.edit_message(content=f"Выбрана роль: <@&{role_id}>", view=None)

        view = View(timeout=120)
        select.callback = on_select
        view.add_item(select)
        await ctx.send("Выберите роль:", view=view)

        # ждём, пока пользователь нажмёт
        def role_chosen() -> bool:
            return role_id is not None

        for _ in range(120):
            if role_chosen():
                break
            await asyncio.sleep(1)

        if role_id is None:
            await ctx.send("⏰ Время выбора роли истекло.")
            return
    else:
        await ctx.send("Укажите пользователя (упоминание или ID).")
        try:
            msg_user = await bot.wait_for("message", timeout=120.0, check=author_check)
        except asyncio.TimeoutError:
            await ctx.send("⏰ Время ожидания истекло.")
            return

        if msg_user.mentions:
            target_user_id = msg_user.mentions[0].id
        else:
            try:
                target_user_id = int(msg_user.content.strip())
            except Exception:
                await ctx.send("Не удалось распознать пользователя.")
                return

    await ctx.send("Режим? Введите `one` (однократно) или `weekly` (по дням недели).")
    try:
        msg_mode = await bot.wait_for("message", timeout=120.0, check=author_check)
    except asyncio.TimeoutError:
        await ctx.send("⏰ Время ожидания истекло.")
        return
    mode = msg_mode.content.strip().lower()
    if mode not in ("one", "weekly"):
        await ctx.send("Нужно ввести `one` или `weekly`.")
        return

    run_at_iso = None
    weekly_days = None
    weekly_time = None

    if mode == "one":
        await ctx.send("Введите дату и время в формате `YYYY-MM-DD HH:MM` (например, `2025-08-15 18:00`). Время — по UTC.")
        try:
            msg_dt = await bot.wait_for("message", timeout=180.0, check=author_check)
            dt = dateparser.parse(msg_dt.content.strip())
            run_at_iso = dt.replace(second=0, microsecond=0).isoformat()
        except Exception:
            await ctx.send("Не удалось распарсить дату/время.")
            return
    else:
        await ctx.send("Введите дни недели через запятую (напр. `mon,wed,fri` или `пн,ср,пт`).")
        try:
            msg_days = await bot.wait_for("message", timeout=120.0, check=author_check)
        except asyncio.TimeoutError:
            await ctx.send("⏰ Время ожидания истекло.")
            return
        days = normalize_days(msg_days.content)
        if not days:
            await ctx.send("Не удалось распознать дни недели.")
            return
        weekly_days = ",".join(days)

        await ctx.send("Введите время в формате `HH:MM` (24ч, по UTC).")
        try:
            msg_time = await bot.wait_for("message", timeout=120.0, check=author_check)
            hh, mm = parse_hhmm(msg_time.content.strip())
            weekly_time = f"{hh:02d}:{mm:02d}"
        except Exception as e:
            await ctx.send(f"Ошибка времени: {e}")
            return

    await ctx.send("Текст напоминания?")
    try:
        msg_text = await bot.wait_for("message", timeout=240.0, check=author_check)
    except asyncio.TimeoutError:
        await ctx.send("⏰ Время ожидания истекло.")
        return
    message_text = msg_text.content.strip()

    await ctx.send("Включить проверку реакции ✅ перед ЛС? (`yes`/`no`)")
    try:
        msg_ack = await bot.wait_for("message", timeout=120.0, check=author_check)
    except asyncio.TimeoutError:
        await ctx.send("⏰ Время ожидания истекло.")
        return
    ack_required = 1 if msg_ack.content.strip().lower() in ("yes", "y", "да", "true", "1") else 0

    # Сохраняем в БД
    db_execute(
        """INSERT INTO reminders
           (guild_id, creator_id, kind, role_id, target_user_id, message, mode, run_at, weekly_days, weekly_time, ack_required, active, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ctx.guild.id, ctx.author.id, kind,
            role_id, target_user_id,
            message_text, mode,
            run_at_iso, weekly_days, weekly_time,
            ack_required, 1, datetime.utcnow().isoformat()
        )
    )
    # Получим последнюю вставку
    row = db_fetchone("SELECT * FROM reminders WHERE rowid = last_insert_rowid()")
    if not row:
        await ctx.send("❌ Не удалось создать напоминание.")
        return

    # Планируем
    schedule_reminder(row)
    await ctx.send(f"✅ Напоминание создано
