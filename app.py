import os, re, json, sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from io import BytesIO

from fastapi import FastAPI, Request, Form, UploadFile, File, Query, Response
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette import status

from PyPDF2 import PdfReader
from docx import Document
from passlib.hash import pbkdf2_sha256

# ---------- OpenAI opzionale ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        ai_client = None  # se fallisce, useremo fallback

APP_NAME = "VoxUp"
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please-very-long-secret")

# ---------- Env numeriche robuste ----------
def get_int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    m = re.search(r"(\d+)", str(val))
    return int(m.group(1)) if m else default

MAX_UPLOAD_MB = get_int_env("MAX_UPLOAD_MB", 20)     # request intera
MAX_FILE_MB   = get_int_env("MAX_FILE_MB", 15)       # per singolo file
PDF_PAGE_LIMIT= get_int_env("PDF_PAGE_LIMIT", 10)    # pagine lette per PDF

# ---------- Cartelle dati & DB ----------
def get_writable_data_dir() -> str:
    for p in [os.getenv("DATA_DIR"), "/var/data", os.path.join(os.getcwd(), "data")]:
        if not p: continue
        try:
            os.makedirs(p, exist_ok=True)
            test = os.path.join(p, ".w"); open(test, "w").write("ok"); os.remove(test)
            return p
        except Exception:
            continue
    return os.getcwd()

DATA_DIR = get_writable_data_dir()
DB_PATH = os.path.join(DATA_DIR, "voxup.sqlite3")
DRAFTS_PATH = os.path.join(DATA_DIR, "voxup_drafts.json")
NOTES_PATH  = os.path.join(DATA_DIR, "voxup_notes.json")
NEWS_CACHE  = os.path.join(DATA_DIR, "voxup_news_cache.json")
for path, default in [(DRAFTS_PATH, []), (NOTES_PATH, [])]:
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f: json.dump(default, f)
        except Exception:
            pass

def db_connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def db_init():
    conn = db_connect(); cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE,
      role TEXT DEFAULT '',
      password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    """)
    conn.commit(); conn.close()
db_init()

# ---------- App ----------
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------- Helpers ----------
def ensure_session_defaults(session: Dict[str, Any]) -> None:
    session.setdefault("auth", False)
    session.setdefault("user", None)
    session.setdefault("onboarding_done", False)
    session.setdefault("profile", {
        "first_name": "", "last_name": "", "role": "",
        "tones": [], "tone_other": "",
        "channels": ["Social"],
        "add_ai": bool(ai_client)  # ON di default se la chiave c'è
    })
    session.setdefault("style_guide", "")
    session.setdefault("last_results", {})

def unicode_bold(s: str) -> str:
    def b(c: str):
        if 'A' <= c <= 'Z': return chr(ord('𝐀') + (ord(c)-ord('A')))
        if 'a' <= c <= 'z': return chr(ord('𝐚') + (ord(c)-ord('a')))
        if '0' <= c <= '9': return chr(ord('𝟎') + (ord(c)-ord('0')))
        return c
    return "".join(b(c) for c in s)

EMOJI_RE = re.compile("["                     
    "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\u2600-\u26FF\u2700-\u27BF]+")
def remove_emojis(s: str) -> str: return EMOJI_RE.sub("", s)

def format_for_channel(text: str, channel: str) -> str:
    if channel.lower() == "social":
        return unicode_bold(text)
    clean = remove_emojis(text)
    if ":" in clean:
        h, t = clean.split(":", 1)
        return f"<strong>{h.strip()}:</strong>{t}"
    return f"<strong>{clean}</strong>"

def extract_text_from_upload(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith((".txt",".md")):
        return data.decode("utf-8", errors="ignore")
    if name.endswith(".pdf"):
        try:
            r = PdfReader(BytesIO(data)); out=[]
            for i, p in enumerate(r.pages):
                if i >= PDF_PAGE_LIMIT: break
                out.append(p.extract_text() or "")
            return "\n".join(out).strip()
        except Exception:
            return ""
    if name.endswith(".docx"):
        try:
            d = Document(BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception:
            return ""
    return ""

def split_into_posts(text: str, limit: int = 280) -> List[str]:
    words = text.strip().split()
    if not words: return []
    res, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip() if cur else w
        if len(cand) <= limit: cur = cand
        else:
            if cur: res.append(cur)
            while len(w) > limit: res.append(w[:limit]); w = w[limit:]
            cur = w
    if cur: res.append(cur)
    if len(res) > 1:
        total = len(res)
        res = [f"{i}/{total} " + chunk for i, chunk in enumerate(res, 1)]
    return res

async def read_limited(upload: UploadFile, byte_limit: int) -> bytes:
    CHUNK = 1024*1024
    buf = bytearray(); got = 0
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk: break
        got += len(chunk)
        if got > byte_limit: raise ValueError("file_too_large")
        buf.extend(chunk)
    return bytes(buf)

def save_draft(entry: Dict[str, Any]) -> None:
    try:
        arr = json.load(open(DRAFTS_PATH, "r", encoding="utf-8"))
    except Exception:
        arr = []
    arr.insert(0, entry); arr = arr[:50]
    try:
        json.dump(arr, open(DRAFTS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass

# ---------- AI ----------
def build_ai_prompt(ctx: str, profile: Dict[str, Any], style: str) -> Tuple[str,str]:
    full = f"{profile.get('first_name','').strip()} {profile.get('last_name','').strip()}".strip()
    role = profile.get("role","").strip()
    tones = profile.get("tones", []); other = profile.get("tone_other","").strip()
    tono = ", ".join([t for t in tones if t] + ([other] if other else [])) or "istituzionale"
    sys = ("Sei un addetto stampa politico italiano. Rielabora il contenuto in modo chiaro, sintetico e verificabile. "
           "Inserisci una citazione del politico. Non inventare fatti.")
    usr = (f"Ruolo: {role}\nNome: {full}\nToni: {tono}\nStile guida: {style[:600]}\n\n"
           f"Contesto:\n{ctx[:6000]}\n\nScrivi un comunicato breve (6-10 frasi) con una citazione in virgolette.")
    return sys, usr

def ai_rewrite_or_fallback(ctx: str, profile: Dict[str, Any], style: str) -> str:
    # Se c'è client, prova AI
    try:
        if ai_client:
            sys, usr = build_ai_prompt(ctx, profile, style)
            r = ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":usr}],
                temperature=0.5, max_tokens=700
            )
            out = (r.choices[0].message.content or "").strip()
            if out: return out + "\n\n[rielaborato con AI]"
    except Exception:
        pass
    # Fallback deterministico
    full = f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
    role = profile.get("role","").strip()
    return (f"{role} {full} — Sintesi punti chiave:\n- {ctx[:240]}...\n\n"
            "Dichiarazione: \"Mettiamo al centro cittadini e territori con serietà e risultati.\""
            "\n\n[modalità AI non attiva o non disponibile]")

def require_auth(request: Request) -> bool:
    ensure_session_defaults(request.session)
    return bool(request.session.get("auth"))

# ---------- News (RSS) con cache ----------
FEEDS = {
    "ANSA": "https://www.ansa.it/sito/ansait_rss.xml",
    "Repubblica": "https://www.repubblica.it/rss/homepage/rss2.0.xml",
    "Corriere": "https://xml2.corriereobjects.it/rss/homepage.xml",
    "Il Sole 24 Ore": "https://www.ilsole24ore.com/rss/italia.xml",
    "AGI": "https://www.agi.it/rss/ultime-notizie.xml",
    "BBC": "http://feeds.bbci.co.uk/news/rss.xml",
    "Reuters": "http://feeds.reuters.com/reuters/topNews",
    "AP": "https://apnews.com/hub/apf-topnews?utm_source=ap_rss&utm_medium=rss&utm_campaign=ap_rss",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "Politico EU": "https://www.politico.eu/feed/"
}
NEWS_TTL_MIN = 15

def news_cache_load():
    try:
        data = json.load(open(NEWS_CACHE, "r", encoding="utf-8"))
        return data.get("items", []), datetime.fromisoformat(data.get("ts"))
    except Exception:
        return [], datetime.min

def news_cache_save(items: List[Dict[str, Any]]):
    try:
        json.dump({"ts": datetime.utcnow().isoformat(), "items": items},
                  open(NEWS_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

def news_fetch() -> List[Dict[str, Any]]:
    try:
        import feedparser
    except Exception:
        return []
    items = []
    for src, url in FEEDS.items():
        try:
            f = feedparser.parse(url)
            for e in f.entries[:10]:
                title = getattr(e, "title", "").strip()
                link = getattr(e, "link", "")
                pub  = getattr(e, "published", "") or getattr(e, "updated", "")
                if title and link:
                    items.append({"title": title, "link": link, "source": src, "published": pub})
        except Exception:
            continue
    seen, out = set(), []
    for it in items:
        k = (it["title"][:140], it["source"])
        if k not in seen: seen.add(k); out.append(it)
    return out[:60]

def news_get() -> List[Dict[str, Any]]:
    cached, ts = news_cache_load()
    if ts != datetime.min and datetime.utcnow() - ts < timedelta(minutes=NEWS_TTL_MIN) and cached:
        return cached
    items = news_fetch()
    if items: news_cache_save(items); return items
    return cached

# ---------- Error handler gentile ----------
@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception):
    # NON mandiamo 500 in pagina Cloudflare; mostriamo pagina Compose con messaggio.
    return templates.TemplateResponse("compose.html", {
        "request": request, "app_name": APP_NAME,
        "profile": request.session.get("profile", {}),
        "results": None, "file_previews": [],
        "errors": [f"Errore: {type(exc).__name__}"]
    }, status_code=200)

# ---------- Health ----------
@app.head("/")
def head_root(): return RedirectResponse(url="/login", status_code=302)

@app.get("/health")
def health(): return {"status": "ok", "app": APP_NAME, "data_dir": DATA_DIR}

# ---------- Auth ----------
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    ensure_session_defaults(request.session)
    if request.session.get("auth"): return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request, "app_name": APP_NAME})

@app.post("/register")
def register_submit(request: Request,
    name: str = Form(...), email: str = Form(...), role: str = Form(""),
    password: str = Form(...), password2: str = Form(...)):
    ensure_session_defaults(request.session)
    if password != password2 or len(password) < 6:
        return templates.TemplateResponse("register.html", {"request": request, "app_name": APP_NAME,
            "error":"Password non valida (min 6) o non coincidono."}, status_code=400)
    email = email.strip().lower(); name = name.strip(); role = role.strip()
    pw = pbkdf2_sha256.hash(password)
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute("INSERT INTO users(name,email,role,password_hash,created_at) VALUES(?,?,?,?,?)",
                    (name, email, role, pw, datetime.utcnow().isoformat()+"Z"))
        conn.commit(); conn.close()
    except sqlite3.IntegrityError:
        return templates.TemplateResponse("register.html", {"request": request, "app_name": APP_NAME,
            "error":"Email già registrata."}, status_code=400)
    request.session["auth"]=True
    request.session["user"]={"name":name,"email":email,"role":role}
    request.session["profile"]={
        "first_name": name.split(" ")[0] if name else "",
        "last_name": " ".join(name.split(" ")[1:]) if len(name.split(" "))>1 else "",
        "role": role, "tones": [], "tone_other": "", "channels":["Social"], "add_ai": bool(ai_client)
    }
    return RedirectResponse(url="/", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    ensure_session_defaults(request.session)
    if request.session.get("auth"): return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME})

@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    ensure_session_defaults(request.session)
    email = email.strip().lower()
    conn = db_connect(); cur = conn.cursor()
    cur.execute("SELECT id,name,email,role,password_hash FROM users WHERE email=?", (email,))
    row = cur.fetchone(); conn.close()
    if not row or not pbkdf2_sha256.verify(password, row["password_hash"]):
        return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME,
            "error":"Credenziali non valide."}, status_code=401)
    request.session["auth"]=True
    request.session["user"]={"name":row["name"],"email":row["email"],"role":row["role"]}
    prof = request.session.get("profile", {})
    if not prof.get("role"): prof["role"]=row["role"] or ""
    request.session["profile"]=prof
    return RedirectResponse(url="/", status_code=302)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

# ---------- Onboarding ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    if request.session.get("onboarding_done"): return RedirectResponse(url="/compose", status_code=302)
    return templates.TemplateResponse("home.html", {
        "request": request, "app_name": APP_NAME,
        "profile": request.session.get("profile", {}), "user": request.session.get("user") or {}
    })

@app.post("/save_onboarding")
def save_onboarding(request: Request,
    first_name: str = Form(""), last_name: str = Form(""), role: str = Form(""),
    tones: List[str] = Form([]), channels: List[str] = Form(["Social"]),
    tone_other: str = Form("")):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    ensure_session_defaults(request.session)
    p = request.session["profile"]
    p["first_name"]=first_name.strip(); p["last_name"]=last_name.strip(); p["role"]=role.strip()
    p["tones"]=tones; p["channels"]=channels or ["Social"]; p["tone_other"]=tone_other.strip()
    request.session["profile"]=p; request.session["onboarding_done"]=True
    return RedirectResponse(url="/compose", status_code=302)

# ---------- Compose / Generate ----------
@app.get("/compose", response_class=HTMLResponse)
def compose_page(request: Request):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("compose.html", {
        "request": request, "app_name": APP_NAME,
        "profile": request.session.get("profile", {}),
        "results": None, "file_previews": [], "errors": []
    })

@app.post("/generate", response_class=HTMLResponse)
async def generate(request: Request,
    text_input: str = Form(""), url_input: str = Form(""),
    split_x: Optional[str] = Form(None),
    files: List[UploadFile] = File(None)):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    ensure_session_defaults(request.session)
    profile = request.session["profile"]; style = request.session.get("style_guide", "")
    add_ai = profile.get("add_ai", False)

    errors: List[str] = []; bodies: List[str] = []; previews = []

    # blocco request size (evita crash 502)
    try:
        clen = int(request.headers.get("content-length") or "0")
        if clen > MAX_UPLOAD_MB*1024*1024:
            errors.append(f"Dimensione totale troppo grande (>{MAX_UPLOAD_MB} MB).")
    except Exception:
        pass

    if text_input.strip(): bodies.append(text_input.strip())
    if url_input.strip():  bodies.append(f"Fonte: {url_input.strip()}")

    if files:
        for f in files:
            raw = b""
            try:
                raw = await read_limited(f, MAX_FILE_MB*1024*1024)
            except ValueError:
                errors.append(f"{f.filename}: supera {MAX_FILE_MB} MB, escluso.")
            except Exception as ex:
                errors.append(f"{f.filename}: errore lettura: {str(ex)[:120]}")
            txt = extract_text_from_upload(f.filename, raw) if raw else ""
            snippet = txt[:500] + ("…" if len(txt) > 500 else "")
            if not txt and raw: snippet = "(Anteprima non disponibile: formato non supportato o PDF/Docx protetto)"
            if not raw: snippet = "(File non elaborato)"
            previews.append({"name": f.filename, "snippet": snippet})
            if txt: bodies.append(f"File {f.filename}:\n{txt[:4000]}")

    base_ctx = "\n\n".join(bodies).strip() or "Nessun testo inserito."
    out_text = ai_rewrite_or_fallback(base_ctx, profile, style) if add_ai else base_ctx + ("\n\n[AI disattivata]")
    channels = profile.get("channels", ["Social"])

    # build risultati
    results: Dict[str, Any] = {}
    do_split = (split_x == "on")
    for ch in channels:
        if ch == "Social" and do_split:
            pieces = [unicode_bold(p) for p in split_into_posts(out_text, 280)]
            results[ch] = pieces
        else:
            results[ch] = format_for_channel(out_text, ch)

    request.session["last_results"] = results
    save_draft({"ts": datetime.utcnow().isoformat()+"Z", "profile": profile,
                "input": {"text": text_input, "url": url_input}, "results": results})

    return templates.TemplateResponse("compose.html", {
        "request": request, "app_name": APP_NAME,
        "profile": profile, "results": results,
        "file_previews": previews, "split_used": do_split, "errors": errors
    })

# ---------- Export ----------
@app.get("/export")
def export_result(request: Request, channel: str = Query(...), fmt: str = Query(...)):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    results = request.session.get("last_results", {})
    if not results or channel not in results:
        return PlainTextResponse("Nessun risultato da esportare per questo canale.", status_code=400)
    value = results[channel]; safe = channel.lower()

    if isinstance(value, list):
        joined = "\n\n".join(value)
        if fmt == "txt":
            p = os.path.join(DATA_DIR, f"voxup_{safe}.txt")
            open(p, "w", encoding="utf-8").write(joined)
            return FileResponse(p, media_type="text/plain", filename=f"{APP_NAME}_{safe}.txt")
        if fmt == "html":
            p = os.path.join(DATA_DIR, f"voxup_{safe}.html")
            open(p, "w", encoding="utf-8").write(f"<!doctype html><meta charset='utf-8'><body>{'<br>'.join(value)}</body>")
            return FileResponse(p, media_type="text/html", filename=f"{APP_NAME}_{safe}.html")
        return PlainTextResponse("Formato non supportato per lista.", status_code=400)

    content = str(value)
    if fmt == "txt":
        plain = re.sub(r"<[^>]+>", "", content)
        p = os.path.join(DATA_DIR, f"voxup_{safe}.txt")
        open(p, "w", encoding="utf-8").write(plain)
        return FileResponse(p, media_type="text/plain", filename=f"{APP_NAME}_{safe}.txt")
    if fmt == "html":
        html = content if channel != "Social" else f"<pre>{content}</pre>"
        p = os.path.join(DATA_DIR, f"voxup_{safe}.html")
        open(p, "w", encoding="utf-8").write(f"<!doctype html><meta charset='utf-8'><body>{html}</body>")
        return FileResponse(p, media_type="text/html", filename=f"{APP_NAME}_{safe}.html")
    if fmt == "docx" and channel == "Stampa":
        doc = Document(); plain = re.sub(r"<[^>]+>", "", content)
        for line in plain.split("\n"): doc.add_paragraph(line)
        p = os.path.join(DATA_DIR, f"voxup_{safe}.docx"); doc.save(p)
        return FileResponse(p, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            filename=f"{APP_NAME}_{safe}.docx")
    return PlainTextResponse("Formato non supportato.", status_code=400)

# ---------- Pagine varie ----------
@app.get("/style", response_class=HTMLResponse)
def style_page(request: Request):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("style.html", {"request": request, "app_name": APP_NAME,
                                                     "style_guide": request.session.get("style_guide","")})

@app.post("/style")
def style_save(request: Request, style_guide: str = Form("")):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    request.session["style_guide"] = style_guide.strip()
    return RedirectResponse(url="/style", status_code=302)

@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("profile.html", {"request": request, "app_name": APP_NAME,
                                                       "profile": request.session.get("profile", {}),
                                                       "user": request.session.get("user") or {}})

@app.post("/profile")
def profile_save(request: Request,
    first_name: str = Form(""), last_name: str = Form(""), role: str = Form(""),
    tones: List[str] = Form([]), channels: List[str] = Form(["Social"]),
    add_ai: Optional[str] = Form(None), tone_other: str = Form("")):
    if not require_auth(request): return RedirectResponse(url="/login", status_code=302)
    ensure_session_defaults(request.session)
    p = request.session["profile"]
    p["first_name"]=first_name.strip(); p["last_name"]=last_name.strip(); p["role"]=role.strip()
    p["tones"]=tones; p["channels"]=channels or ["Social"]; p["tone_other"]=tone_other.strip()
    p["add_ai"] = (add_ai == "on")
    request.session["profile"]=p
    return RedirectResponse(url="/profile", status_code=302)

@app.get("/news.json")
def news_json(): return JSONResponse(news_get())
