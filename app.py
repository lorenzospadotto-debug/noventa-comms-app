from __future__ import annotations

import json, os, re, logging, requests
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER

# --- AI client (opzionale: usa OPENAI_API_KEY se presente) ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # fallback se libreria non installata

from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text as pdf_extract_text
try:
    import docx  # python-docx
except Exception:
    docx = None

logger = logging.getLogger("uvicorn.error")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SETTINGS_PATH = BASE_DIR / "settings.json"

STATIC_DIR.mkdir(parents=True, exist_ok=True)
(TEMPLATES_DIR).mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "img").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="VoxUp — Ufficio Stampa Politico")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# -------------------------------
# Settings
# -------------------------------
DEFAULT_SETTINGS: Dict[str, Any] = {
    "first_name": "",
    "last_name": "",
    "role": "",
    "tones": ["Istituzionale"],       # elenco di aggettivi selezionati
    "tone_other": "",
    "emoji": "📣",
    "channels": ["facebook", "instagram", "x", "website", "press"],
    "social": {
        "facebook_url": "",
        "instagram_username": "",
        "x_username": "",
        "website_url": "",
        "can_publish": False,  # serve OAuth: solo URL non basta
    },
}

def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        merged = DEFAULT_SETTINGS.copy()
        for k in ["first_name","last_name","role","tones","tone_other","emoji","channels"]:
            if k in data:
                merged[k] = data[k]
        social = DEFAULT_SETTINGS["social"].copy()
        social.update(data.get("social", {}))
        merged["social"] = social
        if not isinstance(merged.get("channels"), list):
            merged["channels"] = DEFAULT_SETTINGS["channels"]
        return merged
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(data: Dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# -------------------------------
# Formatting utilities
# -------------------------------
BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF\U0001F1E6-\U0001F1FF]", flags=re.UNICODE)
_A_BOLD = ord("𝐀"); _a_BOLD = ord("𝐚"); _ZERO_BOLD = ord("𝟎")

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
# AI rewriter
# -------------------------------
def ai_rewrite(base_text: str, settings: Dict[str, Any]) -> str:
    if not base_text.strip():
        return ""
    tones = settings.get("tones") or []
    tone_other = settings.get("tone_other") or ""
    persona = f"{settings.get('first_name','').strip()} {settings.get('last_name','').strip()}".strip()
    role = settings.get("role", "").strip()
    prompt = (
        "Sei l'ufficio stampa di un politico italiano. Riscrivi il testo in modo chiaro, sintetico, adatto ai canali. "
        "Mantieni i fatti, rimuovi ridondanze, proponi un titolo, un abstract (3-4 frasi) e 3 bullet chiave. "
        f"Persona: {persona or 'N/D'} — Ruolo: {role or 'N/D'}. "
        f"Toni richiesti: {', '.join(tones)}{(' + ' + tone_other) if tone_other else ''}. "
        "Usa il markdown per il **grassetto** dove serve. Rispondi in italiano e solo con il testo finale."
    )

    api_key = os.getenv("OPENAI_API_KEY")
    if OpenAI and api_key:
        try:
            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Assistente di redazione politica, sintetico, preciso, imparziale."},
                    {"role": "user", "content": prompt + "\n\nTESTO:\n" + base_text},
                ],
                temperature=0.5,
                max_tokens=700,
            )
            return (resp.choices[0].message.content or base_text).strip()
        except Exception as e:
            logger.exception("AI fallback: %s", e)

    # Fallback locale (senza API)
    text = re.sub(r"\s+", " ", base_text).strip()
    return f"**Titolo provvisorio**\n\n{text}\n\n**Punti chiave**\n- Punto 1\n- Punto 2\n- Punto 3"

# -------------------------------
# Extractors (file & url)
# -------------------------------
def extract_text_from_bytes(filename: str, content: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith((".txt", ".md")):
        try: return content.decode("utf-8", errors="ignore")
        except Exception: return ""
    if name.endswith(".pdf"):
        try: return pdf_extract_text(BytesIO(content))
        except Exception: return ""
    if name.endswith(".docx") and docx:
        try:
            d = docx.Document(BytesIO(content))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception:
            return ""
    # fallback
    try: return content.decode("utf-8", errors="ignore")
    except Exception: return ""

def extract_text_from_url(url: str) -> str:
    if not url: return ""
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script","style","noscript"]): tag.extract()
        article = soup.find("article")
        text = (article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True))
        return "\n".join(line for line in text.splitlines() if line)
    except Exception as e:
        logger.exception("URL extract error: %s", e)
        return ""

# -------------------------------
# Build previews per canale
# -------------------------------
def build_previews(raw_text: str, settings: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
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
    return previews

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
    first_name: str = Form(""),
    last_name: str = Form(""),
    role: str = Form(""),
    tones: List[str] = Form([]),
    tone_other: str = Form(""),
    emoji: str = Form("📣"),
    facebook_url: str = Form(""),
    instagram_username: str = Form(""),
    x_username: str = Form(""),
    website_url: str = Form(""),
    channels: List[str] = Form([]),
):
    data = load_settings()
    data.update({
        "first_name": first_name,
        "last_name": last_name,
        "role": role,
        "tones": tones or ["Istituzionale"],
        "tone_other": tone_other,
        "emoji": emoji,
    })
    data["social"] = {
        "facebook_url": facebook_url.strip(),
        "instagram_username": instagram_username.strip().lstrip("@"),
        "x_username": x_username.strip().lstrip("@"),
        "website_url": website_url.strip(),
        "can_publish": False,  # finché non facciamo OAuth
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
    ai_text = ai_rewrite(raw_text, settings)
    previews = build_previews(ai_text, settings)
    return templates.TemplateResponse(
        "generate.html",
        {
            "request": request,
            "settings": settings,
            "previews": previews,
            "raw_text": raw_text,
            "ai_text": ai_text,
            "channel_labels": CHANNEL_LABELS,
        },
    )

@app.get("/upload", response_class=HTMLResponse)
async def upload_get(request: Request):
    return templates.TemplateResponse(
        "upload.html", {"request": request, "settings": load_settings()}
    )

@app.post("/upload", response_class=HTMLResponse)
async def upload_post(
    request: Request,
    file: Optional[UploadFile] = File(None),
    article_url: str = Form("")
):
    try:
        base_text = ""
        if file and file.filename:
            content = await file.read()
            base_text = extract_text_from_bytes(file.filename, content)
        elif article_url:
            base_text = extract_text_from_url(article_url)
        else:
            raise HTTPException(status_code=400, detail="Nessun file o URL fornito")

        if not base_text.strip():
            raise HTTPException(status_code=422, detail="Impossibile estrarre testo")

        settings = load_settings()
        ai_text = ai_rewrite(base_text, settings)
        previews = build_previews(ai_text, settings)
        return templates.TemplateResponse(
            "generate.html",
            {
                "request": request,
                "settings": settings,
                "previews": previews,
                "raw_text": base_text,
                "ai_text": ai_text,
                "channel_labels": CHANNEL_LABELS,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Errore upload/generazione")
        raise HTTPException(status_code=500, detail="Errore durante caricamento o generazione")

@app.get("/healthz")
def healthz():
    return {"status": "ok"}
