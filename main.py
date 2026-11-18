import io
import os
import json
import uuid
import time
import secrets
import asyncio
from pathlib import Path

from contextlib import asynccontextmanager
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pypdf import PdfReader
import docx
import pandas as pd
from pptx import Presentation
from bs4 import BeautifulSoup

from ai_engine import ask_ai, get_all_sources, save_sources, LOGS_FILE
from discord_bot import start_discord, stop_discord

discord_task = None
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

web_chat_history = defaultdict(list)
MAX_WEB_HISTORY = 10
MAX_UPLOAD_SIZE = 15 * 1024 * 1024  # จำกัดขนาดอัปโหลดสูงสุด 15MB


@asynccontextmanager
async def lifespan(app: FastAPI):
    global discord_task
    discord_task = asyncio.create_task(start_discord())
    await asyncio.sleep(0)
    yield
    await stop_discord()
    if discord_task and not discord_task.done():
        discord_task.cancel()
        try:
            await discord_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="AI Knowledge Base Assistant", lifespan=lifespan)


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
        raise HTTPException(status_code=403, detail="Admin Key ไม่ถูกต้อง หรือยังไม่ได้ตั้งค่าใน .env")


async def extract_text_from_file(safe_filename: str, content_bytes: bytes) -> str:
    filename = safe_filename.lower()
    extracted_text = ""

    if filename.endswith((".txt", ".md", ".log", ".json")):
        extracted_text = content_bytes.decode("utf-8", errors="ignore")
    elif filename.endswith(".pdf"):
        pdf_reader = PdfReader(io.BytesIO(content_bytes))
        for page in pdf_reader.pages:
            t = page.extract_text()
            if t:
                extracted_text += t + "\n"
    elif filename.endswith(".docx"):
        doc = docx.Document(io.BytesIO(content_bytes))
        for p in doc.paragraphs:
            if p.text:
                extracted_text += p.text + "\n"
    elif filename.endswith((".csv", ".xlsx", ".xls")):
        df = pd.read_csv(io.BytesIO(content_bytes)) if filename.endswith(".csv") else pd.read_excel(io.BytesIO(content_bytes))
        extracted_text = df.to_string(index=False)
    elif filename.endswith(".pptx"):
        prs = Presentation(io.BytesIO(content_bytes))
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    extracted_text += shape.text + "\n"
    elif filename.endswith((".html", ".htm")):
        soup = BeautifulSoup(content_bytes, "html.parser")
        extracted_text = soup.get_text(separator="\n")
    else:
        raise HTTPException(status_code=400, detail="นามสกุลไฟล์ไม่รองรับ")

    return extracted_text.strip()


@app.get("/api/health")
async def health():
    return {"status": "online", "sources_count": len(get_all_sources())}


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


@app.post("/api/chat/clear")
async def clear_web_chat(request: Request, session_id: str | None = None):
    ip = request.client.host if request.client else "unknown"
    session_key = f"{ip}_{session_id}" if session_id else ip
    web_chat_history.pop(session_key, None)
    return {"success": True, "message": "ล้างประวัติการสนทนาแล้ว"}


@app.get("/api/admin/logs")
async def get_chat_logs(admin_key: str, limit: int = 50):
    verify_admin(admin_key)
    if not LOGS_FILE.exists():
        return {"success": True, "logs": []}

    lines = LOGS_FILE.read_text(encoding="utf-8").splitlines()
    recent_logs = [json.loads(line) for line in lines[-limit:]]
    recent_logs.reverse()
    return {"success": True, "logs": recent_logs}


@app.get("/api/admin/sources")
async def get_sources(admin_key: str):
    verify_admin(admin_key)
    return {"success": True, "sources": get_all_sources()}


@app.post("/api/admin/sources/text")
async def add_text_source(data: AddTextRequest):
    verify_admin(data.admin_key)
    sources = get_all_sources()
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
    save_sources(sources)
    return {"success": True, "message": "บันทึกโน้ตเรียบร้อย"}


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

    # ป้องกัน Path Traversal
    safe_filename = Path(file.filename or "uploaded_file").name
    text = await extract_text_from_file(safe_filename, content_bytes)

    if not text:
        raise HTTPException(status_code=400, detail="ไฟล์ไม่มีข้อความที่สามารถอ่านได้")

    file_id = str(uuid.uuid4())
    saved_filename = f"{file_id}_{safe_filename}"
    file_path = UPLOAD_DIR / saved_filename
    file_path.write_bytes(content_bytes)

    sources = get_all_sources()
    new_source = {
        "id": file_id,
        "name": safe_filename,
        "type": "file",
        "path": str(file_path),
        "content": text,
        "active": True,
        "confidential": confidential,
        "created_at": time.strftime("%Y-%m-%d %H:%M")
    }
    sources.append(new_source)
    save_sources(sources)
    return {"success": True, "message": f"อัปโหลด '{safe_filename}' เรียบร้อย"}


@app.delete("/api/admin/sources/{source_id}")
async def delete_source(source_id: str, admin_key: str):
    verify_admin(admin_key)
    sources = get_all_sources()
    source_to_delete = next((s for s in sources if s["id"] == source_id), None)

    if not source_to_delete:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ที่ต้องการลบ")

    if source_to_delete.get("path") and os.path.exists(source_to_delete["path"]):
        try:
            os.remove(source_to_delete["path"])
        except Exception:
            pass

    updated_sources = [s for s in sources if s["id"] != source_id]
    save_sources(updated_sources)
    return {"success": True, "message": "ลบข้อมูลเรียบร้อยแล้ว"}


@app.patch("/api/admin/sources/{source_id}/toggle")
async def toggle_source(source_id: str, admin_key: str):
    verify_admin(admin_key)
    sources = get_all_sources()
    found = False
    for s in sources:
        if s["id"] == source_id:
            s["active"] = not s.get("active", True)
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์")
    save_sources(sources)
    return {"success": True, "message": "อัปเดตสถานะเรียบร้อย"}


if os.path.exists("frontend"):
    @app.get("/")
    async def home():
        return FileResponse("frontend/index.html")

    @app.get("/admin")
    async def admin_page():
        return FileResponse("frontend/admin.html")

    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)