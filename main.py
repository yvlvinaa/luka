import os
import discord
import random
import time
import asyncio
import requests
import tempfile
import json
from io import BytesIO
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

# =========================
# CARD RENDERING SETTINGS
# =========================

PRINT_SETTINGS = {
    "common": {
        "print_x": 1000,
        "print_y": 355,
        "font_size": 85
    },
    "rare": {
        "print_x": 355,
        "print_y": 300,
        "font_size": 85
    }
}

# Text positioning for card name and series (1536x2048)
TEXT_SETTINGS = {
    "common": {
        "name_y": 1800,
        "series_y": 1900,
        "name_size": 60,
        "series_size": 48,
        "center_x": 768
    },
    "rare": {
        "name_y": 1800,
        "series_y": 1900,
        "name_size": 60,
        "series_size": 48,
        "center_x": 768
    }
}

PRINT_FONT = "Fredoka-Bold.ttf"
TEXT_FONT = "Fredoka-Bold.ttf"
TEXT_COLOR = (255, 255, 255)
TEXT_STROKE_WIDTH = 2
TEXT_STROKE_COLOR = (0, 0, 0)

# Initialize intents
intents = discord.Intents.all()

# Global Configurations
DROP_COOLDOWN = 600
CLAIM_COOLDOWN = 300
CARDS_PER_PAGE = 10
THEME_COLOR = discord.Color.from_rgb(255, 227, 102)

# Global tracking for lookup history sessions
user_last_lookup = {}

# Global tracking for trade and gift sessions
active_trades = {}
active_gifts = {}
user_viewing_inventory = {}

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


def clean_url(url):
    """Cleans GitHub URLs to point to raw image assets."""
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url.split("?")[0]


def get_image(url):
    """Downloads an image over HTTP and returns a PIL Image object."""
    try:
        # If it's a local file path, load from disk
        if url.startswith("card_art/"):
            if os.path.exists(url):
                return Image.open(url).convert("RGBA")
            else:
                print(f"LOCAL IMAGE ERROR: {url} not found")
                return Image.new("RGBA", (400, 560), (80, 80, 80, 255))

        # Otherwise download from URL
        url = clean_url(url)
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        return Image.open(BytesIO(response.content)).convert("RGBA")
    except Exception as e:
        print("IMAGE ERROR:", url, e)
        return Image.new("RGBA", (400, 560), (80, 80, 80, 255))


def format_print(print_num):
    """Formats print number for display."""
    if print_num < 100:
        return f"#{print_num}"
    if print_num == 100:
        return "#100"
    if print_num > 100:
        return "L"


def create_vertical_gradient(width, height, color=(0,0,0), start_alpha=0, end_alpha=150):
    """
    Create a vertical gradient image (transparent -> color with alpha).
    color is an (r,g,b) tuple. Alpha range 0..255.
    """
    base = Image.new('RGBA', (width, height), (0,0,0,0))
    draw = ImageDraw.Draw(base)
    for y in range(height):
        alpha = int(start_alpha + (end_alpha - start_alpha) * (y / max(1, height-1)))
        draw.line([(0,y),(width,y)], fill=(color[0], color[1], color[2], alpha))
    return base


def average_color_of_image(img):
    """Return the average color (r,g,b) of non-transparent pixels in img (PIL RGBA)."""
    try:
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        arr = img.copy().convert("RGB")
        stat = ImageStat.Stat(arr)
        r, g, b = [int(x) for x in stat.mean]
        return (r, g, b)
    except Exception:
        return (0,0,0)


def save_cards_json():
    """Saves the cards list to cards.json"""
    with open('cards.json', 'w') as f:
        json.dump(cards, f, indent=2)


def generate_card_id(character_name, frame_type):
    """
    Generates a card ID automatically.
    Format: name_frame or name_frame_2, name_frame_3, etc.
    """
    base_id = f"{character_name.lower().replace(' ', '_')}_{frame_type}"

    # Count how many cards with this base ID already exist
    count = sum(1 for card in cards if card["id"].startswith(base_id))

    if count == 0:
        return base_id
    else:
        return f"{base_id}_{count + 1}"


# =========================
# RENDERING
# =========================

def render_card_final(card, print_num):
    """
    Renders the final card image with a gradient shadow and robust fallbacks.
    Always returns a temp PNG path.
    """
    try:
        frame_name = card.get("frame", "common")
        frame_path = f"frames/{frame_name}.png"

        if os.path.exists(frame_path):
            frame = Image.open(frame_path).convert("RGBA")
        else:
            print(f"FRAME NOT FOUND: {frame_path} - using placeholder")
            frame = Image.new("RGBA", (1536, 2048), (40, 40, 40, 255))
            draw_f = ImageDraw.Draw(frame)
            draw_f.rounded_rectangle([30,30,1506,2018], radius=24, outline=(180,180,180,255), width=10)

        try:
            resample_filter = Image.Resampling.LANCZOS
        except AttributeError:
            resample_filter = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS

        char_image = get_image(card.get("image", ""))
        char_image = char_image.resize((400, 560), resample_filter)

        base = Image.new("RGBA", frame.size, (0,0,0,0))
        art_x, art_y = 568, 0
        base.paste(char_image, (art_x, art_y), char_image)

        try:
            avg_color = average_color_of_image(frame)
            if abs(avg_color[0] - avg_color[1]) < 10 and abs(avg_color[1] - avg_color[2]) < 10:
                gradient_color = (0,0,0)
            else:
                gradient_color = avg_color
        except:
            gradient_color = (0,0,0)

        gradient = create_vertical_gradient(400, 560, color=gradient_color, start_alpha=0, end_alpha=140)
        base.paste(gradient, (art_x, art_y), gradient)

        if frame.size != base.size:
            frame = frame.resize(base.size, resample_filter)
        final = Image.alpha_composite(base, frame)

        draw = ImageDraw.Draw(final)
        print_settings = PRINT_SETTINGS.get(frame_name if frame_name in PRINT_SETTINGS else "common")

        try:
            print_font = ImageFont.truetype(PRINT_FONT, print_settings["font_size"])
        except:
            print_font = ImageFont.load_default()

        draw.text(
            (print_settings["print_x"], print_settings["print_y"]),
            format_print(print_num),
            font=print_font,
            fill=(255,255,255),
            stroke_width=TEXT_STROKE_WIDTH,
            stroke_fill=TEXT_STROKE_COLOR
        )

        text_settings = TEXT_SETTINGS.get(frame_name if frame_name in TEXT_SETTINGS else "common")
        try:
            name_font = ImageFont.truetype(TEXT_FONT, text_settings["name_size"])
            series_font = ImageFont.truetype(TEXT_FONT, text_settings["series_size"])
        except:
            name_font = ImageFont.load_default()
            series_font = ImageFont.load_default()

        name = card.get("name", "Unknown")
        draw.text(
            (text_settings["center_x"], text_settings["name_y"]),
            name,
            font=name_font,
            fill=TEXT_COLOR,
            stroke_width=TEXT_STROKE_WIDTH,
            stroke_fill=TEXT_STROKE_COLOR,
            anchor="mm"
        )

        series = card.get("series", "Unknown Series")
        draw.text(
            (text_settings["center_x"], text_settings["series_y"]),
            series,
            font=series_font,
            fill=TEXT_COLOR,
            stroke_width=TEXT_STROKE_WIDTH,
            stroke_fill=TEXT_STROKE_COLOR,
            anchor="mm"
        )

        if frame_name == "common" and card.get("stars", 1) < 4:
            star_count = card.get("stars", 1)
            star_path = f"stars/star_{star_count}.png"
            if os.path.exists(star_path):
                star_overlay = Image.open(star_path).convert("RGBA")
                final.paste(star_overlay, (0, 0), star_overlay)

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        final.save(temp_file.name)
        return temp_file.name
    except Exception as e:
        print("RENDER ERROR:", e)
        fallback = Image.new("RGBA", (1536,2048), (30,30,30,255))
        d = ImageDraw.Draw(fallback)
        try:
            fnt = ImageFont.truetype(TEXT_FONT, 40)
        except:
            fnt = ImageFont.load_default()
        d.text((50,50), f"Render Error: {str(e)[:200]}", font=fnt, fill=(255,255,255))
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        fallback.save(temp_file.name)
        return temp_file.name


# =========================
# 1. DROP VIEW
# =========================
class CardView(discord.ui.View):
    def __init__(self, card1, card2):
        super().__init__(timeout=30)
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
        for idx, owned_card in enumerate(cards_page, start=start + 1):
            card = owned_card["card"]
            name = card.get("name", "Unknown")
            series = card.get("series", "Unknown Series")
            star_val = card.get("stars", 1)
            print_num = owned_card["print"]

            text += (
                f"`{idx:02d}` ✦ "
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

    def get_embed(self):
        card = self.versions[self.index]

        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name=f"{self.user.name}'s Search",
            icon_url=self.user.display_avatar.url
        )

        claims = card_prints.get(card.get("id"), 0)

        embed.description = (
            f"## **{card.get('name', 'Unknown')}**\n"
            f"✦ **Series:** **{card.get('series', 'Unknown')}**\n"
            f"✦ **Claims:** **{claims}**\n"
            f"───\n"
            f"✦ **Level:** **{stars(card.get('stars', 1))}**\n"
        )

        image_url = clean_url(card.get("image", ""))
        if image_url and (image_url.startswith("http") or image_url.startswith("card_art/")):
            embed.set_image(url=image_url)
        
        embed.set_footer(text=f"Version {self.index + 1}/{len(self.versions)}")

        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        if self.index > 0:
            self.index -= 1

        await interaction.response.edit_message(
            embed=self.get_embed(),
            view=self
        )

    @discord.ui.button(emoji="🔍", style=discord.ButtonStyle.secondary)
    async def owners(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        card = self.versions[self.index]
        owners_list = []

        for owner_id, inventory in inventories.items():
            for owned_card in inventory:
                if owned_card["card"]["id"] == card["id"]:
                    owners_list.append((owned_card["print"], owner_id))

        owners_list.sort(key=lambda x: x[0])

        embed = discord.Embed(color=THEME_COLOR)
        embed.title = f"{card['name']} - Owners"

        if not owners_list:
            embed.description = "Nobody owns this card yet."
        else:
            lines = []
            guild = interaction.guild

            for print_num, owner_id in owners_list:
                member = guild.get_member(owner_id)

                if member is None:
                    try:
                        member = await guild.fetch_member(owner_id)
                    except:
                        continue

                lines.append(
                    f"`{format_print(print_num)}.` {member.mention}"
                )

            if lines:
                embed.description = "\n".join(lines)
            else:
                embed.description = "Nobody owns this card yet."

        await interaction.response.send_message(
            embed=embed,
            ephemeral=False
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your search!",
                ephemeral=True
            )

        if self.index < len(self.versions) - 1:
            self.index += 1

        await interaction.response.edit_message(
            embed=self.get_embed(),
            view=self
        )

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

    def get_embed(self):
        card = self.versions[self.index]
        cid = card.get("id", "unknown")
        name = card.get("name", "Unknown")
        series = card.get("series", "Unknown Series")
        stars_val = card.get("stars", 1)
        frame = card.get("frame", "common")
        claims = card_prints.get(cid, 0)

        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(name="Card Information", icon_url=self.user.display_avatar.url)

        embed.description = (
            f"## **{name}**\n"
            f"✦ **Series:** **{series}**\n"
            f"✦ **ID:** `{cid}`\n"
            f"✦ **Stars:** {stars(stars_val)}\n"
            f"✦ **Frame:** **{frame}**\n"
            f"✦ **Claims:** **{claims}**\n"
        )

        # Only set thumbnail if URL is valid
        image_url = clean_url(card.get("image", ""))
        if image_url and (image_url.startswith("http") or image_url.startswith("card_art/")):
            embed.set_thumbnail(url=image_url)
        
        embed.set_footer(text=f"Version {self.index + 1}/{len(self.versions)}")

        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1

        await interaction.response.edit_message(
            embed=self.get_embed(),
            view=self
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.versions) - 1:
            self.index += 1

        await interaction.response.edit_message(
            embed=self.get_embed(),
            view=self
        )

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
            os.remove(image_path)
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

        await interaction.response.edit_message(
            content=None,
            embed=accepted_embed,
            view=None,
            attachments=[] if not file else [file]
        )

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
        embed.title = "## Trade In Progress"

        if self.stage == "selecting":
            user1_status = "Waiting for selection"
            user2_status = "Waiting for selection"
        elif self.stage == "locking":
            user1_status = "Pending" if not self.user1_locked else "Confirming"
            user2_status = "Pending" if not self.user2_locked else "Confirming"
        elif self.stage == "confirming":
            user1_status = "Completed!" if self.user1_confirmed else "Completing"
            user2_status = "Completed!" if self.user2_confirmed else "Completing"

        user1_text = f"**<:Bluka:1511044685781663866> {self.user1.mention} is offering.. - {user1_status}**\n"
        if self.user1_card:
            card = self.user1_card["card"]
            name = card.get("name", "Unknown")
            series = card.get("series", "Unknown Series")
            print_num = self.user1_card["print"]
            card_num = (self.user1_card_index + 1) if self.user1_card_index is not None else "?"
            user1_text += f"`({card_num})` • **{name}** • *{series}* • `{format_print(print_num)}`\n"

        user2_text = f"**<:Bluka:1511044685781663866> {self.user2.mention} is offering.. - {user2_status}**\n"
        if self.user2_card:
            card = self.user2_card["card"]
            name = card.get("name", "Unknown")
            series = card.get("series", "Unknown Series")
            print_num = self.user2_card["print"]
            card_num = (self.user2_card_index + 1) if self.user2_card_index is not None else "?"
            user2_text += f"`({card_num})` • **{name}** • *{series}* • `{format_print(print_num)}`\n"

        embed.description = user1_text + "\n────────────────────────\n" + user2_text

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
                f"{self.user1.mention} received **{user2_name}**\n"
                f"{self.user2.mention} received **{user1_name}**"
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

            # Download and save the image
            try:
                image_data = await attachment.read()
                file_ext = attachment.filename.split('.')[-1]
                save_path = f"card_art/{card_id}.{file_ext}"

                with open(save_path, 'wb') as f:
                    f.write(image_data)

                # Update the card's image field
                card["image"] = save_path
                save_cards_json()

                await message.channel.send(f"✅ Card `{card_id}` image updated successfully!\nNew path: `{save_path}`")
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
                requested_frame = parts[2].lower()
                stars_val = int(parts[3])

                # Frame resolution logic
                frame_name = None
                frames_dir = "frames"
                if requested_frame == "common":
                    frame_name = "common"
                elif requested_frame == "rare":
                    try:
                        files = [f for f in os.listdir(frames_dir) if f.lower().startswith("rare")]
                        if not files:
                            return await message.channel.send("No rare frames found on disk.")
                        chosen = random.choice(files)
                        frame_name = os.path.splitext(chosen)[0]
                    except Exception:
                        return await message.channel.send("Error listing frames directory.")
                else:
                    candidate = requested_frame
                    candidate_path = os.path.join(frames_dir, f"{candidate}.png")
                    if not os.path.exists(candidate_path):
                        return await message.channel.send(f"Frame `{candidate}` not found. Use `common`, `rare`, or an exact frame filename without extension.")
                    frame_name = candidate

                if stars_val not in [1, 2, 3, 4]:
                    return await message.channel.send("Stars must be 1, 2, 3, or 4.")

                # Generate card ID using the exact frame_name for uniqueness
                card_id = generate_card_id(char_name, frame_name)

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
                    file_ext = attachment.filename.split('.')[-1]
                    save_path = f"card_art/{card_id}.{file_ext}"

                    with open(save_path, 'wb') as f:
                        f.write(image_data)

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
        # COOLDOWNS COMMAND (lcd)
        # =========================
        if content_lower == "lcd":
            now = time.time()
            drop_text = "✨ You can drop your card now!"
            claim_text = "✨ You can claim a card now!"

            if user_id in drop_cooldowns:
                remaining = int(DROP_COOLDOWN - (now - drop_cooldowns[user_id]))
                if remaining > 0:
                    drop_text = f"⏳ `{format_time(remaining)}` before you can drop"

            if user_id in claim_cooldowns:
                remaining = int(CLAIM_COOLDOWN - (now - claim_cooldowns[user_id]))
                if remaining > 0:
                    claim_text = f"⏳ `{format_time(remaining)}` before you can claim"

            embed = discord.Embed(color=THEME_COLOR)
            embed.set_author(name=f"{message.author.name}'s Cooldowns", icon_url=message.author.display_avatar.url)
            embed.description = (
                f"## Drop\n"
                f"{drop_text}\n\n"
                f"## Claim\n"
                f"{claim_text}"
            )
            return await message.channel.send(embed=embed)

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
            filtered_inventory = target_inv[:]

            args_lower = args.lower()

            if "s:" in args_lower:
                series_query = args_lower.split("s:", 1)[1].strip()
                filtered_inventory = [
                    owned_card for owned_card in filtered_inventory
                    if series_query in owned_card["card"].get("series", "").lower()
                ]

            elif "c:" in args_lower:
                char_query = args_lower.split("c:", 1)[1].strip()
                filtered_inventory = [
                    owned_card for owned_card in filtered_inventory
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
                card_index = int(parts[-1]) - 1
            except:
                return await message.channel.send(
                    "Please provide a valid inventory number."
                )

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

            await message.channel.send(
                content=f"{message.author.mention} is gifting {target_user.mention} a card!",
                embed=gift_embed,
                file=file if file else None,
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
                pos_idx = requested_num - 1
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
                index = int(content_lower.split()[1]) - 1

                viewing_user_id = user_viewing_inventory.get(user_id, user_id)
                target_inv = get_inventory(viewing_user_id)

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
                os.remove(image_path)
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
                return await message.channel.send(
                    embed=view.get_embed(),
                    view=view
                )

            matched_cards = [
                card for card in cards
                if (
                    query in card.get("name", "").lower()
                    or query in card.get("series", "").lower()
                )
            ]

            if not matched_cards:
                return await message.channel.send("No cards found.")

            unique_results = []
            seen_names = set()

            for card in matched_cards:
                card_name_lower = card.get("name", "").lower()
                if card_name_lower not in seen_names:
                    seen_names.add(card_name_lower)
                    unique_results.append(card)

            user_last_lookup[user_id] = unique_results

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
                return await message.channel.send(
                    embed=view.get_embed(),
                    view=view
                )

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
                    return await message.channel.send(f"Wait {format_time(remaining)} before dropping again.")

            card1 = get_weighted_card()
            card2 = get_weighted_card()

            while card1 == card2:
                card2 = get_weighted_card()

            drop_cooldowns[user_id] = now

            loop = asyncio.get_event_loop()
            image_path = await loop.run_in_executor(
                None,
                render_card_final,
                card1,
                peek_next_print(card1["id"])
            )

            if image_path:
                file = discord.File(image_path, filename="drop.png")
                view = CardView(card1, card2)

                await message.channel.send(
                    content=f"{message.author.mention} is dropping 2 cards!",
                    file=file,
                    view=view
                )
                os.remove(image_path)
            else:
                await message.channel.send(f"{message.author.mention} Error rendering card.")
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

            # Find all versions of this card
            all_versions = [
                c for c in cards
                if c.get("name", "").lower() == card.get("name", "").lower()
            ]
            all_versions.sort(key=lambda x: x.get("stars", 1))

            if len(all_versions) == 1:
                # Only one version, show it without navigation buttons
                view = FindcardVersionView(all_versions, message.author, user_id)
                return await message.channel.send(embed=view.get_embed(), view=view)
            else:
                # Multiple versions, show with navigation buttons
                view = FindcardVersionView(all_versions, message.author, user_id)
                return await message.channel.send(embed=view.get_embed(), view=view)

# --- Run Bot Connection ---
import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
