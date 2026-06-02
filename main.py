import discord
import random
import time
import asyncio
import requests
from io import BytesIO
from PIL import Image

# Import your database and card assets
from cards import cards
from data import inventories, drop_cooldowns, claim_cooldowns

# Initialize intents
intents = discord.Intents.all()

# Global Configurations
DROP_COOLDOWN = 600
CLAIM_COOLDOWN = 300
CARDS_PER_PAGE = 10
THEME_COLOR = discord.Color.from_rgb(255, 227, 102)

# Global tracking for lookup history sessions
user_last_lookup = {}


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


def add_card(user_id, card):
    """Adds a card dictionary to a user's collection."""
    get_inventory(user_id).append(card)


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

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def pick1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.claim(interaction, 1, button)

    @discord.ui.button(label="2", style=discord.ButtonStyle.success)
    async def pick2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.claim(interaction, 2, button)


# =========================
# 2. INVENTORY VIEW
# =========================
class InventoryView(discord.ui.View):
    def __init__(self, user, inventory):
        super().__init__(timeout=60)
        self.user = user
        self.inventory = inventory
        self.page = 0

    def get_embed(self):
        embed = discord.Embed(color=THEME_COLOR)
        embed.set_author(name=f"{self.user.name}'s Collection", icon_url=self.user.display_avatar.url)

        start = self.page * CARDS_PER_PAGE
        end = start + CARDS_PER_PAGE
        cards_page = self.inventory[start:end]

        if not cards_page:
            embed.description = "No cards collected."
            return embed

        text = ""
        for i, card in enumerate(cards_page, start=start + 1):
            name = card.get("name", "Unknown")
            star_val = card.get("stars", 1)
            series = card.get("series", "Unknown Series")
            text += f"`{i:02d}` ✦ `⭐ {star_val}` **{name}** • *{series}*\n"

        embed.description = text
        total_pages = (len(self.inventory) - 1) // CARDS_PER_PAGE + 1
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages}")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        max_page = (len(self.inventory) - 1) // CARDS_PER_PAGE
        if self.page < max_page:
            self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


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
        embed.set_author(name=f"{self.user.name}'s Search", icon_url=self.user.display_avatar.url)
        
        name = card.get("name", "Unknown")
        series = card.get("series", "Unknown Series")
        star_val = card.get("stars", 1)

        embed.description = (
            f"## **{name}**\n"
            f"✦ **Series:** **{series}**\n"
            f"───\n"
            f"✦ **Level:** **{stars(star_val)}**\n"
        )
        embed.set_image(url=clean_url(card.get("image", "")))
        embed.set_footer(text=f"Version {self.index + 1}/{len(self.versions)}")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.index > 0:
            self.index -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        if self.index < len(self.versions) - 1:
            self.index += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

# =========================
# 5. GIFT VIEW (UPDATED)
# =========================
class GiftView(discord.ui.View):
    def __init__(self, from_user, to_user, card, from_id, to_id, card_index):
        super().__init__(timeout=60)
        self.from_user = from_user
        self.to_user = to_user
        self.card = card
        self.from_id = from_id
        self.to_id = to_id
        self.card_index = card_index

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.to_id:
            return await interaction.response.send_message("Not your gift.", ephemeral=True)

        giver_inv = get_inventory(self.from_id)
        if self.card_index >= len(giver_inv) or giver_inv[self.card_index] != self.card:
            return await interaction.response.send_message("This card is no longer available to trade.", ephemeral=True)

        remove_card(self.from_id, self.card_index)
        add_card(self.to_id, self.card)

        await interaction.response.edit_message(
            content=f"🎉 {self.to_user.mention} accepted {self.from_user.mention}'s gift!",
            view=None
        )

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.to_id:
            return await interaction.response.send_message("Not your gift.", ephemeral=True)

        await interaction.response.edit_message(
            content=f"❌ {self.to_user.mention} declined {self.from_user.mention}'s gift!",
            view=None
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
        # INVENTORY COMMAND (lc)
        # =========================
        if content_lower == "lc":
            view = InventoryView(message.author, inv)
            await message.channel.send(embed=view.get_embed(), view=view)
            return

        # =========================
        # VIEW CARD COMMAND (lv <num>)
        # =========================
        if content_lower.startswith("lv "):
            try:
                index = int(content_lower.split()[1]) - 1
                if index < 0 or index >= len(inv):
                    raise IndexError
                card = inv[index]
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
                f"✦ **Owner:** {message.author.mention}\n"
                f"✦ **Level:** **{stars(star_val)}**\n"
            )
            embed.set_image(url=clean_url(card.get("image", "")))
            return await message.channel.send(embed=embed)

        # =========================
        # LOOKUP COMMAND (lup <query>)
        # =========================
        if content_lower.startswith("lup "):
            query = content[4:].strip().lower()
            if not query:
                return await message.channel.send("Please provide a name or a number to search.")

            # Handling numeric selection from a prior text search
            if query.isdigit():
                if user_id not in user_last_lookup:
                    return await message.channel.send("You haven't searched for anything yet! Search using a name first.")
                
                selection = int(query) - 1
                previous_results = user_last_lookup[user_id]

                if selection < 0 or selection >= len(previous_results):
                    return await message.channel.send("Invalid number selection from your last search.")

                chosen_card = previous_results[selection]
                all_versions = [c for c in cards if c.get("name", "").lower() == chosen_card.get("name", "").lower()]
                all_versions.sort(key=lambda x: x.get("stars", 1))

                if len(all_versions) == 1:
                    card = all_versions[0]
                    embed = discord.Embed(color=THEME_COLOR)
                    embed.set_author(name=f"{message.author.name}'s Search", icon_url=message.author.display_avatar.url)
                    embed.description = (
                        f"## **{card.get('name', 'Unknown')}**\n"
                        f"✦ **Series:** **{card.get('series', 'Unknown')}**\n"
                        f"───\n"
                        f"✦ **Level:** **{stars(card.get('stars', 1))}**\n"
                    )
                    embed.set_image(url=clean_url(card.get("image", "")))
                    return await message.channel.send(embed=embed)
                
                view = CharacterVersionView(all_versions, message.author, user_id)
                return await message.channel.send(embed=view.get_embed(), view=view)

            # Handling standard name text queries
            matched_cards = [card for card in cards if query in card.get("name", "").lower()]

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
                all_versions = [c for c in cards if c.get("name", "").lower() == unique_results[0].get("name", "").lower()]
                all_versions.sort(key=lambda x: x.get("stars", 1))
                
                if len(all_versions) == 1:
                    card = all_versions[0]
                    embed = discord.Embed(color=THEME_COLOR)
                    embed.set_author(name=f"{message.author.name}'s Search", icon_url=message.author.display_avatar.url)
                    embed.description = (
                        f"## **{card.get('name', 'Unknown')}**\n"
                        f"✦ **Series:** **{card.get('series', 'Unknown')}**\n"
                        f"───\n"
                        f"✦ **Level:** **{stars(card.get('stars', 1))}**\n"
                    )
                    embed.set_image(url=clean_url(card.get("image", "")))
                    return await message.channel.send(embed=embed)
                
                view = CharacterVersionView(all_versions, message.author, user_id)
                return await message.channel.send(embed=view.get_embed(), view=view)

            view = LookupListView(unique_results, message.author, user_id)
            return await message.channel.send(embed=view.get_embed(), view=view)


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
            image_path = await loop.run_in_executor(None, combine_cards, card1.get("image"), card2.get("image"))

            file = discord.File(image_path, filename="drop.png")
            view = CardView(card1, card2)

            await message.channel.send(
                content=f"{message.author.mention} is dropping 2 cards!",
                file=file,
                view=view
            )
            return


import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
