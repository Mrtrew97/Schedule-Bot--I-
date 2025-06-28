import discord
from discord.ext import commands, tasks
import aiosqlite
from datetime import datetime, timedelta, timezone
import pytz
import os
import json
from dotenv import load_dotenv

load_dotenv()

# === Load config from .env ===
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = int(os.getenv("APPLICATION_ID"))
GUILD_ID = int(os.getenv("GUILD_ID"))
EVENTS_CHANNEL_ID = int(os.getenv("CHANNEL_ID"))  # your .env uses CHANNEL_ID for events channel
COMMANDS_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID"))  # only commands from here count
ROLE_ID_HOME_KINGDOM = int(os.getenv("ROLE_ID_HOME_KINGDOM"))

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)

UTC = pytz.utc

# === Database setup ===
async def setup_database():
    async with aiosqlite.connect("events.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                event_name TEXT,
                event_time TEXT,
                channel_id INTEGER,
                message_id INTEGER,
                reminders_sent TEXT DEFAULT '[]'
            )
        """)
        await db.commit()

@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await setup_database()
    check_events.start()

# === Helper: parse datetime from input ===
def parse_datetime(time_str, date_str=None):
    try:
        if date_str:
            dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            now = datetime.now(timezone.utc)
            dt_today = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
            dt_today = dt_today.replace(tzinfo=timezone.utc)
            if dt_today < now:
                dt_today += timedelta(days=1)
            dt = dt_today
        return dt
    except Exception as e:
        print(f"Error parsing datetime: {e}")
        return None

# === Command to schedule any event type ===
@bot.command(name="schedule")
async def schedule(ctx, event_type: str, time_utc: str, *args):
    # Only accept commands from specific channel
    if ctx.channel.id != COMMANDS_CHANNEL_ID:
        return

    """
    Usage:
    /schedule hydra 19:00 "Zone 3 Push"
    /schedule caravan 19:00 13/06/2025 "Caravan Event"
    """
    date_str = None
    event_name = ""

    if len(args) == 1:
        event_name = args[0]
    elif len(args) >= 2:
        date_str = args[0]
        event_name = " ".join(args[1:])
    else:
        await ctx.send("Invalid command format.\nUsage: `/schedule event_type HH:MM [dd/mm/yyyy] \"Event Name\"`")
        return

    dt = parse_datetime(time_utc, date_str)
    if not dt:
        await ctx.send("Invalid date/time format. Use HH:MM or HH:MM dd/mm/yyyy")
        return

    async with aiosqlite.connect("events.db") as db:
        await db.execute("""
            INSERT INTO events (event_type, event_name, event_time, channel_id)
            VALUES (?, ?, ?, ?)
        """, (event_type, event_name, dt.isoformat(), EVENTS_CHANNEL_ID))
        await db.commit()

        cursor = await db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        event_id = row[0]

    time_delta = dt - datetime.now(timezone.utc)
    total_seconds = int(time_delta.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        time_remaining = f"{days} days {hours} hours {minutes} minutes"
    else:
        time_remaining = f"{hours} hours {minutes} minutes"

    mention = f"<@&{ROLE_ID_HOME_KINGDOM}>"
    
    # Windows-compatible date formatting for strftime (avoid %-d, %-I)
    def format_dt(dt_obj):
        # Day without leading zero
        day = dt_obj.day
        # Hour (12-hour format) without leading zero
        hour = dt_obj.strftime("%I").lstrip("0")
        if hour == "":
            hour = "0"
        return dt_obj.strftime(f"%A, %B {day}, %Y {hour}:%M %p")

    formatted_time = format_dt(dt)

    channel = bot.get_channel(EVENTS_CHANNEL_ID)
    if not channel:
        await ctx.send("Events channel not found. Please check configuration.")
        return

    # --- EMBED for scheduled event ---
    embed = discord.Embed(
        title=f"🛡️ Scheduled {event_type.capitalize()}",
        description=f"{event_name}",
        color=discord.Color.gold()
    )
    embed.add_field(name="🕒 Time", value=formatted_time, inline=False)
    embed.add_field(name="⏳ Time Remaining", value=time_remaining, inline=False)
    embed.add_field(name="🗳️ React with:", value="✅ — Yes\n❌ — No\n❓ — Maybe", inline=False)
    embed.set_footer(text=f"Event ID: {event_id}")
    embed.timestamp = dt  # show UTC time in embed footer timestamp

    msg = await channel.send(content=mention, embed=embed)

    for emoji in ["✅", "❌", "❓"]:
        await msg.add_reaction(emoji)

    async with aiosqlite.connect("events.db") as db:
        await db.execute("UPDATE events SET message_id = ? WHERE id = ?", (msg.id, event_id))
        await db.commit()

# === Background task to check for reminders and event start ===
@tasks.loop(seconds=60)
async def check_events():
    now = datetime.now(timezone.utc)

    async with aiosqlite.connect("events.db") as db:
        async with db.execute("SELECT id, event_type, event_name, event_time, channel_id, reminders_sent FROM events") as cursor:
            rows = await cursor.fetchall()

        for event_id, event_type, name, iso_time, channel_id, reminders_json in rows:
            event_time = datetime.fromisoformat(iso_time).replace(tzinfo=timezone.utc)
            delta = event_time - now
            total_seconds = delta.total_seconds()

            if total_seconds < -60:
                # Event passed more than 1 minute ago; optionally skip or clean up
                continue

            reminders_sent = json.loads(reminders_json) if reminders_json else []

            reminders_to_check = []

            if total_seconds > 3600:  # More than 1 hour left
                halfway = total_seconds / 2
                if halfway > 60:
                    reminders_to_check.append(("halfway", halfway))
                reminders_to_check.extend([
                    ("12h", 43200),
                    ("6h", 21600),
                    ("3h", 10800),
                    ("1h", 3600),
                    ("30m", 1800),
                    ("10m", 600)
                ])
            else:
                reminders_to_check.extend([
                    ("30m", 1800),
                    ("15m", 900)
                    
                ])

            # Add the final event time ping
            # Only send once, mark with "start"
            if "start" not in reminders_sent and -60 <= total_seconds <= 0:
                reminders_to_check.append(("start", 0))

            due_reminders = []
            for name_r, seconds_before in reminders_to_check:
                # For "start" reminder, seconds_before == 0, trigger if within -60 to 0 seconds
                if name_r == "start":
                    if -60 <= total_seconds <= 0:
                        due_reminders.append(name_r)
                else:
                    if name_r not in reminders_sent and 0 <= total_seconds - seconds_before < 60:
                        due_reminders.append(name_r)

            if due_reminders:
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                mention = f"<@&{ROLE_ID_HOME_KINGDOM}>"

                for reminder in due_reminders:
                    # Compose embed message for each reminder type
                    if reminder == "halfway":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: {event_type.capitalize()} Halfway There!",
                            description=f"Event **{event_type.capitalize()} - {name}** is halfway there!\nHappening at <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.orange()
                        )
                        embed.set_footer(text="Get ready!")
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "12h":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 12 Hours Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 12 hours.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.dark_orange()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "6h":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 6 Hours Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 6 hours.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.gold()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "3h":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 3 Hours Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 3 hours.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.gold()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "1h":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 1 Hour Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 1 hour.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.green()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "30m":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 30 Minutes Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 30 minutes.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.green()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "15m":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 15 Minutes Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 15 minutes.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.green()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "10m":
                        embed = discord.Embed(
                            title=f"⏰ Reminder: 10 Minutes Left",
                            description=f"Event **{event_type.capitalize()} - {name}** starts in 10 minutes.\nTime: <t:{int(event_time.timestamp())}:F>",
                            color=discord.Color.green()
                        )
                        await channel.send(content=mention, embed=embed)

                    elif reminder == "start":
                        embed = discord.Embed(
                            title=f"🚨 {event_type.capitalize()} Started!",
                            description=f"**{event_type.capitalize()} - {name} IS NOW!!! LET'S DO THIS!!**",
                            color=discord.Color.red()
                        )
                        await channel.send(content=mention, embed=embed)

                reminders_sent.extend(due_reminders)
                reminders_sent_json = json.dumps(reminders_sent)
                await db.execute("UPDATE events SET reminders_sent = ? WHERE id = ?", (reminders_sent_json, event_id))
                await db.commit()

# === Reaction handler to enforce single vote per user and keep reaction counts intact ===
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    # Only enforce reactions on event messages in the EVENTS_CHANNEL_ID
    if reaction.message.channel.id != EVENTS_CHANNEL_ID:
        return

    # Allowed vote emojis
    vote_emojis = {"✅", "❌", "❓"}
    if reaction.emoji not in vote_emojis:
        return

    message = reaction.message
    # Remove other votes by the same user on this message
    for react in message.reactions:
        if react.emoji != reaction.emoji and react.emoji in vote_emojis:
            async for u in react.users():
                if u.id == user.id:
                    try:
                        await message.remove_reaction(react.emoji, user)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

bot.run(TOKEN)
