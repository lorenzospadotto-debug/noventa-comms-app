
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from content_gen import extract_texts_from_files, generate_outputs
from social_adapters import (
    post_facebook, post_instagram, post_linkedin, post_x
)

load_dotenv()

APP_DIR = Path(__file__).parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="NoventaCommsApp")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

CITY_NAME = os.getenv("CITY_NAME", "Noventa di Piave")
MAYOR_NAME = os.getenv("MAYOR_NAME", "Claudio Marian")
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL", "http://localhost:8000")

# Social env
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
IG_USER_ID = os.getenv("IG_USER_ID")
LINKEDIN_ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_ORG_ID = os.getenv("LINKEDIN_ORG_ID")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
X_USER_ID = os.getenv("X_USER_ID")


@app.get("/")
async def home(request: Request, msg: Optional[str] = None):
    return templates.TemplateResponse("index.html", {"request": request, "msg": msg, "city": CITY_NAME, "mayor": MAYOR_NAME})


@app.post("/generate")
async def generate(
    request: Request,
    source_files: List[UploadFile] = File(default=[]),
    photo: Optional[UploadFile] = File(default=None),
    audience: str = Form(default="cittadini"),
    topics: str = Form(default=""),
    add_hashtags: bool = Form(default=True),
    add_call_to_action: bool = Form(default=True),
):
    # Save source files
    saved_paths = []
    for f in source_files:
        if not f.filename:
            continue
        safe_name = f"{uuid.uuid4().hex}_{Path(f.filename).name}"
        out = UPLOAD_DIR / safe_name
        with out.open("wb") as w:
            w.write(await f.read())
        saved_paths.append(str(out))

    # Save photo (if present)
    photo_url = None
    photo_filename = None
    if photo and photo.filename:
        safe_name = f"{uuid.uuid4().hex}_{Path(photo.filename).name}"
        out = UPLOAD_DIR / safe_name
        with out.open("wb") as w:
            w.write(await photo.read())
        photo_filename = safe_name
        photo_url = f"{BASE_PUBLIC_URL}/uploads/{safe_name}"

    # Extract text
    source_text = extract_texts_from_files(saved_paths)

    # Generate outputs
    outputs = generate_outputs(
        source_text=source_text,
        city=CITY_NAME,
        mayor=MAYOR_NAME,
        audience=audience,
        topics=topics,
        photo_url=photo_url,
        add_hashtags=add_hashtags,
        add_call_to_action=add_call_to_action,
    )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "generated": outputs,
            "photo_url": photo_url,
            "photo_filename": photo_filename,
            "city": CITY_NAME,
            "mayor": MAYOR_NAME,
            "msg": "Contenuti generati. Puoi modificarli e (opzionalmente) pubblicare.",
        },
    )


@app.post("/post")
async def post(
    request: Request,
    target_fb: Optional[str] = Form(default=None),
    target_ig: Optional[str] = Form(default=None),
    target_li: Optional[str] = Form(default=None),
    target_x: Optional[str] = Form(default=None),
    press_release: str = Form(default=""),
    website_article: str = Form(default=""),
    social_fb_ig: str = Form(default=""),
    social_li: str = Form(default=""),
    social_x: str = Form(default=""),
    photo_filename: Optional[str] = Form(default=None),
):
    results = []
    photo_url = f"{BASE_PUBLIC_URL}/uploads/{photo_filename}" if photo_filename else None

    if target_fb == "on" and FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN:
        ok, resp = post_facebook(
            page_id=FB_PAGE_ID,
            access_token=FB_PAGE_ACCESS_TOKEN,
            message=social_fb_ig or website_article or press_release,
            image_url=photo_url,
        )
        results.append(("Facebook", ok, resp))

    if target_ig == "on" and IG_USER_ID and FB_PAGE_ACCESS_TOKEN and photo_url:
        ok, resp = post_instagram(
            ig_user_id=IG_USER_ID,
            access_token=FB_PAGE_ACCESS_TOKEN,
            caption=social_fb_ig,
            image_url=photo_url,
        )
        results.append(("Instagram", ok, resp))

    if target_li == "on" and LINKEDIN_ACCESS_TOKEN:
        ok, resp = post_linkedin(
            access_token=LINKEDIN_ACCESS_TOKEN,
            text=social_li or website_article,
            image_url=photo_url,
            org_id=LINKEDIN_ORG_ID,
        )
        results.append(("LinkedIn", ok, resp))

    if target_x == "on" and X_BEARER_TOKEN and X_USER_ID:
        ok, resp = post_x(
            bearer_token=X_BEARER_TOKEN,
            user_id=X_USER_ID,
            text=social_x,
        )
        results.append(("X", ok, resp))

    msg = "; ".join([f"{name}: {'OK' if ok else 'ERRORE'}" for name, ok, _ in results]) or "Nessun canale selezionato o token mancanti."
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)
