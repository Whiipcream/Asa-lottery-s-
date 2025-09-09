# bot.py
import discord
from discord.ext import commands, tasks
import asyncio
import json
import os
import datetime
import random
import string
import uuid
from typing import Dict, Any, Optional, List

# ---------- Configuration ----------
DATA_FILE = "lotteries_data.json"
BOT_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

# Channel/category names (edit to match server)
TICKET_CATEGORY_NAME = "Tickets"
LOTTERY_CATEGORY_NAME = "Lotteries"
LOTTERY_DISPLAY_CHANNEL = "lotteries"  # channel where active lotteries are posted

save_lock = asyncio.Lock()
# -----------------------------------

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS)

# In-memory structures (persisted to DATA_FILE)
# lotteries: dict[msg_id_str] = lottery_obj
# Each lottery_obj contains:
#  - id (message id when posted to display) (string)
#  - item, seller_id, ticket_price, max_tickets (or None), end_time (iso str)
#  - image_url (optional)
#  - tickets: list of {code: str, buyer_id: str}
#  - created_at
lotteries: Dict[str, Dict[str, Any]] = {}

def now_iso():
    return datetime.datetime.utcnow().isoformat()

def load_data():
    global lotteries
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            lotteries = data.get("lotteries", {})
        except Exception:
            lotteries = {}
    else:
        lotteries = {}

async def save_data():
    async with save_lock:
        data = {"lotteries": lotteries}
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)

def parse_duration(s: str) -> int:
    """
    Parse duration strings like:
    '10m' -> 600
    '30s' -> 30
    '1h' -> 3600
    default: seconds if plain number
    """
    s = s.strip().lower()
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    return int(s)

def gen_ticket_code() -> str:
    """Generate a short unique ticket code."""
    # Use uuid4 + short slice to be reasonably unique
    return uuid.uuid4().hex[:8].upper()

async def find_or_create_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    for c in guild.categories:
        if c.name.lower() == name.lower():
            return c
    return await guild.create_category(name)

async def find_or_create_channel(guild: discord.Guild, name: str, category_name: Optional[str]=None) -> discord.TextChannel:
    for ch in guild.text_channels:
        if ch.name.lower() == name.lower():
            return ch
    cat = None
    if category_name:
        cat = await find_or_create_category(guild, category_name)
    return await guild.create_text_channel(name, category=cat)

async def create_ticket_channel(guild: discord.Guild, seller: discord.Member, item_name: str) -> discord.TextChannel:
    cat = await find_or_create_category(guild, TICKET_CATEGORY_NAME)
    base = f"ticket-{seller.name.lower()}-{seller.discriminator}"
    chan_name = base
    # ensure unique
    existing_names = {c.name for c in cat.channels}
    if chan_name in existing_names:
        chan_name = f"{base}-{int(datetime.datetime.utcnow().timestamp())}"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        seller: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    channel = await guild.create_text_channel(chan_name, overwrites=overwrites, category=cat)
    await channel.send(f"🎟️ Ticket channel for {seller.mention}\nItem: **{item_name}**\nWhen the lottery ends the result will be posted here.")
    return channel

async def post_lottery_display(guild: discord.Guild, lottery: Dict[str, Any]) -> int:
    """
    Post the lottery to the display channel.
    Returns the message id posted (int).
    """
    display = await find_or_create_channel(guild, LOTTERY_DISPLAY_CHANNEL, LOTTERY_CATEGORY_NAME)
    embed = discord.Embed(
        title=f"Lottery: {lottery['item']}",
        description=(
            f"Seller: <@{lottery['seller_id']}>\n"
            f"Ticket Price: {lottery['ticket_price']}\n"
            f"Max Tickets: {lottery['max_tickets'] if lottery.get('max_tickets') else 'Unlimited'}\n"
            f"Ends: <t:{int(datetime.datetime.fromisoformat(lottery['end_time']).timestamp())}:R>\n\n"
            "Buy tickets with `!buy <count>` in this channel."
        ),
        timestamp=datetime.datetime.utcnow()
    )
    if lottery.get("image_url"):
        embed.set_image(url=lottery["image_url"])
    embed.add_field(name="Tickets Sold", value=str(len(lottery.get("tickets", []))))
    msg = await display.send(embed=embed)
    return msg.id

async def update_display_message(guild: discord.Guild, message_id: int):
    """
    Edit the display message embed to reflect updated tickets sold.
    """
    display_chan = await find_or_create_channel(guild, LOTTERY_DISPLAY_CHANNEL, LOTTERY_CATEGORY_NAME)
    try:
        msg = await display_chan.fetch_message(message_id)
    except discord.NotFound:
        return
    lottery = lotteries.get(str(message_id))
    if not lottery:
        return
    embed = discord.Embed(
        title=f"Lottery: {lottery['item']}",
        description=(
            f"Seller: <@{lottery['seller_id']}>\n"
            f"Ticket Price: {lottery['ticket_price']}\n"
            f"Max Tickets: {lottery['max_tickets'] if lottery.get('max_tickets') else 'Unlimited'}\n"
            f"Ends: <t:{int(datetime.datetime.fromisoformat(lottery['end_time']).timestamp())}:R>\n\n"
            "Buy tickets with `!buy <count>` in this channel."
        ),
        timestamp=datetime.datetime.utcnow()
    )
    if lottery.get("image_url"):
        embed.set_image(url=lottery["image_url"])
    embed.set_footer(text=f"Tickets Sold: {len(lottery.get('tickets', []))}")
    try:
        await msg.edit(embed=embed)
    except Exception:
        pass

async def finalize_lottery(guild: discord.Guild, message_id_str: str):
    """
    Called when lottery ends. Picks random ticket and announces winner.
    """
    lottery = lotteries.pop(message_id_str, None)
    if not lottery:
        return
    # pick winner
    tickets = lottery.get("tickets", [])
    seller = guild.get_member(int(lottery["seller_id"]))
    ticket_channel = guild.get_channel(int(lottery["ticket_channel_id"])) if lottery.get("ticket_channel_id") else None

    if tickets:
        winning_ticket = random.choice(tickets)
        winner_id = int(winning_ticket["buyer_id"])
        winner = guild.get_member(winner_id)
        # Announce in display channel
        display_chan = await find_or_create_channel(guild, LOTTERY_DISPLAY_CHANNEL, LOTTERY_CATEGORY_NAME)
        announce_text = (
            f"🎉 **Lottery Ended: {lottery['item']}**\n"
            f"Winner: <@{winner_id}> \n"
            f"Winning Ticket: `{winning_ticket['code']}`\n"
            f"Seller: <@{lottery['seller_id']}>"
        )
        await display_chan.send(announce_text)
        # Post in seller ticket channel
        if ticket_channel:
            await ticket_channel.send(announce_text)
        # DM buyer and seller friendly summary
        try:
            if winner:
                await winner.send(f"Congrats! You won the lottery for **{lottery['item']}** with ticket `{winning_ticket['code']}`. Please contact the seller <@{lottery['seller_id']}>.")
        except Exception:
            pass
        try:
            if seller:
                await seller.send(f"Your lottery for **{lottery['item']}** has ended. Winner: <@{winner_id}> with ticket `{winning_ticket['code']}`.")
        except Exception:
            pass
    else:
        # no tickets sold
        display_chan = await find_or_create_channel(guild, LOTTERY_DISPLAY_CHANNEL, LOTTERY_CATEGORY_NAME)
        await display_chan.send(f"⛔ Lottery ended for **{lottery['item']}** but no tickets were sold.")
        if ticket_channel:
            await ticket_channel.send(f"⛔ Your lottery for **{lottery['item']}** ended with no tickets sold.")
        try:
            if seller:
                await seller.send(f"Your lottery for **{lottery['item']}** ended with no tickets sold.")
        except Exception:
            pass

    await save_data()

async def lottery_timer_task(guild: discord.Guild, message_id_str: str, seconds: int):
    try:
        await asyncio.sleep(seconds)
        # finalize
        await finalize_lottery(guild, message_id_str)
    except asyncio.CancelledError:
        pass

# Load existing data on startup and resume timers
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    load_data()
    # Attempt to re-create timers for active lotteries
    for msg_id_str, lot in list(lotteries.items()):
        end_time = datetime.datetime.fromisoformat(lot["end_time"])
        seconds_left = (end_time - datetime.datetime.utcnow()).total_seconds()
        if seconds_left <= 0:
            # finalize immediately
            bot.loop.create_task(finalize_lottery(bot.guilds[0], msg_id_str))
        else:
            bot.loop.create_task(lottery_timer_task(bot.guilds[0], msg_id_str, int(seconds_left)))
    # start periodic save
    periodic_save.start()

# ----------------- Commands -----------------

@bot.command(name="lottery")
async def lottery_create(ctx: commands.Context, action: str, *, rest: str = None):
    """
    Create a lottery.
    Usage:
    !lottery create "Item Name" <duration> <ticket_price> [max_tickets]
    Example:
    !lottery create "Golden Sword" 1h 50 100
    - duration: 10m, 30s, 1h, etc.
    - ticket_price: number (informational; you can enforce currency later)
    - max_tickets: optional maximum number of tickets total
    NOTE: Attach an image to your command message if you want an item image saved.
    """
    if action.lower() != "create":
        return await ctx.send("Usage: `!lottery create \"Item Name\" <duration> <ticket_price> [max_tickets]` (attach an image optionally)")

    if not rest:
        return await ctx.send("Missing parameters. Usage: `!lottery create \"Item Name\" <duration> <ticket_price> [max_tickets]`")

    # split by spaces but keep quoted item name
    # Expecting: "Item Name" duration ticket_price [max_tickets]
    import shlex
    try:
        parts = shlex.split(rest)
    except Exception:
        return await ctx.send("Couldn't parse command. Make sure the item name is in quotes if it has spaces.")

    if len(parts) < 3:
        return await ctx.send("Missing parameters. Usage: `!lottery create \"Item Name\" <duration> <ticket_price> [max_tickets]`")

    item_name = parts[0]
    duration_raw = parts[1]
    ticket_price = parts[2]
    max_tickets = None
    if len(parts) >= 4:
        try:
            max_tickets = int(parts[3])
        except Exception:
            return await ctx.send("`max_tickets` must be a number.")

    # optional image from message attachments
    image_url = None
    if ctx.message.attachments:
        image_url = ctx.message.attachments[0].url

    # parse duration
    try:
        seconds = parse_duration(duration_raw)
    except Exception:
        return await ctx.send("Couldn't parse duration. Examples: 10m, 30s, 1h")

    end_time = (datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)).isoformat()
    # create ticket channel for seller
    ticket_chan = await create_ticket_channel(ctx.guild, ctx.author, item_name)

    # build lottery object
    lottery_obj = {
        "item": item_name,
        "seller_id": str(ctx.author.id),
        "ticket_price": ticket_price,
        "max_tickets": max_tickets,
        "image_url": image_url,
        "tickets": [],  # list of {code, buyer_id}
        "created_at": now_iso(),
        "end_time": end_time,
        "ticket_channel_id": str(ticket_chan.id)
    }

    # post to display channel
    msg_id = await post_lottery_display(ctx.guild, lottery_obj)
    lottery_obj["id"] = str(msg_id)
    lotteries[str(msg_id)] = lottery_obj
    await save_data()
    # start timer
    bot.loop.create_task(lottery_timer_task(ctx.guild, str(msg_id), seconds))
    await ctx.send(f"✅ Lottery created and posted in <#{(await find_or_create_channel(ctx.guild, LOTTERY_DISPLAY_CHANNEL, LOTTERY_CATEGORY_NAME)).id}>. Ticket channel: {ticket_chan.mention}")

@bot.command(name="buy")
async def buy_tickets(ctx: commands.Context, count: int):
    """
    Buy <count> tickets for the active lottery posted in this channel.
    Usage: !buy 3
    Buyer will receive a DM listing all ticket codes purchased.
    """
    if count <= 0:
        return await ctx.send("⚠ Ticket count must be 1 or greater.")

    # Identify which lottery this channel has a message for. We'll find the most recent lottery message in this channel that is active
    channel = ctx.channel
    display_chan = await find_or_create_channel(ctx.guild, LOTTERY_DISPLAY_CHANNEL, LOTTERY_CATEGORY_NAME)
    if channel.id != display_chan.id:
        return await ctx.send(f"⚠ Use this command in the lottery display channel: {display_chan.mention}")

    # find the latest lottery posted message in this display channel among active lotteries
    # We'll fetch last 50 messages and find the first one whose id is in lotteries
    found_msg = None
    try:
        async for message in channel.history(limit=50):
            if str(message.id) in lotteries:
                found_msg = message
                break
    except Exception:
        pass

    if not found_msg:
        return await ctx.send("⚠ No active lottery found in this channel to buy tickets for.")

    lottery = lotteries.get(str(found_msg.id))
    if not lottery:
        return await ctx.send("⚠ Lottery not found or already ended.")

    # check max_tickets constraint
    current_sold = len(lottery.get("tickets", []))
    max_tickets = lottery.get("max_tickets")
    if max_tickets is not None and (current_sold + count) > max_tickets:
        return await ctx.send(f"⚠ Not enough tickets remaining. Tickets left: {max_tickets - current_sold}")

    # generate ticket codes and append tickets
    new_codes = []
    for _ in range(count):
        code = gen_ticket_code()
        lottery["tickets"].append({"code": code, "buyer_id": str(ctx.author.id)})
        new_codes.append(code)

    await save_data()
    # update display message embed to show new tickets sold count
    await update_display_message(ctx.guild, int(found_msg.id))

    # send DM to buyer with ticket codes
    codes_text = "\n".join(f"- `{c}`" for c in new_codes)
    try:
        await ctx.author.send(f"You purchased {count} ticket(s) for **{lottery['item']}**.\nYour ticket codes:\n{codes_text}\nKeep these codes safe — you may need them for support.")
    except Exception:
        # If can't DM, inform in channel but avoid exposing codes publicly
        await ctx.send(f"✅ {ctx.author.mention} purchased {count} ticket(s). I couldn't DM you — please enable DMs from server members to receive your ticket codes.")
        # as fallback, create a short ephemeral-like message with masked codes (not ideal). We'll avoid exposing codes publicly.

    await ctx.send(f"✅ {ctx.author.mention} purchased {count} ticket(s). Check your DMs for ticket codes.")

@bot.command(name="mytickets")
async def my_tickets(ctx: commands.Context):
    """List ticket codes the user has across active lotteries (DM)."""
    user_id = str(ctx.author.id)
    found = []
    for lid, lot in lotteries.items():
        user_codes = [t["code"] for t in lot.get("tickets", []) if t["buyer_id"] == user_id]
        if user_codes:
            found.append((lot["item"], user_codes))
    if not found:
        return await ctx.send("You have no tickets in active lotteries.")

    out = []
    for item, codes in found:
        out.append(f"**{item}**:\n" + "\n".join(f"- `{c}`" for c in codes))
    try:
        await ctx.author.send("Your active lottery tickets:\n\n" + "\n\n".join(out))
        await ctx.send(f"✅ {ctx.author.mention} I sent you a DM with your tickets.")
    except Exception:
        await ctx.send("⚠ I couldn't DM you. Please enable DMs from server members to receive your tickets.")

@bot.command(name="endlottery")
@commands.has_permissions(manage_guild=True)
async def admin_end(ctx: commands.Context, message_id: int):
    """
    Admin command: force end a lottery by its display message ID.
    Usage: !endlottery <message_id>
    """
    msg_id_str = str(message_id)
    if msg_id_str not in lotteries:
        return await ctx.send("No active lottery with that message ID.")
    await finalize_lottery(ctx.guild, msg_id_str)
    await ctx.send("✅ Lottery finalized.")

@bot.command(name="lotterystatus")
async def lottery_status(ctx: commands.Context):
    """Show current active lotteries summary."""
    if not lotteries:
        return await ctx.send("No active lotteries right now.")
    lines = []
    for lid, lot in lotteries.items():
        end_ts = int(datetime.datetime.fromisoformat(lot["end_time"]).timestamp())
        lines.append(f"- **{lot['item']}** | Seller: <@{lot['seller_id']}> | Tickets: {len(lot.get('tickets',[]))} | Ends: <t:{end_ts}:R> | MsgID: {lid}")
    await ctx.send("📋 Active Lotteries:\n" + "\n".join(lines))

# Periodic save to disk
@tasks.loop(minutes=1)
async def periodic_save():
    await save_data()

# Run
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("ERROR: Set DISCORD_BOT_TOKEN environment variable.")
        exit(1)
    bot.run(TOKEN)
