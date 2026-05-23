import discord
import random
import time
import os
from cards import cards

from PIL import Image
from io import BytesIO
import requests

intents = discord.Intents.all()

cooldowns = {}
COOLDOWN_TIME = 600  # 10 minutes


def get_weighted_card():
    weighted = []

    for card in cards:
        for i in range(card["weight"]):
            weighted.append(card)

    return random.choice(weighted)


def combine_cards(url1, url2):

    response1 = requests.get(url1)
    response2 = requests.get(url2)

    img1 = Image.open(BytesIO(response1.content))
    img2 = Image.open(BytesIO(response2.content))

    img1 = img1.resize((300, 420))
    img2 = img2.resize((300, 420))

    combined = Image.new("RGB", (600, 420))

    combined.paste(img1, (0, 0))
    combined.paste(img2, (300, 0))

    combined.save("drop.png")

    return "drop.png"


class CardView(discord.ui.View):

    def __init__(self, card1, card2):
        super().__init__(timeout=30)

        self.card1 = card1
        self.card2 = card2

        self.card1_claimed = False
        self.card2_claimed = False

    def stars(self, n):
        return "⭐" * n

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def pick1(self, interaction: discord.Interaction, button: discord.ui.Button):

        if self.card1_claimed:
            return await interaction.response.send_message(
                "Card 1 already claimed!",
                ephemeral=True
            )

        self.card1_claimed = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"{interaction.user.mention} claimed {self.card1['name']} {self.stars(self.card1['stars'])}"
        )

    @discord.ui.button(label="2", style=discord.ButtonStyle.success)
    async def pick2(self, interaction: discord.Interaction, button: discord.ui.Button):

        if self.card2_claimed:
            return await interaction.response.send_message(
                "Card 2 already claimed!",
                ephemeral=True
            )

        self.card2_claimed = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"{interaction.user.mention} claimed {self.card2['name']} {self.stars(self.card2['stars'])}"
        )


class Client(discord.Client):

    async def on_ready(self):
        print(f"Logged in as {self.user}")

    async def on_message(self, message):

        if message.author == self.user:
            return

        # LD COMMAND
        if message.content == "ld":

            user_id = message.author.id
            now = time.time()

            if user_id in cooldowns:

                if now - cooldowns[user_id] < COOLDOWN_TIME:

                    remaining = int(
                        COOLDOWN_TIME - (now - cooldowns[user_id])
                    )

                    return await message.channel.send(
                        f"⏳ Wait {remaining} seconds before dropping again."
                    )

            cooldowns[user_id] = now

            card1 = get_weighted_card()
            card2 = get_weighted_card()

            while card2 == card1:
                card2 = get_weighted_card()

            await message.channel.send(
                f"{message.author.mention} is dropping 2 cards!"
            )

            image_path = combine_cards(
                card1["image"],
                card2["image"]
            )

            file = discord.File(image_path, filename="drop.png")

            view = CardView(card1, card2)

            await message.channel.send(
                file=file,
                view=view
            )


client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")

client.run(TOKEN)
