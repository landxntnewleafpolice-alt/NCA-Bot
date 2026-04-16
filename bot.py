import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import time
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1453439287570858017

ROSTER_CHANNEL_ID = 1456514593407897651

GM_ROLE_ID = 1456448992236933181
AGM_ROLE_ID = 1456449453572624477
FREE_AGENT_ROLE_ID = 1456449872075952309

SALARY_CAP = 125

VALID_CONTRACTS = {"ROS", "1W"}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- DATA ----------------
rosters: dict = {}

# ---------------- SAFE LOAD/SAVE ----------------
def load_data():
    global rosters
    if os.path.exists("rosters.json"):
        with open("rosters.json", "r") as f:
            raw = json.load(f)

        # Migrate old entries that may be missing keys
        for team_id, players in raw.items():
            sanitized = []
            for p in players:
                sanitized.append({
                    "id": str(p.get("id", "")),
                    "salary": int(p.get("salary", 0)),
                    "contract": p.get("contract", "ROS"),
                    "signed_at": float(p.get("signed_at", time.time())),
                    "warned": bool(p.get("warned", False)),
                })
            raw[team_id] = sanitized

        rosters = raw

def save_data():
    with open("rosters.json", "w") as f:
        json.dump(rosters, f, indent=4)

# ---------------- PERMISSIONS ----------------
def is_gm(user: discord.Member) -> bool:
    return any(r.id == GM_ROLE_ID for r in user.roles)

def is_agm(user: discord.Member) -> bool:
    return any(r.id == AGM_ROLE_ID for r in user.roles)

def can_manage(user: discord.Member) -> bool:
    return is_gm(user) or is_agm(user)

# ---------------- ROSTER MESSAGE ----------------
def build_roster_embed() -> str:
    if not rosters:
        return "🏀 **LIVE ROSTERS** 🏀\n\n*No rosters yet.*"

    lines = ["🏀 **LIVE ROSTERS** 🏀\n"]

    for team_id, players in rosters.items():
        if not players:
            continue

        total = sum(p["salary"] for p in players)
        lines.append(f"<@&{team_id}>")

        for p in players:
            contract_label = p.get("contract", "ROS")
            lines.append(f"• <@{p['id']}> — {p['salary']}% | {contract_label}")

        cap_bar = "🟩" * min(total // 10, 12) + "🟥" * max((total - 100) // 10, 0)
        lines.append(f"**Cap:** {total}/{SALARY_CAP} {cap_bar}\n")

    full = "\n".join(lines)
    return full[:1990] if len(full) > 1990 else full

async def update_roster_message():
    channel = bot.get_channel(ROSTER_CHANNEL_ID)
    if not channel:
        print("⚠️ Roster channel not found.")
        return

    content = build_roster_embed()

    # Try to find and edit existing bot message; delete extras
    existing = None
    async for m in channel.history(limit=50):
        if m.author == bot.user:
            if existing is None:
                existing = m
            else:
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass

    if existing:
        try:
            await existing.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions.none()
            )
    else:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions.none()
        )

# ---------------- CONTRACT EXPIRY LOOP ----------------
@tasks.loop(minutes=1)
async def contract_loop():
    now = time.time()
    changed = False

    for team_id in list(rosters.keys()):
        kept = []

        for p in rosters[team_id]:
            if p.get("contract", "ROS") != "1W":
                kept.append(p)
                continue

            age = now - p.get("signed_at", now)

            # 6-day warning
            if age >= 6 * 86400 and not p.get("warned", False):
                try:
                    user = await bot.fetch_user(int(p["id"]))
                    await user.send("⚠️ Your 1-week contract has less than 24 hours left!")
                except Exception:
                    pass
                p["warned"] = True
                changed = True

            # 7-day expiry
            if age >= 7 * 86400:
                try:
                    user = await bot.fetch_user(int(p["id"]))
                    await user.send("❌ Your contract has expired. You are now a free agent.")
                except Exception:
                    pass

                # Re-assign FA role if we can find the guild
                guild = bot.get_guild(GUILD_ID)
                if guild:
                    member = guild.get_member(int(p["id"]))
                    fa_role = guild.get_role(FREE_AGENT_ROLE_ID)
                    team_role = guild.get_role(int(team_id))
                    if member:
                        try:
                            if team_role:
                                await member.remove_roles(team_role)
                            if fa_role:
                                await member.add_roles(fa_role)
                        except Exception:
                            pass

                changed = True
                continue  # Don't keep this player

            kept.append(p)

        rosters[team_id] = kept

    if changed:
        save_data()
        await update_roster_message()

@contract_loop.before_loop
async def before_contract_loop():
    await bot.wait_until_ready()

# ---------------- READY ----------------
@bot.event
async def on_ready():
    load_data()

    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    synced = await bot.tree.sync(guild=guild_obj)
    print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")

    contract_loop.start()
    print(f"🟢 BOT ONLINE: {bot.user}")

# ================================================================
# SLASH COMMANDS
# ================================================================

# ---------------- /sign ----------------
@bot.tree.command(
    name="sign",
    description="Sign a player to a team",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    team="The team role to sign the player to",
    player="The player to sign",
    salary="Salary percentage (1–125)",
    contract="Contract type: ROS or 1W"
)
async def sign(
    interaction: discord.Interaction,
    team: discord.Role,
    player: discord.Member,
    salary: int,
    contract: str
):
    await interaction.response.defer()

    if not can_manage(interaction.user):
        return await interaction.followup.send("❌ You must be a GM or AGM to use this command.")

    contract = contract.upper()
    if contract not in VALID_CONTRACTS:
        return await interaction.followup.send(f"❌ Invalid contract type. Use `ROS` or `1W`.")

    if salary <= 0 or salary > SALARY_CAP:
        return await interaction.followup.send(f"❌ Salary must be between 1 and {SALARY_CAP}.")

    team_key = str(team.id)
    rosters.setdefault(team_key, [])

    # Check if player is already on this team
    if any(p["id"] == str(player.id) for p in rosters[team_key]):
        return await interaction.followup.send(f"❌ {player.display_name} is already on that team.")

    total = sum(p["salary"] for p in rosters[team_key])
    if total + salary > SALARY_CAP:
        remaining = SALARY_CAP - total
        return await interaction.followup.send(
            f"❌ Salary cap exceeded. This team has **{remaining}%** remaining (cap: {SALARY_CAP}%)."
        )

    rosters[team_key].append({
        "id": str(player.id),
        "salary": salary,
        "contract": contract,
        "signed_at": time.time(),
        "warned": False,
    })

    try:
        await player.add_roles(team)
    except discord.Forbidden:
        return await interaction.followup.send("❌ Bot lacks permission to assign that role.")

    fa_role = interaction.guild.get_role(FREE_AGENT_ROLE_ID)
    if fa_role and fa_role in player.roles:
        try:
            await player.remove_roles(fa_role)
        except discord.Forbidden:
            pass

    save_data()
    await update_roster_message()

    await interaction.followup.send(
        f"✅ Signed {player.mention} to {team.mention} | **{salary}%** | `{contract}`",
        allowed_mentions=discord.AllowedMentions.none()
    )

# ---------------- /drop ----------------
@bot.tree.command(
    name="drop",
    description="Drop a player from a team",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    team="The team to drop the player from",
    player="The player to drop"
)
async def drop(
    interaction: discord.Interaction,
    team: discord.Role,
    player: discord.Member
):
    await interaction.response.defer()

    if not can_manage(interaction.user):
        return await interaction.followup.send("❌ You must be a GM or AGM to use this command.")

    team_key = str(team.id)

    if team_key not in rosters or not any(p["id"] == str(player.id) for p in rosters[team_key]):
        return await interaction.followup.send(f"❌ {player.display_name} is not on that team's roster.")

    rosters[team_key] = [p for p in rosters[team_key] if p["id"] != str(player.id)]

    try:
        await player.remove_roles(team)
    except discord.Forbidden:
        pass

    fa_role = interaction.guild.get_role(FREE_AGENT_ROLE_ID)
    if fa_role:
        try:
            await player.add_roles(fa_role)
        except discord.Forbidden:
            pass

    save_data()
    await update_roster_message()

    await interaction.followup.send(
        f"❌ Dropped {player.mention} from {team.mention}. They are now a free agent.",
        allowed_mentions=discord.AllowedMentions.none()
    )

# ---------------- /trade ----------------
@bot.tree.command(
    name="trade",
    description="Trade two players between teams",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    team1="First team",
    player1="Player from team 1",
    team2="Second team",
    player2="Player from team 2"
)
async def trade(
    interaction: discord.Interaction,
    team1: discord.Role,
    player1: discord.Member,
    team2: discord.Role,
    player2: discord.Member
):
    await interaction.response.defer()

    if not can_manage(interaction.user):
        return await interaction.followup.send("❌ You must be a GM or AGM to use this command.")

    t1, t2 = str(team1.id), str(team2.id)

    if t1 == t2:
        return await interaction.followup.send("❌ Cannot trade between the same team.")

    rosters.setdefault(t1, [])
    rosters.setdefault(t2, [])

    a = next((x for x in rosters[t1] if x["id"] == str(player1.id)), None)
    b = next((x for x in rosters[t2] if x["id"] == str(player2.id)), None)

    if a is None:
        return await interaction.followup.send(f"❌ {player1.display_name} not found on {team1.name}'s roster.")
    if b is None:
        return await interaction.followup.send(f"❌ {player2.display_name} not found on {team2.name}'s roster.")

    # Cap check after swap
    t1_total = sum(p["salary"] for p in rosters[t1]) - a["salary"] + b["salary"]
    t2_total = sum(p["salary"] for p in rosters[t2]) - b["salary"] + a["salary"]

    if t1_total > SALARY_CAP:
        return await interaction.followup.send(
            f"❌ Trade would put {team1.name} over cap ({t1_total}/{SALARY_CAP})."
        )
    if t2_total > SALARY_CAP:
        return await interaction.followup.send(
            f"❌ Trade would put {team2.name} over cap ({t2_total}/{SALARY_CAP})."
        )

    # Swap in rosters
    rosters[t1] = [p for p in rosters[t1] if p["id"] != str(player1.id)]
    rosters[t2] = [p for p in rosters[t2] if p["id"] != str(player2.id)]
    rosters[t1].append(b)
    rosters[t2].append(a)

    # Swap roles
    try:
        await player1.remove_roles(team1)
        await player1.add_roles(team2)
        await player2.remove_roles(team2)
        await player2.add_roles(team1)
    except discord.Forbidden:
        return await interaction.followup.send("❌ Bot lacks permission to update roles.")

    save_data()
    await update_roster_message()

    await interaction.followup.send(
        f"🔁 Trade complete!\n{player1.mention} → {team2.mention}\n{player2.mention} → {team1.mention}",
        allowed_mentions=discord.AllowedMentions.none()
    )

# ---------------- /giveagm ----------------
@bot.tree.command(
    name="giveagm",
    description="Give a user the AGM role and assign them to a team (GM only)",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    user="The user to promote to AGM",
    team="The team to assign the AGM to"
)
async def giveagm(interaction: discord.Interaction, user: discord.Member, team: discord.Role):
    await interaction.response.defer()

    if not is_gm(interaction.user):
        return await interaction.followup.send("❌ Only GMs can assign the AGM role.")

    agm_role = interaction.guild.get_role(AGM_ROLE_ID)
    if not agm_role:
        return await interaction.followup.send("❌ AGM role not found in this server.")

    roles_to_add = [agm_role, team]
    roles_to_remove = []

    fa_role = interaction.guild.get_role(FREE_AGENT_ROLE_ID)
    if fa_role and fa_role in user.roles:
        roles_to_remove.append(fa_role)

    try:
        await user.add_roles(*roles_to_add)
        if roles_to_remove:
            await user.remove_roles(*roles_to_remove)
    except discord.Forbidden:
        return await interaction.followup.send("❌ Bot lacks permission to update roles.")

    await interaction.followup.send(
        f"✅ {user.mention} is now AGM of {team.mention}.",
        allowed_mentions=discord.AllowedMentions.none()
    )

# ---------------- /removeagm ----------------
@bot.tree.command(
    name="removeagm",
    description="Remove the AGM role from a user (GM only)",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    user="The user to demote from AGM",
    team="The team to remove them from"
)
async def removeagm(interaction: discord.Interaction, user: discord.Member, team: discord.Role):
    await interaction.response.defer()

    if not is_gm(interaction.user):
        return await interaction.followup.send("❌ Only GMs can remove the AGM role.")

    agm_role = interaction.guild.get_role(AGM_ROLE_ID)
    if not agm_role:
        return await interaction.followup.send("❌ AGM role not found in this server.")

    if agm_role not in user.roles:
        return await interaction.followup.send(f"⚠️ {user.display_name} doesn't have the AGM role.")

    fa_role = interaction.guild.get_role(FREE_AGENT_ROLE_ID)

    try:
        await user.remove_roles(agm_role, team)
        if fa_role:
            await user.add_roles(fa_role)
    except discord.Forbidden:
        return await interaction.followup.send("❌ Bot lacks permission to update roles.")

    await interaction.followup.send(
        f"✅ Removed AGM from {user.mention} and returned them to free agency.",
        allowed_mentions=discord.AllowedMentions.none()
    )

# ---------------- /roster ----------------
@bot.tree.command(
    name="roster",
    description="Force-refresh the roster message",
    guild=discord.Object(id=GUILD_ID)
)
async def roster_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    if not can_manage(interaction.user):
        return await interaction.followup.send("❌ No permission.")

    await update_roster_message()
    await interaction.followup.send("✅ Roster refreshed.")

# ---------------- /cap ----------------
@bot.tree.command(
    name="cap",
    description="Check a team's current salary cap usage",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(team="The team to check")
async def cap(interaction: discord.Interaction, team: discord.Role):
    await interaction.response.defer()

    team_key = str(team.id)
    players = rosters.get(team_key, [])
    total = sum(p["salary"] for p in players)
    remaining = SALARY_CAP - total

    lines = [f"💰 **{team.name} Cap Sheet**\n"]
    for p in players:
        lines.append(f"• <@{p['id']}> — {p['salary']}% `{p.get('contract','ROS')}`")
    lines.append(f"\n**Total:** {total}% / {SALARY_CAP}%")
    lines.append(f"**Remaining:** {remaining}%")

    await interaction.followup.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none()
    )

# ---------------- RUN ----------------
bot.run(TOKEN)