from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Any

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.templating import Jinja2Templates

# --- percorsi (repo root con app.py, templates/, static/, settings.json) ---
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SETTINGS_PATH = BASE_DIR / "settings.json"

# garantiamo che esista /static
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Ufficio Stampa Politico")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# -------------------------------
# Settings helpers
# -------------------------------
DEFAULT_SETTINGS: Dict[str, Any] = {
    "name": "",
    "role": "",
    "tone": "Istituzionale",
    "emoji": "🏛️",
    "channels": ["facebook", "instagram", "x", "website", "press"],
    "social": {
        "facebook_url": "",
        "instagram_username": "",
        "x_username": "",
        "website_url": "",
    },
}

def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        merged = DEFAULT_SETTINGS.copy()
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        social = DEFAULT_SETTINGS["social"].copy()
        social.update(data.get("social", {}))
        merged["social"] = social
        if not isinstance(merged.get("channels"), list):
            merged["channels"] = DEFAULT_SETTINGS["channels"]
        return merged
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(data: Dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# -------------------------------
# Formatting utilities
# -------------------------------
BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF\U0001F1E6-\U0001F1FF]",
    flags=re.UNICODE,
)

_A_BOLD = ord("𝐀")
_a_BOLD = ord("𝐚")
_ZERO_BOLD = ord("𝟎")

def _to_unicode_bold(s: str) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if "A" <= ch <= "Z":
            out.append(chr(_A_BOLD + (code - ord("A"))))
        elif "a" <= ch <= "z":
            out.append(chr(_a_BOLD + (code - ord("a"))))
        elif "0" <= ch <= "9":
            out.append(chr(_ZERO_BOLD + (code - ord("0"))))
        else:
            out.append(ch)
    return "".join(out)

def format_for_social(text: str) -> str:
    return BOLD_RE.sub(lambda m: _to_unicode_bold(m.group(1)), text)

def format_for_website(text: str) -> str:
    return BOLD_RE.sub(r"<strong>\1</strong>", text)

def format_for_press(text: str) -> str:
    no_emoji = EMOJI_RE.sub("", text)
    return BOLD_RE.sub(r"<strong>\1</strong>", no_emoji)

CHANNEL_LABELS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "x": "X (Twitter)",
    "website": "Sito",
    "press": "Stampa",
}

# -------------------------------
# Routes
# -------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    settings = load_settings()
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "settings": settings, "channel_labels": CHANNEL_LABELS},
    )

@app.get("/profile", response_class=HTMLResponse)
async def profile_get(request: Request):
    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "settings": load_settings(), "channel_labels": CHANNEL_LABELS},
    )

@app.post("/profile")
async def profile_post(
    request: Request,
    name: str = Form(""),
    role: str = Form(""),
    tone: str = Form("Istituzionale"),
    emoji: str = Form("🏛️"),
    facebook_url: str = Form(""),
    instagram_username: str = Form(""),
    x_username: str = Form(""),
    website_url: str = Form(""),
    channels: List[str] = Form([]),
):
    data = load_settings()
    data.update({"name": name, "role": role, "tone": tone, "emoji": emoji})
    data["social"] = {
        "facebook_url": facebook_url.strip(),
        "instagram_username": instagram_username.strip().lstrip("@"),
        "x_username": x_username.strip().lstrip("@"),
        "website_url": website_url.strip(),
    }
    valid = [c for c in channels if c in CHANNEL_LABELS]
    data["channels"] = valid or DEFAULT_SETTINGS["channels"]
    save_settings(data)
    return RedirectResponse(url="/profile", status_code=HTTP_303_SEE_OTHER)

@app.get("/generate", response_class=HTMLResponse)
async def generate_get(request: Request):
    return templates.TemplateResponse(
        "generate.html",
        {"request": request, "settings": load_settings(), "channel_labels": CHANNEL_LABELS},
    )

@app.post("/generate", response_class=HTMLResponse)
async def generate_post(request: Request, raw_text: str = Form("")):
    settings = load_settings()
    chosen = settings.get("channels", [])
    previews: Dict[str, Dict[str, Any]] = {}

    for ch in chosen:
        if ch in ("facebook", "instagram", "x"):
            formatted = format_for_social(raw_text)
            char_limit = 280 if ch == "x" else None
        elif ch == "website":
            formatted = format_for_website(raw_text)
            char_limit = None
        elif ch == "press":
            formatted = format_for_press(raw_text)
            char_limit = None
        else:
            formatted = raw_text
            char_limit = None
        previews[ch] = {
            "formatted": formatted,
            "char_limit": char_limit,
            "length": len(formatted),
            "label": CHANNEL_LABELS.get(ch, ch.title()),
        }

    return templates.TemplateResponse(
        "generate.html",
        {"request": request, "settings": settings, "previews": previews, "raw_text": raw_text, "channel_labels": CHANNEL_LABELS},
    )

@app.get("/upload", response_class=HTMLResponse)
async def upload_get(request: Request):
    return templates.TemplateResponse(
        "upload.html", {"request": request, "settings": load_settings()}
    )

@app.post("/upload")
async def upload_post(file: UploadFile = File(...)):
    _ = await file.read()
    return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)
