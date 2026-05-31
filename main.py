import discord
import random
import time
from cards import cards

from PIL import Image
from io import BytesIO
import requests

intents = discord.Intents.all()

# -----------------------------
# COOLDOWNS
# -----------------------------
drop_cooldowns = {}
claim_cooldowns = {}

DROP_COOLDOWN = 600   # 10 mins
CLAIM_COOLDOWN = 300  # 5 mins


# -----------------------------
# PICK CARD
# -----------------------------
def get_weighted_card():
    weighted = []

    for card in cards:
        for i in range(card["weight"]):
            weighted.append(card)

    return random.choice(weighted)


# -----------------------------
# FIX URLS
# -----------------------------
def clean_url(url):

    if "github.com" in url and "/blob/" in url:
        url = url.replace(
            "github.com",
            "raw.githubusercontent.com"
        ).replace(
            "/blob/",
            "/"
        )

    url = url.split("?")[0]

    return url


# -----------------------------
# IMAGE LOADER
# -----------------------------
def get_image(url):

    try:
        url = clean_url(url)

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=10
        )

        if response.status_code != 200:
            raise Exception("Bad response")

        return Image.open(
            BytesIO(response.content)
        ).convert("RGBA")

    except Exception as e:

        print("IMAGE ERROR:", url, e)

        return Image.new(
            "RGBA",
            (300, 420),
            (80, 80, 80, 255)
        )


# -----------------------------
# COMBINE CARDS
# -----------------------------
def combine_cards(url1, url2):

    img1 = get_image(url1)
    img2 = get_image(url2)

    img1 = img1.resize((400, 560))
    img2 = img2.resize((400, 560))

    combined = Image.new(
        "RGBA",
        (800, 560),
        (0, 0, 0, 0)
    )

    combined.paste(img1, (0, 0), img1)
    combined.paste(img2, (400, 0), img2)

    path = "drop.png"

    combined.save(path)

    return path


# -----------------------------
# CLAIM VIEW
# -----------------------------
class CardView(discord.ui.View):

    def __init__(self, card1, card2):
        super().__init__(timeout=30)

        self.card1 = card1
        self.card2 = card2

        self.card1_claimed = False
        self.card2_claimed = False

    def stars(self, n):
        return "⭐" * n

    @discord.ui.button(
        label="1",
        style=discord.ButtonStyle.primary
    )
    async def pick1(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        user_id = interaction.user.id
        now = time.time()

        if user_id in claim_cooldowns:

            if now - claim_cooldowns[user_id] < CLAIM_COOLDOWN:

                remaining = int(
                    CLAIM_COOLDOWN -
                    (now - claim_cooldowns[user_id])
                )

                minutes = remaining // 60
                seconds = remaining % 60

                return await interaction.response.send_message(
                    f"Wait {minutes}m {seconds}s before claiming again.",
                    ephemeral=True
                )

        if self.card1_claimed:
            return await interaction.response.send_message(
                "Card 1 already claimed!",
                ephemeral=True
            )

        claim_cooldowns[user_id] = now

        self.card1_claimed = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"Under the Stage's observation, {interaction.user.mention} claimed **{self.card1['name']}**! {self.stars(self.card1['stars'])}"
        )

    @discord.ui.button(
        label="2",
        style=discord.ButtonStyle.success
    )
    async def pick2(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        user_id = interaction.user.id
        now = time.time()

        if user_id in claim_cooldowns:

            if now - claim_cooldowns[user_id] < CLAIM_COOLDOWN:

                remaining = int(
                    CLAIM_COOLDOWN -
                    (now - claim_cooldowns[user_id])
                )

                minutes = remaining // 60
                seconds = remaining % 60

                return await interaction.response.send_message(
                    f"Wait **{minutes}m {seconds}s** before claiming again.",
                    ephemeral=True
                )

        if self.card2_claimed:
            return await interaction.response.send_message(
                "Card 2 already claimed!",
                ephemeral=True
            )

        claim_cooldowns[user_id] = now

        self.card2_claimed = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"Under the Stage's observation, {interaction.user.mention} claimed **{self.card2['name']}**! {self.stars(self.card2['stars'])}"
        )


# -----------------------------
# BOT
# -----------------------------
class Client(discord.Client):

    async def on_ready(self):
        print(f"Logged in as {self.user}")

    async def on_message(self, message):

        if message.author == self.user:
            return

        # -----------------
        # LCD COMMAND
        # -----------------
        if message.content == "lcd":

            user_id = message.author.id
            now = time.time()

            # DROP TIMER
            if user_id in drop_cooldowns:

                remaining = max(
                    0,
                    int(
                        DROP_COOLDOWN -
                        (now - drop_cooldowns[user_id])
                    )
                )

                if remaining > 0:
                    drop_text = (
                        f"{remaining // 60}m "
                        f"{remaining % 60}s"
                    )
                else:
                    drop_text = "You can drop your card now!"

            else:
                drop_text = "You can drop your card now!"

            # CLAIM TIMER
            if user_id in claim_cooldowns:

                remaining = max(
                    0,
                    int(
                        CLAIM_COOLDOWN -
                        (now - claim_cooldowns[user_id])
                    )
                )

                if remaining > 0:
                    claim_text = (
                        f"{remaining // 60}m "
                        f"{remaining % 60}s"
                    )
                else:
                    claim_text = "You can claim a card now!"

            else:
                claim_text = "You can claim a card now!"

            embed = discord.Embed(
                title="Stage Monitor",
                color=discord.Color.dark_purple()
            )

            embed.add_field(
                name="Drop",
                value=drop_text,
                inline=False
            )

            embed.add_field(
                name="Claim",
                value=claim_text,
                inline=False
            )

            await message.channel.send(embed=embed)
            return

        # -----------------
        # DROP COMMAND
        # -----------------
        if message.content == "ld":

            user_id = message.author.id
            now = time.time()

            if user_id in drop_cooldowns:

                if now - drop_cooldowns[user_id] < DROP_COOLDOWN:

                    remaining = int(
                        DROP_COOLDOWN -
                        (now - drop_cooldowns[user_id])
                    )

                    minutes = remaining // 60
                    seconds = remaining % 60

                    return await message.channel.send(
                        f"Wait **{minutes}m {seconds}s** before dropping again."
                    )

            drop_cooldowns[user_id] = now

            card1 = get_weighted_card()
            card2 = get_weighted_card()

            while card2 == card1:
                card2 = get_weighted_card()

            image_path = combine_cards(
                card1["image"],
                card2["image"]
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


import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
