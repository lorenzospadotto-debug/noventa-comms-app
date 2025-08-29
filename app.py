import os
import re
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, Query
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from starlette.middleware.sessions import SessionMiddleware
from starlette import status

# Per estrazione/creazione documenti
from io import BytesIO
from docx import Document
from PyPDF2 import PdfReader

APP_NAME = "VoxUp"

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please-very-long-secret")
ADMIN_PIN = os.getenv("ADMIN_PIN", "246810")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ----------------------------
# Directory dati (bozze/esportazioni)
# - Se DATA_DIR è impostata (es. /var/data su Render Disk), usala.
# - Altrimenti prova /var/data (comune su Render se hai montato un Disk).
# - Se non scrivibile, fallback a ./data dentro al progetto.
# ----------------------------
def get_writable_data_dir() -> str:
    candidates = []
    # 1) ENV esplicita
    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        candidates.append(env_dir)
    # 2) Path standard Render Disk
    candidates.append("/var/data")
    # 3) Cartella locale del progetto
    candidates.append(os.path.join(os.getcwd(), "data"))

    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            # Test scrittura
            testfile = os.path.join(path, ".write_test")
            with open(testfile, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(testfile)
            return path
        except Exception:
            continue
    # Estremo fallback: current dir
    return os.getcwd()

DATA_DIR = get_writable_data_dir()
DRAFTS_PATH = os.path.join(DATA_DIR, "voxup_drafts.json")
if not os.path.exists(DRAFTS_PATH):
    try:
        with open(DRAFTS_PATH, "w", encoding="utf-8") as f:
            json.dump([], f)
    except Exception:
        pass

app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ----------------------------
# Helpers
# ----------------------------
def ensure_session_defaults(session: Dict[str, Any]) -> None:
    session.setdefault("auth", False)
    session.setdefault("profile", {
        "first_name": "",
        "last_name": "",
        "role": "",
        "tones": [],
        "channels": ["Social"],
        "add_ai": False
    })
    session.setdefault("style_guide", "")
    session.setdefault("last_results", {})  # per esportazioni


def unicode_bold(text: str) -> str:
    def _bold_char(c: str) -> str:
        if 'A' <= c <= 'Z':
            return chr(ord('𝐀') + (ord(c) - ord('A')))
        if 'a' <= c <= 'z':
            return chr(ord('𝐚') + (ord(c) - ord('a')))
        if '0' <= c <= '9':
            return chr(ord('𝟎') + (ord(c) - ord('0')))
        return c
    return "".join(_bold_char(c) for c in text)


EMOJI_PATTERN = re.compile(
    "["                     
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+", flags=re.UNICODE
)

def remove_emojis(text: str) -> str:
    return EMOJI_PATTERN.sub("", text)


def format_for_channel(base_text: str, channel: str) -> str:
    if channel.lower() == "social":
        return unicode_bold(base_text)
    else:
        no_emoji = remove_emojis(base_text)
        if ":" in no_emoji:
            head, tail = no_emoji.split(":", 1)
            return f"<strong>{head.strip()}:</strong>{tail}"
        return f"<strong>{no_emoji}</strong>"


def extract_text_from_upload(filename: str, data: bytes) -> str:
    """Supporta .txt/.md, .pdf, .docx."""
    name = filename.lower()
    if name.endswith((".txt", ".md")):
        try:
            return data.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if name.endswith(".pdf"):
        try:
            reader = PdfReader(BytesIO(data))
            parts = []
            for page in reader.pages[:20]:
                text = page.extract_text() or ""
                parts.append(text)
            return "\n".join(parts).strip()
        except Exception:
            return ""
    if name.endswith(".docx"):
        try:
            doc = Document(BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            return ""
    return ""


def split_into_posts(text: str, limit: int = 280) -> List[str]:
    text = text.strip()
    if not text:
        return []
    words = text.split()
    chunks: List[str] = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip() if cur else w
        if len(candidate) <= limit:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            if len(w) > limit:
                while len(w) > limit:
                    chunks.append(w[:limit])
                    w = w[limit:]
                cur = w
            else:
                cur = w
    if cur:
        chunks.append(cur)

    if len(chunks) > 1:
        total = len(chunks)
        numbered: List[str] = []
        for i, c in enumerate(chunks, start=1):
            prefix = f"{i}/{total} "
            room = limit - len(prefix)
            numbered.append(prefix + (c[:room] if len(c) > room else c))
        return numbered
    return chunks


def save_draft(entry: Dict[str, Any]) -> None:
    try:
        with open(DRAFTS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
    except Exception:
        arr = []
    arr.insert(0, entry)
    arr = arr[:50]
    try:
        with open(DRAFTS_PATH, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME, "data_dir": DATA_DIR}

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    ensure_session_defaults(request.session)
    if request.session.get("auth"):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME})

@app.post("/login")
async def login_submit(request: Request, pin: str = Form(...)):
    ensure_session_defaults(request.session)
    if pin.strip() == ADMIN_PIN:
        request.session["auth"] = True
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "app_name": APP_NAME, "error": "PIN errato. Riprova."},
        status_code=status.HTTP_401_UNAUTHORIZED
    )

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


def require_auth(request: Request):
    ensure_session_defaults(request.session)
    return bool(request.session.get("auth"))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    profile = request.session.get("profile", {})
    return templates.TemplateResponse("home.html", {
        "request": request,
        "app_name": APP_NAME,
        "profile": profile
    })


@app.post("/save_onboarding")
async def save_onboarding(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    role: str = Form(""),
    tones: List[str] = Form(
