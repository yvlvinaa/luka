import os
from dotenv import load_dotenv

load_dotenv()

import discord
import random
import re
import time
import asyncio
import requests
import tempfile
import json
import base64
import functools
import traceback
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont, ImageStat

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


# Initialize intents
intents = discord.Intents.all()

# Global Configurations
DROP_COOLDOWN = 600
CLAIM_COOLDOWN = 300
CLAIM_TIME_LIMIT = 90  # seconds a dropped card stays claimable before its buttons expire
CARDS_PER_PAGE = 10
THEME_COLOR = discord.Color.from_rgb(255, 227, 102)

# Global tracking for lookup history sessions
user_last_lookup = {}

# Global tracking for trade and gift sessions
active_trades = {}
active_gifts = {}
user_viewing_inventory = {}

# Guards every write transaction that touches the card database: the
# in-memory `cards` list, cards.json, GitHub sync, and card_art/ files.
# Held for the full transaction (mutate -> GitHub commit -> local
# mirror/save) so two admin commands can never interleave a
# read-modify-write against the same data and silently overwrite each
# other's changes. Never used by read-only paths (ld, lookups, inventory,
# claiming, trading, gifting, rendering).
cards_lock = asyncio.Lock()

# Create card_art directory if it doesn't exist
if not os.path.exists('card_art'):
    os.makedirs('card_art')

# =========================
# HELPERS
# =========================

def stars(amount):
    """Converts a number into a star emoji string."""
    return "⭐" * int(amount)


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


def remove_card(user_id, index):
    """Removes a card from a user's collection by its index position."""
    return get_inventory(user_id).pop(index)


def get_weighted_card():
    """Selects a card randomly based on its assigned weight value."""
    weighted = []
    for card in cards:
        weighted.extend([card] * card.get("weight", 1))
    return random.choice(weighted)


def format_print(print_num):
    """Formats print number for display."""
    if print_num < 100:
        return f"#{print_num}"
    if print_num == 100:
        return "#100"
    if print_num > 100:
        return "L"


def save_cards_json():
    """Saves the cards list to cards.json"""
    with open('cards.json', 'w') as f:
        json.dump(cards, f, indent=2)


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
SERIES_LINE_SPACING = 30

# Print number position -- moved ~5px lower and slightly further right to
# match the original renderer's placement more closely.
PRINT_POS_COMMON = (1030, 325)
PRINT_POS_RARE = (380, 295)

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
ARTWORK_INNER_MARGIN_X = 185
ARTWORK_INNER_MARGIN_TOP = 50
ARTWORK_INNER_MARGIN_BOTTOM = 105

# Rounded clip box the COMMON frame's gradient fades into (see
# _common_gradient_box). Sized tightly around the name/series text area
# (plus a little padding) instead of a large fraction of the card, so it
# never extends far up into the artwork.
COMMON_GRADIENT_BOX_RADIUS = 60
COMMON_GRADIENT_BOX_TOP_PADDING = 50
COMMON_GRADIENT_BOX_BOTTOM_PADDING = 50


# Per-frame gradient colors. "common" is intentionally absent -- it always
# uses GRADIENT_COLOR (the gray) above. Any frame name not listed here also
# falls back to GRADIENT_COLOR. To add a new rare frame's gradient color,
# just add an entry here -- no rendering logic needs to change.
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
DROP_UPSCALE = 2.0   # higher output resolution so Discord shows it bigger/sharper

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

def render_card(card: dict, print_num, hide_print: bool = False) -> Image.Image:
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
    """
    frame_name = card.get("frame", "common")
    rare = is_rare(frame_name)

    # 1. Artwork
    art_source = load_artwork_source(card.get("image", ""), card_id=card.get("id"))
    canvas = center_crop_to_fill(art_source).convert("RGBA")

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
        print_text = format_print(print_num).lstrip("#")
        print_font = get_font(PRINT_FONT_SIZE)
        print_pos = PRINT_POS_RARE if rare else PRINT_POS_COMMON
        draw_text_with_outline(draw, print_pos, print_text, print_font, anchor="la")

    # 6. Character name (large) -- shrinks and/or wraps to two balanced
    # lines if it would otherwise exceed MAX_NAME_WIDTH
    name_wrapped = draw_character_name(draw, card.get("name", "Unknown"), CENTER_X, NAME_Y)

    # 7. Series (smaller, wraps to a second centered line past 4 words)
    series_font = get_font(SERIES_FONT_SIZE)
    series_y = SERIES_Y + SERIES_Y_SHIFT_FOR_WRAPPED_NAME if name_wrapped else SERIES_Y
    draw_series_text(draw, card.get("series", "Unknown Series"), series_font, CENTER_X, series_y)

    return canvas


def render_card_final(card: dict, print_num, hide_print: bool = False) -> str:
    """
    Drop-in replacement for the old render_card_final().
    Same contract: renders one card, saves it to a temp PNG, and returns
    the file path. Existing callers don't need to change at all -- pass
    hide_print=True to render the card without its print number (used only
    by the lup command).
    """
    try:
        final = render_card(card, print_num, hide_print=hide_print)
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
    try:
        future1 = _render_executor.submit(render_card, card1, print1)
        future2 = _render_executor.submit(render_card, card2, print2)
        img1 = future1.result()
        img2 = future2.result()
        combined = combine_cards([img1, img2])
    except Exception as e:
        print("RENDER ERROR:", e)
        combined = Image.new("RGBA", (CARD_WIDTH * 2 + DROP_SPACING, CARD_HEIGHT), (30, 30, 30, 255))
        d = ImageDraw.Draw(combined)
        d.text((50, 50), f"Render Error: {str(e)[:200]}", font=get_font(40), fill=(255, 255, 255))

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    # compress_level=1: same lossless tradeoff as render_card_final -- no
    # effect on decoded pixels, just faster encoding of the large combined
    # (2x-upscaled) drop image.
    combined.save(temp_file.name, compress_level=1)
    return temp_file.name


# =========================
# 1. DROP VIEW
# =========================
class CardView(discord.ui.View):
    def __init__(self, card1, card2):
        super().__init__(timeout=CLAIM_TIME_LIMIT)
        self.card1 = card1
        self.card2 = card2
        self.card1_claimed = False
        self.card2_claimed = False

    async def claim(self, interaction, which, button):
        user_id = interaction.user.id
        now = time.time()

        if user_id in claim_cooldowns:
            remaining = int(CLAIM_COOLDOWN - (now - claim_cooldowns[user_id]))
            if remaining > 0:
                return await interaction.response.send_message(
                    f"Wait {format_time(remaining)} before claiming again.", ephemeral=True
                )

        if which == 1 and self.card1_claimed:
            return await interaction.response.send_message("Already claimed.", ephemeral=True)
        if which == 2 and self.card2_claimed:
            return await interaction.response.send_message("Already claimed.", ephemeral=True)

        card = self.card1 if which == 1 else self.card2

        if which == 1:
            self.card1_claimed = True
        else:
            self.card2_claimed = True

        button.disabled = True
        claim_cooldowns[user_id] = now
        add_card(user_id, card)

        await interaction.response.edit_message(view=self)

        name = card.get("name", "Unknown")
        star_val = card.get("stars", 1)
        await interaction.channel.send(
            f"{interaction.user.mention} claimed **{name}**! {stars(star_val)} from the Stage."
        )

    @discord.ui.button(emoji="1️⃣", style=discord.ButtonStyle.primary)
    async def pick1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.claim(interaction, 1, button)

    @discord.ui.button(emoji="2️⃣", style=discord.ButtonStyle.primary)
    async def pick2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.claim(interaction, 2, button)


# =========================
# 2. INVENTORY VIEW
# =========================

class InventoryView(discord.ui.View):
    def __init__(self, user, inventory, viewer_id=None):
        super().__init__(timeout=60)
        self.user = user
        # self.inventory is a list of (display_number, owned_card) tuples.
        # display_number is each card's TRUE position-based number in the
        # user's full, unfiltered inventory (see the lc command) -- so
        # filtering/searching never changes what number a card shows.
        self.inventory = inventory
        self.viewer_id = viewer_id
        self.page = 0

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name=f"{self.user.name}'s Collection",
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

            text += (
                f"`{display_number:02d}` ✦ "
                f"• `{format_print(print_num)}` "
                f"• `⭐ {star_val}` "
                f"• **{name}** • *{series}*\n"
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

        card = self.versions[self.index]

        owners = []

        for owner_id, inventory in inventories.items():
            for owned in inventory:
                if owned["card"]["id"] == card["id"]:
                    owners.append((owned["print"], owner_id))

        owners.sort()

        embed = discord.Embed(color=THEME_COLOR)
        embed.title = f"**{card['name']} Owners**"

        if not owners:
            embed.description = "Nobody owns this card yet."

        else:
            lines = []

            for print_num, owner_id in owners:

                member = interaction.guild.get_member(owner_id)

                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(owner_id)
                    except:
                        continue

                lines.append(
                    f"`{format_print(print_num)}.` • {member.mention}"
                )

            embed.description = "\n".join(lines)

        await interaction.response.send_message(
            embed=embed
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
        emoji="◀️",
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
        emoji="▶️",
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

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
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
        super().__init__(timeout=120)
        self.from_user = from_user
        self.to_user = to_user
        self.owned_card = owned_card
        self.card = owned_card["card"]
        self.print_num = owned_card["print"]
        self.from_id = from_id
        self.to_id = to_id
        self.card_index = card_index
        self.gift_id = f"{from_id}_{to_id}_{int(time.time())}"
        active_gifts[self.gift_id] = {"time": time.time()}

    def build_embed(self, owner_user, status_text=None):
        card = self.card
        star_val = card.get("stars", 1)

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
            f"✦ **Print:** **{format_print(self.print_num)}**\n"
            f"✦ **Level:** **{stars(star_val)}**\n"
        )

        image_path = render_card_final(card, self.print_num)
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


# =========================
# 7. TRADE REQUEST VIEW
# =========================

class TradeRequestView(discord.ui.View):
    def __init__(self, user1, user2, user1_id, user2_id):
        super().__init__(timeout=60)
        self.user1 = user1
        self.user2 = user2
        self.user1_id = user1_id
        self.user2_id = user2_id
        self.request_id = f"{user1_id}_{user2_id}_{int(time.time())}"

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.description = f"{self.user2.mention}, you've received a trade request from {self.user1.mention}!"
        return embed

    @discord.ui.button(emoji="<:accept:1515633292605657088>", style=discord.ButtonStyle.success, label="Trade")
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

    @discord.ui.button(emoji="<:decline:1515633309953163344>", style=discord.ButtonStyle.danger, label="Cancel")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user2_id:
            return await interaction.response.send_message(
                "This isn't your trade request!",
                ephemeral=True
            )

        embed = discord.Embed(color=THEME_COLOR)
        embed.description = "Trade request has been denied."

        await interaction.response.edit_message(
            embed=embed,
            view=None
        )


# =========================
# 8. TRADE VIEW
# =========================

class TradeView(discord.ui.View):
    def __init__(self, user1, user2, user1_id, user2_id):
        super().__init__(timeout=180)
        self.user1 = user1
        self.user2 = user2
        self.user1_id = user1_id
        self.user2_id = user2_id
        self.user1_card = None
        self.user1_card_index = None
        self.user2_card = None
        self.user2_card_index = None
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

        def format_offer(user, owned_card, card_index, status):
            block = f"> # {trade_emoji} {user.mention} is offering... - {status}\n"
            if owned_card:
                card = owned_card["card"]
                name = card.get("name", "Unknown")
                series = card.get("series", "Unknown Series")
                print_num = owned_card["print"]
                star_val = card.get("stars", 1)
                if card_index is not None:
                    # Same descending scheme as the inventory display:
                    # highest number = newest/top of the owner's inventory.
                    owner_inv_len = len(get_inventory(user.id))
                    inv_num = owner_inv_len - card_index
                else:
                    inv_num = "?"
                block += f"`{inv_num} • {format_print(print_num)} • ☆{star_val} • **{name}** • *{series}*`\n"
            else:
                block += "`No cards selected yet.`\n"
            return block

        user1_text = format_offer(self.user1, self.user1_card, self.user1_card_index, user1_status)
        user2_text = format_offer(self.user2, self.user2_card, self.user2_card_index, user2_status)

        embed.description = user1_text + "────────────────────────\n" + user2_text

        embed.description += "\n-# 💡 **Reminder:** There are no official values for cards in LukaNet right now. Trade based on what you and the other user think is fair."

        return embed

    @discord.ui.button(emoji="<:decline:1515633309953163344>", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.trade_id in active_trades:
            del active_trades[self.trade_id]

        await interaction.response.edit_message(
            content="Trade has been declined.",
            embed=None,
            view=None
        )

    @discord.ui.button(emoji="<:lock:1522002571496128553>", style=discord.ButtonStyle.secondary)
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
            try:
                self.lock.emoji = "<:accept:1515633292605657088>"
            except Exception:
                pass

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self
        )

    @discord.ui.button(emoji="<:accept:1515633292605657088>", style=discord.ButtonStyle.success)
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
                if self.user1_card and self.user2_card:
                    inv1 = get_inventory(self.user1_id)
                    inv2 = get_inventory(self.user2_id)

                    # find by id + print to be robust against index shifts
                    seq1_match_idx = next((i for i,c in enumerate(inv1) if c["card"]["id"] == self.user1_card["card"]["id"] and c["print"] == self.user1_card["print"]), None)
                    seq2_match_idx = next((i for i,c in enumerate(inv2) if c["card"]["id"] == self.user2_card["card"]["id"] and c["print"] == self.user2_card["print"]), None)

                    if seq1_match_idx is not None and seq2_match_idx is not None:
                        c1 = inv1.pop(seq1_match_idx)
                        c2 = inv2.pop(seq2_match_idx)
                        inv1.insert(0, c2)  # receiver gets new card newest-first
                        inv2.insert(0, c1)
            except Exception as e:
                print("TRADE FINALIZE ERROR:", e)

            embed = discord.Embed(color=THEME_COLOR)
            embed.title = "Trade Completed!"

            user1_name = self.user1_card["card"].get("name", "Unknown") if self.user1_card else "Nothing"
            user2_name = self.user2_card["card"].get("name", "Unknown") if self.user2_card else "Nothing"

            if self.trade_id in active_trades:
                del active_trades[self.trade_id]

            embed.description = (
                f"{self.user1.mention} received **{user2_name}**\n!"
                f"{self.user2.mention} received **{user1_name}**!"
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

    async def on_message(self, message):
        # Ignore bot's own messages
        if message.author == self.user:
            return

        content = message.content.strip()
        content_lower = content.lower()
        user_id = message.author.id
        inv = get_inventory(user_id)

        # =========================
        # LUPDATEIMAGE COMMAND
        # =========================
        if content_lower.startswith("lupdateimage "):
            # Check if user has "uploader" role
            if not any(role.name.lower() == "uploader" for role in message.author.roles):
                return await message.channel.send("You need the **Uploader** role to use this command.")

            parts = content.split()
            if len(parts) < 2:
                return await message.channel.send("Usage: `lupdateimage <card_id>`")

            card_id = parts[1]

            # Find the card
            card = next((c for c in cards if c["id"] == card_id), None)
            if not card:
                return await message.channel.send(f"Card with ID `{card_id}` not found.")

            # Check for attachments
            if not message.attachments:
                return await message.channel.send("Please attach an image to update.")

            attachment = message.attachments[0]

            # The image field in cards.json is the single source of truth
            # for this card's filename/path. Never invent or fall back to
            # a generated path -- if it's missing or invalid, stop here.
            existing_path = card.get("image", "") or ""
            if not existing_path.startswith("card_art/"):
                return await message.channel.send(
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

                await message.channel.send(
                    f"✅ Card `{card_id}` image updated successfully and pushed to GitHub!\nNew path: `{save_path}`"
                )
            except Exception as e:
                await message.channel.send(f"❌ Error updating image: {e}")
            return

        # =========================
        # LADDCARD COMMAND
        # =========================
        if content_lower.startswith("laddcard "):
            # Check if user has "uploader" role
            if not any(role.name.lower() == "uploader" for role in message.author.roles):
                return await message.channel.send("You need the **Uploader** role to use this command.")

            # Parse the command: laddcard "Name" | "Series" | frame | stars
            try:
                args = content[9:].strip()  # Remove 'laddcard '
                parts = [p.strip().strip('"') for p in args.split('|')]

                if len(parts) < 4:
                    return await message.channel.send("Usage: `laddcard \"Name\" | \"Series\" | frame | stars`\nExample: `laddcard \"Ivan\" | \"Alien Stage\" | common | 4`")

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
                    return await message.channel.send(
                        f"❌ Frame `{requested_frame}` not found in the `frames` folder. "
                        "Use `common` or the exact name of an existing frame file (with or without `.png`)."
                    )

                frame_name = candidate
                is_rare = (frame_name.lower() != "common")

                if stars_val not in [1, 2, 3, 4]:
                    return await message.channel.send("Stars must be 1, 2, 3, or 4.")

                # Generate card ID based on rarity (common/rare), not the
                # specific frame color -- e.g. mydei_common / mydei_rare,
                # regardless of whether the rare frame is blue, red, gold, etc.
                card_id = generate_card_id(char_name, is_rare)

                # Ask for image
                await message.channel.send(f"Card ID: `{card_id}`\nNow send the art image for **{char_name}**.")

                # Wait for image attachment using the client wait_for
                def check(m):
                    return m.author == message.author and len(m.attachments) > 0 and m.channel == message.channel

                try:
                    img_msg = await self.wait_for('message', check=check, timeout=300)
                except asyncio.TimeoutError:
                    return await message.channel.send("❌ Image upload timed out. Card creation cancelled.")

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

                        _atomic_write_bytes(save_path, image_data)

                        # Create the card object
                        new_card = {
                            "id": card_id,
                            "name": char_name,
                            "series": series,
                            "stars": stars_val,
                            "weight": 10,
                            "image": save_path,
                            "frame": frame_name
                        }

                        # Add to cards list and save
                        cards.append(new_card)
                        save_cards_json()

                    await message.channel.send(f"✅ Card created successfully!\n**ID:** `{card_id}`\n**Name:** {char_name}\n**Series:** {series}\n**Stars:** {stars_val}\n**Frame:** {frame_name}")
                except Exception as e:
                    await message.channel.send(f"❌ Error creating card: {e}")

            except Exception as e:
                await message.channel.send(f"❌ Error parsing command: {e}")
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

            await message.channel.send(embed=embed)
            return

        # =========================
        # LEDITCARD COMMAND
        # =========================
        if content_lower.startswith("leditcard "):
            if not has_uploader_role(message.author):
                return await message.channel.send("You need the **Uploader** role to use this command.")

            parts = content.split()
            if len(parts) < 2:
                return await message.channel.send("Usage: `leditcard <card_id>`")

            card_id = parts[1]
            card = next((c for c in cards if c.get("id") == card_id), None)
            if not card:
                return await message.channel.send(f"Card with ID `{card_id}` not found.")

            view = EditCardView(self, card, message.author, user_id)
            sent = await message.channel.send(embed=view.build_embed(), view=view)
            view.message = sent
            return

        # =========================
        # LREMOVECARD COMMAND
        # =========================
        if content_lower.startswith("lremovecard "):
            if not has_uploader_role(message.author):
                return await message.channel.send("You need the **Uploader** role to use this command.")

            parts = content.split()
            if len(parts) < 2:
                return await message.channel.send("Usage: `lremovecard <card_id>`")

            card_id = parts[1]
            card = next((c for c in cards if c.get("id") == card_id), None)
            if not card:
                return await message.channel.send(f"Card with ID `{card_id}` not found.")

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
            embed = discord.Embed(
                color=THEME_COLOR,
                title="📖 Luka Commands Helper",
                description=(
                    "### 𝗖𝗮𝗿𝗱𝘀\n"
                    "`ld` ─ Drop 2 random cards.\n"
                    "`lv <number>` ─ View a card.\n"
                    "`lc` ─ Look through your collection.\n"
                    "`lup <name/series>` ─ Search a character or series.\n\n"
                    "### 𝗧𝗿𝗮𝗱𝗶𝗻𝗴\n"
                    "`lg` / `lgift` ─ Gift one of your cards to another player.\n"
                    "`lt` / `ltrade` ─ Trade cards with another player.\n\n"
                    "### 𝗢𝘁𝗵𝗲𝗿\n"
                    "`lcd` ─ Check your current cooldowns."
                )
            )

            await message.channel.send(embed=embed)
            return

        # =========================
        # COOLDOWNS COMMAND (lcd)
        # =========================
        if content_lower == "lcd":
            now = time.time()

            def _cooldown_status(seconds_remaining, ready_text):
                if seconds_remaining <= 0:
                    return f"**{ready_text}**"
                minutes = seconds_remaining // 60
                secs = seconds_remaining % 60
                if minutes > 0:
                    return f"**{minutes}m {secs}s remaining**"
                else:
                    return f"**{secs}s remaining**"

            # Drop status
            if user_id in drop_cooldowns:
                remaining = int(DROP_COOLDOWN - (now - drop_cooldowns[user_id]))
            else:
                remaining = 0
            drop_status = _cooldown_status(remaining, "Ready to drop!")

            # Claim status
            if user_id in claim_cooldowns:
                remaining = int(CLAIM_COOLDOWN - (now - claim_cooldowns[user_id]))
            else:
                remaining = 0
            claim_status = _cooldown_status(remaining, "Ready to claim!")

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
            embed.set_thumbnail(
                url="https://media.discordapp.net/attachments/1505599262120087633/1521878802123198634/IMG_9608.jpg?ex=6a466f95&is=6a451e15&hm=cd768ebbfc75ea69ea3a4940c1a57b709bd32b4d9aeaac0ffebb4df475e6ec93&=&format=webp&width=1148&height=666"
            )
            embed.set_footer(text=f"Checked at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))} UTC")

            await message.channel.send(embed=embed)
            return

        # =========================
        # INVENTORY COMMAND (lc)
        # =========================
        if content_lower.startswith("lc"):
            target_user = message.author
            args = content[2:].strip()

            if message.reference and message.reference.resolved:
                replied_msg = message.reference.resolved
                if replied_msg and replied_msg.author:
                    target_user = replied_msg.author

            elif args:
                first_part = args.split()[0]
                if first_part.isdigit():
                    member = message.guild.get_member(int(first_part))
                    if member:
                        target_user = member
                        args = args[len(first_part):].strip()

            target_inv = get_inventory(target_user.id)

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

            user_viewing_inventory[user_id] = target_user.id

            view = InventoryView(
                target_user,
                filtered_inventory,
                viewer_id=message.author.id
            )

            await message.channel.send(
                embed=view.get_embed(),
                view=view
            )
            return

        # =========================
        # GIFT COMMAND (lg / lgift)
        # =========================
        if content_lower.startswith(("lgift ", "lg ")):
            if not message.mentions:
                return await message.channel.send(
                    "Usage: `lgift @user <inventory number>`"
                )

            target_user = message.mentions[0]

            if target_user.bot:
                return await message.channel.send(
                    "You can't gift cards to bots."
                )

            if target_user.id == message.author.id:
                return await message.channel.send(
                    "You can't gift cards to yourself."
                )

            parts = message.content.split()

            try:
                requested_num = int(parts[-1])
            except:
                return await message.channel.send(
                    "Please provide a valid inventory number."
                )

            # Displayed numbers count down from newest (highest) to oldest
            # (1), so convert back to a list index accordingly.
            card_index = len(inv) - requested_num

            if card_index < 0 or card_index >= len(inv):
                return await message.channel.send(
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
                await message.channel.send(
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
                await message.channel.send(
                    content=f"{message.author.mention} is gifting {target_user.mention} a card!",
                    embed=gift_embed,
                    view=view
                )

            return

        # =========================
        # TRADE COMMAND (lt / ltrade)
        # =========================
        if content_lower.startswith(("ltrade ", "lt")):
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
                return await message.channel.send(
                    "Usage: `lt @user` (reply to their message or mention them)"
                )

            if target_user.bot:
                return await message.channel.send(
                    "You can't trade with bots."
                )

            if target_user.id == message.author.id:
                return await message.channel.send(
                    "You can't trade with yourself."
                )

            if len(inv) == 0:
                return await message.channel.send(
                    "You don't have any cards to trade."
                )

            target_inv = get_inventory(target_user.id)
            if len(target_inv) == 0:
                return await message.channel.send(
                    f"{target_user.mention} doesn't have any cards to trade."
                )

            request_view = TradeRequestView(
                message.author,
                target_user,
                message.author.id,
                target_user.id
            )

            await message.channel.send(
                embed=request_view.get_embed(),
                view=request_view
            )

            return

        # =========================
        # ADD CARD TO TRADE (add <card_number>)
        # =========================
        if content_lower.startswith("add "):
            try:
                raw = content.split()[1]
                try:
                    requested_num = int(raw)
                except:
                    return await message.channel.send("Usage: `add <card_number>` (use the number shown in your inventory)")

                # Find the user's active trade view
                user_trade = None
                for trade_id, trade_data in active_trades.items():
                    parts = trade_id.split('_')
                    if str(user_id) in parts and trade_data.get("view"):
                        user_trade = trade_data["view"]
                        break

                if not user_trade:
                    return await message.channel.send("You're not in an active trade.")

                inv_list = get_inventory(user_id)
                # Displayed numbers count down from newest (highest) to
                # oldest (1), so convert back to a list index accordingly.
                pos_idx = len(inv_list) - requested_num
                if pos_idx < 0 or pos_idx >= len(inv_list):
                    return await message.channel.send("Invalid card number.")

                owned_card = inv_list[pos_idx]
                card_index = pos_idx

                # Assign to trade by index
                if user_id == user_trade.user1_id:
                    user_trade.user1_card = owned_card
                    user_trade.user1_card_index = card_index
                elif user_id == user_trade.user2_id:
                    user_trade.user2_card = owned_card
                    user_trade.user2_card_index = card_index
                else:
                    return await message.channel.send("You're not part of this trade.")

                if user_trade.user1_card and user_trade.user2_card:
                    user_trade.stage = "locking"

                # Update the trade message immediately (if stored)
                try:
                    trade_msg = active_trades[user_trade.trade_id].get("message")
                    if trade_msg:
                        await trade_msg.edit(embed=user_trade.build_embed(), view=user_trade)
                except Exception:
                    pass

                return
            except Exception as e:
                return await message.channel.send(f"Error: {e}")

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
                return await message.channel.send("Invalid card number.")

            name = card.get("name", "Unknown Character")
            series = card.get("series", "Unknown Series")
            star_val = card.get("stars", 1)

            embed = discord.Embed(color=THEME_COLOR)
            embed.set_author(name=f"{message.author.name}'s Card", icon_url=message.author.display_avatar.url)
            embed.description = (
                f"## **{name}**\n"
                f"✦ **Series:** **{series}**\n"
                f"───\n"
                f"✦ **Owner:** <@{viewing_user_id}>\n"
                f"✦ **Print:** **{format_print(print_num)}**\n"
                f"✦ **Level:** **{stars(star_val)}**\n"
            )
            image_path = render_card_final(card, print_num)

            if image_path:
                file = discord.File(image_path, filename="card.png")
                embed.set_image(url="attachment://card.png")
                await message.channel.send(embed=embed, file=file)
                try:
                    os.remove(image_path)
                except:
                    pass
            else:
                await message.channel.send(embed=embed)

            return

        # =========================
        # LOOKUP COMMAND (lup <query>)
        # =========================
        if content_lower.startswith("lup "):
            query = content[4:].strip().lower()
            if not query:
                return await message.channel.send(
                    "Please provide a name or a number to search."
                )

            # If user sent a number selection after a previous search
            if query.isdigit():
                if user_id not in user_last_lookup:
                    return await message.channel.send(
                        "You haven't searched for anything yet! Search using a name first."
                    )

                selection = int(query) - 1
                previous_results = user_last_lookup[user_id]

                if selection < 0 or selection >= len(previous_results):
                    return await message.channel.send(
                        "Invalid number selection from your last search."
                    )

                chosen_card = previous_results[selection]

                all_versions = [
                    c for c in cards
                    if c.get("name", "").lower() == chosen_card.get("name", "").lower()
                ]
                all_versions.sort(key=lambda x: x.get("stars", 1))

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

                await message.channel.send(
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
                return await message.channel.send("No cards found.")

            # collapse to unique names for list view
            unique_results = []
            seen_names = set()

            for card in matched_cards:
                card_name_lower = card.get("name", "").lower()
                if card_name_lower not in seen_names:
                    seen_names.add(card_name_lower)
                    unique_results.append(card)

            user_last_lookup[user_id] = unique_results

            # If only one unique result, show the card directly
            if len(unique_results) == 1:
                all_versions = [
                    c for c in cards
                    if c.get("name", "").lower() == unique_results[0].get("name", "").lower()
                ]
                all_versions.sort(key=lambda x: x.get("stars", 1))

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

                await message.channel.send(
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
            return await message.channel.send(
                embed=view.get_embed(),
                view=view
            )

        # =========================
        # DROP CARDS COMMAND (ld)
        # =========================
        if content_lower == "ld":
            now = time.time()

            if user_id in drop_cooldowns:
                remaining = int(DROP_COOLDOWN - (now - drop_cooldowns[user_id]))

                if remaining > 0:
                    return await message.channel.send(
                        f"⏳ You must wait **{format_time(remaining)}** before dropping again."
                    )

            card1 = get_weighted_card()
            card2 = get_weighted_card()

            while card2["id"] == card1["id"]:
                card2 = get_weighted_card()

            drop_cooldowns[user_id] = now

            loop = asyncio.get_running_loop()

            image_path = await loop.run_in_executor(
                None,
                render_drop,
                card1,
                peek_next_print(card1["id"]),
                card2,
                peek_next_print(card2["id"])
            )

            if image_path is None:
                return await message.channel.send(
                    "❌ Failed to render the drop."
                )

            file = discord.File(
                image_path,
                filename="drop.png"
            )

            view = CardView(card1, card2)

            await message.channel.send(
                content=f"{message.author.mention} is dropping 2 cards!",
                file=file,
                view=view
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
                return await message.channel.send("Usage: lfindcard <card name>")

            # try exact match then substring
            card = next((c for c in cards if c.get("name", "").lower() == query), None)
            if not card:
                card = next((c for c in cards if query in c.get("name", "").lower()), None)

            if not card:
                return await message.channel.send("Card not found.")

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

            await message.channel.send(embed=embed, file=file, view=view)

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
                return await message.channel.send("Usage: lfindseries <series name>")

            # try exact match then substring
            matched_cards = [c for c in cards if c.get("series", "").lower() == query]
            if not matched_cards:
                matched_cards = [c for c in cards if query in c.get("series", "").lower()]

            if not matched_cards:
                return await message.channel.send("No cards found for that series.")

            matched_cards.sort(key=lambda c: (c.get("name", "").lower(), c.get("stars", 1)))

            series_display = matched_cards[0].get("series", query)

            view = FindSeriesView(series_display, matched_cards, message.author, user_id)

            return await message.channel.send(
                embed=view.get_embed(),
                view=view
            )


# --- Run Bot Connection ---
client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
