import os
import io
import time
import random
import sqlite3
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands

from PIL import Image, ImageDraw, ImageFont, ImageFilter


# =========================
# CONFIG
# =========================
TOKEN = os.getenv("DISCORD_TOKEN", "MET_TON_TOKEN_ICI")

DB_PATH = "coinsbot.sqlite3"

CURRENCY_NAME = "Coinsbot Coins"
CURRENCY_EMOJI = "🪙"

START_BALANCE = 1000

# cooldowns (seconds)
CD_DAILY = 6 * 3600
CD_COLLECT = 15 * 60
CD_GIFT = 20 * 60  # 20 min

# rewards
DAILY_REWARD = (1500, 3000)
COLLECT_REWARD = (200, 900)
GIFT_REWARD = (50, 350)

# level bonus
LEVEL_BONUS_EVERY = 5
LEVEL_BONUS_AMOUNT = 5000

# clans
CLAN_MAX_MODS = 2

# profile image
PROFILE_WIDTH = 920
PROFILE_HEIGHT = 340

# Mets ton fond anime ici (même dossier que ce script)
PROFILE_BG_PATH = "coinsbot_bg.png"

# Optionnel : chemin vers une police TTF (sinon fallback)
FONT_PATH = None

# Voile noir (plus bas = fond plus visible)
PROFILE_OVERLAY_ALPHA = 60

# Mines total multipliers (after n safes, cashout = bet * mult)
MINES_MULTS = [0.5, 0.9, 1.2, 1.7, 2.2, 2.7, 3.2, 4]


# =========================
# DB LAYER + MIGRATIONS
# =========================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def db_init():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            xp INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            draws INTEGER NOT NULL DEFAULT 0,
            steals INTEGER NOT NULL DEFAULT 0,
            cf_streak INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            next_ts INTEGER NOT NULL,
            PRIMARY KEY (user_id, key)
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            delta INTEGER NOT NULL,
            ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS clans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL,
            bank INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS clan_members (
            clan_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'member', -- owner/mod/member
            joined_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (clan_id, user_id)
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS clan_invites (
            clan_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            invited_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (clan_id, user_id)
        )
        """)

        # Migrations pour users (colonnes manquantes d'une vieille DB)
        if not _column_exists(conn, "users", "xp"):
            conn.execute("ALTER TABLE users ADD COLUMN xp INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "level"):
            conn.execute("ALTER TABLE users ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
        if not _column_exists(conn, "users", "draws"):
            conn.execute("ALTER TABLE users ADD COLUMN draws INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "steals"):
            conn.execute("ALTER TABLE users ADD COLUMN steals INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "cf_streak"):
            conn.execute("ALTER TABLE users ADD COLUMN cf_streak INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "created_at"):
            conn.execute("ALTER TABLE users ADD COLUMN created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))")

        # migration si tu avais une vieille DB sans bank
        if not _column_exists(conn, "clans", "bank"):
            conn.execute("ALTER TABLE clans ADD COLUMN bank INTEGER NOT NULL DEFAULT 0")

        conn.commit()


def ensure_user(user_id: int):
    with db_connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, balance, xp, level) VALUES(?,?,?,?)",
                (user_id, START_BALANCE, 0, 1),
            )
            conn.commit()


def get_user(user_id: int) -> sqlite3.Row:
    ensure_user(user_id)
    with db_connect() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def add_balance(user_id: int, delta: int, action: str = "unknown") -> int:
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
        conn.execute("INSERT INTO logs(user_id, action, delta) VALUES(?,?,?)", (user_id, action, delta))
        conn.commit()
        bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]
        return int(bal)


def set_balance(user_id: int, amount: int):
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
        conn.commit()


def add_draws(user_id: int, n: int = 1):
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute("UPDATE users SET draws = draws + ? WHERE user_id=?", (n, user_id))
        conn.commit()


def get_top(limit: int = 10) -> List[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_cd(user_id: int, key: str) -> int:
    ensure_user(user_id)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT next_ts FROM cooldowns WHERE user_id=? AND key=?",
            (user_id, key),
        ).fetchone()
        return int(row["next_ts"]) if row else 0


def set_cd(user_id: int, key: str, next_ts: int):
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO cooldowns(user_id, key, next_ts)
            VALUES(?,?,?)
            ON CONFLICT(user_id, key) DO UPDATE SET next_ts=excluded.next_ts
        """, (user_id, key, next_ts))
        conn.commit()


# =========================
# CLANS HELPERS
# =========================
def user_clan_id(user_id: int) -> Optional[int]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT clan_id FROM clan_members WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        return int(row["clan_id"]) if row else None


def user_clan_role(user_id: int) -> Optional[str]:
    with db_connect() as conn:
        row = conn.execute("SELECT role FROM clan_members WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
        return row["role"] if row else None


def clan_info_by_id(clan_id: int):
    with db_connect() as conn:
        clan = conn.execute("SELECT * FROM clans WHERE id=?", (clan_id,)).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM clan_members WHERE clan_id=?",
            (clan_id,),
        ).fetchone()["c"]
        mods = conn.execute(
            "SELECT COUNT(*) AS c FROM clan_members WHERE clan_id=? AND role='mod'",
            (clan_id,),
        ).fetchone()["c"]
        return clan, int(count), int(mods)


def clan_name_for_user(user_id: int) -> str:
    cid = user_clan_id(user_id)
    if not cid:
        return "Aucun clan"
    with db_connect() as conn:
        row = conn.execute("SELECT name FROM clans WHERE id=?", (cid,)).fetchone()
        return row["name"] if row else "Aucun clan"


def is_clan_owner(clan_id: int, user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT role FROM clan_members WHERE clan_id=? AND user_id=?",
            (clan_id, user_id),
        ).fetchone()
        return bool(row and row["role"] == "owner")


def is_clan_mod_or_owner(clan_id: int, user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT role FROM clan_members WHERE clan_id=? AND user_id=?",
            (clan_id, user_id),
        ).fetchone()
        return bool(row and row["role"] in ("owner", "mod"))


def clan_bank_get(clan_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute("SELECT bank FROM clans WHERE id=?", (clan_id,)).fetchone()
        return int(row["bank"]) if row else 0


def clan_bank_add(clan_id: int, delta: int):
    with db_connect() as conn:
        conn.execute("UPDATE clans SET bank = bank + ? WHERE id=?", (delta, clan_id))
        conn.commit()


def top_clans(limit: int = 10) -> List[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT name, bank FROM clans ORDER BY bank DESC LIMIT ?",
            (limit,),
        ).fetchall()


# CF Helpers
def get_cf_streak(user_id: int) -> int:
    ensure_user(user_id)
    with db_connect() as conn:
        row = conn.execute("SELECT cf_streak FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["cf_streak"]) if row else 0


def set_cf_streak(user_id: int, streak: int):
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute("UPDATE users SET cf_streak = ? WHERE user_id=?", (streak, user_id))
        conn.commit()


# =========================
# XP / LEVELING
# =========================
def add_xp(user_id: int, xp_gain: int) -> Tuple[int, int, bool, int]:
    """
    returns: (level, xp, leveled, bonus_total)
    """
    ensure_user(user_id)
    with db_connect() as conn:
        u = conn.execute("SELECT xp, level FROM users WHERE user_id=?", (user_id,)).fetchone()
        xp = int(u["xp"]) + xp_gain
        level = int(u["level"])

        def need_for_level(lv: int) -> int:
            return 200 + (lv - 1) * 150

        leveled = False
        bonus_total = 0

        while xp >= need_for_level(level):
            xp -= need_for_level(level)
            level += 1
            leveled = True
            if level % LEVEL_BONUS_EVERY == 0:
                bonus_total += LEVEL_BONUS_AMOUNT

        conn.execute("UPDATE users SET xp=?, level=? WHERE user_id=?", (xp, level, user_id))

        if bonus_total > 0:
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (bonus_total, user_id))
            conn.execute("INSERT INTO logs(user_id, action, delta) VALUES(?,?,?)", (user_id, "level_bonus", bonus_total))

        conn.commit()
        return level, xp, leveled, bonus_total


# =========================
# UTILS
# =========================
def now_ts() -> int:
    return int(time.time())


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_money(n: int) -> str:
    return f"{fmt_int(n)} {CURRENCY_NAME} {CURRENCY_EMOJI}"


def cd_left(user_id: int, key: str) -> int:
    return max(0, get_cd(user_id, key) - now_ts())


def human_time(seconds: int) -> str:
    if seconds <= 0:
        return "Disponible"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return " ".join(parts)


# Roulette colors
RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

def roulette_spin() -> Tuple[int, str]:
    n = random.randint(0, 36)
    if n == 0:
        return n, "vert"
    return n, ("rouge" if n in RED_NUMBERS else "noir")


# =========================
# BLACKJACK (simple)
# =========================
def bj_card() -> int:
    return random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11])

def bj_score(cards: List[int]) -> int:
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def bj_pretty(cards: List[int]) -> str:
    return " ".join("A" if c == 11 else str(c) for c in cards)

@dataclass
class BJGame:
    bet: int
    player: List[int]
    dealer: List[int]
    finished: bool = False

BJ_SESSIONS: Dict[int, BJGame] = {}


# =========================
# MINES GAME (total mults, bet risked at start)
# =========================
@dataclass
class MinesGame:
    bet: int
    mines_pos: List[int]  # Positions des mines (1-9)
    revealed: set = None
    safe_count: int = 0
    num_mines: int = 1
    num_safes: int = 8
    finished: bool = False

    def __post_init__(self):
        if self.revealed is None:
            self.revealed = set()


class MinesView(discord.ui.View):
    def __init__(self, user_id: int, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce jeu de mines n'est pas le tien.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        game = MINES_SESSIONS.get(self.user_id)
        if game and not game.finished:
            MINES_SESSIONS.pop(self.user_id, None)
            # Bet already lost on timeout

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, row=0)
    async def btn1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, row=0)
    async def btn2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, row=0)
    async def btn3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, row=1)
    async def btn4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary, row=1)
    async def btn5(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 5)

    @discord.ui.button(label="6", style=discord.ButtonStyle.secondary, row=1)
    async def btn6(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 6)

    @discord.ui.button(label="7", style=discord.ButtonStyle.secondary, row=2)
    async def btn7(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 7)

    @discord.ui.button(label="8", style=discord.ButtonStyle.secondary, row=2)
    async def btn8(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 8)

    @discord.ui.button(label="9", style=discord.ButtonStyle.secondary, row=2)
    async def btn9(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._reveal(interaction, 9)

    @discord.ui.button(label="Réclamer", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = MINES_SESSIONS.get(self.user_id)
        if not game or game.finished:
            return await interaction.response.send_message("Partie terminée.", ephemeral=True)

        if game.safe_count == 0:
            await interaction.response.send_message("❌ Révèle au moins une safe pour réclamer.", ephemeral=True)
            return

        idx = min(game.safe_count - 1, len(MINES_MULTS) - 1)
        mult = MINES_MULTS[idx]
        cashout = int(game.bet * mult)

        game.finished = True
        MINES_SESSIONS.pop(self.user_id, None)

        new_bal = add_balance(self.user_id, cashout, action="mines_claim")
        _, _, _, bonus = add_xp(self.user_id, random.randint(5, 15))
        if bonus > 0:
            new_bal = int(get_user(self.user_id)["balance"])

        e = base_embed("Minesweeper - Réclamé", user=interaction.user)
        e.add_field(name="Safe trouvées", value=f"{game.safe_count}/8", inline=True)
        e.add_field(name="Multiplicateur", value=f"x{mult:.1f}", inline=True)
        e.add_field(name="Réclamé", value=f"**+{fmt_int(cashout)}** {CURRENCY_EMOJI}", inline=False)
        grid = self._render_grid(game)
        e.add_field(name="Grille", value=grid, inline=False)
        if bonus > 0:
            e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
        e.add_field(name="Solde", value=fmt_money(new_bal), inline=False)
        self.stop()
        await interaction.response.edit_message(embed=e, view=None)

    async def _reveal(self, interaction: discord.Interaction, pos: int):
        game = MINES_SESSIONS.get(self.user_id)
        if not game or game.finished or pos in game.revealed:
            return await interaction.response.send_message("Case déjà révélée ou partie finie.", ephemeral=True)

        game.revealed.add(pos)
        if pos in game.mines_pos:
            # Mine ! Bet already lost at start
            game.finished = True
            MINES_SESSIONS.pop(self.user_id, None)
            add_draws(self.user_id, 1)
            add_xp(self.user_id, random.randint(1, 5))  # Petit XP
            e = base_embed("Minesweeper", user=interaction.user)
            e.add_field(name="💥 Mine touchée !", value=f"Perdu ta mise de **{fmt_int(game.bet)}** {CURRENCY_EMOJI}", inline=False)
            grid = self._render_grid(game)
            e.add_field(name="Grille", value=grid, inline=False)
            new_bal = int(get_user(self.user_id)["balance"])
            e.add_field(name="Solde", value=fmt_money(new_bal), inline=False)
            self.stop()
            await interaction.response.edit_message(embed=e, view=None)
        else:
            # Safe
            game.safe_count += 1
            idx = min(game.safe_count - 1, len(MINES_MULTS) - 1)
            mult = MINES_MULTS[idx]
            cashout = int(game.bet * mult)
            e = base_embed("Minesweeper", user=interaction.user)
            grid = self._render_grid(game)
            e.add_field(name="Grille", value=grid, inline=False)
            e.add_field(name="Safe trouvées", value=f"{game.safe_count}/8", inline=True)
            e.add_field(name="Cashout potentiel", value=f"x{mult:.1f} ({fmt_int(cashout)} {CURRENCY_EMOJI})", inline=True)
            if game.safe_count == game.num_safes:  # Toutes safe révélées
                game.finished = True
                MINES_SESSIONS.pop(self.user_id, None)
                mult = 3.5
                cashout = int(game.bet * mult)
                new_bal = add_balance(self.user_id, cashout, action="mines_win")
                add_draws(self.user_id, 1)
                _, _, _, bonus = add_xp(self.user_id, random.randint(10, 20))
                if bonus > 0:
                    new_bal = int(get_user(self.user_id)["balance"])
                e.add_field(name="🎉 Victoire totale !", value=f"**+{fmt_int(cashout)}** {CURRENCY_EMOJI} (x3.5)", inline=False)
                if bonus > 0:
                    e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
                e.add_field(name="Solde", value=fmt_money(new_bal), inline=False)
                self.stop()
            await interaction.response.edit_message(embed=e, view=self)

    def _render_grid(self, game: MinesGame) -> str:
        grid = [["?" for _ in range(3)] for _ in range(3)]
        for pos in game.revealed:
            r, c = divmod(pos - 1, 3)
            if pos in game.mines_pos:
                grid[r][c] = "💀"
            else:
                grid[r][c] = "💎"
        if game.finished:
            for pos in game.mines_pos:
                if pos not in game.revealed:
                    r, c = divmod(pos - 1, 3)
                    grid[r][c] = "💀"
        return "\n".join(" | ".join(row) for row in grid)


MINES_SESSIONS: Dict[int, MinesGame] = {}


# =========================
# DISCORD BOT
# =========================
def base_embed(title: str, description: str = "", user: Optional[discord.abc.User] = None) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=0x3498db)
    if user:
        e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    e.set_footer(text="Coinsbot • Casino")
    return e


class CoinsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix=".", intents=intents)

    async def setup_hook(self):
        db_init()
        await self.tree.sync()


bot = CoinsBot()


@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user} (Coinsbot)")


# =========================
# /help
# =========================
@bot.tree.command(name="help", description="Affiche toutes les commandes Coinsbot")
async def help_cmd(interaction: discord.Interaction):
    e = base_embed("Coinsbot • Aide", user=interaction.user)

    e.add_field(
        name="💰 Économie",
        value=(
            "• `/bal [membre]` → voir le solde\n"
            "• `/daily` → récompense (cooldown)\n"
            "• `/collect` → collecte (cooldown)\n"
            "• `/gift` → cadeau (20 min, max 350)\n"
            "• `/give @membre montant` → donner des coins\n"
            "• `/top` → classement joueurs\n"
            "• `/topclan` → classement clans (banque)"
        ),
        inline=False
    )

    e.add_field(
        name="🎲 Casino",
        value=(
            "• `/roulette mise choix` → choix: `noir`, `rouge` ou `0-36`\n"
            "• `/slots mise` → machine à sous\n"
            "• `/bj mise` → blackjack (boutons Hit/Stand)\n"
            "• `/nombre mise choix` → devine 1-10 (x4)\n"
            "• `/cf mise` → coin flip twist (x1.5)\n"
            "• `/rps mise choix` → pierre/feuille/ciseaux (x2)\n"
            "• `/mines mise` → minesweeper 3x3 (1 mine, boutons, cashout max x3.5)"
        ),
        inline=False
    )

    e.add_field(
        name="🖼️ Profil",
        value=(
            "• `/profil [membre]` → carte profil\n\n"
        ),
        inline=False
    )

    e.add_field(
        name="🏰 Clans",
        value=(
            "• `/clan create nom` → créer un clan\n"
            "• `/clan invite @membre` → inviter (owner)\n"
            "• `/clan accept` → accepter\n"
            "• `/clan info` → infos clan\n"
            "• `/clan deposit montant` → déposer (tout membre)\n"
            "• `/clan withdraw montant` → retirer (owner/mod)\n"
            "• `/clan setmod @membre` → mod (owner, max 2)\n"
            "• `/clan unmod @membre` → retirer mod (owner)\n"
            "• `/clan transfer @membre` → transférer (owner)\n"
            "• `/clan rename nom` → renommer (owner)\n"
            "• `/clan delete` → supprimer (owner)\n"
            "• `/clan leave` → quitter (pas owner)"
        ),
        inline=False
    )

    await interaction.response.send_message(embed=e, ephemeral=True)


# =========================
# COMMANDS (SLASH) - CORE
# =========================
@bot.tree.command(name="bal", description="Voir ton solde")
async def bal(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    membre = membre or interaction.user
    u = get_user(membre.id)
    clan = clan_name_for_user(membre.id)
    role = user_clan_role(membre.id) or "-"

    e = base_embed("Portefeuille", user=membre)
    e.add_field(name="Solde", value=fmt_money(int(u["balance"])), inline=False)
    e.add_field(name="Niveau", value=f"LVL {int(u['level'])} • XP {fmt_int(int(u['xp']))}", inline=True)
    e.add_field(name="Tirages", value=str(int(u["draws"])), inline=True)
    e.add_field(name="Clan", value=f"{clan} ({role})" if clan != "Aucun clan" else clan, inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="top", description="Classement des plus riches (joueurs)")
@app_commands.describe(limit="Nombre de personnes (max 20)")
async def top(interaction: discord.Interaction, limit: int = 10):
    limit = max(3, min(20, limit))
    rows = get_top(limit)

    lines = []
    for i, r in enumerate(rows, start=1):
        uid = int(r["user_id"])
        bal_ = int(r["balance"])

        name = None
        if interaction.guild:
            m = interaction.guild.get_member(uid)
            if m:
                name = m.display_name
            else:
                try:
                    m2 = await interaction.guild.fetch_member(uid)
                    name = m2.display_name
                except:
                    name = None

        if not name:
            try:
                u = await bot.fetch_user(uid)
                name = u.name
            except:
                name = f"User {uid}"

        lines.append(f"**{i})** {name}\n`{fmt_int(bal_)} {CURRENCY_NAME}` {CURRENCY_EMOJI}")

    e = base_embed("Classement des Coinsbot Coins", "\n\n".join(lines))
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="topclan", description="Classement des clans par banque")
@app_commands.describe(limit="Nombre de clans (max 20)")
async def topclan(interaction: discord.Interaction, limit: int = 10):
    limit = max(3, min(20, limit))
    rows = top_clans(limit)
    if not rows:
        return await interaction.response.send_message("Aucun clan.", ephemeral=True)

    lines = []
    for i, r in enumerate(rows, start=1):
        lines.append(f"**{i})** {r['name']}\n`{fmt_int(int(r['bank']))} {CURRENCY_NAME}` {CURRENCY_EMOJI}")

    e = base_embed("Top Clans (banque)", "\n\n".join(lines))
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="timer", description="Afficher les cooldowns")
async def timer(interaction: discord.Interaction):
    u = interaction.user
    e = base_embed("Temps restant des commandes", user=u)
    e.add_field(
        name="Coinsbot",
        value=(
            f"• Daily : **{human_time(cd_left(u.id, 'daily'))}**\n"
            f"• Collect : **{human_time(cd_left(u.id, 'collect'))}**\n"
            f"• Gift : **{human_time(cd_left(u.id, 'gift'))}**\n"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="daily", description="Récupère ta récompense quotidienne")
async def daily(interaction: discord.Interaction):
    u = interaction.user
    left = cd_left(u.id, "daily")
    if left > 0:
        return await interaction.response.send_message(
            embed=base_embed("Daily", f"⏳ Pas dispo. Reviens dans **{human_time(left)}**.", user=u),
            ephemeral=True,
        )

    reward = random.randint(*DAILY_REWARD)
    new_bal = add_balance(u.id, reward, action="daily")
    _, _, _, bonus = add_xp(u.id, random.randint(15, 35))
    set_cd(u.id, "daily", now_ts() + CD_DAILY)

    e = base_embed("Daily", user=u)
    e.add_field(name="Récompense", value=f"+{fmt_int(reward)} {CURRENCY_NAME} {CURRENCY_EMOJI}", inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI} (tous les 5 niveaux)", inline=False)
        new_bal = int(get_user(u.id)["balance"])
    e.add_field(name="Nouveau solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="collect", description="Collecte des coins (cooldown)")
async def collect(interaction: discord.Interaction):
    u = interaction.user
    left = cd_left(u.id, "collect")
    if left > 0:
        return await interaction.response.send_message(
            embed=base_embed("Collect", f"⏳ Pas dispo. Reviens dans **{human_time(left)}**.", user=u),
            ephemeral=True,
        )

    reward = random.randint(*COLLECT_REWARD)
    new_bal = add_balance(u.id, reward, action="collect")
    _, _, _, bonus = add_xp(u.id, random.randint(5, 15))
    set_cd(u.id, "collect", now_ts() + CD_COLLECT)

    e = base_embed("Collecte de Coinsbot Coins", user=u)
    e.add_field(name="Gains", value=f"Tu as collecté **{fmt_int(reward)}** {CURRENCY_EMOJI}", inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
        new_bal = int(get_user(u.id)["balance"])
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="gift", description="Cadeau aléatoire (cooldown 20 min, max 350)")
async def gift(interaction: discord.Interaction):
    u = interaction.user
    left = cd_left(u.id, "gift")
    if left > 0:
        return await interaction.response.send_message(
            embed=base_embed("Cadeau", f"⏳ Pas dispo. Reviens dans **{human_time(left)}**.", user=u),
            ephemeral=True,
        )

    set_cd(u.id, "gift", now_ts() + CD_GIFT)

    reward = random.randint(*GIFT_REWARD)
    new_bal = add_balance(u.id, reward, action="gift")
    _, _, _, bonus = add_xp(u.id, random.randint(8, 16))
    if bonus > 0:
        new_bal = int(get_user(u.id)["balance"])

    e = base_embed("Cadeau", user=u)
    e.add_field(name="Résultat", value=f"Vous avez gagné **{fmt_int(reward)}** {CURRENCY_EMOJI}", inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


# Nouvelle commande /give
@bot.tree.command(name="give", description="Donne des coins à un membre")
@app_commands.describe(membre="Le membre à qui donner", montant="Montant à donner")
async def give(interaction: discord.Interaction, membre: discord.Member, montant: int):
    u = interaction.user
    if membre.id == u.id:
        return await interaction.response.send_message("❌ Tu ne peux pas te donner à toi-même.", ephemeral=True)
    if montant <= 0:
        return await interaction.response.send_message("❌ Montant invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if montant > bal:
        return await interaction.response.send_message("❌ Pas assez de coins.", ephemeral=True)

    # Transfert direct
    set_balance(u.id, bal - montant)
    new_bal_receiver = add_balance(membre.id, montant, action="gift_received")
    add_balance(u.id, 0, action="give")  # Log pour sender

    e = base_embed("Don de Coins", user=u)
    e.add_field(name="Donné", value=f"{fmt_int(montant)} {CURRENCY_EMOJI} à {membre.mention}", inline=False)
    e.add_field(name="Ton solde", value=fmt_money(bal - montant), inline=True)
    e.add_field(name="Solde de {membre.display_name}", value=fmt_money(new_bal_receiver), inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="roulette", description="Joue à la roulette (noir/rouge ou numéro)")
@app_commands.describe(mise="Montant", choix="noir/rouge/0-36")
async def roulette(interaction: discord.Interaction, mise: int, choix: str):
    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        return await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        return await interaction.response.send_message("❌ T'as pas assez de coins.", ephemeral=True)

    choix = choix.strip().lower()
    n, color = roulette_spin()
    add_draws(u.id, 1)

    delta = -mise
    info = ""

    if choix in ("noir", "rouge"):
        if n != 0 and choix == color:
            delta = +mise
            info = f"Félicitations ! Vous avez gagné **{fmt_int(mise)}** {CURRENCY_EMOJI} (x2)"
        else:
            info = f"Perdu. Vous avez perdu **{fmt_int(mise)}** {CURRENCY_EMOJI}"
    else:
        try:
            picked = int(choix)
        except ValueError:
            return await interaction.response.send_message("❌ Choix invalide (noir/rouge/0-36).", ephemeral=True)
        if not (0 <= picked <= 36):
            return await interaction.response.send_message("❌ Numéro invalide (0-36).", ephemeral=True)

        if picked == n:
            delta = 35 * mise
            info = f"🎉 JACKPOT ! Vous avez gagné **{fmt_int(35*mise)}** {CURRENCY_EMOJI} (x36)"
        else:
            info = f"Perdu. Vous avez perdu **{fmt_int(mise)}** {CURRENCY_EMOJI}"

    new_bal = add_balance(u.id, delta, action="roulette")
    _, _, _, bonus = add_xp(u.id, random.randint(6, 18))
    if bonus > 0:
        new_bal = int(get_user(u.id)["balance"])

    e = base_embed("La roue a fini de tourner", user=u)
    e.add_field(name="Choix", value=str(choix), inline=True)
    e.add_field(name="Numéro gagnant", value=f"{color} {n}", inline=True)
    e.add_field(name="Résultat", value=info, inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="slots", description="Machine à sous")
@app_commands.describe(mise="Montant")
async def slots(interaction: discord.Interaction, mise: int):
    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        return await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        return await interaction.response.send_message("❌ T'as pas assez de coins.", ephemeral=True)

    symbols = ["🍒", "🍋", "🔔", "⭐", "💎", "7️⃣"]
    roll = [random.choice(symbols) for _ in range(3)]
    add_draws(u.id, 1)

    payout_mult = 0
    if roll[0] == roll[1] == roll[2]:
        mult_map = {"7️⃣": 10, "💎": 8, "⭐": 6, "🔔": 5, "🍒": 4, "🍋": 3}
        payout_mult = mult_map.get(roll[0], 3)
    elif roll[0] == roll[1] or roll[1] == roll[2] or roll[0] == roll[2]:
        payout_mult = 2

    if payout_mult == 0:
        new_bal = add_balance(u.id, -mise, action="slots")
        res = f"Perdu **-{fmt_int(mise)}** {CURRENCY_EMOJI}"
    else:
        net = mise * payout_mult - mise
        new_bal = add_balance(u.id, net, action="slots")
        res = f"Gagné ! **x{payout_mult}** → **+{fmt_int(net)}** {CURRENCY_EMOJI}"

    _, _, _, bonus = add_xp(u.id, random.randint(4, 12))
    if bonus > 0:
        new_bal = int(get_user(u.id)["balance"])

    e = base_embed("Machine à sous", user=u)
    e.add_field(name="Tirage", value=" | ".join(roll), inline=False)
    e.add_field(name="Résultat", value=res, inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


# Nouvelle commande /rps
@bot.tree.command(name="rps", description="Pierre/Feuille/Ciseaux vs bot (x2 si win)")
@app_commands.describe(mise="Montant", choix="pierre/feuille/ciseaux")
async def rps(interaction: discord.Interaction, mise: int, choix: str):
    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        return await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        return await interaction.response.send_message("❌ T'as pas assez de coins.", ephemeral=True)

    choix = choix.strip().lower()
    if choix not in ("pierre", "feuille", "ciseaux"):
        return await interaction.response.send_message("❌ Choix invalide (pierre/feuille/ciseaux).", ephemeral=True)

    bot_choice = random.choice(["pierre", "feuille", "ciseaux"])
    add_draws(u.id, 1)

    # Logique win/lose
    if (choix == "pierre" and bot_choice == "ciseaux") or \
       (choix == "feuille" and bot_choice == "pierre") or \
       (choix == "ciseaux" and bot_choice == "feuille"):
        delta = mise  # x2 total
        info = f"✅ Tu gagnes ! **+{fmt_int(mise)}** {CURRENCY_EMOJI} (x2)"
        action = "rps_win"
    elif choix == bot_choice:
        delta = 0
        info = f"🤝 Égalité ! Mise remboursée."
        action = "rps_tie"
    else:
        delta = -mise
        info = f"❌ Tu perds. **-{fmt_int(mise)}** {CURRENCY_EMOJI}"
        action = "rps_lose"

    new_bal = add_balance(u.id, delta, action=action)
    _, _, _, bonus = add_xp(u.id, random.randint(5, 12))
    if bonus > 0:
        new_bal = int(get_user(u.id)["balance"])

    e = base_embed("Pierre/Feuille/Ciseaux", user=u)
    e.add_field(name="Ton choix", value=choix.title(), inline=True)
    e.add_field(name="Choix bot", value=bot_choice.title(), inline=True)
    e.add_field(name="Résultat", value=info, inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


# Commande /mines corrigée
@bot.tree.command(name="mines", description="Minesweeper 3x3 (1 mine, révèle safes pour cashout x3.5 max)")
@app_commands.describe(mise="Montant")
async def mines(interaction: discord.Interaction, mise: int):
    await interaction.response.defer(ephemeral=False)  # Defer pour éviter timeout (privé pour debug)

    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        await interaction.followup.send("❌ Mise invalide.", ephemeral=True)
        return
    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        await interaction.followup.send("❌ T'as pas assez de coins.", ephemeral=True)
        return

    # Risquer la mise au démarrage
    add_balance(u.id, -mise, action="mines_bet")

    mines_pos = random.sample(range(1, 10), 1)
    game = MinesGame(bet=mise, mines_pos=mines_pos)
    MINES_SESSIONS[u.id] = game

    view = MinesView(user_id=u.id)
    grid = view._render_grid(game)

    e = base_embed("Minesweeper", user=u)
    e.add_field(name="Règles", value="1 mine. Révèle les safes pour multiplier ton cashout (x0.5 après 1, jusqu'à x3.5 après 8). Réclame pour récupérer mise * x. Mine = lose mise !", inline=False)
    e.add_field(name="Mise", value=f"{fmt_int(mise)} {CURRENCY_EMOJI}", inline=True)
    e.add_field(name="Mines", value="1", inline=True)
    e.add_field(name="Grille", value=grid, inline=False)

    await interaction.followup.send(embed=e, view=view, ephemeral=False)  # Réponse finale privée


# =========================
# BLACKJACK WITH BUTTONS
# =========================
class BlackjackView(discord.ui.View):
    def __init__(self, user_id: int, timeout: int = 90):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce blackjack n'est pas le tien.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = BJ_SESSIONS.get(self.user_id)
        if not game or game.finished:
            return await interaction.response.send_message("Partie terminée.", ephemeral=True)

        game.player.append(bj_card())
        p = bj_score(game.player)

        if p > 21:
            game.finished = True
            BJ_SESSIONS.pop(self.user_id, None)

            e = base_embed("BlackJack", user=interaction.user)
            e.add_field(name="Ton jeu", value=f"`{bj_pretty(game.player)}` (**{p}**)", inline=False)
            e.add_field(name="Résultat", value=f"💥 Bust ! Perdu **-{fmt_int(game.bet)}** {CURRENCY_EMOJI}", inline=False)
            e.add_field(name="Solde", value=fmt_money(int(get_user(self.user_id)["balance"])), inline=False)
            self.stop()
            return await interaction.response.edit_message(embed=e, view=None)

        e = base_embed("BlackJack", user=interaction.user)
        e.add_field(name="Ton jeu", value=f"`{bj_pretty(game.player)}` (**{p}**)", inline=False)
        e.add_field(name="Dealer", value=f"`{('A' if game.dealer[0]==11 else game.dealer[0])} ?`", inline=False)
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.success)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = BJ_SESSIONS.get(self.user_id)
        if not game or game.finished:
            return await interaction.response.send_message("Partie terminée.", ephemeral=True)

        while bj_score(game.dealer) < 17:
            game.dealer.append(bj_card())

        p = bj_score(game.player)
        d = bj_score(game.dealer)

        if d > 21 or p > d:
            delta = +game.bet
            action = "blackjack_win"
            result = f"✅ Vous avez gagné **+{fmt_int(game.bet)}** {CURRENCY_EMOJI}"
        elif p == d:
            delta = 0
            action = "blackjack_push"
            result = "🤝 Égalité ! Vous récupérez votre mise."
        else:
            delta = -game.bet
            action = "blackjack_lose"
            result = f"❌ Vous avez perdu **-{fmt_int(game.bet)}** {CURRENCY_EMOJI}"

        game.finished = True
        BJ_SESSIONS.pop(self.user_id, None)
        new_bal = add_balance(self.user_id, delta, action=action)
        _, _, _, bonus = add_xp(self.user_id, random.randint(8, 20))
        if bonus > 0:
            new_bal = int(get_user(self.user_id)["balance"])

        e = base_embed("BlackJack", user=interaction.user)
        e.add_field(name="Toi", value=f"`{bj_pretty(game.player)}` (**{p}**)", inline=False)
        e.add_field(name="Dealer", value=f"`{bj_pretty(game.dealer)}` (**{d}**)", inline=False)
        e.add_field(name="Résultat", value=result, inline=False)
        if bonus > 0:
            e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
        e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)

        self.stop()
        await interaction.response.edit_message(embed=e, view=None)


@bot.tree.command(name="bj", description="Lance une partie de blackjack (boutons Hit/Stand)")
@app_commands.describe(mise="Montant")
async def bj(interaction: discord.Interaction, mise: int):
    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        return await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)
    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        return await interaction.response.send_message("❌ T'as pas assez de coins.", ephemeral=True)

    player = [bj_card(), bj_card()]
    dealer = [bj_card(), bj_card()]
    BJ_SESSIONS[u.id] = BJGame(bet=mise, player=player, dealer=dealer)

    add_draws(u.id, 1)

    p = bj_score(player)
    e = base_embed("BlackJack", user=u)
    e.add_field(name="Mise", value=f"{fmt_int(mise)} {CURRENCY_EMOJI}", inline=True)
    e.add_field(name="Ton jeu", value=f"`{bj_pretty(player)}` (**{p}**)", inline=False)
    e.add_field(name="Dealer", value=f"`{('A' if dealer[0]==11 else dealer[0])} ?`", inline=False)

    if p == 21:
        d = bj_score(dealer)
        if d == 21:
            BJ_SESSIONS.pop(u.id, None)
            e.add_field(name="Résultat", value="🤝 Égalité (double blackjack).", inline=False)
            return await interaction.response.send_message(embed=e)
        else:
            net = (mise * 3) // 2
            BJ_SESSIONS.pop(u.id, None)
            new_bal = add_balance(u.id, net, action="blackjack_blackjack")
            e.add_field(name="Résultat", value=f"🎉 Blackjack ! **+{fmt_int(net)}** {CURRENCY_EMOJI}", inline=False)
            e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
            return await interaction.response.send_message(embed=e)

    view = BlackjackView(user_id=u.id)
    await interaction.response.send_message(embed=e, view=view)


# Nouvelles commandes casino
@bot.tree.command(name="nombre", description="Devine un nombre entre 1 et 10 (x4 si win)")
@app_commands.describe(mise="Montant à miser", choix="Ton choix (1-10)")
async def nombre(interaction: discord.Interaction, mise: int, choix: str):
    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        return await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        return await interaction.response.send_message("❌ T'as pas assez de coins.", ephemeral=True)

    try:
        picked = int(choix.strip())
    except ValueError:
        return await interaction.response.send_message("❌ Choix invalide (doit être 1-10).", ephemeral=True)
    if not (1 <= picked <= 10):
        return await interaction.response.send_message("❌ Numéro invalide (1-10).", ephemeral=True)

    bot_num = random.randint(1, 10)
    add_draws(u.id, 1)

    delta = -mise
    info = ""

    if picked == bot_num:
        win_amount = 3 * mise
        delta = win_amount
        info = f"🎉 JACKPOT ! Gagné **{fmt_int(win_amount)}** {CURRENCY_EMOJI} (x4)"
    else:
        info = f"Perdu. **-{fmt_int(mise)}** {CURRENCY_EMOJI}"

    new_bal = add_balance(u.id, delta, action="nombre")
    _, _, _, bonus = add_xp(u.id, random.randint(5, 15))
    if bonus > 0:
        new_bal = int(get_user(u.id)["balance"])

    e = base_embed("Devine le nombre", user=u)
    e.add_field(name="Ton choix", value=str(picked), inline=True)
    e.add_field(name="Numéro gagnant", value=str(bot_num), inline=True)
    e.add_field(name="Résultat", value=info, inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="cf", description="Coin flip avec twist (50% →49% après win, reset sur loss, x1.5)")
@app_commands.describe(mise="Montant à miser")
async def cf(interaction: discord.Interaction, mise: int):
    u = interaction.user
    ensure_user(u.id)

    if mise <= 0:
        return await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if mise > bal:
        return await interaction.response.send_message("❌ T'as pas assez de coins.", ephemeral=True)

    streak = get_cf_streak(u.id)
    chance_pct = max(1, 50 - streak)
    chance = chance_pct / 100.0
    win = random.random() < chance
    add_draws(u.id, 1)

    if win:
        win_net = int(0.5 * mise)
        delta = win_net
        set_cf_streak(u.id, streak + 1)
        info = f"✅ Gagné **+{fmt_int(win_net)}** {CURRENCY_EMOJI} (x1.5)"
        next_chance = max(1, 50 - (streak + 1))
    else:
        delta = -mise
        set_cf_streak(u.id, 0)
        info = f"❌ Perdu **-{fmt_int(mise)}** {CURRENCY_EMOJI}"
        next_chance = 50

    new_bal = add_balance(u.id, delta, action="cf")
    _, _, _, bonus = add_xp(u.id, random.randint(3, 10))
    if bonus > 0:
        new_bal = int(get_user(u.id)["balance"])

    e = base_embed("Coin Flip", user=u)
    e.add_field(name="Chance utilisée", value=f"{chance_pct}%", inline=True)
    e.add_field(name="Prochaine chance", value=f"{next_chance}%", inline=True)
    e.add_field(name="Résultat", value=info, inline=False)
    if bonus > 0:
        e.add_field(name="Bonus niveau", value=f"+{fmt_int(bonus)} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    await interaction.response.send_message(embed=e)


# =========================
# PROFILE IMAGE (PILLOW) - AMÉLIORÉ
# =========================
def load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        if FONT_PATH:
            return ImageFont.truetype(FONT_PATH, size=size)
    except:
        pass
    try:
        return ImageFont.truetype("arial.ttf", size=size)
    except:
        return ImageFont.load_default()


def rounded_rect(draw: ImageDraw.ImageDraw, xy, radius: int, fill):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill)


def render_profile_card(
    member: discord.Member,
    user_row: sqlite3.Row,
    clan_name: str,
    clan_role: str,
    clan_bank: int
) -> discord.File:
    W, H = PROFILE_WIDTH, PROFILE_HEIGHT

    if os.path.exists(PROFILE_BG_PATH):
        bg = Image.open(PROFILE_BG_PATH).convert("RGBA").resize((W, H))
    else:
        bg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_bg = ImageDraw.Draw(bg)
        for i in range(H):
            r = int(26 + (i / H) * 39)
            g = int(32 + (i / H) * 47)
            b = int(44 + (i / H) * 62)
            draw_bg.line([(0, i), (W, i)], fill=(r, g, b, 255))

    img = bg.copy()
    draw = ImageDraw.Draw(img)

    rounded_rect(draw, (25, 25, W - 25, H - 25), 22, (0, 0, 0, PROFILE_OVERLAY_ALPHA))

    f_title = load_font(36)
    f_big = load_font(30)
    f_med = load_font(22)
    f_small = load_font(18)

    lvl = int(user_row["level"])
    xp = int(user_row["xp"])
    bal = int(user_row["balance"])
    draws = int(user_row["draws"])
    steals = int(user_row["steals"])

    box_color = (26, 32, 44, 190)
    text_primary = (255, 255, 255, 255)
    text_secondary = (230, 230, 230, 255)
    xp_bar_bg = (60, 60, 60, 200)
    xp_bar_fill = (52, 152, 219, 220)
    badge_color = (241, 196, 15, 255)

    rounded_rect(draw, (45, 45, 200, 95), 18, box_color)
    draw.text((60, 55), f"LVL {lvl}", font=f_med, fill=text_primary)

    rounded_rect(draw, (225, 45, W - 45, 95), 18, box_color)
    draw.text((241, 54), member.display_name, font=f_title, fill=(0, 0, 0, 128))
    draw.text((240, 53), member.display_name, font=f_title, fill=text_primary)

    rounded_rect(draw, (W - 170, 110, W - 45, 160), 18, box_color)
    draw.text((W - 145, 120), "⭐", font=f_big, fill=badge_color)

    def stat_line(y, label, value):
        rounded_rect(draw, (45, y, 360, y + 44), 16, box_color)
        draw.text((61, y + 11), f"{label} : {value}", font=f_small, fill=(0, 0, 0, 128))
        draw.text((60, y + 10), f"{label} : {value}", font=f_small, fill=text_primary)

    stat_line(120, "BANQUE", fmt_int(bal))
    stat_line(170, "TIRAGES", str(draws))
    stat_line(220, "PILLAGES", str(steals))

    rounded_rect(draw, (385, 215, W - 45, 285), 18, box_color)
    if clan_name == "Aucun clan":
        draw.text((406, 231), "CLAN : Aucun clan", font=f_med, fill=(0, 0, 0, 128))
        draw.text((405, 230), "CLAN : Aucun clan", font=f_med, fill=text_primary)
    else:
        draw.text((406, 231), f"CLAN : {clan_name} ({clan_role})", font=f_med, fill=(0, 0, 0, 128))
        draw.text((405, 230), f"CLAN : {clan_name} ({clan_role})", font=f_med, fill=text_primary)
        draw.text((406, 259), f"BANK : {fmt_int(clan_bank)} {CURRENCY_NAME}", font=f_small, fill=(0, 0, 0, 128))
        draw.text((405, 258), f"BANK : {fmt_int(clan_bank)} {CURRENCY_NAME}", font=f_small, fill=text_secondary)

    rounded_rect(draw, (385, 295, W - 45, 320), 12, xp_bar_bg)
    need = 200 + (lvl - 1) * 150
    ratio = max(0.0, min(1.0, xp / max(1, need)))
    bar_w = int((W - 45 - 385 - 4) * ratio)
    rounded_rect(draw, (385 + 2, 297, W - 43, 318), 10, (255, 255, 255, 100))
    rounded_rect(draw, (385 + 2, 297, 385 + 2 + bar_w, 318), 10, xp_bar_fill)
    draw.text(((385 + W - 45) // 2 - 30, 325), f"XP {xp}/{need}", font=f_small, fill=text_primary, anchor="mm")

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return discord.File(fp=bio, filename="profile.png")


@bot.tree.command(name="profil", description="Affiche ton profil (image)")
async def profil(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    membre = membre or interaction.user
    urow = get_user(membre.id)

    clan = clan_name_for_user(membre.id)
    role = user_clan_role(membre.id) or "-"
    cid = user_clan_id(membre.id)
    bank = clan_bank_get(cid) if cid else 0

    file = render_profile_card(membre, urow, clan, role, bank)
    e = base_embed("Profil", user=membre)
    e.set_image(url="attachment://profile.png")
    await interaction.response.send_message(embed=e, file=file)


# =========================
# CLAN COMMANDS
# =========================
clan_group = app_commands.Group(name="clan", description="Commandes de clan")


@clan_group.command(name="create", description="Créer un clan")
@app_commands.describe(nom="Nom du clan (3-20 caractères)")
async def clan_create(interaction: discord.Interaction, nom: str):
    u = interaction.user
    nom = nom.strip()

    if not (3 <= len(nom) <= 20):
        return await interaction.response.send_message("❌ Nom invalide (3-20).", ephemeral=True)
    if user_clan_id(u.id):
        return await interaction.response.send_message("❌ Tu es déjà dans un clan.", ephemeral=True)

    with db_connect() as conn:
        try:
            cur = conn.execute("INSERT INTO clans(name, owner_id, bank) VALUES(?,?,0)", (nom, u.id))
            clan_id = cur.lastrowid
            conn.execute("INSERT INTO clan_members(clan_id, user_id, role) VALUES(?,?,?)", (clan_id, u.id, "owner"))
            conn.commit()
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("❌ Ce nom de clan est déjà pris.", ephemeral=True)

    e = base_embed("Clan créé", user=u)
    e.add_field(name="Nom", value=nom, inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="invite", description="Inviter quelqu’un dans ton clan (owner uniquement)")
async def clan_invite(interaction: discord.Interaction, membre: discord.Member):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le owner peut inviter.", ephemeral=True)
    if user_clan_id(membre.id):
        return await interaction.response.send_message("❌ Cette personne est déjà dans un clan.", ephemeral=True)

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO clan_invites(clan_id, user_id, invited_by)
            VALUES(?,?,?)
            ON CONFLICT(clan_id, user_id) DO UPDATE SET invited_by=excluded.invited_by, created_at=strftime('%s','now')
        """, (cid, membre.id, u.id))
        conn.commit()

    cname = clan_info_by_id(cid)[0]["name"]
    e = base_embed("Invitation envoyée", user=u)
    e.add_field(name="Clan", value=cname, inline=False)
    e.add_field(name="Pour rejoindre", value=f"{membre.mention} doit faire **/clan accept**", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="accept", description="Accepter une invitation de clan")
async def clan_accept(interaction: discord.Interaction):
    u = interaction.user
    if user_clan_id(u.id):
        return await interaction.response.send_message("❌ Tu es déjà dans un clan.", ephemeral=True)

    with db_connect() as conn:
        inv = conn.execute(
            "SELECT clan_id FROM clan_invites WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (u.id,),
        ).fetchone()
        if not inv:
            return await interaction.response.send_message("❌ Tu n’as aucune invitation.", ephemeral=True)

        cid = int(inv["clan_id"])
        conn.execute("DELETE FROM clan_invites WHERE user_id=?", (u.id,))
        conn.execute("INSERT INTO clan_members(clan_id, user_id, role) VALUES(?,?,?)", (cid, u.id, "member"))
        conn.commit()

    cname = clan_info_by_id(cid)[0]["name"]
    e = base_embed("Clan rejoint", user=u)
    e.add_field(name="Clan", value=cname, inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="leave", description="Quitter ton clan (owner ne peut pas)")
async def clan_leave(interaction: discord.Interaction):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)

    if is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Le owner ne peut pas quitter. Utilise /clan transfer ou /clan delete.", ephemeral=True)

    with db_connect() as conn:
        conn.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (cid, u.id))
        conn.commit()

    await interaction.response.send_message(embed=base_embed("Clan", "✅ Tu as quitté ton clan.", user=u))


@clan_group.command(name="info", description="Infos sur ton clan")
async def clan_info(interaction: discord.Interaction):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)

    clan, count, mods = clan_info_by_id(cid)
    bank = int(clan["bank"])
    role = user_clan_role(u.id)

    e = base_embed("Clan", user=u)
    e.add_field(name="Nom", value=clan["name"], inline=False)
    e.add_field(name="Banque", value=f"{fmt_int(bank)} {CURRENCY_NAME} {CURRENCY_EMOJI}", inline=False)
    e.add_field(name="Membres", value=str(count), inline=True)
    e.add_field(name="Mods", value=f"{mods}/{CLAN_MAX_MODS}", inline=True)
    e.add_field(name="Ton rôle", value=role, inline=True)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="deposit", description="Déposer des coins dans la banque du clan (tout membre)")
@app_commands.describe(montant="Montant à déposer")
async def clan_deposit(interaction: discord.Interaction, montant: int):
    u = interaction.user
    ensure_user(u.id)
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if montant <= 0:
        return await interaction.response.send_message("❌ Montant invalide.", ephemeral=True)

    bal = int(get_user(u.id)["balance"])
    if montant > bal:
        return await interaction.response.send_message("❌ T’as pas assez de coins.", ephemeral=True)

    set_balance(u.id, bal - montant)
    clan_bank_add(cid, montant)

    bank = clan_bank_get(cid)
    e = base_embed("Banque du clan", user=u)
    e.add_field(name="Dépôt", value=f"-{fmt_int(montant)} {CURRENCY_EMOJI} depuis ton solde", inline=False)
    e.add_field(name="Banque clan", value=f"{fmt_int(bank)} {CURRENCY_NAME} {CURRENCY_EMOJI}", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="withdraw", description="Retirer des coins de la banque du clan (owner/mod)")
@app_commands.describe(montant="Montant à retirer")
async def clan_withdraw(interaction: discord.Interaction, montant: int):
    u = interaction.user
    ensure_user(u.id)
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_mod_or_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le OWNER ou un MOD peut retirer.", ephemeral=True)
    if montant <= 0:
        return await interaction.response.send_message("❌ Montant invalide.", ephemeral=True)

    bank = clan_bank_get(cid)
    if montant > bank:
        return await interaction.response.send_message("❌ La banque du clan n’a pas assez.", ephemeral=True)

    clan_bank_add(cid, -montant)
    new_bal = add_balance(u.id, montant, action="clan_withdraw")

    bank2 = clan_bank_get(cid)
    e = base_embed("Banque du clan", user=u)
    e.add_field(name="Retrait", value=f"+{fmt_int(montant)} {CURRENCY_EMOJI} vers ton solde", inline=False)
    e.add_field(name="Solde", value=fmt_money(int(new_bal)), inline=False)
    e.add_field(name="Banque clan", value=f"{fmt_int(bank2)} {CURRENCY_NAME} {CURRENCY_EMOJI}", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="setmod", description="Nommer un MOD (owner uniquement, max 2 mods)")
async def clan_setmod(interaction: discord.Interaction, membre: discord.Member):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le owner peut nommer des mods.", ephemeral=True)
    if user_clan_id(membre.id) != cid:
        return await interaction.response.send_message("❌ Cette personne n’est pas dans ton clan.", ephemeral=True)

    clan, _, mods = clan_info_by_id(cid)
    if mods >= CLAN_MAX_MODS:
        return await interaction.response.send_message(f"❌ Max {CLAN_MAX_MODS} mods par clan.", ephemeral=True)

    with db_connect() as conn:
        row = conn.execute("SELECT role FROM clan_members WHERE clan_id=? AND user_id=?", (cid, membre.id)).fetchone()
        if not row:
            return await interaction.response.send_message("❌ Cette personne n’est pas dans ton clan.", ephemeral=True)
        if row["role"] == "owner":
            return await interaction.response.send_message("❌ Le owner est déjà owner.", ephemeral=True)
        if row["role"] == "mod":
            return await interaction.response.send_message("❌ Cette personne est déjà MOD.", ephemeral=True)

        conn.execute("UPDATE clan_members SET role='mod' WHERE clan_id=? AND user_id=?", (cid, membre.id))
        conn.commit()

    e = base_embed("Gestion clan", user=u)
    e.add_field(name="Mod ajouté", value=f"{membre.mention} est maintenant **MOD** de **{clan['name']}**", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="unmod", description="Retirer le rôle MOD (owner uniquement)")
async def clan_unmod(interaction: discord.Interaction, membre: discord.Member):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le owner peut retirer les mods.", ephemeral=True)
    if user_clan_id(membre.id) != cid:
        return await interaction.response.send_message("❌ Cette personne n’est pas dans ton clan.", ephemeral=True)

    with db_connect() as conn:
        row = conn.execute("SELECT role FROM clan_members WHERE clan_id=? AND user_id=?", (cid, membre.id)).fetchone()
        if not row or row["role"] != "mod":
            return await interaction.response.send_message("❌ Cette personne n’est pas MOD.", ephemeral=True)

        conn.execute("UPDATE clan_members SET role='member' WHERE clan_id=? AND user_id=?", (cid, membre.id))
        conn.commit()

    e = base_embed("Gestion clan", user=u)
    e.add_field(name="Mod retiré", value=f"{membre.mention} est redevenu **member**.", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="transfer", description="Transférer le clan à un membre (owner uniquement)")
async def clan_transfer(interaction: discord.Interaction, membre: discord.Member):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le owner peut transférer.", ephemeral=True)
    if user_clan_id(membre.id) != cid:
        return await interaction.response.send_message("❌ Cette personne n’est pas dans ton clan.", ephemeral=True)
    if membre.id == u.id:
        return await interaction.response.send_message("❌ Tu es déjà owner.", ephemeral=True)

    with db_connect() as conn:
        clan = conn.execute("SELECT name FROM clans WHERE id=?", (cid,)).fetchone()
        conn.execute("UPDATE clan_members SET role='member' WHERE clan_id=? AND user_id=?", (cid, u.id))
        conn.execute("UPDATE clan_members SET role='owner' WHERE clan_id=? AND user_id=?", (cid, membre.id))
        conn.execute("UPDATE clans SET owner_id=? WHERE id=?", (membre.id, cid))
        conn.commit()

    e = base_embed("Gestion clan", user=u)
    e.add_field(name="Transfert", value=f"✅ {membre.mention} est maintenant **OWNER** de **{clan['name']}**", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="rename", description="Renommer ton clan (owner uniquement)")
@app_commands.describe(nouveau_nom="Nouveau nom du clan (3-20 caractères)")
async def clan_rename(interaction: discord.Interaction, nouveau_nom: str):
    u = interaction.user
    nouveau_nom = nouveau_nom.strip()
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le owner peut renommer.", ephemeral=True)
    if not (3 <= len(nouveau_nom) <= 20):
        return await interaction.response.send_message("❌ Nom invalide (3-20 caractères).", ephemeral=True)

    with db_connect() as conn:
        if conn.execute("SELECT 1 FROM clans WHERE name=? AND id != ?", (nouveau_nom, cid)).fetchone():
            return await interaction.response.send_message("❌ Ce nom est déjà pris.", ephemeral=True)
        conn.execute("UPDATE clans SET name=? WHERE id=?", (nouveau_nom, cid))
        conn.commit()

    e = base_embed("Gestion clan", user=u)
    e.add_field(name="Renommé", value=f"✅ Ton clan s'appelle maintenant **{nouveau_nom}**.", inline=False)
    await interaction.response.send_message(embed=e)


@clan_group.command(name="delete", description="Supprimer le clan (owner uniquement) ⚠️")
async def clan_delete(interaction: discord.Interaction):
    u = interaction.user
    cid = user_clan_id(u.id)
    if not cid:
        return await interaction.response.send_message("❌ Tu n’es dans aucun clan.", ephemeral=True)
    if not is_clan_owner(cid, u.id):
        return await interaction.response.send_message("❌ Seul le owner peut supprimer le clan.", ephemeral=True)

    with db_connect() as conn:
        clan = conn.execute("SELECT name FROM clans WHERE id=?", (cid,)).fetchone()
        conn.execute("DELETE FROM clan_invites WHERE clan_id=?", (cid,))
        conn.execute("DELETE FROM clan_members WHERE clan_id=?", (cid,))
        conn.execute("DELETE FROM clans WHERE id=?", (cid,))
        conn.commit()

    e = base_embed("Gestion clan", user=u)
    e.add_field(name="Clan supprimé", value=f"🗑️ **{clan['name']}** a été supprimé.", inline=False)
    await interaction.response.send_message(embed=e)


bot.tree.add_command(clan_group)


# =========================
# RUN
# =========================
import os

TOKEN = os.getenv("DISCORD_TOKEN", "MET_TON_TOKEN_ICI")

bot.run(TOKEN)