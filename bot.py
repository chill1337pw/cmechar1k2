# bot.py
import os
import sqlite3
import asyncio
import discord
from discord.ext import commands
from discord.ui import Select, View
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from dateutil import parser

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
DB_FILE = "reminders.db"

# -------------------- База данных --------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER,
        role_id INTEGER,
        message TEXT,
        remind_time TEXT,
        repeat_days TEXT,
        reaction_check INTEGER,
        sent INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS allowed_users (
        user_id INTEGER PRIMARY KEY
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER,
        sent_at TEXT
    )""")
    conn.commit()
    conn.close()

def add_allowed_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def is_allowed(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,))
    allowed = c.fetchone() is not None
    conn.close()
    return allowed

# -------------------- Бот --------------------
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)
scheduler = AsyncIOScheduler()

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} запущен")
    init_db()
    scheduler.start()
    await load_existing_reminders()

async def load_existing_reminders():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, message, remind_time, repeat_days, role_id, reaction_check FROM reminders WHERE sent=0")
    rows = c.fetchall()
    conn.close()
    for rid, msg, time_str, days, role_id, react_check in rows:
        if days:
            scheduler.add_job(send_reminder, CronTrigger(day_of_week=days, hour=int(time_str.split(':')[0]),
                                                        minute=int(time_str.split(':')[1])),
                              args=[rid, msg, role_id, react_check])
        else:
            remind_time = parser.parse(time_str)
            scheduler.add_job(send_reminder, DateTrigger(run_date=remind_time),
                              args=[rid, msg, role_id, react_check])

# -------------------- Создание напоминания --------------------
@bot.command(name="reminder")
async def reminder_cmd(ctx):
    if not is_allowed(ctx.author.id):
        await ctx.send("⛔ У вас нет прав для создания напоминаний.")
        return

    # Выбор роли
    roles = [r for r in ctx.guild.roles if r != ctx.guild.default_role]
    options = [discord.SelectOption(label=r.name, value=str(r.id)) for r in roles]
    select_role = Select(placeholder="Выберите роль", options=options)

    async def role_callback(interaction):
        role_id = int(select_role.values[0])

        # Запрос текста напоминания
        await interaction.response.send_message("Введите текст напоминания:")
        msg = await bot.wait_for("message", check=lambda m: m.author == ctx.author)
        reminder_text = msg.content

        # Одноразовое или повторяющееся
        await ctx.send("Одноразовое или повторяющееся? (one/repeat)")
msg_type = await bot.wait_for("message", check=lambda m: m.author == ctx.author)
repeat_days = None
    if msg_type.content.lower() == "repeat":
            await ctx.send("Введите дни недели (например: mon,wed,fri):")
            days_msg = await bot.wait_for("message", check=lambda m: m.author == ctx.author)
            repeat_days = days_msg.content.lower()

        # Время
        await ctx.send("Введите время (например: 2025-08-15 18:00 или '15:30'):")
        time_msg = await bot.wait_for("message", check=lambda m: m.author == ctx.author)
        remind_time = time_msg.content

        # Реакция перед ЛС
        await ctx.send("Включить проверку реакции перед ЛС? (yes/no):")
        react_msg = await bot.wait_for("message", check=lambda m: m.author == ctx.author)
        reaction_check = 1 if react_msg.content.lower() == "yes" else 0

        # Сохраняем
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""INSERT INTO reminders (creator_id, role_id, message, remind_time, repeat_days, reaction_check)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (ctx.author.id, role_id, reminder_text, remind_time, repeat_days, reaction_check))
        rid = c.lastrowid
        conn.commit()
        conn.close()

        if repeat_days:
            scheduler.add_job(send_reminder, CronTrigger(day_of_week=repeat_days,
                                                         hour=int(remind_time.split(':')[0]),
                                                         minute=int(remind_time.split(':')[1])),
                              args=[rid, reminder_text, role_id, reaction_check])
        else:
            run_time = parser.parse(remind_time)
            scheduler.add_job(send_reminder, DateTrigger(run_date=run_time),
                              args=[rid, reminder_text, role_id, reaction_check])

        await ctx.send("✅ Напоминание создано!")

    view = View()
    select_role.callback = role_callback
    view.add_item(select_role)
    await ctx.send("Выберите роль для напоминания:", view=view)

# -------------------- Отправка напоминаний --------------------
async def send_reminder(rid, message, role_id, reaction_check):
    guild = bot.get_guild((await bot.fetch_channel(CHANNEL_ID)).guild.id)
    role = guild.get_role(role_id)
    channel = bot.get_channel(CHANNEL_ID)
    sent_dm_users = []

    if reaction_check:
        msg = await channel.send(f"{role.mention} {message}\nНажмите ✅ чтобы не получать ЛС.")
        await msg.add_reaction("✅")
        await asyncio.sleep(300)  # 5 минут
        msg = await channel.fetch_message(msg.id)
        reacted_users = [u.id for r in msg.reactions if str(r.emoji) == "✅" async for u in r.users()]
    else:
        reacted_users = []

    for member in role.members:
        if member.bot:
            continue
        if member.id not in reacted_users:
            try:
                await member.send(message)
                sent_dm_users.append(member.id)
            except:
                pass

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO history (reminder_id, sent_at) VALUES (?, ?)", (rid, datetime.now().isoformat()))
    c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()

# -------------------- Управление --------------------
@bot.command(name="add_allowed_user")
async def add_allowed(ctx, user_id: int):
    if ctx.author == ctx.guild.owner:
        add_allowed_user(user_id)
        await ctx.send(f"✅ Пользователь {user_id} теперь может создавать напоминания.")
    else:
        await ctx.send("⛔ Только владелец сервера может добавлять разрешённых пользователей.")

@bot.command(name="reminder_history")
async def reminder_history(ctx):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT reminder_id, sent_at FROM history ORDER BY sent_at DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await ctx.send("📭 История пуста.")
        return
    history_text = "\n".join([f"ID {r[0]} — {r[1]}" for r in rows])
    await ctx.send(f"📜 История:\n{history_text}")

# ------------------------------------------------------

bot.run(TOKEN)


