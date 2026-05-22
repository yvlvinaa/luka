import discord
import random

intents = discord.Intents.all()

cards = [
    {
        "name": "Luka",
        "print": 1,
        "image": "https://static.wikia.nocookie.net/alien-stage/images/b/b7/LukaBirthday2025.jpeg/revision/latest/scale-to-width-down/1000?cb=20251222150118"
    },

    {
        "name": "Luka2",
        "print": 15,
        "image": "https://static.wikia.nocookie.net/alien-stage/images/9/9c/OFF_THE_RECORD_24.09.20.jpg/revision/latest/scale-to-width-down/1000?cb=20241122090215"
    },

    {
        "name": "Luka 3",
        "print": 50,
        "image": "https://static.wikia.nocookie.net/alien-stage/images/0/02/ANOMALY_PARTY_Luka.jpg/revision/latest/scale-to-width-down/1000?cb=20250711035300"
    },

    {
        "name": "Luka4",
        "print": 100,
        "image": "https://media.discordapp.net/attachments/1505769845579452416/1505769968493662248/Untitled336_20260518110929.png?ex=6a111b10&is=6a0fc990&hm=4925e3de0c744616d7ef4768f7aa5bf8d7525b9e6b6127908cae4dbfd9cec9ed&=&format=webp&quality=lossless&width=559&height=745"
    }
]

def get_weighted_card():

    weighted = []

    for card in cards:

        # higher print = more common
        weight = card["print"]

        for i in range(weight):
            weighted.append(card)

    return random.choice(weighted)

class Client(discord.Client):

    async def on_ready(self):
        print(f'Logged on as {self.user}!')

    async def on_message(self, message):
        print(message.content)

        if message.author == self.user:
            return

        # command trigger
        if message.content == "ldrop":

            card = get_weighted_card()

            await message.channel.send(
                f":flower_playing_cards: New Card Drop!\n\n"
                f"Name: {card['name']}\n"
                f"Print: #{card['print']}\n"
                f"{card['image']}"
            )
import os

client = Client(intents=intents)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)

 
