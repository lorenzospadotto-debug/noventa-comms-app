import os, re, json, sqlite3, requests
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from io import BytesIO

from fastapi import FastAPI, Request, Form, UploadFile, File, Query
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from PyPDF2 import PdfReader
from docx import Document
from passlib.hash import pbkdf2_sha256

# ---------- OpenAI opzionale ma AI SEMPRE ON ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        ai_client = None

APP_NAME = "VoxUp"
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please-very-long-secret")

def get_int_env(name: str, default: int) -> int:
    v = os.getenv(name, "")
    m = re.search(r"(\d+)", v)
    return int(m.group(1)) if m else default

MAX_UPLOAD_MB = get_int_env("MAX_UPLOAD_MB", 20)
MAX_FILE_MB   = get_int_env("MAX_FILE_MB", 15)
PDF_PAGE_LIMIT= get_int_env("PDF_PAGE_LIMIT", 10)

def get_writable_data_dir() -> str:
    for p in [os.getenv("DATA_DIR"), "/var/data", os.path.join(os.getcwd(), "data")]:
        if not p: continue
        try:
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, ".w"), "w") as f: f.write("ok")
            os.remove(os.path.join(p, ".w"))
            return p
        except Exception:
            continue
    return os.getcwd()

DATA_DIR = get_writable_data_dir()
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "voxup.sqlite3")
DRAFTS_PATH = os.path.join(DATA_DIR, "voxup_drafts.json")
NOTES_PATH  = os.path.join(DATA_DIR, "voxup_notes.json")
NEWS_CACHE  = os.path.join(DATA_DIR, "voxup_news_cache.json")
for p, default in [(DRAFTS_PATH, []), (NOTES_PATH, [])]:
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f: json.dump(default, f)

def db_connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    conn = db_connect(); cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE,
      role TEXT DEFAULT '',
      ente TEXT DEFAULT '',
      password_hash TEXT NOT NULL,
      photo_path TEXT DEFAULT '',
      header_image_url TEXT DEFAULT '',
      created_at TEXT NOT NULL
    );
    """)
    conn.commit(); conn.close()
db_init()

app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ----------------- Utils -----------------
def ensure_session(session: Dict[str, Any]):
    session.setdefault("auth", False)
    session.setdefault("user", None)
    session.setdefault("onboarding_done", False)
    session.setdefault("profile", {
        "first_name":"", "last_name":"", "role":"", "ente":"",
        "tones": [], "tone_other":"", "channels":["Social"],
        "add_ai": True,               # AI SEMPRE ON
        "photo_path":"", "header_image_url":""
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
    return clean if "<strong>" in clean else f"<strong>{clean.split(':',1)[0]}</strong>" + (":" + clean.split(":",1)[1] if ":" in clean else "")

def extract_text_from_upload(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith((".txt",".md")):
        return data.decode("utf-8", errors="ignore")
    if name.endswith(".pdf"):
        try:
            reader = PdfReader(BytesIO(data))
            out=[]
            for i, p in enumerate(reader.pages):
                if i>=PDF_PAGE_LIMIT: break
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

async def read_limited(upload: UploadFile, byte_limit: int) -> bytes:
    CHUNK = 1024*1024
    buf = bytearray(); got=0
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk: break
        got += len(chunk)
        if got > byte_limit: raise ValueError("file_too_large")
        buf.extend(chunk)
    return bytes(buf)

def save_draft(entry: Dict[str,Any]):
    try:
        arr = json.load(open(DRAFTS_PATH,"r",encoding="utf-8"))
    except Exception:
        arr=[]
    arr.insert(0,entry); arr = arr[:50]
    json.dump(arr, open(DRAFTS_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

# --------- AI core (sempre attiva) ----------
def build_press_release_prompt(ctx: str, profile: Dict[str,Any], style_guide: str) -> Tuple[str,str]:
    full = f"{profile.get('first_name','').strip()} {profile.get('last_name','').strip()}".strip()
    role = profile.get("role","").strip()
    ente = profile.get("ente","").strip()
    tones = profile.get("tones", []); other = profile.get("tone_other","").strip()
    tone = ", ".join([t for t in tones if t] + ([other] if other else [])) or "istituzionale"
    sys = ("Sei un ufficio stampa politico italiano. "
           "Produci un COMUNICATO STAMPA in italiano, chiaro e giornalisticamente solido. "
           "Struttura: titolo incisivo, attacco di 2-3 frasi, corpo con punti chiave, "
           "citazione diretta del politico tra virgolette, call-to-action o info pratiche finali. "
           "Tono: professionale ma umano. Non inventare fatti. Se mancano dettagli, resta generico.")
    usr = (f"Politico: {full} ({role}) — Ente: {ente}\n"
           f"Toni richiesti: {tone}\n"
           f"Stile guida (estratto): {style_guide[:600]}\n\n"
           f"Contesto / materiale da rielaborare:\n{ctx[:8000]}\n\n"
           "Genera il comunicato. Output solo testo (HTML semplice consentito per <strong>).")
    return sys, usr)

def ai_press_release(ctx: str, profile: Dict[str,Any], style_guide: str) -> str:
    # AI: se disponibile usa OpenAI, altrimenti fallback che sintetizza
    try:
        if ai_client:
            sys, usr = build_press_release_prompt(ctx, profile, style_guide)
            r = ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":usr}],
                temperature=0.5, max_tokens=1100
            )
            out = (r.choices[0].message.content or "").strip()
            if out: return out
    except Exception:
        pass
    # Fallback semplice ma “giornalistico”
    lines = [l.strip() for l in ctx.splitlines() if l.strip()]
    abstract = " ".join(lines)[:800]
    return (
        "<strong>Comunicato stampa</strong>\n"
        f"{abstract}\n\n"
        "«Mettiamo al centro i bisogni dei cittadini con serietà e risultati» — dichiarano gli uffici dell'Ente.\n"
        "Per informazioni: Ufficio Stampa."
    )

# --------- News (RSS) ----------
FEEDS = {
    "ANSA":"https://www.ansa.it/sito/ansait_rss.xml",
    "Repubblica":"https://www.repubblica.it/rss/homepage/rss2.0.xml",
    "Corriere":"https://xml2.corriereobjects.it/rss/homepage.xml",
    "Il Sole 24 Ore":"https://www.ilsole24ore.com/rss/italia.xml",
    "AGI":"https://www.agi.it/rss/ultime-notizie.xml",
    "BBC":"http://feeds.bbci.co.uk/news/rss.xml",
    "Reuters":"http://feeds.reuters.com/reuters/topNews",
    "AP":"https://apnews.com/hub/apf-topnews?utm_source=ap_rss&utm_medium=rss&utm_campaign=ap_rss",
    "The Guardian":"https://www.theguardian.com/world/rss",
    "Politico EU":"https://www.politico.eu/feed/"
}
NEWS_TTL_MIN = 15

def news_cache_load():
    try:
        d = json.load(open(NEWS_CACHE,"r",encoding="utf-8"))
        return d.get("items",[]), datetime.fromisoformat(d.get("ts"))
    except Exception:
        return [], datetime.min

def news_cache_save(items):
    json.dump({"ts": datetime.utcnow().isoformat(), "items":items}, open(NEWS_CACHE,"w",encoding="utf-8"), ensure_ascii=False)

def fetch_feeds():
    try:
        import feedparser
    except Exception:
        return []
    items=[]
    for src, url in FEEDS.items():
        try:
            f = feedparser.parse(url)
            for e in f.entries[:10]:
                t = getattr(e,"title","").strip(); link = getattr(e,"link",""); pub = getattr(e,"published","") or getattr(e,"updated","")
                if t and link: items.append({"title":t, "link":link, "source":src, "published":pub})
        except Exception:
            continue
    # dedup
    seen=set(); out=[]
    for it in items:
        k=(it["title"][:140], it["source"])
        if k not in seen: seen.add(k); out.append(it)
    return out[:60]

def get_news():
    cached, ts = news_cache_load()
    if ts!=datetime.min and datetime.utcnow()-ts < timedelta(minutes=NEWS_TTL_MIN) and cached:
        return cached
    items = fetch_feeds()
    if items:
        news_cache_save(items)
        return items
    return cached

# --------- Wikimedia banner by ente (best-effort) ----------
def try_fetch_wikimedia_header(ente: str) -> str:
    try:
        if not ente: return ""
        # 1) ricerca pagina
        r = requests.get("https://it.wikipedia.org/w/api.php", params={
            "action":"opensearch","search":ente,"limit":1,"namespace":0,"format":"json"
        }, timeout=6)
        arr = r.json()
        if not arr or len(arr)<4 or not arr[1]: return ""
        title = arr[1][0]
        # 2) immagine principale
        r2 = requests.get("https://it.wikipedia.org/w/api.php", params={
            "action":"query","titles":title,"prop":"pageimages","pithumbsize":1200,"format":"json"
        }, timeout=6)
        data = r2.json()
        pages = data.get("query",{}).get("pages",{})
        for _,pg in pages.items():
            thumb = pg.get("thumbnail",{}).get("source")
            if thumb: return thumb
    except Exception:
        return ""
    return ""

# ----------------- Error handler soft -----------------
@app.exception_handler(Exception)
async def any_exc(request: Request, exc: Exception):
    ensure_session(request.session)
    return templates.TemplateResponse("compose.html", {
        "request": request, "app_name": APP_NAME,
        "profile": request.session.get("profile", {}),
        "results": None, "file_previews": [],
        "errors": [f"Errore: {type(exc).__name__}"]
    }, status_code=200)

# ----------------- Health/Head -----------------
@app.head("/")
def _head(): return RedirectResponse(url="/login", status_code=302)

@app.get("/health")
def health(): return {"status":"ok","app":APP_NAME,"data_dir":DATA_DIR}

# ----------------- Auth -----------------
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    ensure_session(request.session)
    if request.session.get("auth"): return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request, "app_name": APP_NAME})

@app.post("/register")
def register_submit(request: Request,
    name: str = Form(...), email: str = Form(...), role: str = Form(""),
    ente: str = Form(""), password: str = Form(...), password2: str = Form(...)):
    ensure_session(request.session)
    if password != password2 or len(password)<6:
        return templates.TemplateResponse("register.html", {"request": request,"app_name":APP_NAME,
            "error":"Password non valida (min 6) o non coincidono."}, status_code=400)
    email = email.strip().lower(); name=name.strip(); role=role.strip(); ente=ente.strip()
    pw = pbkdf2_sha256.hash(password)
    conn=db_connect(); cur=conn.cursor()
    try:
        cur.execute("""INSERT INTO users(name,email,role,ente,password_hash,created_at)
                       VALUES(?,?,?,?,?,?)""", (name,email,role,ente,pw,datetime.utcnow().isoformat()+"Z"))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return templates.TemplateResponse("register.html", {"request": request, "app_name": APP_NAME,
            "error":"Email già registrata."}, status_code=400)
    conn.close()
    request.session["auth"]=True
    request.session["user"]={"name":name,"email":email,"role":role,"ente":ente}
    request.session["profile"]={"first_name":name.split(" ")[0] if name else "",
                                "last_name":" ".join(name.split(" ")[1:]) if len(name.split(" "))>1 else "",
                                "role":role,"ente":ente,"tones":[],"tone_other":"",
                                "channels":["Social"],"add_ai":True,"photo_path":"","header_image_url":""}
    return RedirectResponse(url="/", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    ensure_session(request.session)
    if request.session.get("auth"): return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME})

@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    ensure_session(request.session)
    email=email.strip().lower()
    conn=db_connect(); cur=conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=?", (email,))
    row=cur.fetchone(); conn.close()
    if not row or not pbkdf2_sha256.verify(password, row["password_hash"]):
        return templates.TemplateResponse("login.html", {"request": request,"app_name":APP_NAME,
            "error":"Credenziali non valide."}, status_code=401)
    request.session["auth"]=True
    request.session["user"]={"name":row["name"],"email":row["email"],"role":row["role"],"ente":row["ente"]}
    prof=request.session.get("profile",{})
    prof.setdefault("role", row["role"] or "")
    prof.setdefault("ente", row["ente"] or "")
    prof.setdefault("photo_path", row["photo_path"] or "")
    prof.setdefault("header_image_url", row["header_image_url"] or "")
    prof["add_ai"]=True
    request.session["profile"]=prof
    return RedirectResponse(url="/", status_code=302)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

# ----------------- Onboarding -> poi sparisce -----------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    if request.session.get("onboarding_done"): return RedirectResponse(url="/compose", status_code=302)
    return templates.TemplateResponse("home.html", {"request": request, "app_name": APP_NAME,
                                                    "profile": request.session.get("profile", {})})

@app.post("/save_onboarding")
def save_onboarding(request: Request,
    first_name: str = Form(""), last_name: str = Form(""),
    role: str = Form(""), ente: str = Form(""),
    tones: List[str] = Form([]), channels: List[str] = Form(["Social"]), tone_other: str = Form("")):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    p = request.session["profile"]
    p.update({"first_name":first_name.strip(), "last_name":last_name.strip(), "role":role.strip(),
              "ente":ente.strip(), "tones":tones, "channels":channels or ["Social"], "tone_other":tone_other.strip(),
              "add_ai":True})
    request.session["profile"]=p; request.session["onboarding_done"]=True
    return RedirectResponse(url="/compose", status_code=302)

# ----------------- Compose / Generate -----------------
@app.get("/compose", response_class=HTMLResponse)
def compose(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("compose.html", {"request": request, "app_name": APP_NAME,
                                                       "profile": request.session.get("profile", {}),
                                                       "results": None, "file_previews": [], "errors": []})

@app.post("/generate", response_class=HTMLResponse)
async def generate(request: Request,
    text_input: str = Form(""), url_input: str = Form(""), split_x: Optional[str] = Form(None),
    files: List[UploadFile] = File(None)):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    profile = request.session["profile"]; style = request.session.get("style_guide","")

    errors=[]; sources=[]; previews=[]

    # contenuto libero
    if text_input.strip(): sources.append(text_input.strip())
    if url_input.strip():  sources.append(f"Fonte: {url_input.strip()}")

    # file
    if files:
        for f in files:
            raw=b""
            try:
                raw = await read_limited(f, MAX_FILE_MB*1024*1024)
            except ValueError:
                errors.append(f"{f.filename}: supera {MAX_FILE_MB} MB, escluso.")
            except Exception as ex:
                errors.append(f"{f.filename}: errore lettura: {str(ex)[:120]}")
            txt = extract_text_from_upload(f.filename, raw) if raw else ""
            snippet = txt[:500] + ("…" if len(txt)>500 else "")
            if not txt and raw: snippet = "(Anteprima non disponibile: formato non supportato o PDF/DOCX protetto)"
            if not raw: snippet = "(File non elaborato)"
            previews.append({"name": f.filename, "snippet": snippet})
            if txt: sources.append(txt)   # <--- NIENTE prefisso con nome file

    ctx = "\n\n".join(sources).strip()
    if not ctx:
        ctx = "Nessun contenuto fornito."

    # AI SEMPRE ON
    out_text = ai_press_release(ctx, profile, style)

    # canali
    channels = profile.get("channels", ["Social"])
    results: Dict[str, Any] = {}
    if split_x == "on" and "Social" in channels:
        # thread 280
        def split_280(t:str, limit:int=280):
            words=t.strip().split()
            if not words: return []
            res=[]; cur=""
            for w in words:
                cand=(cur+" "+w).strip() if cur else w
                if len(cand)<=limit: cur=cand
                else:
                    if cur: res.append(cur)
                    while len(w)>limit: res.append(w[:limit]); w=w[limit:]
                    cur=w
            if cur: res.append(cur)
            if len(res)>1:
                total=len(res)
                res=[f"{i}/{total} "+c for i,c in enumerate(res,1)]
            return res
        results["Social"]=[unicode_bold(s) for s in split_280(out_text)]
        for ch in channels:
            if ch!="Social": results[ch]=format_for_channel(out_text, ch)
    else:
        for ch in channels: results[ch]=format_for_channel(out_text, ch)

    request.session["last_results"]=results
    save_draft({"ts": datetime.utcnow().isoformat()+"Z", "profile": profile,
                "input":{"text":text_input,"url":url_input}, "results": results})

    return templates.TemplateResponse("compose.html", {"request": request, "app_name": APP_NAME,
                                                       "profile": profile, "results": results,
                                                       "file_previews": previews, "errors": errors})

# ----------------- Export -----------------
@app.get("/export")
def export_result(request: Request, channel: str = Query(...), fmt: str = Query(...)):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    results=request.session.get("last_results",{})
    if channel not in results: return PlainTextResponse("Nessun risultato per questo canale.", status_code=400)
    val=results[channel]; safe=channel.lower()

    if isinstance(val,list):
        joined="\n\n".join(val)
        if fmt=="txt":
            p=os.path.join(DATA_DIR,f"voxup_{safe}.txt"); open(p,"w",encoding="utf-8").write(joined)
            return FileResponse(p, media_type="text/plain", filename=f"{APP_NAME}_{safe}.txt")
        if fmt=="html":
            p=os.path.join(DATA_DIR,f"voxup_{safe}.html"); open(p,"w",encoding="utf-8").write(f"<!doctype html><meta charset='utf-8'><body>{'<br>'.join(val)}</body>")
            return FileResponse(p, media_type="text/html", filename=f"{APP_NAME}_{safe}.html")
        return PlainTextResponse("Formato non supportato.", status_code=400)

    content=str(val)
    if fmt=="txt":
        plain=re.sub(r"<[^>]+>","",content)
        p=os.path.join(DATA_DIR,f"voxup_{safe}.txt"); open(p,"w",encoding="utf-8").write(plain)
        return FileResponse(p, media_type="text/plain", filename=f"{APP_NAME}_{safe}.txt")
    if fmt=="html":
        html=content if channel!="Social" else f"<pre>{content}</pre>"
        p=os.path.join(DATA_DIR,f"voxup_{safe}.html"); open(p,"w",encoding="utf-8").write(f"<!doctype html><meta charset='utf-8'><body>{html}</body>")
        return FileResponse(p, media_type="text/html", filename=f"{APP_NAME}_{safe}.html")
    if fmt=="docx" and channel=="Stampa":
        doc=Document(); plain=re.sub(r"<[^>]+>","",content)
        for line in plain.split("\n"): doc.add_paragraph(line)
        p=os.path.join(DATA_DIR,f"voxup_{safe}.docx"); doc.save(p)
        return FileResponse(p, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            filename=f"{APP_NAME}_{safe}.docx")
    return PlainTextResponse("Formato non supportato.", status_code=400)

# ----------------- Style -----------------
@app.get("/style", response_class=HTMLResponse)
def style_page(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("style.html", {"request": request, "app_name": APP_NAME,
                                                     "style_guide": request.session.get("style_guide","")})

@app.post("/style")
def style_save(request: Request, style_guide: str = Form("")):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    request.session["style_guide"]=style_guide.strip()
    return RedirectResponse(url="/style", status_code=302)

# ----------------- Profilo (foto + ente + header auto) -----------------
@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("profile.html", {"request": request, "app_name": APP_NAME,
                                                       "profile": request.session.get("profile", {})})

@app.post("/profile")
async def profile_save(request: Request,
    first_name: str = Form(""), last_name: str = Form(""), role: str = Form(""), ente: str = Form(""),
    tones: List[str] = Form([]), tone_other: str = Form(""), channels: List[str] = Form(["Social"]),
    photo: UploadFile = File(None), try_header: Optional[str] = Form(None)):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    p = request.session["profile"]
    p.update({"first_name":first_name.strip(), "last_name":last_name.strip(),
              "role":role.strip(), "ente":ente.strip(),
              "tones":tones, "tone_other":tone_other.strip(),
              "channels":channels or ["Social"], "add_ai":True})

    # foto profilo (facoltativa)
    if photo and photo.filename:
        raw = await read_limited(photo, 4*1024*1024)
        ext = os.path.splitext(photo.filename)[1].lower() or ".png"
        path = os.path.join(UPLOADS_DIR, f"photo_{int(datetime.utcnow().timestamp())}{ext}")
        with open(path,"wb") as f: f.write(raw)
        p["photo_path"] = path

    # tenta header da Wikimedia se richiesto
    if try_header == "on" and p.get("ente"):
        url = try_fetch_wikimedia_header(p["ente"])
        if url: p["header_image_url"] = url

    request.session["profile"] = p
    return RedirectResponse(url="/profile", status_code=302)

# ----------------- Bozze / Note / News -----------------
@app.get("/drafts", response_class=HTMLResponse)
def drafts_page(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    try:
        items=json.load(open(DRAFTS_PATH,"r",encoding="utf-8"))
    except Exception:
        items=[]
    return templates.TemplateResponse("drafts.html", {"request": request, "app_name": APP_NAME, "drafts": items[:20]})

@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    try:
        notes=json.load(open(NOTES_PATH,"r",encoding="utf-8"))
    except Exception:
        notes=[]
    return templates.TemplateResponse("notes.html", {"request": request, "app_name": APP_NAME, "notes": notes})

@app.post("/notes/add")
def notes_add(request: Request, title: str = Form(""), body: str = Form("")):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    note={"id": int(datetime.utcnow().timestamp()*1000), "title": title.strip() or "Senza titolo",
          "body": body.strip(), "ts": datetime.utcnow().isoformat()+"Z"}
    try:
        notes=json.load(open(NOTES_PATH,"r",encoding="utf-8"))
    except Exception:
        notes=[]
    notes.insert(0, note)
    json.dump(notes, open(NOTES_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    return RedirectResponse(url="/notes", status_code=302)

@app.post("/notes/delete")
def notes_delete(request: Request, note_id: str = Form(...)):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    try:
        notes=json.load(open(NOTES_PATH,"r",encoding="utf-8"))
    except Exception:
        notes=[]
    notes=[n for n in notes if str(n.get("id"))!=str(note_id)]
    json.dump(notes, open(NOTES_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    return RedirectResponse(url="/notes", status_code=302)

@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request):
    ensure_session(request.session)
    if not request.session.get("auth"): return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("news.html", {"request": request, "app_name": APP_NAME, "items": get_news()})

@app.get("/news.json")
def news_json(): return JSONResponse(get_news())

