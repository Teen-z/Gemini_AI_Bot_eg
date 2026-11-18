import os
import re
import time
import uuid
import discord
import asyncio

from collections import deque
from dotenv import load_dotenv
from ai_engine import ask_ai, get_all_sources, save_sources

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise RuntimeError("ไม่พบ DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

chat_history = {}
MAX_HISTORY = 10

seen_message_ids = set()
recent_message_ids = deque(maxlen=2000)
chat_locks = {}


def is_duplicate_message(message_id):
    if message_id in seen_message_ids:
        return True
    if len(recent_message_ids) == recent_message_ids.maxlen:
        old_id = recent_message_ids[0]
        seen_message_ids.discard(old_id)
    recent_message_ids.append(message_id)
    seen_message_ids.add(message_id)
    return False


def add_discord_knowledge(text: str, author_name: str, confidential: bool = False) -> bool:
    try:
        sources = get_all_sources()
        new_source = {
            "id": str(uuid.uuid4()),
            "name": f"Discord Note (โดย {author_name})",
            "type": "text",
            "content": text.strip(),
            "active": True,
            "confidential": confidential,
            "created_at": time.strftime("%Y-%m-%d %H:%M")
        }
        sources.append(new_source)
        save_sources(sources)
        return True
    except Exception as e:
        print("DISCORD KNOWLEDGE SAVE ERROR:", e)
        return False


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="แท็ก @เพื่อถามคำถาม | !help"))
    print(f"DISCORD ONLINE: {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    is_admin = getattr(message.author.guild_permissions, "administrator", False)
    key = (message.channel.id, message.author.id)

    if message.content.strip() == "!help":
        help_text = (
            "**คำสั่งการใช้งานบอท:**\n"
            "• แท็ก `@บอท` พร้อมคำถาม (สามารถแนบรูปภาพให้บอทช่วยดูได้)\n"
            "• `!clear` — ล้างประวัติการสนทนาของคุณกับบอท\n"
            "• `!addinfo <ข้อความ>` — เพิ่มข้อมูลเข้าคลังความรู้ (เฉพาะ Admin)"
        )
        await message.reply(help_text, mention_author=False)
        return

    if message.content.strip() == "!clear":
        chat_history.pop(key, None)
        await message.reply("🧹 ล้างประวัติการสนทนาเรียบร้อยแล้วครับ", mention_author=False)
        return

    if message.content.startswith("!addinfo"):
        if not is_admin:
            await message.reply("ขออภัย เฉพาะ Admin เท่านั้นที่สามารถเพิ่มข้อมูลได้ครับ", mention_author=False)
            return

        info_content = message.content.replace("!addinfo", "", 1).strip()
        if not info_content:
            await message.reply("กรุณาระบุข้อมูลที่ต้องการเพิ่ม เช่น `!addinfo ประกาศหยุดเรียนวันศุกร์นี้`", mention_author=False)
            return

        if add_discord_knowledge(info_content, str(message.author.display_name), confidential=False):
            await message.reply("✅ เพิ่มข้อมูลเข้าสู่ระบบ AI Sources เรียบร้อยแล้ว!", mention_author=False)
        else:
            await message.reply("❌ เกิดข้อผิดพลาดในการบันทึกข้อมูล", mention_author=False)
        return

    if bot.user not in message.mentions:
        return

    if is_duplicate_message(message.id):
        return

    prompt = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()

    image_bytes = None
    image_mime = None
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            # จำกัดขนาดรูปภาพไม่เกิน 10MB ป้องกันการยิงไฟล์ใหญ่
            if attachment.size <= 10 * 1024 * 1024:
                image_bytes = await attachment.read()
                image_mime = attachment.content_type
            break

    if not prompt and not image_bytes:
        await message.reply("สวัสดีครับ มีอะไรให้ช่วยไหมครับ?", mention_author=False)
        return

    if not prompt and image_bytes:
        prompt = "ช่วยอธิบายหรือวิเคราะห์รูปภาพนี้หน่อยครับ"

    if key not in chat_locks:
        chat_locks[key] = asyncio.Lock()

    async with chat_locks[key]:
        history = chat_history.setdefault(key, [])

        async with message.channel.typing():
            try:
                answer = await asyncio.to_thread(
                    ask_ai,
                    prompt,
                    history,
                    is_admin,
                    image_bytes,
                    image_mime,
                    "discord",
                    f"{message.author} ({message.author.id})"
                )

                history.extend([
                    {"role": "user", "text": prompt},
                    {"role": "assistant", "text": answer}
                ])

                chat_history[key] = history[-MAX_HISTORY:]

                for i in range(0, len(answer), 1900):
                    await message.channel.send(answer[i:i + 1900])

            except Exception as error:
                print("DISCORD AI ERROR:", repr(error))
                error_message = str(error) if isinstance(error, RuntimeError) else "ขออภัยครับ ระบบ AI เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง"
                await message.reply(error_message, mention_author=False)


async def start_discord():
    await bot.start(DISCORD_TOKEN)


async def stop_discord():
    if not bot.is_closed():
        await bot.close()