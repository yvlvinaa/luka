import os
from dotenv import load_dotenv

load_dotenv()

import discord
import random
import re
import time
import asyncio
import signal
import requests
import tempfile
import json
import base64
import copy
import functools
import traceback
import uuid
from io import BytesIO
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont, ImageStat, ImageFilter

# =========================
# LOAD CARDS FROM JSON
# =========================
with open('cards.json', 'r') as f:
    cards = json.load(f)

# Import your database
from data import (
    inventories,
    drop_cooldowns,
    claim_cooldowns,
    card_prints
)


# =========================
# LOAD INVENTORIES FROM JSON (local only, for now)
# =========================
def _load_inventories_json():
    """
    Loads and validates the local inventories.json.

    A local file is considered VALID if:
        - it exists,
        - it parses successfully as JSON,
        - the parsed value is a dict.
    A valid file that parses to an empty dict ({}) is a legitimate,
    valid inventory set -- e.g. a brand new bot with no claims yet --
    and is NOT treated as invalid.

    Returns:
        - the parsed dict (possibly empty) if valid.
        - None if the file is missing, unreadable, contains malformed
          JSON, or parses to something other than a dict. None (not {})
          is the signal for "invalid" specifically so callers can tell
          a genuinely valid empty inventory apart from an invalid file
          without relying on truthiness.
    """
    try:
        with open('inventories.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[inventories] Failed to read inventories.json:")
        traceback.print_exc()
        return None

    if not raw:
        print("[inventories] inventories.json is empty (not even '{}') -- treating as invalid.")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[inventories] inventories.json contains invalid JSON.")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        print(f"[inventories] inventories.json did not contain a JSON object "
              f"(got {type(parsed).__name__}) -- treating as invalid.")
        return None

    return parsed


# NOTE: the actual startup load (local-first, GitHub fallback) happens
# further below, in _sync_inventories_from_github_at_startup(), once the
# GitHub helpers it needs are defined. This is still the single
# load-into-memory step -- nothing here runs it yet.


# Initialize intents
# Narrowed from Intents.all() down to only what's actually used:
#   - guilds: baseline guild/channel data (required for virtually anything).
#   - members: guild.members is genuinely iterated by real commands --
#     `lgive @everyone`/role targeting and `lresetinventories` both walk
#     the full member list, so this stays enabled per spec.
#   - messages: required to actually receive on_message events at all --
#     without this, Discord never dispatches message events to the bot
#     in the first place, regardless of message_content below.
#   - message_content: every command is read from plain message text.
# Everything else (presences, voice_states, typing, invites, webhooks,
# scheduled_events, auto_moderation, etc.) is disabled -- grepped the
# whole file first and confirmed zero references to presence/activity/
# status/voice/typing anywhere, so none of it was ever actually used.
# This only changes what discord.py caches/receives from the gateway;
# it doesn't touch any command logic, targeting, or behavior.
intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

# Global Configurations
DROP_COOLDOWN = 600
CLAIM_COOLDOWN = 300
CLAIM_TIME_LIMIT = 90  # seconds a dropped card stays claimable before its buttons expire
CARDS_PER_PAGE = 10
THEME_COLOR = discord.Color.from_rgb(255, 227, 102)

# "Drop Powers": for this many seconds after a drop is posted, only the
# user who dropped it may claim from it. Ends early the moment the
# dropper successfully claims either card. Separate from DROP_COOLDOWN
# (which gates how often a user may drop) and CLAIM_COOLDOWN (which
# gates how often a user may claim) -- neither of those changes.
DROP_PRIORITY_SECONDS = 6

# Command anti-spam (ld / lg / lt / lcd): NOT the real gameplay
# cooldowns (DROP_COOLDOWN/CLAIM_COOLDOWN above), which are untouched.
# This only stops a user from rapidly double/triple-firing the same
# command. If the same command is used twice within
# COMMAND_SPAM_WINDOW_SECONDS, further attempts are blocked for
# COMMAND_SPAM_BLOCK_SECONDS.
COMMAND_SPAM_WINDOW_SECONDS = 5
COMMAND_SPAM_BLOCK_SECONDS = 3

# lpin / lunpin
MAX_PINNED_CARDS = 3

# Channel where a "new card added" announcement is posted after a
# successful laddcard. Set to 0 to disable; if the channel can't be found
# the notification is just skipped, never an error.
CARD_UPDATES_CHANNEL_ID = 1540008425818169364

# =========================
# BADGES & SHOWCASE CONFIG
# =========================
# Role ID for the "Early Supporter" role, used ONLY for the OG badge.
# Checked by ID, never by name, per the badge spec.
EARLY_SUPPORTER_ROLE_ID = 1505590926947651669

# Role ID for the "Finalist" role, the only role allowed to use `lgw`
# (giveaway cards out of Luka's own recovery inventory). Checked by ID,
# never by name, per spec.
FINALIST_ROLE_ID = 1506025540350509177

# =========================
# OWNER-ONLY STAFF COMMANDS (lgive)
# =========================
# TODO: replace with the actual Discord user IDs of the bot owner(s).
# Checked by explicit ID, never by role/username, per spec. Left empty
# on purpose: an unfilled set means lgive refuses EVERYONE rather than
# accidentally granting access to nobody-in-particular or to a
# plausible-looking placeholder id that isn't actually yours.
OWNER_USER_IDS = {
    727441845789130804,  # owner
    770651160695537697,  # other owner
}

MAX_SHOWCASE_CARDS = 3
BADGES_PER_PAGE = 4
HELP_COMMANDS_PER_PAGE = 6
# Small visual divider between the star rating and the description in
# the `lshowcase` embed.
SHOWCASE_DIVIDER = "─" * 28

# Showcase image layout (Pillow). Canvas is fixed at 1200x675.
#
# BUG FIX: showcased cards previously grew by adding the SAME flat
# pixel amount to width and height independently
# (_SHOWCASE_CARD_SIZE_INCREASE = (180, 180) on top of a 210x280 base).
# Since 210 and 280 aren't equal, adding an identical number of pixels
# to both shifts the ratio away from 3:4 a little more with every
# increase -- 390x460 is a 0.848 ratio, not the native 0.75 -- which is
# exactly why the cards looked stretched. The base render itself
# (CARD_WIDTH x CARD_HEIGHT = 1536x2048) is untouched and still a
# perfect 3:4; only the showcase's own resize target had drifted.
#
# Fix: derive SHOWCASE_CARD_SIZE from a single uniform SCALE FACTOR
# applied to the native 210x280 size, instead of two independent
# deltas. 13/7 is chosen to land on the previous width (390) exactly,
# so the on-screen footprint stays the same size (not smaller) and the
# left/right positions below don't need to change -- only the height
# corrects from 460 to its proportionally-correct 520, restoring the
# exact 3:4 ratio.
SHOWCASE_CANVAS_SIZE = (1200, 675)
_SHOWCASE_CARD_SCALE = 13 / 7
SHOWCASE_CARD_SIZE = (round(210 * _SHOWCASE_CARD_SCALE), round(280 * _SHOWCASE_CARD_SCALE))
SHOWCASE_BACKGROUND_PATH = "showcase_background.png"
SHOWCASE_POSITIONS = {
    1: [(495, 198)],
    2: [(330, 198), (660, 198)],
    # Left moved another ~40px further left (160 -> 120) and right
    # moved another ~40px further right (830 -> 870), purely so the
    # even-larger cards (above) don't overlap; the centre card and the
    # overall centering are untouched.
    3: [(120, 198), (495, 198), (870, 198)],
}

# Soft drop-shadow behind each showcased card, so it reads as sitting
# on the background instead of floating. This is used ONLY inside
# generate_showcase_image below -- it never touches render_card or any
# other renderer in the bot.
SHOWCASE_SHADOW_OFFSET = (8, 8)     # a few px down/right, per spec
SHOWCASE_SHADOW_BLUR_RADIUS = 14    # slight blur
SHOWCASE_SHADOW_OPACITY = 80        # low opacity, out of 255

# Global tracking for lookup history sessions
user_last_lookup = {}

# Global tracking for trade and gift sessions
active_trades = {}
active_gifts = {}

# Global tracking for open merchant-trade sessions (Accept Trade ->
# MerchantTradeView). Keyed by user_id, not a trade_id pair, since a
# merchant trade only ever has one human participant -- the merchant
# itself isn't a party that needs tracking here the way a second player
# is for active_trades.
active_merchant_trades = {}


def user_has_active_merchant_trade(user_id) -> bool:
    """
    True if user_id currently has an open merchant trade session (an
    Accept Trade press that hasn't yet been confirmed, cancelled, or
    timed out). Mirrors user_has_active_trade's role for player-to-
    player trades, one session per user at a time.
    """
    data = active_merchant_trades.get(user_id)
    return data is not None and data.get("view") is not None


def user_has_active_trade(user_id) -> bool:
    """
    True if user_id is a participant in any currently-active trade (an
    accepted TradeRequestView that became a real TradeView, tracked in
    active_trades). Pending/unaccepted trade requests aren't tracked in
    active_trades at all, so they don't count here -- only an actually
    ongoing trade blocks starting another one.
    """
    for trade_data in active_trades.values():
        view = trade_data.get("view")
        if view is not None and user_id in (view.user1_id, view.user2_id):
            return True
    return False


async def force_clear_stuck_trades(target_id) -> tuple:
    """
    Owner-repair helper (see `lfixuser`): force-clears target_id out of
    any active_trades/active_merchant_trades entries they're currently
    stuck in -- exactly the same dict-removal + best-effort disable-
    and-edit-the-old-message cleanup a normal expiry (TradeView.
    on_timeout/decline, trade_expiration_sweep_loop) already performs,
    just triggered manually instead of by a timeout.

    Removing a player-trade entry necessarily ends it for BOTH
    participants -- a trade is inherently bilateral, so there's no way
    to free only the stuck side without ending the trade itself, which
    is exactly what a normal timeout already does too.

    Never touches inventories, cards, or anything actually traded --
    an active_trades/active_merchant_trades entry only ever tracks an
    in-progress SESSION, never any already-completed exchange. Both are
    plain in-memory dicts (not one of the *.json persisted stores), so
    there's nothing to save/roll back here -- clearing an entry is a
    single, already-atomic dict operation.

    Returns (player_trades_cleared, merchant_trades_cleared).
    """
    player_cleared = 0
    for trade_id, trade_data in list(active_trades.items()):
        view = trade_data.get("view")
        if view is None or target_id not in (view.user1_id, view.user2_id):
            continue

        active_trades.pop(trade_id, None)
        player_cleared += 1

        for item in view.children:
            item.disabled = True
        msg = trade_data.get("message")
        if msg is not None:
            try:
                embed = discord.Embed(color=THEME_COLOR)
                embed.description = "This trade was force-cleared by an owner (stuck-state repair)."
                await msg.edit(content=None, embed=embed, view=view)
            except Exception:
                pass

    merchant_cleared = 0
    merchant_trade_data = active_merchant_trades.pop(target_id, None)
    if merchant_trade_data is not None:
        merchant_cleared = 1
        view = merchant_trade_data.get("view")
        if view is not None:
            for item in view.children:
                item.disabled = True
            msg = merchant_trade_data.get("message")
            if msg is not None:
                try:
                    await msg.edit(view=view)
                except Exception:
                    pass

    return player_cleared, merchant_cleared


_trade_sweep_task = None


async def trade_expiration_sweep_loop():
    """
    Backstop for active_trades, independent of any single TradeView's
    own on_timeout callback. Runs every TRADE_SWEEP_INTERVAL_SECONDS and
    force-expires (clears) any active_trades entry older than
    TRADE_MAX_LIFETIME_SECONDS, even if its view's own timeout somehow
    never fired (a swallowed exception, a library-timer edge case, an
    abandoned confirming-stage trade, etc.) and even if nobody has
    touched that trade since. This is what makes expiration reliable
    "even if nobody interacts with the old trade again" -- it never
    depends on further user interaction to run.
    """
    while True:
        await asyncio.sleep(TRADE_SWEEP_INTERVAL_SECONDS)
        try:
            now = time.time()
            stale_ids = [
                trade_id for trade_id, trade_data in list(active_trades.items())
                if now - trade_data.get("time", 0) > TRADE_MAX_LIFETIME_SECONDS
            ]

            for trade_id in stale_ids:
                trade_data = active_trades.pop(trade_id, None)
                if trade_data is None:
                    continue

                view = trade_data.get("view")
                message = trade_data.get("message")

                if view is not None:
                    for item in view.children:
                        item.disabled = True
                    try:
                        # Stops this view from listening for further
                        # interactions -- also means its own on_timeout
                        # (if it fires later regardless) just no-ops on
                        # an already-removed trade_id, never a duplicate
                        # or conflicting cleanup.
                        view.stop()
                    except Exception:
                        pass

                if message is not None:
                    try:
                        embed = discord.Embed(color=THEME_COLOR)
                        embed.description = "Trade has expired."
                        await message.edit(content=None, embed=embed, view=view)
                    except Exception:
                        pass
        except Exception:
            print("[trades] Periodic trade expiration sweep failed (will retry next cycle):")
            traceback.print_exc()


user_viewing_inventory = {}

# Command anti-spam state (see COMMAND_SPAM_* above). Keyed by
# (user_id, command_name); purely in-memory, not persisted.
_command_spam_last_used = {}
_command_spam_blocked_until = {}


def is_command_spam(user_id, command_name: str) -> bool:
    """
    Returns True if this invocation of `command_name` by `user_id`
    should be silently rate-limited as spam (not the real gameplay
    cooldown -- just rapid double/triple-firing of the same command).
    Records this attempt's timestamp as a side effect.
    """
    now = time.time()
    key = (user_id, command_name)

    if now < _command_spam_blocked_until.get(key, 0):
        return True

    last = _command_spam_last_used.get(key)
    _command_spam_last_used[key] = now

    if last is not None and (now - last) < COMMAND_SPAM_WINDOW_SECONDS:
        _command_spam_blocked_until[key] = now + COMMAND_SPAM_BLOCK_SECONDS
        return True

    return False

# Guards every write transaction that touches the card database: the
# in-memory `cards` list, cards.json, GitHub sync, and card_art/ files.
# Held for the full transaction (mutate -> GitHub commit -> local
# mirror/save) so two admin commands can never interleave a
# read-modify-write against the same data and silently overwrite each
# other's changes. Never used by read-only paths (ld, lookups, inventory,
# claiming, trading, gifting, rendering).
cards_lock = asyncio.Lock()

# Same idea as cards_lock, but for the `inventories` dict / local
# inventories.json save. Held for the mutate + save transaction so two
# inventory changes (e.g. two simultaneous claims) can never interleave
# and silently overwrite each other. Local-only for now -- no GitHub
# sync, no background tasks -- per the incremental rebuild plan.
inventories_lock = asyncio.Lock()

# =========================
# SHOWCASE DESCRIPTIONS (showcases.json)
# =========================
# Stores ONLY each user's custom showcase description text -- nothing
# else. Loaded/saved the same way inventories.json is (atomic write,
# own lock). Card selections themselves stay exactly where they
# already were (the "showcased" flag on each owned_card entry).
def _load_showcase_descriptions_json():
    try:
        with open('showcases.json', 'r') as f:
            raw = f.read().strip()
    except (FileNotFoundError, OSError):
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


showcase_descriptions = _load_showcase_descriptions_json()
showcase_descriptions_lock = asyncio.Lock()


def get_showcase_description(user_id):
    """Returns the stored description string for user_id, or None if unset."""
    return showcase_descriptions.get(str(user_id))


def save_showcase_descriptions_local() -> None:
    """Atomically persists showcase_descriptions to showcases.json."""
    data_bytes = json.dumps(showcase_descriptions, indent=2).encode("utf-8")
    _atomic_write_bytes("showcases.json", data_bytes)


# =========================
# SHOWCASE VOTES (showcase_votes.json)
# =========================
# Stores ONLY {owner_user_id: [voter_user_id, ...]} -- nothing else.
# A vote count is simply len(that list). Loaded/saved the same way
# showcases.json is (atomic write, own lock).
def _load_showcase_votes_json():
    """
    Loads and validates the local showcase_votes.json.

    Mirrors _load_inventories_json()'s contract exactly, for the same
    reason: "the file doesn't exist yet" and "the file exists but is
    corrupt/wrong shape" are NOT the same situation and must never be
    silently collapsed into the same "just start with {}" result --
    that silent collapse (the previous version of this function caught
    FileNotFoundError, OSError, and JSONDecodeError and returned {} for
    all three, with no logging at all) is the actual, confirmed root
    cause of every showcase vote reading as reset to 0: on a redeploy
    where showcase_votes.json isn't carried over to the new local disk
    (this bot's inventories.json has an explicit GitHub-backfill step
    for exactly this "brand-new deploy/container" scenario --
    showcase_votes.json has no equivalent, so it silently starts over),
    the plain FileNotFoundError was indistinguishable from "nobody has
    ever voted" -- no error, no log line, nothing.

    Returns:
        - the parsed dict (possibly a legitimately empty {}, e.g. a
          brand new bot with no votes yet) if the file exists and is
          valid.
        - None if the file is missing, unreadable, contains malformed
          JSON, or parses to something other than a dict -- None is
          the explicit "invalid, do not treat as empty" signal, so the
          startup loader below can tell a real empty vote set apart
          from a problem worth reporting.
    """
    try:
        with open('showcase_votes.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[showcase_votes] Failed to read showcase_votes.json:")
        traceback.print_exc()
        return None

    if not raw:
        print("[showcase_votes] showcase_votes.json is empty (not even '{}') -- treating as invalid.")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[showcase_votes] showcase_votes.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        print(f"[showcase_votes] showcase_votes.json did not contain a JSON object "
              f"(got {type(parsed).__name__}) -- treating as invalid.")
        return None

    return parsed


def _load_pending_recovery_json():
    """
    Loads and validates the local pending_recovery.json. Same contract,
    same reasoning, as _load_showcase_votes_json()/_load_inventories_json()
    -- "missing" and "invalid" are both real, distinct situations, never
    silently collapsed into "just start empty" without at least a log
    line, since that's what makes the difference between a countdown
    that's actually surviving restarts and one that silently resets.

    Returns the parsed dict (possibly a legitimately empty {}) if valid,
    or None if missing/unreadable/malformed/not a dict.
    """
    try:
        with open('pending_recovery.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[recovery] Failed to read pending_recovery.json:")
        traceback.print_exc()
        return None

    if not raw:
        print("[recovery] pending_recovery.json is empty (not even '{}') -- treating as invalid.")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[recovery] pending_recovery.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        print(f"[recovery] pending_recovery.json did not contain a JSON object "
              f"(got {type(parsed).__name__}) -- treating as invalid.")
        return None

    return parsed


def get_vote_count(user_id) -> int:
    """Returns the number of showcase votes user_id currently has."""
    return len(showcase_votes.get(str(user_id), []))


def has_voted(owner_id, voter_id) -> bool:
    """Returns whether voter_id has already voted on owner_id's showcase."""
    return str(voter_id) in showcase_votes.get(str(owner_id), [])


# Create card_art directory if it doesn't exist
if not os.path.exists('card_art'):
    os.makedirs('card_art')

# Local, permanent home for merchant profile pictures (see MERCHANT_TEMPLATES
# below) -- these are shipped-in-repo files, never Discord attachment URLs,
# so a merchant's thumbnail never depends on a CDN link that can expire.
# Changing a merchant's picture later only ever means replacing the file at
# this same path/filename -- nothing else needs to change.
MERCHANT_ASSETS_DIR = "merchant_assets"
if not os.path.exists(MERCHANT_ASSETS_DIR):
    os.makedirs(MERCHANT_ASSETS_DIR)

# =========================
# HELPERS
# =========================

def stars(amount):
    """Converts a number into a star emoji string."""
    return "⭐" * int(amount)


async def reply(message, *args, **kwargs):
    """
    Sends a response as a reply to the user's message instead of a bare
    channel send, so busy channels are easier to follow. Falls back to a
    normal channel send if the reply fails for any reason (e.g. the
    original message was deleted in the meantime), so a reply-formatting
    issue can never block a response from going out.
    """
    try:
        return await message.reply(*args, **kwargs, mention_author=False)
    except Exception:
        return await message.channel.send(*args, **kwargs)


def format_time(seconds):
    """Formats raw seconds into human-readable minutes and seconds."""
    if seconds <= 0:
        return "ready"
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}m {seconds}s"


def get_inventory(user_id):
    """Safely fetches or initializes a user's inventory list."""
    return inventories.setdefault(str(user_id), [])


def peek_next_print(card_id):
    """Returns the next print number without reserving it."""
    return card_prints.get(card_id, 0) + 1


def get_next_print(card_id):
    """Actually assigns the next print number when a card is claimed."""
    current = card_prints.get(card_id, 0) + 1
    card_prints[card_id] = current
    return current


def add_card(user_id, card):
    """
    Adds a card to a user's inventory.
    Inserts newest-first; numbering is based on list positions (1-based).
    """
    inv = get_inventory(user_id)

    owned_card = {
        "card": card,
        "print": get_next_print(card["id"]),
        "claimed_at": time.time()
    }

    inv.insert(0, owned_card)


def add_recycled_card(user_id, card, print_num):
    """
    Recycled-card exception (see `lrecyclecards`): inserts a card into
    user_id's inventory using its EXACT original print number instead
    of assigning a fresh one via get_next_print() -- card_prints[card_id]
    is deliberately left completely untouched, so a character's normal
    print progression is unaffected by a recycled claim (a claimed
    recycled Gojo #1 does not change what number the next NORMAL Gojo
    drop gets). Otherwise identical to add_card(): same newest-first
    insert, same owned_card shape. Any drop-only marker keys
    (_recycled_entry_id/_recycled_print, see get_weighted_card()) are
    stripped from the stored card first, so a recycled card is stored
    completely indistinguishably from a normally-claimed one.
    """
    inv = get_inventory(user_id)

    clean_card = {k: v for k, v in card.items() if not k.startswith("_recycled")}
    owned_card = {
        "card": clean_card,
        "print": print_num,
        "claimed_at": time.time()
    }

    inv.insert(0, owned_card)


def remove_card(user_id, index):
    """Removes a card from a user's collection by its index position."""
    return get_inventory(user_id).pop(index)


def get_card_by_id(card_id):
    """Looks up a card TEMPLATE from the global `cards` list by its id.
    Returns None if no such card exists."""
    return next((c for c in cards if c.get("id") == card_id), None)


def get_weighted_card():
    """
    Selects a card randomly based on its assigned weight value,
    restricted to cards currently eligible to drop under the
    version/unlock system (base-card period, then sequential Common
    unlocks + the Rare unlock -- see _card_is_eligible_to_drop). This is
    the single place drop eligibility is enforced; lup/lv/lmissing all
    read the same underlying `version` metadata and `card_prints` claim
    counts, but never re-implement this eligibility check themselves.

    Recycled-card exception (see `lrecyclecards`): every currently-
    active recycled print (see get_active_recycled_entries()) is folded
    into this SAME weighted pool, at its underlying card's normal
    weight -- so a recycled print competes fairly for a drop slot
    instead of being guaranteed or inflated, and has ZERO effect on
    odds whenever nothing is currently recycled (the loop below is then
    simply empty -- identical to this function before recycling
    existed). A recycled pick is a fresh COPY of the underlying card
    dict with two extra marker keys (_recycled_entry_id/_recycled_print)
    added -- the real template in `cards` is never mutated, and those
    marker keys are stripped again before anything is ever persisted
    (see add_recycled_card()).
    """
    now = time.time()
    base_by_character, prev_common_by_card_id = _build_character_version_lookup()

    weighted = []
    for card in cards:
        if not _card_is_eligible_to_drop(card, now, base_by_character, prev_common_by_card_id):
            continue
        weighted.extend([card] * card.get("weight", 1))

    for entry in get_active_recycled_entries():
        underlying_card = get_card_by_id(entry.get("card_id"))
        if underlying_card is None:
            # The template itself no longer exists (e.g. removed since
            # this print was recycled) -- nothing sensible to drop.
            continue
        recycled_card = dict(
            underlying_card,
            _recycled_entry_id=entry.get("id"),
            _recycled_print=entry.get("print"),
        )
        weighted.extend([recycled_card] * underlying_card.get("weight", 1))

    if not weighted:
        # Extremely defensive fallback -- e.g. immediately after a reset,
        # before any base Commons exist in cards.json yet. Never crash a
        # drop; fall back to the full (unfiltered) pool rather than
        # raise on an empty random.choice().
        weighted = [card for card in cards for _ in range(card.get("weight", 1))]

    return random.choice(weighted)


# Intended drop weight per star rating -- higher stars are rarer, so they
# get a lower weight. Used both for new cards (laddcard) and the one-time
# migration of existing cards.
STAR_WEIGHTS = {
    1: 10,
    2: 10,
    3: 8,
    4: 5,
}


def weight_for_stars(stars_val: int) -> int:
    """Returns the intended drop weight for a given star rating, per STAR_WEIGHTS."""
    return STAR_WEIGHTS.get(stars_val, 10)


def format_print(print_num):
    """Formats print number for display."""
    if print_num < 100:
        return f"#{print_num}"
    if print_num == 100:
        return "#100"
    if print_num > 100:
        return "L"


def card_version_label(card):
    """
    Returns 'Common' or 'Rare' for display, based on the card's frame --
    never the frame color/name itself. Any frame other than exactly
    "common" (case-insensitive) counts as Rare.
    """
    return "Common" if card.get("frame", "").strip().lower() == "common" else "Rare"


def card_version_display(card):
    """
    Returns the card's actual `version` metadata for display -- "common",
    "V1", "V2", ... for Commons (per-character, creation-order), "rare"
    for Rares. This is the corrected replacement for card_version_label()
    everywhere a card's specific version (not just its broad Common/Rare
    rarity) needs to be shown, per the version/unlock system -- lup and
    lv both use this now. Falls back to card_version_label()'s broad
    Common/Rare text (lowercased) only for the theoretical case of a
    card with no `version` field yet (e.g. a cards.json that predates
    the migration and hasn't been reloaded).
    """
    version = card.get("version")
    if version:
        # Display-only capitalization fix: "common"/"rare" should read as
        # "Common"/"Rare" to the user, without touching the underlying
        # `version` metadata (still lowercase "common"/"rare" internally,
        # e.g. for the version/unlock system's own comparisons). "V1",
        # "V2", etc. are already capitalized and pass through unchanged.
        if version == "common":
            return "Common"
        if version == "rare":
            return "Rare"
        return version
    return card_version_label(card).lower()


def save_cards_json():
    """
    Saves the cards list to cards.json, atomically (via
    _atomic_write_bytes): written to a temp file in the same directory,
    then moved into place with os.replace(). This guarantees the file on
    disk is always either the complete old version or the complete new
    version -- never truncated or partially written, even if the
    process is killed or an exception occurs mid-write. Previously this
    used a plain open(..., "w"), which had no such guarantee.
    """
    cards_json_bytes = json.dumps(cards, indent=2).encode("utf-8")
    _atomic_write_bytes("cards.json", cards_json_bytes)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """
    Writes `data` to `path` atomically: to a temp file in the same
    directory first, then moved into place with os.replace() (an atomic
    rename on both POSIX and Windows).

    This matters specifically for card_art/*.png files: opening the
    destination path directly in 'wb' mode truncates it to zero bytes
    immediately, before any new bytes are written. If a card render
    happens to load that exact file during that window (e.g.
    lupdateimage/leditcard updating a card's art while it's also being
    rendered for a drop/lookup elsewhere), Pillow reads a zero-byte or
    partial PNG, fails to decode it, and the renderer silently falls back
    to a blank placeholder for that one render -- even though cards.json
    and the file on disk are both completely correct a moment later. Using
    a temp file + atomic rename means a concurrent reader always sees
    either the complete old file or the complete new file, never a
    partial one.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _inventories_json_bytes() -> bytes:
    """Serializes the current in-memory `inventories` dict to JSON bytes."""
    return json.dumps(inventories, indent=2).encode("utf-8")


def save_inventories_local() -> None:
    """
    Writes the current in-memory `inventories` dict to inventories.json
    on disk, atomically (via _atomic_write_bytes). Called immediately
    after every successful inventory mutation (claim/gift/trade).

    Local-only for now: no GitHub sync, no debouncing, no background
    tasks -- this is deliberately just the local-persistence half, per
    the incremental rebuild plan. Raises (after printing a full
    traceback) on failure so the caller can roll back its in-memory
    mutation; inventories.json itself is never left partially written,
    since _atomic_write_bytes only ever produces the complete old file
    or the complete new file.
    """
    try:
        _atomic_write_bytes("inventories.json", _inventories_json_bytes())
    except Exception:
        print("[inventories] Failed to save inventories.json locally:")
        traceback.print_exc()
        raise


# How often (at most) inventories.json is pushed to GitHub. Local saves
# still happen immediately on every mutation regardless of this value --
# this only paces the GitHub half of persistence. Single configurable
# constant, per spec.
INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS = 300

# Handle for the single running instance of inventory_github_sync_loop()
# (started once from Client.on_ready below). Kept at module level --
# rather than as a Client instance attribute -- so Client.__init__
# doesn't need to be touched; on_ready() only needs to read/set this one
# name to guard against starting a duplicate after a reconnect.
_inventory_sync_task = None

# Guards against registering the SIGTERM handler (see Client.on_ready)
# more than once if on_ready fires again after a reconnect.
_shutdown_handler_registered = False

# Set True (under inventories_lock, immediately after a successful
# save_inventories_local()) whenever inventories.json has local changes
# not yet pushed to GitHub. Cleared by inventory_github_sync_loop() once
# it has snapshotted the data for an upload.
_inventories_dirty = False

# Defensive guard against a concurrent upload. Not strictly required by
# the loop's own control flow (it's sequential: one iteration's upload
# always completes, or fails, before the next begins), but kept as an
# explicit belt-and-suspenders check so "never run two uploads at once"
# holds even if this loop is ever called from more than one place.
_inventory_upload_in_progress = False


def mark_inventories_dirty() -> None:
    """
    Marks that inventories.json has local changes not yet pushed to
    GitHub. Must be called while holding inventories_lock, immediately
    after a successful save_inventories_local(), as part of the same
    mutate -> save -> mark-dirty transaction. Does not touch the
    network -- just flips a flag for inventory_github_sync_loop() to
    notice on its next cycle.
    """
    global _inventories_dirty
    _inventories_dirty = True


async def inventory_github_sync_loop():
    """
    Background task: wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS
    and, only if inventories.json has unpushed local changes, performs
    exactly one GitHub commit containing the current inventories --
    coalescing any number of claims/gifts/trades from that interval into
    a single upload, instead of committing on every mutation. Reuses the
    exact same GitHub commit helper already used for cards.json
    (github_commit_files) -- no second GitHub system.

    inventories_lock is only held for the brief snapshot + dirty-flag
    read/write, never across the actual network call, so this never
    blocks a claim/gift/trade for the duration of a GitHub upload.

    If a mutation happens while an upload is in flight, it re-sets the
    dirty flag (via mark_inventories_dirty(), under the lock) after this
    loop already snapshotted and cleared it -- so that change is picked
    up and pushed on the *next* cycle, one interval later, rather than
    being lost or triggering an immediate second upload.

    On failure, the dirty flag is restored so the next cycle retries; a
    GitHub outage never rolls back or otherwise affects gameplay, since
    the local save that already succeeded is untouched.
    """
    global _inventories_dirty, _inventory_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _inventories_dirty or _inventory_upload_in_progress:
            continue

        async with inventories_lock:
            if not _inventories_dirty:
                continue
            data = _inventories_json_bytes()
            _inventories_dirty = False

        _inventory_upload_in_progress = True
        try:
            await github_commit_files({"inventories.json": data}, "Batched inventory sync")
        except Exception:
            print("[inventories] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with inventories_lock:
                _inventories_dirty = True
        finally:
            _inventory_upload_in_progress = False


async def flush_inventories_to_github() -> None:
    """
    Best-effort final push of any pending (locally-saved but not yet
    committed) inventory changes to GitHub. Called from Client.close()
    on graceful shutdown (see below) so that a Railway redeploy -- which
    sends SIGTERM to the outgoing container before replacing it with a
    fresh one built from the latest GitHub state -- can't discard a
    claim/trade/gift that was already confirmed and saved locally just
    because it hadn't reached the next scheduled batch yet.

    Keeps the batching/dirty-flag design entirely intact: this only
    triggers one extra, early upload at shutdown time, using the exact
    same snapshot-then-clear-then-push pattern as the periodic loop
    above. If it fails, the dirty flag is restored so a normal restart
    (without a clean shutdown) still retries via the periodic loop.
    """
    global _inventories_dirty

    async with inventories_lock:
        if not _inventories_dirty:
            return
        data = _inventories_json_bytes()
        _inventories_dirty = False

    try:
        await github_commit_files({"inventories.json": data}, "Final inventory sync (shutdown)")
        print("[inventories] Flushed pending inventory changes to GitHub before shutdown.")
    except Exception:
        async with inventories_lock:
            _inventories_dirty = True
        print("[inventories] Failed to flush pending inventory changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


# Fields every card entry is expected to have (used by lsync / leditcard).
REQUIRED_CARD_FIELDS = ["id", "name", "series", "stars", "frame", "image"]


def has_uploader_role(member) -> bool:
    """Shared permission check for card-management commands (Uploader role)."""
    return any(role.name.lower() == "uploader" for role in member.roles)


def resolve_frame_name(requested_frame: str):
    """
    Resolves a user-typed frame name against the frames/ folder, accepting
    the name with or without a '.png' extension (same rule used by
    laddcard). Returns the resolved frame name (without extension) if it
    exists on disk, otherwise None.
    """
    requested_frame = (requested_frame or "").strip()
    candidate = requested_frame[:-4] if requested_frame.lower().endswith(".png") else requested_frame
    candidate_path = os.path.join(FRAME_DIR, f"{candidate}.png")
    if candidate and os.path.exists(candidate_path):
        return candidate
    return None


# =========================
# GITHUB SYNC (used only by lupdateimage)
# =========================
# Credentials are read from environment variables, never hardcoded:
#   GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
#   GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
#   GITHUB_REPO = os.getenv("GITHUB_REPO")
#   GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
#
# Uses the GitHub Git Data API (blobs -> tree -> commit -> ref update) so
# that multiple files (image + cards.json) land in a single atomic commit.
# If any step fails before the final ref update, the branch is never
# touched, so the repository can never be left in a partially updated state.

GITHUB_API_BASE = "https://api.github.com"


def _github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Luka-Bot",
    }


def _github_get_branch_commit_sha(headers, owner, repo, branch):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/ref/heads/{branch}"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to read branch ref ({resp.status_code}): {resp.text}")
    return resp.json()["object"]["sha"]


def _github_get_commit_tree_sha(headers, owner, repo, commit_sha):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits/{commit_sha}"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to read base commit ({resp.status_code}): {resp.text}")
    return resp.json()["tree"]["sha"]


def _github_create_blob(headers, owner, repo, content_bytes):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/blobs"
    payload = {
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "encoding": "base64",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise Exception(f"Failed to create blob ({resp.status_code}): {resp.text}")
    return resp.json()["sha"]


def _github_create_tree(headers, owner, repo, base_tree_sha, tree_entries):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees"
    payload = {"base_tree": base_tree_sha, "tree": tree_entries}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise Exception(f"Failed to create tree ({resp.status_code}): {resp.text}")
    return resp.json()["sha"]


def _github_tree_contains_path(headers, owner, repo, tree_sha, path):
    """
    Checks whether `path` exists as a blob in the given tree (recursive).
    GitHub's Git Data API returns an error if you try to delete a path
    that doesn't exist, so this lets a delete be safely skipped instead of
    failing the whole atomic commit when the file is already missing/the
    stored path doesn't exactly match what's actually in the repo.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to read tree contents ({resp.status_code}): {resp.text}")
    entries = resp.json().get("tree", [])
    return any(entry.get("path") == path and entry.get("type") == "blob" for entry in entries)


def _github_create_commit(headers, owner, repo, message, tree_sha, parent_sha):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits"
    payload = {"message": message, "tree": tree_sha, "parents": [parent_sha]}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise Exception(f"Failed to create commit ({resp.status_code}): {resp.text}")
    return resp.json()["sha"]


def _github_update_branch_ref(headers, owner, repo, branch, commit_sha):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{branch}"
    payload = {"sha": commit_sha, "force": False}
    resp = requests.patch(url, headers=headers, json=payload, timeout=15)
    if resp.status_code not in (200, 201):
        raise Exception(f"Failed to update branch ref ({resp.status_code}): {resp.text}")
    return resp.json()


def _github_commit_files_sync(files, commit_message):
    """
    Uploads one or more files to the GitHub repo as a single atomic commit.
    files: dict of {repo_relative_path: bytes_content}
    Returns the new commit sha on success. Raises on any failure; if it
    raises, the branch ref was never updated, so nothing was actually
    pushed to the repository's history.
    """
    token = os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GITHUB_USERNAME")
    repo = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH")

    missing = [
        name for name, val in [
            ("GITHUB_TOKEN", token),
            ("GITHUB_USERNAME", owner),
            ("GITHUB_REPO", repo),
            ("GITHUB_BRANCH", branch),
        ] if not val
    ]
    if missing:
        raise Exception(f"Missing required environment variable(s): {', '.join(missing)}")

    headers = _github_headers(token)

    latest_commit_sha = _github_get_branch_commit_sha(headers, owner, repo, branch)
    base_tree_sha = _github_get_commit_tree_sha(headers, owner, repo, latest_commit_sha)

    tree_entries = []
    for path, content_bytes in files.items():
        blob_sha = _github_create_blob(headers, owner, repo, content_bytes)
        tree_entries.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": blob_sha,
        })

    new_tree_sha = _github_create_tree(headers, owner, repo, base_tree_sha, tree_entries)
    new_commit_sha = _github_create_commit(headers, owner, repo, commit_message, new_tree_sha, latest_commit_sha)
    _github_update_branch_ref(headers, owner, repo, branch, new_commit_sha)

    return new_commit_sha


async def github_commit_files(files, commit_message):
    """Async wrapper so the blocking GitHub API calls don't block the bot's event loop."""
    return await asyncio.to_thread(_github_commit_files_sync, files, commit_message)


def _github_commit_changes_sync(write_files, delete_paths, commit_message):
    """
    Same atomic commit machinery as _github_commit_files_sync, but also
    supports deleting files in the same commit (used by lremovecard).
    write_files: dict of {repo_relative_path: bytes_content} to add/update.
    delete_paths: list of repo_relative_paths to remove from the tree.
    A tree entry with sha=None tells the GitHub Git Data API to drop that
    path from the resulting tree, so a write and a delete can land in the
    exact same commit -- the branch ref is only moved once, at the very
    end, so nothing is ever left half-applied.
    """
    token = os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GITHUB_USERNAME")
    repo = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH")

    missing = [
        name for name, val in [
            ("GITHUB_TOKEN", token),
            ("GITHUB_USERNAME", owner),
            ("GITHUB_REPO", repo),
            ("GITHUB_BRANCH", branch),
        ] if not val
    ]
    if missing:
        raise Exception(f"Missing required environment variable(s): {', '.join(missing)}")

    headers = _github_headers(token)

    latest_commit_sha = _github_get_branch_commit_sha(headers, owner, repo, branch)
    base_tree_sha = _github_get_commit_tree_sha(headers, owner, repo, latest_commit_sha)

    tree_entries = []

    for path, content_bytes in (write_files or {}).items():
        blob_sha = _github_create_blob(headers, owner, repo, content_bytes)
        tree_entries.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": blob_sha,
        })

    for path in (delete_paths or []):
        # DEBUG: log the exact repo-relative path being checked/deleted so
        # it's easy to verify the stored image path in cards.json matches
        # what's actually committed in the GitHub repo.
        print(f"[github_commit_changes] checking delete path: {path!r}")

        # GitHub returns an error if you try to delete a path that isn't
        # actually present in the tree. Rather than silently skipping it
        # (which would hide a real mismatch between cards.json and the
        # repo), fail loudly with the exact path so the root cause is
        # obvious instead of guessed at.
        if not _github_tree_contains_path(headers, owner, repo, base_tree_sha, path):
            raise Exception(f"Image '{path}' does not exist in the GitHub repository.")

        # Per GitHub's official Git Data API docs ("Create a tree"), every
        # tree entry -- including deletions -- requires path, mode, and
        # type. Setting sha to null is what marks the path for removal;
        # mode/type are NOT optional for deletions (omitting them causes
        # "422 Must supply a valid tree.mode", confirmed against GitHub's
        # documented examples and multiple independent client-library bug
        # reports). Regular files (like images) use mode 100644, type blob.
        tree_entries.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": None,
        })

    new_tree_sha = _github_create_tree(headers, owner, repo, base_tree_sha, tree_entries)
    new_commit_sha = _github_create_commit(headers, owner, repo, commit_message, new_tree_sha, latest_commit_sha)
    _github_update_branch_ref(headers, owner, repo, branch, new_commit_sha)

    return new_commit_sha


async def github_commit_changes(write_files, delete_paths, commit_message):
    """Async wrapper for _github_commit_changes_sync (write + delete in one atomic commit)."""
    return await asyncio.to_thread(_github_commit_changes_sync, write_files, delete_paths, commit_message)


def _github_get_file_sync(path: str):
    """
    Downloads a single file's raw bytes from the GitHub repo via the
    Contents API. Read-only; separate from the blob/tree/commit write
    machinery above and doesn't touch it. Returns None (never raises) if
    credentials aren't configured, the file doesn't exist in the repo, or
    the request fails for any reason (network issue, GitHub outage,
    etc.) -- callers fall back to local data in that case.
    """
    token = os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GITHUB_USERNAME")
    repo = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH")

    if not all([token, owner, repo, branch]):
        print(f"[github] Skipping download of {path!r}: missing GitHub environment variable(s).")
        return None

    try:
        headers = _github_headers(token)
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
        resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise Exception(f"Failed to download {path} ({resp.status_code}): {resp.text}")
        payload = resp.json()
        if payload.get("encoding") != "base64" or "content" not in payload:
            raise Exception(f"Unexpected response shape downloading {path}: {payload!r}")
        return base64.b64decode(payload["content"])
    except Exception:
        print(f"[github] Failed to download {path} from GitHub:")
        traceback.print_exc()
        return None


async def github_get_file(path: str):
    """Async wrapper so the blocking GitHub API call doesn't block the event loop."""
    return await asyncio.to_thread(_github_get_file_sync, path)


async def _sync_inventories_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory.

    IMPORTANT: local disk is now the preferred/authoritative source, not
    GitHub. This is a deliberate change from the immediate-sync design:
    now that GitHub uploads are debounced (see
    INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS), every mutation is written to
    inventories.json locally *immediately*, while the GitHub copy is
    only a periodic (at most once every interval) mirror of that. GitHub
    can therefore legitimately lag local disk by up to that interval at
    any given moment -- e.g. right after a crash/restart that happened
    before the next scheduled upload. Local disk is always >= GitHub in
    recency by construction, so preferring GitHub here would risk
    silently reverting confirmed, already-saved local changes.

        1. If local inventories.json is VALID -- see
           _load_inventories_json() for the exact criteria (exists,
           parses, is a dict) -- use it, even if it's an empty dict.
           An empty dict is a legitimate inventory set (e.g. a brand
           new bot with no claims yet), not a reason to fall back.
        2. Only if local is missing, unreadable, malformed, or not a
           dict -- which should only really happen on a brand-new
           deploy/container that has never written the file, or a
           corrupted file -- try downloading from GitHub as a backfill.
        3. If both are invalid/unavailable, start with an empty
           inventory set.

    Exactly one load; nothing polls, retries, or reloads after this.
    """
    local_inventories = _load_inventories_json()
    if local_inventories is not None:
        print("[inventories] Loaded inventories.json from local disk.")
        return local_inventories

    print("[inventories] Local inventories.json is missing/unreadable/malformed -- trying GitHub as a backfill.")
    remote_bytes = await github_get_file("inventories.json")

    if remote_bytes is None:
        print("[inventories] GitHub unavailable or has no inventories.json -- starting with empty inventories.")
        return {}

    try:
        remote_inventories = json.loads(remote_bytes.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("[inventories] Downloaded inventories.json from GitHub was not valid JSON -- starting with empty inventories.")
        traceback.print_exc()
        return {}

    try:
        _atomic_write_bytes("inventories.json", remote_bytes)
        print("[inventories] Local inventories.json was missing/empty; backfilled from GitHub.")
    except Exception:
        print("[inventories] Backfilled from GitHub in memory, but failed to write inventories.json locally:")
        traceback.print_exc()

    return remote_inventories


# `inventories` is the exact dict object defined in data.py, and every
# command in this file (get_inventory/add_card/remove_card/etc.) reads
# and writes that same object. We populate it in place -- rather than
# rebinding the name `inventories` to a new dict -- so it stays the
# single source of truth everywhere it's referenced. This is the one and
# only load into memory, done once here at startup, before the bot
# connects. Nothing later re-triggers it -- no polling, no retry loop.
inventories.clear()
inventories.update(asyncio.run(_sync_inventories_from_github_at_startup()))


# =========================
# SHOWCASE VOTES: GitHub persistence (mirrors inventories.json exactly)
# =========================
def _showcase_votes_json_bytes() -> bytes:
    """Serializes the current in-memory `showcase_votes` dict to JSON bytes."""
    return json.dumps(showcase_votes, indent=2).encode("utf-8")


def save_showcase_votes_local() -> None:
    """
    Writes the current in-memory `showcase_votes` dict to
    showcase_votes.json on disk, atomically (via _atomic_write_bytes).
    Called immediately after every successful vote add/remove -- same
    role as save_inventories_local() for inventories.
    """
    try:
        _atomic_write_bytes("showcase_votes.json", _showcase_votes_json_bytes())
    except Exception:
        print("[showcase_votes] Failed to save showcase_votes.json locally:")
        traceback.print_exc()
        raise


# Same interval as inventories -- reusing the constant directly (not a
# separate copy) so the two can never drift out of sync in timing.
_showcase_votes_sync_task = None
_showcase_votes_dirty = False
_showcase_votes_upload_in_progress = False


def mark_showcase_votes_dirty() -> None:
    """
    Marks that showcase_votes.json has local changes not yet pushed to
    GitHub. Must be called while holding showcase_votes_lock,
    immediately after a successful save_showcase_votes_local(). Mirrors
    mark_inventories_dirty() exactly.
    """
    global _showcase_votes_dirty
    _showcase_votes_dirty = True


async def showcase_votes_github_sync_loop():
    """
    Background task: wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS
    (the exact same interval inventories.json uses) and, only if
    showcase_votes.json has unpushed local changes, performs exactly one
    GitHub commit containing the current votes. Mirrors
    inventory_github_sync_loop() exactly -- same snapshot-inside-the-lock,
    push-outside-the-lock pattern, same dirty-flag-restore-on-failure
    behavior, reusing the identical github_commit_files() helper (no
    second GitHub system).
    """
    global _showcase_votes_dirty, _showcase_votes_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _showcase_votes_dirty or _showcase_votes_upload_in_progress:
            continue

        async with showcase_votes_lock:
            if not _showcase_votes_dirty:
                continue
            data = _showcase_votes_json_bytes()
            _showcase_votes_dirty = False

        _showcase_votes_upload_in_progress = True
        try:
            await github_commit_files({"showcase_votes.json": data}, "Batched showcase votes sync")
        except Exception:
            print("[showcase_votes] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with showcase_votes_lock:
                _showcase_votes_dirty = True
        finally:
            _showcase_votes_upload_in_progress = False


async def flush_showcase_votes_to_github() -> None:
    """
    Best-effort final push of any pending showcase vote changes to
    GitHub, called from Client.close() on graceful shutdown -- same
    reasoning and same mechanism as flush_inventories_to_github().
    """
    global _showcase_votes_dirty

    async with showcase_votes_lock:
        if not _showcase_votes_dirty:
            return
        data = _showcase_votes_json_bytes()
        _showcase_votes_dirty = False

    try:
        await github_commit_files({"showcase_votes.json": data}, "Final showcase votes sync (shutdown)")
        print("[showcase_votes] Flushed pending vote changes to GitHub before shutdown.")
    except Exception:
        async with showcase_votes_lock:
            _showcase_votes_dirty = True
        print("[showcase_votes] Failed to flush pending vote changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_showcase_votes_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory. Mirrors
    _sync_inventories_from_github_at_startup()'s exact priority order:
    local disk is authoritative if valid (every vote is written to disk
    immediately; GitHub is only ever a periodic mirror -- see
    showcase_votes_github_sync_loop above), so GitHub is only consulted
    as a backfill when local is missing, unreadable, malformed, or not a
    dict. Reuses the exact same GitHub helpers as inventories
    (github_get_file, github_commit_files) -- no second persistence
    system.

    Preserves the previous version's local-corruption safety net: if a
    local file exists but is invalid, it's backed up (never overwritten
    or deleted) before GitHub is even attempted.
    """
    local_votes = _load_showcase_votes_json()
    if local_votes is not None:
        print("[showcase_votes] Loaded showcase_votes.json from local disk.")
        return local_votes

    print("[showcase_votes] Local showcase_votes.json is missing/unreadable/malformed -- trying GitHub as a backfill.")

    if os.path.exists('showcase_votes.json'):
        backup_path = f"showcase_votes.json.corrupt-{int(time.time())}"
        try:
            with open('showcase_votes.json', 'rb') as src, open(backup_path, 'wb') as dst:
                dst.write(src.read())
            print(f"[showcase_votes] Backed up the invalid local file to '{backup_path}' before trying GitHub.")
        except Exception:
            print("[showcase_votes] Failed to back up the invalid local file (it was NOT modified or deleted):")
            traceback.print_exc()

    remote_bytes = await github_get_file("showcase_votes.json")

    if remote_bytes is None:
        print("[showcase_votes] GitHub unavailable or has no showcase_votes.json -- starting with empty votes.")
        if not os.path.exists('showcase_votes.json'):
            try:
                _atomic_write_bytes("showcase_votes.json", b"{}")
            except Exception:
                print("[showcase_votes] Failed to create showcase_votes.json:")
                traceback.print_exc()
        return {}

    try:
        remote_votes = json.loads(remote_bytes.decode("utf-8") or "{}")
        if not isinstance(remote_votes, dict):
            raise ValueError(f"expected a JSON object, got {type(remote_votes).__name__}")
    except Exception:
        print("[showcase_votes] Downloaded showcase_votes.json from GitHub was not a valid JSON object -- starting with empty votes.")
        traceback.print_exc()
        return {}

    try:
        _atomic_write_bytes("showcase_votes.json", remote_bytes)
        print("[showcase_votes] Local showcase_votes.json was missing/invalid; backfilled from GitHub.")
    except Exception:
        print("[showcase_votes] Backfilled from GitHub in memory, but failed to write showcase_votes.json locally:")
        traceback.print_exc()

    return remote_votes


showcase_votes = asyncio.run(_sync_showcase_votes_from_github_at_startup())
showcase_votes_lock = asyncio.Lock()


# =========================
# ABANDONED-INVENTORY RECOVERY (pending_recovery.json)
# =========================
# Reserved "owner" key for cards recovered from users who left the
# server and never returned within RECOVERY_PENDING_DAYS. Deliberately
# non-numeric (and double-underscored, so it reads unmistakably as a
# reserved internal slot rather than a real Discord user id -- it never
# collides with one) and lives in the exact same `inventories`
# dict/persistence pipeline as every other entry -- no separate storage
# system. This is not a Discord user; it's the bot's own holding
# inventory for recovered cards, for future giveaways/events.
SYSTEM_RECOVERY_USER = "__system__"

# How long a user can stay outside the server, after first being
# detected as gone, before their inventory is actually transferred.
RECOVERY_PENDING_DAYS = 15

# How often the background sweep re-checks everyone currently pending
# recovery (rejoined? still gone but under the window? window elapsed?).
# Daily is intentionally coarse -- a 15-day threshold doesn't need
# fine-grained polling, and this keeps fetch_member calls infrequent.
PENDING_RECOVERY_CHECK_INTERVAL_SECONDS = 86400


def _pending_recovery_json_bytes() -> bytes:
    """Serializes the current in-memory `pending_recovery` dict to JSON bytes."""
    return json.dumps(pending_recovery, indent=2).encode("utf-8")


def save_pending_recovery_local() -> None:
    """
    Writes the current in-memory `pending_recovery` dict to
    pending_recovery.json on disk, atomically. Called immediately after
    any change to the pending list (a user newly marked, a rejoin
    removing them, or a completed recovery removing them) -- same role
    as save_inventories_local()/save_showcase_votes_local().
    """
    try:
        _atomic_write_bytes("pending_recovery.json", _pending_recovery_json_bytes())
    except Exception:
        print("[recovery] Failed to save pending_recovery.json locally:")
        traceback.print_exc()
        raise


_pending_recovery_sync_task = None
_pending_recovery_check_task = None
_pending_recovery_dirty = False
_pending_recovery_upload_in_progress = False

# Guards the one-time immediate recovery sweep in Client.on_ready so a
# later on_ready (e.g. after a reconnect) never repeats it -- "run once
# at startup" should mean once per process, not once per connection.
_startup_recovery_sweep_done = False


def mark_pending_recovery_dirty() -> None:
    """Marks pending_recovery.json as having local changes not yet pushed
    to GitHub. Must be called while holding pending_recovery_lock,
    immediately after a successful save_pending_recovery_local()."""
    global _pending_recovery_dirty
    _pending_recovery_dirty = True


async def pending_recovery_github_sync_loop():
    """
    Background task: wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS
    (the same interval inventories.json/showcase_votes.json use) and,
    only if pending_recovery.json has unpushed local changes, performs
    exactly one GitHub commit. Mirrors inventory_github_sync_loop()
    exactly. This durability matters here specifically: the whole point
    of the 15-day countdown is to survive Railway restarts/redeploys --
    without it, every restart would silently forget when someone was
    first detected as gone.
    """
    global _pending_recovery_dirty, _pending_recovery_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _pending_recovery_dirty or _pending_recovery_upload_in_progress:
            continue

        async with pending_recovery_lock:
            if not _pending_recovery_dirty:
                continue
            data = _pending_recovery_json_bytes()
            _pending_recovery_dirty = False

        _pending_recovery_upload_in_progress = True
        try:
            await github_commit_files({"pending_recovery.json": data}, "Batched pending recovery sync")
        except Exception:
            print("[recovery] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with pending_recovery_lock:
                _pending_recovery_dirty = True
        finally:
            _pending_recovery_upload_in_progress = False


async def flush_pending_recovery_to_github() -> None:
    """Best-effort final push of pending_recovery.json to GitHub on
    graceful shutdown -- same mechanism as flush_inventories_to_github()
    /flush_showcase_votes_to_github()."""
    global _pending_recovery_dirty

    async with pending_recovery_lock:
        if not _pending_recovery_dirty:
            return
        data = _pending_recovery_json_bytes()
        _pending_recovery_dirty = False

    try:
        await github_commit_files({"pending_recovery.json": data}, "Final pending recovery sync (shutdown)")
        print("[recovery] Flushed pending recovery changes to GitHub before shutdown.")
    except Exception:
        async with pending_recovery_lock:
            _pending_recovery_dirty = True
        print("[recovery] Failed to flush pending recovery changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_pending_recovery_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory. Mirrors
    _sync_showcase_votes_from_github_at_startup()'s exact priority order
    and corrupt-file backup safety net -- local disk wins if valid,
    GitHub is only a backfill when local is missing/invalid.
    """
    local_data = _load_pending_recovery_json()
    if local_data is not None:
        print("[recovery] Loaded pending_recovery.json from local disk.")
        return local_data

    print("[recovery] Local pending_recovery.json is missing/unreadable/malformed -- trying GitHub as a backfill.")

    if os.path.exists('pending_recovery.json'):
        backup_path = f"pending_recovery.json.corrupt-{int(time.time())}"
        try:
            with open('pending_recovery.json', 'rb') as src, open(backup_path, 'wb') as dst:
                dst.write(src.read())
            print(f"[recovery] Backed up the invalid local file to '{backup_path}' before trying GitHub.")
        except Exception:
            print("[recovery] Failed to back up the invalid local file (it was NOT modified or deleted):")
            traceback.print_exc()

    remote_bytes = await github_get_file("pending_recovery.json")

    if remote_bytes is None:
        print("[recovery] GitHub unavailable or has no pending_recovery.json -- starting with an empty pending list.")
        if not os.path.exists('pending_recovery.json'):
            try:
                _atomic_write_bytes("pending_recovery.json", b"{}")
            except Exception:
                print("[recovery] Failed to create pending_recovery.json:")
                traceback.print_exc()
        return {}

    try:
        remote_data = json.loads(remote_bytes.decode("utf-8") or "{}")
        if not isinstance(remote_data, dict):
            raise ValueError(f"expected a JSON object, got {type(remote_data).__name__}")
    except Exception:
        print("[recovery] Downloaded pending_recovery.json from GitHub was not a valid JSON object -- starting with an empty pending list.")
        traceback.print_exc()
        return {}

    try:
        _atomic_write_bytes("pending_recovery.json", remote_bytes)
        print("[recovery] Local pending_recovery.json was missing/invalid; backfilled from GitHub.")
    except Exception:
        print("[recovery] Backfilled from GitHub in memory, but failed to write pending_recovery.json locally:")
        traceback.print_exc()

    return remote_data


pending_recovery = asyncio.run(_sync_pending_recovery_from_github_at_startup())
pending_recovery_lock = asyncio.Lock()


# =========================
# RECYCLABLE CARDS POOL (stored inside pending_recovery.json)
# =========================
# Reserved key inside the SAME `pending_recovery` dict every departed-
# user recovery timer already lives in -- same "non-numeric key, never
# collides with a real Discord user id" convention used throughout this
# file (e.g. SYSTEM_RECOVERY_USER in `inventories`). No second
# persistence system: this reuses pending_recovery_lock,
# save_pending_recovery_local(), and mark_pending_recovery_dirty()
# exactly as they already exist. Safe to add -- nothing iterates over
# pending_recovery.items()/.values()/.keys() assuming every value is a
# timestamp except _run_pending_recovery_sweep, which already guards
# each key with `int(user_key)` inside a try/except ValueError: continue,
# so a non-numeric key like this one is silently skipped there exactly
# like any other bad/foreign key would be.
#
# Shape: pending_recovery["__recyclable_cards__"] = [ { "id": <uuid>,
#   "card_id": <the underlying card's id>, "print": <its original print
#   number>, "card": <a full snapshot of the card dict at removal time>,
#   "removed_from": <original owner's user id, str>, "removed_at":
#   <unix ts>, "recycled_active": bool }, ... ]
#
# "recycled_active" distinguishes cards merely SITTING in the pool
# (removed by `lresetinventories`, not yet chosen) from ones
# `lrecyclecards` has explicitly activated -- only active entries are
# ever folded into get_weighted_card()'s drop pool.
RECYCLABLE_CARDS_KEY = "__recyclable_cards__"


def get_recyclable_pool() -> list:
    """Read-only: the full recyclable-cards pool (active and inactive
    alike). Never creates the key as a side effect of reading it."""
    return pending_recovery.get(RECYCLABLE_CARDS_KEY, [])


def get_active_recycled_entries() -> list:
    """Read-only: only the currently-ACTIVE recycled entries (added to
    the pool by `lresetinventories`, then explicitly activated by
    `lrecyclecards`) -- these, and only these, are folded into
    get_weighted_card()'s drop pool. An entry merely sitting in the
    pool that nobody has recycled yet is never droppable."""
    return [e for e in get_recyclable_pool() if e.get("recycled_active")]


async def consume_recycled_entry(entry_id):
    """
    Atomically removes ONE specific recycled-card entry (by id) from
    the pool, if it's still present AND still active -- called at claim
    time (see CardView.claim()) once a recycled drop is actually being
    granted, so the exact same print can never be handed out twice.
    Returns the full removed entry dict on success (so a caller can put
    it back byte-for-byte if the claim it was spent on ends up failing),
    or None if it's already gone (e.g. a concurrent drop's claim on the
    same entry won the race first -- recycled prints aren't reserved at
    drop time, same as a normal print's peek-only preview number isn't
    either).

    Uses the exact same pending_recovery_lock/
    save_pending_recovery_local()/mark_pending_recovery_dirty()
    pipeline every other pending_recovery mutation already uses. On a
    save failure, the entry is put back exactly where it was, so a
    failed persist can never silently make a still-valid recycled print
    vanish from the pool.
    """
    async with pending_recovery_lock:
        pool = pending_recovery.get(RECYCLABLE_CARDS_KEY, [])
        index = next(
            (i for i, e in enumerate(pool) if e.get("id") == entry_id and e.get("recycled_active")),
            None
        )
        if index is None:
            return None

        entry = pool.pop(index)
        try:
            save_pending_recovery_local()
            mark_pending_recovery_dirty()
        except Exception:
            pool.insert(index, entry)
            print(f"[recycle] Failed to persist consuming recycled entry {entry_id}:")
            traceback.print_exc()
            return None

        return entry


def _compute_duplicate_fix_plan() -> dict:
    """
    Computes the full `lfixduplicates` repair plan fresh from the
    CURRENT live `inventories` + recyclable pool state. Pure read --
    never mutates anything itself. Called both for the command's
    preview embed and again at confirm time, so a stale preview can
    never be blindly replayed against data that's changed in between.

    Rules (see LFIXDUPLICATES COMMAND):
      - Self-duplicates (the same user owns the identical (card_id,
        print) twice) and live/live duplicates between two different
        users are handled by the exact same rule: every owner of that
        exact (card_id, print) is sorted by claimed_at; the earliest
        keeps the number, every later copy gets a fresh one.
      - Pool/live overlaps (a pool entry's (card_id, print) also exists
        in someone's live inventory right now): the live owner's print
        is left completely untouched; only the pool entry itself is
        renumbered.
      - Every fresh number handed out is strictly higher than the
        current true max for that card_id across BOTH live inventories
        and the ENTIRE pool (active and inactive alike), and higher
        than every other fresh number already assigned to that same
        card_id earlier in this same pass -- so nothing produced here
        can ever collide with live data, pool data, or itself, and
        normal future progression (get_next_print()) is preserved.

    Returns:
      {
        "live_ops": [ {"user_id", "idx", "card_id", "old_print",
                        "new_print", "kind" ("self_duplicate" or
                        "cross_user_duplicate"), "kept_user_id"}, ... ],
        "pool_ops": [ {"entry_id", "card_id", "old_print", "new_print"}, ... ],
        "self_count": int, "cross_count": int, "overlap_count": int,
      }
    """
    live_index = defaultdict(list)
    for user_id, owned_cards in inventories.items():
        for idx, owned_card in enumerate(owned_cards):
            card_id = owned_card.get("card", {}).get("id")
            print_num = owned_card.get("print")
            if card_id is None or not isinstance(print_num, int):
                continue
            live_index[(card_id, print_num)].append({
                "user_id": user_id,
                "idx": idx,
                "claimed_at": owned_card.get("claimed_at") or 0,
            })

    pool = get_recyclable_pool()

    # Seeded from the TRUE current max across live + the entire pool,
    # then incremented once per assignment below -- so every number
    # handed out is unique against everything that already exists, and
    # against every other number this same pass hands out.
    max_print = defaultdict(int)
    for (card_id, print_num) in live_index.keys():
        if print_num > max_print[card_id]:
            max_print[card_id] = print_num
    for entry in pool:
        card_id = entry.get("card_id")
        print_num = entry.get("print")
        if card_id is not None and isinstance(print_num, int) and print_num > max_print[card_id]:
            max_print[card_id] = print_num

    live_ops = []
    self_count = 0
    cross_count = 0
    for (card_id, print_num), occurrences in live_index.items():
        if len(occurrences) <= 1:
            continue
        occurrences_sorted = sorted(occurrences, key=lambda o: o["claimed_at"])
        keep = occurrences_sorted[0]
        is_self = len(set(o["user_id"] for o in occurrences)) == 1
        for dup in occurrences_sorted[1:]:
            max_print[card_id] += 1
            live_ops.append({
                "user_id": dup["user_id"],
                "idx": dup["idx"],
                "card_id": card_id,
                "old_print": print_num,
                "new_print": max_print[card_id],
                "kind": "self_duplicate" if is_self else "cross_user_duplicate",
                "kept_user_id": keep["user_id"],
            })
            if is_self:
                self_count += 1
            else:
                cross_count += 1

    pool_ops = []
    for entry in pool:
        card_id = entry.get("card_id")
        print_num = entry.get("print")
        if card_id is None or not isinstance(print_num, int):
            continue
        if (card_id, print_num) in live_index:
            max_print[card_id] += 1
            pool_ops.append({
                "entry_id": entry.get("id"),
                "card_id": card_id,
                "old_print": print_num,
                "new_print": max_print[card_id],
            })

    return {
        "live_ops": live_ops,
        "pool_ops": pool_ops,
        "self_count": self_count,
        "cross_count": cross_count,
        "overlap_count": len(pool_ops),
    }


# =========================
# MAIL (mail.json)
# =========================
# In-app mail, sent between users with `lmail @user` and read with
# `lmail`. Keyed by receiver ID (str) -> list of letter dicts, so a
# user's mailbox is just mail[str(user_id)], the same
# setdefault-a-list-per-user shape get_inventory() already uses for
# `inventories`. Persisted/synced exactly the way inventories.json,
# showcase_votes.json, pending_recovery.json, and merchants.json are
# (local file authoritative, atomic writes, own lock, debounced GitHub
# mirror, best-effort flush on shutdown) -- no new persistence system,
# just the existing one reused for a new piece of state.
#
# Each letter dict:
#   id:          str(uuid4()), unique per letter -- lets the "Read"
#                button target the exact letter being viewed even
#                though pages are just a list index.
#   sender_id:   str(sender's Discord user ID)
#   receiver_id: str(receiver's Discord user ID)
#   timestamp:   float, time.time() when the letter was sent
#   message:     str, the letter's body
#   read:        bool, False until the receiver presses "Read"

def _load_mail_json():
    """
    Loads and validates the local mail.json. Same contract, same
    reasoning, as _load_inventories_json()/_load_pending_recovery_json()
    -- "missing" and "invalid" are both real, distinct situations, never
    silently collapsed into "just start empty" without at least a log
    line.

    Returns the parsed dict (possibly a legitimately empty {}) if valid,
    or None if missing/unreadable/malformed/not a dict.
    """
    try:
        with open('mail.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[mail] Failed to read mail.json:")
        traceback.print_exc()
        return None

    if not raw:
        print("[mail] mail.json is empty (not even '{}') -- treating as invalid.")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[mail] mail.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        print(f"[mail] mail.json did not contain a JSON object "
              f"(got {type(parsed).__name__}) -- treating as invalid.")
        return None

    return parsed


def _mail_json_bytes() -> bytes:
    """Serializes the current in-memory `mail` dict to JSON bytes."""
    return json.dumps(mail, indent=2).encode("utf-8")


def save_mail_local() -> None:
    """
    Writes the current in-memory `mail` dict to mail.json on disk,
    atomically. Called immediately after any change (a new letter sent,
    or a letter marked read) -- same role as
    save_inventories_local()/save_pending_recovery_local().
    """
    try:
        _atomic_write_bytes("mail.json", _mail_json_bytes())
    except Exception:
        print("[mail] Failed to save mail.json locally:")
        traceback.print_exc()
        raise


_mail_sync_task = None
_mail_dirty = False
_mail_upload_in_progress = False


def mark_mail_dirty() -> None:
    """Marks mail.json as having local changes not yet pushed to
    GitHub. Must be called while holding mail_lock, immediately after a
    successful save_mail_local()."""
    global _mail_dirty
    _mail_dirty = True


async def mail_github_sync_loop():
    """
    Background task: wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS
    and, only if mail.json has unpushed local changes, performs exactly
    one GitHub commit. Mirrors inventory_github_sync_loop()/
    pending_recovery_github_sync_loop() exactly.
    """
    global _mail_dirty, _mail_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _mail_dirty or _mail_upload_in_progress:
            continue

        async with mail_lock:
            if not _mail_dirty:
                continue
            data = _mail_json_bytes()
            _mail_dirty = False

        _mail_upload_in_progress = True
        try:
            await github_commit_files({"mail.json": data}, "Batched mail sync")
        except Exception:
            print("[mail] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with mail_lock:
                _mail_dirty = True
        finally:
            _mail_upload_in_progress = False


async def flush_mail_to_github() -> None:
    """Best-effort final push of mail.json to GitHub on graceful
    shutdown -- same mechanism as flush_inventories_to_github()/
    flush_pending_recovery_to_github()."""
    global _mail_dirty

    async with mail_lock:
        if not _mail_dirty:
            return
        data = _mail_json_bytes()
        _mail_dirty = False

    try:
        await github_commit_files({"mail.json": data}, "Final mail sync (shutdown)")
        print("[mail] Flushed mail changes to GitHub before shutdown.")
    except Exception:
        async with mail_lock:
            _mail_dirty = True
        print("[mail] Failed to flush mail changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_mail_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory. Mirrors
    _sync_pending_recovery_from_github_at_startup()'s exact priority
    order and corrupt-file backup safety net -- local disk wins if
    valid, GitHub is only a backfill when local is missing/invalid.
    """
    local_data = _load_mail_json()
    if local_data is not None:
        print("[mail] Loaded mail.json from local disk.")
        return local_data

    print("[mail] Local mail.json is missing/unreadable/malformed -- trying GitHub as a backfill.")

    if os.path.exists('mail.json'):
        backup_path = f"mail.json.corrupt-{int(time.time())}"
        try:
            with open('mail.json', 'rb') as src, open(backup_path, 'wb') as dst:
                dst.write(src.read())
            print(f"[mail] Backed up the invalid local file to '{backup_path}' before trying GitHub.")
        except Exception:
            print("[mail] Failed to back up the invalid local file (it was NOT modified or deleted):")
            traceback.print_exc()

    remote_bytes = await github_get_file("mail.json")

    if remote_bytes is None:
        print("[mail] GitHub unavailable or has no mail.json -- starting with empty mail.")
        if not os.path.exists('mail.json'):
            try:
                _atomic_write_bytes("mail.json", b"{}")
            except Exception:
                print("[mail] Failed to create mail.json:")
                traceback.print_exc()
        return {}

    try:
        remote_data = json.loads(remote_bytes.decode("utf-8") or "{}")
        if not isinstance(remote_data, dict):
            raise ValueError(f"expected a JSON object, got {type(remote_data).__name__}")
    except Exception:
        print("[mail] Downloaded mail.json from GitHub was not a valid JSON object -- starting with empty mail.")
        traceback.print_exc()
        return {}

    try:
        _atomic_write_bytes("mail.json", remote_bytes)
        print("[mail] Local mail.json was missing/invalid; backfilled from GitHub.")
    except Exception:
        print("[mail] Backfilled from GitHub in memory, but failed to write mail.json locally:")
        traceback.print_exc()

    return remote_data


mail = asyncio.run(_sync_mail_from_github_at_startup())
mail_lock = asyncio.Lock()


def get_mailbox(user_id) -> list:
    """Safely fetches or initializes a user's mailbox (received mail
    only) -- same setdefault-a-list-per-user shape as get_inventory()."""
    return mail.setdefault(str(user_id), [])


def has_unread_mail(user_id) -> bool:
    """Whether user_id currently has at least one unread letter."""
    return any(not letter.get("read") for letter in get_mailbox(user_id))


def unread_mail_count(user_id) -> int:
    """How many unread letters user_id currently has."""
    return sum(1 for letter in get_mailbox(user_id) if not letter.get("read"))


def mark_letter_read(user_id, letter_id) -> bool:
    """
    Marks the given letter (by id) in user_id's mailbox as read, in
    memory. Returns True if a matching, currently-unread letter was
    found and updated; False if it was already read or no longer
    exists (e.g. a stale button on an old message).
    """
    for letter in get_mailbox(user_id):
        if letter.get("id") == letter_id:
            if letter.get("read"):
                return False
            letter["read"] = True
            return True
    return False


# =========================
# MAIL BLOCKING (stored inside mail.json, no second persistence system)
# =========================
# Reserved key inside the same `mail` dict every mailbox already lives
# in -- same "double-underscore, deliberately non-numeric, never
# collides with a real Discord user id" convention as
# SYSTEM_RECOVERY_USER in `inventories`. Nothing iterates over
# mail.items()/.values()/.keys() anywhere in this file (only ever
# mail[str(user_id)]/mail.setdefault(str(user_id), ...) for a specific
# user), so adding this one extra key is safe and doesn't disturb any
# existing mailbox lookup.
#
# Shape: mail["__blocked__"] = { receiver_id (str): [sender_id (str), ...] }
# i.e. "who has this receiver blocked". One-way by construction: only
# ever consulted as "is sender_id in receiver's blocked list", never
# the reverse, so a block never restricts the blocker's own outgoing
# mail to that person.
MAIL_BLOCKED_KEY = "__blocked__"


def get_blocked_senders(user_id) -> list:
    """Safely fetches or initializes user_id's list of blocked sender
    IDs -- same setdefault-a-list-per-user shape as get_mailbox(), just
    nested one level under the reserved MAIL_BLOCKED_KEY instead of at
    the top level of `mail`."""
    blocked_map = mail.setdefault(MAIL_BLOCKED_KEY, {})
    return blocked_map.setdefault(str(user_id), [])


def is_sender_blocked(sender_id, receiver_id) -> bool:
    """Whether receiver_id has blocked sender_id from sending them
    mail. Read-only -- does not create an entry for receiver_id if
    they've never blocked anyone."""
    blocked_map = mail.get(MAIL_BLOCKED_KEY, {})
    return str(sender_id) in blocked_map.get(str(receiver_id), [])


def block_sender(receiver_id, sender_id) -> bool:
    """
    Adds sender_id to receiver_id's blocked-senders list, in memory.
    One-way: only ever affects mail FROM sender_id TO receiver_id --
    never touches receiver_id's own ability to mail sender_id back.
    Returns True if this newly blocked them, False if they were
    already blocked (so the caller can show an accurate message either
    way).
    """
    blocked = get_blocked_senders(receiver_id)
    sender_key = str(sender_id)
    if sender_key in blocked:
        return False
    blocked.append(sender_key)
    return True


def unblock_sender(receiver_id, sender_id) -> bool:
    """
    Removes sender_id from receiver_id's blocked-senders list, in
    memory. Mirrors block_sender() exactly, in reverse. Returns True if
    this newly unblocked them, False if they weren't blocked to begin
    with (so the caller can show an accurate message either way).
    """
    blocked = get_blocked_senders(receiver_id)
    sender_key = str(sender_id)
    if sender_key not in blocked:
        return False
    blocked.remove(sender_key)
    return True


async def _resolve_mail_sender_info(client, letters: list) -> list:
    """
    Best-effort resolves each letter's sender into a display name and
    avatar URL for the mailbox paginator. Returns shallow copies of the
    letter dicts with `_sender_name`/`_sender_avatar` attached -- the
    real letters in `mail` (and mail.json) are never touched, so this
    is purely a display-time enrichment step. Caches lookups per sender
    ID within a single call so a mailbox with many letters from the
    same person doesn't refetch them repeatedly.
    """
    resolved_cache = {}
    enriched = []
    for letter in letters:
        sender_id = letter.get("sender_id")
        if sender_id not in resolved_cache:
            user_obj = None
            try:
                if sender_id and sender_id.isdigit():
                    user_obj = client.get_user(int(sender_id))
                    if user_obj is None:
                        user_obj = await client.fetch_user(int(sender_id))
            except Exception:
                user_obj = None
            resolved_cache[sender_id] = user_obj

        user_obj = resolved_cache.get(sender_id)
        enriched_letter = dict(letter)
        enriched_letter["_sender_name"] = user_obj.display_name if user_obj else "Unknown user"
        enriched_letter["_sender_avatar"] = user_obj.display_avatar.url if user_obj else None
        enriched.append(enriched_letter)
    return enriched


async def _run_mail_sending_flow(bot, channel, sender, target_user) -> None:
    """
    Shared "type your message, next message becomes the mail" flow.
    Used by both `lmail @user` and each mail page's Reply button (which
    targets that letter's original sender) -- exactly one implementation
    of send+persist, no duplicated logic between the two entry points.

    Prompts via channel.send (works identically whether the caller is a
    plain text command or a button interaction), waits for `sender`'s
    next message in that same channel, then persists it with the same
    letter shape and the same mail_lock/save_mail_local/mark_mail_dirty/
    rollback-on-failed-save sequence the original inline `lmail @user`
    flow used.
    """
    # One-way block check: if target_user has blocked sender, reject
    # before even prompting for a message. This is the single choke
    # point both `lmail @user` and every mail page's Reply button go
    # through, so the block is enforced everywhere mail can be sent,
    # with no separate check needed at either call site. Blocking is
    # one-way by construction (see is_sender_blocked/block_sender) --
    # this never affects sender's ability to receive mail FROM
    # target_user.
    if is_sender_blocked(sender.id, target_user.id):
        await channel.send(
            f"You can't send mail to **{target_user.display_name}** -- they aren't accepting mail from you."
        )
        return

    await channel.send(
        f"What would you like to send to **{target_user.display_name}**? "
        "Type your message now."
    )

    def check(m):
        return m.author.id == sender.id and m.channel.id == channel.id

    try:
        mail_msg = await bot.wait_for("message", check=check, timeout=180)
    except asyncio.TimeoutError:
        await channel.send("Timed out waiting for your mail message.")
        return

    mail_content = mail_msg.content.strip()
    if not mail_content:
        await reply(
            mail_msg,
            "Mail message can't be empty. Please try again."
        )
        return

    letter = {
        "id": str(uuid.uuid4()),
        "sender_id": str(sender.id),
        "receiver_id": str(target_user.id),
        "timestamp": time.time(),
        "message": mail_content,
        "read": False,
    }

    async with mail_lock:
        mailbox = get_mailbox(target_user.id)
        mailbox.append(letter)
        try:
            save_mail_local()
            mark_mail_dirty()
        except Exception:
            mailbox.remove(letter)
            await reply(
                mail_msg,
                "Something went wrong saving your mail. Please try again."
            )
            return

    await reply(mail_msg, f"Mail sent to **{target_user.display_name}**!")


def _looks_like_bot_command(content_lower: str) -> bool:
    """
    Whether a message looks like an attempt to run one of this bot's
    commands -- every single command handled in on_message (lbadges,
    lprogress, lmissing, ldrop, etc.) starts with a lowercase 'l'
    followed by more letters, so that's the one shared signal available
    without hand-maintaining a duplicate list of command names here.
    Used only to decide when to check for/display the unread mail
    reminder -- it never gates any actual command logic below.
    """
    if not content_lower:
        return False
    first_word = content_lower.split(maxsplit=1)[0]
    return len(first_word) > 1 and first_word[0] == "l" and first_word[1].isalpha()


# How often (at most) a single user can be shown the unread-mail
# reminder below. Purely an in-memory rate limit on a repeated
# notification -- not real persistent state, so it deliberately does
# NOT go through the local-first/GitHub persistence system the way
# mail.json itself does; losing it on a restart just means the next
# qualifying command shows the reminder again immediately, which is
# harmless.
MAIL_REMINDER_COOLDOWN_SECONDS = 600  # 10 minutes

# user_id -> unix timestamp the reminder was last actually shown to
# them. Same plain in-memory dict, no lock, as the existing
# drop_cooldowns/claim_cooldowns.
_last_mail_reminder_at = {}


# =========================
# DUO CHALLENGES (duo.json)
# =========================
# State for `lduo @user`: pending-invite -> shared challenge -> weekly
# limits/cooldowns -> bonus drop/claim rewards. Persisted/synced exactly
# the way mail.json/inventories.json/pending_recovery.json are (local
# file authoritative, atomic writes, own lock, debounced GitHub mirror,
# best-effort flush on shutdown) -- no new persistence system, just the
# existing one reused for a new piece of state.
#
# duo = {
#   "active": { challenge_id: { id, player_a, player_b, type, target,
#                                label, progress, series_seen?,
#                                characters_seen?, started_at,
#                                channel_id } },
#   "weekly": { user_id: [ {timestamp, partner_id}, ... ] },  # completions,
#                                                              # trimmed to
#                                                              # the last
#                                                              # DUO_WEEKLY_WINDOW_SECONDS
#   "cooldowns": { user_id: timestamp_of_last_completion },
#   "bonus": { user_id: {"drop": int, "claim": int} },
# }
#
# NOTE ON SCOPE: only claim-based challenge types are implemented (see
# DUO_CHALLENGE_POOL below). A trade-based challenge type was
# deliberately left out, since tracking it would require adding a hook
# into the trading system, which this task explicitly says not to
# modify. The only two touch points added outside this block are a
# single best-effort progress-recording call in CardView.claim (after
# a claim has already fully succeeded) and the bonus-drop/bonus-claim
# consumption checks in `ld`/CardView.claim -- both required for this
# feature to function at all, and neither changes any existing
# drop/claim behavior, odds, or output.

DUO_WEEKLY_LIMIT = 3
DUO_WEEKLY_WINDOW_SECONDS = 7 * 24 * 3600
DUO_COOLDOWN_SECONDS = 2 * 24 * 3600

DUO_CHALLENGE_POOL = [
    {"type": "claim_count", "target": 60, "label": "Claim {target} cards together"},
    {"type": "claim_rarity4", "target": 10, "label": "Collect {target} ★★★★ cards together"},
    {"type": "claim_series", "target": 15, "label": "Claim cards from {target} different series together"},
    {"type": "claim_characters", "target": 20, "label": "Obtain {target} unique characters together"},
]


def _load_duo_json():
    """
    Loads and validates the local duo.json. Same contract, same
    reasoning, as _load_mail_json()/_load_inventories_json() -- "missing"
    and "invalid" are both real, distinct situations, never silently
    collapsed into "just start empty" without at least a log line.

    Returns the parsed dict if valid, or None if
    missing/unreadable/malformed/not a dict.
    """
    try:
        with open('duo.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[duo] Failed to read duo.json:")
        traceback.print_exc()
        return None

    if not raw:
        print("[duo] duo.json is empty (not even '{}') -- treating as invalid.")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[duo] duo.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        print(f"[duo] duo.json did not contain a JSON object "
              f"(got {type(parsed).__name__}) -- treating as invalid.")
        return None

    return parsed


def _duo_default_state() -> dict:
    return {"active": {}, "weekly": {}, "cooldowns": {}, "bonus": {}, "migrations": {}}


def _normalize_duo_state(data: dict) -> dict:
    """Fills in any top-level keys missing from a loaded/downloaded
    duo.json (e.g. an older file from before a section existed) without
    discarding whatever it did have."""
    defaults = _duo_default_state()
    for key, default_value in defaults.items():
        if key not in data or not isinstance(data[key], dict):
            data[key] = default_value
    return data


def _duo_json_bytes() -> bytes:
    """Serializes the current in-memory `duo` dict to JSON bytes."""
    return json.dumps(duo, indent=2).encode("utf-8")


def save_duo_local() -> None:
    """
    Writes the current in-memory `duo` dict to duo.json on disk,
    atomically. Called immediately after any change (new active
    challenge, progress update, completion, bonus consumed/granted) --
    same role as save_mail_local()/save_inventories_local().
    """
    try:
        _atomic_write_bytes("duo.json", _duo_json_bytes())
    except Exception:
        print("[duo] Failed to save duo.json locally:")
        traceback.print_exc()
        raise


_duo_sync_task = None
_duo_dirty = False
_duo_upload_in_progress = False


def mark_duo_dirty() -> None:
    """Marks duo.json as having local changes not yet pushed to GitHub.
    Must be called while holding duo_lock, immediately after a
    successful save_duo_local()."""
    global _duo_dirty
    _duo_dirty = True


async def duo_github_sync_loop():
    """
    Background task: wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS
    and, only if duo.json has unpushed local changes, performs exactly
    one GitHub commit. Mirrors mail_github_sync_loop()/
    pending_recovery_github_sync_loop() exactly.
    """
    global _duo_dirty, _duo_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _duo_dirty or _duo_upload_in_progress:
            continue

        async with duo_lock:
            if not _duo_dirty:
                continue
            data = _duo_json_bytes()
            _duo_dirty = False

        _duo_upload_in_progress = True
        try:
            await github_commit_files({"duo.json": data}, "Batched duo sync")
        except Exception:
            print("[duo] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with duo_lock:
                _duo_dirty = True
        finally:
            _duo_upload_in_progress = False


async def flush_duo_to_github() -> None:
    """Best-effort final push of duo.json to GitHub on graceful shutdown
    -- same mechanism as flush_mail_to_github()/
    flush_pending_recovery_to_github()."""
    global _duo_dirty

    async with duo_lock:
        if not _duo_dirty:
            return
        data = _duo_json_bytes()
        _duo_dirty = False

    try:
        await github_commit_files({"duo.json": data}, "Final duo sync (shutdown)")
        print("[duo] Flushed duo changes to GitHub before shutdown.")
    except Exception:
        async with duo_lock:
            _duo_dirty = True
        print("[duo] Failed to flush duo changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_duo_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory. Mirrors
    _sync_mail_from_github_at_startup()'s exact priority order and
    corrupt-file backup safety net -- local disk wins if valid, GitHub
    is only a backfill when local is missing/invalid. Active challenges
    survive a restart because of this -- they're just whatever was last
    saved to duo.json.
    """
    local_data = _load_duo_json()
    if local_data is not None:
        print("[duo] Loaded duo.json from local disk.")
        return _normalize_duo_state(local_data)

    print("[duo] Local duo.json is missing/unreadable/malformed -- trying GitHub as a backfill.")

    if os.path.exists('duo.json'):
        backup_path = f"duo.json.corrupt-{int(time.time())}"
        try:
            with open('duo.json', 'rb') as src, open(backup_path, 'wb') as dst:
                dst.write(src.read())
            print(f"[duo] Backed up the invalid local file to '{backup_path}' before trying GitHub.")
        except Exception:
            print("[duo] Failed to back up the invalid local file (it was NOT modified or deleted):")
            traceback.print_exc()

    remote_bytes = await github_get_file("duo.json")

    if remote_bytes is None:
        print("[duo] GitHub unavailable or has no duo.json -- starting with empty duo state.")
        default_state = _duo_default_state()
        if not os.path.exists('duo.json'):
            try:
                _atomic_write_bytes("duo.json", json.dumps(default_state, indent=2).encode("utf-8"))
            except Exception:
                print("[duo] Failed to create duo.json:")
                traceback.print_exc()
        return default_state

    try:
        remote_data = json.loads(remote_bytes.decode("utf-8") or "{}")
        if not isinstance(remote_data, dict):
            raise ValueError(f"expected a JSON object, got {type(remote_data).__name__}")
    except Exception:
        print("[duo] Downloaded duo.json from GitHub was not a valid JSON object -- starting with empty duo state.")
        traceback.print_exc()
        return _duo_default_state()

    try:
        _atomic_write_bytes("duo.json", remote_bytes)
        print("[duo] Local duo.json was missing/invalid; backfilled from GitHub.")
    except Exception:
        print("[duo] Backfilled from GitHub in memory, but failed to write duo.json locally:")
        traceback.print_exc()

    return _normalize_duo_state(remote_data)


duo = asyncio.run(_sync_duo_from_github_at_startup())
duo_lock = asyncio.Lock()


# ---- Duo: bonus drop/claim helpers -----------------------------------

def get_bonus(user_id) -> dict:
    """Safely fetches or initializes a user's bonus-use counters."""
    return duo["bonus"].setdefault(str(user_id), {"drop": 0, "claim": 0})


def add_bonus(user_id, kind: str, amount: int = 1) -> None:
    """Grants `amount` additional bonus uses of `kind` ('drop' or
    'claim') to user_id -- purely additive, on top of the existing
    cooldown system, never replacing it."""
    bonus = get_bonus(user_id)
    bonus[kind] = bonus.get(kind, 0) + amount


def consume_bonus(user_id, kind: str) -> bool:
    """Spends one bonus use of `kind` if available. Returns True if one
    was spent, False if none were banked."""
    bonus = get_bonus(user_id)
    if bonus.get(kind, 0) > 0:
        bonus[kind] -= 1
        return True
    return False


# ---- One-time migration: launch bonus for existing players ------------

EXTRA_BONUS_MIGRATION_ID = "extra_bonus_launch_2026_08"
EXTRA_BONUS_MIGRATION_AMOUNT = 5


async def _run_extra_bonus_migration_once() -> None:
    """
    One-time migration: grants EXTRA_BONUS_MIGRATION_AMOUNT bonus drops
    and claims (via the existing add_bonus() helper, same storage as
    normal bonus drops/claims) to every user_id already present in
    `inventories` at the moment this runs. Guarded by
    EXTRA_BONUS_MIGRATION_ID in duo["migrations"], so this can only ever
    apply once regardless of restarts, and any player created after it
    has already run is never touched.
    """
    async with duo_lock:
        if duo["migrations"].get(EXTRA_BONUS_MIGRATION_ID):
            return

        for user_id in list(inventories.keys()):
            add_bonus(user_id, "drop", EXTRA_BONUS_MIGRATION_AMOUNT)
            add_bonus(user_id, "claim", EXTRA_BONUS_MIGRATION_AMOUNT)

        duo["migrations"][EXTRA_BONUS_MIGRATION_ID] = True

        try:
            save_duo_local()
            mark_duo_dirty()
        except Exception:
            print("[duo] Failed to save the one-time extra bonus migration:")
            traceback.print_exc()
            raise


# ---- Duo: weekly limit / cooldown helpers -----------------------------

def _duo_recent_completions(user_id, now=None) -> list:
    """This user's Duo completions within the last
    DUO_WEEKLY_WINDOW_SECONDS -- the rolling window that "weekly limits
    reset automatically every week" is implemented as, so it's always
    correct with no separate reset job needed."""
    now = now if now is not None else time.time()
    entries = duo["weekly"].get(str(user_id), [])
    return [e for e in entries if now - e.get("timestamp", 0) < DUO_WEEKLY_WINDOW_SECONDS]


def duo_weekly_count(user_id) -> int:
    """How many Duo challenges user_id has completed in the last 7 days."""
    return len(_duo_recent_completions(user_id))


def duo_weekly_partners(user_id) -> set:
    """The set of partner IDs (str) user_id has already completed a Duo
    with in the last 7 days -- "each completed Duo must be with a
    different player" is enforced against this set."""
    return {e.get("partner_id") for e in _duo_recent_completions(user_id)}


def duo_cooldown_remaining(user_id) -> int:
    """Seconds left on user_id's post-completion Duo cooldown (0 if
    none/expired) -- same dict[user_id]=timestamp cooldown architecture
    as DROP_COOLDOWN/CLAIM_COOLDOWN."""
    ts = duo["cooldowns"].get(str(user_id))
    if not ts:
        return 0
    return max(0, int(DUO_COOLDOWN_SECONDS - (time.time() - ts)))


# ---- Duo: active-challenge helpers ------------------------------------

def find_active_duo(user_id):
    """Returns (challenge_id, challenge) for user_id's current active
    Duo challenge, or (None, None) if they're not in one."""
    uid = str(user_id)
    for challenge_id, challenge in duo.get("active", {}).items():
        if challenge.get("player_a") == uid or challenge.get("player_b") == uid:
            return challenge_id, challenge
    return None, None


def generate_duo_challenge() -> dict:
    """Picks one random challenge template from DUO_CHALLENGE_POOL and
    returns a fresh, zeroed-out challenge dict (still missing id/
    player_a/player_b/started_at/channel_id -- the caller fills those
    in once both players are confirmed)."""
    template = random.choice(DUO_CHALLENGE_POOL)
    challenge = {
        "type": template["type"],
        "target": template["target"],
        "label": template["label"].format(target=template["target"]),
        "progress": 0,
    }
    if template["type"] == "claim_series":
        challenge["series_seen"] = []
    elif template["type"] == "claim_characters":
        challenge["characters_seen"] = []
    return challenge


def _duo_progress_value(challenge: dict) -> int:
    """The current progress count for `challenge`, regardless of which
    of the 4 challenge types it is."""
    ctype = challenge.get("type")
    if ctype in ("claim_count", "claim_rarity4"):
        return challenge.get("progress", 0)
    if ctype == "claim_series":
        return len(challenge.get("series_seen", []))
    if ctype == "claim_characters":
        return len(challenge.get("characters_seen", []))
    return 0


def build_duo_challenge_embed(challenge: dict, user_a, user_b) -> discord.Embed:
    """The shared embed shown right after a Duo invite is accepted, and
    reused wherever a Duo challenge's current state needs to be
    displayed."""
    progress = _duo_progress_value(challenge)
    target = challenge.get("target", 0)
    embed = discord.Embed(
        color=THEME_COLOR,
        title="🤝 Duo Challenge",
        description=(
            f"**{user_a.mention} & {user_b.mention}**\n\n"
            f"> {challenge.get('label', 'Duo Challenge')}\n"
            f"Progress: **{progress}/{target}**"
        ),
    )
    return embed


def _duo_progress_bar(progress: int, target: int, length: int = 10) -> str:
    """A simple filled/empty block bar for `lduoprogress` -- purely a
    display helper over the same progress/target _duo_progress_value()
    already computes; no new state."""
    target = max(target, 1)
    filled = max(0, min(length, round(length * progress / target)))
    return "🟩" * filled + "⬜" * (length - filled)


def build_duo_progress_embed(client, challenge: dict, viewer_id, partner_user) -> discord.Embed:
    """
    Detailed progress view for `lduoprogress` -- reuses the exact same
    challenge state build_duo_challenge_embed() does (progress/target
    via _duo_progress_value(), label, etc.), just laid out with more
    detail: a percentage/bar indicator, the possible reward, and
    remaining-challenge info. Does not compute or store anything new;
    purely a read-only, richer display of the same `duo` state.
    """
    progress = _duo_progress_value(challenge)
    target = max(challenge.get("target", 0), 0)
    percent = min(100, round((progress / target) * 100)) if target else 0
    remaining = max(target - progress, 0)

    embed = discord.Embed(color=THEME_COLOR, title="🤝 Duo Challenge Progress")

    embed.add_field(name="Partner", value=partner_user.mention, inline=False)
    embed.add_field(name="Challenge", value=challenge.get("label", "Duo Challenge"), inline=False)
    embed.add_field(name="Progress", value=f"**{progress}/{target}**", inline=True)
    embed.add_field(name="Percentage", value=f"**{percent}%**", inline=True)
    embed.add_field(name="Remaining", value=f"**{remaining}** to go", inline=True)
    embed.add_field(name="Indicator", value=_duo_progress_bar(progress, target), inline=False)

    # The actual reward is randomly rolled (drop, claim, or both) only
    # once the challenge is completed -- see _finalize_completed_duo --
    # so this describes the possible outcome rather than a fixed,
    # precomputed one, since the real system never decides it early.
    embed.add_field(
        name="Rewards",
        value="🎁 On completion, you **and** your partner each get a random "
              "bonus: an extra drop, an extra claim, or both.",
        inline=False,
    )

    started_at = challenge.get("started_at")
    started_text = f"<t:{int(started_at)}:R>" if started_at else "Unknown"
    weekly_used = duo_weekly_count(viewer_id)
    embed.add_field(
        name="Other Info",
        value=(
            f"**Started:** {started_text}\n"
            f"**Weekly Duos completed:** {weekly_used}/{DUO_WEEKLY_LIMIT}"
        ),
        inline=False,
    )

    if partner_user.display_avatar:
        embed.set_thumbnail(url=partner_user.display_avatar.url)

    return embed


async def _fetch_duo_users(client, challenge: dict):
    """Best-effort resolves both players in `challenge` into discord
    User objects (cache first, then a fetch), for display purposes
    only. Returns (user_a, user_b), either of which may be None if
    resolution fails."""
    async def _get(uid):
        if not uid:
            return None
        try:
            user_obj = client.get_user(int(uid))
            if user_obj is None:
                user_obj = await client.fetch_user(int(uid))
            return user_obj
        except Exception:
            return None

    return await _get(challenge.get("player_a")), await _get(challenge.get("player_b"))


async def _finalize_completed_duo(client, challenge_id: str, challenge: dict) -> None:
    """
    Finalizes a just-completed Duo challenge: removes it from `active`,
    grants both players their (random, non-rerollable) reward as bonus
    drop/claim uses, records the completion for both players' weekly
    limit / distinct-partner tracking, starts each player's 2-day Duo
    cooldown, persists all of it in one save, and announces completion
    in the channel the Duo was started in.

    Must be called while already holding duo_lock (the caller that
    detects completion is always the one already holding it while
    updating progress).
    """
    player_a = challenge.get("player_a")
    player_b = challenge.get("player_b")
    now = time.time()

    reward = random.choice(["drop", "claim", "both"])
    for uid in (player_a, player_b):
        if reward in ("drop", "both"):
            add_bonus(uid, "drop", 1)
        if reward in ("claim", "both"):
            add_bonus(uid, "claim", 1)

    duo["weekly"].setdefault(player_a, []).append({"timestamp": now, "partner_id": player_b})
    duo["weekly"].setdefault(player_b, []).append({"timestamp": now, "partner_id": player_a})
    duo["cooldowns"][player_a] = now
    duo["cooldowns"][player_b] = now
    duo["active"].pop(challenge_id, None)

    try:
        save_duo_local()
        mark_duo_dirty()
    except Exception:
        print("[duo] Failed to persist a completed Duo challenge:")
        traceback.print_exc()

    channel = client.get_channel(challenge.get("channel_id"))
    if channel:
        user_a, user_b = await _fetch_duo_users(client, challenge)
        reward_text = {
            "drop": "+1 bonus drop",
            "claim": "+1 bonus claim",
            "both": "+1 bonus drop and +1 bonus claim",
        }[reward]
        name_a = user_a.mention if user_a else f"<@{player_a}>"
        name_b = user_b.mention if user_b else f"<@{player_b}>"
        embed = discord.Embed(
            color=THEME_COLOR,
            title="🎉 Duo Challenge Complete!",
            description=(
                f"{name_a} & {name_b} completed **{challenge.get('label', 'their Duo Challenge')}**!\n\n"
                f"Reward: **{reward_text}** each."
            ),
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            traceback.print_exc()


async def _record_duo_claim_progress(client, user_id, card: dict) -> None:
    """
    Best-effort hook, called once right after a claim has already fully
    succeeded and saved: if `user_id` is part of an active Duo
    challenge, applies this single claim toward it, persists the
    update, and finalizes the challenge if it's now complete. Never
    raises -- a failure here can never undo or block the claim that
    already happened.
    """
    try:
        async with duo_lock:
            challenge_id, challenge = find_active_duo(user_id)
            if not challenge:
                return

            ctype = challenge.get("type")
            changed = False

            if ctype == "claim_count":
                challenge["progress"] = challenge.get("progress", 0) + 1
                changed = True
            elif ctype == "claim_rarity4":
                if card.get("stars") == 4:
                    challenge["progress"] = challenge.get("progress", 0) + 1
                    changed = True
            elif ctype == "claim_series":
                series = card.get("series")
                seen = challenge.setdefault("series_seen", [])
                if series and series not in seen:
                    seen.append(series)
                    changed = True
            elif ctype == "claim_characters":
                name = card.get("name")
                seen = challenge.setdefault("characters_seen", [])
                if name and name not in seen:
                    seen.append(name)
                    changed = True

            if not changed:
                return

            if _duo_progress_value(challenge) >= challenge.get("target", 0):
                await _finalize_completed_duo(client, challenge_id, challenge)
            else:
                try:
                    save_duo_local()
                    mark_duo_dirty()
                except Exception:
                    traceback.print_exc()
    except Exception:
        print("[duo] Failed to record claim progress toward an active Duo challenge:")
        traceback.print_exc()


class DuoRequestView(discord.ui.View):
    """
    Accept/Decline Duo invitation -- same shape as TradeRequestView
    (90s timeout, only the invited player can respond, disables itself
    on timeout). Not persisted, same as trade requests: an unaccepted
    invite is only ever in-memory, and simply expiring on restart is
    the same behavior a pending trade request already has.
    """
    def __init__(self, user1, user2, user1_id, user2_id):
        super().__init__(timeout=90)
        self.user1 = user1
        self.user2 = user2
        self.user1_id = user1_id
        self.user2_id = user2_id
        self.message = None
        self.responded = False

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(color=THEME_COLOR)
        embed.description = (
            f"{self.user2.mention}, {self.user1.mention} wants to start a "
            f"**Duo Challenge** with you!"
        )
        return embed

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success, label="Accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user2_id:
            return await interaction.response.send_message(
                "This isn't your Duo invitation!", ephemeral=True
            )

        # Re-validate at accept time -- state (weekly count, cooldowns,
        # an already-active challenge) may have changed since the
        # invite was sent.
        async with duo_lock:
            if find_active_duo(self.user1_id)[0] or find_active_duo(self.user2_id)[0]:
                self.responded = True
                self.stop()
                embed = discord.Embed(color=THEME_COLOR)
                embed.description = "This Duo invitation is no longer available -- one of you is already in an active Duo challenge."
                return await interaction.response.edit_message(embed=embed, view=None)

            if (duo_weekly_count(self.user1_id) >= DUO_WEEKLY_LIMIT
                    or duo_weekly_count(self.user2_id) >= DUO_WEEKLY_LIMIT):
                self.responded = True
                self.stop()
                embed = discord.Embed(color=THEME_COLOR)
                embed.description = "This Duo invitation is no longer available -- one of you has already completed 3 Duos this week."
                return await interaction.response.edit_message(embed=embed, view=None)

            if (duo_cooldown_remaining(self.user1_id) > 0
                    or duo_cooldown_remaining(self.user2_id) > 0):
                self.responded = True
                self.stop()
                embed = discord.Embed(color=THEME_COLOR)
                embed.description = "This Duo invitation is no longer available -- one of you is still on Duo cooldown."
                return await interaction.response.edit_message(embed=embed, view=None)

            if str(self.user2_id) in duo_weekly_partners(self.user1_id):
                self.responded = True
                self.stop()
                embed = discord.Embed(color=THEME_COLOR)
                embed.description = "This Duo invitation is no longer available -- you've already completed a Duo together this week."
                return await interaction.response.edit_message(embed=embed, view=None)

            challenge = generate_duo_challenge()
            challenge_id = str(uuid.uuid4())
            challenge.update({
                "id": challenge_id,
                "player_a": str(self.user1_id),
                "player_b": str(self.user2_id),
                "started_at": time.time(),
                "channel_id": interaction.channel.id,
            })
            duo["active"][challenge_id] = challenge
            try:
                save_duo_local()
                mark_duo_dirty()
            except Exception:
                duo["active"].pop(challenge_id, None)
                self.responded = True
                self.stop()
                embed = discord.Embed(color=THEME_COLOR)
                embed.description = "❌ Something went wrong starting your Duo challenge. Please try again."
                return await interaction.response.edit_message(embed=embed, view=None)

        self.responded = True
        self.stop()

        embed = build_duo_challenge_embed(challenge, self.user1, self.user2)
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger, label="Decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user2_id:
            return await interaction.response.send_message(
                "This isn't your Duo invitation!", ephemeral=True
            )

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = "Duo invitation has been declined."

        self.responded = True
        self.stop()

        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if self.responded:
            return

        for item in self.children:
            item.disabled = True

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = "Duo invitation has expired."

        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except Exception:
                pass


# =========================
# MERCHANTS (merchants.json)
# =========================
# Stores the currently-active (or currently-cooling-down) set of
# traveling merchants: who they are, which series they deal in, the
# fixed offers they generated (wants / rewards), remaining stock, and
# their start/expiration timestamps -- plus, once a full set has gone
# away, the timestamp at which a brand new set is allowed to appear.
#
# Persisted/synced exactly the way showcase_votes.json and
# pending_recovery.json are (local file is authoritative, atomic
# writes, own lock, debounced GitHub mirror, best-effort flush on
# shutdown) -- no new persistence system, just the existing one reused
# for a new piece of state.
#
# NOTE: this file only covers merchant *generation* and *persistence*.
# Trading against a merchant (the Accept button / trade flow, actually
# spending a wanted card and receiving reward(s), decrementing stock on
# a completed trade) is intentionally NOT implemented here -- that's a
# later part. The small stock/print helpers below exist purely as
# infrastructure for that later part and are not wired into any
# command yet.

MERCHANT_DURATION_SECONDS = 24 * 60 * 60           # merchants stay for 24 hours...
MERCHANT_STARTING_STOCK = 10                        # ...or until stock hits 0, whichever first.
MERCHANT_COOLDOWN_SECONDS = 2 * 24 * 60 * 60        # wait 2 days after a full set disappears.
MERCHANT_MAX_REWARD_CARDS_PER_TRADE = 2             # a completed trade grants 1 or 2 cards, never more.

# How often the background loop re-checks expiration/stock/cooldown.
# Deliberately much shorter than MERCHANT_DURATION_SECONDS /
# MERCHANT_COOLDOWN_SECONDS so a set that just expired (or a cooldown
# that just elapsed) doesn't sit unnoticed for anywhere near as long as
# the thing it's waiting on.
MERCHANT_CHECK_INTERVAL_SECONDS = 900

# ---- Merchant templates (configuration -- lives in code, NOT in merchants.json) ----
#
# Each template is a fixed, named merchant "personality": identity/flavor
# (name, description, avatar placeholder, embed colour) plus the exact
# series it's allowed to deal in. merchants.json only ever stores the
# *runtime* result of instantiating a template (stock, timestamps,
# snapshotted offers) -- never this configuration itself. Adding a new
# merchant later means adding a template here, nothing more.
#
# Series names are copied EXACTLY as they must appear in cards.json's
# "series" field -- these are matched with a plain equality check, not
# fuzzy/case-insensitive, so any drift here silently empties that
# merchant's offer pool.
MERCHANT_TEMPLATES = [
    {
        "id": "voyager_merchant",
        "name": "Voyager Merchant",
        "description": "Found a few interesting people on my travel.",
        "avatar": "merchant_assets/voyager.png",  # local file -- see get_merchant_avatar_file()
        "color": 0x3B82F6,  # embed colour (blue)
        "allowed_series": [
            "Genshin Impact",
            "Honkai: Star Rail",
            "Project Sekai: Colorful Stage!",
            "Honkai Impact 3rd",
            "Ensemble Stars!",
        ],
    },
    {
        "id": "lucky_merchant",
        "name": "Lucky Merchant",
        "description": "Let's see if luck's on your side today.",
        "avatar": "merchant_assets/lucky.png",
        "color": 0xF59E0B,  # embed colour (amber)
        "allowed_series": [
            "Jujutsu Kaisen",
            "Kimetsu no Yaiba",
            "Alien Stage",
            "My Hero Academia",
            "BLUELOCK",
        ],
    },
    {
        "id": "collector_merchant",
        "name": "Collector Merchant",
        "description": "Every collection has room for one more.",
        "avatar": "merchant_assets/collector.png",
        "color": 0x8B5CF6,  # embed colour (purple)
        "allowed_series": [
            "Omniscient Reader's Viewpoint",
            "Tokyo Debunker",
            "Tokyo Revengers",
            "Persona",
            "Path To Nowhere",
        ],
    },
]


def get_merchant_template(template_id: str):
    """Looks up a merchant template (config) by its id. Returns None if unknown."""
    for t in MERCHANT_TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


# Derived, not hardcoded separately -- one merchant is instantiated per
# template each cycle, so the active-merchant count always tracks
# MERCHANT_TEMPLATES automatically as templates are added/removed.
MERCHANT_COUNT = len(MERCHANT_TEMPLATES)


def _load_merchants_json():
    """
    Loads and validates the local merchants.json.

    Mirrors _load_showcase_votes_json()/_load_pending_recovery_json()'s
    exact contract: "missing" and "invalid" are both real, distinct
    situations that must never be silently collapsed into "just start
    fresh" without at least a log line -- that's what makes the
    difference between merchants actually surviving a restart and them
    silently regenerating every time.

    Returns:
        - the parsed dict (with "merchants" and "next_generation_at"
          keys, tolerating either being absent/malformed by falling
          back to safe defaults for just that key) if the file exists,
          parses, and is a JSON object.
        - None if the file is missing, unreadable, contains malformed
          JSON, or parses to something other than a dict -- the
          explicit "invalid, do not treat as empty" signal.
    """
    try:
        with open('merchants.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[merchants] Failed to read merchants.json:")
        traceback.print_exc()
        return None

    if not raw:
        print("[merchants] merchants.json is empty (not even '{}') -- treating as invalid.")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[merchants] merchants.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        print(f"[merchants] merchants.json did not contain a JSON object "
              f"(got {type(parsed).__name__}) -- treating as invalid.")
        return None

    merchants_list = parsed.get("merchants")
    if not isinstance(merchants_list, list):
        merchants_list = []

    next_gen = parsed.get("next_generation_at")
    if next_gen is not None and not isinstance(next_gen, (int, float)):
        next_gen = None

    return {"merchants": merchants_list, "next_generation_at": next_gen}


def _merchants_json_bytes() -> bytes:
    """Serializes the current in-memory `merchants` dict to JSON bytes."""
    return json.dumps(merchants, indent=2).encode("utf-8")


def save_merchants_local() -> None:
    """
    Writes the current in-memory `merchants` dict to merchants.json on
    disk, atomically (via _atomic_write_bytes). Same role as
    save_inventories_local()/save_showcase_votes_local() for their
    respective stores.
    """
    try:
        _atomic_write_bytes("merchants.json", _merchants_json_bytes())
    except Exception:
        print("[merchants] Failed to save merchants.json locally:")
        traceback.print_exc()
        raise


_merchants_sync_task = None
_merchant_check_task = None
_merchants_dirty = False
_merchants_upload_in_progress = False


def mark_merchants_dirty() -> None:
    """
    Marks that merchants.json has local changes not yet pushed to
    GitHub. Must be called while holding merchants_lock, immediately
    after a successful save_merchants_local(). Mirrors
    mark_inventories_dirty()/mark_showcase_votes_dirty() exactly.
    """
    global _merchants_dirty
    _merchants_dirty = True


async def merchants_github_sync_loop():
    """
    Background task: wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS
    (the same shared interval every other store uses) and, only if
    merchants.json has unpushed local changes, performs exactly one
    GitHub commit containing the current merchant state. Mirrors
    inventory_github_sync_loop()/showcase_votes_github_sync_loop()
    exactly -- same snapshot-inside-the-lock, push-outside-the-lock
    pattern, same dirty-flag-restore-on-failure behavior, reusing the
    identical github_commit_files() helper.
    """
    global _merchants_dirty, _merchants_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _merchants_dirty or _merchants_upload_in_progress:
            continue

        async with merchants_lock:
            if not _merchants_dirty:
                continue
            data = _merchants_json_bytes()
            _merchants_dirty = False

        _merchants_upload_in_progress = True
        try:
            await github_commit_files({"merchants.json": data}, "Batched merchant state sync")
        except Exception:
            print("[merchants] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with merchants_lock:
                _merchants_dirty = True
        finally:
            _merchants_upload_in_progress = False


async def flush_merchants_to_github() -> None:
    """
    Best-effort final push of any pending merchant state changes to
    GitHub, called from Client.close() on graceful shutdown -- same
    reasoning and same mechanism as flush_inventories_to_github()/
    flush_showcase_votes_to_github().
    """
    global _merchants_dirty

    async with merchants_lock:
        if not _merchants_dirty:
            return
        data = _merchants_json_bytes()
        _merchants_dirty = False

    try:
        await github_commit_files({"merchants.json": data}, "Final merchant state sync (shutdown)")
        print("[merchants] Flushed pending merchant state changes to GitHub before shutdown.")
    except Exception:
        async with merchants_lock:
            _merchants_dirty = True
        print("[merchants] Failed to flush pending merchant state changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_merchants_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory. Mirrors
    _sync_showcase_votes_from_github_at_startup()'s exact priority
    order: local disk is authoritative if valid (every change is
    written to disk immediately; GitHub is only ever a periodic
    mirror), so GitHub is only consulted as a backfill when local is
    missing, unreadable, malformed, or not a dict.

    This is what makes "merchant state must survive bot restarts, do
    not regenerate merchants every restart" actually true across a
    Railway redeploy (where local disk isn't carried over to the new
    container) and not just across an in-place restart.
    """
    local_state = _load_merchants_json()
    if local_state is not None:
        print("[merchants] Loaded merchants.json from local disk.")
        return local_state

    print("[merchants] Local merchants.json is missing/unreadable/malformed -- trying GitHub as a backfill.")

    if os.path.exists('merchants.json'):
        backup_path = f"merchants.json.corrupt-{int(time.time())}"
        try:
            with open('merchants.json', 'rb') as src, open(backup_path, 'wb') as dst:
                dst.write(src.read())
            print(f"[merchants] Backed up the invalid local file to '{backup_path}' before trying GitHub.")
        except Exception:
            print("[merchants] Failed to back up the invalid local file (it was NOT modified or deleted):")
            traceback.print_exc()

    default_state = {"merchants": [], "next_generation_at": None}
    remote_bytes = await github_get_file("merchants.json")

    if remote_bytes is None:
        print("[merchants] GitHub unavailable or has no merchants.json -- starting with no merchants (one will be generated on the next check).")
        if not os.path.exists('merchants.json'):
            try:
                _atomic_write_bytes("merchants.json", json.dumps(default_state, indent=2).encode("utf-8"))
            except Exception:
                print("[merchants] Failed to create merchants.json:")
                traceback.print_exc()
        return default_state

    try:
        remote_parsed = json.loads(remote_bytes.decode("utf-8") or "{}")
        if not isinstance(remote_parsed, dict):
            raise ValueError(f"expected a JSON object, got {type(remote_parsed).__name__}")
        remote_state = {
            "merchants": remote_parsed.get("merchants") if isinstance(remote_parsed.get("merchants"), list) else [],
            "next_generation_at": remote_parsed.get("next_generation_at")
            if isinstance(remote_parsed.get("next_generation_at"), (int, float)) else None,
        }
    except Exception:
        print("[merchants] Downloaded merchants.json from GitHub was not a valid JSON object -- starting with no merchants.")
        traceback.print_exc()
        return default_state

    try:
        _atomic_write_bytes("merchants.json", json.dumps(remote_state, indent=2).encode("utf-8"))
        print("[merchants] Local merchants.json was missing/invalid; backfilled from GitHub.")
    except Exception:
        print("[merchants] Backfilled from GitHub in memory, but failed to write merchants.json locally:")
        traceback.print_exc()

    return remote_state


merchants = asyncio.run(_sync_merchants_from_github_at_startup())
merchants_lock = asyncio.Lock()


# ---- Offer generation ----

def _serialize_merchant_card(card: dict) -> dict:
    """
    Snapshots the fields of a card that a merchant offer needs, so the
    offer stays exactly as it was generated even if cards.json is later
    edited (leditcard/lupdateimage) -- "these offers remain fixed until
    the merchant leaves" applies to the snapshot, not a live reference.
    """
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "series": card.get("series"),
        "stars": card.get("stars"),
        "frame": card.get("frame"),
        "image": card.get("image"),
    }


def _cards_for_template(template: dict) -> list:
    """
    Every card in cards.json belonging to one of this template's
    allowed_series -- and ONLY those series. This is the sole pool
    "Looking For" requirements and rewards are ever drawn from for this
    merchant; there is deliberately no fallback that widens to other
    series if this pool is small, since a merchant must never offer or
    request a card outside its allowed series.
    """
    allowed = template.get("allowed_series", [])
    return [c for c in cards if c.get("series") in allowed]


def _generate_merchant_for_template(template: dict, now: float):
    """
    Instantiates one runtime merchant from a template: rolls its fixed
    "Looking For" list and its fixed reward pool, both drawn strictly
    from template["allowed_series"] via _cards_for_template(). Returns
    None only if that template's allowed series currently have no cards
    at all in cards.json (nothing to offer yet) -- in which case this
    template is simply skipped for this cycle rather than reaching into
    other series to fill the gap.
    """
    pool = _cards_for_template(template)
    if not pool:
        print(f"[merchants] Template '{template['id']}' has no cards in its allowed_series -- skipping this cycle.")
        return None

    want_count = min(len(pool), random.randint(2, 3))
    wants = random.sample(pool, want_count)

    remaining_pool = [c for c in pool if c not in wants]
    if not remaining_pool:
        # Still constrained to this template's own series-only pool --
        # never falls back to the full card list.
        remaining_pool = pool
    reward_count = min(len(remaining_pool), random.randint(2, 3))
    rewards = random.sample(remaining_pool, reward_count)

    return {
        # Runtime instance id -- distinct from template["id"], since the
        # same template gets a fresh instance (fresh offers, fresh
        # stock, fresh timestamps) every time the set regenerates.
        "id": f"{template['id']}_{int(now)}_{random.randint(1000, 9999)}",
        "template_id": template["id"],
        "wants": [_serialize_merchant_card(c) for c in wants],
        "rewards": [_serialize_merchant_card(c) for c in rewards],
        "stock": MERCHANT_STARTING_STOCK,
        "start_ts": now,
        "expires_ts": now + MERCHANT_DURATION_SECONDS,
    }


def _generate_merchant_batch(now: float) -> list:
    """
    Instantiates exactly one merchant per entry in MERCHANT_TEMPLATES
    (currently 3: Voyager, Lucky, and Collector Merchant) with
    freshly-rolled, fixed offers. Called only from the regeneration
    check below -- never on a plain restart, and never while any
    merchant from the current set is still active.
    """
    batch = []
    for template in MERCHANT_TEMPLATES:
        m = _generate_merchant_for_template(template, now)
        if m is not None:
            batch.append(m)
    return batch


# ---- Lifecycle / regeneration ----

def _merchant_is_active(m: dict, now: float) -> bool:
    """A merchant is available to trade with as long as it has stock left and hasn't expired."""
    return m.get("stock", 0) > 0 and now < m.get("expires_ts", 0)


def _all_merchants_inactive(state: dict, now: float) -> bool:
    merchants_list = state.get("merchants") or []
    return not any(_merchant_is_active(m, now) for m in merchants_list)


def _apply_merchant_regeneration_check(state: dict, now: float) -> bool:
    """
    Applies the regeneration rule to `state` in place:

        - No merchants have ever been generated yet -> generate the
          first set immediately (this is initial creation, not a
          replacement, so no cooldown applies).
        - Every current merchant is out of stock or expired, and no
          cooldown is running yet -> start the 2-day cooldown, right
          now, from this moment.
        - The cooldown has fully elapsed -> generate a brand new set of
          3, with brand new offers, and clear the cooldown.
        - Otherwise (something is still active, or the cooldown hasn't
          elapsed yet) -> leave state untouched entirely, including the
          existing (possibly now-inactive) merchants -- they are never
          rerolled or cleared early.

    Returns True if `state` was modified (so the caller knows to
    persist it), False otherwise.
    """
    merchants_list = state.get("merchants") or []

    if not merchants_list:
        state["merchants"] = _generate_merchant_batch(now)
        state["next_generation_at"] = None
        return True

    if _all_merchants_inactive(state, now):
        if state.get("next_generation_at") is None:
            state["next_generation_at"] = now + MERCHANT_COOLDOWN_SECONDS
            return True
        if now >= state["next_generation_at"]:
            state["merchants"] = _generate_merchant_batch(now)
            state["next_generation_at"] = None
            return True

    return False


# Channel where merchant arrival/departure announcements are posted.
# Set to 0 to disable; if the channel can't be found (or the bot isn't
# actually connected yet -- see below), the announcement is simply
# skipped, never an error, and never affects the merchant system itself.
MERCHANT_ANNOUNCEMENT_CHANNEL_ID = 1540149945284431942

# Arrival/departure announcements detected before the client is ready
# (see the "not ready yet" branch in _announce_merchant_event below)
# are queued here, in order, instead of being dropped -- and sent as
# soon as the client becomes ready (_flush_pending_merchant_announcements(),
# called from on_ready). This is what makes a brand-new bot's very
# first merchant arrival -- detected at import time, well before login
# -- still get announced instead of being permanently skipped.
_pending_merchant_announcements = []


async def _announce_merchant_event(arrived: bool) -> None:
    """
    Sends a single embed announcement when the merchants arrive or
    leave. Called only from check_and_update_merchants() below, right
    after it detects that exact transition in the existing merchant
    lifecycle state -- this has no timer or scheduling of its own, so
    it can never fire on its own or drift out of sync with the real
    merchant state.

    Defensively checks for a connected `client` before doing anything:
    check_and_update_merchants() also runs once at import time (see the
    module-level asyncio.run() call below), before the bot has actually
    logged in -- on a brand-new bot with no prior merchants.json, that
    very first call could detect an "arrival" before there's any
    gateway connection to send an announcement over at all. In that
    case the announcement is queued in _pending_merchant_announcements
    (not dropped) and sent as soon as the client becomes ready -- see
    _flush_pending_merchant_announcements(). A missing/unconfigured
    announcement channel, on the other hand, is still skipped outright
    below -- that's a config state waiting can't fix.
    """
    client_obj = globals().get("client")
    if client_obj is None or not client_obj.is_ready():
        _pending_merchant_announcements.append(arrived)
        return

    channel = client_obj.get_channel(MERCHANT_ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        return

    if arrived:
        embed = discord.Embed(
            color=THEME_COLOR,
            title="🧭 The Merchants have arrived!",
            description=(
                "New trades are now available from all three merchants. "
                "They'll be around for a limited time, so make sure to "
                "check what they're offering before they move on. "
                "View everything with `lmerchants`."
            ),
        )
    else:
        embed = discord.Embed(
            color=THEME_COLOR,
            title="🧭 The Merchants have continued their journey.",
            description=(
                "Their time here has come to an end. They'll return in "
                "a few days with a brand new selection of trades."
            ),
        )

    try:
        await channel.send(embed=embed)
    except Exception:
        print("[merchants] Failed to send a merchant arrival/departure announcement:")
        traceback.print_exc()


async def _flush_pending_merchant_announcements() -> None:
    """
    Sends any merchant arrival/departure announcements that were
    detected while the client wasn't ready yet (queued by
    _announce_merchant_event above), in the order they originally
    happened. Called from on_ready -- safe to call on every on_ready
    (including reconnects), since the queue is drained as each entry
    is sent and is simply empty on later calls.
    """
    while _pending_merchant_announcements:
        arrived = _pending_merchant_announcements.pop(0)
        await _announce_merchant_event(arrived=arrived)


async def _force_merchant_active_state(active: bool) -> bool:
    """
    Testing-only helper for lmerchantcontrol (owner-only). Flips ONLY
    the timing half of _merchant_is_active()'s check -- each current
    merchant's "expires_ts" -- for every merchant in the CURRENT batch,
    without touching stock, wants, rewards, template_id, or id. No
    merchant is rerolled, no offer/reward changes.

    On `active=True` ("arrive") this also clears next_generation_at back
    to None. That mirrors exactly what a REAL arrival does in
    _apply_merchant_regeneration_check, and is required to keep the
    system's own invariant intact: next_generation_at must only ever be
    non-None while every merchant is inactive. Without this, reviving an
    already-inactive (cooldown-pending) batch via `arrive` would leave
    that cooldown timestamp sitting there unused -- and if the batch is
    later forced inactive again (naturally or via `leave`) after that
    stale timestamp has already passed, the very next
    _apply_merchant_regeneration_check call would see
    `now >= next_generation_at` immediately and regenerate on the spot,
    skipping the 2-day cooldown entirely. `active=False` ("leave") still
    never sets/clears next_generation_at itself -- that half is entirely
    _apply_merchant_regeneration_check's job, untouched, exactly as
    before.

    Reuses the exact same persistence (save_merchants_local/
    mark_merchants_dirty) and the exact same _announce_merchant_event()
    every real arrival/departure already uses, so `lmerchants` and the
    announcement channel both reflect this exactly like a real
    transition would -- no second/parallel state, no new system.

    Returns False (and does nothing else) if there's no current
    merchant batch to flip at all.
    """
    global merchants
    now = time.time()

    async with merchants_lock:
        current = merchants.get("merchants") or []
        if not current:
            return False

        for m in current:
            m["expires_ts"] = (now + MERCHANT_DURATION_SECONDS) if active else (now - 1)

        if active:
            merchants["next_generation_at"] = None

        try:
            save_merchants_local()
            mark_merchants_dirty()
        except Exception:
            print("[merchants] Failed to persist a testing arrive/leave state change:")
            traceback.print_exc()

    await _announce_merchant_event(arrived=active)
    return True


async def check_and_update_merchants() -> bool:
    """
    The single entry point for merchant lifecycle progression: call
    this at startup and periodically (see merchant_check_loop below).
    Safe to call as often as desired -- it's a no-op unless a
    regeneration or a cooldown-start is actually due.

    Also detects, from the exact same state transition, whether this
    call was the moment merchants just arrived (a fresh batch was just
    generated) or just departed (the post-sellout/expiry cooldown was
    just started) -- reusing the existing regeneration state
    (`next_generation_at`/`merchants`) rather than any separate
    scheduling of its own -- and fires at most one announcement for
    whichever happened (see _announce_merchant_event above).
    """
    global merchants
    now = time.time()

    async with merchants_lock:
        had_any_merchants = bool(merchants.get("merchants"))
        had_next_generation_at = merchants.get("next_generation_at") is not None

        changed = _apply_merchant_regeneration_check(merchants, now)

        # Arrival: the merchants list was just (re)populated -- either
        # the very first-ever generation, or a fresh rotation after a
        # completed cooldown.
        arrived = (
            changed
            and bool(merchants.get("merchants"))
            and merchants.get("next_generation_at") is None
            and (not had_any_merchants or had_next_generation_at)
        )
        # Departure: the cooldown was just started this call (it was
        # None a moment ago, and is now set) -- the exact moment all
        # three merchants have left.
        departed = (
            changed
            and had_any_merchants
            and not had_next_generation_at
            and merchants.get("next_generation_at") is not None
        )

        if changed:
            try:
                save_merchants_local()
                mark_merchants_dirty()
            except Exception:
                print("[merchants] Failed to persist merchant state after a regeneration check:")
                traceback.print_exc()

    if arrived:
        await _announce_merchant_event(arrived=True)
    if departed:
        await _announce_merchant_event(arrived=False)

    return changed


# Applies once, immediately, at import time -- so a brand new bot (or
# one whose merchants.json never made it into this container) gets its
# very first set of merchants right away, instead of waiting up to
# MERCHANT_CHECK_INTERVAL_SECONDS for the background loop's first tick.
# A bot with an existing, still-valid merchants.json is unaffected by
# this call (see _apply_merchant_regeneration_check above): it changes
# nothing unless a regeneration/cooldown-start is actually due.
asyncio.run(check_and_update_merchants())


async def merchant_check_loop():
    """
    Background task: re-runs check_and_update_merchants() every
    MERCHANT_CHECK_INTERVAL_SECONDS so an expiring/depleted set of
    merchants (or an elapsed cooldown) is noticed and acted on while
    the bot is running, not just at startup.
    """
    while True:
        await asyncio.sleep(MERCHANT_CHECK_INTERVAL_SECONDS)
        try:
            await check_and_update_merchants()
        except Exception:
            print("[merchants] Periodic merchant check failed (will retry next cycle):")
            traceback.print_exc()


def get_active_merchants() -> list:
    """
    Returns the currently-tradeable merchants (in stock and not
    expired) from the in-memory state. Read-only convenience helper for
    later parts (listing merchants, trade UI) -- not used by any
    command yet.
    """
    now = time.time()
    return [m for m in (merchants.get("merchants") or []) if _merchant_is_active(m, now)]


def get_merchant_by_id(merchant_id: str):
    """Looks up a merchant (active or not) by its id. Returns None if not found."""
    for m in (merchants.get("merchants") or []):
        if m.get("id") == merchant_id:
            return m
    return None


def get_merchant_display_info(m: dict):
    """
    Joins a runtime merchant record (from merchants.json) with its
    template (name/description/avatar/colour/allowed_series, from code)
    for anything that needs to *display* a merchant. Runtime data and
    configuration are only ever merged at read time like this -- never
    persisted together. Returns None if the merchant's template_id
    doesn't match a known template (shouldn't happen unless a template
    was renamed/removed after merchants.json was written).

    Not called from anywhere yet -- provided for the trade flow / any
    future merchant-listing command to use.
    """
    template = get_merchant_template(m.get("template_id"))
    if template is None:
        return None
    return {**template, **m}


def get_merchant_avatar_file(template_or_info: dict):
    """
    Loads a merchant's local avatar (merchant_assets/<file>.png, per
    MERCHANT_TEMPLATES) as a fresh discord.File, so it can be sent as a
    message attachment and referenced with an "attachment://" thumbnail
    URL -- never a Discord CDN URL that can expire. Returns
    (discord.File, attachment_url), or (None, None) if this template has
    no avatar configured or the file is missing/unreadable, so a caller
    can gracefully render the embed with no thumbnail instead of
    crashing.

    A brand-new discord.File is created on every call, since File
    objects are single-use (consumed once actually sent/edited).
    Swapping a merchant's picture later only ever requires replacing the
    file at that same path -- this function, and the templates that
    point to it, never need to change.
    """
    avatar_path = template_or_info.get("avatar") if template_or_info else None
    if not avatar_path or not os.path.exists(avatar_path):
        return None, None

    try:
        filename = os.path.basename(avatar_path)
        return discord.File(avatar_path, filename=filename), f"attachment://{filename}"
    except Exception:
        print(f"[merchants] Failed to load avatar file '{avatar_path}':")
        traceback.print_exc()
        return None, None


# ---- Infrastructure for the trade flow ----
#
# Used by MerchantTradeView's trade-finalize step below. Kept here,
# next to the rest of the merchant persistence code, per spec:
# "Merchant prints ... ONLY applies to merchant rewards. Every other
# command ... must continue showing L exactly as before. Do not change
# global print logic." The existing get_next_print()/format_print()
# above are therefore left completely untouched; merchant rewards will
# use the two helpers below instead, once the trade flow exists.

def get_next_merchant_print(card_id: str) -> int:
    """
    Assigns the next print number for a merchant-rewarded card, using
    the exact same shared `card_prints` counter get_next_print() uses
    for every other command (claims/gifts/drops) -- so merchant rewards
    still consume real, sequential print numbers and never collide with
    or duplicate a print number issued anywhere else. The ONLY
    difference from get_next_print() is what happens at display time
    (see format_merchant_print below): merchant rewards are shown with
    their real number even past 100, instead of "L".
    """
    return get_next_print(card_id)


def format_merchant_print(print_num: int) -> str:
    """
    Display formatting for a merchant-rewarded card's print number.
    Unlike format_print() (used everywhere else), this never collapses
    to "L" -- merchant rewards show the real number (e.g. #101, #145,
    #273) even once a series has passed the normal 100-print cap.
    """
    return f"#{print_num}"


async def decrement_merchant_stock(merchant_id: str):
    """
    Reduces a merchant's remaining stock by 1 after a completed trade
    and persists the change. Returns the updated merchant dict, or None
    if that merchant id doesn't exist. A merchant whose stock reaches 0
    here simply stops being returned by get_active_merchants() from
    that point on (per _merchant_is_active) -- it is not removed from
    `merchants["merchants"]`, since the whole set only gets replaced
    once every merchant in it is inactive (see
    _apply_merchant_regeneration_check).

    Note: MerchantTradeView finalizes a trade by mutating the merchant
    dict directly under merchants_lock (so the stock decrement and the
    inventory/reward changes commit together as one atomic operation)
    rather than calling this function -- this helper is kept for any
    other, non-trade caller that needs to drop stock by exactly one.
    """
    global merchants
    async with merchants_lock:
        target = None
        for m in (merchants.get("merchants") or []):
            if m.get("id") == merchant_id:
                target = m
                break
        if target is None:
            return None

        target["stock"] = max(0, target.get("stock", 0) - 1)

        try:
            save_merchants_local()
            mark_merchants_dirty()
        except Exception:
            print("[merchants] Failed to persist merchant state after a stock decrement:")
            traceback.print_exc()

        return target



async def mark_user_pending_recovery(user_id) -> None:
    """
    Records that `user_id` was just confirmed (discord.NotFound) to no
    longer be in the server. Does NOT transfer any cards -- only starts
    the RECOVERY_PENDING_DAYS countdown, or leaves it running unchanged
    if they're already tracked (their original first-detected timestamp
    is never reset just because another Owners lookup happens to see
    them again). The actual transfer only ever happens from
    pending_recovery_check_loop, after the full window elapses with no
    rejoin.
    """
    user_key = str(user_id)

    async with pending_recovery_lock:
        if user_key in pending_recovery:
            return

        pending_recovery[user_key] = time.time()
        try:
            save_pending_recovery_local()
            mark_pending_recovery_dirty()
        except Exception:
            del pending_recovery[user_key]
            print(f"[recovery] Failed to save after marking user {user_id} pending recovery:")
            traceback.print_exc()


async def _perform_full_recovery(user_id) -> int:
    """
    Transfers user_id's ENTIRE inventory into SYSTEM_RECOVERY_USER's
    inventory -- every card they owned, not just one -- preserving each
    entry's "print" field exactly (cards are only ever reassigned, never
    modified or deleted). Uses the exact same `inventories` dict, the
    exact same inventories_lock, and the exact same
    save_inventories_local()/mark_inventories_dirty() every other
    inventory mutation (claim/gift/trade) already goes through -- no
    separate persistence path for the transfer itself. On any save
    failure, the entire move is rolled back so the departed user's
    inventory is only ever actually cleared once the transfer has fully
    succeeded (in memory AND on local disk).

    Each card entry that lands in SYSTEM_RECOVERY_USER's inventory also
    gets two extra fields added to it: "original_owner" (the departed
    user's Discord id, as a string) and "recovered_at" (a Unix
    timestamp, shared by every card from this same transfer) -- so a
    future giveaway/admin-recovery command can tell where a recovered
    card came from and when. Both are purely informational and don't
    affect any current behavior. This is a NEW dict per card --
    moved_cards (the untagged originals, kept for a possible rollback
    below) is never mutated, so neither field can ever leak onto a
    card still sitting in a normal player's inventory.

    Returns the number of cards transferred (0 if they had none).
    """
    user_key = str(user_id)

    async with inventories_lock:
        user_inv = inventories.get(user_key)
        if not user_inv:
            return 0

        moved_cards = list(user_inv)
        recovered_inv = inventories.setdefault(SYSTEM_RECOVERY_USER, [])

        # Fresh dict copies for the recovery inventory only -- moved_cards
        # itself (used to restore the user's inventory exactly as it was
        # if the save below fails) is left completely untouched.
        recovery_timestamp = time.time()
        tagged_cards = [
            dict(owned_card, original_owner=user_key, recovered_at=recovery_timestamp)
            for owned_card in moved_cards
        ]

        recovered_inv.extend(tagged_cards)
        inventories[user_key] = []

        try:
            save_inventories_local()
            mark_inventories_dirty()
        except Exception:
            # Roll back completely -- the departed user's inventory is
            # only actually cleared once this has fully succeeded. The
            # restored entries are the original, untagged ones, since
            # the recovery never actually completed.
            del recovered_inv[len(recovered_inv) - len(tagged_cards):]
            inventories[user_key] = moved_cards
            print(f"[recovery] Failed to recover departed user {user_id}'s inventory:")
            traceback.print_exc()
            raise

    print(f"[recovery] Recovered {len(moved_cards)} card(s) from departed user {user_id} "
          f"into '{SYSTEM_RECOVERY_USER}' after {RECOVERY_PENDING_DAYS} days outside the server "
          f"(tagged with original_owner={user_key}).")
    return len(moved_cards)


async def _run_pending_recovery_sweep(guild) -> None:
    """
    Runs exactly one pass over the current pending-recovery list: for
    every user pending recovery, checks whether they've rejoined (if
    so, removes them from the pending list -- their inventory is never
    touched) or, if RECOVERY_PENDING_DAYS have elapsed since they were
    first detected as gone AND they're still not a member, performs the
    full transfer via _perform_full_recovery.

    This is the exact body pending_recovery_check_loop below used to
    run inline; it's factored out, unchanged, so the same logic can
    also be run once immediately at startup (see Client.on_ready)
    without duplicating it.
    """
    async with pending_recovery_lock:
        pending_snapshot = dict(pending_recovery)

    for user_key, first_detected in pending_snapshot.items():
        try:
            user_id = int(user_key)
        except ValueError:
            continue

        try:
            await guild.fetch_member(user_id)
            # Rejoined -- remove from pending, inventory untouched.
            async with pending_recovery_lock:
                if pending_recovery.pop(user_key, None) is not None:
                    try:
                        save_pending_recovery_local()
                        mark_pending_recovery_dirty()
                    except Exception:
                        print(f"[recovery] Failed to save after {user_id} rejoined:")
                        traceback.print_exc()
            continue
        except discord.NotFound:
            pass  # still gone -- fall through to the elapsed-time check
        except Exception:
            # Any other fetch failure is inconclusive -- don't act
            # on it, just retry next cycle.
            continue

        elapsed_days = (time.time() - first_detected) / 86400
        if elapsed_days < RECOVERY_PENDING_DAYS:
            continue

        try:
            await _perform_full_recovery(user_id)
        except Exception:
            continue  # already logged inside; stays pending, retried next cycle

        async with pending_recovery_lock:
            if pending_recovery.pop(user_key, None) is not None:
                try:
                    save_pending_recovery_local()
                    mark_pending_recovery_dirty()
                except Exception:
                    print(f"[recovery] Failed to save after recovering {user_id}'s inventory:")
                    traceback.print_exc()


def _find_pending_recovery_matches(name: str, print_num: int, star_count: int) -> list:
    """
    Owner-repair helper (see `lrecover`/`lpendingrecovery`). Searches
    every user CURRENTLY in the pending-recovery countdown -- their
    cards still live in their OWN inventory at this stage, exactly as
    _perform_full_recovery/_run_pending_recovery_sweep above describe,
    nothing has been transferred yet -- for an owned card whose
    character name, print number, and star count all match exactly.

    Read-only: never mutates inventories or pending_recovery. Returns a
    list of (owner_key, index, owned_card) tuples -- normally 0 or 1
    (prints are unique per card id), but this never assumes that; the
    caller decides what an unexpected 0 or 2+ means.
    """
    matches = []
    name_lower = name.strip().lower()
    for user_key in pending_recovery:
        if user_key == RECYCLABLE_CARDS_KEY or not str(user_key).isdigit():
            continue
        inv = inventories.get(user_key, [])
        for i, owned_card in enumerate(inv):
            card = owned_card.get("card", {})
            if (
                card.get("name", "").strip().lower() == name_lower
                and owned_card.get("print") == print_num
                and card.get("stars") == star_count
            ):
                matches.append((user_key, i, owned_card))
    return matches


async def pending_recovery_check_loop():
    """
    Runs once every PENDING_RECOVERY_CHECK_INTERVAL_SECONDS, performing
    one _run_pending_recovery_sweep() pass (see that function for what
    a sweep actually does). This loop is the ONLY place a full
    inventory transfer periodically happens on an ongoing basis --
    never as a side effect of an Owners lookup (see
    OwnersPaginationView, which only ever calls
    mark_user_pending_recovery). An identical sweep is also run once,
    immediately, at startup -- see Client.on_ready -- so a
    RECOVERY_PENDING_DAYS deadline that passed while the bot was
    offline is caught right away instead of waiting for this loop's
    first sleep to elapse.
    """
    while True:
        await asyncio.sleep(PENDING_RECOVERY_CHECK_INTERVAL_SECONDS)

        if not client.guilds:
            continue
        guild = client.guilds[0]

        await _run_pending_recovery_sweep(guild)


# One-time migration: every existing card previously had a hardcoded
# "weight": 10 regardless of its star rating, so rarity never actually
# affected drop odds. This corrects each card's weight to match
# STAR_WEIGHTS based on its existing "stars" value. Idempotent -- it only
# writes cards.json if a card's weight actually needed changing, so on
# every later startup (once all cards already match) this is a no-op.
# One-time, startup-only pass -- nothing polls or re-runs this later.
_weights_migrated = 0
for _card in cards:
    _correct_weight = weight_for_stars(_card.get("stars", 1))
    if _card.get("weight") != _correct_weight:
        _card["weight"] = _correct_weight
        _weights_migrated += 1

if _weights_migrated:
    save_cards_json()
    print(f"[cards] Migrated weight for {_weights_migrated} card(s) to match their star rating.")


# =========================
# CARD VERSION METADATA (common / V1 / V2 / ... / rare)
# =========================
# Single source of truth for a card's "version" field, used by
# get_weighted_card() (drop eligibility), lup/lv (display), and
# lmissing (per-version missing display). Grouping is always by
# (name, series) case-insensitively -- the same "character" grouping
# CharacterVersionView/lmissing already use elsewhere -- so the same
# name in a different series is a different character with its own
# independent version sequence.

def _card_character_key(card: dict):
    """The (name, series) key identifying which character a card
    belongs to, matching the grouping already used by
    CharacterVersionView/lup's "all_versions" lookup."""
    return (card.get("name", "").strip().lower(), card.get("series", "").strip().lower())


def _is_common_card(card: dict) -> bool:
    """Whether a card is a Common (frame == "common", case-insensitive)
    -- the same check card_version_label() already uses for display."""
    return card.get("frame", "").strip().lower() == "common"


def _version_index(version) -> int:
    """"common" -> 0, "V1" -> 1, "V2" -> 2, etc. Used only to order a
    character's own Common cards relative to each other; meaningless
    (and unused) for Rare cards."""
    if version == "common":
        return 0
    if isinstance(version, str) and version.startswith("V") and version[1:].isdigit():
        return int(version[1:])
    return 0


def _lup_version_sort_key(card: dict):
    """
    Sort key for ordering a character's cards in `lup` display:
    Common first, then V1/V2/V3/... in ascending NUMERIC order (not
    alphabetical, so V2 sorts before V10), then all Rare cards last.
    Based purely on the card's stored `version` metadata -- never on
    cards.json's list order or creation order.
    """
    version = card.get("version")
    if version == "rare" or not _is_common_card(card):
        return (1, 0)
    return (0, _version_index(version))


def _normalize_version_token(token: str):
    """
    Normalizes a user-typed version argument (for `lsetdate`/
    `ldateversion`) into the exact string stored on card["version"]:
    "common", "rare", or "V<n>" (any case/whitespace on input). Returns
    None if the token doesn't match any of those shapes -- callers
    treat that as an invalid version, never guess.
    """
    token = (token or "").strip().lower()
    if token == "common":
        return "common"
    if token == "rare":
        return "rare"
    if token.startswith("v") and token[1:].isdigit() and token[1:]:
        return f"V{int(token[1:])}"
    return None


def _schedule_version_sort_key(version: str):
    """Same Common-then-numeric-then-Rare ordering as
    _lup_version_sort_key(), but for a bare version STRING (used by
    `ldateversion`, which has no card object to check)."""
    if version == "rare":
        return (1, 0)
    return (0, _version_index(version))


def _parse_relative_unlock_time(time_str: str):
    """
    Parses the free-form time argument `lsetdate` accepts (everything
    after the version token) into an absolute unix timestamp. Accepts
    "now" and "clear" are handled by the caller before this is ever
    called -- this only handles "<amount> <unit>", e.g. "4 days", "12
    hours", "30 minutes", "1 week" -- singular/plural and
    minute/hour/day/week (and short forms min/hr/wk) all accepted,
    case-insensitively. Returns None if the string doesn't match that
    shape, so the caller can show a usage error rather than silently
    misinterpreting it.
    """
    match = re.match(
        r'^\s*(\d+(?:\.\d+)?)\s*(minute|min|hour|hr|day|week|wk)s?\s*$',
        (time_str or "").strip(),
        re.IGNORECASE
    )
    if not match:
        return None

    amount = float(match.group(1))
    unit = match.group(2).lower()
    seconds_per_unit = {
        "minute": 60, "min": 60,
        "hour": 3600, "hr": 3600,
        "day": 86400,
        "week": 604800, "wk": 604800,
    }
    return time.time() + amount * seconds_per_unit[unit]


def is_base_common_card(card: dict) -> bool:
    """A character's BASE Common card -- the first Common ever created
    for that character. Exactly one card per character satisfies this
    (per _recompute_card_versions below), and it's what the base-card
    period, the sequential Common unlock chain, and the Rare unlock all
    key off of."""
    return _is_common_card(card) and card.get("version") == "common"


def _build_character_version_lookup():
    """
    Builds, fresh from the current `cards` list, the two lookups
    get_weighted_card() needs to evaluate drop eligibility. Cheap to
    rebuild on every drop -- keeps this correct immediately after
    laddcard adds a brand new card, with no separate cache-invalidation
    step needed.

    Returns (base_by_character, prev_common_by_card_id):
      - base_by_character: {character_key: that character's base Common
        card dict (version == "common"), or missing if it has none}.
      - prev_common_by_card_id: {card id: that SAME character's Common
        card exactly one version before it} -- e.g. V2's entry points
        at V1, V1's entry points at the base "common". Only populated
        for Common cards that aren't already the base.
    """
    commons_by_character = {}
    for c in cards:
        if _is_common_card(c):
            commons_by_character.setdefault(_card_character_key(c), []).append(c)

    base_by_character = {}
    prev_common_by_card_id = {}
    for key, group in commons_by_character.items():
        by_index = {_version_index(c.get("version")): c for c in group}
        if 0 in by_index:
            base_by_character[key] = by_index[0]
        for idx, c in by_index.items():
            if idx == 0:
                continue
            prev = by_index.get(idx - 1)
            if prev is not None:
                prev_common_by_card_id[c["id"]] = prev

    return base_by_character, prev_common_by_card_id


def _card_is_eligible_to_drop(card, now, base_by_character, prev_common_by_card_id) -> bool:
    """
    The drop-eligibility rule (see DROP LOGIC in the version/unlock
    spec):
      - During the 5-day base-card period: only each character's base
        Common can drop -- no later Common versions, no Rare cards.
      - After the base-card period:
          * a character's base Common remains eligible forever.
          * a later Common version (V1, V2, ...) is eligible once the
            PREVIOUS Common version of that SAME character has been
            claimed COMMON_VERSION_UNLOCK_CLAIMS times.
          * a character's Rare card(s) are eligible once that
            character's base Common has been claimed
            RARE_UNLOCK_CLAIMS times.
      - On top of ALL of the above (never instead of it): if this
        card's version-tier has an owner-set `lsetdate` release time
        (see LSETDATE COMMAND) that hasn't arrived yet, the card is NOT
        eligible even if its claim requirement above is already
        satisfied -- both conditions must hold. A version with no
        `lsetdate` entry is entirely unaffected by this and behaves
        exactly as it did before `lsetdate` existed.
    Claims are always read per exact card id from `card_prints` --
    never shared across characters or versions.
    """
    # A character with no Common card at all (i.e. it only has Rare/
    # 4-star cards) has nothing to gate its Rare behind -- no base
    # Common to sit through the 5-day base-card period for, and no
    # base-Common claim count to reach RARE_UNLOCK_CLAIMS on. Its Rare
    # is therefore automatically eligible to drop, unconditionally --
    # including with respect to `lsetdate`: this exception is checked
    # BEFORE the release-date gate below and returns immediately, so no
    # schedule can ever delay it.
    if not _is_common_card(card) and base_by_character.get(_card_character_key(card)) is None:
        return True

    if base_card_period_active(now):
        claim_requirement_met = is_base_common_card(card)
    elif is_base_common_card(card):
        claim_requirement_met = True
    elif _is_common_card(card):
        prev = prev_common_by_card_id.get(card.get("id"))
        if prev is None:
            # No known previous version to chain off of (e.g. a data
            # inconsistency) -- never eligible rather than guessing.
            claim_requirement_met = False
        else:
            claim_requirement_met = card_prints.get(prev["id"], 0) >= COMMON_VERSION_UNLOCK_CLAIMS
    else:
        # Rare (character has a base Common, so the normal 50-claim rule applies).
        base = base_by_character.get(_card_character_key(card))
        claim_requirement_met = base is not None and card_prints.get(base["id"], 0) >= RARE_UNLOCK_CLAIMS

    if not claim_requirement_met:
        return False

    # `lsetdate` release-date gate: an ADDITIONAL requirement on top of
    # the claim check above, never a replacement for it. If this
    # version has a scheduled release time and it hasn't arrived yet,
    # the card stays ineligible even though its claim requirement is
    # already satisfied -- e.g. V1 hitting 75 claims does not unlock V2
    # early if `lsetdate V2 5 days` hasn't finished counting down yet.
    scheduled_at = version_system.get("scheduled_unlocks", {}).get(card.get("version"))
    if scheduled_at is not None and now < scheduled_at:
        return False

    return True


def _next_version_for_new_card(char_name: str, series: str, frame_name: str) -> str:
    """
    Computes the `version` a brand-new card (about to be appended to
    `cards` by laddcard) should get -- reuses the exact same
    character-grouping/Common-counting rule _recompute_card_versions
    uses for the startup migration, so laddcard and the migration are
    never two separate implementations of this logic. Must be called
    with the new card NOT YET appended to `cards`.
    """
    if frame_name.strip().lower() != "common":
        return "rare"
    key = (char_name.strip().lower(), series.strip().lower())
    existing_commons = sum(
        1 for c in cards
        if _card_character_key(c) == key and _is_common_card(c)
    )
    return "common" if existing_commons == 0 else f"V{existing_commons}"


def _recompute_card_versions() -> int:
    """
    One-time (startup-only) migration: assigns/corrects every card's
    `version` field from scratch, based purely on cards.json's list
    order -- which is creation order, since laddcard only ever
    cards.append()s a brand new card, never inserts one out of order --
    grouped per character:

        - Common cards, in creation order, per character:
          1st -> "common" (the character's base card), 2nd -> "V1",
          3rd -> "V2", 4th -> "V3", etc.
        - Rare cards (any frame other than exactly "common") always ->
          "rare", regardless of creation order or how many exist.

    Idempotent, same pattern as the weight migration above -- only
    writes cards.json if something actually changed, so every later
    startup (once everything already matches) is a no-op.
    """
    common_seen_count = {}
    changed = 0
    for card in cards:
        if _is_common_card(card):
            key = _card_character_key(card)
            n = common_seen_count.get(key, 0)
            new_version = "common" if n == 0 else f"V{n}"
            common_seen_count[key] = n + 1
        else:
            new_version = "rare"

        if card.get("version") != new_version:
            card["version"] = new_version
            changed += 1
    return changed


_versions_migrated = _recompute_card_versions()
if _versions_migrated:
    save_cards_json()
    print(f"[cards] Migrated version metadata for {_versions_migrated} card(s).")


# =========================
# VERSION SYSTEM STATE (version_system.json)
# =========================
# Tracks exactly one thing: the timestamp this card-version/unlock
# system first went live on this bot (its very first startup after the
# inventory reset that precedes it). The 5-day base-card-only period is
# counted from that single moment, once, forever -- a later restart
# must NOT push it back or restart the clock. Persisted/synced with the
# exact same local-first + GitHub-backfill + debounced-sync +
# shutdown-flush pipeline every other piece of state in this file
# already uses (mail.json, duo.json, merchants.json, ...) -- no second
# persistence system, just that same one reused for one more small
# piece of state.

BASE_CARD_PERIOD_SECONDS = 5 * 24 * 3600         # 5 days
COMMON_VERSION_UNLOCK_CLAIMS = 75                # V(n) unlocks once V(n-1) hits this many claims
RARE_UNLOCK_CLAIMS = 50                          # a character's Rare unlocks once its base Common hits this many claims


def _load_version_system_json():
    """Loads and validates the local version_system.json. Same
    missing-vs-invalid contract as every other loader in this file --
    returns the parsed dict if valid, None if
    missing/unreadable/malformed/not a dict."""
    try:
        with open('version_system.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[versions] Failed to read version_system.json:")
        traceback.print_exc()
        return None

    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[versions] version_system.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _version_system_json_bytes() -> bytes:
    return json.dumps(version_system, indent=2).encode("utf-8")


def save_version_system_local() -> None:
    """Writes the current in-memory `version_system` dict to
    version_system.json, atomically -- same role as
    save_mail_local()/save_duo_local()."""
    try:
        _atomic_write_bytes("version_system.json", _version_system_json_bytes())
    except Exception:
        print("[versions] Failed to save version_system.json locally:")
        traceback.print_exc()
        raise


_version_system_sync_task = None
_version_system_dirty = False
_version_system_upload_in_progress = False


def mark_version_system_dirty() -> None:
    global _version_system_dirty
    _version_system_dirty = True


async def version_system_github_sync_loop():
    """Background task: same debounced-commit pattern as
    mail_github_sync_loop()/duo_github_sync_loop() -- wakes up every
    INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS and, only if there are
    unpushed local changes, performs exactly one GitHub commit."""
    global _version_system_dirty, _version_system_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _version_system_dirty or _version_system_upload_in_progress:
            continue

        async with version_system_lock:
            if not _version_system_dirty:
                continue
            data = _version_system_json_bytes()
            _version_system_dirty = False

        _version_system_upload_in_progress = True
        try:
            await github_commit_files({"version_system.json": data}, "Batched version system sync")
        except Exception:
            print("[versions] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with version_system_lock:
                _version_system_dirty = True
        finally:
            _version_system_upload_in_progress = False


async def flush_version_system_to_github() -> None:
    """Best-effort final push on graceful shutdown -- same mechanism as
    flush_mail_to_github()/flush_duo_to_github()."""
    global _version_system_dirty

    async with version_system_lock:
        if not _version_system_dirty:
            return
        data = _version_system_json_bytes()
        _version_system_dirty = False

    try:
        await github_commit_files({"version_system.json": data}, "Final version system sync (shutdown)")
        print("[versions] Flushed version system changes to GitHub before shutdown.")
    except Exception:
        async with version_system_lock:
            _version_system_dirty = True
        print("[versions] Failed to flush version system changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_version_system_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory -- same local-wins,
    GitHub-as-backfill priority as every other _sync_*_from_github_at_startup()
    in this file. If, after that, there's still no "started_at" (a
    genuinely first-ever launch, or a pre-existing file from before this
    field existed), it's set to now() right here, once -- this is the
    ONLY place "started_at" is ever written from scratch.
    """
    state = _load_version_system_json()
    if state is not None:
        print("[versions] Loaded version_system.json from local disk.")
    else:
        print("[versions] Local version_system.json is missing/unreadable/malformed -- trying GitHub as a backfill.")
        remote_bytes = await github_get_file("version_system.json")
        if remote_bytes is None:
            state = {}
        else:
            try:
                remote_data = json.loads(remote_bytes.decode("utf-8") or "{}")
                state = remote_data if isinstance(remote_data, dict) else {}
            except Exception:
                print("[versions] Downloaded version_system.json from GitHub was not valid JSON -- starting fresh.")
                traceback.print_exc()
                state = {}

    if "started_at" not in state:
        state["started_at"] = time.time()
        try:
            _atomic_write_bytes("version_system.json", json.dumps(state, indent=2).encode("utf-8"))
            mark_version_system_dirty()
            print(f"[versions] First-ever launch of the version system -- base-card period started now.")
        except Exception:
            print("[versions] Failed to persist the new version_system.json locally:")
            traceback.print_exc()

    return state


version_system = asyncio.run(_sync_version_system_from_github_at_startup())
version_system_lock = asyncio.Lock()


def base_card_period_active(now=None) -> bool:
    """Whether we're still within the 5-day base-card-only period
    following this system's one-time, persisted launch timestamp."""
    now = now if now is not None else time.time()
    started_at = version_system.get("started_at", now)
    return (now - started_at) < BASE_CARD_PERIOD_SECONDS


# =========================
# BACKUP STATUS (backup_status.json)
# =========================
# Tracks exactly one thing: the timestamp of the last `lbackup` GitHub
# commit that actually SUCCEEDED (see the LBACKUP COMMAND below).
# Persisted/synced with the exact same local-first + GitHub-backfill +
# debounced-sync + shutdown-flush pipeline every other piece of state
# in this file already uses (mail.json, duo.json, version_system.json,
# ...) -- no second persistence system, just that same one reused for
# one more small piece of state. The local file is created
# automatically the first time the bot starts (see
# _sync_backup_status_from_github_at_startup below) -- nothing needs
# to be created by hand.
#
# backup_status = { "last_successful_backup_at": float (unix
#   timestamp) }, key absent entirely if no `lbackup` has ever
#   succeeded yet -- lbackupstatus reports that explicitly rather than
#   showing a fake/zero timestamp.

def _load_backup_status_json():
    """Loads and validates the local backup_status.json. Same
    missing-vs-invalid contract as every other loader in this file --
    returns the parsed dict if valid, None if
    missing/unreadable/malformed/not a dict."""
    try:
        with open('backup_status.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[backup_status] Failed to read backup_status.json:")
        traceback.print_exc()
        return None

    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[backup_status] backup_status.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _backup_status_json_bytes() -> bytes:
    return json.dumps(backup_status, indent=2).encode("utf-8")


def save_backup_status_local() -> None:
    """Writes the current in-memory `backup_status` dict to
    backup_status.json, atomically -- same role as
    save_version_system_local()/save_mail_local()."""
    try:
        _atomic_write_bytes("backup_status.json", _backup_status_json_bytes())
    except Exception:
        print("[backup_status] Failed to save backup_status.json locally:")
        traceback.print_exc()
        raise


_backup_status_sync_task = None
_backup_status_dirty = False
_backup_status_upload_in_progress = False


def mark_backup_status_dirty() -> None:
    """Marks backup_status.json as having local changes not yet pushed
    to GitHub. Must be called while holding backup_status_lock,
    immediately after a successful save_backup_status_local()."""
    global _backup_status_dirty
    _backup_status_dirty = True


async def backup_status_github_sync_loop():
    """
    Background task: same debounced-commit pattern as
    version_system_github_sync_loop()/mail_github_sync_loop() -- wakes
    up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS and, only if there
    are unpushed local changes, performs exactly one GitHub commit.
    In normal operation this should rarely ever have anything to do,
    since `lbackup` already commits backup_status.json itself as part
    of its own single atomic commit -- this loop exists purely as the
    same safety net every other store gets, in case a local save ever
    succeeds without its matching commit going through.
    """
    global _backup_status_dirty, _backup_status_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _backup_status_dirty or _backup_status_upload_in_progress:
            continue

        async with backup_status_lock:
            if not _backup_status_dirty:
                continue
            data = _backup_status_json_bytes()
            _backup_status_dirty = False

        _backup_status_upload_in_progress = True
        try:
            await github_commit_files({"backup_status.json": data}, "Batched backup status sync")
        except Exception:
            print("[backup_status] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with backup_status_lock:
                _backup_status_dirty = True
        finally:
            _backup_status_upload_in_progress = False


async def flush_backup_status_to_github() -> None:
    """Best-effort final push on graceful shutdown -- same mechanism as
    flush_version_system_to_github()/flush_mail_to_github()."""
    global _backup_status_dirty

    async with backup_status_lock:
        if not _backup_status_dirty:
            return
        data = _backup_status_json_bytes()
        _backup_status_dirty = False

    try:
        await github_commit_files({"backup_status.json": data}, "Final backup status sync (shutdown)")
        print("[backup_status] Flushed backup status changes to GitHub before shutdown.")
    except Exception:
        async with backup_status_lock:
            _backup_status_dirty = True
        print("[backup_status] Failed to flush backup status changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_backup_status_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory -- same local-wins,
    GitHub-as-backfill priority as _sync_version_system_from_github_at_startup()
    and every other _sync_*_from_github_at_startup() in this file.

    Unlike version_system, nothing is auto-populated into the returned
    dict here: an absent "last_successful_backup_at" key just means no
    `lbackup` has ever succeeded yet, which lbackupstatus reports as-is
    rather than inventing a value. The local FILE itself, however, is
    always created here if it doesn't already exist (even if that just
    means writing "{}") -- so it's always present from the bot's very
    first startup onward, with no manual setup step required.
    """
    state = _load_backup_status_json()
    if state is not None:
        print("[backup_status] Loaded backup_status.json from local disk.")
    else:
        print("[backup_status] Local backup_status.json is missing/unreadable/malformed -- trying GitHub as a backfill.")
        remote_bytes = await github_get_file("backup_status.json")
        if remote_bytes is None:
            state = {}
        else:
            try:
                remote_data = json.loads(remote_bytes.decode("utf-8") or "{}")
                state = remote_data if isinstance(remote_data, dict) else {}
            except Exception:
                print("[backup_status] Downloaded backup_status.json from GitHub was not valid JSON -- starting fresh.")
                traceback.print_exc()
                state = {}

    if not os.path.exists('backup_status.json'):
        try:
            _atomic_write_bytes("backup_status.json", json.dumps(state, indent=2).encode("utf-8"))
            print("[backup_status] Created backup_status.json (no successful backup on record yet).")
        except Exception:
            print("[backup_status] Failed to create backup_status.json:")
            traceback.print_exc()

    return state


backup_status = asyncio.run(_sync_backup_status_from_github_at_startup())
backup_status_lock = asyncio.Lock()


# =========================
# MAINTENANCE MODE (maintenance.json)
# =========================
# Tracks exactly one thing: whether owner-declared maintenance mode is
# currently on (see `lmaintenance start`/`lmaintenance end` below).
# Persisted/synced with the exact same local-first + GitHub-backfill +
# debounced-sync + shutdown-flush pipeline every other piece of state
# in this file already uses (mail.json, duo.json, backup_status.json,
# ...) -- no second persistence system, just that same one reused for
# one more small piece of state, so maintenance mode survives a Railway
# redeploy instead of silently turning back off.
#
# maintenance = { "active": bool, "since": float (unix timestamp of the
#   last start/end toggle) }. Defaults to inactive if the key is
# missing entirely (e.g. a brand new bot that's never had maintenance
# toggled).

def _load_maintenance_json():
    """Loads and validates the local maintenance.json. Same
    missing-vs-invalid contract as every other loader in this file --
    returns the parsed dict if valid, None if
    missing/unreadable/malformed/not a dict."""
    try:
        with open('maintenance.json', 'r') as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        print("[maintenance] Failed to read maintenance.json:")
        traceback.print_exc()
        return None

    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[maintenance] maintenance.json contains invalid JSON:")
        traceback.print_exc()
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _maintenance_json_bytes() -> bytes:
    return json.dumps(maintenance, indent=2).encode("utf-8")


def save_maintenance_local() -> None:
    """Writes the current in-memory `maintenance` dict to
    maintenance.json, atomically -- same role as
    save_backup_status_local()/save_version_system_local()."""
    try:
        _atomic_write_bytes("maintenance.json", _maintenance_json_bytes())
    except Exception:
        print("[maintenance] Failed to save maintenance.json locally:")
        traceback.print_exc()
        raise


_maintenance_sync_task = None
_maintenance_dirty = False
_maintenance_upload_in_progress = False


def mark_maintenance_dirty() -> None:
    """Marks maintenance.json as having local changes not yet pushed to
    GitHub. Must be called while holding maintenance_lock, immediately
    after a successful save_maintenance_local()."""
    global _maintenance_dirty
    _maintenance_dirty = True


async def maintenance_github_sync_loop():
    """
    Background task: same debounced-commit pattern as
    backup_status_github_sync_loop()/version_system_github_sync_loop().
    Wakes up every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS and, only if
    there are unpushed local changes, performs exactly one GitHub
    commit.
    """
    global _maintenance_dirty, _maintenance_upload_in_progress
    while True:
        await asyncio.sleep(INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)

        if not _maintenance_dirty or _maintenance_upload_in_progress:
            continue

        async with maintenance_lock:
            if not _maintenance_dirty:
                continue
            data = _maintenance_json_bytes()
            _maintenance_dirty = False

        _maintenance_upload_in_progress = True
        try:
            await github_commit_files({"maintenance.json": data}, "Batched maintenance state sync")
        except Exception:
            print("[maintenance] Periodic GitHub sync failed (will retry next cycle):")
            traceback.print_exc()
            async with maintenance_lock:
                _maintenance_dirty = True
        finally:
            _maintenance_upload_in_progress = False


async def flush_maintenance_to_github() -> None:
    """Best-effort final push on graceful shutdown -- same mechanism as
    flush_backup_status_to_github()/flush_version_system_to_github()."""
    global _maintenance_dirty

    async with maintenance_lock:
        if not _maintenance_dirty:
            return
        data = _maintenance_json_bytes()
        _maintenance_dirty = False

    try:
        await github_commit_files({"maintenance.json": data}, "Final maintenance state sync (shutdown)")
        print("[maintenance] Flushed maintenance state changes to GitHub before shutdown.")
    except Exception:
        async with maintenance_lock:
            _maintenance_dirty = True
        print("[maintenance] Failed to flush maintenance state changes to GitHub on shutdown "
              "(they remain saved locally):")
        traceback.print_exc()


async def _sync_maintenance_from_github_at_startup() -> dict:
    """
    Startup-only, single load into memory -- same local-wins,
    GitHub-as-backfill priority as _sync_backup_status_from_github_at_startup()
    and every other _sync_*_from_github_at_startup() in this file. The
    local file is always created here if it doesn't already exist (even
    if that just means writing "{}"/inactive) -- so it's always present
    from the bot's very first startup onward.
    """
    state = _load_maintenance_json()
    if state is not None:
        print("[maintenance] Loaded maintenance.json from local disk.")
    else:
        print("[maintenance] Local maintenance.json is missing/unreadable/malformed -- trying GitHub as a backfill.")
        remote_bytes = await github_get_file("maintenance.json")
        if remote_bytes is None:
            state = {}
        else:
            try:
                remote_data = json.loads(remote_bytes.decode("utf-8") or "{}")
                state = remote_data if isinstance(remote_data, dict) else {}
            except Exception:
                print("[maintenance] Downloaded maintenance.json from GitHub was not valid JSON -- starting fresh.")
                traceback.print_exc()
                state = {}

    if not os.path.exists('maintenance.json'):
        try:
            _atomic_write_bytes("maintenance.json", json.dumps(state, indent=2).encode("utf-8"))
            print("[maintenance] Created maintenance.json (maintenance mode off by default).")
        except Exception:
            print("[maintenance] Failed to create maintenance.json:")
            traceback.print_exc()

    return state


maintenance = asyncio.run(_sync_maintenance_from_github_at_startup())
maintenance_lock = asyncio.Lock()


def _rebuild_card_prints_from_inventories() -> None:
    """
    Rebuilds the in-memory card_prints counter from the inventories that
    were just loaded above, instead of relying on card_prints itself
    surviving a restart (it doesn't -- it's never written to disk).

    For every owned card in every user's inventory, keeps the highest
    print number seen for that card's id. get_next_print() then resumes
    counting up from there, so a restart (crash or Railway redeploy)
    can no longer hand out a print number that's already owned by
    someone else. Purely a read over the already-loaded `inventories`;
    does not touch inventories.json's format or content, and does not
    write anything to disk itself.

    Also folds in every print number sitting in the recyclable-cards
    pool (pending_recovery[RECYCLABLE_CARDS_KEY]) -- active AND
    inactive alike. A pool entry is a print that's already been issued
    once; it isn't currently "live" in anyone's inventory, but the
    number itself is still spoken for; it must never be silently
    reused for a normal fresh drop just because it's temporarily
    sitting in the pool rather than in `inventories`. Without this, a
    pool print higher than that card's current live max would be
    invisible to this rebuild, and a completely ordinary future drop
    could eventually hand out that exact same number again.

    Always CLEARS card_prints first, then rebuilds it purely from what's
    actually in `inventories` (and the pool) right now -- this used to
    only ever raise each card's count, never lower it, so a fully wiped
    inventories.json (all claim data intentionally reset) left every
    card's claim count frozen at its old, stale high-water mark forever
    (since nothing else ever lowers card_prints, and this function ran
    once at startup and only compared upward). Clearing first means a
    wipe is reflected correctly the very next time this runs -- normal
    ongoing play is unaffected, since every currently-owned print (live
    or pooled) still gets counted right back in below.
    """
    card_prints.clear()
    for owned_cards in inventories.values():
        for owned_card in owned_cards:
            card_id = owned_card.get("card", {}).get("id")
            print_num = owned_card.get("print")
            if card_id is None or not isinstance(print_num, int):
                continue
            if print_num > card_prints.get(card_id, 0):
                card_prints[card_id] = print_num

    for entry in pending_recovery.get(RECYCLABLE_CARDS_KEY, []):
        card_id = entry.get("card_id")
        print_num = entry.get("print")
        if card_id is None or not isinstance(print_num, int):
            continue
        if print_num > card_prints.get(card_id, 0):
            card_prints[card_id] = print_num


_rebuild_card_prints_from_inventories()


def _convert_image_bytes_to_png_sync(raw_bytes):
    """
    Decodes raw uploaded image bytes (png, jpg, jpeg, webp, etc.) with
    Pillow and re-encodes them as a genuine PNG, converting to RGBA to
    preserve transparency where possible. Returns real PNG bytes -- not
    just the original bytes with a renamed extension.
    """
    img = Image.open(BytesIO(raw_bytes))
    img = img.convert("RGBA")

    output = BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


async def convert_image_bytes_to_png(raw_bytes):
    """Async wrapper so Pillow decode/encode work doesn't block the event loop."""
    return await asyncio.to_thread(_convert_image_bytes_to_png_sync, raw_bytes)


def generate_card_id(character_name, is_rare):
    """
    Generates a card ID automatically, based on rarity rather than the
    specific frame color.
    Format: <first_word>_common / <first_word>_rare, or with a numeric
    suffix (_2, _3, ...) if that base id + rarity combination already
    exists.
    """
    first_word = character_name.strip().split()[0].lower()
    rarity = "rare" if is_rare else "common"
    base_id = f"{first_word}_{rarity}"

    # Count existing cards that are exactly this base id, or this base id
    # with a numeric suffix (e.g. mydei_rare, mydei_rare_2, mydei_rare_3).
    pattern = re.compile(rf"^{re.escape(base_id)}(_\d+)?$")
    existing_count = sum(1 for card in cards if pattern.match(card["id"]))

    if existing_count == 0:
        return base_id
    else:
        return f"{base_id}_{existing_count + 1}"



"""
=========================
RENDERING
=========================
Completely rewritten card renderer for Luka (Discord card game bot).
Lives directly inside main.py -- not a separate module.

render_card_final() and render_drop() are called the same way they
always were elsewhere in this file; only their internals changed.

Expected directory layout:
    frames/
        common.png
        blue.png
        red.png
        yellow.png
        pink.png
        ...
    stars/
        star_1.png
        star_2.png
        star_3.png

Card dict fields used (matching cards.json):
    "name"   : str
    "series" : str
    "frame"  : str  -> "common" or any other value (treated as rare)
    "image"  : str  -> local path under card_art/ OR a remote URL
    "stars"  : int  -> 1-3, picks stars/star_<n>.png

Frame rule:
    "common"      -> print number drawn TOP-RIGHT
    anything else -> "rare", print number drawn TOP-LEFT

Stars rule:
    Only the "common" frame draws stars. Any rare frame (anything that
    isn't "common") never gets a star overlay.
"""

# ---------------------------------------------------------------------------
# CONFIG -- tweak freely, nothing else needs to change
# ---------------------------------------------------------------------------

CARD_WIDTH = 1536
CARD_HEIGHT = 2048

FRAME_DIR = "frames"
STAR_DIR = "stars"

# Original font paths/names -- unchanged
PRINT_FONT = "Fredoka-SemiBold.ttf"
TEXT_FONT = "Fredoka-SemiBold.ttf"
FONT_PATH = "Fredoka-SemiBold.ttf"

# Text sizes -- name a little smaller, series a touch smaller (still readable)
NAME_FONT_SIZE = 125
SERIES_FONT_SIZE = 65
PRINT_FONT_SIZE = 98

TEXT_COLOR = (255, 255, 255)
TEXT_STROKE_WIDTH = 5
TEXT_STROKE_COLOR = (0, 0, 0)

CENTER_X = CARD_WIDTH // 2
NAME_Y = 1540      # moved significantly higher, closer to the artwork
SERIES_Y = 1650    # raised with the name, +5px extra gap for spacing

# Maximum usable pixel width for the character name inside the decorative
# inner frame, so long names never touch/clip into the inner border.
# Not based on overall frame width -- adjust this directly if needed.
MAX_NAME_WIDTH = 700

# Amount to shrink the name font by (in px) when it's too wide at the
# default size, before falling back to wrapping onto two lines.
NAME_SHRINK_STEP = 30

# Vertical gap between the two lines when a name wraps to two lines.
NAME_LINE_SPACING = 95

# If the name wraps to two lines, the series text is pushed down by this
# much extra to keep spacing comfortable beneath the taller name block.
SERIES_Y_SHIFT_FOR_WRAPPED_NAME = 30

# Maximum usable pixel width for the series text -- reuses the same limit
# as the character name so long series names never touch/clip into the
# inner border.
MAX_SERIES_WIDTH = MAX_NAME_WIDTH

# Amount to shrink the series font by (in px) when it's too wide at the
# default size, before falling back to wrapping onto two lines.
SERIES_SHRINK_STEP = 15

# Vertical gap between the two lines when a series name wraps to two lines.
SERIES_LINE_SPACING = 50

# Print number position -- moved ~5px lower and slightly further right to
# match the original renderer's placement more closely.
PRINT_POS_COMMON = (1030, 325)
PRINT_POS_RARE = (380, 295)

# Print text is drawn left-anchored (anchor="la"), so it grows rightward
# from a fixed left edge as digits are added. A single digit (1-9) is
# already visually centered where it should be; two digits (10-99) and
# three digits (100) then extend further right than intended, so the x
# position is nudged left by these amounts to compensate. Legacy prints
# ("L") are a single character and use no shift, same as single digits.
PRINT_X_SHIFT_2_DIGITS = -12
PRINT_X_SHIFT_3_DIGITS = -40

# Gradient (Kita/Gachapon style: dark gray, not pure black) -- shorter now
# so it covers less of the artwork and the card reads brighter overall.
GRADIENT_COLOR = (25, 25, 28)
GRADIENT_HEIGHT_RATIO = 0.40   # portion of the card (from the bottom) the gradient covers
GRADIENT_START_ALPHA = 0
GRADIENT_END_ALPHA = 170

# Inner artwork area the gradient is clipped to, so it never bleeds onto
# the frame's decorative border/corners. This is the margin (in px) between
# the outer canvas edge and the visible inner artwork region -- adjust if
# the frame art's border thickness changes.
ARTWORK_INNER_MARGIN_X = 205
ARTWORK_INNER_MARGIN_TOP = 50
ARTWORK_INNER_MARGIN_BOTTOM = 100

# Rounded clip box the COMMON frame's gradient fades into (see
# _common_gradient_box). Sized tightly around the name/series text area
# (plus a little padding) instead of a large fraction of the card, so it
# never extends far up into the artwork.
COMMON_GRADIENT_BOX_RADIUS = 60
COMMON_GRADIENT_BOX_TOP_PADDING = 45
COMMON_GRADIENT_BOX_BOTTOM_PADDING = 65


# Per-frame gradient colors. "common" is intentionally absent -- it always
# uses GRADIENT_COLOR (the gray) above. Any frame name not listed here also
# falls back to GRADIENT_COLOR. To add a new rare frame's gradient color,
# just add an entry here -- no rendering logic needs to change.
#
# "white" is intentionally absent too: it's a rare-style frame (so it still
# renders with the rare gradient's box/placement via is_rare()), but its
# gradient color is meant to match Common's gray exactly, which it gets for
# free by falling through to GRADIENT_COLOR below. Do not add a "white"
# entry here, or it will start using its own tint instead of Common's gray.
FRAME_GRADIENT_COLORS = {
    "blue": (55, 125, 195),
    "red": (175, 55, 50),
    "pink": (225, 125, 165),
    "yellow": (210, 185, 85),
    "orange": (215, 135, 65),
    "green": (115, 175, 125),
    "purple": (145, 105, 185),
}


def get_gradient_color(frame_name: str) -> tuple:
    """
    Returns the bottom-gradient color for a given frame name. Common (and
    any unrecognized/future frame name not yet in FRAME_GRADIENT_COLORS)
    falls back to the default gray gradient.
    """
    return FRAME_GRADIENT_COLORS.get((frame_name or "").lower(), GRADIENT_COLOR)


# Drop image (two cards combined) -- spacing matches the original renderer
DROP_SPACING = 70
DROP_UPSCALE = 1.5   # higher output resolution so Discord shows it bigger/sharper

_font_cache = {}

# Static-asset caches. Frames, star overlays, and rendered gradient layers
# are fully deterministic (same input -> same output, no per-card
# variation), so each is loaded/rendered from disk exactly once and reused
# for every subsequent card instead of re-reading files or re-running the
# pixel-by-pixel gradient loop on every single render. Safe under
# concurrent access (see render_drop): every cache here is idempotent --
# two threads racing to fill the same key just compute the same
# deterministic value twice at worst, never a wrong or partial one.
_frame_cache = {}
_star_cache = {}
_gradient_cache = {}

# Persistent thread pool for rendering the two cards in a drop concurrently.
# Created once at import time and reused for every drop, instead of
# spinning up (and tearing down) a new pool on every single call.
_render_executor = ThreadPoolExecutor(max_workers=2)

# Separate, dedicated fixed-size executor for the OUTER render_drop()
# call itself -- distinct from _render_executor above, which is used
# INSIDE render_drop to render its two cards concurrently (an inner
# concern). Every render_drop() invocation builds a large combined-image
# buffer (~58MB at this resolution); dispatching the outer call through
# this dedicated 2-worker pool instead of asyncio's shared default
# executor (loop.run_in_executor(None, ...)) caps how many drops can be
# rendering AT ONCE across the whole bot -- a burst of simultaneous `ld`
# calls queues and waits its turn instead of each allocating one of
# these buffers in parallel without limit. This only limits how many
# renders run concurrently; it never changes drop selection, drop
# rates, cooldowns, timing, claiming, or any other gameplay behavior --
# and a single render's own speed/output is unaffected either way.
_drop_render_executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

# Font loader with fallbacks so we don't fall back to tiny default font
# (restored exactly from the original main.py)
def load_font(preferred_name, size):
    candidates = [preferred_name, "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "Arial.ttf"]
    for cand in candidates:
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    return ImageFont.load_default()


def get_font(size: int) -> ImageFont.FreeTypeFont:
    """Load Fredoka-SemiBold at the given size via the original fallback chain, cached per size."""
    if size not in _font_cache:
        _font_cache[size] = load_font(FONT_PATH, size)
    return _font_cache[size]


def is_rare(frame_name: str) -> bool:
    """Everything that isn't literally 'common' counts as rare."""
    return frame_name.lower() != "common"


def clean_url(url: str) -> str:
    """Cleans GitHub URLs to point to raw image assets."""
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url.split("?")[0]


def load_artwork_source(image_field: str, card_id=None) -> Image.Image:
    """
    Loads raw artwork from either a local card_art/ path or a remote URL.
    Mirrors the old get_image() behavior so existing cards.json entries
    keep working unchanged.

    TEMPORARY DEBUG LOGGING: every load attempt logs the card id, the
    resolved local path or URL, and the outcome -- success + dimensions,
    or the exact exception type/message (with traceback) and which
    fallback was used. This is here so that if a card ever renders
    without artwork again, the cause is visible in the logs instead of a
    single terse "IMAGE ERROR" line with no context. Safe to trim back to
    just the error-path logging once the pipeline has been observed to be
    stable.
    """
    log_prefix = f"[artwork] card={card_id!r} image_field={image_field!r}"

    try:
        if image_field and image_field.startswith("card_art/"):
            print(f"{log_prefix} source=local path={image_field!r}")
            if os.path.exists(image_field):
                img = Image.open(image_field).convert("RGBA")
                print(f"{log_prefix} OK (local) size={img.size}")
                return img
            print(f"{log_prefix} LOCAL IMAGE ERROR: path does not exist on disk -- using blank fallback")
            return Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (80, 80, 80, 255))

        if not image_field:
            print(f"{log_prefix} EMPTY image field -- using blank fallback")
            return Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (80, 80, 80, 255))

        url = clean_url(image_field)
        print(f"{log_prefix} source=remote url={url!r}")
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        img = Image.open(BytesIO(response.content)).convert("RGBA")
        print(f"{log_prefix} OK (remote) size={img.size}")
        return img

    except Exception as e:
        # Never swallow this silently: log the exact exception type/message,
        # a full traceback, and enough filesystem context (does the path
        # exist? what size is it?) to immediately tell apart a missing
        # file, a zero-byte/truncated file, and a corrupt-but-present
        # image, instead of having to guess.
        exists = os.path.exists(image_field) if image_field else False
        size_line = ""
        if exists:
            try:
                size_line = f"File size: {os.path.getsize(image_field)} bytes\n"
            except OSError as size_err:
                size_line = f"File size: <error reading size: {size_err}>\n"

        print(
            "========== ARTWORK LOAD ERROR ==========\n"
            f"Card ID: {card_id}\n"
            f"Image: {image_field}\n"
            f"Exists: {exists}\n"
            f"{size_line}"
            f"Exception: {type(e).__name__}: {e}\n"
            "Traceback:\n"
            f"{traceback.format_exc()}"
            "=======================================",
        )
        return Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (80, 80, 80, 255))


def center_crop_to_fill(image: Image.Image, target_size=(CARD_WIDTH, CARD_HEIGHT)) -> Image.Image:
    """
    Resize + center-crop so the artwork completely fills target_size
    with no empty space and no stretching/distortion (cover-fit).
    """
    target_w, target_h = target_size
    src_w, src_h = image.size

    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return image.crop((left, top, left + target_w, top + target_h))


def load_frame(frame_name: str) -> Image.Image:
    """Load the frame PNG by name, with a visible fallback if it's missing. Cached after first load."""
    if frame_name in _frame_cache:
        return _frame_cache[frame_name]

    path = os.path.join(FRAME_DIR, f"{frame_name}.png")

    if os.path.exists(path):
        frame = Image.open(path).convert("RGBA")
    else:
        print(f"FRAME NOT FOUND: {path} - using placeholder")
        frame = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (40, 40, 40, 255))
        d = ImageDraw.Draw(frame)
        d.rounded_rectangle(
            [30, 30, CARD_WIDTH - 30, CARD_HEIGHT - 30],
            radius=24, outline=(180, 180, 180, 255), width=10
        )

    if frame.size != (CARD_WIDTH, CARD_HEIGHT):
        frame = frame.resize((CARD_WIDTH, CARD_HEIGHT), Image.LANCZOS)

    _frame_cache[frame_name] = frame
    return frame


def load_star_overlay(card: dict):
    """
    Loads the star overlay for this card's star tier (1-3). Star assets
    are full 1536x2048 transparent overlays. Only called for common-frame
    cards -- rare frames never get a star overlay. Cached after first load.
    """
    tier = int(card.get("stars", 1))
    tier = min(max(tier, 1), 3)

    if tier in _star_cache:
        return _star_cache[tier]

    path = os.path.join(STAR_DIR, f"star_{tier}.png")

    if not os.path.exists(path):
        print(f"STAR NOT FOUND: {path}")
        _star_cache[tier] = None
        return None

    star = Image.open(path).convert("RGBA")
    if star.size != (CARD_WIDTH, CARD_HEIGHT):
        star = star.resize((CARD_WIDTH, CARD_HEIGHT), Image.LANCZOS)

    _star_cache[tier] = star
    return star


def create_bottom_gradient(size=(CARD_WIDTH, CARD_HEIGHT), color=GRADIENT_COLOR, clip_box=None, clip_radius=0, relative_fade=False) -> Image.Image:
    """
    Vertical gradient overlay, transparent at the top and linearly fading
    into `color` toward the bottom.

    By default (relative_fade=False), the fade spans GRADIENT_HEIGHT_RATIO
    of the full card height, exactly as before -- this is the path rare
    frames use, and its math is unchanged.

    If relative_fade=True, the fade is computed relative to clip_box's own
    top/bottom instead: 0% opacity right at the top of the box, ramping up
    to full opacity at the box's bottom. This is what the smaller common
    box uses, so it reads as a true smooth fade contained inside that box
    instead of a mostly-opaque block (which is what happens if a small
    clipped region only samples the middle of a much taller canvas-wide
    fade).

    If clip_box (left, top, right, bottom) is given, the gradient is
    clipped to that rectangle -- pixels outside it are dropped entirely
    (fully transparent), regardless of their computed alpha. Pass
    clip_radius > 0 to round that box's corners (used for the smaller
    common-frame box); rare frames keep clip_radius=0 for sharp corners,
    matching the original renderer. Leave clip_box as None (default) for
    a full card-width/height gradient with no clipping.

    The result is fully determined by (size, color, clip_box, clip_radius,
    relative_fade) -- there's no per-card variation -- so it's rendered
    once per distinct parameter combination and cached; every later call
    with the same parameters gets the exact same cached image back
    instead of re-running the pixel-row draw loop.
    """
    cache_key = (size, tuple(color), clip_box, clip_radius, relative_fade)
    if cache_key in _gradient_cache:
        return _gradient_cache[cache_key]

    width, height = size
    gradient = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)

    if relative_fade and clip_box is not None:
        fade_top = clip_box[1]
        fade_bottom = clip_box[3]
    else:
        fade_top = int(height * (1 - GRADIENT_HEIGHT_RATIO))
        fade_bottom = height

    for y in range(fade_top, min(fade_bottom, height)):
        progress = (y - fade_top) / max(1, (fade_bottom - fade_top))
        alpha = int(GRADIENT_START_ALPHA + (GRADIENT_END_ALPHA - GRADIENT_START_ALPHA) * progress)
        draw.line([(0, y), (width, y)], fill=(*color, alpha))

    if clip_box is None:
        _gradient_cache[cache_key] = gradient
        return gradient

    clip_mask = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(clip_mask)
    if clip_radius > 0:
        mask_draw.rounded_rectangle(clip_box, radius=clip_radius, fill=255)
    else:
        mask_draw.rectangle(clip_box, fill=255)

    clipped = Image.new("RGBA", size, (0, 0, 0, 0))
    clipped.paste(gradient, (0, 0), clip_mask)

    _gradient_cache[cache_key] = clipped
    return clipped


@functools.lru_cache(maxsize=None)
def _inner_artwork_box(size=(CARD_WIDTH, CARD_HEIGHT)):
    """The rare-frame clip box: full inner artwork area inset by the
    ARTWORK_INNER_MARGIN_* constants. Pure function of constants only, so
    it's memoized instead of recomputed on every render."""
    width, height = size
    return (
        ARTWORK_INNER_MARGIN_X,
        ARTWORK_INNER_MARGIN_TOP,
        width - ARTWORK_INNER_MARGIN_X,
        height - ARTWORK_INNER_MARGIN_BOTTOM,
    )


@functools.lru_cache(maxsize=None)
def _common_gradient_box(size=(CARD_WIDTH, CARD_HEIGHT)):
    """
    Smaller rounded box the COMMON frame's gradient is clipped into --
    same left/right margins as the rare frame's inner artwork box, but
    sized tightly around the name/series text area (plus a little
    padding) instead of extending far up into the artwork. Accounts for
    the name and/or series each possibly wrapping to two lines. Pure
    function of constants only, so it's memoized instead of recomputed
    on every render.
    """
    width, height = size

    # Topmost point the (possibly two-line) name can reach.
    text_top = NAME_Y - (NAME_LINE_SPACING // 2) - (NAME_FONT_SIZE // 2)
    # Bottommost point the (possibly two-line, shifted) series can reach.
    text_bottom = SERIES_Y + SERIES_Y_SHIFT_FOR_WRAPPED_NAME + SERIES_LINE_SPACING + (SERIES_FONT_SIZE // 2)

    box_left = ARTWORK_INNER_MARGIN_X
    box_right = width - ARTWORK_INNER_MARGIN_X
    box_top = text_top - COMMON_GRADIENT_BOX_TOP_PADDING
    box_bottom = min(
        height - ARTWORK_INNER_MARGIN_BOTTOM,
        text_bottom + COMMON_GRADIENT_BOX_BOTTOM_PADDING
    )

    return (box_left, box_top, box_right, box_bottom)


def draw_text_with_outline(draw: ImageDraw.ImageDraw, position, text, font, anchor="la"):
    """White fill text with a black outline, using Pillow's native stroke support."""
    draw.text(
        position, text, font=font,
        fill=TEXT_COLOR, stroke_width=TEXT_STROKE_WIDTH, stroke_fill=TEXT_STROKE_COLOR,
        anchor=anchor,
    )


def _text_pixel_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """Measures the actual rendered pixel width of text (including outline stroke) with Pillow."""
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=TEXT_STROKE_WIDTH)
    return bbox[2] - bbox[0]


def _best_two_line_split(draw: ImageDraw.ImageDraw, words: list, font):
    """
    Finds the split point (breaking only at spaces, never mid-word) that
    produces the most visually balanced two lines, by minimizing the
    rendered pixel-width difference between the two resulting lines.
    """
    best_diff = None
    best_split = (words[0], " ".join(words[1:]))

    for i in range(1, len(words)):
        line1 = " ".join(words[:i])
        line2 = " ".join(words[i:])
        diff = abs(_text_pixel_width(draw, line1, font) - _text_pixel_width(draw, line2, font))

        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_split = (line1, line2)

    return best_split


def draw_character_name(draw: ImageDraw.ImageDraw, name_text: str, center_x: int, base_y: int) -> bool:
    """
    Draws the character name centered at (center_x, base_y), using its
    actual rendered pixel width (measured with Pillow, not character
    count) to decide how to fit it within MAX_NAME_WIDTH:
      1. Render at the normal size if it already fits.
      2. Otherwise shrink the font by NAME_SHRINK_STEP px and re-measure.
      3. If it still doesn't fit, wrap onto two balanced centered lines,
         breaking only at spaces between words (never mid-word).

    Returns True if the name was wrapped onto two lines (so the caller can
    shift the series text down for spacing), False for a single line.
    """
    font = get_font(NAME_FONT_SIZE)
    width = _text_pixel_width(draw, name_text, font)

    if width <= MAX_NAME_WIDTH:
        draw_text_with_outline(draw, (center_x, base_y), name_text, font, anchor="mm")
        return False

    shrunk_size = max(NAME_FONT_SIZE - NAME_SHRINK_STEP, 1)
    shrunk_font = get_font(shrunk_size)
    width = _text_pixel_width(draw, name_text, shrunk_font)

    if width <= MAX_NAME_WIDTH:
        draw_text_with_outline(draw, (center_x, base_y), name_text, shrunk_font, anchor="mm")
        return False

    words = name_text.split()

    if len(words) <= 1:
        # Nothing to break on -- render as a single (still shrunk) line.
        draw_text_with_outline(draw, (center_x, base_y), name_text, shrunk_font, anchor="mm")
        return False

    line1, line2 = _best_two_line_split(draw, words, shrunk_font)

    draw_text_with_outline(draw, (center_x, base_y - NAME_LINE_SPACING // 2), line1, shrunk_font, anchor="mm")
    draw_text_with_outline(draw, (center_x, base_y + NAME_LINE_SPACING // 2), line2, shrunk_font, anchor="mm")
    return True


def draw_series_text(draw: ImageDraw.ImageDraw, series_text: str, font, center_x: int, base_y: int):
    """
    Draws the series name centered under the character name, using its
    actual rendered pixel width (measured with Pillow, not character/word
    count) to decide how to fit it within MAX_SERIES_WIDTH:
      1. Render at the normal size if it already fits.
      2. Otherwise shrink the font by SERIES_SHRINK_STEP px and re-measure.
      3. If it still doesn't fit, wrap onto two balanced centered lines,
         breaking only at spaces between words (never mid-word).

    Unlike the character name, the series text's vertical position never
    shifts when it wraps: the first line stays anchored at base_y and the
    second line is simply placed SERIES_LINE_SPACING below it.
    """
    width = _text_pixel_width(draw, series_text, font)

    if width <= MAX_SERIES_WIDTH:
        draw_text_with_outline(draw, (center_x, base_y), series_text, font, anchor="mm")
        return

    shrunk_size = max(SERIES_FONT_SIZE - SERIES_SHRINK_STEP, 1)
    shrunk_font = get_font(shrunk_size)
    width = _text_pixel_width(draw, series_text, shrunk_font)

    if width <= MAX_SERIES_WIDTH:
        draw_text_with_outline(draw, (center_x, base_y), series_text, shrunk_font, anchor="mm")
        return

    words = series_text.split()

    if len(words) <= 1:
        # Nothing to break on -- render as a single (still shrunk) line.
        draw_text_with_outline(draw, (center_x, base_y), series_text, shrunk_font, anchor="mm")
        return

    line1, line2 = _best_two_line_split(draw, words, shrunk_font)

    draw_text_with_outline(draw, (center_x, base_y), line1, shrunk_font, anchor="mm")
    draw_text_with_outline(draw, (center_x, base_y + SERIES_LINE_SPACING), line2, shrunk_font, anchor="mm")


# ---------------------------------------------------------------------------
# CARD RENDERING
# ---------------------------------------------------------------------------

def render_card(card: dict, print_num, hide_print: bool = False, force_real_print: bool = False) -> Image.Image:
    """
    Renders a single full card and returns a PIL Image (in memory).

    Rendering order:
        1. Load + center-crop artwork
        2. Bottom gradient
        3. Frame
        4. Stars
        5. Print number (skipped entirely if hide_print=True)
        6. Character name
        7. Series

    force_real_print: merchant-reward-card-only. When True, the print
    number is drawn via format_merchant_print() (real number, e.g.
    #237) instead of format_print() (which collapses anything past 100
    to "L"). Defaults to False, so every existing caller's output is
    byte-for-byte unchanged. See get_next_merchant_print/
    format_merchant_print above for why merchant rewards need this.
    """
    frame_name = card.get("frame", "common")
    rare = is_rare(frame_name)

    # 1. Artwork
    art_source = load_artwork_source(card.get("image", ""), card_id=card.get("id"))
    canvas = center_crop_to_fill(art_source).convert("RGBA")
    # Release the original decoded artwork immediately -- center_crop_to_fill
    # already produced a fully independent resized+cropped copy (PIL's
    # resize() always allocates fresh pixel data; it is never a lazy
    # view onto the source image), and load_artwork_source() never
    # returns a cached/shared object, so nothing anywhere below this
    # line, or after render_card() returns, ever reads art_source
    # again. Freeing it here (rather than waiting for it to fall out of
    # scope at the end of this function) is a real RAM saving,
    # especially with several cards rendering concurrently. Does not
    # change canvas/the rendered result in any way.
    art_source.close()
    del art_source

    # 2. Gradient (color depends on frame -- common stays gray, rare
    # frames get their own subtle tint via FRAME_GRADIENT_COLORS).
    # Both rare and common now clip the fading gradient into a box so it
    # never bleeds onto the frame's decorative border/corners: rare uses
    # the full inner-artwork box with sharp corners (unchanged), common
    # uses a smaller, rounded box so it echoes that boxed look while
    # staying noticeably smaller/quieter.
    if rare:
        gradient_layer = create_bottom_gradient(
            color=get_gradient_color(frame_name),
            clip_box=_inner_artwork_box(),
        )
    else:
        gradient_layer = create_bottom_gradient(
            color=get_gradient_color(frame_name),
            clip_box=_common_gradient_box(),
            clip_radius=COMMON_GRADIENT_BOX_RADIUS,
            relative_fade=True,
        )

    canvas = Image.alpha_composite(canvas, gradient_layer)

    # 3. Frame
    canvas = Image.alpha_composite(canvas, load_frame(frame_name))

    # 4. Stars (common frame only -- rare frames never get stars)
    if not rare:
        star = load_star_overlay(card)
        if star is not None:
            canvas = Image.alpha_composite(canvas, star)

    draw = ImageDraw.Draw(canvas)

    # 5. Print number -- common: top-right area, rare: top-left area
    # (coordinates + anchor restored to match the original renderer's placement)
    # Card art shows just the number (no "#"); format_print() itself is left
    # untouched since it's still used with the "#" elsewhere in the bot.
    if not hide_print:
        print_source = format_merchant_print(print_num) if force_real_print else format_print(print_num)
        print_text = print_source.lstrip("#")
        print_font = get_font(PRINT_FONT_SIZE)
        base_x, base_y = PRINT_POS_RARE if rare else PRINT_POS_COMMON

        if print_text.isdigit():
            if len(print_text) == 2:
                base_x += PRINT_X_SHIFT_2_DIGITS
            elif len(print_text) >= 3:
                base_x += PRINT_X_SHIFT_3_DIGITS

        print_pos = (base_x, base_y)
        draw_text_with_outline(draw, print_pos, print_text, print_font, anchor="la")

    # 6. Character name (large) -- shrinks and/or wraps to two balanced
    # lines if it would otherwise exceed MAX_NAME_WIDTH
    name_wrapped = draw_character_name(draw, card.get("name", "Unknown"), CENTER_X, NAME_Y)

    # 7. Series (smaller, wraps to a second centered line past 4 words)
    series_font = get_font(SERIES_FONT_SIZE)
    series_y = SERIES_Y + SERIES_Y_SHIFT_FOR_WRAPPED_NAME if name_wrapped else SERIES_Y
    draw_series_text(draw, card.get("series", "Unknown Series"), series_font, CENTER_X, series_y)

    return canvas


def render_card_final(card: dict, print_num, hide_print: bool = False, force_real_print: bool = False) -> str:
    """
    Drop-in replacement for the old render_card_final().
    Same contract: renders one card, saves it to a temp PNG, and returns
    the file path. Existing callers don't need to change at all -- pass
    hide_print=True to render the card without its print number (used only
    by the lup command), or force_real_print=True for a merchant-reward
    owned card (see render_card above).
    """
    try:
        final = render_card(card, print_num, hide_print=hide_print, force_real_print=force_real_print)
    except Exception as e:
        print("RENDER ERROR:", e)
        final = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (30, 30, 30, 255))
        d = ImageDraw.Draw(final)
        d.text((50, 50), f"Render Error: {str(e)[:200]}", font=get_font(40), fill=(255, 255, 255))

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    # compress_level=1: PNG compression is lossless, so this has zero effect
    # on the decoded pixel data -- it only trades a bit of file size for
    # meaningfully faster encoding than Pillow's default (6).
    final.save(temp_file.name, compress_level=1)
    return temp_file.name


async def send_card_added_notification(bot, card: dict):
    """
    Posts a "new card added" announcement to CARD_UPDATES_CHANNEL_ID.
    Only ever called after laddcard has fully succeeded (image saved,
    cards.json updated, GitHub sync completed if applicable) -- if that
    hasn't happened, this is never invoked, so no notification is sent.

    Silently does nothing if CARD_UPDATES_CHANNEL_ID is 0 or the channel
    truly can't be found (even after a fetch_channel fallback). Never
    raises -- any failure here is logged and swallowed so it can't turn an
    already-successful card creation into a reported error.

    TEMPORARY DEBUG LOGGING: prints each stage to the console so a card
    updates channel issue can be diagnosed from logs instead of guessed
    at. This doesn't change the (intentionally silent-to-the-user)
    behavior at all -- it only makes that existing behavior visible in
    the console. Safe to trim back once the pipeline is confirmed working.
    """
    print("[Card Updates] Notification started")
    print(f"[Card Updates] Channel ID: {CARD_UPDATES_CHANNEL_ID}")

    if not CARD_UPDATES_CHANNEL_ID:
        print("[Card Updates] Channel ID is 0/falsy -- notifications disabled, skipping.")
        return

    channel = bot.get_channel(CARD_UPDATES_CHANNEL_ID)
    print(f"[Card Updates] Channel found via get_channel: {channel is not None}")

    if channel is None:
        # get_channel only checks the client's internal cache -- it can
        # return None even for a perfectly valid channel id if that guild
        # or channel hasn't been cached. Fall back to an actual API fetch
        # before giving up.
        print("[Card Updates] get_channel returned None -- trying bot.fetch_channel()...")
        try:
            channel = await bot.fetch_channel(CARD_UPDATES_CHANNEL_ID)
            print(f"[Card Updates] fetch_channel succeeded: {channel!r}")
        except Exception as e:
            print(f"[Card Updates] fetch_channel FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
            return

    if channel is None:
        print("[Card Updates] Channel still None after fetch_channel -- giving up silently.")
        return

    image_path = None
    try:
        # Reuses the existing renderer with hide_print=True -- same flag
        # already used elsewhere (e.g. lup) to render a card without its
        # print number, instead of a second renderer.
        print("[Card Updates] Rendering thumbnail...")
        image_path = render_card_final(card, peek_next_print(card.get("id")), hide_print=True)

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = (
            f"### Character: `{card.get('name', 'Unknown Character')}`\n"
            f"### Series: `{card.get('series', 'Unknown Series')}`\n"
            f"### Frame: `{card.get('frame', 'Unknown')}`\n"
            f"### Stars: `{card.get('stars', 1)}`"
        )

        print("[Card Updates] Sending embed...")
        if image_path:
            file = discord.File(image_path, filename="card.png")
            embed.set_thumbnail(url="attachment://card.png")
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)

        print("[Card Updates] Notification sent successfully")
    except Exception as e:
        print(f"[Card Updates] FAILED while rendering/sending: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if image_path:
            try:
                os.remove(image_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# DROP IMAGE (combine two rendered cards side by side)
# ---------------------------------------------------------------------------

def combine_cards(images: list, spacing: int = DROP_SPACING, upscale: float = DROP_UPSCALE) -> Image.Image:
    """
    Combine rendered card images side by side with spacing, then upscale
    the final image so it displays larger and sharper in Discord.
    """
    count = len(images)
    total_w = (CARD_WIDTH * count) + (spacing * (count - 1))
    total_h = CARD_HEIGHT

    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))

    x = 0
    for img in images:
        canvas.alpha_composite(img, (x, 0))
        x += CARD_WIDTH + spacing

    if upscale and upscale != 1.0:
        new_size = (round(total_w * upscale), round(total_h * upscale))
        canvas = canvas.resize(new_size, Image.LANCZOS)

    return canvas


def render_drop(card1: dict, print1, card2: dict, print2) -> str:
    """
    Drop-in replacement for the old render_drop().
    Same contract: renders both cards, combines them side by side,
    saves to a temp PNG, and returns the file path.

    The two cards are rendered concurrently (one worker thread each) using
    a persistent, module-level thread pool (avoids the overhead of
    spawning/tearing down threads on every single drop) instead of one
    after another. render_card()'s only shared state is the module-level
    asset caches (fonts/frames/stars/gradients), and those are only ever
    populated with the same deterministic value no matter which thread
    gets there first (see the cache comments above), so this is safe and
    produces pixel-identical output to rendering sequentially -- just
    faster, since most of PIL's underlying image work releases the GIL
    while it runs.
    """
    t_render_start = time.perf_counter()
    try:
        future1 = _render_executor.submit(render_card, card1, print1)
        future2 = _render_executor.submit(render_card, card2, print2)
        img1 = future1.result()
        img2 = future2.result()
        t_rendered = time.perf_counter()
        combined = combine_cards([img1, img2])
        t_combined = time.perf_counter()
    except Exception as e:
        print("RENDER ERROR:", e)
        combined = Image.new("RGBA", (CARD_WIDTH * 2 + DROP_SPACING, CARD_HEIGHT), (30, 30, 30, 255))
        d = ImageDraw.Draw(combined)
        d.text((50, 50), f"Render Error: {str(e)[:200]}", font=get_font(40), fill=(255, 255, 255))
        t_rendered = t_combined = time.perf_counter()

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    # PNG SAVE SETTING (this is the actual `ld` slowdown -- confirmed via
    # the render_drop timing print below): `optimize=True` makes Pillow
    # run its full PNG optimizer -- multiple filter strategies plus a
    # maximum zlib pass -- which is CPU-expensive on an image this size
    # (two full cards, upscaled by DROP_UPSCALE) and was adding several
    # seconds to every single drop.
    #
    # The ORIGINAL problem this was trying to fix was compress_level=1
    # (PNG's near-store setting), which barely compresses at all and
    # produced files large enough to trip Discord's 413 Payload Too
    # Large. PNG is always lossless regardless of this setting --
    # compress_level only trades encode CPU time for file size, it can
    # NEVER change a single decoded pixel -- so a normal, non-maximal
    # level fixes the 413 without paying the optimizer's full cost.
    # compress_level=6 is zlib's own standard default (a deliberate
    # speed/size balance point, not a low-effort setting) and comes in
    # far below the old level-1 file size while encoding a small
    # fraction of optimize=True's time.
    combined.save(temp_file.name, format="PNG", compress_level=6)
    t_saved = time.perf_counter()

    file_size_mb = os.path.getsize(temp_file.name) / 1_000_000
    print(
        f"[Drop Render] Final image: {combined.size[0]}x{combined.size[1]}px, "
        f"format=PNG, size={file_size_mb:.2f} MB"
    )
    # TEMPORARY instrumentation to identify the `ld` bottleneck --
    # measurement only, safe to remove once confirmed.
    print(
        "render_drop timing:\n"
        f"- Render both cards (concurrent): {(t_rendered - t_render_start) * 1000:.1f} ms\n"
        f"- Combine into one image: {(t_combined - t_rendered) * 1000:.1f} ms\n"
        f"- PNG encode+save (compress_level=6): {(t_saved - t_combined) * 1000:.1f} ms\n"
        f"- Total render_drop: {(t_saved - t_render_start) * 1000:.1f} ms"
    )

    return temp_file.name


# =========================
# TARGET USER RESOLUTION (shared by lbadges / lshowcase)
# =========================
async def resolve_target_user(message, args: str):
    """
    Resolves the target user for a command in the same style as the
    rest of the bot: an explicit mention, a replied-to message, a raw
    ID, then a username/display-name fallback, and finally the command
    author if nothing else matched.
    """
    if message.mentions:
        return message.mentions[0]

    if message.reference and message.reference.resolved:
        replied_msg = message.reference.resolved
        if replied_msg and replied_msg.author:
            return replied_msg.author

    args = (args or "").strip()
    if args:
        first_part = args.split()[0]

        if first_part.isdigit() and message.guild:
            member = message.guild.get_member(int(first_part))
            if member:
                return member

        if message.guild:
            lowered = first_part.lower()
            for member in message.guild.members:
                if member.name.lower() == lowered or member.display_name.lower() == lowered:
                    return member
            for member in message.guild.members:
                if lowered in member.name.lower() or lowered in member.display_name.lower():
                    return member

    return message.author


def _as_member(message, target_user):
    """
    Best-effort upgrade of a resolved target (which may be a plain
    discord.User, e.g. from message.mentions in some contexts) into a
    guild discord.Member, so role-based checks (the OG badge) work.
    Falls back to returning target_user unchanged if no Member can be
    found -- role checks on it then simply see no roles.
    """
    if isinstance(target_user, discord.Member):
        return target_user
    if message.guild:
        member = message.guild.get_member(target_user.id)
        if member:
            return member
    return target_user


# =========================
# BADGES (computed fresh every time -- NEVER stored)
# =========================

# Number of distinct series that must be fully completed to earn
# Completionist (own every card from N different series).
COMPLETIONIST_SERIES_TARGET = 5

BADGE_DEFINITIONS = [
    {"id": "collector", "emoji": "🗂️", "name": "Collector", "description": "Own at least 150 cards."},
    {"id": "hoarder", "emoji": "📦", "name": "Hoarder", "description": "Own at least 40 cards of ONE character."},
    {"id": "secret_admirer", "emoji": "💕", "name": "Secret Admirer", "description": "Own at least 15 cards of ONE character."},
    {"id": "completionist", "emoji": "📚", "name": "Completionist", "description": "Own every card from 5 different series."},
    {"id": "perfectionist", "emoji": "🏷️", "name": "Perfectionist", "description": "Tag at least 50 cards."},
    {"id": "og", "emoji": "⏳", "name": "OG", "description": "Has the Early Supporter role."},
    {"id": "hundred_hunter", "emoji": "💯", "name": "Hundred Hunter", "description": "Own at least one Print #100."},
    {"id": "lucky_pull", "emoji": "1️⃣", "name": "Lucky Pull", "description": "Own at least one Print #1."},
    {"id": "rarity_hunter", "emoji": "🏆", "name": "Rarity Hunter", "description": "Own 100 four-star cards."},
    {"id": "explorer", "emoji": "🌍", "name": "Explorer", "description": "Own cards from 100 different characters."},
    {"id": "crowd_favorite", "emoji": "❤️", "name": "Crowd Favorite", "description": "Receive 15 showcase votes."},
    {"id": "trendsetter", "emoji": "🔥", "name": "Trendsetter", "description": "Receive 100 showcase votes."},
]


def _series_character_totals() -> dict:
    """
    {series: total unique character names in that series}, computed
    fresh from the live `cards` list every call so a newly-added card
    (a new character, or the first card of a brand new series) is
    reflected immediately -- no migration needed.
    """
    totals = {}
    for card in cards:
        series = card.get("series", "Unknown Series")
        name = card.get("name", "Unknown")
        totals.setdefault(series, set()).add(name)
    return {series: len(names) for series, names in totals.items()}


def compute_badge_progress(member, inv: list) -> dict:
    """
    Computes every badge's current progress/completion for `member`
    from their LIVE inventory (plus, for OG, their live roles).
    Nothing here is ever read from or written to a stored "badge"
    record -- this always reflects the player's current data/statistics.

    Returns {badge_id: (current, target, completed)}.
    """
    total_owned = len(inv)

    per_character_counts = {}
    per_series_owned = {}
    tagged_count = 0
    has_print_100 = False
    has_print_1 = False
    four_star_count = 0

    for owned_card in inv:
        card = owned_card.get("card", {})
        name = card.get("name", "Unknown")
        series = card.get("series", "Unknown Series")

        per_character_counts[name] = per_character_counts.get(name, 0) + 1
        per_series_owned.setdefault(series, set()).add(name)

        if owned_card.get("tags"):
            tagged_count += 1

        print_num = owned_card.get("print")
        if print_num == 100:
            has_print_100 = True
        if print_num == 1:
            has_print_1 = True

        if card.get("stars") == 4:
            four_star_count += 1

    largest_character_count = max(per_character_counts.values(), default=0)
    # Distinct characters owned, for Explorer -- just the number of keys
    # per_character_counts already collected above, live every call.
    distinct_character_count = len(per_character_counts)

    # Completionist: count how many DISTINCT series the player has fully
    # completed (owns every character in that series), not just the
    # single closest one.
    series_totals = _series_character_totals()
    completed_series_count = sum(
        1 for series, total in series_totals.items()
        if total > 0 and len(per_series_owned.get(series, set())) >= total
    )

    has_og_role = any(role.id == EARLY_SUPPORTER_ROLE_ID for role in getattr(member, "roles", []))

    # Showcase vote count is read live from showcase_votes.json every
    # call, never stored as part of the badge itself -- same "always
    # computed fresh" rule as every other badge here.
    vote_count = get_vote_count(getattr(member, "id", None))

    return {
        "collector": (total_owned, 150, total_owned >= 150),
        "hoarder": (largest_character_count, 40, largest_character_count >= 40),
        "secret_admirer": (largest_character_count, 15, largest_character_count >= 15),
        "completionist": (
            min(completed_series_count, COMPLETIONIST_SERIES_TARGET),
            COMPLETIONIST_SERIES_TARGET,
            completed_series_count >= COMPLETIONIST_SERIES_TARGET,
        ),
        "perfectionist": (tagged_count, 50, tagged_count >= 50),
        "og": (1 if has_og_role else 0, 1, has_og_role),
        "hundred_hunter": (1 if has_print_100 else 0, 1, has_print_100),
        "lucky_pull": (1 if has_print_1 else 0, 1, has_print_1),
        "rarity_hunter": (min(four_star_count, 100), 100, four_star_count >= 100),
        "explorer": (min(distinct_character_count, 100), 100, distinct_character_count >= 100),
        "crowd_favorite": (min(vote_count, 15), 15, vote_count >= 15),
        "trendsetter": (min(vote_count, 100), 100, vote_count >= 100),
    }


def compute_star_rating(completed_badges: int, total_badges: int) -> str:
    """
    Dynamic 0-5 star rating string from completed/total badges. There
    are no hardcoded badge-count thresholds here -- the ratio is
    recalculated against whatever `total_badges` currently is, so
    adding or removing a badge from BADGE_DEFINITIONS automatically
    adjusts every rating without touching this function.
    """
    if total_badges <= 0:
        filled_stars = 0
    else:
        filled_stars = round((completed_badges / total_badges) * 5)
    filled_stars = max(0, min(5, filled_stars))
    empty_stars = 5 - filled_stars
    return ("★" * filled_stars) + ("☆" * empty_stars)


def _badge_star_rating_for(target_user, member) -> str:
    """
    Convenience wrapper: computes target_user's live badge progress and
    returns their star rating string (see compute_star_rating). Does
    not change the calculation itself -- just a shared spot for the
    places that need it (lshowcase, lbadges, and the showcase's "View
    Badges" button), so the star rating always reflects the exact same
    completed/total ratio wherever it's shown.
    """
    inv = get_inventory(target_user.id)
    progress = compute_badge_progress(member, inv)
    completed = sum(1 for (_, _, c) in progress.values() if c)
    total = len(BADGE_DEFINITIONS)
    return compute_star_rating(completed, total)


def _ordered_badge_blocks(target_user, member) -> list:
    """
    Computes every badge's progress fresh (see compute_badge_progress)
    and returns one pre-formatted display block per badge -- completed
    badges first, incomplete underneath -- as the exact text format
    `lbadges` uses:

        {emoji} **{name}**
        -# {description}
        -# **Completed!**  (or)  -# **Progress: current/target**

    Single source of truth shared by both the plain lbadges embed and
    its pagination, and by the showcase's "View Badges" button.
    """
    inv = get_inventory(target_user.id)
    progress = compute_badge_progress(member, inv)

    completed_blocks = []
    incomplete_blocks = []

    for badge in BADGE_DEFINITIONS:
        current, target, completed = progress[badge["id"]]
        status_line = "-# **Completed!**" if completed else f"-# **Progress: {current}/{target}**"
        block = (
            f"{badge['emoji']} **{badge['name']}**\n"
            f"-# {badge['description']}\n"
            f"{status_line}"
        )
        (completed_blocks if completed else incomplete_blocks).append(block)

    return completed_blocks + incomplete_blocks


# =========================
# COLLECTION PROGRESS (lprogress / lmissing)
# =========================
# Divider matching the exact style requested for these two commands.
# Distinct from SHOWCASE_DIVIDER (a different character/length already
# used elsewhere) rather than repurposing it, since these commands
# specify their own visual layout.
PROGRESS_DIVIDER = "━" * 18

# How many series to list in lprogress's "Highest Completion" section.
PROGRESS_TOP_SERIES_COUNT = 3


def _series_character_name_sets() -> dict:
    """
    {series: set of every unique character name in that series},
    computed fresh from the live `cards` list every call -- the same
    grouping _series_character_totals() already does for the badge
    system, just keeping the actual name sets instead of collapsing
    them to a count, since lmissing needs the real missing names, not
    just how many there are.
    """
    result = {}
    for card in cards:
        series = card.get("series", "Unknown Series")
        name = card.get("name", "Unknown")
        result.setdefault(series, set()).add(name)
    return result


def _series_max_stars() -> dict:
    """
    {series: highest 'stars' value among any card in that series} --
    purely cosmetic, used to prefix a series name with the right
    number of ★ in lprogress's Highest Completion list, using the same
    filled-only star convention already used for individual cards
    elsewhere (e.g. lfindseries) rather than the badge system's
    padded-to-5 rating.
    """
    result = {}
    for card in cards:
        series = card.get("series", "Unknown Series")
        stars = card.get("stars", 1)
        if stars > result.get(series, 0):
            result[series] = stars
    return result


def _compute_collection_progress(inv: list) -> dict:
    """
    Shared collection-progress computation for both lprogress and
    lmissing.

    Series completion (series_completed/series_in_progress, and every
    per-series set below) is character-based, same as the Completionist
    badge: owning at least one copy of a name -- regardless of which
    rarity/frame version it came from -- counts as "collecting" that
    character, so a series is "complete" once every unique character
    name in it has been collected at least once.

    The overall owned_card_count/total_card_count pair below is
    DIFFERENT on purpose: it counts actual card ENTRIES (unique "id"
    values, e.g. Common and Rare versions of the same character are two
    separate entries) rather than unique characters -- this is what
    lprogress's "Cards Collected" line uses. A card is "collected" here
    once the player owns at least one copy of that specific id;
    duplicate copies of the same id don't count extra, since this is
    still a completion-style stat, not a raw inventory size.

    Returns a dict with:
        series_totals      -- {series: total unique character names}
        series_name_sets   -- {series: set of every character name in it}
        per_series_owned   -- {series: set of character names THIS inventory owns}
        owned_characters   -- total unique characters owned across all series
        total_characters   -- total unique characters that exist
        series_completed   -- count of series fully owned
        series_in_progress -- count of series with >=1 owned but not complete
        owned_card_count   -- distinct card ids (versions) this inventory owns
        total_card_count   -- distinct card ids (versions) that exist, from cards.json
        owned_card_ids     -- set of every card id this inventory owns at least
                              one copy of -- used by lmissing to show missing
                              specific versions/stars per character, not just
                              missing character names.
    """
    series_totals = _series_character_totals()
    series_name_sets = _series_character_name_sets()
    total_characters = sum(series_totals.values())

    per_series_owned = {}
    owned_card_ids = set()
    for owned_card in inv:
        card = owned_card.get("card", {})
        series = card.get("series", "Unknown Series")
        name = card.get("name", "Unknown")
        per_series_owned.setdefault(series, set()).add(name)
        owned_card_ids.add(card.get("id"))

    owned_characters = sum(len(names) for names in per_series_owned.values())

    # Distinct by "id" so Common/Rare (or any other alternate version)
    # are never collapsed into a single entry, per spec.
    total_card_ids = {c.get("id") for c in cards}
    owned_card_count = len(owned_card_ids)
    total_card_count = len(total_card_ids)

    series_completed = 0
    series_in_progress = 0
    for series, total in series_totals.items():
        if total <= 0:
            continue
        owned_count = len(per_series_owned.get(series, set()))
        if owned_count >= total:
            series_completed += 1
        elif owned_count > 0:
            series_in_progress += 1

    return {
        "series_totals": series_totals,
        "series_name_sets": series_name_sets,
        "per_series_owned": per_series_owned,
        "owned_characters": owned_characters,
        "total_characters": total_characters,
        "series_completed": series_completed,
        "series_in_progress": series_in_progress,
        "owned_card_count": owned_card_count,
        "total_card_count": total_card_count,
        "owned_card_ids": owned_card_ids,
    }


def _match_series(query: str):
    """
    Resolves a user-typed series name to the canonical series string as
    stored on cards -- exact match (case-insensitive) preferred,
    falling back to a substring match. Mirrors the exact two-tier
    matching lfindseries already uses, so series lookups behave
    identically everywhere in the bot. Returns None if nothing matches.
    """
    query = (query or "").strip().lower()
    if not query:
        return None

    for card in cards:
        if card.get("series", "").lower() == query:
            return card.get("series")

    for card in cards:
        if query in card.get("series", "").lower():
            return card.get("series")

    return None


def _character_missing_stars(series: str, name: str, owned_card_ids: set) -> list:
    """
    For one character (identified by name+series, same grouping used
    everywhere else -- CharacterVersionView, the version migration,
    etc.), returns the sorted, deduplicated list of `stars` values
    belonging to that character's cards that are NOT present in
    `owned_card_ids` (matched by exact card id) -- i.e. exactly which
    version(s) of this character are missing, using the actual star
    numbers already on each card rather than a separate "version"
    label. An empty list means every version of this character is
    already owned.
    """
    missing_stars = set()
    for card in cards:
        if card.get("series") != series or card.get("name") != name:
            continue
        if card.get("id") not in owned_card_ids:
            missing_stars.add(card.get("stars", 1))
    return sorted(missing_stars)


def _missing_character_lines(series: str, names, owned_card_ids: set) -> list:
    """
    For each character name in `names` (all within `series`), returns
    one formatted "Name ★1, ★2" display line for any character missing
    at least one version -- fully-collected characters (zero missing
    versions) are omitted entirely. Shared by every lmissing display
    mode (single-series, the overview's per-series pages, and the
    comparison view) so this exact "Name ★star, ★star" formatting is
    never re-implemented per caller.
    """
    lines = []
    for name in sorted(names):
        missing_stars = _character_missing_stars(series, name, owned_card_ids)
        if not missing_stars:
            continue
        star_text = ", ".join(f"★{s}" for s in missing_stars)
        lines.append(f"{name} {star_text}")
    return lines


async def _parse_missing_args(message, raw_args: str):
    """
    lmissing's argument grammar is `[series name] [other user]`, which
    is different from every other command's `[other user]` grammar
    (see resolve_target_user) -- the user reference here is optional
    AND comes after a free-text series name that can itself contain
    spaces/colons, so it can't reuse resolve_target_user's "args IS the
    user token" assumption directly. It still reuses the same
    underlying resolution mechanics -- reply author, a real mention,
    guild.get_member() (cache) falling back to guild.fetch_member()
    (API) for a raw id -- just applied at a different position in the
    string. The fetch fallback is what makes all three forms (reply,
    mention, raw id) actually behave the same: a mention/reply always
    resolves to a real member object regardless of cache state, so a
    raw id that's simply not cached shouldn't silently fail either.

    Returns (series_query, other_user_or_None).
    """
    if message.reference and message.reference.resolved:
        replied_msg = message.reference.resolved
        if replied_msg and replied_msg.author:
            return raw_args.strip(), replied_msg.author

    if message.mentions:
        other_user = message.mentions[0]
        # Strip the mention token itself back out, so it's not left
        # sitting inside the series name text.
        series_query = re.sub(r"<@!?\d+>", "", raw_args).strip()
        return series_query, other_user

    parts = raw_args.split()
    if parts and parts[-1].isdigit() and len(parts[-1]) >= 15 and message.guild:
        user_id_candidate = int(parts[-1])
        member = message.guild.get_member(user_id_candidate)
        if member is None:
            # Not cached -- fetch it, same as a mention/reply would
            # always resolve regardless of cache state. A genuinely
            # invalid id (left the server, typo, wrong guild) just
            # falls through to treating the whole string as a series
            # name below, same as before.
            try:
                member = await message.guild.fetch_member(user_id_candidate)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member:
            series_query = " ".join(parts[:-1]).strip()
            return series_query, member

    return raw_args.strip(), None


def _build_progress_embed(target_user, stats: dict) -> discord.Embed:
    """
    Builds the (non-paginated) lprogress embed from an already-computed
    stats dict. "Cards Collected"/"Cards Remaining" use the card-entry
    counts (owned_card_count/total_card_count -- distinct by "id", so
    alternate versions like Common/Rare are never collapsed together).
    Series Completed/In Progress and Highest Completion below are
    unchanged and stay character-based, exactly as before.
    """
    owned = stats["owned_card_count"]
    total = stats["total_card_count"]
    percent = (owned / total * 100) if total else 0.0
    remaining = total - owned

    series_max_stars = _series_max_stars()
    ranked_series = []
    for series, series_total in stats["series_totals"].items():
        if series_total <= 0:
            continue
        owned_count = len(stats["per_series_owned"].get(series, set()))
        if owned_count <= 0:
            continue
        ranked_series.append((series, owned_count / series_total * 100))
    ranked_series.sort(key=lambda pair: pair[1], reverse=True)

    lines = [
        f"## {target_user.mention}'s Collection Progress",
        PROGRESS_DIVIDER,
        "**Cards Collected**",
        f"> {owned} / {total} • {percent:.1f}%",
        PROGRESS_DIVIDER,
        "**Series Completed**",
        f"> {stats['series_completed']}",
        "**Series In Progress**",
        f"> {stats['series_in_progress']}",
        PROGRESS_DIVIDER,
        "**Cards Remaining**",
        f"> {remaining}",
        PROGRESS_DIVIDER,
        "**Highest Completion**",
        "",
    ]

    if ranked_series:
        for series, series_percent in ranked_series[:PROGRESS_TOP_SERIES_COUNT]:
            star_str = "★" * series_max_stars.get(series, 1)
            lines.append(f"{star_str} {series} • {series_percent:.0f}%")
    else:
        lines.append("*No cards collected yet.*")

    embed = discord.Embed(color=THEME_COLOR, description="\n".join(lines))
    embed.set_author(name=f"@{target_user.display_name}", icon_url=target_user.display_avatar.url)
    embed.set_thumbnail(url=target_user.display_avatar.url)
    return embed


def _build_missing_overview_embed(target_user, incomplete_series: list, total_pages: int) -> discord.Embed:
    """Page 1 of lmissing: every incomplete series, fewest-remaining first."""
    lines = ["## Missing Cards", "", "**Nearly Complete**", PROGRESS_DIVIDER]

    if incomplete_series:
        for series, remaining in incomplete_series:
            lines.append(f"- {series}\n-# {remaining} remaining\n")
    else:
        lines.append("*Every series is fully complete!*")

    embed = discord.Embed(color=THEME_COLOR, description="\n".join(lines).rstrip())
    embed.set_author(name=f"@{target_user.display_name}", icon_url=target_user.display_avatar.url)
    embed.set_footer(text=f"Page 1/{total_pages}")
    return embed


def _build_missing_series_embed(target_user, series: str, stats: dict, page_num=None, total_pages=None) -> discord.Embed:
    """
    One full page for a single series: each character missing at least
    one version, shown with the specific missing star number(s) (e.g.
    "Gojo ★1, ★2, ★4") rather than just the character's name -- plus
    the same character-based collected/remaining summary as before.
    """
    total = stats["series_totals"].get(series, 0)
    owned_set = stats["per_series_owned"].get(series, set())
    all_names = stats["series_name_sets"].get(series, set())
    owned_card_ids = stats.get("owned_card_ids", set())

    # Collected/Remaining stay character-based, exactly as before --
    # only the missing LIST below now shows specific missing versions.
    missing_names = sorted(all_names - owned_set)
    owned_count = len(owned_set)
    remaining = len(missing_names)

    missing_lines = _missing_character_lines(series, all_names, owned_card_ids)

    lines = [f"**{series}**", PROGRESS_DIVIDER, ""]
    if missing_lines:
        code_block = "\n".join(f"X {line}" for line in missing_lines)
        lines.append(f"```{code_block}```")
    else:
        lines.append("*Nothing missing here -- fully collected!*")
    lines += [
        PROGRESS_DIVIDER,
        "**Collected**",
        f"-# {owned_count} / {total}",
        "Remaining",
        f"**{remaining}**",
    ]

    embed = discord.Embed(color=THEME_COLOR, description="\n".join(lines))
    embed.set_author(name=f"@{target_user.display_name}", icon_url=target_user.display_avatar.url)
    if page_num is not None and total_pages is not None:
        embed.set_footer(text=f"Page {page_num}/{total_pages}")
    return embed


def _build_missing_comparison_embed(series: str, other_user, missing_lines: list) -> discord.Embed:
    """
    Comparison mode: one "Name ★star, ★star" line per character in
    `series` that other_user owns a version of that the requester
    doesn't -- specific missing versions, not just bare character names.
    """
    embed = discord.Embed(color=THEME_COLOR)
    embed.title = series
    if missing_lines:
        embed.description = "\n\n".join(missing_lines)
    else:
        embed.description = f"You already own every card from this series that {other_user.mention} has."
    return embed


class MissingCardsPaginationView(discord.ui.View):
    """
    Generic "list of already-built pages" pagination view for
    lmissing. Unlike BadgesPaginationView/OwnersPaginationView (which
    slice ONE flat list into uniform per-page chunks), lmissing's pages
    are heterogeneous -- one overview page, then one full page per
    series -- so each embed is built once up front and this view just
    flips between them. Same button style/emoji and same
    "only the requester can page" rule as every other pagination view
    in the bot.
    """
    def __init__(self, embeds: list, user_id: int):
        super().__init__(timeout=90)
        self.embeds = embeds
        self.user_id = user_id
        self.page = 0
        self._update_button_states()

    def _update_button_states(self):
        self.previous.disabled = (self.page <= 0)
        self.next.disabled = (self.page >= len(self.embeds) - 1)

    def current_embed(self) -> discord.Embed:
        return self.embeds[self.page]

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.page > 0:
            self.page -= 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.page < len(self.embeds) - 1:
            self.page += 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


class BadgesPaginationView(discord.ui.View):
    """
    Paginates the already-computed, completed-first-ordered badge
    blocks for target_user, BADGES_PER_PAGE per page. Title and
    thumbnail are rebuilt identically on every page; only the
    description (which 4 badges are shown) changes. Everything is
    computed once up front (compute_badge_progress is pure/in-memory),
    so paging back and forth here never does any extra work.
    """
    def __init__(self, target_user, blocks, user_id, star_rating: str = ""):
        super().__init__(timeout=90)
        self.target_user = target_user
        self.blocks = blocks
        self.user_id = user_id
        self.star_rating = star_rating
        self.page = 0
        self.max_page = max(0, (len(blocks) - 1) // BADGES_PER_PAGE) if blocks else 0
        self._update_button_states()

    def _update_button_states(self):
        self.previous.disabled = (self.page <= 0)
        self.next.disabled = (self.page >= self.max_page)

    def build_embed(self) -> discord.Embed:
        title = f"{self.target_user.display_name}'s Collection Badges"
        if self.star_rating:
            title = f"{title}\n{self.star_rating}"
        embed = discord.Embed(
            color=THEME_COLOR,
            title=title,
        )
        embed.set_thumbnail(url=self.target_user.display_avatar.url)

        start = self.page * BADGES_PER_PAGE
        page_blocks = self.blocks[start:start + BADGES_PER_PAGE]
        embed.description = "\n\n".join(page_blocks)

        if self.max_page > 0:
            embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")

        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)

        if self.page > 0:
            self.page -= 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)

        if self.page < self.max_page:
            self.page += 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class MailboxPaginationView(discord.ui.View):
    """
    Paginates a user's mail, one letter per page -- same prev/next
    pagination row and button-state pattern as BadgesPaginationView,
    just with one letter instead of a chunk of badge blocks per page.
    Row 1 has "Read" (marks only the currently-viewed letter as read),
    "Reply" (targets THAT letter's original sender, reusing the exact
    same lmail @user send/persist flow), and "🚫 Block" (adds that
    letter's sender to this mailbox owner's blocked-senders list, so
    they can no longer send new mail here -- one-way, never affects the
    owner's own ability to mail that sender back). Row 2 has "Read All"
    (marks every unread letter in the whole mailbox as read at once).
    Read letters stay visible (still paginated through normally) but no
    longer count toward the unread mail reminder.
    """
    def __init__(self, letters: list, user_id: int):
        super().__init__(timeout=90)
        # `letters` are already enriched with _sender_name/_sender_avatar
        # by _resolve_mail_sender_info() -- this view never fetches
        # users itself, it's purely presentational.
        self.letters = letters
        self.user_id = user_id
        self.page = 0
        self.max_page = max(0, len(letters) - 1)
        self._update_button_states()

    def _update_button_states(self):
        self.previous.disabled = (self.page <= 0)
        self.next.disabled = (self.page >= self.max_page)
        self.mark_read.disabled = bool(self.letters[self.page].get("read"))
        self.read_all.disabled = not any(not l.get("read") for l in self.letters)

        # Block button: disabled only if the current letter's sender
        # can't be identified/targeted at all (same validity check as
        # Reply), or is the viewer themselves. It no longer disables
        # once already blocked -- the button is now a toggle, so it
        # stays enabled and its handler decides block vs. unblock based
        # on the sender's current state.
        letter = self.letters[self.page]
        sender_id = letter.get("sender_id")
        sender_valid = bool(sender_id) and str(sender_id).isdigit() and int(sender_id) != self.user_id
        self.block_sender_btn.disabled = not sender_valid

    def build_embed(self) -> discord.Embed:
        letter = self.letters[self.page]
        status = "Read" if letter.get("read") else "Unread"

        embed = discord.Embed(
            color=THEME_COLOR,
            title=f"Mail from {letter.get('_sender_name', 'Unknown user')}",
            description=f"### {letter.get('message')}" if letter.get("message") else "*(empty message)*",
        )
        if letter.get("_sender_avatar"):
            embed.set_thumbnail(url=letter["_sender_avatar"])

        timestamp = letter.get("timestamp")
        if timestamp:
            embed.add_field(name="Sent", value=f"<t:{int(timestamp)}:F>", inline=True)
        embed.add_field(name="Status", value=status, inline=True)

        if self.max_page > 0:
            embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")

        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your mailbox!", ephemeral=True)

        if self.page > 0:
            self.page -= 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your mailbox!", ephemeral=True)

        if self.page < self.max_page:
            self.page += 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Read", style=discord.ButtonStyle.primary, row=1)
    async def mark_read(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your mailbox!", ephemeral=True)

        letter = self.letters[self.page]
        if letter.get("read"):
            return await interaction.response.send_message(
                "This letter is already marked as read.", ephemeral=True
            )

        async with mail_lock:
            updated = mark_letter_read(self.user_id, letter.get("id"))
            if not updated:
                return await interaction.response.send_message(
                    "This letter no longer exists.", ephemeral=True
                )
            try:
                save_mail_local()
                mark_mail_dirty()
            except Exception:
                # Roll back the in-memory change so mail.json and the
                # `mail` dict stay consistent with each other.
                for real_letter in get_mailbox(self.user_id):
                    if real_letter.get("id") == letter.get("id"):
                        real_letter["read"] = False
                        break
                return await interaction.response.send_message(
                    "Something went wrong saving your mail. Please try again.", ephemeral=True
                )

        letter["read"] = True
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Reply", style=discord.ButtonStyle.secondary, row=1)
    async def reply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your mailbox!", ephemeral=True)

        letter = self.letters[self.page]
        sender_id = letter.get("sender_id")

        if not sender_id or not str(sender_id).isdigit():
            return await interaction.response.send_message(
                "This letter's sender can't be identified.", ephemeral=True
            )

        if int(sender_id) == self.user_id:
            return await interaction.response.send_message(
                "You can't reply to yourself.", ephemeral=True
            )

        bot = interaction.client
        sender_user = bot.get_user(int(sender_id))
        if sender_user is None:
            try:
                sender_user = await bot.fetch_user(int(sender_id))
            except Exception:
                sender_user = None

        if sender_user is None:
            return await interaction.response.send_message(
                "This letter's sender could no longer be found.", ephemeral=True
            )
        if sender_user.bot:
            return await interaction.response.send_message(
                "You can't send mail to a bot.", ephemeral=True
            )

        await interaction.response.send_message(
            f"Replying to **{sender_user.display_name}** -- check below!",
            ephemeral=True
        )

        # Same send/persist flow as `lmail @user` -- see
        # _run_mail_sending_flow. This mailbox view/message is left
        # exactly as-is; the reply happens as its own follow-up prompt
        # in the channel, same as running the command directly would.
        await _run_mail_sending_flow(bot, interaction.channel, interaction.user, sender_user)

    @discord.ui.button(label="🚫 Block", style=discord.ButtonStyle.danger, row=1)
    async def block_sender_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your mailbox!", ephemeral=True)

        letter = self.letters[self.page]
        sender_id = letter.get("sender_id")

        if not sender_id or not str(sender_id).isdigit():
            return await interaction.response.send_message(
                "This letter's sender can't be identified.", ephemeral=True
            )

        if int(sender_id) == self.user_id:
            return await interaction.response.send_message(
                "You can't block yourself.", ephemeral=True
            )

        # Blocking is one-way and only ever affects mail FROM sender_id
        # TO this mailbox's owner -- it never touches the owner's own
        # ability to mail sender_id back. Persisted through the exact
        # same mail_lock/save_mail_local/mark_mail_dirty/rollback
        # sequence every other mail mutation in this view already uses.
        #
        # This button is a toggle: it checks the sender's CURRENT
        # blocked state and performs the opposite action, so the same
        # "🚫 Block" button both blocks and unblocks -- no separate
        # Unblock button.
        currently_blocked = is_sender_blocked(sender_id, self.user_id)

        async with mail_lock:
            if currently_blocked:
                changed = unblock_sender(self.user_id, sender_id)
            else:
                changed = block_sender(self.user_id, sender_id)

            if not changed:
                # Someone else already flipped this state concurrently;
                # nothing to persist, just report the current reality.
                msg = (
                    f"**{letter.get('_sender_name', 'This user')}** is already blocked."
                    if not currently_blocked
                    else f"**{letter.get('_sender_name', 'This user')}** isn't blocked."
                )
                return await interaction.response.send_message(msg, ephemeral=True)

            try:
                save_mail_local()
                mark_mail_dirty()
            except Exception:
                # Roll back the in-memory change so it stays consistent
                # with mail.json.
                if currently_blocked:
                    block_sender(self.user_id, sender_id)
                else:
                    get_blocked_senders(self.user_id).remove(str(sender_id))
                return await interaction.response.send_message(
                    "Something went wrong saving that. Please try again.", ephemeral=True
                )

        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if currently_blocked:
            await interaction.followup.send(
                f"Unblocked **{letter.get('_sender_name', 'this user')}** -- they can send you mail again.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Blocked **{letter.get('_sender_name', 'this user')}** -- they can no longer send you mail.",
                ephemeral=True
            )

    @discord.ui.button(label="Read All", style=discord.ButtonStyle.secondary, row=2)
    async def read_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your mailbox!", ephemeral=True)

        async with mail_lock:
            mailbox = get_mailbox(self.user_id)
            # Snapshot exactly which letters were unread BEFORE touching
            # anything, so a failed save can roll back precisely these
            # (and only these) -- never letters that were already read
            # coming in. A single pass + a single save_mail_local() call
            # regardless of mailbox size, so this stays fast even at
            # 50+ letters (no per-letter I/O).
            newly_read_ids = {l.get("id") for l in mailbox if not l.get("read")}

            if not newly_read_ids:
                return await interaction.response.send_message(
                    "You have no unread mail.", ephemeral=True
                )

            for real_letter in mailbox:
                if real_letter.get("id") in newly_read_ids:
                    real_letter["read"] = True

            try:
                save_mail_local()
                mark_mail_dirty()
            except Exception:
                for real_letter in mailbox:
                    if real_letter.get("id") in newly_read_ids:
                        real_letter["read"] = False
                return await interaction.response.send_message(
                    "Something went wrong saving your mail. Please try again.", ephemeral=True
                )

        # Reflect the change in this view's own (already-fetched) copy
        # too, so the current page/footer update immediately without
        # needing to reopen `lmail`.
        for l in self.letters:
            if l.get("id") in newly_read_ids:
                l["read"] = True

        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# =========================
# SHOWCASE IMAGE (Pillow)
# =========================
def _build_showcase_card_shadow(rendered_card: Image.Image) -> Image.Image:
    """
    Builds a soft, blurred drop-shadow silhouette matching a rendered
    card's own shape (via its alpha channel), so it works regardless
    of the card art's shape. Showcase-only -- render_card and every
    other renderer in the bot are untouched.
    """
    alpha = rendered_card.split()[-1]
    shadow = Image.new("RGBA", rendered_card.size, (0, 0, 0, 0))
    black_layer = Image.new("RGBA", rendered_card.size, (0, 0, 0, SHOWCASE_SHADOW_OPACITY))
    shadow.paste(black_layer, (0, 0), mask=alpha)
    return shadow.filter(ImageFilter.GaussianBlur(SHOWCASE_SHADOW_BLUR_RADIUS))


_showcase_background_cache = None


def _get_showcase_background() -> Image.Image:
    """
    Loads + resizes the showcase background exactly once and caches it.
    Unlike card_art (which lupdateimage can change at any time), this
    asset never changes while the bot is running -- there's no command
    that touches it -- so re-opening and re-resizing it from disk on
    every single `lshowcase` call was pure waste. Every caller still
    takes a .copy() before compositing onto it (see generate_showcase_image
    below), so the cached original can never be mutated.
    """
    global _showcase_background_cache
    if _showcase_background_cache is not None:
        return _showcase_background_cache

    if os.path.exists(SHOWCASE_BACKGROUND_PATH):
        background = Image.open(SHOWCASE_BACKGROUND_PATH).convert("RGBA")
        if background.size != SHOWCASE_CANVAS_SIZE:
            background = background.resize(SHOWCASE_CANVAS_SIZE, Image.LANCZOS)
    else:
        print(f"SHOWCASE BACKGROUND NOT FOUND: {SHOWCASE_BACKGROUND_PATH} - using placeholder")
        background = Image.new("RGBA", SHOWCASE_CANVAS_SIZE, (20, 20, 20, 255))

    _showcase_background_cache = background
    return _showcase_background_cache


def generate_showcase_image(showcased_owned_cards: list) -> str:
    """
    Renders the showcase image: the fixed background at
    SHOWCASE_CANVAS_SIZE, with 0-3 already-rendered cards placed at
    the fixed SHOWCASE_POSITIONS (never calculated dynamically), each
    with a soft drop-shadow behind it. Saves to a temp PNG and returns
    its path (same contract as render_card_final / render_drop). With
    zero showcased cards, only the background is drawn -- no
    placeholders, no text, no slots.
    """
    canvas = _get_showcase_background().copy()

    count = len(showcased_owned_cards)
    if count > 0:
        positions = SHOWCASE_POSITIONS[count]
        for owned_card, (x, y) in zip(showcased_owned_cards, positions):
            card = owned_card["card"]
            rendered = render_card(card, owned_card["print"])
            # Uniform resize only (same 3:4 aspect ratio as the native
            # render) -- never a crop or a disproportionate stretch.
            rendered = rendered.resize(SHOWCASE_CARD_SIZE, Image.LANCZOS)
            # SHOWCASE_POSITIONS store the top-left corner for the
            # native 210x280 card size. Shift by half the size
            # difference in each axis so the card's CENTER lands
            # exactly where it always has, even though the card itself
            # is now larger.
            paste_x = x - (SHOWCASE_CARD_SIZE[0] - 210) // 2
            paste_y = y - (SHOWCASE_CARD_SIZE[1] - 280) // 2

            # Shadow first (offset down/right, blurred, low opacity),
            # so the card is pasted on top of it, not the other way
            # around.
            shadow = _build_showcase_card_shadow(rendered)
            shadow_x = paste_x + SHOWCASE_SHADOW_OFFSET[0]
            shadow_y = paste_y + SHOWCASE_SHADOW_OFFSET[1]
            canvas.alpha_composite(shadow, (shadow_x, shadow_y))

            canvas.alpha_composite(rendered, (paste_x, paste_y))

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    canvas.save(temp_file.name, format="PNG", optimize=True)
    return temp_file.name


# =========================
# OWNER CONFIRMATION VIEW (shared by dangerous owner-only commands)
# =========================
class OwnerConfirmView(discord.ui.View):
    """
    Generic Confirm/Cancel button pair for a single dangerous owner-only
    action -- shared by `lresetinventories` and `lrecyclecards` rather
    than each command building its own. `on_confirm` is an async
    callback -- on_confirm(interaction) -- called only once, only after
    the SAME owner who issued the original command presses Confirm; it
    is entirely responsible for actually performing (and persisting)
    the action and reporting the result. Both buttons disable
    themselves and the view stops itself immediately on press, so a
    double-click can never run the action twice, and the view disables
    itself on timeout so an ignored confirmation can never be actioned
    late.
    """
    def __init__(self, owner_id, on_confirm, timeout=60):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.on_confirm = on_confirm
        self.message = None

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "This isn't your confirmation.", ephemeral=True
            )
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(view=self)
        await self.on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "This isn't your confirmation.", ephemeral=True
            )
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(content="Cancelled -- no changes were made.", embed=None, view=self)

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# =========================
# OWNER READ-ONLY CARD LIST VIEW (shared by lpendingrecovery/llukainventory)
# =========================
class AdminCardListView(discord.ui.View):
    """
    Generic, purely read-only one-card-per-page pager for owner admin
    tools -- shared by `lpendingrecovery` and `llukainventory` rather
    than each building its own near-identical pagination, the same
    "reuse instead of duplicate" pattern as OwnerConfirmView above.
    Never mutates anything: entries are already-built display data
    (plain dicts), not live references into `inventories`/
    `pending_recovery`, so paging through this can never regenerate or
    change any state.
    """
    def __init__(self, title: str, entries: list, owner_id: int):
        super().__init__(timeout=90)
        self.title = title
        self.entries = entries
        self.owner_id = owner_id
        self.page = 0
        self.max_page = max(0, len(entries) - 1)
        self._update_button_states()

    def _update_button_states(self):
        self.previous.disabled = (self.page <= 0)
        self.next.disabled = (self.page >= self.max_page)

    def build_embed(self) -> discord.Embed:
        if not self.entries:
            return discord.Embed(color=THEME_COLOR, title=self.title, description="*(nothing to show)*")

        entry = self.entries[self.page]
        embed = discord.Embed(color=THEME_COLOR, title=self.title, description=entry.get("description", ""))
        for name, value in entry.get("fields", []):
            embed.add_field(name=name, value=value, inline=True)
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This isn't your view.", ephemeral=True)
        if self.page > 0:
            self.page -= 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This isn't your view.", ephemeral=True)
        if self.page < self.max_page:
            self.page += 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# =========================
# 1. DROP VIEW
# =========================
class CardView(discord.ui.View):
    def __init__(self, card1, card2, dropper_id):
        super().__init__(timeout=CLAIM_TIME_LIMIT)
        self.card1 = card1
        self.card2 = card2
        self.card1_claimed = False
        self.card2_claimed = False

        # Drop priority ("Drop Powers"): for DROP_PRIORITY_SECONDS after
        # this drop is posted, only `dropper_id` may claim. Ends early --
        # before the timer runs out -- the instant the dropper lands a
        # successful claim (see claim() below). Purely additive: nobody
        # but the dropper is affected once the window closes, and the
        # dropper themself is never restricted by it.
        self.dropper_id = dropper_id
        self.drop_time = time.time()
        self.priority_active = True

    def _priority_blocks(self, user_id) -> bool:
        """
        True if `user_id` must be silently ignored right now because the
        dropper's exclusive priority window is still open. Never blocks
        the dropper. Lazily turns itself off once the window naturally
        expires, so it only needs to be checked here.
        """
        if user_id == self.dropper_id or not self.priority_active:
            return False
        if time.time() - self.drop_time >= DROP_PRIORITY_SECONDS:
            self.priority_active = False
            return False
        return True

    async def claim(self, interaction, which, button):
        user_id = interaction.user.id

        # Drop priority: anyone but the dropper is completely ignored
        # during the window -- no response of any kind, per spec.
        if self._priority_blocks(user_id):
            return

        # Checked BEFORE any cooldown/bonus logic below. This used to run
        # AFTER the bonus-claim consumption block, which meant a user who
        # clicked an already-claimed slot while on cooldown had their
        # bonus claim spent and then got nothing -- an extra claim must
        # only ever be spent once a card is actually about to be granted.
        if which == 1 and self.card1_claimed:
            return await interaction.response.send_message("Already claimed.", ephemeral=True)
        if which == 2 and self.card2_claimed:
            return await interaction.response.send_message("Already claimed.", ephemeral=True)

        card = self.card1 if which == 1 else self.card2

        now = time.time()

        if user_id in claim_cooldowns:
            remaining = int(CLAIM_COOLDOWN - (now - claim_cooldowns[user_id]))
        else:
            remaining = 0

        # Duo bonus claims: same "only spent when actually needed" rule
        # as bonus drops in `ld` -- only touched when the normal
        # cooldown would otherwise block this claim, never on an
        # already-off-cooldown claim. Doesn't change claiming's own
        # logic/odds/behavior otherwise.
        used_bonus_claim = False
        if remaining > 0:
            async with duo_lock:
                if consume_bonus(user_id, "claim"):
                    used_bonus_claim = True
                    try:
                        save_duo_local()
                        mark_duo_dirty()
                    except Exception:
                        add_bonus(user_id, "claim", 1)
                        used_bonus_claim = False

            if not used_bonus_claim:
                return await interaction.response.send_message(
                    f"Wait {format_time(remaining)} before claiming again.", ephemeral=True
                )

        # Recycled-card exception (see get_weighted_card()/lrecyclecards):
        # resolved BEFORE touching inventories_lock, same structural
        # pattern as the duo bonus consumption just above -- an entirely
        # separate lock, fully settled first. A recycled print isn't
        # reserved at drop time (same as a normal print's peek-only
        # preview number isn't), so if two concurrent drops both
        # happened to offer this exact same recycled entry, only the
        # FIRST claim to actually reach this point gets it;
        # consume_recycled_entry() returns None for the second, which
        # then falls straight through to a completely normal, freshly-
        # assigned print via add_card() below -- the user still gets a
        # real, valid card either way, never an error.
        recycled_entry_id = card.get("_recycled_entry_id")
        recycled_entry_won = None
        if recycled_entry_id is not None:
            recycled_entry_won = await consume_recycled_entry(recycled_entry_id)

        if which == 1:
            self.card1_claimed = True
        else:
            self.card2_claimed = True

        button.disabled = True
        # A bonus-claim never resets/restarts the normal cooldown -- the
        # normal cooldown only resumes once every bonus has been spent.
        if not used_bonus_claim:
            claim_cooldowns[user_id] = now

        async with inventories_lock:
            if recycled_entry_won is not None:
                add_recycled_card(user_id, card, recycled_entry_won.get("print"))
            else:
                add_card(user_id, card)
            try:
                save_inventories_local()
                mark_inventories_dirty()
                if user_id == self.dropper_id:
                    # Successful claim by the dropper ends the exclusive
                    # window immediately, even if time remains on it.
                    self.priority_active = False
            except Exception:
                # Roll back the claim entirely so a failed save never
                # silently duplicates or loses a card, or blocks the
                # user's next claim attempt.
                get_inventory(user_id).pop(0)
                if which == 1:
                    self.card1_claimed = False
                else:
                    self.card2_claimed = False
                button.disabled = False
                if not used_bonus_claim:
                    claim_cooldowns.pop(user_id, None)
                else:
                    # Refund the bonus claim that was spent, since the
                    # claim it was spent on never actually went through.
                    async with duo_lock:
                        add_bonus(user_id, "claim", 1)
                        try:
                            save_duo_local()
                            mark_duo_dirty()
                        except Exception:
                            traceback.print_exc()
                if recycled_entry_won is not None:
                    # The recycled entry was already consumed from the
                    # pool above -- since the claim itself never actually
                    # went through, put it back byte-for-byte so it's
                    # still recyclable.
                    async with pending_recovery_lock:
                        pool = pending_recovery.setdefault(RECYCLABLE_CARDS_KEY, [])
                        pool.append(recycled_entry_won)
                        try:
                            save_pending_recovery_local()
                            mark_pending_recovery_dirty()
                        except Exception:
                            pool.pop()
                            traceback.print_exc()
                return await interaction.response.send_message(
                    "❌ Something went wrong saving your claim. Please try again.",
                    ephemeral=True
                )

        await interaction.response.edit_message(view=self)

        name = card.get("name", "Unknown")
        star_val = card.get("stars", 1)
        await interaction.channel.send(
            f"{interaction.user.mention} claimed **{name}**! {stars(star_val)} from the Stage."
        )

        # Duo progress hook: best-effort only. If `user_id` is part of an
        # active Duo challenge, this claim counts toward it. Never
        # affects whether the claim above succeeded -- it already has,
        # by this point -- and never changes claiming's own behavior.
        await _record_duo_claim_progress(interaction.client, user_id, card)

    @discord.ui.button(emoji="1️⃣", style=discord.ButtonStyle.primary)
    async def pick1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.claim(interaction, 1, button)

    @discord.ui.button(emoji="2️⃣", style=discord.ButtonStyle.primary)
    async def pick2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.claim(interaction, 2, button)

    async def on_timeout(self):
        # CLAIM_TIME_LIMIT reached: disable both claim buttons (still
        # visible, just inactive) so nobody can claim after the window
        # closes. The message content/embed is left untouched.
        for item in self.children:
            item.disabled = True
        if getattr(self, "message", None):
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# =========================
# 2. INVENTORY VIEW
# =========================

class InventoryView(discord.ui.View):
    def __init__(self, user, inventory, viewer_id=None, title_override=None):
        super().__init__(timeout=60)
        self.user = user
        # self.inventory is a list of (display_number, owned_card) tuples.
        # display_number is each card's TRUE position-based number in the
        # user's full, unfiltered inventory (see the lc command) -- so
        # filtering/searching never changes what number a card shows.
        self.inventory = inventory
        self.viewer_id = viewer_id
        # Optional literal title override (used only by `lc @Luka` to show
        # "🤖 Luka's Collection" instead of "{user.name}'s Collection").
        # None for every other caller, which reproduces the exact previous
        # title -- nothing else about this view changes for them.
        self.title_override = title_override
        self.page = 0

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name=self.title_override or f"{self.user.name}'s Collection",
            icon_url=self.user.display_avatar.url
        )

        total = len(self.inventory)
        start = self.page * CARDS_PER_PAGE
        end = start + CARDS_PER_PAGE
        cards_page = self.inventory[start:end]

        if not cards_page:
            embed.description = "No cards collected."
            total_pages = 1
            embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • Cards 0-0/{total}")
            return embed

        text = ""
        for display_number, owned_card in cards_page:
            card = owned_card["card"]
            name = card.get("name", "Unknown")
            series = card.get("series", "Unknown Series")
            star_val = card.get("stars", 1)
            print_num = owned_card["print"]
            pin_prefix = "📌 " if owned_card.get("pinned") else ""
            tags = owned_card.get("tags")
            tag_prefix = f"`{tags}` • " if tags else ""

            text += (
                f"{pin_prefix}`{display_number:02d}` ✦ "
                f"• `{format_print(print_num)}` "
                f"• `★ {star_val}` "
                f"• {tag_prefix}**{name}** • *{series}*\n"
            )

        embed.description = text
        total_pages = (total - 1) // CARDS_PER_PAGE + 1 if total > 0 else 1
        a = start + 1
        b = min(end, total)
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • Cards {a}-{b}/{total}")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.viewer_id is not None and interaction.user.id != self.viewer_id:
            return await interaction.response.send_message(
                "This isn't your inventory view!",
                ephemeral=True
            )

        if self.page > 0:
            self.page -= 1

        await interaction.response.edit_message(
            embed=self.get_embed(),
            view=self
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.viewer_id is not None and interaction.user.id != self.viewer_id:
            return await interaction.response.send_message(
                "This isn't your inventory view!",
                ephemeral=True
            )

        max_page = (len(self.inventory) - 1) // CARDS_PER_PAGE if len(self.inventory) > 0 else 0
        if self.page < max_page:
            self.page += 1

        await interaction.response.edit_message(
            embed=self.get_embed(),
            view=self
        )


# =========================
# 3. LOOKUP LIST VIEW
# =========================
class LookupListView(discord.ui.View):
    def __init__(self, results, user, user_id):
        super().__init__(timeout=60)
        self.results = results
        self.user = user
        self.user_id = user_id
        self.page = 0

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(name=f"{self.user.name}'s Search Results", icon_url=self.user.display_avatar.url)

        start = self.page * CARDS_PER_PAGE
        end = start + CARDS_PER_PAGE
        results_page = self.results[start:end]

        text = ""
        for i, card in enumerate(results_page, start=start + 1):
            name = card.get("name", "Unknown")
            star_val = card.get("stars", 1)
            series = card.get("series", "Unknown Series")
            text += f"`{i:02d}` ✦ `⭐ {star_val}` **{name}** • *{series}*\n"

        embed.description = text
        total_pages = (len(self.results) - 1) // CARDS_PER_PAGE + 1 if len(self.results) > 0 else 1
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • Type 'lup <number>' to view versions!")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        max_page = (len(self.results) - 1) // CARDS_PER_PAGE
        if self.page < max_page:
            self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# =========================
# 4. CHARACTER VERSION VIEW
# =========================

OWNERS_PER_PAGE = 10


class OwnersPaginationView(discord.ui.View):
    """
    Paginates the owners of a card, 10 per page.

    PERFORMANCE: member resolution is lazy and cached per-page, not done
    for all owners up front. The old version fetched every uncached
    owner (up to 100, since prints #1-#100 are the cap) before the
    first embed was even sent -- for a popular card with many
    uncached members, that's dozens of individual fetch_member() REST
    calls (rate-limited by Discord) all paid before anyone sees
    anything, which is exactly what made this "sometimes take minutes
    or never send." Since only ~10 owners are ever visible on the
    current page, there's no reason to resolve the other 90 until (if)
    the user actually pages there.

    `member_cache` (owner_id -> Member-or-None) is shared across the
    whole view: once an id is resolved -- from Discord's local cache
    (free) or a real fetch_member() call -- it's stored here and never
    looked up again, no matter how many times the user pages back and
    forth over it.
    """
    def __init__(self, user_id, card_name, card_id, owners, guild, member_cache):
        super().__init__(timeout=90)
        self.user_id = user_id
        self.card_name = card_name
        self.card_id = card_id
        self.owners = owners  # sorted list of (print_num, owner_id)
        self.guild = guild
        self.member_cache = member_cache  # owner_id -> Member | None, pre-seeded with free cache hits
        self.page = 0
        self.max_page = max(0, (len(owners) - 1) // OWNERS_PER_PAGE) if owners else 0
        self._update_button_states()

    def _update_button_states(self):
        self.previous.disabled = (self.page <= 0)
        self.next.disabled = (self.page >= self.max_page)

    def _page_slice(self, page):
        start = page * OWNERS_PER_PAGE
        return self.owners[start:start + OWNERS_PER_PAGE]

    async def _ensure_page_resolved(self, page):
        """
        Fetches only the owners on `page` that aren't already in
        member_cache -- i.e. only ones neither a previous fetch nor a
        free cache hit has ever resolved. A no-op (no network call at
        all) if the page has already been visited.

        If a fetch fails specifically because the user is no longer in
        the guild (discord.NotFound -- not a rate limit, network hiccup,
        or any other transient error), they're marked pending recovery
        (see mark_user_pending_recovery) -- this does NOT transfer any
        cards and does NOT change what's displayed here; the entry
        keeps showing "Unknown User" exactly as before. The actual
        transfer, of their ENTIRE inventory, only ever happens later,
        automatically, from pending_recovery_check_loop, after
        RECOVERY_PENDING_DAYS with no rejoin -- never as a side effect
        of viewing an Owners list.
        """
        page_owner_ids = list(dict.fromkeys(
            owner_id for _, owner_id in self._page_slice(page)
        ))
        missing_ids = [oid for oid in page_owner_ids if oid not in self.member_cache]

        if not missing_ids:
            return

        fetch_results = await asyncio.gather(
            *(self.guild.fetch_member(oid) for oid in missing_ids),
            return_exceptions=True
        )

        for owner_id, result in zip(missing_ids, fetch_results):
            if isinstance(result, discord.NotFound):
                # Confirmed: genuinely no longer a member of this guild
                # -- not merely a fetch that failed for some other
                # reason. Starts (or leaves running, if already
                # started) their recovery countdown; never transfers
                # anything here.
                self.member_cache[owner_id] = None
                await mark_user_pending_recovery(owner_id)
            elif isinstance(result, Exception):
                # Rate limit, network hiccup, missing permissions, etc.
                # -- do NOT assume they left; just couldn't resolve them
                # right now. Never treated as a departure.
                self.member_cache[owner_id] = None
            else:
                self.member_cache[owner_id] = result

    def build_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.title = f"**{self.card_name} Owners**"

        if not self.owners:
            embed.description = "Nobody owns this card yet."
        else:
            lines = []
            for print_num, owner_id in self._page_slice(self.page):
                member = self.member_cache.get(owner_id)
                mention_text = member.mention if member is not None else "Unknown User"
                lines.append(f"`{format_print(print_num)}.` • {mention_text}")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")

        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)

        if self.page > 0:
            self.page -= 1
        self._update_button_states()

        # Only defer (and pay a fetch) if this page has an owner that's
        # never been resolved before -- a page the user already visited,
        # or one made entirely of already-cached members, updates
        # instantly exactly like before.
        page_owner_ids = {owner_id for _, owner_id in self._page_slice(self.page)}
        if any(oid not in self.member_cache for oid in page_owner_ids):
            await interaction.response.defer()
            await self._ensure_page_resolved(self.page)
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)

        if self.page < self.max_page:
            self.page += 1
        self._update_button_states()

        page_owner_ids = {owner_id for _, owner_id in self._page_slice(self.page)}
        if any(oid not in self.member_cache for oid in page_owner_ids):
            await interaction.response.defer()
            await self._ensure_page_resolved(self.page)
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)


class CharacterVersionView(discord.ui.View):
    def __init__(self, versions, user, user_id):
        super().__init__(timeout=60)
        self.versions = versions
        self.user = user
        self.user_id = user_id
        self.index = 0

    def build_embed(self):
        card = self.versions[self.index]

        claims = card_prints.get(card["id"], 0)

        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name=f"{self.user.name}'s Search",
            icon_url=self.user.display_avatar.url
        )

        embed.description = (
            f"## **{card['name']}**\n"
            f"✦ **Series:** **{card['series']}**\n"
            f"────────────────────\n"
            f"✦ **Claims:** **{claims}**\n"
            f"✦ **Level:** **{stars(card['stars'])}**\n"
            f"✦ **Version:** **{card_version_display(card)}**\n"
        )

        embed.set_footer(
            text=f"Version {self.index+1}/{len(self.versions)}"
        )

        return embed

    async def update_message(self, interaction):
        card = self.versions[self.index]

        image_path = render_card_final(
            card,
            peek_next_print(card["id"]),
            hide_print=True
        )

        file = discord.File(image_path, filename="card.png")

        embed = self.build_embed()
        embed.set_image(url="attachment://card.png")

        # For interaction edits we must supply attachments when embed references attachment://
        await interaction.response.edit_message(
            embed=embed,
            attachments=[file],
            view=self
        )

        try:
            os.remove(image_path)
        except:
            pass

    @discord.ui.button(
        emoji="⬅️",
        style=discord.ButtonStyle.secondary
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        if self.index > 0:
            self.index -= 1

        await self.update_message(interaction)

    @discord.ui.button(
        emoji="🔍",
        style=discord.ButtonStyle.secondary
    )
    async def owners(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        # Defer immediately, before any member-resolution -- resolving
        # owners below can still involve real fetch_member() calls for
        # any uncached users, and with enough owners that can easily
        # blow past Discord's 3-second initial response window.
        # Deferring extends that to ~15 minutes.
        await interaction.response.defer()

        t_start = time.perf_counter()

        card = self.versions[self.index]

        owners = []

        for owner_id, inventory in inventories.items():
            for owned in inventory:
                if owned["card"]["id"] == card["id"]:
                    print_num = owned["print"]
                    # Only standard prints (#1-#100) are ever shown --
                    # Legacy (L) prints are anything above 100, per
                    # format_print(). This also caps the list at 100
                    # owners -> a maximum of 10 pages.
                    if print_num <= 100:
                        owners.append((print_num, owner_id))

        owners.sort()

        t_built_list = time.perf_counter()

        # Cache-only pass over EVERY owner: get_member() is a pure local
        # cache read (no API call, no privileged Server Members Intent
        # needed), so doing this for all of them costs nothing and lets
        # later pages skip a fetch entirely if they turn out to already
        # be cached.
        member_cache = {}
        for owner_id in dict.fromkeys(owner_id for _, owner_id in owners):
            member = interaction.guild.get_member(owner_id)
            if member is not None:
                member_cache[owner_id] = member
            # else: deliberately left unresolved here (not set to None)
            # -- that's the signal for _ensure_page_resolved to actually
            # try fetch_member for it, but only once it's on a page
            # that's actually being shown.

        t_cache_lookup = time.perf_counter()

        owners_view = OwnersPaginationView(self.user_id, card['name'], card['id'], owners, interaction.guild, member_cache)

        # PERFORMANCE: only page 0's owners (<=10 unique ids) are ever
        # fetched before this first response goes out -- not all up to
        # 100 owners across every page like before. Paging further
        # resolves lazily, once, per page (see OwnersPaginationView).
        await owners_view._ensure_page_resolved(0)
        t_fetch = time.perf_counter()
        embed = owners_view.build_embed()
        t_embed = time.perf_counter()

        # Instrumentation kept from the earlier investigation -- still
        # accurate, just now measuring a fetch of page 0's owners only
        # (<=10 unique ids) instead of every owner across every page.
        print(
            "Owners timing:\n"
            f"- Build owners list: {(t_built_list - t_start) * 1000:.1f} ms\n"
            f"- Cache lookup (free, all owners): {(t_cache_lookup - t_built_list) * 1000:.1f} ms\n"
            f"- Fetch page 0's missing members only: {(t_fetch - t_cache_lookup) * 1000:.1f} ms\n"
            f"- Build embed: {(t_embed - t_fetch) * 1000:.1f} ms\n"
            f"- Total: {(t_embed - t_start) * 1000:.1f} ms"
        )

        await interaction.followup.send(
            embed=embed,
            view=owners_view
        )

    @discord.ui.button(
        emoji="➡️",
        style=discord.ButtonStyle.secondary
    )
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        if self.index < len(self.versions)-1:
            self.index += 1

        await self.update_message(interaction)

# =========================
# 5. FINDCARD VERSION VIEW
# =========================

class FindcardVersionView(discord.ui.View):
    def __init__(self, versions, user, user_id):
        super().__init__(timeout=60)
        self.versions = versions
        self.user = user
        self.user_id = user_id
        self.index = 0

        # No navigation needed when there's only one version -- keep the
        # interface clean instead of showing buttons that do nothing.
        if len(self.versions) <= 1:
            self.clear_items()

    def build_embed(self):
        card = self.versions[self.index]

        claims = card_prints.get(card["id"], 0)

        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name="Card Lookup",
            icon_url=self.user.display_avatar.url
        )

        embed.title = card.get("name", "Unknown")
        embed.description = f"*{card.get('series', 'Unknown Series')}*"

        embed.add_field(name="Card ID", value=f"`{card['id']}`", inline=True)
        embed.add_field(name="Frame", value=card.get("frame", "common").title(), inline=True)
        embed.add_field(name="Stars", value=stars(card.get("stars", 1)), inline=True)
        embed.add_field(name="Claims", value=f"**{claims}**", inline=True)

        if len(self.versions) > 1:
            embed.set_footer(text=f"Version {self.index + 1}/{len(self.versions)}")

        return embed

    async def update_message(self, interaction: discord.Interaction):
        card = self.versions[self.index]

        image_path = render_card_final(
            card,
            peek_next_print(card["id"])
        )

        file = discord.File(
            image_path,
            filename="card.png"
        )

        embed = self.build_embed()
        embed.set_thumbnail(url="attachment://card.png")

        await interaction.response.edit_message(
            embed=embed,
            attachments=[file],
            view=self
        )

        try:
            os.remove(image_path)
        except:
            pass

    @discord.ui.button(
        emoji="◀",
        style=discord.ButtonStyle.secondary
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        if self.index > 0:
            self.index -= 1

        await self.update_message(interaction)

    @discord.ui.button(
        emoji="▶",
        style=discord.ButtonStyle.secondary
    )
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        if self.index < len(self.versions) - 1:
            self.index += 1

        await self.update_message(interaction)


# =========================
# 5b. FIND SERIES VIEW
# =========================

class FindSeriesView(discord.ui.View):
    def __init__(self, series_name, results, user, user_id):
        super().__init__(timeout=60)
        self.series_name = series_name
        self.results = results
        self.user = user
        self.user_id = user_id
        self.page = 0

        total_pages = (len(self.results) - 1) // CARDS_PER_PAGE + 1 if self.results else 1
        if total_pages <= 1:
            self.clear_items()

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name=f"Series: {self.series_name}",
            icon_url=self.user.display_avatar.url
        )

        start = self.page * CARDS_PER_PAGE
        end = start + CARDS_PER_PAGE
        page_cards = self.results[start:end]

        lines = []
        for card in page_cards:
            claims = card_prints.get(card["id"], 0)
            lines.append(
                f"**{card.get('name', 'Unknown')}** • `{card['id']}`\n"
                f"✦ Frame: **{card.get('frame', 'common').title()}** • "
                f"Stars: {stars(card.get('stars', 1))} • Claims: **{claims}**"
            )

        embed.description = "\n\n".join(lines) if lines else "No cards found."

        total_pages = (len(self.results) - 1) // CARDS_PER_PAGE + 1 if self.results else 1
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • {len(self.results)} card(s) found")

        return embed

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        max_page = (len(self.results) - 1) // CARDS_PER_PAGE
        if self.page < max_page:
            self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


# =========================
# 6. GIFT VIEW
# =========================

class GiftView(discord.ui.View):
    def __init__(self, from_user, to_user, owned_card, from_id, to_id, card_index):
        super().__init__(timeout=90)
        self.from_user = from_user
        self.to_user = to_user
        self.owned_card = owned_card
        self.card = owned_card["card"]
        self.print_num = owned_card["print"]
        # Merchant-reward cards keep their real print number (text +
        # rendered image) instead of collapsing to "L" past 100 -- see
        # render_card's force_real_print. False for every normal gift.
        self.is_merchant_reward = bool(owned_card.get("merchant_reward"))
        self.from_id = from_id
        self.to_id = to_id
        self.card_index = card_index
        self.gift_id = f"{from_id}_{to_id}_{int(time.time())}"
        active_gifts[self.gift_id] = {"time": time.time()}
        self.message = None

    def build_embed(self, owner_user, status_text=None):
        card = self.card
        star_val = card.get("stars", 1)
        print_display = (
            format_merchant_print(self.print_num) if self.is_merchant_reward
            else format_print(self.print_num)
        )

        embed = discord.Embed(color=THEME_COLOR)

        if status_text:
            embed.set_author(
                name=status_text,
                icon_url=owner_user.display_avatar.url
            )
        else:
            embed.set_author(
                name=f"{self.from_user.name} is gifting {self.to_user.name} a card!",
                icon_url=self.from_user.display_avatar.url
            )

        embed.description = (
            f"## **{card.get('name', 'Unknown Character')}**\n"
            f"✦ **Series:** **{card.get('series', 'Unknown Series')}**\n"
            f"───\n"
            f"✦ **Owner:** {owner_user.mention}\n"
            f"✦ **Print:** **{print_display}**\n"
            f"✦ **Level:** **{stars(star_val)}**\n"
        )

        image_path = render_card_final(card, self.print_num, force_real_print=self.is_merchant_reward)
        if image_path:
            file = discord.File(image_path, filename="card.png")
            embed.set_image(url="attachment://card.png")
            # leave file removal to caller (we'll remove after send)
            return embed, file
        return embed, None

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.to_id:
            return await interaction.response.send_message(
                "Not your gift.",
                ephemeral=True
            )

        async with inventories_lock:
            giver_inv = get_inventory(self.from_id)

            if self.card_index >= len(giver_inv):
                return await interaction.response.send_message(
                    "This card is no longer available to trade.",
                    ephemeral=True
                )

            current_owned_card = giver_inv[self.card_index]

            if (
                current_owned_card["card"]["id"] != self.card["id"]
                or current_owned_card["print"] != self.print_num
            ):
                return await interaction.response.send_message(
                    "This card is no longer available to trade.",
                    ephemeral=True
                )

            # Remove from giver by index and insert to receiver newest-first
            moved_card = remove_card(self.from_id, self.card_index)
            get_inventory(self.to_id).insert(0, moved_card)

            try:
                save_inventories_local()
                mark_inventories_dirty()
            except Exception:
                # Roll back the transfer so a failed save never silently
                # duplicates or loses the card.
                get_inventory(self.to_id).pop(0)
                giver_inv.insert(self.card_index, moved_card)
                return await interaction.response.send_message(
                    "❌ Something went wrong saving this gift. Please try again.",
                    ephemeral=True
                )

        accepted_embed, file = self.build_embed(
            self.to_user,
            status_text=f"{self.to_user.name} accepted {self.from_user.name}'s gift!"
        )

        if self.gift_id in active_gifts:
            del active_gifts[self.gift_id]

        # When editing with embed referencing attachment:// we must attach the file
        await interaction.response.edit_message(
            content=None,
            embed=accepted_embed,
            view=None,
            attachments=[] if not file else [file]
        )

        if file:
            try:
                os.remove(file.fp.name)
            except Exception:
                pass

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.to_id:
            return await interaction.response.send_message(
                "Not your gift.",
                ephemeral=True
            )

        if self.gift_id in active_gifts:
            del active_gifts[self.gift_id]

        declined_embed, file = self.build_embed(
            self.from_user,
            status_text=f"{self.to_user.name} declined {self.from_user.name}'s gift!"
        )

        await interaction.response.edit_message(
            content=None,
            embed=declined_embed,
            view=None,
            attachments=[] if not file else [file]
        )

        if file:
            try:
                os.remove(file.fp.name)
            except Exception:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.from_id:
            return await interaction.response.send_message(
                "Only the sender can cancel this gift.",
                ephemeral=True
            )

        if self.gift_id not in active_gifts:
            # Already accepted, declined, expired, or cancelled -- do nothing.
            return await interaction.response.send_message(
                "This gift is no longer active.",
                ephemeral=True
            )

        del active_gifts[self.gift_id]

        for item in self.children:
            item.disabled = True

        cancelled_embed, file = self.build_embed(
            self.from_user,
            status_text="Gift cancelled."
        )

        await interaction.response.edit_message(
            content=None,
            embed=cancelled_embed,
            view=self,
            attachments=[] if not file else [file]
        )

        if file:
            try:
                os.remove(file.fp.name)
            except Exception:
                pass

    async def on_timeout(self):
        if self.gift_id not in active_gifts:
            # Already accepted, declined, or cancelled -- nothing to do.
            return

        del active_gifts[self.gift_id]

        for item in self.children:
            item.disabled = True

        expired_embed, file = self.build_embed(
            self.from_user,
            status_text="Gift has expired."
        )

        if self.message:
            try:
                await self.message.edit(
                    content=None,
                    embed=expired_embed,
                    view=self,
                    attachments=[] if not file else [file]
                )
            except Exception:
                pass

            if file:
                try:
                    os.remove(file.fp.name)
                except Exception:
                    pass


# =========================
# SHOWCASE VIEW
# =========================
# =========================
# HELP PAGINATION
# =========================

# Exact same category names/command text as before -- only how they're
# split across pages changed. Splitting a section across pages only
# happens when it doesn't fit in HELP_COMMANDS_PER_PAGE (currently only
# "Other", since Cards+Trading together already fit in one page).
HELP_SECTIONS = [
    ("𝗖𝗮𝗿𝗱𝘀", [
        "`ld` ─ Drop 2 random cards.",
        "`lv <number>` ─ View a card.",
        "`lc` ─ Look through your collection.",
        "`lup <name/series>` ─ Search a character or series.",
    ]),
    ("𝗧𝗿𝗮𝗱𝗶𝗻𝗴", [
        "`lg` / `lgift` ─ Gift one of your cards to another player.",
        "`lt` / `ltrade` ─ Trade cards with another player.",
    ]),
    ("𝗢𝘁𝗵𝗲𝗿", [
        "`lcd` ─ Check your current cooldowns.",
        "`ltag <name>` OR `<number> <tag>` ─ Tag 1 or multiple cards.",
        "`lpin <number>` ─ Pin up to 3 cards.",
        "`lc -untagged` ─ Check untagged cards.",
        "`lc p:<number>` ─ Check cards by prints.",
        "`lc -p` ─ Check cards by prints lowest to highest.",
        "`lc t:<tag>` ─ Check cards with that specific tag.",
        "`lbadges [user]` ─ View collection badges.",
        "`lshowcase [user]` ─ View a showcase of top cards.",
        "`lscadd <number>` ─ Add a card to your showcase.",
        "`lscremove <number>` ─ Remove a card from your showcase.",
        "`lprogress [user]` ─ View collection progress.",
        "`lmissing [series] [user]` ─ View missing cards, or compare with another user.",
        "`lmail` ─ View your mailbox.",
        "`lmail @user` ─ Send someone mail.",
        "`lduo @user` ─ Invite someone to a Duo Challenge.",
    ]),
]


def _build_help_pages() -> list:
    """
    Flattens HELP_SECTIONS into pages of at most HELP_COMMANDS_PER_PAGE
    command lines each, keeping a category's commands together on one
    page whenever they fit. A category is only split across pages if it
    alone exceeds the per-page limit (currently only "Other"), in which
    case the continuation page's header is marked "(continued)".
    """
    pages = []
    current_lines = []
    current_count = 0

    def flush_page():
        nonlocal current_lines, current_count
        if current_lines:
            pages.append("\n".join(current_lines))
        current_lines = []
        current_count = 0

    for section_name, commands in HELP_SECTIONS:
        remaining = commands[:]
        first_chunk_in_section = True
        while remaining:
            space_left = HELP_COMMANDS_PER_PAGE - current_count
            if space_left <= 0:
                flush_page()
                space_left = HELP_COMMANDS_PER_PAGE

            chunk = remaining[:space_left]
            remaining = remaining[space_left:]

            header = f"### {section_name}" if first_chunk_in_section else f"### {section_name} (continued)"
            current_lines.append(header)
            current_lines.extend(chunk)
            current_lines.append("")
            current_count += len(chunk)
            first_chunk_in_section = False

    flush_page()
    return pages


class HelpPaginationView(discord.ui.View):
    """
    Paginates the static lhelp command list, HELP_COMMANDS_PER_PAGE (6)
    commands per page. Pages are pre-built once by _build_help_pages()
    -- this is static reference text, not per-user data, so there's
    nothing to recompute when paging back and forth.
    """
    def __init__(self, pages, user_id):
        super().__init__(timeout=90)
        self.pages = pages
        self.user_id = user_id
        self.page = 0
        self.max_page = max(0, len(pages) - 1)
        self._update_button_states()

    def _update_button_states(self):
        self.previous.disabled = (self.page <= 0)
        self.next.disabled = (self.page >= self.max_page)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            color=THEME_COLOR,
            title="📖 Luka Commands Helper",
            description=self.pages[self.page],
        )
        if self.max_page > 0:
            embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)

        if self.page > 0:
            self.page -= 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)

        if self.page < self.max_page:
            self.page += 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class ShowcaseView(discord.ui.View):
    """
    View attached to the `lshowcase` embed: "View Badges", the ❤︎ vote
    button, and "⚙ Edit". All three are always attached to every
    showcase message, for every viewer -- Discord has no way to show
    different components to different viewers on the same public
    message, so "Edit" is restricted by USAGE, not visibility: anyone
    can see the button, but the click handler only lets the actual
    owner proceed past it (everyone else gets a short ephemeral
    rejection and the editing flow never opens for them).
    """
    def __init__(self, bot, owner_user, owner_member, is_owner_view: bool):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_user = owner_user
        self.owner_member = owner_member
        # Vote button starts labeled with whatever vote count is
        # currently stored in showcase_votes.json for the owner --
        # nothing here is guessed or defaulted to zero unnecessarily.
        self.vote.label = f"❤︎ {get_vote_count(owner_user.id)}"

    @discord.ui.button(label="View Badges", style=discord.ButtonStyle.secondary, emoji="🏅")
    async def view_badges(self, interaction: discord.Interaction, button: discord.ui.Button):
        blocks = _ordered_badge_blocks(self.owner_user, self.owner_member)
        star_rating = _badge_star_rating_for(self.owner_user, self.owner_member)
        view = BadgesPaginationView(self.owner_user, blocks, interaction.user.id, star_rating)
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="❤︎ 0", style=discord.ButtonStyle.danger)
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.owner_user.id:
            return await interaction.response.send_message(
                "You can't vote on your own showcase!", ephemeral=True
            )

        owner_id = str(self.owner_user.id)
        voter_id = str(interaction.user.id)

        async with showcase_votes_lock:
            voters = showcase_votes.setdefault(owner_id, [])
            already_voted = voter_id in voters

            if already_voted:
                voters.remove(voter_id)
            else:
                voters.append(voter_id)

            try:
                save_showcase_votes_local()
                mark_showcase_votes_dirty()
            except Exception:
                # Roll back the in-memory change so it never drifts
                # from what's actually on disk.
                if already_voted:
                    voters.append(voter_id)
                else:
                    voters.remove(voter_id)
                return await interaction.response.send_message(
                    "❌ Something went wrong saving your vote. Please try again.",
                    ephemeral=True
                )

            new_count = len(voters)

        button.label = f"❤︎ {new_count}"
        await interaction.response.edit_message(view=self)

        confirmation = "Vote removed!" if already_voted else "Successfully voted!"
        await interaction.followup.send(confirmation, ephemeral=True)

    @discord.ui.button(label="⚙ Edit", style=discord.ButtonStyle.secondary)
    async def edit_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Always attached, for every viewer -- restricted by usage
        # here, not by visibility (see class docstring).
        if interaction.user.id != self.owner_user.id:
            return await interaction.response.send_message(
                "Only the owner of this showcase can edit it.", ephemeral=True
            )

        await interaction.response.send_message(
            "Send your new showcase description (max 5 lines). It will replace your current one.",
            ephemeral=True
        )

        def check(m):
            return m.author.id == self.owner_user.id and m.channel.id == interaction.channel.id

        try:
            reply_msg = await self.bot.wait_for("message", check=check, timeout=180)
        except asyncio.TimeoutError:
            return await interaction.followup.send("❌ Timed out waiting for a description.", ephemeral=True)

        lines = reply_msg.content.strip("\n").split("\n")
        if not reply_msg.content.strip() or len(lines) > 5:
            return await interaction.followup.send(
                "❌ Description must be 1-5 lines. Please click **⚙ Edit** again to retry.",
                ephemeral=True
            )

        new_description = reply_msg.content.strip("\n")

        async with showcase_descriptions_lock:
            showcase_descriptions[str(self.owner_user.id)] = new_description
            try:
                save_showcase_descriptions_local()
            except Exception:
                return await interaction.followup.send(
                    "❌ Something went wrong saving your description. Please try again.",
                    ephemeral=True
                )

        await interaction.followup.send("✅ Showcase description updated.", ephemeral=True)


# =========================
# 7. TRADE REQUEST VIEW
# =========================

class TradeRequestView(discord.ui.View):
    def __init__(self, user1, user2, user1_id, user2_id):
        super().__init__(timeout=90)
        self.user1 = user1
        self.user2 = user2
        self.user1_id = user1_id
        self.user2_id = user2_id
        self.request_id = f"{user1_id}_{user2_id}_{int(time.time())}"
        self.message = None
        self.responded = False

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.description = f"{self.user2.mention}, you've received a trade request from {self.user1.mention}!"
        return embed

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success, label="Trade")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user2_id:
            return await interaction.response.send_message(
                "This isn't your trade request!",
                ephemeral=True
            )

        view = TradeView(
            self.user1,
            self.user2,
            self.user1_id,
            self.user2_id
        )

        self.responded = True
        self.stop()

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view
        )

        # Store message reference for immediate edits by add command
        try:
            active_trades[view.trade_id]["message"] = interaction.message
            active_trades[view.trade_id]["view"] = view
            view.message = interaction.message
        except Exception:
            pass

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger, label="Cancel")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user2_id:
            return await interaction.response.send_message(
                "This isn't your trade request!",
                ephemeral=True
            )

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = "Trade request has been denied."

        self.responded = True
        self.stop()

        await interaction.response.edit_message(
            embed=embed,
            view=None
        )

    async def on_timeout(self):
        if self.responded:
            # Already accepted or declined -- nothing to do.
            return

        for item in self.children:
            item.disabled = True

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = "Trade has expired."

        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except Exception:
                pass


# =========================
# 8. TRADE VIEW
# =========================

MAX_TRADE_CARDS = 3

# The "confirming" stage (both players locked, waiting on final Confirm
# presses) used to disable its timeout entirely, on the assumption a
# finalizing trade should never expire mid-transaction. In practice
# that meant an abandoned trade -- one side locks and then never
# presses Confirm -- had literally no expiration at all, leaving both
# participants permanently unable to start a new trade. It now gets
# its own bounded timeout instead of none.
TRADE_CONFIRM_TIMEOUT_SECONDS = 300  # 5 minutes to finish confirming once both are locked

# Backstop for active_trades: independent of any single view's own
# on_timeout callback (library timer races, a swallowed exception during
# the message edit, etc.), trade_expiration_sweep_loop periodically
# force-clears any active_trades entry older than this, guaranteeing
# expiration is reliable even if nobody ever interacts with the old
# trade again. Generous enough to never fire before a trade's own
# on_timeout would have already cleaned it up normally.
TRADE_MAX_LIFETIME_SECONDS = TRADE_CONFIRM_TIMEOUT_SECONDS + 180 + 120  # confirm window + select/lock window + slack
TRADE_SWEEP_INTERVAL_SECONDS = 60


class TradeView(discord.ui.View):
    def __init__(self, user1, user2, user1_id, user2_id):
        # 3-minute timeout for the selecting/locking phase only -- once
        # both sides lock in and move to "confirming" (see lock() below),
        # the timeout is disabled entirely so the final confirmation step
        # never expires mid-transaction.
        super().__init__(timeout=180)
        self.user1 = user1
        self.user2 = user2
        self.user1_id = user1_id
        self.user2_id = user2_id
        self.user1_cards = []
        self.user1_card_indices = []
        self.user2_cards = []
        self.user2_card_indices = []
        self.user1_locked = False
        self.user2_locked = False
        self.user1_confirmed = False
        self.user2_confirmed = False
        self.stage = "selecting"
        self.trade_id = f"{user1_id}_{user2_id}_{int(time.time())}"
        self.message = None
        active_trades[self.trade_id] = {
            "time": time.time(),
            "view": self,
            "message": None
        }

    def build_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.title = "**Trade In Progress**"

        trade_emoji = "<:Bluka:1511044685781663866>"

        if self.stage == "selecting":
            user1_status = "Waiting for selection"
            user2_status = "Waiting for selection"
        elif self.stage == "locking":
            user1_status = "Pending" if not self.user1_locked else "Confirming"
            user2_status = "Pending" if not self.user2_locked else "Confirming"
        elif self.stage == "confirming":
            user1_status = "Completed!" if self.user1_confirmed else "Completing"
            user2_status = "Completed!" if self.user2_confirmed else "Completing"

        def format_offer(user, owned_cards, card_indices, status):
            block = f"> ## {trade_emoji} {user.mention} is offering... - {status}\n"
            if owned_cards:
                owner_inv_len = len(get_inventory(user.id))
                for owned_card, card_index in zip(owned_cards, card_indices):
                    card = owned_card["card"]
                    name = card.get("name", "Unknown")
                    series = card.get("series", "Unknown Series")
                    print_num = owned_card["print"]
                    star_val = card.get("stars", 1)
                    if card_index is not None:
                        # Same descending scheme as the inventory display:
                        # highest number = newest/top of the owner's inventory.
                        inv_num = owner_inv_len - card_index
                    else:
                        inv_num = "?"
                    block += f"### `{inv_num} • {format_print(print_num)} • ☆{star_val} • {name} • {series}`\n"
            else:
                block += "### `No cards selected yet.`\n"
            return block

        user1_text = format_offer(self.user1, self.user1_cards, self.user1_card_indices, user1_status)
        user2_text = format_offer(self.user2, self.user2_cards, self.user2_card_indices, user2_status)

        embed.description = user1_text + "────────────────────────\n" + user2_text

        embed.description += "\n-# 💡 **Reminder:** There are no official values for cards in LukaNet right now. Trade based on what you and the other user think is fair."

        return embed

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.trade_id in active_trades:
            del active_trades[self.trade_id]

        await interaction.response.edit_message(
            content="Trade has been declined.",
            embed=None,
            view=None
        )

    async def on_timeout(self):
        # Now fires for both the selecting/locking timeout AND the
        # confirming-stage timeout (see TRADE_CONFIRM_TIMEOUT_SECONDS).
        # Clearing active_trades happens FIRST and unconditionally --
        # regardless of stage, and regardless of whether the message
        # edit below succeeds -- so an abandoned trade can never
        # permanently block either participant from starting a new one.
        if self.trade_id in active_trades:
            del active_trades[self.trade_id]

        for item in self.children:
            item.disabled = True

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = "Trade has expired."

        if self.message:
            try:
                await self.message.edit(content=None, embed=embed, view=self)
            except Exception:
                pass

    @discord.ui.button(emoji="🔒", style=discord.ButtonStyle.secondary)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.stage == "selecting":
            return await interaction.response.send_message(
                "Both players need to select cards first!",
                ephemeral=True
            )

        if interaction.user.id == self.user1_id:
            self.user1_locked = True
        elif interaction.user.id == self.user2_id:
            self.user2_locked = True
        else:
            return await interaction.response.send_message(
                "This isn't your trade!",
                ephemeral=True
            )

        if self.user1_locked and self.user2_locked:
            self.stage = "confirming"
            # Bounded timeout for the confirming stage too now (see
            # TRADE_CONFIRM_TIMEOUT_SECONDS) -- an abandoned trade (one
            # side locks and never confirms) used to never expire at all.
            self.timeout = TRADE_CONFIRM_TIMEOUT_SECONDS
            try:
                self.lock.emoji = "✅"
            except Exception:
                pass

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self
        )

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.stage != "confirming":
            return await interaction.response.send_message(
                "Both players need to lock first!",
                ephemeral=True
            )

        if interaction.user.id == self.user1_id:
            self.user1_confirmed = True
        elif interaction.user.id == self.user2_id:
            self.user2_confirmed = True
        else:
            return await interaction.response.send_message(
                "This isn't your trade!",
                ephemeral=True
            )

        if self.user1_confirmed and self.user2_confirmed:
            self.decline.disabled = True
            self.lock.disabled = True
            self.confirm.disabled = True

            # finalize trade: remove the correct entries by matching card id + print
            try:
                if self.user1_cards and self.user2_cards:
                    async with inventories_lock:
                        inv1 = get_inventory(self.user1_id)
                        inv2 = get_inventory(self.user2_id)

                        # find by id + print to be robust against index shifts
                        idx1_list = []
                        ok = True
                        for sel in self.user1_cards:
                            idx = next((i for i, c in enumerate(inv1)
                                        if c["card"]["id"] == sel["card"]["id"] and c["print"] == sel["print"]), None)
                            if idx is None:
                                ok = False
                                break
                            idx1_list.append(idx)

                        idx2_list = []
                        if ok:
                            for sel in self.user2_cards:
                                idx = next((i for i, c in enumerate(inv2)
                                            if c["card"]["id"] == sel["card"]["id"] and c["print"] == sel["print"]), None)
                                if idx is None:
                                    ok = False
                                    break
                                idx2_list.append(idx)

                        if ok:
                            # Snapshot both inventories so a failed save can
                            # restore them exactly, regardless of how many
                            # cards moved.
                            inv1_backup = list(inv1)
                            inv2_backup = list(inv2)

                            c1_list = [inv1.pop(i) for i in sorted(idx1_list, reverse=True)]
                            c2_list = [inv2.pop(i) for i in sorted(idx2_list, reverse=True)]

                            for c in c2_list:
                                inv1.insert(0, c)  # receiver gets new cards newest-first
                            for c in c1_list:
                                inv2.insert(0, c)

                            try:
                                save_inventories_local()
                                mark_inventories_dirty()
                            except Exception:
                                # Roll back the swap so a failed save
                                # never silently duplicates or loses
                                # either side's cards.
                                inv1[:] = inv1_backup
                                inv2[:] = inv2_backup
                                raise
            except Exception as e:
                print("TRADE FINALIZE ERROR:", e)
                traceback.print_exc()

            embed = discord.Embed(color=THEME_COLOR)
            embed.title = "Trade Completed!"

            user1_names = ", ".join(c["card"].get("name", "Unknown") for c in self.user1_cards) if self.user1_cards else "Nothing"
            user2_names = ", ".join(c["card"].get("name", "Unknown") for c in self.user2_cards) if self.user2_cards else "Nothing"

            if self.trade_id in active_trades:
                del active_trades[self.trade_id]

            embed.description = (
                f"{self.user1.mention} received **{user2_names}**\n!"
                f"{self.user2.mention} received **{user1_names}**!"
            )

            await interaction.response.edit_message(
                embed=embed,
                view=self
            )
        else:
            await interaction.response.edit_message(
                embed=self.build_embed(),
                view=self
            )


# =========================
# MERCHANT LIST VIEW (lmerchant)
# =========================

# Presentation-only: emoji shown next to each merchant's name in the
# lmerchants embed. Kept separate from MERCHANT_TEMPLATES (rather than
# added as a field there) so this purely visual addition never touches
# that template/backend data structure.
MERCHANT_DISPLAY_EMOJI = {
    "voyager_merchant": "🧭",
    "lucky_merchant": "🍀",
    "collector_merchant": "📦",
}


class MerchantListView(discord.ui.View):
    """
    Paginated, read-only browser over the currently active merchants --
    one merchant per page, navigated with the same prev/next button
    pattern InventoryView uses. Every page render re-reads
    get_active_merchants() live, so if stock/expiry changed (e.g.
    someone else just finished a trade with this merchant) that's
    reflected the next time a page is drawn, per "merchants are global
    state." The Accept Trade button hands off to a fresh
    MerchantTradeView for the merchant currently on screen.
    """

    def __init__(self, viewer_id):
        super().__init__(timeout=120)
        self.viewer_id = viewer_id
        self.page = 0
        self.message = None

    def build_embed_and_file(self):
        active = get_active_merchants()

        if not active:
            embed = discord.Embed(
                color=THEME_COLOR,
                description="The merchants haven't arrived yet. check the channel for updates!"
            )
            self.accept.disabled = True
            self.previous.disabled = True
            self.next.disabled = True
            return embed, None

        self.page = max(0, min(self.page, len(active) - 1))
        m = active[self.page]
        info = get_merchant_display_info(m)

        if info is None:
            embed = discord.Embed(color=THEME_COLOR, description="This merchant is unavailable.")
            self.accept.disabled = True
            self.previous.disabled = True
            self.next.disabled = True
            return embed, None

        separator = "━" * 20
        emoji = MERCHANT_DISPLAY_EMOJI.get(info.get("template_id"), "🛒")

        lines = [
            f"## {emoji} {info.get('name', 'Merchant')}",
            "",
            f'`"{info.get("description", "")}"`',
            separator,
            "**- Looking For**",
        ]

        wants = info.get("wants", [])
        if wants:
            for c in wants:
                star_str = "★" * int(c.get("stars", 1))
                lines.append(f"{star_str} {c.get('name', 'Unknown')} • *{c.get('series', 'Unknown Series')}*")
        else:
            lines.append("Nothing right now.")

        lines.append(separator)
        lines.append("**- Rewards**")

        rewards = info.get("rewards", [])
        if rewards:
            for i, c in enumerate(rewards):
                star_str = "★" * int(c.get("stars", 1))
                lines.append(f"{star_str} {c.get('name', 'Unknown')}")
                # NOTE: the reward pool shown here doesn't have a print
                # number yet -- one is only ever generated per-player at
                # actual trade completion (see the trade-finalize logic),
                # never reserved/fixed at listing time. Showing the
                # series here instead of a fabricated print number, so
                # this stays accurate to what's actually been generated
                # so far.
                lines.append(f"-# {c.get('series', 'Unknown Series')}")
                if i != len(rewards) - 1:
                    lines.append("")
        else:
            lines.append("Nothing right now.")

        lines.append(separator)

        stock = info.get("stock", 0)
        lines.append(f"**Stock: {stock} / {MERCHANT_STARTING_STOCK}**")
        lines.append("")
        # Discord's own relative-timestamp markdown -- renders and keeps
        # counting down client-side on its own, so this is always
        # accurate at the moment someone looks at it rather than frozen
        # at whatever it said when the embed was built.
        expires_ts = int(info.get("expires_ts", 0))
        lines.append(f"Leaves In: <t:{expires_ts}:R>")

        embed = discord.Embed(
            description="\n".join(lines),
            color=info.get("color", THEME_COLOR)
        )

        file, attach_url = get_merchant_avatar_file(info)
        if attach_url:
            embed.set_thumbnail(url=attach_url)

        self.accept.disabled = stock <= 0
        self.previous.disabled = len(active) <= 1
        self.next.disabled = len(active) <= 1

        return embed, file

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.viewer_id:
            return await interaction.response.send_message("This isn't your merchant menu!", ephemeral=True)

        active = get_active_merchants()
        if active:
            self.page = (self.page - 1) % len(active)

        embed, file = self.build_embed_and_file()
        await interaction.response.edit_message(embed=embed, view=self, attachments=[file] if file else [])

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.viewer_id:
            return await interaction.response.send_message("This isn't your merchant menu!", ephemeral=True)

        active = get_active_merchants()
        if active:
            self.page = (self.page + 1) % len(active)

        embed, file = self.build_embed_and_file()
        await interaction.response.edit_message(embed=embed, view=self, attachments=[file] if file else [])

    @discord.ui.button(label="Accept Trade", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.viewer_id:
            return await interaction.response.send_message("This isn't your merchant menu!", ephemeral=True)

        active = get_active_merchants()
        if not active:
            return await interaction.response.send_message("No merchants are around right now.", ephemeral=True)

        self.page = max(0, min(self.page, len(active) - 1))
        m = active[self.page]

        if user_has_active_merchant_trade(interaction.user.id):
            return await interaction.response.send_message(
                "You already have an open trade with a merchant. Finish or cancel that one first.",
                ephemeral=True
            )

        if m.get("stock", 0) <= 0:
            return await interaction.response.send_message(
                "That merchant is out of stock.", ephemeral=True
            )

        trade_view = MerchantTradeView(interaction.user, interaction.user.id, m["id"])
        embed, file = trade_view.build_embed_and_file()

        if embed is None:
            return await interaction.response.send_message(
                "That merchant is no longer available.", ephemeral=True
            )

        await interaction.response.edit_message(embed=embed, view=trade_view, attachments=[file] if file else [])

        active_merchant_trades[interaction.user.id] = {
            "time": time.time(),
            "view": trade_view,
            "message": interaction.message,
        }
        trade_view.message = interaction.message

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# =========================
# MERCHANT TRADE VIEW (Accept Trade -> trade interface)
# =========================

class MerchantTradeView(discord.ui.View):
    """
    A single player's own trade session against ONE fixed merchant,
    opened by MerchantListView's Accept Trade button. Reuses the same
    manual, command-driven card-selection pattern as the player-to-
    player TradeView/"add <number>" flow (see `madd` below): the player
    always picks their own cards by hand, one at a time, from their
    existing `lc` inventory numbering -- the merchant NEVER reaches into
    a player's inventory itself. Only Confirm Trade actually mutates
    inventories.json/merchants.json, and only after full validation,
    atomically.
    """

    def __init__(self, user, user_id, merchant_id):
        super().__init__(timeout=180)
        self.user = user
        self.user_id = user_id
        self.merchant_id = merchant_id
        # want_index -> owned_card dict, as currently offered. Re-verified
        # by id+print against the live inventory at confirm time, exactly
        # like TradeView's finalize step -- so this is just a display/
        # intent record, never treated as a guarantee of ownership.
        self.selections = {}
        self.message = None

    def _merchant(self):
        return get_merchant_by_id(self.merchant_id)

    def _info(self):
        m = self._merchant()
        if m is None:
            return None
        return get_merchant_display_info(m)

    def build_embed_and_file(self):
        info = self._info()
        if info is None:
            for item in self.children:
                item.disabled = True
            return None, None

        if not _merchant_is_active(self._merchant(), time.time()):
            for item in self.children:
                item.disabled = True
            embed = discord.Embed(color=THEME_COLOR, description="This merchant is no longer available.")
            return embed, None

        embed = discord.Embed(title=f"Trading with {info.get('name', 'the Merchant')}", color=info.get("color", THEME_COLOR))

        wants = info.get("wants", [])
        lines = []
        for i, want in enumerate(wants):
            offered = self.selections.get(i)
            if offered:
                oc = offered["card"]
                status = f"✅ offering `{format_print(offered['print'])}` **{oc.get('name', 'Unknown')}**"
            else:
                status = "❌ not yet offered"
            lines.append(
                f"`★{want.get('stars', 1)}` **{want.get('name', 'Unknown')}** • *{want.get('series', 'Unknown Series')}* — {status}"
            )
        embed.add_field(name="Looking For", value="\n".join(lines) if lines else "Nothing right now.", inline=False)

        rewards_text = "\n".join(
            f"• `★{c.get('stars', 1)}` **{c.get('name', 'Unknown')}** • *{c.get('series', 'Unknown Series')}*"
            for c in info.get("rewards", [])
        ) or "Nothing right now."
        embed.add_field(name="Possible Rewards (1-2 granted)", value=rewards_text, inline=False)

        embed.set_footer(
            text=f"Stock remaining: {info.get('stock', 0)} • Use `madd <card number>` from your `lc` list to offer a card."
        )

        file, attach_url = get_merchant_avatar_file(info)
        if attach_url:
            embed.set_thumbnail(url=attach_url)

        all_filled = len(wants) > 0 and all(i in self.selections for i in range(len(wants)))
        self.confirm.disabled = not all_filled

        return embed, file

    async def refresh_message(self):
        if not self.message:
            return
        embed, file = self.build_embed_and_file()
        if embed is None:
            return
        try:
            await self.message.edit(embed=embed, view=self, attachments=[file] if file else [])
        except Exception:
            pass

    def toggle_card(self, owned_card) -> str:
        """
        Called from the `madd` command below. Adds `owned_card` to
        whichever "Looking For" slot it satisfies, or removes it if it's
        already offered there. Never touches the player's actual
        inventory -- purely an in-memory change to this session's
        intended offer, which only becomes real on Confirm Trade.
        """
        info = self._info()
        if info is None:
            return "That merchant is no longer available."

        wants = info.get("wants", [])
        card_id = owned_card["card"].get("id")
        print_num = owned_card["print"]

        # Toggle off if this exact card (id + print) is already offered
        # for some want slot.
        for i, sel in list(self.selections.items()):
            if sel["card"].get("id") == card_id and sel["print"] == print_num:
                del self.selections[i]
                return f"Removed **{owned_card['card'].get('name', 'Unknown')}** from your offer."

        # Otherwise, fill the first unfulfilled want slot this card matches.
        for i, want in enumerate(wants):
            if i in self.selections:
                continue
            if want.get("id") == card_id:
                self.selections[i] = owned_card
                return f"Offered **{owned_card['card'].get('name', 'Unknown')}** for the merchant's request."

        return "The merchant isn't looking for that card (or you've already offered it)."

    @discord.ui.button(label="Confirm Trade", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your trade!", ephemeral=True)

        result = await self._execute_trade()

        for item in self.children:
            item.disabled = True
        active_merchant_trades.pop(self.user_id, None)

        if result["ok"]:
            reward_names = ", ".join(
                f"{c.get('name', 'Unknown')} ({format_merchant_print(p)})" for c, p in result["rewards"]
            ) or "nothing (empty reward pool)"
            embed = discord.Embed(
                title="Trade Completed!",
                description=f"You received: **{reward_names}**",
                color=THEME_COLOR
            )
        else:
            embed = discord.Embed(
                title="Trade Failed",
                description=result["reason"],
                color=discord.Color.red()
            )

        await interaction.response.edit_message(embed=embed, view=self, attachments=[])

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your trade!", ephemeral=True)

        active_merchant_trades.pop(self.user_id, None)
        for item in self.children:
            item.disabled = True

        embed = discord.Embed(color=THEME_COLOR, description="Trade cancelled. No cards were taken.")
        await interaction.response.edit_message(embed=embed, view=self, attachments=[])

    async def on_timeout(self):
        active_merchant_trades.pop(self.user_id, None)
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                embed = discord.Embed(color=THEME_COLOR, description="Trade expired. No cards were taken.")
                await self.message.edit(embed=embed, view=self, attachments=[])
            except Exception:
                pass

    async def _execute_trade(self) -> dict:
        """
        Validates, then -- only if every check passes -- atomically
        executes the trade: removes exactly the player-selected cards
        (nothing else), grants 1-2 merchant-exclusive-print reward
        cards, and decrements the merchant's stock by exactly 1,
        regardless of whether 1 or 2 reward cards were granted.

        If ANY step after validation fails (a save error, most notably),
        every mutation made during this call -- the inventory removals
        AND additions, the merchant's stock, and the shared card_prints
        counter used for merchant-print numbers -- is rolled back in
        full, so a failure can never partially remove/duplicate/lose
        cards or leave merchant stock out of sync with what was actually
        granted.
        """
        merchant = self._merchant()
        if merchant is None:
            return {"ok": False, "reason": "This merchant no longer exists."}

        if not _merchant_is_active(merchant, time.time()):
            return {"ok": False, "reason": "This merchant is no longer available."}

        info = get_merchant_display_info(merchant)
        if info is None:
            return {"ok": False, "reason": "This merchant is no longer available."}

        wants = info.get("wants", [])

        # Stock check.
        if info.get("stock", 0) <= 0:
            return {"ok": False, "reason": "This merchant is out of stock."}

        # Every "Looking For" slot must have an offered card.
        if not wants or len(self.selections) != len(wants) or any(i not in self.selections for i in range(len(wants))):
            return {"ok": False, "reason": "You haven't offered a card for every request yet."}

        # Duplicate-selection check: every offered card must be distinct
        # (no offering the same physical card for two slots).
        keys = [(sel["card"].get("id"), sel["print"]) for sel in self.selections.values()]
        if len(set(keys)) != len(keys):
            return {"ok": False, "reason": "You can't offer the same card twice."}

        # Requirement check: each slot's offered card must match that
        # slot's id/series/rarity exactly, against the merchant's fixed,
        # snapshotted requirement -- never live cards.json, since a
        # merchant's requirements stay fixed until it leaves.
        for i, want in enumerate(wants):
            sel = self.selections[i]
            oc = sel["card"]
            if oc.get("id") != want.get("id"):
                return {"ok": False, "reason": f"{oc.get('name', 'That card')} doesn't match what the merchant is looking for."}
            if oc.get("series") != want.get("series"):
                return {"ok": False, "reason": "One of your offered cards doesn't match the required series."}
            if oc.get("stars") != want.get("stars"):
                return {"ok": False, "reason": "One of your offered cards doesn't match the required rarity."}

        async with inventories_lock:
            inv = get_inventory(self.user_id)

            # Ownership check: resolve each selection to its CURRENT
            # index by id + print (robust against any index drift since
            # selection time), exactly like TradeView's finalize step.
            idx_list = []
            for sel in self.selections.values():
                idx = next(
                    (i for i, c in enumerate(inv)
                     if c["card"].get("id") == sel["card"].get("id") and c["print"] == sel["print"]),
                    None
                )
                if idx is None:
                    return {"ok": False, "reason": "You no longer own one of the cards you offered."}
                idx_list.append(idx)

            if len(set(idx_list)) != len(idx_list):
                return {"ok": False, "reason": "You can't offer the same card twice."}

            async with merchants_lock:
                # Re-resolve the live merchant dict and re-check
                # stock/activity right before mutating anything -- covers
                # another trade completing against this same merchant in
                # the window between the checks above and now.
                live_merchant = None
                for m in (merchants.get("merchants") or []):
                    if m.get("id") == self.merchant_id:
                        live_merchant = m
                        break

                if live_merchant is None or not _merchant_is_active(live_merchant, time.time()):
                    return {"ok": False, "reason": "This merchant is no longer available."}

                inv_backup = list(inv)
                stock_backup = live_merchant.get("stock", 0)
                prints_backup = dict(card_prints)

                try:
                    # Remove ONLY the selected cards -- highest index
                    # first so earlier indices stay valid mid-removal.
                    for i in sorted(idx_list, reverse=True):
                        inv.pop(i)

                    reward_pool = info.get("rewards", [])
                    reward_count = (
                        min(len(reward_pool), random.randint(1, MERCHANT_MAX_REWARD_CARDS_PER_TRADE))
                        if reward_pool else 0
                    )
                    chosen_rewards = random.sample(reward_pool, reward_count) if reward_count else []

                    granted = []
                    for reward_card in chosen_rewards:
                        print_num = get_next_merchant_print(reward_card.get("id"))
                        owned_card = {
                            "card": reward_card,
                            "print": print_num,
                            "claimed_at": time.time(),
                            # Marks this specific owned card as merchant-
                            # granted, so any later render of it (lv,
                            # lgift/lgw's GiftView) knows to show its real
                            # print number instead of collapsing to "L"
                            # past 100 -- see render_card's force_real_print.
                            # Never set anywhere else, so every other
                            # command's cards are completely unaffected.
                            "merchant_reward": True,
                        }
                        inv.insert(0, owned_card)
                        granted.append((reward_card, print_num))

                    # Stock drops by exactly 1 per trade, regardless of
                    # whether 1 or 2 reward cards were granted.
                    live_merchant["stock"] = max(0, live_merchant.get("stock", 0) - 1)

                    save_inventories_local()
                    save_merchants_local()
                    mark_inventories_dirty()
                    mark_merchants_dirty()
                except Exception:
                    # Roll back EVERYTHING -- inventory, merchant stock,
                    # and the shared print counter -- so a failed save
                    # never leaves a half-completed trade in any form.
                    inv[:] = inv_backup
                    live_merchant["stock"] = stock_backup
                    card_prints.clear()
                    card_prints.update(prints_backup)
                    print("[merchants] Trade finalize failed, rolled back:")
                    traceback.print_exc()
                    return {"ok": False, "reason": "Something went wrong completing the trade. No cards were taken."}

                return {"ok": True, "rewards": granted}


# =========================
# EDIT CARD VIEW (leditcard)
# =========================
class EditCardView(discord.ui.View):
    """
    Interactive edit panel for a single card. `card` is the actual dict
    object living inside the global `cards` list, so mutating it here is
    immediately reflected everywhere else in the bot -- the only extra
    steps needed are persisting it (save_cards_json) and pushing it to
    GitHub (github_commit_files / github_commit_changes), exactly like
    the existing laddcard/lupdateimage commands do.
    """

    def __init__(self, bot, card, user, user_id):
        super().__init__(timeout=180)
        self.bot = bot
        self.card = card
        self.user = user
        self.user_id = user_id
        self.message = None  # set by the caller right after sending

    def build_embed(self):
        card = self.card
        claims = card_prints.get(card.get("id", ""), 0)

        embed = discord.Embed(color=THEME_COLOR, title=f"✏️ Editing: {card.get('name', 'Unknown')}")
        embed.add_field(name="Card ID", value=f"`{card.get('id', 'unknown')}`", inline=True)
        embed.add_field(name="Series", value=card.get("series", "Unknown Series"), inline=True)
        embed.add_field(name="Frame", value=card.get("frame", "common"), inline=True)
        embed.add_field(name="Stars", value=stars(card.get("stars", 1)), inline=True)
        embed.add_field(name="Claims", value=f"**{claims}**", inline=True)
        embed.add_field(name="Image Path", value=f"`{card.get('image', 'none')}`", inline=False)
        embed.set_footer(text="Use the buttons below to edit a single property.")
        return embed

    def interaction_ok(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def refresh_message(self):
        """Re-renders the embed on the original message after a successful edit."""
        if self.message is not None:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except Exception:
                pass

    async def persist_and_sync(self, commit_message, extra_files=None):
        """
        Persists the current in-memory `cards` list: pushes to GitHub
        first (atomically, reusing the existing sync helpers), and only
        mirrors the change to local disk once that succeeds -- matching
        the safety pattern used by lupdateimage/laddcard.
        """
        cards_json_bytes = json.dumps(cards, indent=2).encode("utf-8")
        files = dict(extra_files or {})
        files["cards.json"] = cards_json_bytes

        await github_commit_files(files, commit_message)

        for path, content in files.items():
            if path == "cards.json":
                continue
            _atomic_write_bytes(path, content)

        save_cards_json()

    async def prompt_for_message(self, interaction: discord.Interaction, prompt_text, require_attachment=False):
        """Sends a prompt and waits for the user's next reply in the same channel."""
        await interaction.response.send_message(prompt_text, ephemeral=True)

        def check(m):
            if m.author.id != self.user_id or m.channel.id != interaction.channel.id:
                return False
            if require_attachment:
                return len(m.attachments) > 0
            return True

        return await self.bot.wait_for("message", check=check, timeout=180)

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.primary, row=0)
    async def rename_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.interaction_ok(interaction):
            return await interaction.response.send_message("This isn't your edit session!", ephemeral=True)

        try:
            msg = await self.prompt_for_message(interaction, "Send the new **name** for this card.")
        except asyncio.TimeoutError:
            return await interaction.followup.send("❌ Timed out waiting for a response.", ephemeral=True)

        new_name = msg.content.strip()
        if not new_name:
            return await interaction.followup.send("❌ Name cannot be empty.", ephemeral=True)

        old_name = self.card.get("name")
        try:
            async with cards_lock:
                self.card["name"] = new_name
                await self.persist_and_sync(f"Renamed {self.card.get('id')} to {new_name}")
        except Exception as e:
            self.card["name"] = old_name
            return await interaction.followup.send(f"❌ Failed to update card: {e}", ephemeral=True)

        await self.refresh_message()
        await interaction.followup.send("✅ Card updated successfully.")

    @discord.ui.button(label="Change Series", style=discord.ButtonStyle.primary, row=0)
    async def series_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.interaction_ok(interaction):
            return await interaction.response.send_message("This isn't your edit session!", ephemeral=True)

        try:
            msg = await self.prompt_for_message(interaction, "Send the new **series** for this card.")
        except asyncio.TimeoutError:
            return await interaction.followup.send("❌ Timed out waiting for a response.", ephemeral=True)

        new_series = msg.content.strip()
        if not new_series:
            return await interaction.followup.send("❌ Series cannot be empty.", ephemeral=True)

        old_series = self.card.get("series")
        try:
            async with cards_lock:
                self.card["series"] = new_series
                await self.persist_and_sync(f"Changed series for {self.card.get('id')} to {new_series}")
        except Exception as e:
            self.card["series"] = old_series
            return await interaction.followup.send(f"❌ Failed to update card: {e}", ephemeral=True)

        await self.refresh_message()
        await interaction.followup.send("✅ Card updated successfully.")

    @discord.ui.button(label="Change Frame", style=discord.ButtonStyle.primary, row=1)
    async def frame_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.interaction_ok(interaction):
            return await interaction.response.send_message("This isn't your edit session!", ephemeral=True)

        try:
            msg = await self.prompt_for_message(
                interaction,
                "Send the new **frame** name (e.g. `common`, `blue`, or `blue.png`)."
            )
        except asyncio.TimeoutError:
            return await interaction.followup.send("❌ Timed out waiting for a response.", ephemeral=True)

        resolved = resolve_frame_name(msg.content)
        if resolved is None:
            return await interaction.followup.send(
                f"❌ Frame `{msg.content.strip()}` not found in the `frames` folder.", ephemeral=True
            )

        old_frame = self.card.get("frame")
        try:
            async with cards_lock:
                self.card["frame"] = resolved
                await self.persist_and_sync(f"Changed frame for {self.card.get('id')} to {resolved}")
        except Exception as e:
            self.card["frame"] = old_frame
            return await interaction.followup.send(f"❌ Failed to update card: {e}", ephemeral=True)

        await self.refresh_message()
        await interaction.followup.send("✅ Card updated successfully.")

    @discord.ui.button(label="Change Stars", style=discord.ButtonStyle.primary, row=1)
    async def stars_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.interaction_ok(interaction):
            return await interaction.response.send_message("This isn't your edit session!", ephemeral=True)

        try:
            msg = await self.prompt_for_message(interaction, "Send the new **star count** (a number from 1 to 4).")
        except asyncio.TimeoutError:
            return await interaction.followup.send("❌ Timed out waiting for a response.", ephemeral=True)

        try:
            new_stars = int(msg.content.strip())
        except ValueError:
            return await interaction.followup.send("❌ Stars must be a whole number between 1 and 4.", ephemeral=True)

        if new_stars not in (1, 2, 3, 4):
            return await interaction.followup.send("❌ Stars must be between 1 and 4.", ephemeral=True)

        old_stars = self.card.get("stars")
        try:
            async with cards_lock:
                self.card["stars"] = new_stars
                await self.persist_and_sync(f"Changed stars for {self.card.get('id')} to {new_stars}")
        except Exception as e:
            self.card["stars"] = old_stars
            return await interaction.followup.send(f"❌ Failed to update card: {e}", ephemeral=True)

        await self.refresh_message()
        await interaction.followup.send("✅ Card updated successfully.")

    @discord.ui.button(label="Change Image", style=discord.ButtonStyle.secondary, row=2)
    async def image_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.interaction_ok(interaction):
            return await interaction.response.send_message("This isn't your edit session!", ephemeral=True)

        try:
            msg = await self.prompt_for_message(
                interaction, "Please upload the new **image** for this card.", require_attachment=True
            )
        except asyncio.TimeoutError:
            return await interaction.followup.send("❌ Timed out waiting for an image upload.", ephemeral=True)

        # Reuse the exact same image pipeline as lupdateimage: download,
        # re-encode as a real PNG, then push it (plus cards.json) as a
        # single atomic GitHub commit before touching local disk.
        try:
            attachment = msg.attachments[0]
            raw_bytes = await attachment.read()
            png_bytes = await convert_image_bytes_to_png(raw_bytes)

            existing_path = self.card.get("image", "") or ""
            save_path = existing_path if existing_path.startswith("card_art/") else f"card_art/{self.card.get('id')}.png"
            old_image = self.card.get("image")

            async with cards_lock:
                self.card["image"] = save_path

                await self.persist_and_sync(
                    f"Updated {self.card.get('name', self.card.get('id'))} image via leditcard",
                    extra_files={save_path: png_bytes}
                )
        except Exception as e:
            self.card["image"] = old_image
            return await interaction.followup.send(f"❌ Failed to update image: {e}", ephemeral=True)

        await self.refresh_message()
        await interaction.followup.send("✅ Card updated successfully.")


# =========================
# REMOVE CARD VIEW (lremovecard)
# =========================
class RemoveCardView(discord.ui.View):
    def __init__(self, card, user_id):
        super().__init__(timeout=60)
        self.card = card
        self.user_id = user_id

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your confirmation prompt!", ephemeral=True)

        card_id = self.card.get("id")
        image_path = self.card.get("image", "") or ""

        try:
            async with cards_lock:
                # Build the post-removal cards.json contents without mutating
                # the live `cards` list yet, so nothing changes locally if the
                # GitHub commit below fails partway through. Held under
                # cards_lock for the whole transaction so no other
                # card-management command can append/remove an entry on
                # `cards` while this snapshot is in flight -- otherwise that
                # concurrent change would be silently wiped out the moment
                # `cards[:] = remaining_cards` below runs.
                remaining_cards = [c for c in cards if c.get("id") != card_id]
                cards_json_bytes = json.dumps(remaining_cards, indent=2).encode("utf-8")

                delete_paths = [image_path] if image_path.startswith("card_art/") else []

                # Push the removal to GitHub FIRST, as a single atomic commit
                # (cards.json update + image deletion together). If this
                # fails, nothing below runs and nothing local changes.
                await github_commit_changes(
                    write_files={"cards.json": cards_json_bytes},
                    delete_paths=delete_paths,
                    commit_message=f"Removed card {card_id}"
                )

                # GitHub succeeded -- now mirror the removal locally.
                cards[:] = remaining_cards
                save_cards_json()

                if image_path.startswith("card_art/") and os.path.exists(image_path):
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass

                card_prints.pop(card_id, None)

        except Exception as e:
            self.clear_items()
            await interaction.response.edit_message(
                content=f"❌ Failed to remove card `{card_id}`: {e}", embed=None, view=self
            )
            return

        self.clear_items()
        await interaction.response.edit_message(
            content=f"✅ Successfully removed `{card_id}`.", embed=None, attachments=[], view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your confirmation prompt!", ephemeral=True)

        self.clear_items()
        await interaction.response.edit_message(content="Cancelled. No changes were made.", view=self)


# =========================
# BOT CORE CONTROLLER
# =========================
class Client(discord.Client):

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        # Starts the periodic (every INVENTORY_GITHUB_SYNC_INTERVAL_SECONDS)
        # inventories.json GitHub sync exactly once. on_ready can fire
        # again after a reconnect; the .done() check stops a second copy
        # of the loop from ever running (it never returns normally, so
        # .done() only becomes True if it died -- in which case starting
        # a fresh one here is correct self-healing, not a duplicate).
        global _inventory_sync_task
        if _inventory_sync_task is None or _inventory_sync_task.done():
            _inventory_sync_task = asyncio.create_task(inventory_github_sync_loop())

        # Same singleton-guard pattern, same reasoning, for showcase
        # votes' own periodic GitHub sync.
        global _showcase_votes_sync_task
        if _showcase_votes_sync_task is None or _showcase_votes_sync_task.done():
            _showcase_votes_sync_task = asyncio.create_task(showcase_votes_github_sync_loop())

        # Same pattern again, for pending_recovery.json's own periodic
        # GitHub sync.
        global _pending_recovery_sync_task
        if _pending_recovery_sync_task is None or _pending_recovery_sync_task.done():
            _pending_recovery_sync_task = asyncio.create_task(pending_recovery_github_sync_loop())

        # One-time immediate sweep, run once at startup (guarded so a
        # later on_ready from a reconnect never repeats it) and BEFORE
        # the periodic loop task below is created. Without this, a user
        # whose RECOVERY_PENDING_DAYS deadline passed while the bot was
        # offline would sit fully elapsed but unrecovered for up to a
        # further PENDING_RECOVERY_CHECK_INTERVAL_SECONDS (a day) after
        # the bot comes back, since the loop's first action is always
        # to sleep. Uses the exact same _run_pending_recovery_sweep()
        # the periodic loop calls -- not a second implementation.
        global _startup_recovery_sweep_done
        if not _startup_recovery_sweep_done and client.guilds:
            _startup_recovery_sweep_done = True
            try:
                await _run_pending_recovery_sweep(client.guilds[0])
                print("[recovery] Completed immediate startup recovery sweep.")
            except Exception:
                print("[recovery] Immediate startup recovery sweep failed "
                      "(the periodic loop will retry on its own schedule):")
                traceback.print_exc()

        # The actual rejoin/15-day-elapsed sweep -- separate task from
        # the GitHub sync above (that one just persists whatever the
        # pending list currently is; this one is what actually changes
        # it over time).
        global _pending_recovery_check_task
        if _pending_recovery_check_task is None or _pending_recovery_check_task.done():
            _pending_recovery_check_task = asyncio.create_task(pending_recovery_check_loop())

        # Same singleton-guard pattern, same reasoning, for mail.json's
        # own periodic GitHub sync.
        global _mail_sync_task
        if _mail_sync_task is None or _mail_sync_task.done():
            _mail_sync_task = asyncio.create_task(mail_github_sync_loop())

        # Same singleton-guard pattern, same reasoning, for duo.json's
        # own periodic GitHub sync.
        global _duo_sync_task
        if _duo_sync_task is None or _duo_sync_task.done():
            _duo_sync_task = asyncio.create_task(duo_github_sync_loop())

        # Same singleton-guard pattern, same reasoning, for
        # version_system.json's own periodic GitHub sync.
        global _version_system_sync_task
        if _version_system_sync_task is None or _version_system_sync_task.done():
            _version_system_sync_task = asyncio.create_task(version_system_github_sync_loop())

        # Same singleton-guard pattern, same reasoning, for
        # backup_status.json's own periodic GitHub sync (a safety net --
        # `lbackup` itself already commits this file directly).
        global _backup_status_sync_task
        if _backup_status_sync_task is None or _backup_status_sync_task.done():
            _backup_status_sync_task = asyncio.create_task(backup_status_github_sync_loop())

        # Same singleton-guard pattern, same reasoning, for
        # maintenance.json's own periodic GitHub sync (a safety net --
        # `lmaintenance` itself already saves+marks this file directly).
        global _maintenance_sync_task
        if _maintenance_sync_task is None or _maintenance_sync_task.done():
            _maintenance_sync_task = asyncio.create_task(maintenance_github_sync_loop())

        # One-time migration: existing players get +5 extra drops/claims.
        # See _run_extra_bonus_migration_once() -- guarded so it can
        # never run twice and never applies to players created later.
        try:
            await _run_extra_bonus_migration_once()
        except Exception:
            print("[duo] Extra bonus migration failed (will retry next startup):")
            traceback.print_exc()

        # Same singleton-guard pattern, same reasoning, for merchants.json's
        # own periodic GitHub sync.
        global _merchants_sync_task
        if _merchants_sync_task is None or _merchants_sync_task.done():
            _merchants_sync_task = asyncio.create_task(merchants_github_sync_loop())

        # Separate task from the GitHub sync above (that one just persists
        # whatever the merchant state currently is; this one is what
        # actually progresses it over time -- expiring/depleting the
        # current set, starting the 2-day cooldown, and eventually
        # generating a new set).
        global _merchant_check_task
        if _merchant_check_task is None or _merchant_check_task.done():
            _merchant_check_task = asyncio.create_task(merchant_check_loop())

        # Backstop sweep for active_trades -- see trade_expiration_sweep_loop
        # for why this exists alongside TradeView's own on_timeout.
        global _trade_sweep_task
        if _trade_sweep_task is None or _trade_sweep_task.done():
            _trade_sweep_task = asyncio.create_task(trade_expiration_sweep_loop())

        # Sends any merchant arrival/departure announcement that was
        # detected before the client was ready (most commonly: a
        # brand-new bot's very first-ever merchant batch, generated at
        # import time before login) -- see _announce_merchant_event /
        # _flush_pending_merchant_announcements above. Never permanently
        # skipped; just deferred until this point.
        try:
            await _flush_pending_merchant_announcements()
        except Exception:
            print("[merchants] Failed to flush a deferred merchant announcement:")
            traceback.print_exc()

        # Registers a SIGTERM handler exactly once so Railway's redeploy
        # signal actually triggers a graceful close() (and therefore the
        # shutdown flush below) -- Python does NOT do this on its own for
        # SIGTERM (unlike SIGINT/Ctrl-C, which asyncio.run already turns
        # into a clean shutdown). Without this, Railway's SIGTERM would
        # just kill the process outright, skipping the flush entirely.
        global _shutdown_handler_registered
        if not _shutdown_handler_registered:
            try:
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.close()))
                _shutdown_handler_registered = True
            except (NotImplementedError, RuntimeError):
                # add_signal_handler isn't available on some platforms
                # (e.g. Windows); Railway's containers are Linux, so this
                # should always succeed there, but don't fail startup if
                # it's ever unavailable.
                pass

    async def close(self):
        # Best-effort: push any inventory changes saved locally since
        # the last periodic sync before the process actually exits.
        # Triggered both by the SIGTERM handler above (Railway redeploy)
        # and by discord.py's own normal shutdown path. Keeps the
        # batched/dirty-flag design entirely intact -- this is only one
        # extra, early flush at shutdown, not a change to how batching
        # works day-to-day.
        try:
            await flush_inventories_to_github()
        except Exception:
            print("[inventories] Failed to flush pending inventory changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_showcase_votes_to_github()
        except Exception:
            print("[showcase_votes] Failed to flush pending vote changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_pending_recovery_to_github()
        except Exception:
            print("[recovery] Failed to flush pending recovery changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_mail_to_github()
        except Exception:
            print("[mail] Failed to flush pending mail changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_duo_to_github()
        except Exception:
            print("[duo] Failed to flush pending duo changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_version_system_to_github()
        except Exception:
            print("[versions] Failed to flush pending version system changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_backup_status_to_github()
        except Exception:
            print("[backup_status] Failed to flush pending backup status changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_maintenance_to_github()
        except Exception:
            print("[maintenance] Failed to flush pending maintenance state changes on shutdown:")
            traceback.print_exc()

        try:
            await flush_merchants_to_github()
        except Exception:
            print("[merchants] Failed to flush pending merchant state changes on shutdown:")
            traceback.print_exc()

        await super().close()

    async def on_message(self, message):
        # Ignore bot's own messages
        if message.author == self.user:
            return

        content = message.content.strip()
        content_lower = content.lower()
        user_id = message.author.id
        inv = get_inventory(user_id)

        # =========================
        # MAINTENANCE MODE GATE
        # =========================
        # While maintenance mode is active (see `lmaintenance` below),
        # every normal-user command is blocked with a friendly notice;
        # owners are completely exempt so they can keep managing the
        # bot (including turning maintenance back off) while it's on.
        # _looks_like_bot_command() is the exact same "does this look
        # like an attempt to run one of our commands" signal the
        # unread-mail reminder below already uses, so this never
        # intercepts ordinary chat messages -- only actual `l...`
        # command attempts.
        if maintenance.get("active") and user_id not in OWNER_USER_IDS and _looks_like_bot_command(content_lower):
            return await reply(message, "The bot is currently under maintenance, please check <#1540476573608706179> for updates.")

        # =========================
        # UNREAD MAIL REMINDER
        # =========================
        # Fires on every recognized command (see _looks_like_bot_command's
        # docstring for what "recognized" means here) while unread mail
        # exists, but at most once every MAIL_REMINDER_COOLDOWN_SECONDS
        # per user -- so a chatty user running several commands in a row
        # only gets nagged once per window, not on every single command.
        # Once everything's been marked read via the mailbox's Read
        # button, has_unread_mail() goes False and this simply stops
        # firing on its own; the cooldown timestamp is only ever
        # refreshed when a reminder is actually shown, so the very next
        # command after new mail arrives still reminds immediately.
        if _looks_like_bot_command(content_lower) and has_unread_mail(user_id):
            now_ts = time.time()
            last_shown = _last_mail_reminder_at.get(user_id, 0)
            if now_ts - last_shown >= MAIL_REMINDER_COOLDOWN_SECONDS:
                _last_mail_reminder_at[user_id] = now_ts
                unread_count = unread_mail_count(user_id)
                letter_word = "letter" if unread_count == 1 else "letters"
                await reply(message,
                    f"📬 You have {unread_count} unread {letter_word}! Use `lmail` to open your mailbox."
                )

        # =========================
        # LUPDATEIMAGE COMMAND
        # =========================
        if content_lower.startswith("lupdateimage "):
            # Check if user has "uploader" role
            if not any(role.name.lower() == "uploader" for role in message.author.roles):
                return await reply(message, "You need the **Uploader** role to use this command.")

            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lupdateimage <card_id>`")

            card_id = parts[1]

            # Find the card
            card = next((c for c in cards if c["id"] == card_id), None)
            if not card:
                return await reply(message, f"Card with ID `{card_id}` not found.")

            # Check for attachments
            if not message.attachments:
                return await reply(message, "Please attach an image to update.")

            attachment = message.attachments[0]

            # The image field in cards.json is the single source of truth
            # for this card's filename/path. Never invent or fall back to
            # a generated path -- if it's missing or invalid, stop here.
            existing_path = card.get("image", "") or ""
            if not existing_path.startswith("card_art/"):
                return await reply(message, 
                    "❌ This card does not have a valid image path in `cards.json`. "
                    "Please fix the `image` field before using `lupdateimage`."
                )

            save_path = existing_path

            # Download and save the image, then sync to GitHub
            try:
                image_data = await attachment.read()

                # Decode the uploaded image with Pillow and re-encode it as
                # a genuine PNG (not just a renamed file extension).
                image_data = await convert_image_bytes_to_png(image_data)

                async with cards_lock:
                    # The image field itself is never rewritten -- save_path
                    # came directly from cards.json above. cards.json is still
                    # included in the commit (as its current, unchanged
                    # contents) so the image and cards.json always land in the
                    # same atomic commit together.
                    cards_json_bytes = json.dumps(cards, indent=2).encode("utf-8")
                    github_files = {
                        save_path: image_data,
                        "cards.json": cards_json_bytes,
                    }

                    commit_message = f"Updated {card.get('name', card_id)} image"

                    # Push to GitHub FIRST, as a single atomic commit. If this
                    # fails, nothing below runs, so local disk stays in sync
                    # with the remote repo.
                    await github_commit_files(github_files, commit_message)

                    # GitHub commit succeeded -- now mirror the change locally.
                    _atomic_write_bytes(save_path, image_data)

                await reply(message, 
                    f"✅ Card `{card_id}` image updated successfully and pushed to GitHub!\nNew path: `{save_path}`"
                )
            except Exception as e:
                await reply(message, f"❌ Error updating image: {e}")
            return

        # =========================
        # LADDCARD COMMAND
        # =========================
        if content_lower.startswith("laddcard "):
            # Check if user has "uploader" role
            if not any(role.name.lower() == "uploader" for role in message.author.roles):
                return await reply(message, "You need the **Uploader** role to use this command.")

            # Parse the command: laddcard "Name" | "Series" | frame | stars
            try:
                args = content[9:].strip()  # Remove 'laddcard '
                parts = [p.strip().strip('"') for p in args.split('|')]

                if len(parts) < 4:
                    return await reply(message, "Usage: `laddcard \"Name\" | \"Series\" | frame | stars`\nExample: `laddcard \"Ivan\" | \"Alien Stage\" | common | 4`")

                char_name = parts[0]
                series = parts[1]
                requested_frame = parts[2].strip()
                stars_val = int(parts[3])

                # Frame resolution logic -- accepts any exact frame that
                # exists in the frames folder, with or without ".png".
                # There is no generic "rare" option anymore: the user must
                # always specify the exact frame they want (blue, red,
                # pink, gold, etc.). Nothing is chosen randomly.
                frames_dir = "frames"
                candidate = requested_frame[:-4] if requested_frame.lower().endswith(".png") else requested_frame
                candidate_path = os.path.join(frames_dir, f"{candidate}.png")

                if not os.path.exists(candidate_path):
                    return await reply(message, 
                        f"❌ Frame `{requested_frame}` not found in the `frames` folder. "
                        "Use `common` or the exact name of an existing frame file (with or without `.png`)."
                    )

                frame_name = candidate
                is_rare = (frame_name.lower() != "common")

                if stars_val not in [1, 2, 3, 4]:
                    return await reply(message, "Stars must be 1, 2, 3, or 4.")

                # Generate card ID based on rarity (common/rare), not the
                # specific frame color -- e.g. mydei_common / mydei_rare,
                # regardless of whether the rare frame is blue, red, gold, etc.
                card_id = generate_card_id(char_name, is_rare)

                # Ask for image
                await reply(message, f"Card ID: `{card_id}`\nNow send the art image for **{char_name}**.")

                # Wait for image attachment using the client wait_for
                def check(m):
                    return m.author == message.author and len(m.attachments) > 0 and m.channel == message.channel

                try:
                    img_msg = await self.wait_for('message', check=check, timeout=300)
                except asyncio.TimeoutError:
                    return await reply(message, "❌ Image upload timed out. Card creation cancelled.")

                # Save the image
                try:
                    attachment = img_msg.attachments[0]
                    image_data = await attachment.read()

                    # Decode the uploaded image with Pillow and re-encode it
                    # as a genuine PNG (not just a renamed file extension).
                    image_data = await convert_image_bytes_to_png(image_data)

                    save_path = f"card_art/{card_id}.png"

                    async with cards_lock:
                        # `card_id` was computed before the (possibly
                        # multi-minute) image-upload wait above, entirely
                        # outside this lock. If another laddcard for the
                        # same character/rarity completed in the meantime,
                        # it could have already claimed this exact id --
                        # writing to the same card_art/<id>.png path would
                        # silently overwrite that other card's artwork.
                        # Re-check now, inside the lock, and regenerate a
                        # fresh unique id if that happened.
                        if any(c.get("id") == card_id for c in cards):
                            card_id = generate_card_id(char_name, is_rare)
                            save_path = f"card_art/{card_id}.png"

                        # Create the card object
                        new_card = {
                            "id": card_id,
                            "name": char_name,
                            "series": series,
                            "stars": stars_val,
                            "weight": weight_for_stars(stars_val),
                            "image": save_path,
                            "frame": frame_name,
                            # Same character-grouped Common numbering the
                            # startup migration assigns to every existing
                            # card -- see _next_version_for_new_card.
                            "version": _next_version_for_new_card(char_name, series, frame_name),
                        }

                        # Mutate in-memory first so the cards.json payload
                        # below includes the new card, then push to GitHub
                        # FIRST -- same safety pattern as
                        # lupdateimage/persist_and_sync -- and only mirror
                        # locally once that succeeds. Previously this saved
                        # locally only and never reached GitHub, so a new
                        # card would vanish on the next Railway redeploy
                        # (a fresh checkout never had it).
                        cards.append(new_card)

                        try:
                            cards_json_bytes = json.dumps(cards, indent=2).encode("utf-8")
                            github_files = {
                                save_path: image_data,
                                "cards.json": cards_json_bytes,
                            }
                            commit_message = f"Added card {card_id}"

                            await github_commit_files(github_files, commit_message)

                            # GitHub commit succeeded -- now mirror locally.
                            _atomic_write_bytes(save_path, image_data)
                            save_cards_json()
                        except Exception:
                            # Roll back the in-memory addition so a failed
                            # GitHub push never leaves a card that only
                            # exists in memory (and would vanish silently
                            # on the next restart anyway).
                            cards.pop()
                            raise

                    await reply(message, f"✅ Card created successfully!\n**ID:** `{card_id}`\n**Name:** {char_name}\n**Series:** {series}\n**Stars:** {stars_val}\n**Frame:** {frame_name}")

                    await send_card_added_notification(self, new_card)
                except Exception as e:
                    await reply(message, f"❌ Error creating card: {e}")

            except Exception as e:
                await reply(message, f"❌ Error parsing command: {e}")
            return

        # =========================
        # LSYNC COMMAND (health/status check)
        # =========================
        if content_lower == "lsync":
            total_cards = len(cards)
            unique_characters = len({c.get("name", "").strip().lower() for c in cards if c.get("name")})

            # Duplicate card IDs
            id_counts = {}
            for c in cards:
                cid = c.get("id")
                if cid:
                    id_counts[cid] = id_counts.get(cid, 0) + 1
            duplicate_ids = sorted([cid for cid, count in id_counts.items() if count > 1])

            # Duplicate image paths
            image_counts = {}
            for c in cards:
                img = c.get("image")
                if img:
                    image_counts[img] = image_counts.get(img, 0) + 1
            duplicate_images = sorted([img for img, count in image_counts.items() if count > 1])

            # Missing required fields
            missing_fields = []
            for c in cards:
                missing = [
                    field for field in REQUIRED_CARD_FIELDS
                    if c.get(field) is None or c.get(field) == ""
                ]
                if missing:
                    identifier = c.get("id") or c.get("name") or "unknown card"
                    missing_fields.append(f"`{identifier}` missing: {', '.join(missing)}")

            # Broken local image paths
            broken_images = []
            for c in cards:
                img = c.get("image", "") or ""
                if img.startswith("card_art/") and not os.path.exists(img):
                    broken_images.append(f"`{c.get('id', 'unknown')}` -> `{img}`")

            # GitHub configuration
            github_missing = [
                name for name in ("GITHUB_TOKEN", "GITHUB_USERNAME", "GITHUB_REPO", "GITHUB_BRANCH")
                if not os.environ.get(name)
            ]
            github_ok = len(github_missing) == 0

            database_healthy = not (duplicate_ids or duplicate_images or missing_fields or broken_images)

            def format_list(items, limit=10):
                if not items:
                    return "✅ None found"
                shown = items[:limit]
                text = "\n".join(f"• {item}" for item in shown)
                if len(items) > limit:
                    text += f"\n...and {len(items) - limit} more"
                return text

            embed = discord.Embed(
                color=discord.Color.green() if database_healthy and github_ok else discord.Color.orange(),
                title="🔄 Luka Sync Status",
                description="Read-only diagnostic report of `cards.json`. Nothing is modified automatically."
            )
            embed.add_field(name="📦 Total Cards", value=str(total_cards), inline=True)
            embed.add_field(name="🎭 Unique Characters", value=str(unique_characters), inline=True)
            embed.add_field(
                name="🔑 GitHub Config",
                value="✅ Configured" if github_ok else f"❌ Missing: {', '.join(github_missing)}",
                inline=True
            )
            embed.add_field(name="🆔 Duplicate Card IDs", value=format_list(duplicate_ids), inline=False)
            embed.add_field(name="🖼️ Duplicate Image Paths", value=format_list(duplicate_images), inline=False)
            embed.add_field(name="⚠️ Missing Required Fields", value=format_list(missing_fields), inline=False)
            embed.add_field(name="📁 Broken/Missing Image Files", value=format_list(broken_images), inline=False)
            embed.add_field(
                name="Overall Status",
                value="✅ Database appears healthy." if database_healthy else "⚠️ Issues found -- see above. Please fix manually.",
                inline=False
            )
            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time()))} UTC")

            await reply(message, embed=embed)
            return

        # =========================
        # LEDITCARD COMMAND
        # =========================
        if content_lower.startswith("leditcard "):
            if not has_uploader_role(message.author):
                return await reply(message, "You need the **Uploader** role to use this command.")

            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `leditcard <card_id>`")

            card_id = parts[1]
            card = next((c for c in cards if c.get("id") == card_id), None)
            if not card:
                return await reply(message, f"Card with ID `{card_id}` not found.")

            view = EditCardView(self, card, message.author, user_id)
            sent = await message.channel.send(embed=view.build_embed(), view=view)
            view.message = sent
            return

        # =========================
        # LREMOVECARD COMMAND
        # =========================
        if content_lower.startswith("lremovecard "):
            if not has_uploader_role(message.author):
                return await reply(message, "You need the **Uploader** role to use this command.")

            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lremovecard <card_id>`")

            card_id = parts[1]
            card = next((c for c in cards if c.get("id") == card_id), None)
            if not card:
                return await reply(message, f"Card with ID `{card_id}` not found.")

            embed = discord.Embed(
                color=discord.Color.red(),
                title="⚠️ Confirm Card Removal",
                description="This will permanently delete this card from `cards.json` and GitHub."
            )
            embed.add_field(name="Character", value=card.get("name", "Unknown"), inline=True)
            embed.add_field(name="Series", value=card.get("series", "Unknown Series"), inline=True)
            embed.add_field(name="Card ID", value=f"`{card_id}`", inline=True)
            embed.add_field(name="Frame", value=card.get("frame", "common"), inline=True)
            embed.add_field(name="Stars", value=stars(card.get("stars", 1)), inline=True)

            view = RemoveCardView(card, user_id)

            image_path = None
            try:
                image_path = render_card_final(card, peek_next_print(card_id), hide_print=True)
                file = discord.File(image_path, filename="card.png")
                embed.set_thumbnail(url="attachment://card.png")
                await message.channel.send(embed=embed, file=file, view=view)
            except Exception:
                await message.channel.send(embed=embed, view=view)
            finally:
                if image_path:
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass
            return

        # =========================
        # HELP COMMAND (lhelp)
        # =========================
        if content_lower == "lhelp":
            pages = _build_help_pages()
            view = HelpPaginationView(pages, message.author.id)
            await reply(message, embed=view.build_embed(), view=view)
            return

        # =========================
        # COOLDOWNS COMMAND (lcd)
        # =========================
        if content_lower == "lcd":
            if is_command_spam(user_id, "lcd"):
                return await reply(message, 
                    "Please wait a few seconds before using this command again."
                )

            now = time.time()

            def _cooldown_status(seconds_remaining, ready_text, bonus_count=0):
                if seconds_remaining <= 0:
                    status = f"**{ready_text}**"
                else:
                    minutes = seconds_remaining // 60
                    secs = seconds_remaining % 60
                    if minutes > 0:
                        status = f"**{minutes}m {secs}s remaining**"
                    else:
                        status = f"**{secs}s remaining**"
                # Duo bonus uses: the multiplier shown is the TOTAL
                # number of times this action can be done back-to-back
                # right now -- this use plus whatever's banked -- so 1
                # banked bonus (which lets you go again once more) reads
                # as "(x2)", not "(x1)". With 0 banked, nothing is shown
                # at all -- there's no multiplier over the normal rate.
                # Always reflects duo["bonus"] as it currently stands
                # (get_bonus() reads it live), so this can never show a
                # stale count.
                if bonus_count >= 1:
                    status = f"{status} (x{bonus_count + 1})"
                return status

            # Drop status
            if user_id in drop_cooldowns:
                remaining = int(DROP_COOLDOWN - (now - drop_cooldowns[user_id]))
            else:
                remaining = 0
            drop_bonus = get_bonus(user_id).get("drop", 0)
            drop_status = _cooldown_status(remaining, "Ready to drop!", drop_bonus)

            # Claim status
            if user_id in claim_cooldowns:
                remaining = int(CLAIM_COOLDOWN - (now - claim_cooldowns[user_id]))
            else:
                remaining = 0
            claim_bonus = get_bonus(user_id).get("claim", 0)
            claim_status = _cooldown_status(remaining, "Ready to claim!", claim_bonus)

            embed = discord.Embed(
                color=THEME_COLOR,
                title=f"{message.author.display_name}'s Cooldowns",
                description=(
                    f"> ### Drop Cooldown\n"
                    f"Status: {drop_status}\n"
                    f"Cooldown: `{DROP_COOLDOWN//60} minutes`\n\n"
                    f"> ### Claim Cooldown\n"
                    f"Status: {claim_status}\n"
                    f"Cooldown: `{CLAIM_COOLDOWN//60} minutes`"
                )
            )
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
            embed.set_thumbnail(url="attachment://cooldown.png")
            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))} UTC")

            cooldown_image_file = discord.File("cooldown.png", filename="cooldown.png")
            await reply(message, embed=embed, file=cooldown_image_file)
            return

        # =========================
        # INVENTORY COMMAND (lc)
        # =========================
        if content_lower.startswith("lc"):
            target_user = message.author
            args = content[2:].strip()
            # True only for `lc @Luka` (a mention of the bot's own
            # account) -- routes the SAME InventoryView/pagination every
            # normal user's `lc` uses at the bot's recovery/"__system__"
            # inventory instead of a real per-user one, with only the
            # embed title swapped out below. No other `lc` behavior
            # (sorting, filters, pagination, non-Luka mentions/IDs) changes.
            is_luka_inventory = False

            if message.reference and message.reference.resolved:
                replied_msg = message.reference.resolved
                if replied_msg and replied_msg.author:
                    target_user = replied_msg.author
                    if target_user.id == self.user.id:
                        is_luka_inventory = True

            elif message.mentions and message.mentions[0].id == self.user.id:
                target_user = message.mentions[0]
                is_luka_inventory = True
                mention_tokens = (f"<@{target_user.id}>", f"<@!{target_user.id}>")
                for token in mention_tokens:
                    if args.startswith(token):
                        args = args[len(token):].strip()
                        break

            elif args:
                first_part = args.split()[0]
                if first_part.isdigit():
                    member = message.guild.get_member(int(first_part))
                    if member:
                        target_user = member
                        args = args[len(first_part):].strip()
                        if member.id == self.user.id:
                            is_luka_inventory = True

            # `-p`: display-order-only sort by print number ascending.
            # `-untagged`: shows only cards with no tag at all.
            # Both extracted as standalone flags before the s:/c:/t:/p:
            # filters below so neither can be mistaken for part of a
            # query. Never changes inventory numbering.
            arg_tokens = args.split()
            sort_by_print = any(t.lower() == "-p" for t in arg_tokens)
            show_untagged_only = any(t.lower() == "-untagged" for t in arg_tokens)
            if sort_by_print or show_untagged_only:
                args = " ".join(
                    t for t in arg_tokens
                    if t.lower() not in ("-p", "-untagged")
                )

            target_inv = get_inventory(SYSTEM_RECOVERY_USER) if is_luka_inventory else get_inventory(target_user.id)

            # Attach each card's TRUE display number (its position in the
            # full, unfiltered inventory, counted from highest/newest at
            # the top down to 1/oldest at the bottom) BEFORE filtering, so
            # a card's number never changes depending on what filter is
            # applied -- only which cards are shown changes.
            full_total = len(target_inv)
            numbered_inventory = [
                (full_total - i, owned_card)
                for i, owned_card in enumerate(target_inv)
            ]

            filtered_inventory = numbered_inventory[:]

            args_lower = args.lower()

            if "s:" in args_lower:
                series_query = args_lower.split("s:", 1)[1].strip()
                filtered_inventory = [
                    (num, owned_card) for num, owned_card in filtered_inventory
                    if series_query in owned_card["card"].get("series", "").lower()
                ]

            elif "c:" in args_lower:
                char_query = args_lower.split("c:", 1)[1].strip()
                filtered_inventory = [
                    (num, owned_card) for num, owned_card in filtered_inventory
                    if char_query in owned_card["card"].get("name", "").lower()
                ]

            elif "t:" in args_lower:
                # Exact match against the stored tag -- unlike s:/c:
                # (substring, case-insensitive), the tag must match
                # exactly what's stored, so case/emoji content is taken
                # from the original (non-lowercased) args.
                tag_marker_index = args_lower.index("t:")
                tag_query = args[tag_marker_index + 2:].strip()
                filtered_inventory = [
                    (num, owned_card) for num, owned_card in filtered_inventory
                    if owned_card.get("tags") == tag_query
                ]

            elif "p:" in args_lower:
                print_query_raw = args_lower.split("p:", 1)[1].strip()
                try:
                    print_query = int(print_query_raw)
                    filtered_inventory = [
                        (num, owned_card) for num, owned_card in filtered_inventory
                        if owned_card["print"] == print_query
                    ]
                except ValueError:
                    pass  # not a valid number -- same silent no-op as an
                          # empty/unmatched s:/c: query

            if show_untagged_only:
                filtered_inventory = [
                    (num, owned_card) for num, owned_card in filtered_inventory
                    if not owned_card.get("tags")
                ]

            if sort_by_print:
                filtered_inventory.sort(key=lambda item: item[1]["print"])

            # Pinned cards always appear first, regardless of any other
            # sort/filter above -- a stable sort preserves whatever
            # relative order was already established (newest-first
            # normally, or print-ascending under -p) within each of the
            # pinned/unpinned groups. Display order only; doesn't touch
            # display_number, which was already fixed above.
            filtered_inventory.sort(key=lambda item: not item[1].get("pinned", False))

            user_viewing_inventory[user_id] = target_user.id

            view = InventoryView(
                target_user,
                filtered_inventory,
                viewer_id=message.author.id,
                title_override="🤖 Luka's Collection" if is_luka_inventory else None
            )

            await reply(message, 
                embed=view.get_embed(),
                view=view
            )
            return

        # =========================
        # GIFT COMMAND (lg / lgift)
        # =========================
        if content_lower.startswith(("lgift ", "lg ")):
            if is_command_spam(user_id, "lg"):
                return await reply(message, 
                    "Please wait a few seconds before using this command again."
                )

            if not message.mentions:
                return await reply(message, 
                    "Usage: `lgift @user <inventory number>`"
                )

            target_user = message.mentions[0]

            if target_user.bot:
                return await reply(message, 
                    "You can't gift cards to bots."
                )

            if target_user.id == message.author.id:
                return await reply(message, 
                    "You can't gift cards to yourself."
                )

            parts = message.content.split()

            try:
                requested_num = int(parts[-1])
            except:
                return await reply(message, 
                    "Please provide a valid inventory number."
                )

            # Displayed numbers count down from newest (highest) to oldest
            # (1), so convert back to a list index accordingly.
            card_index = len(inv) - requested_num

            if card_index < 0 or card_index >= len(inv):
                return await reply(message, 
                    "Invalid inventory number."
                )

            owned_card = inv[card_index]

            view = GiftView(
                message.author,
                target_user,
                owned_card,
                message.author.id,
                target_user.id,
                card_index
            )

            gift_embed, file = view.build_embed(message.author)

            # If there's an image, attach it when sending
            if file:
                gift_message = await message.channel.send(
                    content=f"{message.author.mention} is gifting {target_user.mention} a card!",
                    embed=gift_embed,
                    file=file,
                    view=view
                )
                try:
                    os.remove(file.fp.name)
                except:
                    pass
            else:
                gift_message = await message.channel.send(
                    content=f"{message.author.mention} is gifting {target_user.mention} a card!",
                    embed=gift_embed,
                    view=view
                )

            view.message = gift_message

            return

        # =========================
        # GIVEAWAY COMMAND (lgw) -- Finalist-only, gifts from Luka's
        # recovery ("__system__") inventory using the exact same
        # GiftView/accept flow as lgift. No separate gifting system.
        # =========================
        if content_lower.startswith("lgw "):
            if not any(r.id == FINALIST_ROLE_ID for r in message.author.roles):
                return await reply(message, "You need the **Finalist** role to use this command.")

            if is_command_spam(user_id, "lgw"):
                return await reply(message, 
                    "Please wait a few seconds before using this command again."
                )

            if not message.mentions:
                return await reply(message, 
                    "Usage: `lgw @winner <inventory number>`"
                )

            winner_user = message.mentions[0]

            if winner_user.bot:
                return await reply(message, 
                    "You can't give cards to bots."
                )

            if winner_user.id == message.author.id:
                return await reply(message, 
                    "You can't give cards to yourself."
                )

            parts = message.content.split()

            try:
                requested_num = int(parts[-1])
            except:
                return await reply(message, 
                    "Please provide a valid inventory number."
                )

            luka_inv = get_inventory(SYSTEM_RECOVERY_USER)

            # Same newest-first display-number -> index conversion as lc
            # (SYSTEM_RECOVERY_USER is the exact inventory `lc @Luka`
            # browses), so a number copied straight from `lc @Luka`
            # always refers to the same card here.
            card_index = len(luka_inv) - requested_num

            if card_index < 0 or card_index >= len(luka_inv):
                return await reply(message, 
                    "Invalid inventory number. Use `lc @Luka` to see Luka's current cards."
                )

            owned_card = luka_inv[card_index]

            # The Member representation of the bot's own account ("Luka")
            # in this guild -- used purely for GiftView's display (name,
            # avatar, mention), the same way message.author is for lgift.
            luka_member = message.guild.me or self.user

            view = GiftView(
                luka_member,
                winner_user,
                owned_card,
                SYSTEM_RECOVERY_USER,
                winner_user.id,
                card_index
            )

            gift_embed, file = view.build_embed(luka_member)

            # If there's an image, attach it when sending
            if file:
                gift_message = await message.channel.send(
                    content=f"{luka_member.mention} is gifting {winner_user.mention} a card!",
                    embed=gift_embed,
                    file=file,
                    view=view
                )
                try:
                    os.remove(file.fp.name)
                except:
                    pass
            else:
                gift_message = await message.channel.send(
                    content=f"{luka_member.mention} is gifting {winner_user.mention} a card!",
                    embed=gift_embed,
                    view=view
                )

            view.message = gift_message

            return

        # =========================
        # LGIVE COMMAND (owner-only: grant bonus drops/claims)
        # =========================
        if content_lower.startswith("lgive "):
            # Explicit user-ID allowlist, never a role or username check.
            if message.author.id not in OWNER_USER_IDS:
                return

            parts = message.content.split()

            # `lgive reset` -- resets EVERY user's banked extra
            # drops/claims back to 0. Checked before the normal
            # usage-length guard below since this form only ever has 2
            # parts ("lgive" "reset"). Uses the exact same bonus storage
            # (duo["bonus"]) and duo persistence (duo_lock/
            # save_duo_local/mark_duo_dirty) as add_bonus/consume_bonus
            # above -- no separate storage, no changes to cooldowns,
            # inventories, or anything else.
            if len(parts) >= 2 and parts[1].lower() == "reset":
                async with duo_lock:
                    # Deep-copy snapshot of the ENTIRE bonus dict so a
                    # failed save can restore it exactly as it was,
                    # rather than leaving some users reset and others
                    # not -- atomic across all users, not just per-user.
                    previous_bonus = copy.deepcopy(duo["bonus"])

                    reset_count = 0
                    for user_bonus in duo["bonus"].values():
                        if user_bonus.get("drop", 0) or user_bonus.get("claim", 0):
                            reset_count += 1
                        user_bonus["drop"] = 0
                        user_bonus["claim"] = 0

                    try:
                        save_duo_local()
                        mark_duo_dirty()
                    except Exception:
                        duo["bonus"] = previous_bonus
                        return await reply(message,
                            "❌ Failed to save the reset. No extra drops/claims were changed."
                        )

                return await reply(message,
                    f"✅ Reset extra drops and extra claims to **0** for all users "
                    f"({reset_count} user(s) had a nonzero balance)."
                )

            if len(parts) < 4:
                return await reply(message, 
                    "Usage: `lgive @everyone <amount> <drops|claims>`\n"
                    "Also works with `lgive @role <amount> <drops|claims>` and `lgive reset`."
                )

            target_token = parts[1]

            # Resolve WHO this grant applies to. Two forms, checked in
            # this order (none of their patterns overlap, so order only
            # matters for which error message a malformed target gets):
            #   1. literal "@everyone" -- checked against the raw text,
            #      not message.mention_everyone, since Discord leaves
            #      "@everyone" in message.content as-is even when the
            #      author lacks permission to actually ping everyone.
            #   2. an actual role mention (<@&id>) -- message.role_mentions.
            # (Single-user targeting -- via user mention or raw user ID --
            # has been intentionally removed from `lgive`; only batch
            # grants to @everyone or a role remain.)
            # In every case bots are filtered out entirely (never
            # granted to), and each resulting member is still subject
            # to the exact same amount below -- "per recipient", not
            # split across the group.
            target_members = None
            target_label = None
            bots_skipped = 0

            if target_token.lower() == "@everyone":
                if not message.guild:
                    return await reply(message, "This can only be used in a server.")
                all_members = message.guild.members
                target_members = [m for m in all_members if not m.bot]
                bots_skipped = len(all_members) - len(target_members)
                target_label = "@everyone"

            elif message.role_mentions:
                role = message.role_mentions[0]
                target_members = [m for m in role.members if not m.bot]
                bots_skipped = len(role.members) - len(target_members)
                target_label = f"the **{role.name}** role"

            if target_members is None:
                return await reply(message, 
                    "Could not find that role or `@everyone`. "
                    "Use a role mention or `@everyone`."
                )

            if not target_members:
                return await reply(message, 
                    "No eligible (non-bot) members found for that target."
                )

            try:
                amount = int(parts[2])
            except ValueError:
                return await reply(message, 
                    "Please provide a valid whole number amount (1-10)."
                )

            if amount < 1 or amount > 10:
                return await reply(message, 
                    "Amount must be between 1 and 10."
                )

            kind_token = parts[3].lower().rstrip("s")
            if kind_token not in ("drop", "claim"):
                return await reply(message, 
                    "Please specify `drops` or `claims`."
                )

            # Reuses the EXACT existing bonus drop/claim system (see
            # add_bonus/consume_bonus and their use in `ld` and the claim
            # button) -- no separate reward mechanism, no changes to
            # normal cooldown/drop/claim behavior for anyone. A granted
            # bonus is only ever spent the next time that specific user
            # would otherwise be on cooldown, exactly like a Duo bonus.
            #
            # Every resolved member is granted under this SAME lock
            # acquisition and saved with ONE save_duo_local() call, so a
            # large @everyone/role batch either fully lands or fully
            # rolls back together on failure, rather than saving after
            # every single member (which could leave a batch half-applied
            # if something failed partway through).
            async with duo_lock:
                for member in target_members:
                    add_bonus(member.id, kind_token, amount)
                try:
                    save_duo_local()
                    mark_duo_dirty()
                except Exception:
                    for member in target_members:
                        add_bonus(member.id, kind_token, -amount)
                    return await reply(message, 
                        "❌ Failed to save. Please try again."
                    )

            kind_label = "claim" if kind_token == "claim" else "drop"
            plural = "s" if amount != 1 else ""

            if len(target_members) == 1:
                return await reply(message, 
                    f"✅ Gave {target_members[0].mention} **{amount}** extra {kind_label}{plural}."
                )

            skipped_note = f" (skipped {bots_skipped} bot{'s' if bots_skipped != 1 else ''})" if bots_skipped else ""
            return await reply(message, 
                f"✅ Gave **{amount}** extra {kind_label}{plural} to **{len(target_members)}** "
                f"member(s) in {target_label}{skipped_note}."
            )

        # =========================
        # LBACKUP COMMAND (owner-only: force an immediate GitHub backup)
        # =========================
        # Every persistent JSON store this bot maintains -- filename,
        # snapshot-to-bytes helper, and the lock that already guards it
        # in its own periodic GitHub sync loop. Kept as one explicit list
        # (rather than a scan of globals()) so adding a new persistent
        # store later is a one-line addition here, matching the same
        # explicit, unrolled style already used in on_ready()/close()
        # for registering/flushing each store.
        if content_lower == "lbackup":
            if message.author.id not in OWNER_USER_IDS:
                return

            # Snapshot every store's CURRENT local/in-memory state, each
            # under its own lock, using the exact same _*_json_bytes()
            # helper its own periodic sync loop already calls -- so this
            # reuses the existing per-file serialization exactly, rather
            # than reading raw files off disk (which could race a
            # mutation that hasn't hit disk yet) or inventing a second
            # snapshot mechanism. The dirty flag is cleared in the SAME
            # lock scope as the snapshot -- exactly like every
            # inventory_github_sync_loop-style loop already does -- so a
            # mutation that lands after this snapshot is taken correctly
            # stays (or becomes) dirty for the next periodic cycle,
            # instead of being silently marked "synced" when it wasn't
            # actually included in this commit.
            global _inventories_dirty, _showcase_votes_dirty, _pending_recovery_dirty
            global _mail_dirty, _duo_dirty, _merchants_dirty, _version_system_dirty
            global _backup_status_dirty

            backup_files = {}

            async with inventories_lock:
                backup_files["inventories.json"] = _inventories_json_bytes()
                _inventories_dirty = False

            async with showcase_votes_lock:
                backup_files["showcase_votes.json"] = _showcase_votes_json_bytes()
                _showcase_votes_dirty = False

            async with pending_recovery_lock:
                backup_files["pending_recovery.json"] = _pending_recovery_json_bytes()
                _pending_recovery_dirty = False

            async with mail_lock:
                backup_files["mail.json"] = _mail_json_bytes()
                _mail_dirty = False

            async with duo_lock:
                backup_files["duo.json"] = _duo_json_bytes()
                _duo_dirty = False

            async with merchants_lock:
                backup_files["merchants.json"] = _merchants_json_bytes()
                _merchants_dirty = False

            async with version_system_lock:
                backup_files["version_system.json"] = _version_system_json_bytes()
                _version_system_dirty = False

            # Tentatively stamp backup_status.json with THIS attempt's
            # timestamp and fold it into the SAME commit as the 7 files
            # above -- one atomic commit, not a separate follow-up one,
            # so the timestamp can never end up out of sync with the
            # data it describes. old_backup_status_snapshot/old_dirty
            # are kept so a failed commit below can put backup_status
            # back exactly how it was, in memory AND on the next
            # periodic/shutdown sync -- "do NOT update the timestamp"
            # on failure means the in-memory value too, not just the
            # file.
            old_backup_status_snapshot = dict(backup_status)
            old_backup_status_dirty = _backup_status_dirty
            attempted_backup_timestamp = time.time()

            async with backup_status_lock:
                backup_status["last_successful_backup_at"] = attempted_backup_timestamp
                backup_files["backup_status.json"] = _backup_status_json_bytes()
                _backup_status_dirty = False

            # Reuses the exact same commit machinery every periodic sync
            # loop and the shutdown flush already use -- one atomic
            # commit containing all seven data files PLUS
            # backup_status.json, instead of a second backup system.
            # Current LOCAL data is what was just snapshotted above;
            # nothing here ever downloads from or otherwise consults
            # GitHub, so an older remote copy can never overwrite a
            # newer local one.
            try:
                await github_commit_files(backup_files, "Manual backup (lbackup)")
            except Exception:
                print("[lbackup] Manual backup failed:")
                traceback.print_exc()

                # Restore every dirty flag so the normal periodic loops
                # (and the shutdown flush) still retry each file on
                # their own schedule -- exactly the same on-failure
                # restore every individual flush_*_to_github() already
                # does.
                async with inventories_lock:
                    _inventories_dirty = True
                async with showcase_votes_lock:
                    _showcase_votes_dirty = True
                async with pending_recovery_lock:
                    _pending_recovery_dirty = True
                async with mail_lock:
                    _mail_dirty = True
                async with duo_lock:
                    _duo_dirty = True
                async with merchants_lock:
                    _merchants_dirty = True
                async with version_system_lock:
                    _version_system_dirty = True

                # The commit never went through, so this attempt did
                # NOT successfully back anything up -- revert
                # backup_status to exactly what it was before this
                # command ran (local file, dirty flag, and in-memory
                # dict alike), instead of leaving a timestamp that
                # claims a backup happened when it didn't.
                async with backup_status_lock:
                    backup_status.clear()
                    backup_status.update(old_backup_status_snapshot)
                    _backup_status_dirty = old_backup_status_dirty
                    try:
                        save_backup_status_local()
                    except Exception:
                        print("[backup_status] Failed to revert backup_status.json locally after a failed lbackup:")
                        traceback.print_exc()

                return await reply(message,
                    "❌ Backup failed -- the GitHub commit did not go through. "
                    "Local data is untouched; nothing was lost. Check the logs for details."
                )

            # Commit succeeded -- persist backup_status.json's new value
            # locally too (it's already committed to GitHub as part of
            # the commit above; this just keeps the local file/disk copy
            # in sync with it, same as save_*_local() does for every
            # other store right after its own successful commit path).
            try:
                save_backup_status_local()
            except Exception:
                print("[backup_status] Backup succeeded on GitHub, but failed to save backup_status.json locally:")
                traceback.print_exc()

            file_list = "\n".join(f"• `{name}`" for name in backup_files.keys())
            embed = discord.Embed(
                color=discord.Color.green(),
                title="✅ Backup Complete",
                description=f"Successfully committed **{len(backup_files)}** file(s) to GitHub using current local data."
            )
            embed.add_field(name="Files Backed Up", value=file_list, inline=False)
            embed.set_footer(text=f"Backed up at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time()))} UTC")
            return await reply(message, embed=embed)

        # =========================
        # LBACKUPSTATUS COMMAND (owner-only: read-only backup/sync status)
        # =========================
        if content_lower == "lbackupstatus":
            if message.author.id not in OWNER_USER_IDS:
                return

            # (filename, dirty-flag value, upload-in-progress value,
            # sync-task handle, loader function) for every persistent
            # store -- read-only, nothing here is written or mutated.
            # The loaders reused below are the exact same
            # _load_*_json() functions each store's own startup sync
            # already calls to decide "valid or not"; calling them here
            # again is safe/idempotent since they only read from disk.
            stores = [
                ("inventories.json", _inventories_dirty, _inventory_upload_in_progress, _inventory_sync_task, _load_inventories_json),
                ("showcase_votes.json", _showcase_votes_dirty, _showcase_votes_upload_in_progress, _showcase_votes_sync_task, _load_showcase_votes_json),
                ("pending_recovery.json", _pending_recovery_dirty, _pending_recovery_upload_in_progress, _pending_recovery_sync_task, _load_pending_recovery_json),
                ("mail.json", _mail_dirty, _mail_upload_in_progress, _mail_sync_task, _load_mail_json),
                ("duo.json", _duo_dirty, _duo_upload_in_progress, _duo_sync_task, _load_duo_json),
                ("merchants.json", _merchants_dirty, _merchants_upload_in_progress, _merchants_sync_task, _load_merchants_json),
                ("version_system.json", _version_system_dirty, _version_system_upload_in_progress, _version_system_sync_task, _load_version_system_json),
                ("backup_status.json", _backup_status_dirty, _backup_status_upload_in_progress, _backup_status_sync_task, _load_backup_status_json),
            ]

            exists_lines = []
            dirty_lines = []
            in_progress_lines = []
            unhealthy_lines = []
            loops_running = 0

            for filename, is_dirty, in_progress, sync_task, loader in stores:
                exists = os.path.exists(filename)
                exists_lines.append(f"{'✅' if exists else '❌'} `{filename}`")

                if is_dirty:
                    dirty_lines.append(f"• `{filename}`")

                if in_progress:
                    in_progress_lines.append(f"• `{filename}`")

                if sync_task is not None and not sync_task.done():
                    loops_running += 1

                # A store is "healthy" here if its own loader considers
                # the local file valid (exists, parses, right shape) --
                # same definition of valid/invalid the startup sync
                # already uses, just called read-only after the fact.
                if exists and loader() is None:
                    unhealthy_lines.append(f"• `{filename}` -- exists but failed local validation")
                elif not exists:
                    unhealthy_lines.append(f"• `{filename}` -- missing locally")

            any_dirty = bool(dirty_lines)
            any_in_progress = bool(in_progress_lines)
            all_healthy = not unhealthy_lines

            embed = discord.Embed(
                color=discord.Color.green() if (all_healthy and not any_in_progress) else discord.Color.orange(),
                title="💾 Backup/Sync Status",
                description="Read-only snapshot of persistence state. Nothing here is modified."
            )
            embed.add_field(name="📁 Local Files", value="\n".join(exists_lines), inline=False)
            embed.add_field(
                name="🔄 Pending (Dirty) Changes",
                value="\n".join(dirty_lines) if any_dirty else "✅ None -- everything is synced.",
                inline=False
            )
            embed.add_field(
                name="⏳ GitHub Sync In Progress",
                value="\n".join(in_progress_lines) if any_in_progress else "✅ No sync currently running.",
                inline=False
            )
            embed.add_field(
                name="🧵 Background Sync Loops",
                value=f"{loops_running}/{len(stores)} running",
                inline=True
            )
            last_backup_at = backup_status.get("last_successful_backup_at")
            if last_backup_at:
                last_backup_value = (
                    f"<t:{int(last_backup_at)}:R> "
                    f"(<t:{int(last_backup_at)}:F>)"
                )
            else:
                last_backup_value = "⚠️ No successful `lbackup` has been recorded yet."

            embed.add_field(
                name="🕐 Last Successful Backup",
                value=last_backup_value,
                inline=False
            )
            embed.add_field(
                name="🩺 File Health",
                value="\n".join(unhealthy_lines) if unhealthy_lines else "✅ All persistent files appear healthy.",
                inline=False
            )
            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time()))} UTC")

            return await reply(message, embed=embed)

        # =========================
        # LSETDATE COMMAND (owner-only: additional release-date gate on a version)
        # =========================
        # Persists into version_system.json's "scheduled_unlocks" dict
        # (reusing that store's exact existing local-first/GitHub-sync
        # pipeline -- no new file/sync loop needed). See
        # _card_is_eligible_to_drop() for how this is actually applied:
        # an ADDITIONAL requirement on top of the normal per-character
        # claim threshold, never a replacement/bypass for it -- a
        # version only drops once BOTH are satisfied.
        if content_lower.startswith("lsetdate "):
            if message.author.id not in OWNER_USER_IDS:
                return

            parts = message.content.split()
            if len(parts) < 3:
                return await reply(message,
                    "Usage: `lsetdate <version> <amount unit>`, `lsetdate <version> now`, or `lsetdate <version> clear`\n"
                    "e.g. `lsetdate V2 5 days`, `lsetdate V3 12 hours`, `lsetdate V2 now`, `lsetdate V2 clear`."
                )

            version_token = _normalize_version_token(parts[1])
            if version_token is None:
                return await reply(message,
                    "Invalid version. Use `common`, `rare`, or a numbered version like `V1`, `V2`, `V3`, ..."
                )

            action_str = " ".join(parts[2:]).strip()

            if action_str.lower() == "clear":
                async with version_system_lock:
                    scheduled = version_system.setdefault("scheduled_unlocks", {})
                    if version_token not in scheduled:
                        return await reply(message, f"**{version_token}** doesn't have a scheduled release date set.")
                    previous_value = scheduled.pop(version_token)
                    try:
                        save_version_system_local()
                        mark_version_system_dirty()
                    except Exception:
                        scheduled[version_token] = previous_value
                        return await reply(message, "❌ Something went wrong saving that. Please try again.")
                return await reply(message,
                    f"✅ Cleared the scheduled release date for **{version_token}** -- "
                    f"it now follows the normal claim-based unlock only."
                )

            if action_str.lower() == "now":
                unlock_at = time.time()
            else:
                unlock_at = _parse_relative_unlock_time(action_str)
                if unlock_at is None:
                    return await reply(message,
                        "Couldn't parse that time. Use e.g. `5 days`, `12 hours`, `30 minutes`, `1 week`, `now`, or `clear`."
                    )

            async with version_system_lock:
                scheduled = version_system.setdefault("scheduled_unlocks", {})
                had_previous = version_token in scheduled
                previous_value = scheduled.get(version_token)
                scheduled[version_token] = unlock_at
                try:
                    save_version_system_local()
                    mark_version_system_dirty()
                except Exception:
                    if had_previous:
                        scheduled[version_token] = previous_value
                    else:
                        scheduled.pop(version_token, None)
                    return await reply(message, "❌ Something went wrong saving that. Please try again.")

            if unlock_at <= time.time():
                when_text = "immediately (the date requirement is already satisfied)"
            else:
                when_text = f"<t:{int(unlock_at)}:F> (<t:{int(unlock_at)}:R>)"

            return await reply(message,
                f"✅ **{version_token}** now also requires reaching {when_text} before it can drop, "
                f"**in addition to** its normal claim requirement -- whichever finishes last is what counts."
            )

        # =========================
        # LDATEVERSION COMMAND (owner-only: read-only unlock schedule)
        # =========================
        if content_lower == "ldateversion":
            if message.author.id not in OWNER_USER_IDS:
                return

            scheduled = version_system.get("scheduled_unlocks", {}) or {}
            now = time.time()

            embed = discord.Embed(
                color=discord.Color.blurple(),
                title="📅 Version Release-Date Schedule",
                description=(
                    "Read-only snapshot of `lsetdate` release-date requirements. "
                    "A version still also needs its normal claim threshold met -- "
                    "this only shows the date half of that. Nothing here is modified."
                )
            )

            if not scheduled:
                embed.add_field(
                    name="No scheduled release dates",
                    value="Use `lsetdate <version> <amount unit>` to schedule one.",
                    inline=False
                )
            else:
                lines = []
                for version_token in sorted(scheduled.keys(), key=_schedule_version_sort_key):
                    unlock_at = scheduled[version_token]
                    if unlock_at <= now:
                        status = "✅ Date requirement met"
                    else:
                        status = f"<t:{int(unlock_at)}:F> (<t:{int(unlock_at)}:R>)"
                    lines.append(f"**{version_token}** -- {status}")
                embed.add_field(name="Scheduled Versions", value="\n".join(lines), inline=False)

            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time()))} UTC")
            return await reply(message, embed=embed)

        # =========================
        # LMERCHANTSTATUS COMMAND (owner-only: read-only merchant status)
        # =========================
        # Purely reads the existing `merchants` state -- never calls
        # check_and_update_merchants() or any other function that could
        # regenerate/mutate/persist merchant state. `lmerchants` remains
        # the only command that ever changes it.
        if content_lower == "lmerchantstatus":
            if message.author.id not in OWNER_USER_IDS:
                return

            now = time.time()
            active_merchants = get_active_merchants()

            embed = discord.Embed(
                color=discord.Color.green() if active_merchants else discord.Color.red(),
                title="🛒 Merchant Status",
                description="Read-only snapshot of merchant availability. Nothing here is modified."
            )

            if active_merchants:
                soonest_expiry = min(m.get("expires_ts", now) for m in active_merchants)
                embed.add_field(name="Status", value=f"🟢 Active -- {len(active_merchants)} merchant(s) trading", inline=False)
                embed.add_field(
                    name="Time Remaining",
                    value=f"<t:{int(soonest_expiry)}:R> (<t:{int(soonest_expiry)}:F>)",
                    inline=False
                )
            else:
                next_generation_at = merchants.get("next_generation_at")
                embed.add_field(name="Status", value="🔴 Inactive -- no merchants currently trading", inline=False)
                if next_generation_at:
                    embed.add_field(
                        name="Merchants Return",
                        value=f"<t:{int(next_generation_at)}:R> (<t:{int(next_generation_at)}:F>)",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="Merchants Return",
                        value="⏳ Unknown -- pending the next scheduled check.",
                        inline=False
                    )

            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time()))} UTC")
            return await reply(message, embed=embed)

        # =========================
        # LMAINTENANCE COMMAND (owner-only: persistent maintenance mode)
        # =========================
        if content_lower == "lmaintenance start" or content_lower == "lmaintenance end":
            if message.author.id not in OWNER_USER_IDS:
                return

            turning_on = content_lower.endswith("start")

            async with maintenance_lock:
                previous_state = dict(maintenance)
                maintenance["active"] = turning_on
                maintenance["since"] = time.time()
                try:
                    save_maintenance_local()
                    mark_maintenance_dirty()
                except Exception:
                    maintenance.clear()
                    maintenance.update(previous_state)
                    return await reply(message, "❌ Something went wrong saving that. Please try again.")

            if turning_on:
                return await reply(message,
                    "🛠️ Maintenance mode is now **ON**. Normal commands are blocked for everyone except owners."
                )
            else:
                return await reply(message, "✅ Maintenance mode is now **OFF**. Normal commands are available again.")

        if content_lower == "lmaintenance":
            if message.author.id not in OWNER_USER_IDS:
                return

            status_text = "🛠️ **ON**" if maintenance.get("active") else "✅ **OFF**"
            return await reply(message,
                f"Maintenance mode is currently {status_text}. Usage: `lmaintenance start` / `lmaintenance end`."
            )

        # =========================
        # LFIXUSER COMMAND (owner-only: diagnostic view of one user's data)
        # =========================
        # Diagnostic-only for now, deliberately -- no repair actions are
        # implemented yet (per spec). Structured as "resolve target ->
        # gather read-only diagnostics -> render embed" specifically so
        # a future revision can add actual repair actions (e.g. buttons
        # on this same embed, or a confirmation view) without needing to
        # rewrite the target-resolution or data-gathering parts.
        if content_lower.startswith("lfixuser"):
            if message.author.id not in OWNER_USER_IDS:
                return

            parts = message.content.split()
            if len(parts) < 2:
                return await reply(message,
                    "Usage: `lfixuser @user` or `lfixuser <user_id>`"
                )

            # Same explicit mention-first, then raw-ID resolution as
            # `lgive` above -- deliberately NOT resolve_target_user()
            # (which falls back to the command author when nothing
            # matches), since silently inspecting the owner's own data
            # because a target was mistyped would be the wrong failure
            # mode for a repair tool.
            target = None
            if message.mentions:
                target = message.mentions[0]
            elif parts[1].isdigit() and message.guild:
                candidate_id = int(parts[1])
                target = message.guild.get_member(candidate_id)
                if target is None:
                    try:
                        target = await message.guild.fetch_member(candidate_id)
                    except (discord.NotFound, discord.HTTPException):
                        target = None

            if target is None:
                return await reply(message,
                    "Could not find that user. Use a mention or a valid Discord user ID."
                )

            target_key = str(target.id)

            # =========================
            # REPAIR PASS -- diagnose first, then apply only fixes that
            # are already fully supported by existing state/helpers.
            # Nothing here invents new state or touches unrelated data
            # (mail, bonus, cooldowns, merchants, etc. below are read-only,
            # same as before).
            # =========================
            issues = []

            # 1. Stuck / incorrectly-active trades. force_clear_stuck_trades
            # is the exact same cleanup a normal expiry already performs
            # (see trade_expiration_sweep_loop/TradeView.on_timeout), just
            # triggered manually -- never touches any card that was
            # already actually exchanged, only an in-progress SESSION.
            had_player_trade = user_has_active_trade(target.id)
            had_merchant_trade = user_has_active_merchant_trade(target.id)
            if had_player_trade or had_merchant_trade:
                player_cleared, merchant_cleared = await force_clear_stuck_trades(target.id)
                if player_cleared:
                    issues.append(f"🔧 Was stuck in **{player_cleared}** player trade(s) -- force-cleared.")
                if merchant_cleared:
                    issues.append("🔧 Had an open merchant-trade session -- force-cleared.")

            # 2. Malformed inventory entries -- structurally broken owned-
            # card entries (missing the "card" or "print" a real entry
            # always has, per add_card()/add_recycled_card()) can't be
            # repaired into something valid, only safely dropped. Reads
            # inventories.get() directly (not get_inventory()), so a user
            # with no inventory at all is never given one as a side effect.
            inv = inventories.get(target_key, [])
            malformed_indices = [
                i for i, entry in enumerate(inv)
                if not isinstance(entry, dict) or "card" not in entry or "print" not in entry
            ]

            if malformed_indices:
                async with inventories_lock:
                    live_inv = inventories.get(target_key, [])
                    removed_snapshot = [(i, live_inv[i]) for i in malformed_indices if i < len(live_inv)]
                    for i in sorted(malformed_indices, reverse=True):
                        if i < len(live_inv):
                            live_inv.pop(i)
                    try:
                        save_inventories_local()
                        mark_inventories_dirty()
                        issues.append(f"🔧 Removed **{len(removed_snapshot)}** malformed inventory entrie(s).")
                    except Exception:
                        # Put them back exactly where they were, in
                        # original order, so a failed save can't lose them.
                        for i, entry in sorted(removed_snapshot, key=lambda pair: pair[0]):
                            live_inv.insert(min(i, len(live_inv)), entry)
                        issues.append("⚠️ Found malformed inventory entries, but failed to save the fix -- left unchanged.")

            # ---- Everything below is unchanged: pure diagnostics, all
            # still read-only. ----
            malformed_entries = sum(
                1 for entry in inventories.get(target_key, [])
                if not isinstance(entry, dict) or "card" not in entry or "print" not in entry
            )

            bonus = duo.get("bonus", {}).get(target_key, {"drop": 0, "claim": 0})

            now = time.time()
            if target.id in drop_cooldowns:
                drop_remaining = int(DROP_COOLDOWN - (now - drop_cooldowns[target.id]))
                drop_status = "ready" if drop_remaining <= 0 else f"on cooldown -- {format_time(drop_remaining)} left"
            else:
                drop_status = "ready (no cooldown on record)"

            if target.id in claim_cooldowns:
                claim_remaining = int(CLAIM_COOLDOWN - (now - claim_cooldowns[target.id]))
                claim_status = "ready" if claim_remaining <= 0 else f"on cooldown -- {format_time(claim_remaining)} left"
            else:
                claim_status = "ready (no cooldown on record)"

            mailbox = mail.get(target_key, [])
            unread_count = sum(1 for letter in mailbox if not letter.get("read"))

            recovery_status = "Not pending recovery"
            if target_key in pending_recovery:
                detected_at = pending_recovery[target_key]
                days_elapsed = (now - detected_at) / 86400
                days_left = max(0, RECOVERY_PENDING_DAYS - days_elapsed)
                recovery_status = f"⚠️ Pending recovery -- {days_left:.1f} day(s) until transfer"

            embed = discord.Embed(
                color=discord.Color.green() if not issues else discord.Color.orange(),
                title=f"🔧 User Repair: {target}",
                description="Diagnosed known recoverable issues and applied any safe fixes found. "
                            "Everything below reflects the CURRENT state, after any fixes above."
            )
            embed.add_field(
                name="🛠️ Issues Found & Fixed",
                value="\n".join(issues) if issues else "✅ No known issues found.",
                inline=False
            )
            embed.add_field(name="🆔 User ID", value=f"`{target.id}`", inline=True)
            embed.add_field(name="🎴 Inventory Size", value=str(len(inv)), inline=True)
            embed.add_field(
                name="⚠️ Malformed Inventory Entries",
                value=str(malformed_entries) if malformed_entries else "✅ None",
                inline=True
            )
            embed.add_field(name="🎁 Bonus Drops", value=str(bonus.get("drop", 0)), inline=True)
            embed.add_field(name="🎁 Bonus Claims", value=str(bonus.get("claim", 0)), inline=True)
            embed.add_field(name="⏱️ Drop Cooldown", value=drop_status, inline=False)
            embed.add_field(name="⏱️ Claim Cooldown", value=claim_status, inline=False)
            embed.add_field(
                name="📬 Mail",
                value=f"{len(mailbox)} total, {unread_count} unread",
                inline=True
            )
            embed.add_field(name="♻️ Recovery Status", value=recovery_status, inline=False)
            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time()))} UTC")

            return await reply(message, embed=embed)

        # =========================
        # LRESETINVENTORIES COMMAND (owner-only, dangerous, confirm-gated)
        # =========================
        # Trims every member's inventory down to a max card count, always
        # keeping every pinned AND every tagged card (both count toward
        # the max), filling remaining slots randomly from the rest.
        # Removed cards are never deleted -- they're moved into the
        # SAME recovery pool `lrecyclecards` already reads from
        # (pending_recovery[RECYCLABLE_CARDS_KEY], added as *inactive*
        # entries -- see that command and get_recyclable_pool() above).
        if content_lower.startswith("lresetinventories"):
            if message.author.id not in OWNER_USER_IDS:
                return

            parts = message.content.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return await reply(message, "Usage: `lresetinventories <max cards to keep>` (e.g. `lresetinventories 20`)")

            keep_max = int(parts[1])
            if keep_max < 1:
                return await reply(message, "The amount to keep must be at least 1.")

            if not message.guild:
                return await reply(message, "This can only be used in a server.")

            confirm_embed = discord.Embed(
                color=discord.Color.red(),
                title="⚠️ Confirm Mass Inventory Reset",
                description=(
                    f"This will trim **every member's** inventory down to **{keep_max}** cards.\n\n"
                    "For anyone with more than that:\n"
                    f"• ALL pinned cards and ALL tagged cards are kept (they count toward the {keep_max}), "
                    "**unless** someone has more protected cards than that -- in that case exactly "
                    f"**{keep_max}** of their protected cards are randomly kept instead, and everything else "
                    "(including the rest of their protected cards) is removed like normal.\n"
                    "• Otherwise, remaining slots are filled with a random selection of their other cards.\n"
                    "• Everything else is removed and moved into the recovery pool "
                    "(not deleted, not yet recyclable -- see `lrecyclecards`).\n\n"
                    "**This cannot be undone from Discord. Proceed?**"
                )
            )

            async def do_reset(interaction: discord.Interaction):
                now = time.time()
                # Snapshots taken BEFORE any mutation, so a failed save
                # at either step below can restore both stores to
                # exactly how they were -- nothing partially applied.
                inventory_snapshot = {}
                pool_snapshot = copy.deepcopy(pending_recovery.get(RECYCLABLE_CARDS_KEY, []))

                affected_count = 0
                kept_count = 0
                removed_count = 0

                for member in message.guild.members:
                    if member.bot:
                        continue
                    key = str(member.id)
                    inv = inventories.get(key, [])
                    if len(inv) <= keep_max:
                        continue

                    protected_indices = [i for i, oc in enumerate(inv) if oc.get("pinned") or oc.get("tags")]

                    if len(protected_indices) > keep_max:
                        # More protected (pinned/tagged) cards than the
                        # keep limit allows -- randomly keep exactly
                        # `keep_max` of the PROTECTED cards themselves
                        # (never guessing based on unprotected ones,
                        # since there aren't enough protected slots to
                        # keep everything protected anyway). Every other
                        # card -- the remaining protected ones AND every
                        # unprotected one -- is removed into the
                        # recovery pool via the exact same path below.
                        # This user is no longer skipped/reported;
                        # everyone with more than `keep_max` total cards
                        # is now always actually trimmed.
                        keep_set = set(random.sample(protected_indices, keep_max))
                    else:
                        protected_set = set(protected_indices)
                        unprotected_indices = [i for i in range(len(inv)) if i not in protected_set]
                        needed = keep_max - len(protected_indices)
                        chosen_random = set(random.sample(unprotected_indices, needed)) if needed > 0 else set()
                        keep_set = protected_set | chosen_random

                    removed_entries = [inv[i] for i in range(len(inv)) if i not in keep_set]
                    new_inv = [inv[i] for i in range(len(inv)) if i in keep_set]

                    inventory_snapshot[key] = copy.deepcopy(inv)
                    inventories[key] = new_inv

                    for owned_card in removed_entries:
                        card = owned_card.get("card", {})
                        pending_recovery.setdefault(RECYCLABLE_CARDS_KEY, []).append({
                            "id": str(uuid.uuid4()),
                            "card_id": card.get("id"),
                            "print": owned_card.get("print"),
                            "card": card,
                            "removed_from": key,
                            "removed_at": now,
                            "recycled_active": False,
                        })

                    affected_count += 1
                    kept_count += len(new_inv)
                    removed_count += len(removed_entries)

                if affected_count == 0:
                    return await interaction.followup.send(
                        "No members had more than the threshold -- nothing to do."
                    )

                def _restore():
                    for key, original in inventory_snapshot.items():
                        inventories[key] = original
                    pending_recovery[RECYCLABLE_CARDS_KEY] = pool_snapshot

                async with inventories_lock:
                    try:
                        save_inventories_local()
                        mark_inventories_dirty()
                    except Exception:
                        _restore()
                        traceback.print_exc()
                        return await interaction.followup.send(
                            "❌ Failed to save the inventory changes. No changes were made."
                        )

                async with pending_recovery_lock:
                    try:
                        save_pending_recovery_local()
                        mark_pending_recovery_dirty()
                    except Exception:
                        traceback.print_exc()
                        _restore()
                        # inventories.json was already written with the NEW
                        # state in the block above -- since the matching
                        # recovery-pool entries failed to save, that write
                        # must be undone too, or the removed cards would be
                        # lost for good instead of merely staying put.
                        async with inventories_lock:
                            try:
                                save_inventories_local()
                                mark_inventories_dirty()
                            except Exception:
                                print("[lresetinventories] CRITICAL: failed to re-save inventories.json "
                                      "after rolling back -- in-memory state IS rolled back, but the "
                                      "on-disk file may still reflect the failed reset until the next "
                                      "successful sync.")
                                traceback.print_exc()
                        return await interaction.followup.send(
                            "❌ Failed to save the recovery-pool changes. Rolled back -- no changes were kept."
                        )

                result_embed = discord.Embed(
                    color=discord.Color.green(),
                    title="✅ Mass Inventory Reset Complete",
                    description=(
                        f"**{affected_count}** member(s) trimmed to **{keep_max}** cards.\n"
                        f"**{removed_count}** card(s) removed and moved into the recovery pool.\n"
                        f"**{kept_count}** card(s) kept in total."
                    )
                )
                await interaction.followup.send(embed=result_embed)

            view = OwnerConfirmView(message.author.id, do_reset)
            sent = await reply(message, embed=confirm_embed, view=view)
            view.message = sent
            return

        # =========================
        # LRECYCLECARDS COMMAND (owner-only, dangerous, confirm-gated)
        # =========================
        # Activates cards that are currently sitting INACTIVE in the
        # recovery pool (put there by `lresetinventories`) so they start
        # competing for drops again via get_weighted_card(), at their
        # original print number. Never reads from inventories or Luka's
        # own ("__system__") inventory directly -- only ever from
        # pending_recovery[RECYCLABLE_CARDS_KEY].
        if content_lower.startswith("lrecyclecards"):
            if message.author.id not in OWNER_USER_IDS:
                return

            parts = message.content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lrecyclecards <amount>` or `lrecyclecards all`")

            arg = parts[1].lower()

            inactive_entries = [e for e in get_recyclable_pool() if not e.get("recycled_active")]
            if not inactive_entries:
                return await reply(message, "There are no cards currently pending in the recovery pool to recycle.")

            if arg == "all":
                amount = len(inactive_entries)
            elif arg.isdigit():
                amount = int(arg)
                if amount < 1:
                    return await reply(message, "Amount must be at least 1.")
                amount = min(amount, len(inactive_entries))
            else:
                return await reply(message, "Usage: `lrecyclecards <amount>` or `lrecyclecards all`")

            chosen = random.sample(inactive_entries, amount)
            chosen_ids = {e.get("id") for e in chosen}

            preview_lines = [
                f"• {e.get('card', {}).get('name', 'Unknown')} -- print #{e.get('print')}"
                for e in chosen[:10]
            ]
            more = f"\n...and {amount - 10} more" if amount > 10 else ""

            confirm_embed = discord.Embed(
                color=discord.Color.red(),
                title="⚠️ Confirm Card Recycling",
                description=(
                    f"This will activate **{amount}** card(s) from the recovery pool to drop again, "
                    "at their original print number -- normal print progression and claim/version "
                    "thresholds are unaffected.\n\n"
                    + ("\n".join(preview_lines) + more + "\n\n" if preview_lines else "")
                    + "**Proceed?**"
                )
            )

            async def do_recycle(interaction: discord.Interaction):
                async with pending_recovery_lock:
                    live_pool = pending_recovery.get(RECYCLABLE_CARDS_KEY, [])
                    snapshot = copy.deepcopy(live_pool)

                    # Re-checked against the LIVE pool at confirm time, not
                    # the (possibly now-stale) preview above -- an entry
                    # already claimed/activated/removed by someone else in
                    # the meantime is simply skipped, never double-counted.
                    activated = 0
                    for entry in live_pool:
                        if entry.get("id") in chosen_ids and not entry.get("recycled_active"):
                            entry["recycled_active"] = True
                            activated += 1

                    try:
                        save_pending_recovery_local()
                        mark_pending_recovery_dirty()
                    except Exception:
                        pending_recovery[RECYCLABLE_CARDS_KEY] = snapshot
                        traceback.print_exc()
                        return await interaction.followup.send(
                            "❌ Failed to save. No cards were recycled."
                        )

                await interaction.followup.send(
                    f"✅ Activated **{activated}** card(s) for recycling -- they'll now compete for drops "
                    "alongside everything else, at their original print numbers."
                )

            view = OwnerConfirmView(message.author.id, do_recycle)
            sent = await reply(message, embed=confirm_embed, view=view)
            view.message = sent
            return

        # =========================
        # LPENDINGRECOVERY COMMAND (owner-only, read-only)
        # =========================
        # Every card belonging to a user CURRENTLY counting down in
        # pending_recovery -- their cards still live in their own
        # inventory at this stage (see _perform_full_recovery above),
        # nothing has been transferred yet. Purely a read -- never
        # mutates or regenerates anything.
        if content_lower == "lpendingrecovery":
            if message.author.id not in OWNER_USER_IDS:
                return

            entries = []
            now = time.time()
            for user_key, first_detected in pending_recovery.items():
                if user_key == RECYCLABLE_CARDS_KEY or not str(user_key).isdigit():
                    continue

                inv = inventories.get(user_key, [])
                if not inv:
                    continue

                elapsed_days = (now - first_detected) / 86400
                days_left = max(0, RECOVERY_PENDING_DAYS - elapsed_days)
                owner_display = f"<@{user_key}> (`{user_key}`)"

                for owned_card in inv:
                    card = owned_card.get("card", {})
                    entries.append({
                        "description": f"## {card.get('name', 'Unknown Character')}",
                        "fields": [
                            ("Series", card.get("series", "Unknown Series")),
                            ("Stars", stars(card.get("stars", 1))),
                            ("Print", format_print(owned_card.get("print"))),
                            ("Version/Frame", card_version_label(card)),
                            ("Original Owner", owner_display),
                            ("Recovery Status", f"⚠️ {days_left:.1f} day(s) until automatic transfer"),
                        ],
                    })

            if not entries:
                return await reply(message, "No cards are currently in pending recovery.")

            view = AdminCardListView("♻️ Pending Recovery", entries, message.author.id)
            return await reply(message, embed=view.build_embed(), view=view)

        # =========================
        # LRECYCLABLEPOOL COMMAND (owner-only, read-only)
        # =========================
        # Completely separate from `lpendingrecovery` above -- that one
        # shows the unrelated left-server-member recovery system; this
        # one shows pending_recovery[RECYCLABLE_CARDS_KEY], the pool
        # `lresetinventories` fills and `lrecyclecards` activates from.
        # Reads get_recyclable_pool() directly -- never mutates or
        # activates anything itself.
        if content_lower == "lrecyclablepool":
            if message.author.id not in OWNER_USER_IDS:
                return

            pool = get_recyclable_pool()
            if not pool:
                return await reply(message, "The recyclable-card pool is currently empty.")

            active_count = sum(1 for e in pool if e.get("recycled_active"))
            inactive_count = len(pool) - active_count

            entries = []
            for entry in pool:
                card = entry.get("card", {})
                removed_from = entry.get("removed_from")
                owner_display = f"<@{removed_from}> (`{removed_from}`)" if removed_from else "Unknown"
                status = "✅ Active -- currently droppable" if entry.get("recycled_active") else "⏳ Inactive -- awaiting `lrecyclecards`"
                entries.append({
                    "description": f"## {card.get('name', 'Unknown Character')}",
                    "fields": [
                        ("Series", card.get("series", "Unknown Series")),
                        ("Stars", stars(card.get("stars", 1))),
                        ("Print", format_print(entry.get("print"))),
                        ("Version/Frame", card_version_label(card)),
                        ("Original Owner", owner_display),
                        ("Status", status),
                    ],
                })

            view = AdminCardListView(
                f"♻️ Recyclable Card Pool -- {len(pool)} total ({active_count} active, {inactive_count} inactive)",
                entries,
                message.author.id
            )
            return await reply(message, embed=view.build_embed(), view=view)

        # =========================
        # LFIXDUPLICATES COMMAND (owner-only, dangerous, confirm-gated)
        # =========================
        # Repairs every duplicate (card_id, print) pair currently found
        # across live inventories and the recyclable pool -- see
        # _compute_duplicate_fix_plan() for the exact rules. Only ever
        # changes the `print` field of the affected entries; never
        # deletes a card, never touches pins/tags/card data/anything
        # else. Recomputes the plan fresh both here (for the preview)
        # and again inside do_fix at confirm time, so nothing stale
        # from the preview is ever blindly replayed against data that's
        # since changed.
        if content_lower == "lfixduplicates":
            if message.author.id not in OWNER_USER_IDS:
                return

            plan = _compute_duplicate_fix_plan()
            total_ops = len(plan["live_ops"]) + len(plan["pool_ops"])

            if total_ops == 0:
                return await reply(message, "✅ No duplicate prints found -- nothing to fix.")

            preview_lines = []
            for op in plan["live_ops"][:10]:
                kind_label = "self-duplicate" if op["kind"] == "self_duplicate" else "cross-user duplicate"
                preview_lines.append(
                    f"• [{kind_label}] `{op['card_id']}` print {op['old_print']} -> {op['new_print']} "
                    f"(user <@{op['user_id']}>, kept by <@{op['kept_user_id']}>)"
                )
            for op in plan["pool_ops"][:max(0, 10 - len(preview_lines))]:
                preview_lines.append(
                    f"• [pool/live overlap] `{op['card_id']}` print {op['old_print']} -> {op['new_print']} (pool entry only)"
                )
            more = f"\n...and {total_ops - len(preview_lines)} more" if total_ops > len(preview_lines) else ""

            confirm_embed = discord.Embed(
                color=discord.Color.red(),
                title="⚠️ Confirm Duplicate Print Repair",
                description=(
                    f"This will fix **{total_ops}** duplicate print(s):\n"
                    f"• **{plan['self_count']}** self-duplicate(s) -- earliest claim keeps the print, "
                    "the later copy gets a fresh one.\n"
                    f"• **{plan['cross_count']}** cross-user duplicate(s) -- same rule, across two different owners.\n"
                    f"• **{plan['overlap_count']}** pool/live overlap(s) -- the live owner's print is left "
                    "alone; only the pool entry is renumbered.\n\n"
                    "Only the `print` value ever changes -- no card is deleted, and pins/tags/card data are "
                    "never touched.\n\n"
                    + ("\n".join(preview_lines) + more + "\n\n" if preview_lines else "")
                    + "**Proceed?**"
                )
            )

            async def do_fix(interaction: discord.Interaction):
                async with inventories_lock:
                    async with pending_recovery_lock:
                        # Recomputed fresh, right before mutating, under
                        # both locks -- guards against anything having
                        # changed since the preview above.
                        fresh_plan = _compute_duplicate_fix_plan()
                        live_ops = fresh_plan["live_ops"]
                        pool_ops = fresh_plan["pool_ops"]
                        fresh_total = len(live_ops) + len(pool_ops)

                        if fresh_total == 0:
                            return await interaction.followup.send(
                                "✅ No duplicates found -- nothing to fix (data changed since this was previewed)."
                            )

                        # Snapshot exactly what will be touched -- only
                        # the specific users' inventories involved, and
                        # the pool as a whole -- so a failed save can
                        # restore everything byte-for-byte, never partial.
                        touched_users = {op["user_id"] for op in live_ops}
                        inventory_snapshot = {uid: copy.deepcopy(inventories.get(uid, [])) for uid in touched_users}
                        pool_snapshot = copy.deepcopy(pending_recovery.get(RECYCLABLE_CARDS_KEY, []))

                        for op in live_ops:
                            owned_card = inventories[op["user_id"]][op["idx"]]
                            owned_card["print"] = op["new_print"]

                        pool_by_id = {e.get("id"): e for e in pending_recovery.get(RECYCLABLE_CARDS_KEY, [])}
                        for op in pool_ops:
                            entry = pool_by_id.get(op["entry_id"])
                            if entry is not None:
                                entry["print"] = op["new_print"]

                        def _restore():
                            for uid, original in inventory_snapshot.items():
                                inventories[uid] = original
                            pending_recovery[RECYCLABLE_CARDS_KEY] = pool_snapshot

                        try:
                            save_inventories_local()
                            mark_inventories_dirty()
                        except Exception:
                            _restore()
                            traceback.print_exc()
                            return await interaction.followup.send(
                                "❌ Failed to save the inventory changes. No changes were made."
                            )

                        try:
                            save_pending_recovery_local()
                            mark_pending_recovery_dirty()
                        except Exception:
                            traceback.print_exc()
                            _restore()
                            # inventories.json was already written with the
                            # NEW state above -- since the matching pool
                            # changes failed to save, that write must be
                            # undone too, or the renumbering would be only
                            # half-applied on disk.
                            try:
                                save_inventories_local()
                                mark_inventories_dirty()
                            except Exception:
                                print("[lfixduplicates] CRITICAL: failed to re-save inventories.json "
                                      "after rolling back -- in-memory state IS rolled back, but the "
                                      "on-disk file may still reflect the failed repair until the next "
                                      "successful sync.")
                                traceback.print_exc()
                            return await interaction.followup.send(
                                "❌ Failed to save the pool changes. Rolled back -- no changes were kept."
                            )

                await interaction.followup.send(
                    f"✅ Fixed **{fresh_total}** duplicate print(s): "
                    f"**{fresh_plan['self_count']}** self-duplicate(s), "
                    f"**{fresh_plan['cross_count']}** cross-user duplicate(s), "
                    f"**{fresh_plan['overlap_count']}** pool/live overlap(s)."
                )

            view = OwnerConfirmView(message.author.id, do_fix)
            sent = await reply(message, embed=confirm_embed, view=view)
            view.message = sent
            return

        # =========================
        # LRECOVER COMMAND (owner-only, dangerous single-card transfer,
        # confirm-gated)
        # =========================
        # Manually pulls ONE exact card out of a still-pending user's
        # inventory early and moves it into Luka's ("__system__")
        # inventory -- same destination/tagging _perform_full_recovery
        # uses (original_owner/recovered_at), same inventories_lock/
        # save_inventories_local()/mark_inventories_dirty() pipeline,
        # no new persistence path. The user's OTHER cards and their
        # pending_recovery countdown are left completely untouched --
        # only this one card entry moves.
        if content_lower.startswith("lrecover "):
            if message.author.id not in OWNER_USER_IDS:
                return

            raw_args = content[len("lrecover"):].strip()
            match = re.match(r"^(.+?)\s*#\s*(\d+)\s*,\s*(\d+)\s*stars?$", raw_args, re.IGNORECASE)
            if not match:
                return await reply(message,
                    "Usage: `lrecover <full card name> #<print>, <stars> stars`\n"
                    "Example: `lrecover Satoru Gojo #5, 2 stars`"
                )

            name = match.group(1).strip()
            print_num = int(match.group(2))
            star_count = int(match.group(3))

            matches = _find_pending_recovery_matches(name, print_num, star_count)

            if not matches:
                return await reply(message,
                    f"No pending-recovery card exactly matches **{name}** #{print_num}, {star_count}★. "
                    "Check `lpendingrecovery` for the exact name/print/stars."
                )

            if len(matches) > 1:
                return await reply(message,
                    f"⚠️ Found **{len(matches)}** pending-recovery cards matching **{name}** #{print_num}, "
                    f"{star_count}★ across different owners -- refusing to guess which one you mean "
                    "(this shouldn't normally happen, since prints are unique per card). "
                    "Please investigate manually."
                )

            owner_key, index, owned_card = matches[0]
            card = owned_card.get("card", {})

            confirm_embed = discord.Embed(
                color=discord.Color.red(),
                title="⚠️ Confirm Manual Recovery",
                description=(
                    f"## {card.get('name', 'Unknown Character')}\n"
                    f"**Series:** {card.get('series', 'Unknown Series')}\n"
                    f"**Stars:** {stars(card.get('stars', 1))}\n"
                    f"**Print:** {format_print(owned_card.get('print'))}\n"
                    f"**Version:** {card_version_label(card)}\n"
                    f"**Current Owner:** <@{owner_key}> (`{owner_key}`, still pending recovery)\n\n"
                    "This will remove this exact card from that user's inventory and move it into "
                    "Luka's inventory, preserving its print number exactly (no new print is assigned). "
                    "The user's other cards and their pending-recovery countdown are untouched.\n\n"
                    "**Proceed?**"
                )
            )

            async def do_recover(interaction: discord.Interaction):
                async with inventories_lock:
                    # Re-resolved against LIVE state at confirm time,
                    # never the (possibly now-stale) match found above --
                    # e.g. the user could have rejoined and traded/lost
                    # this exact card in the meantime.
                    live_matches = _find_pending_recovery_matches(name, print_num, star_count)
                    if len(live_matches) != 1:
                        return await interaction.followup.send(
                            "⚠️ This card no longer exactly matches a single pending-recovery entry "
                            "(it may have already moved) -- no changes were made."
                        )

                    live_owner_key, live_index, _ = live_matches[0]
                    owner_inv = inventories.get(live_owner_key, [])
                    recovered_inv = inventories.setdefault(SYSTEM_RECOVERY_USER, [])

                    owner_snapshot = copy.deepcopy(owner_inv)
                    recovered_snapshot = copy.deepcopy(recovered_inv)

                    removed_card = owner_inv.pop(live_index)
                    tagged_card = dict(
                        removed_card,
                        original_owner=live_owner_key,
                        recovered_at=time.time(),
                    )
                    recovered_inv.append(tagged_card)

                    try:
                        save_inventories_local()
                        mark_inventories_dirty()
                    except Exception:
                        inventories[live_owner_key] = owner_snapshot
                        inventories[SYSTEM_RECOVERY_USER] = recovered_snapshot
                        traceback.print_exc()
                        return await interaction.followup.send(
                            "❌ Failed to save. No changes were made -- the card remains with its "
                            "original (still pending) owner."
                        )

                await interaction.followup.send(
                    f"✅ Recovered **{card.get('name', 'Unknown Character')}** "
                    f"{format_print(removed_card.get('print'))} into Luka's inventory. "
                    f"Original owner (`{live_owner_key}`) is otherwise unaffected."
                )

            view = OwnerConfirmView(message.author.id, do_recover)
            sent = await reply(message, embed=confirm_embed, view=view)
            view.message = sent
            return

        # =========================
        # LLUKAINVENTORY COMMAND (owner-only, read-only)
        # =========================
        if content_lower == "llukainventory":
            if message.author.id not in OWNER_USER_IDS:
                return

            luka_inv = inventories.get(SYSTEM_RECOVERY_USER, [])
            if not luka_inv:
                return await reply(message, "Luka's inventory is currently empty.")

            entries = []
            total = len(luka_inv)
            for i, owned_card in enumerate(luka_inv):
                card = owned_card.get("card", {})
                # Same newest-first numbering `lc @Luka`/`lgw` already use,
                # so a number shown here can be handed straight to `lgw`.
                display_number = total - i

                fields = [
                    ("Series", card.get("series", "Unknown Series")),
                    ("Stars", stars(card.get("stars", 1))),
                    ("Print", format_print(owned_card.get("print"))),
                    ("Version/Frame", card_version_label(card)),
                    ("Inventory #", f"`{display_number}` (use with `lgw`)"),
                ]
                if owned_card.get("original_owner"):
                    fields.append((
                        "Recovered From",
                        f"<@{owned_card['original_owner']}> (`{owned_card['original_owner']}`)"
                    ))
                if owned_card.get("recovered_at"):
                    fields.append(("Recovered At", f"<t:{int(owned_card['recovered_at'])}:R>"))

                entries.append({
                    "description": f"## {card.get('name', 'Unknown Character')}",
                    "fields": fields,
                })

            view = AdminCardListView("🤖 Luka's Inventory", entries, message.author.id)
            return await reply(message, embed=view.build_embed(), view=view)

        # =========================
        # LTAG COMMAND
        # =========================
        if content_lower.startswith("ltag "):
            raw_args = content[5:].strip()
            if not raw_args:
                return await reply(message, "Usage: `ltag <character> <tags>`")

            words = raw_args.split()

            # Leading inventory-number mode: "ltag 17 <tags>" or
            # "ltag 17, 25, 81 <tags>" -- consume as many leading
            # comma-separated-number tokens as match (e.g. "17,", "25,",
            # "81", or a single token like "17,25,81"), then treat
            # everything after as the tag text. Falls through to the
            # existing character-name mode if no leading tokens match.
            number_token = re.compile(r'^\d+(,\d+)*,?$')
            inventory_numbers = []
            consumed = 0
            for w in words:
                if number_token.fullmatch(w):
                    inventory_numbers.extend(int(x) for x in re.findall(r'\d+', w))
                    consumed += 1
                else:
                    break

            if inventory_numbers and consumed < len(words):
                tag_text = " ".join(words[consumed:]).strip()

                if len(tag_text) > 10:
                    return await reply(message, "Tags cannot exceed 10 letter limit.")

                target_indexes = []
                invalid_numbers = []
                for requested_num in inventory_numbers:
                    card_index = len(inv) - requested_num
                    if card_index < 0 or card_index >= len(inv):
                        invalid_numbers.append(requested_num)
                    else:
                        target_indexes.append(card_index)

                if not target_indexes:
                    return await reply(message, "Invalid inventory number(s).")

                async with inventories_lock:
                    previous_tags = {}
                    for i in target_indexes:
                        previous_tags[i] = inv[i].get("tags")
                        inv[i]["tags"] = tag_text

                    try:
                        save_inventories_local()
                        mark_inventories_dirty()
                    except Exception:
                        for i, old_value in previous_tags.items():
                            if old_value is None:
                                inv[i].pop("tags", None)
                            else:
                                inv[i]["tags"] = old_value
                        return await reply(message, 
                            "❌ Something went wrong saving your tags. Please try again."
                        )

                updated = len(target_indexes)
                note = ""
                if invalid_numbers:
                    note = f" (skipped invalid number(s): {', '.join(map(str, invalid_numbers))})"
                return await reply(message, f"Updated {updated} card(s) with {tag_text}.{note}")

            # Character-name mode: "ltag <character> <tags>". Longest-
            # prefix match against character names the user actually
            # owns, so multi-word names work without needing quotes --
            # everything left over after the matched name is the tag
            # text, exactly as typed.
            owned_names = {oc["card"].get("name", "") for oc in inv}

            match_name = None
            tag_text = None
            for word_count in range(len(words), 0, -1):
                candidate = " ".join(words[:word_count])
                found = next((n for n in owned_names if n.lower() == candidate.lower()), None)
                if found:
                    match_name = found
                    tag_text = " ".join(words[word_count:]).strip()
                    break

            if not match_name:
                return await reply(message, 
                    f"You don't own any cards matching **{words[0]}**."
                )
            if not tag_text:
                return await reply(message, "Usage: `ltag <character> <tags>`")

            if len(tag_text) > 10:
                return await reply(message, "Tags cannot exceed 10 letter limit.")

            async with inventories_lock:
                previous_tags = {}
                updated = 0
                for i, owned_card in enumerate(inv):
                    if owned_card["card"].get("name", "") == match_name:
                        previous_tags[i] = owned_card.get("tags")
                        owned_card["tags"] = tag_text
                        updated += 1

                try:
                    save_inventories_local()
                    mark_inventories_dirty()
                except Exception:
                    # Roll back so a failed save never leaves tags
                    # applied only in memory.
                    for i, old_value in previous_tags.items():
                        if old_value is None:
                            inv[i].pop("tags", None)
                        else:
                            inv[i]["tags"] = old_value
                    return await reply(message, 
                        "❌ Something went wrong saving your tags. Please try again."
                    )

            return await reply(message, f"Updated {updated} {match_name} card(s) with {tag_text}.")

        # =========================
        # LUNTAG COMMAND
        # =========================
        if content_lower.startswith("luntag "):
            raw_args = content[7:].strip()
            if not raw_args:
                return await reply(message, "Usage: `luntag <character>` or `luntag <inventory number(s)>`")

            words = raw_args.split()

            # Leading inventory-number mode: "luntag 17" or
            # "luntag 17, 25, 81" -- same comma-aware parsing as ltag's
            # number mode. Only triggers if EVERY word is a numeric
            # token (luntag takes no trailing text like ltag does), so
            # a character name never gets misread as numbers. Falls
            # through to the existing character-name mode otherwise.
            number_token = re.compile(r'^\d+(,\d+)*,?$')
            inventory_numbers = []
            consumed = 0
            for w in words:
                if number_token.fullmatch(w):
                    inventory_numbers.extend(int(x) for x in re.findall(r'\d+', w))
                    consumed += 1
                else:
                    break

            if inventory_numbers and consumed == len(words):
                target_indexes = []
                invalid_numbers = []
                for requested_num in inventory_numbers:
                    card_index = len(inv) - requested_num
                    if card_index < 0 or card_index >= len(inv):
                        invalid_numbers.append(requested_num)
                    else:
                        target_indexes.append(card_index)

                if not target_indexes:
                    return await reply(message, "Invalid inventory number(s).")

                async with inventories_lock:
                    previous_tags = {}
                    updated = 0
                    for i in target_indexes:
                        if "tags" in inv[i]:
                            previous_tags[i] = inv[i]["tags"]
                            del inv[i]["tags"]
                            updated += 1

                    try:
                        save_inventories_local()
                        mark_inventories_dirty()
                    except Exception:
                        for i, old_value in previous_tags.items():
                            inv[i]["tags"] = old_value
                        return await reply(message, 
                            "❌ Something went wrong saving your tags. Please try again."
                        )

                note = ""
                if invalid_numbers:
                    note = f" (skipped invalid number(s): {', '.join(map(str, invalid_numbers))})"
                return await reply(message, f"Updated {updated} card(s).{note}")

            # Character-name mode: "luntag <character>" -- unchanged.
            owned_names = {oc["card"].get("name", "") for oc in inv}
            match_name = next((n for n in owned_names if n.lower() == raw_args.lower()), None)

            if not match_name:
                return await reply(message, f"You don't own any cards matching **{raw_args}**.")

            async with inventories_lock:
                previous_tags = {}
                updated = 0
                for i, owned_card in enumerate(inv):
                    if owned_card["card"].get("name", "") == match_name and "tags" in owned_card:
                        previous_tags[i] = owned_card["tags"]
                        del owned_card["tags"]
                        updated += 1

                try:
                    save_inventories_local()
                    mark_inventories_dirty()
                except Exception:
                    for i, old_value in previous_tags.items():
                        inv[i]["tags"] = old_value
                    return await reply(message, 
                        "❌ Something went wrong saving your tags. Please try again."
                    )

            return await reply(message, f"Updated {updated} {match_name} card(s).")

        # =========================
        # LPIN COMMAND
        # =========================
        if content_lower.startswith("lpin "):
            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lpin <inventory number>`")

            try:
                requested_num = int(parts[1])
            except ValueError:
                return await reply(message, "Please provide a valid inventory number.")

            card_index = len(inv) - requested_num
            if card_index < 0 or card_index >= len(inv):
                return await reply(message, "Invalid inventory number.")

            owned_card = inv[card_index]

            if owned_card.get("pinned"):
                return await reply(message, "That card is already pinned.")

            pinned_count = sum(1 for oc in inv if oc.get("pinned"))
            if pinned_count >= MAX_PINNED_CARDS:
                return await reply(message, 
                    f"You can only pin up to {MAX_PINNED_CARDS} cards. "
                    f"Unpin one first with `lunpin <inventory number>`."
                )

            async with inventories_lock:
                owned_card["pinned"] = True
                try:
                    save_inventories_local()
                    mark_inventories_dirty()
                except Exception:
                    owned_card.pop("pinned", None)
                    return await reply(message, 
                        "❌ Something went wrong saving your pin. Please try again."
                    )

            name = owned_card["card"].get("name", "Unknown")
            return await reply(message, f"📌 Pinned **{name}**.")

        # =========================
        # LUNPIN COMMAND
        # =========================
        if content_lower.startswith("lunpin "):
            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lunpin <inventory number>`")

            try:
                requested_num = int(parts[1])
            except ValueError:
                return await reply(message, "Please provide a valid inventory number.")

            card_index = len(inv) - requested_num
            if card_index < 0 or card_index >= len(inv):
                return await reply(message, "Invalid inventory number.")

            owned_card = inv[card_index]

            if not owned_card.get("pinned"):
                return await reply(message, "That card isn't pinned.")

            async with inventories_lock:
                owned_card["pinned"] = False
                try:
                    save_inventories_local()
                    mark_inventories_dirty()
                except Exception:
                    owned_card["pinned"] = True
                    return await reply(message, 
                        "❌ Something went wrong saving your unpin. Please try again."
                    )

            name = owned_card["card"].get("name", "Unknown")
            return await reply(message, f"Unpinned **{name}**.")

        # =========================
        # LMAIL COMMAND
        # =========================
        if content_lower == "lmail" or content_lower.startswith("lmail "):
            args = content[len("lmail"):].strip()

            # No target given -- open the mailbox (reuses the same
            # prev/next pagination style as `lbadges`, via
            # MailboxPaginationView).
            if not args:
                letters = sorted(
                    get_mailbox(user_id),
                    key=lambda l: l.get("timestamp", 0),
                    reverse=True,
                )
                if not letters:
                    return await reply(message, "Your mailbox is empty.")

                enriched_letters = await _resolve_mail_sender_info(self, letters)
                view = MailboxPaginationView(enriched_letters, message.author.id)
                return await reply(message, embed=view.build_embed(), view=view)

            # Target given -- start the sending flow. Reuses the same
            # target resolution (@mention / reply / raw ID / username)
            # every other targeted command (lbadges, lprogress, ...) uses.
            target_user = await resolve_target_user(message, args)

            if target_user.id == message.author.id:
                return await reply(message, "You can't send mail to yourself!")
            if target_user.bot:
                return await reply(message, "You can't send mail to a bot.")

            await _run_mail_sending_flow(self, message.channel, message.author, target_user)
            return

        # =========================
        # LDUO COMMAND
        # =========================
        if content_lower == "lduo" or content_lower.startswith("lduo "):
            args = content[len("lduo"):].strip()
            if not args:
                return await reply(message, "Usage: `lduo @user`")

            target_user = await resolve_target_user(message, args)

            if target_user.id == message.author.id:
                return await reply(message, "You can't start a Duo Challenge with yourself!")
            if target_user.bot:
                return await reply(message, "You can't start a Duo Challenge with a bot.")

            author_id = message.author.id
            target_id = target_user.id

            async with duo_lock:
                if find_active_duo(author_id)[0]:
                    return await reply(message, "You're already in an active Duo Challenge.")
                if find_active_duo(target_id)[0]:
                    return await reply(message,
                        f"**{target_user.display_name}** is already in an active Duo Challenge."
                    )

                if duo_weekly_count(author_id) >= DUO_WEEKLY_LIMIT:
                    return await reply(message,
                        "You've already completed 3 Duo Challenges this week."
                    )
                if duo_weekly_count(target_id) >= DUO_WEEKLY_LIMIT:
                    return await reply(message,
                        f"**{target_user.display_name}** has already completed 3 Duo Challenges this week."
                    )

                remaining = duo_cooldown_remaining(author_id)
                if remaining > 0:
                    return await reply(message,
                        f"⏳ You must wait **{format_time(remaining)}** before starting another Duo."
                    )
                remaining = duo_cooldown_remaining(target_id)
                if remaining > 0:
                    return await reply(message,
                        f"⏳ **{target_user.display_name}** must wait **{format_time(remaining)}** "
                        "before starting another Duo."
                    )

                if str(target_id) in duo_weekly_partners(author_id):
                    return await reply(message,
                        f"You've already completed a Duo with **{target_user.display_name}** this week. "
                        "Try a different player."
                    )

            view = DuoRequestView(message.author, target_user, author_id, target_id)
            try:
                sent = await reply(message, embed=view.get_embed(), view=view)
            except Exception:
                print("[lduo] Failed to send the Duo request embed:")
                traceback.print_exc()
                return await reply(message, "❌ Something went wrong sending that Duo request. Please try again.")

            view.message = sent
            return

        # =========================
        # LDUOPROGRESS COMMAND
        # =========================
        if content_lower == "lduoprogress":
            challenge_id, challenge = find_active_duo(user_id)
            if not challenge:
                return await reply(message, "You don't currently have an active Duo Challenge. Start one with `lduo @user`!")

            partner_id = challenge.get("player_a") if challenge.get("player_b") == str(user_id) else challenge.get("player_b")
            partner_user = None
            if partner_id:
                try:
                    partner_user = self.get_user(int(partner_id))
                    if partner_user is None:
                        partner_user = await self.fetch_user(int(partner_id))
                except Exception:
                    partner_user = None

            if partner_user is None:
                return await reply(message, "Your Duo partner could no longer be found.")

            embed = build_duo_progress_embed(self, challenge, user_id, partner_user)
            return await reply(message, embed=embed)

        # =========================
        # LBADGES COMMAND
        # =========================
        if content_lower == "lbadges" or content_lower.startswith("lbadges "):
            args = content[len("lbadges"):].strip()
            target_user = await resolve_target_user(message, args)
            member = _as_member(message, target_user)

            blocks = _ordered_badge_blocks(target_user, member)
            star_rating = _badge_star_rating_for(target_user, member)
            view = BadgesPaginationView(target_user, blocks, message.author.id, star_rating)
            return await reply(message, embed=view.build_embed(), view=view)

        # =========================
        # LSCADD COMMAND (add a card to your showcase)
        # =========================
        if content_lower.startswith("lscadd"):
            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lscadd <inventory number>`")

            try:
                requested_num = int(parts[1])
            except ValueError:
                return await reply(message, "Please provide a valid inventory number.")

            card_index = len(inv) - requested_num
            if card_index < 0 or card_index >= len(inv):
                return await reply(message, "Invalid inventory number.")

            owned_card = inv[card_index]

            if owned_card.get("showcased"):
                return await reply(message, "That card is already in your showcase.")

            showcased_count = sum(1 for oc in inv if oc.get("showcased"))
            if showcased_count >= MAX_SHOWCASE_CARDS:
                return await reply(message,
                    f"You can only showcase up to {MAX_SHOWCASE_CARDS} cards. "
                    f"Remove one first with `lscremove <inventory number>`."
                )

            async with inventories_lock:
                owned_card["showcased"] = True
                try:
                    save_inventories_local()
                    mark_inventories_dirty()
                except Exception:
                    owned_card.pop("showcased", None)
                    return await reply(message,
                        "❌ Something went wrong saving your showcase. Please try again."
                    )

            name = owned_card["card"].get("name", "Unknown")
            return await reply(message, f"✅ Added **{name}** to your showcase.")

        # =========================
        # LSCREMOVE COMMAND (remove a card from your showcase)
        # =========================
        if content_lower.startswith("lscremove"):
            parts = content.split()
            if len(parts) < 2:
                return await reply(message, "Usage: `lscremove <inventory number>`")

            try:
                requested_num = int(parts[1])
            except ValueError:
                return await reply(message, "Please provide a valid inventory number.")

            card_index = len(inv) - requested_num
            if card_index < 0 or card_index >= len(inv):
                return await reply(message, "Invalid inventory number.")

            owned_card = inv[card_index]

            if not owned_card.get("showcased"):
                return await reply(message, "That card isn't in your showcase.")

            async with inventories_lock:
                owned_card["showcased"] = False
                try:
                    save_inventories_local()
                    mark_inventories_dirty()
                except Exception:
                    owned_card["showcased"] = True
                    return await reply(message,
                        "❌ Something went wrong saving your showcase. Please try again."
                    )

            name = owned_card["card"].get("name", "Unknown")
            return await reply(message, f"Removed **{name}** from your showcase.")

        # =========================
        # LSHOWCASE COMMAND
        # =========================
        if content_lower == "lshowcase" or content_lower.startswith("lshowcase "):
            args = content[len("lshowcase"):].strip()
            target_user = await resolve_target_user(message, args)
            member = _as_member(message, target_user)
            is_owner_view = (target_user.id == message.author.id)

            target_inv = get_inventory(target_user.id)
            showcased_cards = [oc for oc in target_inv if oc.get("showcased")][:MAX_SHOWCASE_CARDS]

            loop = asyncio.get_running_loop()
            image_path = await loop.run_in_executor(
                None,
                generate_showcase_image,
                showcased_cards
            )

            # The player's own custom description (showcases.json)
            # replaces the old character-info block. Each stored line
            # is shown in its own backtick "text box"; unset stays
            # blank rather than showing a placeholder card list.
            stored_description = get_showcase_description(target_user.id)
            if stored_description:
                description_lines = stored_description.split("\n")
                description_block = "\n".join(f"> ✦ {line}" for line in description_lines)
            else:
                description_block = ""

            # Badge progress/star rating, computed live from the same
            # completed/total ratio as always (see compute_star_rating)
            # -- needed up front now since the star rating is shown in
            # the title rather than the footer.
            progress = compute_badge_progress(member, target_inv)
            completed_badge_count = sum(1 for (_, _, completed) in progress.values() if completed)
            total_badge_count = len(BADGE_DEFINITIONS)
            star_rating = compute_star_rating(completed_badge_count, total_badge_count)

            embed = discord.Embed(color=THEME_COLOR)
            # Standard author line (avatar + "@user's Showcase"), in
            # addition to -- not instead of -- the existing "## " title
            # styling below, per spec.
            embed.set_author(
                name=f"@{target_user.display_name}'s Showcase",
                icon_url=target_user.display_avatar.url
            )
            # Embed titles don't render markdown -- the description does
            # -- so the prominent title lives here as a "## " header,
            # with the showcase description directly beneath it, all
            # above the image. The star rating sits on its own line
            # under the title, separated from the description below it
            # by a small divider.
            embed.description = (
                f"## {target_user.mention}'s Showcase\n{star_rating}\n{SHOWCASE_DIVIDER}\n\n{description_block}"
            ).rstrip()
            embed.set_image(url="attachment://showcase.png")

            # Footer is just the badge count now -- the star rating
            # moved up into the title above.
            embed.set_footer(text=f"🏅 Badges: {completed_badge_count}/{total_badge_count}")

            file = discord.File(image_path, filename="showcase.png")
            view = ShowcaseView(self, target_user, member, is_owner_view)

            await reply(message, embed=embed, file=file, view=view)

            try:
                os.remove(image_path)
            except:
                pass
            return

        # =========================
        # TRADE COMMAND (lt / ltrade)
        # =========================
        if content_lower.startswith(("ltrade ", "lt")):
            if is_command_spam(user_id, "lt"):
                return await reply(message, 
                    "Please wait a few seconds before using this command again."
                )

            target_user = None

            if message.reference:
                try:
                    replied_msg = await message.channel.fetch_message(message.reference.message_id)
                    if replied_msg and replied_msg.author:
                        target_user = replied_msg.author
                except:
                    pass

            if not target_user and message.mentions:
                target_user = message.mentions[0]

            if not target_user:
                return await reply(message, 
                    "Usage: `lt @user` (reply to their message or mention them)"
                )

            if target_user.bot:
                return await reply(message, 
                    "You can't trade with bots."
                )

            if target_user.id == message.author.id:
                return await reply(message, 
                    "You can't trade with yourself."
                )

            if user_has_active_trade(message.author.id) or user_has_active_trade(target_user.id):
                return await reply(message, "You already have an ongoing trade.")

            if len(inv) == 0:
                return await reply(message, 
                    "You don't have any cards to trade."
                )

            target_inv = get_inventory(target_user.id)
            if len(target_inv) == 0:
                return await reply(message, 
                    f"{target_user.mention} doesn't have any cards to trade."
                )

            request_view = TradeRequestView(
                message.author,
                target_user,
                message.author.id,
                target_user.id
            )

            request_message = await message.channel.send(
                embed=request_view.get_embed(),
                view=request_view
            )
            request_view.message = request_message

            return

        # =========================
        # ADD CARD TO TRADE (add <card_number>)
        # =========================
        if content_lower.startswith("add "):
            try:
                words = content.split()
                if len(words) < 2:
                    return  # bare "add" -- ignore silently, no usage reply

                raw = words[1]
                try:
                    requested_num = int(raw)
                except:
                    return  # non-numeric -- ignore silently, no usage reply

                # Find the user's active trade view
                user_trade = None
                for trade_id, trade_data in active_trades.items():
                    parts = trade_id.split('_')
                    if str(user_id) in parts and trade_data.get("view"):
                        user_trade = trade_data["view"]
                        break

                if not user_trade:
                    return  # not in an active trade -- ignore silently

                inv_list = get_inventory(user_id)
                # Displayed numbers count down from newest (highest) to
                # oldest (1), so convert back to a list index accordingly.
                pos_idx = len(inv_list) - requested_num
                if pos_idx < 0 or pos_idx >= len(inv_list):
                    return await reply(message, "Invalid card number.")

                owned_card = inv_list[pos_idx]
                card_index = pos_idx

                # Assign to trade by index -- append up to MAX_TRADE_CARDS
                # cards per person. If this exact card (by id + print) is
                # already in that person's selection, treat it as a
                # toggle-off: remove only that one card, leaving the rest
                # of the selection untouched.
                if user_id == user_trade.user1_id:
                    cards_list = user_trade.user1_cards
                    indices_list = user_trade.user1_card_indices
                elif user_id == user_trade.user2_id:
                    cards_list = user_trade.user2_cards
                    indices_list = user_trade.user2_card_indices
                else:
                    return await reply(message, "You're not part of this trade.")

                existing_pos = next(
                    (i for i, c in enumerate(cards_list)
                     if c["card"]["id"] == owned_card["card"]["id"] and c["print"] == owned_card["print"]),
                    None
                )

                if existing_pos is not None:
                    cards_list.pop(existing_pos)
                    indices_list.pop(existing_pos)
                    action_text = f"Removed {owned_card['card'].get('name', 'Unknown Character')}"
                elif len(cards_list) >= MAX_TRADE_CARDS:
                    return await reply(message, f"You can only add up to {MAX_TRADE_CARDS} cards.")
                else:
                    cards_list.append(owned_card)
                    indices_list.append(card_index)
                    action_text = f"Added {owned_card['card'].get('name', 'Unknown Character')}"

                if user_trade.user1_cards and user_trade.user2_cards:
                    if user_trade.stage == "selecting":
                        user_trade.stage = "locking"
                elif user_trade.stage == "locking":
                    # A toggle-off just emptied one side after locking had
                    # already begun -- go back to selecting and clear any
                    # lock flags, since they no longer reflect a real,
                    # non-empty offer on both sides. Without this, someone
                    # could still hit "lock" with zero cards selected,
                    # since that button only blocks while stage ==
                    # "selecting".
                    user_trade.stage = "selecting"
                    user_trade.user1_locked = False
                    user_trade.user2_locked = False

                # Update the trade message immediately (if stored)
                try:
                    trade_msg = active_trades[user_trade.trade_id].get("message")
                    if trade_msg:
                        await trade_msg.edit(embed=user_trade.build_embed(), view=user_trade)
                except Exception:
                    pass

                return await reply(message, action_text)
            except Exception as e:
                return await reply(message, f"Error: {e}")

        # =========================
        # MERCHANT LIST / ACCEPT TRADE (lmerchant)
        # =========================
        if content_lower == "lmerchant" or content_lower == "lmerchants":
            if is_command_spam(user_id, "lmerchant"):
                return await reply(message,
                    "Please wait a few seconds before using this command again."
                )

            await check_and_update_merchants()

            list_view = MerchantListView(user_id)
            embed, file = list_view.build_embed_and_file()

            send_kwargs = {"embed": embed, "view": list_view}
            if file:
                send_kwargs["file"] = file

            try:
                sent = await message.channel.send(**send_kwargs)
            except Exception:
                print("[lmerchants] Failed to send the merchant list embed:")
                traceback.print_exc()
                return await reply(message, "❌ Something went wrong showing the merchants. Please try again.")

            list_view.message = sent
            return

        # =========================
        # MERCHANT ADMIN TESTING (lmerchantcontrol arrive|leave)
        # =========================
        if content_lower.startswith("lmerchantcontrol "):
            if message.author.id not in OWNER_USER_IDS:
                return

            action = content_lower[len("lmerchantcontrol "):].strip()

            if action not in ("arrive", "leave"):
                return await reply(message, 
                    "Usage: `lmerchantcontrol arrive` or `lmerchantcontrol leave`."
                )

            did_something = await _force_merchant_active_state(active=(action == "arrive"))

            if not did_something:
                return await reply(message, 
                    "There's no current merchant batch to flip -- nothing to do."
                )

            if action == "arrive":
                return await reply(message, 
                    "✅ The current merchants now count as active/arrived. "
                    "Check `lmerchants` and the announcement channel."
                )
            else:
                return await reply(message, 
                    "✅ The current merchants now count as inactive/left. "
                    "Check `lmerchants` and the announcement channel."
                )

        # =========================
        # MERCHANT TRADE: ADD CARD TO OFFER (madd <card_number>)
        # =========================
        if content_lower.startswith("madd "):
            try:
                words = content.split()
                if len(words) < 2:
                    return  # bare "madd" -- ignore silently, no usage reply

                raw = words[1]
                try:
                    requested_num = int(raw)
                except:
                    return  # non-numeric -- ignore silently, no usage reply

                trade_data = active_merchant_trades.get(user_id)
                if not trade_data or not trade_data.get("view"):
                    return  # not in an active merchant trade -- ignore silently

                trade_view = trade_data["view"]

                # Displayed numbers count down from newest (highest) to
                # oldest (1), same convention as the player-to-player
                # "add" command above.
                pos_idx = len(inv) - requested_num
                if pos_idx < 0 or pos_idx >= len(inv):
                    return await reply(message, "Invalid card number.")

                owned_card = inv[pos_idx]

                status_text = trade_view.toggle_card(owned_card)
                await trade_view.refresh_message()

                return await reply(message, status_text)
            except Exception as e:
                return await reply(message, f"Error: {e}")

        # =========================
        # VIEW CARD COMMAND (lv <num>)
        # =========================
        if content_lower.startswith("lv "):
            try:
                requested_num = int(content_lower.split()[1])

                viewing_user_id = user_viewing_inventory.get(user_id, user_id)
                target_inv = get_inventory(viewing_user_id)

                # Displayed numbers count down from newest (highest) to
                # oldest (1), so convert back to a list index accordingly.
                index = len(target_inv) - requested_num

                if index < 0 or index >= len(target_inv):
                    raise IndexError
                owned_card = target_inv[index]
                card = owned_card["card"]
                print_num = owned_card["print"]
            except:
                return await reply(message, "Invalid card number.")

            name = card.get("name", "Unknown Character")
            series = card.get("series", "Unknown Series")
            star_val = card.get("stars", 1)

            # Merchant-reward cards keep their real print number here too
            # (text + rendered image both), instead of collapsing to "L"
            # past 100 -- see render_card's force_real_print. Every other
            # card is completely unaffected (owned_card.get(...) is False).
            is_merchant_reward = bool(owned_card.get("merchant_reward"))
            print_display = format_merchant_print(print_num) if is_merchant_reward else format_print(print_num)

            embed = discord.Embed(color=THEME_COLOR)
            embed.set_author(name=f"{message.author.name}'s Card", icon_url=message.author.display_avatar.url)
            embed.description = (
                f"## **{name}**\n"
                f"✦ **Series:** **{series}**\n"
                f"───\n"
                f"✦ **Owner:** <@{viewing_user_id}>\n"
                f"✦ **Print:** **{print_display}**\n"
                f"✦ **Level:** **{stars(star_val)}**\n"
                f"✦ **Version:** **{card_version_display(card)}**\n"
            )
            image_path = render_card_final(card, print_num, force_real_print=is_merchant_reward)

            if image_path:
                file = discord.File(image_path, filename="card.png")
                embed.set_image(url="attachment://card.png")
                await reply(message, embed=embed, file=file)
                try:
                    os.remove(image_path)
                except:
                    pass
            else:
                await reply(message, embed=embed)

            return

        # =========================
        # LOOKUP COMMAND (lup <query>)
        # =========================
        if content_lower.startswith("lup "):
            query = content[4:].strip().lower()
            if not query:
                return await reply(message, 
                    "Please provide a name or a number to search."
                )

            # If user sent a number selection after a previous search
            if query.isdigit():
                if user_id not in user_last_lookup:
                    return await reply(message, 
                        "You haven't searched for anything yet! Search using a name first."
                    )

                selection = int(query) - 1
                previous_results = user_last_lookup[user_id]

                if selection < 0 or selection >= len(previous_results):
                    return await reply(message, 
                        "Invalid number selection from your last search."
                    )

                chosen_card = previous_results[selection]

                # Grouped by (name, series) -- same name in a DIFFERENT
                # series (e.g. Robin from Honkai vs. Robin from DC) is a
                # completely separate character, not another version.
                all_versions = [
                    c for c in cards
                    if c.get("name", "").lower() == chosen_card.get("name", "").lower()
                    and c.get("series", "").lower() == chosen_card.get("series", "").lower()
                ]
                # Display order: Common -> V1 -> V2 -> ... -> Rare, based
                # on each card's actual stored `version` metadata (never
                # cards.json's list order). See _lup_version_sort_key.
                all_versions.sort(key=_lup_version_sort_key)

                view = CharacterVersionView(
                    all_versions,
                    message.author,
                    user_id
                )

                image_path = render_card_final(
                    all_versions[0],
                    peek_next_print(all_versions[0]["id"]),
                    hide_print=True
                )

                file = discord.File(image_path, filename="card.png")
                embed = view.build_embed()
                embed.set_image(url="attachment://card.png")

                await reply(message, 
                    embed=embed,
                    file=file,
                    view=view
                )
                try:
                    os.remove(image_path)
                except:
                    pass
                return

            # search by string (either name or series)
            matched_cards = [
                card for card in cards
                if (
                    query in card.get("name", "").lower()
                    or query in card.get("series", "").lower()
                )
            ]

            if not matched_cards:
                return await reply(message, "No cards found.")

            # collapse to unique (name, series) pairs for list view --
            # same name in a different series is a separate character.
            unique_results = []
            seen_keys = set()

            for card in matched_cards:
                key = (card.get("name", "").lower(), card.get("series", "").lower())
                if key not in seen_keys:
                    seen_keys.add(key)
                    unique_results.append(card)

            user_last_lookup[user_id] = unique_results

            # If only one unique result, show the card directly
            if len(unique_results) == 1:
                all_versions = [
                    c for c in cards
                    if c.get("name", "").lower() == unique_results[0].get("name", "").lower()
                    and c.get("series", "").lower() == unique_results[0].get("series", "").lower()
                ]
                # Display order: Common -> V1 -> V2 -> ... -> Rare, based
                # on each card's actual stored `version` metadata (never
                # cards.json's list order). See _lup_version_sort_key.
                all_versions.sort(key=_lup_version_sort_key)

                view = CharacterVersionView(
                    all_versions,
                    message.author,
                    user_id
                )

                image_path = render_card_final(
                    all_versions[0],
                    peek_next_print(all_versions[0]["id"]),
                    hide_print=True
                )

                file = discord.File(image_path, filename="card.png")
                embed = view.build_embed()
                embed.set_image(url="attachment://card.png")

                await reply(message, 
                    embed=embed,
                    file=file,
                    view=view
                )
                try:
                    os.remove(image_path)
                except:
                    pass
                return

            view = LookupListView(unique_results, message.author, user_id)
            return await reply(message, 
                embed=view.get_embed(),
                view=view
            )

        # =========================
        # DROP CARDS COMMAND (ld)
        # =========================
        if content_lower == "ld":
            t_ld_start = time.perf_counter()

            if is_command_spam(user_id, "ld"):
                return await reply(message, 
                    "Please wait a few seconds before using this command again."
                )

            now = time.time()

            if user_id in drop_cooldowns:
                remaining = int(DROP_COOLDOWN - (now - drop_cooldowns[user_id]))
            else:
                remaining = 0

            # Duo bonus drops: only ever touched when the normal cooldown
            # would otherwise block this drop. If the player is already
            # off cooldown, this is a completely normal drop and no
            # bonus is spent -- bonuses stay banked for when they're
            # actually needed. Does not change drop generation, odds,
            # rendering, or anything else about how a drop works.
            used_bonus_drop = False
            if remaining > 0:
                async with duo_lock:
                    if consume_bonus(user_id, "drop"):
                        used_bonus_drop = True
                        try:
                            save_duo_local()
                            mark_duo_dirty()
                        except Exception:
                            add_bonus(user_id, "drop", 1)
                            used_bonus_drop = False

                if not used_bonus_drop:
                    return await reply(message, 
                        f"⏳ You must wait **{format_time(remaining)}** before dropping again."
                    )

            t_ld_precheck = time.perf_counter()

            card1 = get_weighted_card()
            card2 = get_weighted_card()

            while card2["id"] == card1["id"]:
                card2 = get_weighted_card()

            # A bonus-drop never resets/restarts the normal cooldown --
            # per the Duo bonus system, the normal cooldown only resumes
            # once every bonus has been spent.
            if not used_bonus_drop:
                drop_cooldowns[user_id] = now

            t_ld_cardselect = time.perf_counter()

            loop = asyncio.get_running_loop()

            # Recycled-card exception (see get_weighted_card()/
            # lrecyclecards): a recycled slot displays its EXACT
            # original print number instead of the normal preview
            # (peek_next_print never even runs for it) -- this is only
            # ever a display/preview number either way, same as a
            # normal drop's; the print actually granted is decided at
            # claim time (see CardView.claim()).
            display_print1 = card1["_recycled_print"] if card1.get("_recycled_entry_id") is not None else peek_next_print(card1["id"])
            display_print2 = card2["_recycled_print"] if card2.get("_recycled_entry_id") is not None else peek_next_print(card2["id"])

            image_path = await loop.run_in_executor(
                _drop_render_executor,
                render_drop,
                card1,
                display_print1,
                card2,
                display_print2
            )

            t_ld_render = time.perf_counter()

            if image_path is None:
                return await reply(message, 
                    "❌ Failed to render the drop."
                )

            # Trace point: exact resolution, format, and encoded size of the
            # single file about to be uploaded to Discord for `ld`, right
            # before it's attached/sent (bot upload cap is well under
            # Discord's normal per-file limit, so this is the number that
            # actually matters for the 413 error).
            with Image.open(image_path) as _dbg_img:
                _dbg_w, _dbg_h = _dbg_img.size
                _dbg_format = _dbg_img.format
            _dbg_size_mb = os.path.getsize(image_path) / 1_000_000
            print(
                f"[ld] Uploading drop image -> {_dbg_w}x{_dbg_h}px, "
                f"format={_dbg_format}, size={_dbg_size_mb:.2f} MB"
            )

            file = discord.File(
                image_path,
                filename="drop.png"
            )

            view = CardView(card1, card2, dropper_id=user_id)

            drop_message = await message.channel.send(
                content=f"{message.author.mention} is dropping 2 cards!",
                file=file,
                view=view
            )
            view.message = drop_message
            # The priority window must start counting from the moment
            # the drop is actually visible/clickable, not from
            # CardView's construction above -- rendering and the upload
            # itself can easily eat 1-3+ seconds, which was silently
            # consuming the window before anyone could even see the
            # buttons.
            view.drop_time = time.time()

            t_ld_sent = time.perf_counter()

            # TEMPORARY instrumentation to identify the `ld` bottleneck --
            # measurement only, safe to remove once confirmed. Note: no
            # inventory lookup, owner lookup, badge computation, showcase
            # load, vote load, or pagination happens anywhere in this
            # command -- those categories are simply not part of `ld`'s
            # code path, so there is nothing to time for them here.
            print(
                "ld timing:\n"
                f"- Spam/cooldown precheck: {(t_ld_precheck - t_ld_start) * 1000:.1f} ms\n"
                f"- Weighted card selection: {(t_ld_cardselect - t_ld_precheck) * 1000:.1f} ms\n"
                f"- render_drop (executor, see render_drop timing above): "
                f"{(t_ld_render - t_ld_cardselect) * 1000:.1f} ms\n"
                f"- Discord file/message send: {(t_ld_sent - t_ld_render) * 1000:.1f} ms\n"
                f"- Total ld: {(t_ld_sent - t_ld_start) * 1000:.1f} ms"
            )

            try:
                os.remove(image_path)
            except:
                pass

            return

        # =========================
        # LFINDCARD COMMAND
        # =========================
        if content_lower.startswith("lfindcard "):
            query = content[10:].strip().lower()
            if not query:
                return await reply(message, "Usage: lfindcard <card name>")

            # try exact match then substring
            card = next((c for c in cards if c.get("name", "").lower() == query), None)
            if not card:
                card = next((c for c in cards if query in c.get("name", "").lower()), None)

            if not card:
                return await reply(message, "Card not found.")

            # Find all versions of this character
            all_versions = [
                c for c in cards
                if c.get("name", "").lower() == card.get("name", "").lower()
            ]
            all_versions.sort(key=lambda x: x.get("stars", 1))

            view = FindcardVersionView(all_versions, message.author, user_id)

            image_path = render_card_final(
                all_versions[0],
                peek_next_print(all_versions[0]["id"])
            )

            file = discord.File(image_path, filename="card.png")
            embed = view.build_embed()
            embed.set_thumbnail(url="attachment://card.png")

            await reply(message, embed=embed, file=file, view=view)

            try:
                os.remove(image_path)
            except:
                pass
            return

        # =========================
        # LFINDSERIES COMMAND
        # =========================
        if content_lower.startswith("lfindseries "):
            query = content[12:].strip().lower()
            if not query:
                return await reply(message, "Usage: lfindseries <series name>")

            # try exact match then substring
            matched_cards = [c for c in cards if c.get("series", "").lower() == query]
            if not matched_cards:
                matched_cards = [c for c in cards if query in c.get("series", "").lower()]

            if not matched_cards:
                return await reply(message, "No cards found for that series.")

            matched_cards.sort(key=lambda c: (c.get("name", "").lower(), c.get("stars", 1)))

            series_display = matched_cards[0].get("series", query)

            view = FindSeriesView(series_display, matched_cards, message.author, user_id)

            return await reply(message, 
                embed=view.get_embed(),
                view=view
            )

        # =========================
        # LPROGRESS COMMAND
        # =========================
        if content_lower == "lprogress" or content_lower.startswith("lprogress "):
            args = content[len("lprogress"):].strip()
            target_user = await resolve_target_user(message, args)

            target_inv = get_inventory(target_user.id)
            stats = _compute_collection_progress(target_inv)

            embed = _build_progress_embed(target_user, stats)
            return await reply(message, embed=embed)

        # =========================
        # LMISSING COMMAND
        # =========================
        if content_lower == "lmissing" or content_lower.startswith("lmissing "):
            raw_args = content[len("lmissing"):].strip()
            series_query, other_user = await _parse_missing_args(message, raw_args)

            requester_inv = get_inventory(message.author.id)
            stats = _compute_collection_progress(requester_inv)

            # --- Comparison mode: which of THEIR cards from a series am I missing? ---
            if other_user is not None:
                if not series_query:
                    return await reply(message,
                        "Please specify a series to compare, e.g. `lmissing <series> @user`."
                    )

                matched_series = _match_series(series_query)
                if matched_series is None:
                    return await reply(message, f"No series found matching `{series_query}`.")

                other_inv = get_inventory(other_user.id)
                other_stats = _compute_collection_progress(other_inv)

                your_ids = stats.get("owned_card_ids", set())
                their_ids = other_stats.get("owned_card_ids", set())

                # Only cards actually in this series that other_user owns
                # and the requester doesn't -- grouped by character, with
                # the specific missing star number(s) shown (same "Name
                # ★star, ★star" format lmissing uses everywhere else),
                # not just bare character names.
                missing_by_name = {}
                for card in cards:
                    if card.get("series") != matched_series:
                        continue
                    card_id = card.get("id")
                    if card_id in their_ids and card_id not in your_ids:
                        missing_by_name.setdefault(card.get("name", "Unknown"), set()).add(card.get("stars", 1))

                missing_lines = [
                    f"{name} " + ", ".join(f"★{s}" for s in sorted(star_set))
                    for name, star_set in sorted(missing_by_name.items())
                ]

                embed = _build_missing_comparison_embed(matched_series, other_user, missing_lines)
                return await reply(message, embed=embed)

            # --- Single-series view: only that series, no overview page ---
            if series_query:
                matched_series = _match_series(series_query)
                if matched_series is None:
                    return await reply(message, f"No series found matching `{series_query}`.")

                embed = _build_missing_series_embed(message.author, matched_series, stats)
                return await reply(message, embed=embed)

            # --- No filters: paginated overview + one page per incomplete series ---
            incomplete_series = []
            for series, total in stats["series_totals"].items():
                if total <= 0:
                    continue
                remaining = total - len(stats["per_series_owned"].get(series, set()))
                if remaining > 0:
                    incomplete_series.append((series, remaining))
            incomplete_series.sort(key=lambda pair: pair[1])  # fewest remaining first

            total_pages = 1 + len(incomplete_series)
            embeds = [_build_missing_overview_embed(message.author, incomplete_series, total_pages)]
            for i, (series, _remaining) in enumerate(incomplete_series, start=2):
                embeds.append(
                    _build_missing_series_embed(message.author, series, stats, page_num=i, total_pages=total_pages)
                )

            if len(embeds) == 1:
                return await reply(message, embed=embeds[0])

            view = MissingCardsPaginationView(embeds, message.author.id)
            return await reply(message, embed=view.current_embed(), view=view)


# --- Run Bot Connection ---
client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)