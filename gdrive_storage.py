import io
import os
import json
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

from pypdf import PdfReader
import docx
import pandas as pd
from pptx import Presentation
from bs4 import BeautifulSoup

SCOPES = ['https://www.googleapis.com/auth/drive']
FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")

# Cache ชั่วคราวใน RAM ลดการเรียก API ซ้ำซ้อน
_cache = {
    "sources": None,
    "last_fetch": 0
}
CACHE_TTL = 60  # อัปเดตข้อมูลจาก Drive ทุกๆ 60 วินาที


def get_drive_service():
    """เชื่อมต่อ Google Drive ผ่านไฟล์หรือ Environment Variable (เหมาะสำหรับ Cloud Server)"""
    creds_json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json_str:
        info = json.loads(creds_json_str)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists("service_account.json"):
        creds = service_account.Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    else:
        raise RuntimeError("ไม่พบข้อมูลยืนยันตัวตน Google Drive (service_account.json หรือ GOOGLE_SERVICE_ACCOUNT_JSON)")

    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def extract_text_bytes(filename: str, content_bytes: bytes) -> str:
    """แปลงไฟล์เอกสารต่างๆ เป็นข้อความ"""
    fname = filename.lower()
    text = ""
    try:
        if fname.endswith((".txt", ".md", ".log", ".json", ".csv")):
            text = content_bytes.decode("utf-8", errors="ignore")
        elif fname.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(content_bytes))
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
        elif fname.endswith(".docx"):
            doc = docx.Document(io.BytesIO(content_bytes))
            for p in doc.paragraphs:
                if p.text:
                    text += p.text + "\n"
        elif fname.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content_bytes))
            text = df.to_string(index=False)
        elif fname.endswith(".pptx"):
            prs = Presentation(io.BytesIO(content_bytes))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        text += shape.text + "\n"
        elif fname.endswith((".html", ".htm")):
            soup = BeautifulSoup(content_bytes, "html.parser")
            text = soup.get_text(separator="\n")
    except Exception as e:
        print(f"EXTRACT ERROR ({filename}):", e)
    return text.strip()


def _find_file_in_folder(service, filename: str):
    query = f"name = '{filename}' and '{FOLDER_ID}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None


def get_all_sources(force_refresh: bool = False) -> list:
    """ดึงข้อมูลทั้งหมดจาก sources.json บน Drive และไฟล์เอกสารที่คุณโยนใส่โฟลเดอร์ Drive โดยตรง"""
    if not FOLDER_ID:
        return []

    now = time.time()
    if not force_refresh and _cache["sources"] is not None and (now - _cache["last_fetch"] < CACHE_TTL):
        return _cache["sources"]

    try:
        service = get_drive_service()
        sources = []

        # 1. อ่านไฟล์ sources.json (โน้ตและไฟล์ที่เพิ่มผ่านระบบ Admin)
        sources_file_id = _find_file_in_folder(service, "sources.json")
        if sources_file_id:
            request = service.files().get_media(fileId=sources_file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            sources = json.loads(fh.getvalue().decode("utf-8"))

        # 2. อ่านไฟล์เอกสารอื่นๆ ที่คุณอัปโหลดลงโฟลเดอร์ Google Drive โดยตรง
        tracked_drive_ids = {s.get("drive_file_id") for s in sources if s.get("drive_file_id")}
        query = f"'{FOLDER_ID}' in parents and trashed = false and name != 'sources.json' and name != 'chat_logs.jsonl'"
        results = service.files().list(q=query, fields="files(id, name, mimeType, createdTime)").execute()

        updated_from_drive = False
        for f in results.get("files", []):
            if f["id"] in tracked_drive_ids:
                continue

            # ดาวน์โหลดไฟล์ใหม่ที่เพิ่งโยนเข้า Drive มาแปลงเป็นข้อความ
            if f["mimeType"] == "application/vnd.google-apps.document":
                req = service.files().export_media(fileId=f["id"], mimeType="text/plain")
            elif f["mimeType"] == "application/vnd.google-apps.spreadsheet":
                req = service.files().export_media(fileId=f["id"], mimeType="text/csv")
            else:
                req = service.files().get_media(fileId=f["id"])

            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            ext_name = f["name"] + (".txt" if "google-apps" in f["mimeType"] else "")
            extracted = extract_text_bytes(ext_name, fh.getvalue())
            if extracted:
                sources.append({
                    "id": f["id"],
                    "drive_file_id": f["id"],
                    "name": f["name"],
                    "type": "gdrive_direct",
                    "content": extracted,
                    "active": True,
                    "confidential": False,
                    "created_at": f.get("createdTime", "")[:16].replace("T", " ")
                })
                updated_from_drive = True

        if updated_from_drive:
            save_sources(sources)

        _cache["sources"] = sources
        _cache["last_fetch"] = now
        return sources

    except Exception as e:
        print("GDRIVE READ ERROR:", e)
        return _cache["sources"] or []


def save_sources(sources: list):
    """บันทึกข้อมูลกลับไปยัง sources.json บน Google Drive"""
    if not FOLDER_ID:
        return
    try:
        service = get_drive_service()
        file_id = _find_file_in_folder(service, "sources.json")
        json_bytes = json.dumps(sources, ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype="application/json", resumable=True)

        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            metadata = {"name": "sources.json", "parents": [FOLDER_ID]}
            service.files().create(body=metadata, media_body=media).execute()

        _cache["sources"] = sources
        _cache["last_fetch"] = time.time()
    except Exception as e:
        print("GDRIVE SAVE ERROR:", e)


def upload_file_to_drive(filename: str, content_bytes: bytes, mime_type: str = "application/octet-stream") -> str:
    """อัปโหลดไฟล์จริงขึ้นไปเก็บใน Google Drive"""
    service = get_drive_service()
    metadata = {"name": filename, "parents": [FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime_type, resumable=True)
    created = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return created.get("id")


def delete_file_from_drive(drive_file_id: str):
    """ลบไฟล์ออกจาก Google Drive"""
    try:
        service = get_drive_service()
        service.files().delete(fileId=drive_file_id).execute()
    except Exception as e:
        print("GDRIVE DELETE ERROR:", e)