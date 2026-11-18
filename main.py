import os
import uuid
import time
import secrets
import asyncio
from pathlib import Path

from contextlib import asynccontextmanager
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ai_engine import ask_ai
from gdrive_storage import (
    get_all_sources,
    save_sources,
    extract_text_bytes,
    upload_file_to_drive,
    delete_file_from_drive
)
from discord_bot import start_discord, stop_discord

discord_task = None
web_chat_history = defaultdict(list)
MAX_WEB_HISTORY = 10
MAX_UPLOAD_SIZE = 15 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    global discord_task
    if os.getenv("DISCORD_TOKEN"):
        discord_task = asyncio.create_task(start_discord())
    await asyncio.sleep(0)
    yield
    if discord_task:
        await stop_discord()
        if not discord_task.done():
            discord_task.cancel()
            try:
                await discord_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title="AI Cloud Knowledge Base Assistant", lifespan=lifespan)

# อนุญาตให้หน้าเว็บที่รันบน GitHub Pages เรียกใช้งาน API ข้ามโดเมนได้
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in allowed_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=100)


class AddTextRequest(BaseModel):
    admin_key: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    confidential: bool = False


requests_by_ip = defaultdict(deque)
RATE_LIMIT = 15
WINDOW_SECONDS = 60


def check_rate_limit(ip):
    now = time.monotonic()
    requests = requests_by_ip[ip]
    while requests and now - requests[0] > WINDOW_SECONDS:
        requests.popleft()
    if len(requests) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="คุณส่งคำถามบ่อยเกินไป กรุณารอสักครู่")
    requests.append(now)


def verify_admin(key: str):
    expected_key = os.getenv("ADMIN_KEY")
    if not expected_key or not secrets.compare_digest(key, expected_key):
        raise HTTPException(status_code=403, detail="Admin Key ไม่ถูกต้อง")


@app.get("/")
@app.get("/api/health")
async def health():
    return {"status": "online", "storage": "Google Drive"}


@app.post("/api/chat")
async def chat(data: ChatRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip)

    session_key = f"{ip}_{data.session_id}" if data.session_id else ip
    history = web_chat_history[session_key]

    try:
        answer = await asyncio.to_thread(
            ask_ai,
            data.message,
            history,
            False,
            None,
            None,
            "web",
            session_key
        )

        history.extend([
            {"role": "user", "text": data.message},
            {"role": "assistant", "text": answer}
        ])
        web_chat_history[session_key] = history[-MAX_WEB_HISTORY:]

        return {"success": True, "answer": answer}
    except Exception as error:
        print("WEB AI ERROR:", repr(error))
        raise HTTPException(status_code=503, detail="AI ไม่สามารถตอบคำถามได้ในขณะนี้")


@app.get("/api/admin/sources")
async def get_sources(admin_key: str):
    verify_admin(admin_key)
    sources = await asyncio.to_thread(get_all_sources, True)
    return {"success": True, "sources": sources}


@app.post("/api/admin/sources/text")
async def add_text_source(data: AddTextRequest):
    verify_admin(data.admin_key)
    sources = await asyncio.to_thread(get_all_sources, True)
    new_source = {
        "id": str(uuid.uuid4()),
        "name": data.title,
        "type": "text",
        "content": data.content,
        "active": True,
        "confidential": data.confidential,
        "created_at": time.strftime("%Y-%m-%d %H:%M")
    }
    sources.append(new_source)
    await asyncio.to_thread(save_sources, sources)
    return {"success": True, "message": "บันทึกโน้ตลง Google Drive เรียบร้อย"}


@app.post("/api/admin/sources/upload")
async def upload_source_file(
    admin_key: str = Form(...),
    confidential: bool = Form(False),
    file: UploadFile = File(...)
):
    verify_admin(admin_key)
    content_bytes = await file.read()

    if len(content_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="ขนาดไฟล์ใหญ่เกิน 15MB")

    safe_filename = Path(file.filename or "uploaded_file").name
    text = extract_text_bytes(safe_filename, content_bytes)

    if not text:
        raise HTTPException(status_code=400, detail="ไฟล์ไม่มีข้อความที่สามารถอ่านได้")

    # อัปโหลดไฟล์จริงขึ้น Google Drive
    drive_file_id = await asyncio.to_thread(
        upload_file_to_drive,
        safe_filename,
        content_bytes,
        file.content_type or "application/octet-stream"
    )

    sources = await asyncio.to_thread(get_all_sources, True)
    new_source = {
        "id": str(uuid.uuid4()),
        "drive_file_id": drive_file_id,
        "name": safe_filename,
        "type": "file",
        "content": text,
        "active": True,
        "confidential": confidential,
        "created_at": time.strftime("%Y-%m-%d %H:%M")
    }
    sources.append(new_source)
    await asyncio.to_thread(save_sources, sources)
    return {"success": True, "message": f"อัปโหลด '{safe_filename}' ขึ้น Google Drive เรียบร้อย"}


@app.delete("/api/admin/sources/{source_id}")
async def delete_source(source_id: str, admin_key: str):
    verify_admin(admin_key)
    sources = await asyncio.to_thread(get_all_sources, True)
    source_to_delete = next((s for s in sources if s["id"] == source_id), None)

    if not source_to_delete:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ที่ต้องการลบ")

    if source_to_delete.get("drive_file_id"):
        await asyncio.to_thread(delete_file_from_drive, source_to_delete["drive_file_id"])

    updated_sources = [s for s in sources if s["id"] != source_id]
    await asyncio.to_thread(save_sources, updated_sources)
    return {"success": True, "message": "ลบข้อมูลออกจาก Google Drive เรียบร้อยแล้ว"}


@app.patch("/api/admin/sources/{source_id}/toggle")
async def toggle_source(source_id: str, admin_key: str):
    verify_admin(admin_key)
    sources = await asyncio.to_thread(get_all_sources, True)
    found = False
    for s in sources:
        if s["id"] == source_id:
            s["active"] = not s.get("active", True)
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์")
    await asyncio.to_thread(save_sources, sources)
    return {"success": True, "message": "อัปเดตสถานะเรียบร้อย"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)