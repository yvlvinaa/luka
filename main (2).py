import discord
import random
import time
from cards import cards

from PIL import Image
from io import BytesIO
import requests

intents = discord.Intents.all()

# DROP COOLDOWN
cooldowns = {}
COOLDOWN_TIME = 10 * 60  # 10 minutes

# CLAIM COOLDOWN
claim_cooldowns = {}
CLAIM_COOLDOWN = 5 * 60  # 5 minutes


def get_weighted_card():
    weighted = []

    for card in cards:
        for i in range(card["weight"]):
            weighted.append(card)

    return random.choice(weighted)


# combine 2 card images into 1 image
def combine_cards(url1, url2):

    # timeout prevents freezing
    response1 = requests.get(url1, timeout=10)
    response2 = requests.get(url2, timeout=10)

    # convert to RGBA for transparency
    img1 = Image.open(BytesIO(response1.content)).convert("RGBA")
    img2 = Image.open(BytesIO(response2.content)).convert("RGBA")

    # resize both cards
    img1 = img1.resize((300, 420))
    img2 = img2.resize((300, 420))

    # transparent background
    combined = Image.new("RGBA", (600, 420), (0, 0, 0, 0))

    # paste cards side by side
    combined.paste(img1, (0, 0), img1)
    combined.paste(img2, (300, 0), img2)

    # unique filename (IMPORTANT)
    filename = f"drop_{time.time()}.png"

    # save image
    combined.save(filename)

    return filename


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

        user_id = interaction.user.id
        now = time.time()

        # CLAIM COOLDOWN CHECK
        if user_id in claim_cooldowns:

            if now - claim_cooldowns[user_id] < CLAIM_COOLDOWN:

                remaining = int(
                    CLAIM_COOLDOWN - (now - claim_cooldowns[user_id])
                )

                minutes = remaining // 60
                seconds = remaining % 60

                return await interaction.response.send_message(
                    f"⏳ Wait {minutes}m {seconds}s before claiming again.",
                    ephemeral=True
                )

        if self.card1_claimed:
            return await interaction.response.send_message(
                "Card 1 already claimed!",
                ephemeral=True
            )

        # SET CLAIM COOLDOWN
        claim_cooldowns[user_id] = now

        self.card1_claimed = True

        # disable ONLY button 1
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"Under the Stage's observation, "
            f"{interaction.user.mention} claimed "
            f"**{self.card1['name']}** "
            f"{self.stars(self.card1['stars'])}!"
        )

    @discord.ui.button(label="2", style=discord.ButtonStyle.success)
    async def pick2(self, interaction: discord.Interaction, button: discord.ui.Button):

        user_id = interaction.user.id
        now = time.time()

        # CLAIM COOLDOWN CHECK
        if user_id in claim_cooldowns:

            if now - claim_cooldowns[user_id] < CLAIM_COOLDOWN:

                remaining = int(
                    CLAIM_COOLDOWN - (now - claim_cooldowns[user_id])
                )

                minutes = remaining // 60
                seconds = remaining % 60

                return await interaction.response.send_message(
                    f"⏳ Wait {minutes}m {seconds}s before claiming again.",
                    ephemeral=True
                )

        if self.card2_claimed:
            return await interaction.response.send_message(
                "Card 2 already claimed!",
                ephemeral=True
            )

        # SET CLAIM COOLDOWN
        claim_cooldowns[user_id] = now

        self.card2_claimed = True

        # disable ONLY button 2
        button.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.channel.send(
            f"Under the Stage's observation, "
            f"{interaction.user.mention} claimed "
            f"**{self.card2['name']}** "
            f"{self.stars(self.card2['stars'])}!"
        )


class Client(discord.Client):

    async def on_ready(self):
        print(f"Logged in as {self.user}")

    async def on_message(self, message):

        if message.author == self.user:
            return

        # DROP COMMAND
        if message.content == "ld":

            user_id = message.author.id
            now = time.time()

            # DROP COOLDOWN CHECK
            if user_id in cooldowns:

                if now - cooldowns[user_id] < COOLDOWN_TIME:

                    remaining = int(
                        COOLDOWN_TIME - (now - cooldowns[user_id])
                    )

                    minutes = remaining // 60
                    seconds = remaining % 60

                    return await message.channel.send(
                        f"⏳ Wait {minutes}m {seconds}s before dropping again."
                    )

            try:

                # pick cards
                card1 = get_weighted_card()
                card2 = get_weighted_card()

                # prevent duplicate same drop
                while card2 == card1:
                    card2 = get_weighted_card()

                # combine images
                image_path = combine_cards(
                    card1["image"],
                    card2["image"]
                )

                file = discord.File(image_path, filename="drop.png")
                view = CardView(card1, card2)

                # send drop
                await message.channel.send(
                    content=(
                        f"📡 Under the Stage's observation, "
                        f"{message.author.mention} initiated a drop..."
                    ),
                    file=file,
                    view=view
                )

                # SET COOLDOWN ONLY AFTER SUCCESS
                cooldowns[user_id] = now

            except Exception as e:

                print("DROP ERROR:", e)

                await message.channel.send(
                    "⚠️ The Stage failed to generate a drop."
                )


import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)HM_c")
