import discord
import random
import time
import asyncio
import requests
import tempfile

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

# Import your database and card assets
from cards import cards
from data import (
    inventories,
    drop_cooldowns,
    claim_cooldowns,
    card_prints
)

# =========================

# PRINT SETTINGS

# =========================

PRINT_SETTINGS = {
    "common": {
        "print_x": 1000,
        "print_y": 335,
        "font_size": 85
    },

    "rare": {
        "print_x": 355,
        "print_y": 300,
        "font_size": 85
    }

}

# Font (Fredoka SemiBold)

PRINT_FONT = "Fredoka-SemiBold.ttf"

# Text styling

PRINT_FILL = "white"
PRINT_STROKE_FILL = "black"
PRINT_STROKE_WIDTH = 3

def get_frame_type(card):
    # 1⭐, 2⭐, 3⭐ = common frame
    # 4⭐ = rare frame
    return "rare" if card.get("stars", 1) == 4 else "common"

def format_print(print_num):
    # #1 - #99
    if print_num < 100:
        return f"#{print_num}"

    # 100
    if print_num == 100:
        return "#100"

    # 101+
    if print_num > 100:
        return "L"

def render_card_with_print(card, print_num):
    frame_type = get_frame_type(card)
    settings = PRINT_SETTINGS[frame_type]

    response = requests.get(card["image"])
    image = Image.open(BytesIO(response.content)).convert("RGBA")
    image = image.resize((1536, 2048))

    # ----- Bottom gradient shadow -----
    gradient_height = 250

    gradient = Image.new("L", (image.width, gradient_height), 0)

    for y in range(gradient_height):
        alpha = int(180 * (y / gradient_height))
        for x in range(image.width):
            gradient.putpixel((x, y), alpha)

    shadow = Image.new("RGBA", (image.width, gradient_height), (0, 0, 0, 255))
    shadow.putalpha(gradient)

    image.paste(
        shadow,
        (0, image.height - gradient_height),
        shadow
    )

    # Recreate draw object after applying the gradient
    draw = ImageDraw.Draw(image)

    font = ImageFont.truetype(
        PRINT_FONT,
        settings["font_size"]
    )

    print("SIZE:", image.size)
    print("PRINT:", format_print(print_num))
    print("X:", settings["print_x"])
    print("Y:", settings["print_y"])
    print("FONT:", settings["font_size"])

    draw.text(
        (settings["print_x"], settings["print_y"]),
        format_print(print_num),
        font=font,
        fill=PRINT_FILL,
        stroke_width=PRINT_STROKE_WIDTH,
        stroke_fill=PRINT_STROKE_FILL 
    )

    temp_file = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".png"
    )

    image.save(temp_file.name)

    return temp_file.name

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
    owned_card = {
        "card": card,
        "print": get_next_print(card["id"])
    }

    get_inventory(user_id).insert(0, owned_card)


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
        url = clean_url(url)
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        return Image.open(BytesIO(response.content)).convert("RGBA")
    except Exception as e:
        print("IMAGE ERROR:", url, e)
        return Image.new("RGBA", (400, 560), (80, 80, 80, 255))


def combine_cards(url1, url2):
    """Merges two cards side-by-side into a single image asset."""
    img1 = get_image(url1).resize((400, 560), Image.Resampling.LANCZOS)
    img2 = get_image(url2).resize((400, 560), Image.Resampling.LANCZOS)

    combined = Image.new("RGBA", (800, 560))
    combined.paste(img1, (0, 0))
    combined.paste(img2, (400, 0))

    path = "drop.png"
    combined.save(path)
    return path


def combine_cards_with_prints(card1, card2):
    print1 = card_prints.get(card1["id"], 0) + 1
    print2 = card_prints.get(card2["id"], 0) + 1

    img1_path = render_card_with_print(card1, print1)
    img2_path = render_card_with_print(card2, print2)

    img1 = Image.open(img1_path).resize((400, 560), Image.Resampling.LANCZOS)
    img2 = Image.open(img2_path).resize((400, 560), Image.Resampling.LANCZOS)

    combined = Image.new("RGBA", (800, 560))
    combined.paste(img1, (0, 0))
    combined.paste(img2, (400, 0))

    path = "drop.png"
    combined.save(path)

    os.remove(img1_path)
    os.remove(img2_path)

    return path


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
        self.user = user              # whose inventory is being viewed
        self.inventory = inventory    # filtered or full inventory
        self.viewer_id = viewer_id    # who opened the view
        self.page = 0

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(
            name=f"{self.user.name}'s Collection",
            icon_url=self.user.display_avatar.url
        )

        start = self.page * CARDS_PER_PAGE
        end = start + CARDS_PER_PAGE
        cards_page = self.inventory[start:end]

        if not cards_page:
            embed.description = "No cards collected."
            return embed

        text = ""
        for i, owned_card in enumerate(cards_page, start=start + 1):
            card = owned_card["card"]

            name = card.get("name", "Unknown")
            series = card.get("series", "Unknown Series")
            star_val = card.get("stars", 1)
            print_num = owned_card["print"]

            text += (
                f"`{i:02d}` ✦ "
                f"• `{format_print(print_num)}` "
                f"• `⭐ {star_val}` "
                f"• **{name}** • *{series}*\n"
            )

        embed.description = text
        total_pages = (len(self.inventory) - 1) // CARDS_PER_PAGE + 1
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages}")
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

        max_page = (len(self.inventory) - 1) // CARDS_PER_PAGE
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
        total_pages = (len(self.results) - 1) // CARDS_PER_PAGE + 1
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

        embed.description = (
            f"## **{card.get('name', 'Unknown')}**\n"
            f"✦ **Series:** **{card.get('series', 'Unknown')}**\n"
            f"───\n"
            f"✦ **Level:** **{stars(card.get('stars', 1))}**\n"
        )

        embed.set_image(url=clean_url(card.get("image", "")))
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
        embed.set_thumbnail(url=clean_url(card.get("image", "")))

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

# 5. GIFT VIEW

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

        image_path = render_card_with_print(card, self.print_num)
        file = discord.File(image_path, filename="card.png")
        embed.set_image(url="attachment://card.png")
        os.remove(image_path)
        return embed, file

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
            view=None
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
            view=None
        )


# =========================

# 6. TRADE REQUEST VIEW

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

        # Proceed to trade
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

# 7. TRADE VIEW

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
        self.stage = "selecting"  # "selecting", "locking" or "confirming"
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
            star_val = card.get("stars", 1)
            print_num = self.user1_card["print"]
            card_num = self.user1_card_index + 1
            user1_text += f"`({card_num})` • **{name}** • *{series}* • `{format_print(print_num)}`\n"

        user2_text = f"**<:Bluka:1511044685781663866> {self.user2.mention} is offering.. - {user2_status}**\n"
        if self.user2_card:
            card = self.user2_card["card"]
            name = card.get("name", "Unknown")
            series = card.get("series", "Unknown Series")
            star_val = card.get("stars", 1)
            print_num = self.user2_card["print"]
            card_num = self.user2_card_index + 1
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
            self.lock.emoji = "✅"

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
            
            embed = discord.Embed(color=THEME_COLOR)
            embed.title = "Trade Completed!"
            
            user1_card = self.user1_card["card"]
            user2_card = self.user2_card["card"]
            
            user1_name = user1_card.get("name", "Unknown")
            user2_name = user2_card.get("name", "Unknown")
            
            embed.description = (
                f"{self.user1.mention} received **{user2_name}**\n"
                f"{self.user2.mention} received **{user1_name}**"
            )
            
            if self.trade_id in active_trades:
                del active_trades[self.trade_id]
            
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

            # -------------------------
            # Reply to someone's message -> view their inventory
            # -------------------------
            if message.reference and message.reference.resolved:
                replied_msg = message.reference.resolved
                if replied_msg and replied_msg.author:
                    target_user = replied_msg.author

            # -------------------------
            # lc <user_id>
            # lc <user_id> s: <series>
            # lc <user_id> c: <character>
            # -------------------------
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

            # -------------------------
            # Series filter
            # lc s: honkai
            # lc 123456789 s: honkai
            # -------------------------
            if "s:" in args_lower:
                series_query = args_lower.split("s:", 1)[1].strip()
                filtered_inventory = [
                    owned_card for owned_card in filtered_inventory
                    if series_query in owned_card["card"].get("series", "").lower()
                ]

            # -------------------------
            # Character filter
            # lc c: sunday
            # lc 123456789 c: sunday
            # -------------------------
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
                file=file,
                view=view
            )

            return

        # =========================
        # TRADE COMMAND (lt / ltrade)
        # =========================
        if content_lower.startswith(("ltrade ", "lt")):
            target_user = None

            # Check for reply first (works for both "lt" and "ltrade @user")
            if message.reference:
                try:
                    replied_msg = await message.channel.fetch_message(message.reference.message_id)
                    if replied_msg and replied_msg.author:
                        target_user = replied_msg.author
                except:
                    pass
            
            # Check for mentions if no reply
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

            # Send trade request embed
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
                card_num = int(content_lower.split()[1]) - 1
            except (IndexError, ValueError):
                return await message.channel.send("Usage: `add <card_number>`")

            if card_num < 0 or card_num >= len(inv):
                return await message.channel.send("Invalid card number.")

            # Find active trade for this user
            user_trade = None
            for trade_id, trade_data in active_trades.items():
                parts = trade_id.split('_')
                if str(user_id) in parts and trade_data.get("view"):
                    user_trade = trade_data["view"]
                    break

            if not user_trade:
                return await message.channel.send("You're not in an active trade.")

            owned_card = inv[card_num]

            if user_id == user_trade.user1_id:
                user_trade.user1_card = owned_card
                user_trade.user1_card_index = card_num
            elif user_id == user_trade.user2_id:
                user_trade.user2_card = owned_card
                user_trade.user2_card_index = card_num
            else:
                return await message.channel.send("You're not part of this trade.")

            # Check if both cards are selected
            if user_trade.user1_card and user_trade.user2_card:
                user_trade.stage = "locking"

            # Update the trade message
            if user_trade.trade_id in active_trades and active_trades[user_trade.trade_id].get("message"):
                try:
                    await active_trades[user_trade.trade_id]["message"].edit(
                        embed=user_trade.build_embed(),
                        view=user_trade
                    )
                except:
                    pass

            return

        # =========================
        # VIEW CARD COMMAND (lv <num>)
        # =========================
        if content_lower.startswith("lv "):
            try:
                index = int(content_lower.split()[1]) - 1
                
                # Check if user is viewing someone else's inventory
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
            image_path = render_card_with_print(card, print_num)

            file = discord.File(
                image_path,
                filename="card.png"
            )

            embed.set_image(
                url="attachment://card.png"
            )

            await message.channel.send(
                embed=embed,
                file=file
            )

            os.remove(image_path)

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

            # =========================
            # Numeric selection from a previous search
            # =========================
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

                # ALWAYS use CharacterVersionView
                # even if there's only 1 version,
                # so the owners button still appears
                view = CharacterVersionView(
                    all_versions,
                    message.author,
                    user_id
                )
                return await message.channel.send(
                    embed=view.get_embed(),
                    view=view
                )

            # =========================
            # Normal text search
            # =========================
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

            # If search only matches 1 character,
            # open CharacterVersionView directly
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

            # Otherwise show the list of matching characters
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
                combine_cards_with_prints,
                card1,
                card2
            )

            file = discord.File(image_path, filename="drop.png")
            view = CardView(card1, card2)

            await message.channel.send(
                content=f"{message.author.mention} is dropping 2 cards!",
                file=file,
                view=view
            )
            return


# --- Run Bot Connection ---
import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)