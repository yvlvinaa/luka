import discord
import random
import time
from cards import cards

from PIL import Image
from io import BytesIO
import requests

intents = discord.Intents.all()

cooldowns = {}
COOLDOWN_TIME = 600  # 10 minutes


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
# SAFE IMAGE LOADER
# -----------------------------
def get_image(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        return Image.open(BytesIO(response.content)).convert("RGBA")

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

    img1 = get_image(url1).resize((300, 420))
    img2 = get_image(url2).resize((300, 420))

    combined = Image.new(
        "RGBA",
        (600, 420),
        (0, 0, 0, 0)
    )

    combined.paste(img1, (0, 0), img1)
    combined.paste(img2, (300, 0), img2)

    path = "drop.png"
    combined.save(path)

    return path


# -----------------------------
# VIEW (CLAIM SYSTEM)
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

        if self.card1_claimed:
            return await interaction.response.send_message(
                "Card 1 already claimed!",
                ephemeral=True
            )

        self.card1_claimed = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"Under the Stage's observation, "
            f"{interaction.user.mention} claimed "
            f"**{self.card1['name']}** "
            f"{self.stars(self.card1['stars'])}"
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

        if self.card2_claimed:
            return await interaction.response.send_message(
                "Card 2 already claimed!",
                ephemeral=True
            )

        self.card2_claimed = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"Under the Stage's observation, "
            f"{interaction.user.mention} claimed "
            f"**{self.card2['name']}** "
            f"{self.stars(self.card2['stars'])}"
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

        if message.content == "ld":

            user_id = message.author.id
            now = time.time()

            # cooldown check
            if user_id in cooldowns:

                if now - cooldowns[user_id] < COOLDOWN_TIME:

                    remaining = int(
                        COOLDOWN_TIME -
                        (now - cooldowns[user_id])
                    )

                    minutes = remaining // 60
                    seconds = remaining % 60

                    return await message.channel.send(
                        f"Wait {minutes}m {seconds}s before dropping again."
                    )

            cooldowns[user_id] = now

            # pick cards
            card1 = get_weighted_card()
            card2 = get_weighted_card()

            while card2 == card1:
                card2 = get_weighted_card()

            # combine cards
            image_path = combine_cards(
                card1["image"],
                card2["image"]
            )

            file = discord.File(
                image_path,
                filename="drop.png"
            )

            view = CardView(
                card1,
                card2
            )

            await message.channel.send(
                content=f"{message.author.mention} is dropping 2 cards!",
                file=file,
                view=view
            )


import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
