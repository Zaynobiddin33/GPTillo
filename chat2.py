from aiogram import Bot, Dispatcher, types
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode, ChatType, ChatAction
from aiogram.types import Message, ChatMemberUpdated, FSInputFile
from aiogram.filters import Command, CommandStart
import asyncio
from aiogram.client.default import DefaultBotProperties
import os
from PIL import Image
from io import BytesIO
import requests
import base64  # ← NEW

from groq import Groq  # ← NEW
from tokens import *    # now contains GROQ_API_KEY
from gen import generate_image
from functions import *
import re


print('start')
def remove_think(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

# ====================== GROQ SETUP ======================
client = Groq(api_key=GROQ_API_KEY)
chat_histories = {}   # replaces your old chat_sessions

def get_or_create_chat_session(telegram_chat_id: int, chat_type: str, description=None):
    if telegram_chat_id not in chat_histories or description:
        default = get_default_prompt()
        if chat_type in ["group", "supergroup", "channel"]:
            instruction = get_group_prompt()
        else:
            instruction = get_private_prompt()

        if description and len(description) > 3:
            instruction += description
        else:
            instruction += default

        # Force plain text (Telegram doesn't like markdown)
        instruction += "\n\nAlways respond in plain text only. Never use ** or * markdown."

        chat_histories[telegram_chat_id] = [
            {"role": "system", "content": instruction}
        ]
    return chat_histories[telegram_chat_id]

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties()
)
dp = Dispatcher()

groups_list = load_groups()
users_list = load_users()

async def escape_markdown(message: Message, text: str):
    """Simplified - no longer needs Gemini session"""
    if "**" in text:
        print("Markdown detected → sending plain text")
    chunks = split_message(text)  # your function from functions.py
    for chunk in chunks:
        await message.answer(
            chunk,
            reply_to_message_id=message.message_id
        )

@dp.message(lambda message: not message.text or not message.text.startswith("/"))
async def handle_group_messages(message: Message):
    try:
        chat_history = get_or_create_chat_session(message.chat.id, message.chat.type)

        # Register new users/groups (your original logic)
        if message.chat.type == 'private':
            if not any(user["id"] == message.from_user.id for user in users_list):
                users_list.append({
                    'id': message.from_user.id,
                    'username': message.from_user.username or "Unknown",
                    'name': f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
                })
                save_users(users_list)
                print('NEW USER')

        if message.chat.type in ['supergroup', 'group']:
            if not any(g["id"] == message.chat.id for g in groups_list):
                groups_list.append({
                    "id": message.chat.id,
                    "title": message.chat.title or "Unknown",
                    "url": f"https://t.me/{message.chat.username}" if message.chat.username else "Private/No link"
                })
                save_groups(groups_list)

        await bot.send_chat_action(message.chat.id, action=ChatAction.TYPING)

        user = message.from_user
        full_name = f"{user.first_name} {user.last_name or ''}".strip()
        if full_name.lower() in ['telegram', 'group', 'admin']:
            full_name = 'Admin'

        original = ""
        if message.reply_to_message:
            reply_name = 'Admin' if message.reply_to_message.from_user.full_name.lower() in ['telegram', 'group', 'admin'] else message.reply_to_message.from_user.full_name
            if message.reply_to_message.text:
                original = f"(reply to {reply_name}: {message.reply_to_message.text})"
            elif message.reply_to_message.caption:
                original = f"(reply to {reply_name}: {message.reply_to_message.caption})"

        # Build user message
        if message.photo:
            data = f"{full_name}: {message.caption if message.caption else 'Image sent'} {original}"
            file_id = message.photo[-1].file_id
            file = await bot.get_file(file_id)
            image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            image = requests.get(image_url)

            base64_image = base64.b64encode(image.content).decode("utf-8")
            user_content = [
                {"type": "text", "text": data},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        else:
            data = f"{full_name}: {message.text} {original}"
            user_content = data

        print(data, flush=True)

        # ====================== GROQ CALL ======================
        chat_history.append({"role": "user", "content": user_content})

        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",   # supports images + very fast
            messages=chat_history,
            temperature=0.8,
            max_tokens=1024
        )

        response_text = remove_think(completion.choices[0].message.content.strip())
        chat_history.append({"role": "assistant", "content": response_text})

        print(f"Groq: {response_text}", flush=True)

        # Your original image generation logic
        if "GENERATE_IMAGE" in response_text:
            prompt = response_text.split("GENERATE_IMAGE")[1]
            caption = response_text.split("GENERATE_IMAGE")[0]
            image_path = generate_image(prompt)
            if image_path == 'error':
                err = "Sorry, due to high demand, I cannot generate this image right now. Retry later..."
                await message.answer(err, reply_to_message_id=message.message_id)
            else:
                await message.answer_photo(FSInputFile(image_path), caption=caption, show_caption_above_media=True, reply_to_message_id=message.message_id)
                os.system(f'rm {image_path}')
        elif "SKIP" not in response_text:
            await escape_markdown(message, response_text)

    except Exception as e:
        error_str = str(e).lower()
        if "rate limit" in error_str or "429" in error_str:
            await message.answer("⏳ Free tier rate limit reached. Please wait a few seconds and try again.", reply_to_message_id=message.message_id)
            print("Groq rate limit hit")
        else:
            print(f"Error: {e}")
            # await message.answer("Sorry, something went wrong. Try again.", reply_to_message_id=message.message_id)

# ==================== YOUR OTHER HANDLERS (unchanged) ====================
@dp.my_chat_member()
async def handle_bot_status_change(event: ChatMemberUpdated):
    # ... (your original code - unchanged)
    pass

@dp.message(Command("groups"))
async def pollmath_handler(message: Message):
    # ... (your original code - unchanged)
    pass

@dp.message(Command('broadcast'))
async def broadcast_message(message: Message):
    # ... (your original code - unchanged)
    pass

@dp.message(Command('personality'))
async def add_personality(message: Message):
    description = message.text.split('/personality')[1].strip()
    get_or_create_chat_session(message.chat.id, message.chat.type, description)
    await message.answer(f'Personality changed to: {description}')

@dp.message(Command('start'))
async def start_handler(message: Message):
    if message.chat.type.lower() == "private":
        chat_history = get_or_create_chat_session(message.chat.id, message.chat.type)
        user = message.from_user
        full_name = f"{user.first_name} {user.last_name or ''}".strip()
        
        chat_history.append({"role": "user", "content": f'{full_name}: Hello'})
        
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=chat_history
        )
        response_text = remove_think(completion.choices[0].message.content.strip())
        chat_history.append({"role": "assistant", "content": response_text})
        
        await message.answer(response_text)

async def main():
    await dp.start_polling(bot, skip_updates=True, relax=1.0)

if __name__ == "__main__":
    asyncio.run(main())