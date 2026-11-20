"""Server Memory Bot
- React with a star to any message to save it to the server's Hall of Fame.
- Every day the bot posts a memory: a starred message or one from a year ago.
- /memory pulls a random memory on demand.
- /setchannel picks where the daily memory is posted.
"""

import datetime as dt
import os
import random
import sqlite3

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
POST_HOUR_UTC = int(os.getenv("POST_HOUR_UTC", "9"))  # daily post time (UTC)
STAR = "⭐"

# ---------- Database ----------
db = sqlite3.connect("memories.db")
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS hall (
        message_id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        author_name TEXT NOT NULL,
        content TEXT,
        image_url TEXT,
        created_at TEXT NOT NULL,
        jump_url TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL
    );
    """
)
db.commit()


# ---------- Helpers ----------
def embed_from_row(row: sqlite3.Row) -> discord.Embed:
    created = dt.datetime.fromisoformat(row["created_at"])
    embed = discord.Embed(
        title="⭐ From the Hall of Fame",
        description=row["content"] or "",
        color=discord.Color.gold(),
        timestamp=created,
    )
    embed.set_author(name=row["author_name"])
    if row["image_url"]:
        embed.set_image(url=row["image_url"])
    embed.add_field(name="Original", value=f"[Jump to message]({row['jump_url']})")
    return embed


def embed_from_message(msg: discord.Message) -> discord.Embed:
    embed = discord.Embed(
        title="🕰️ On this day, one year ago",
        description=msg.content,
        color=discord.Color.blurple(),
        timestamp=msg.created_at,
    )
    embed.set_author(
        name=msg.author.display_name, icon_url=msg.author.display_avatar.url
    )
    embed.add_field(name="Original", value=f"[Jump to message]({msg.jump_url})")
    return embed


async def one_year_ago(channel: discord.abc.Messageable) -> discord.Embed | None:
    """Find a random message posted about a year ago in this channel."""
    target = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)
    start, end = target - dt.timedelta(days=1), target + dt.timedelta(days=1)
    candidates = []
    async for msg in channel.history(after=start, before=end, limit=200):
        if not msg.author.bot and len(msg.content.strip()) > 10:
            candidates.append(msg)
    return embed_from_message(random.choice(candidates)) if candidates else None


def from_hall(guild_id: int) -> discord.Embed | None:
    rows = db.execute("SELECT * FROM hall WHERE guild_id = ?", (guild_id,)).fetchall()
    return embed_from_row(random.choice(rows)) if rows else None


async def pick_memory(guild: discord.Guild, channel) -> discord.Embed | None:
    """Mix Hall of Fame picks with 'a year ago' picks, falling back if one is empty."""
    sources = ["year", "hall"] if random.random() < 0.4 else ["hall", "year"]
    for source in sources:
        embed = await one_year_ago(channel) if source == "year" else from_hall(guild.id)
        if embed:
            return embed
    return None


# ---------- Bot ----------
class MemoryBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # must also be enabled in the Developer Portal
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        daily_memory.start()


bot = MemoryBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != STAR or payload.guild_id is None:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    if msg.author.bot:
        return
    image = msg.attachments[0].url if msg.attachments else None
    if not msg.content and not image:
        return
    db.execute(
        "INSERT OR IGNORE INTO hall VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg.id,
            payload.guild_id,
            msg.channel.id,
            msg.author.display_name,
            msg.content,
            image,
            msg.created_at.isoformat(),
            msg.jump_url,
        ),
    )
    db.commit()


# ---------- Slash commands ----------
@bot.tree.command(name="memory", description="Pull a random memory from the server")
async def memory(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = await pick_memory(interaction.guild, interaction.channel)
    if embed:
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(
            f"No memories yet! React with {STAR} to great messages to save them."
        )


@bot.tree.command(name="setchannel", description="Set the channel for daily memories")
@app_commands.describe(channel="Where the bot should post each day")
@app_commands.default_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    db.execute(
        "INSERT OR REPLACE INTO settings VALUES (?, ?)",
        (interaction.guild.id, channel.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"Daily memories will be posted in {channel.mention} ✅", ephemeral=True
    )


@bot.tree.command(name="halloffame", description="How many moments have been saved?")
async def halloffame(interaction: discord.Interaction):
    count = db.execute(
        "SELECT COUNT(*) FROM hall WHERE guild_id = ?", (interaction.guild.id,)
    ).fetchone()[0]
    await interaction.response.send_message(f"{STAR} {count} moments saved so far.")


# ---------- Daily task ----------
@tasks.loop(time=dt.time(hour=POST_HOUR_UTC, tzinfo=dt.timezone.utc))
async def daily_memory():
    for row in db.execute("SELECT * FROM settings").fetchall():
        guild = bot.get_guild(row["guild_id"])
        channel = bot.get_channel(row["channel_id"])
        if guild is None or channel is None:
            continue
        embed = await pick_memory(guild, channel)
        if embed:
            await channel.send(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
