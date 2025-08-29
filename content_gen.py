import os
from typing import List, Optional

from PyPDF2 import PdfReader
from docx import Document as DocxDocument

from openai import OpenAI

MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


def _read_pdf(path: str) -> str:
    try:
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _read_docx(path: str) -> str:
    try:
        doc = DocxDocument(path)
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        return ""


def _read_txt(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def extract_texts_from_files(paths: List[str]) -> str:
    chunks = []
    for p in paths:
        p_lower = p.lower()
        if p_lower.endswith(".pdf"):
            chunks.append(_read_pdf(p))
        elif p_lower.endswith(".docx"):
            chunks.append(_read_docx(p))
        else:
            chunks.append(_read_txt(p))
    merged = "\n\n".join([c for c in chunks if c])
    return merged[:60000]  # prudential limit


def _bolding_rule() -> str:
    # Linee guida per il grassetto nei social
    return (
        "Nei testi per i social usa il **grassetto** per 4-8 parole/locuzioni davvero chiave "
        "(es. titolo iniziativa, luogo, data/orario, benefici concreti). Evita di abusarne."
    )


def _prompt(city: str, mayor: str, audience: str, topics: str, source_text: str,
           photo_url: Optional[str], add_hashtags: bool, add_call_to_action: bool,
           tone: str, use_emojis_social: bool, targets: List[str]) -> str:

    # Normalizza i target
    tset = {t.lower() for t in targets}
    want_fb = "facebook" in tset
    want_ig = "instagram" in tset
    want_x  = "x" in tset
    want_web = "sito" in tset or "web" in tset
    want_press = "stampa" in tset or "giornali" in tset or "comunicato" in tset

    # Regole emoji: social sì/no (site/press sempre NO)
    emoji_social = "Sì, usa 1-2 emoji pertinenti" if use_emojis_social else "No, non usare emoji"
    emoji_non_social = "Non usare emoji nei testi per sito e stampa."

    sections = []
    if want_press:
        sections.append("COMUNICATO_STAMPA")
    if want_web:
        sections.append("SITO_ISTITUZIONALE")
    if want_fb or want_ig:
        sections.append("SOCIAL_FB_IG")
    if want_x:
        sections.append("SOCIAL_X")

    # Se l’utente non seleziona nulla, generiamo tutto (fallback)
    if not sections:
        sections = ["COMUNICATO_STAMPA", "SITO_ISTITUZIONALE", "SOCIAL_FB_IG", "SOCIAL_X"]

    instructions = f"""
Sei l'ufficio comunicazione del Comune di {city}. Scrivi in italiano. Il portavoce è {mayor}.
Tono richiesto: {tone}.
Regole emoji: Social → {emoji_social}. {emoji_non_social}
Bolding: {_bolding_rule()}

Contesto da documenti/articoli (usa solo se rilevante, evita ripetizioni):

{source_text}

Temi aggiuntivi richiesti: {topics or '—'}
Pubblico principale: {audience}
Foto allegata: {photo_url or 'no'}
"""

    outputs_spec = []
    if "COMUNICATO_STAMPA" in sections:
        outputs_spec.append("1) >>>COMUNICATO_STAMPA<<< (600-900 parole), titolo, occhiello, corpo, citazione del portavoce. Vietate emoji; niente grassetto decorativo.")
    if "SITO_ISTITUZIONALE" in sections:
        outputs_spec.append("2) >>>SITO_ISTITUZIONALE<<< (400-700 parole), H2/H3, punti elenco operativi se utile. Linguaggio pratico. Vietate emoji; grassetto solo se strettamente necessario su termini tecnici.")
    if "SOCIAL_FB_IG" in sections:
        outputs_spec.append(
            f"3) >>>SOCIAL_FB_IG<<< (max 900 caratteri), tono empatico ma sobrio, usa il grassetto per parole chiave. "
            f"{'Consenti' if use_emojis_social else 'Non usare'} emoji. "
            f"{'Aggiungi 2-4 hashtag pertinenti.' if add_hashtags else ''} "
            f"{'Chiudi con invito/CTA (date, link, partecipazione).' if add_call_to_action else ''}"
        )
    if "SOCIAL_X" in sections:
        outputs_spec.append(
            f"4) >>>SOCIAL_X<<< (max 280 caratteri), incisivo e chiaro, possibile 1 grassetto mirato. "
            if "SOCIAL_X" in sections:
    outputs_spec.append(
        f"4) >>>SOCIAL_X<<< (max 280 caratteri), incisivo e chiaro, possibile 1 grassetto mirato. "
        f"{'Consenti' if use_emojis_social else 'Non usare'} emoji. "
        f"{'Aggiungi 1-2 hashtag se pertinenti.' if add_hashtags else ''}"
    )

    rules = """
Regole generali:
- Evita tecnicismi non necessari; spiega con semplicità.
- Nessuna promessa non verificata.
- Inserisci riferimenti temporali/concreti se presenti nel contesto.
- Ogni sezione DEVE iniziare esattamente con >>>NOME_SEZIONE<<< su una riga, poi il contenuto.
"""

    return instructions + "\nProduci SOLO le sezioni richieste:\n" + "\n".join(outputs_spec) + "\n" + rules


def generate_outputs(
    source_text: str,
    city: str,
    mayor: str,
    audience: str,
    topics: str,
    photo_url: Optional[str],
    add_hashtags: bool,
    add_call_to_action: bool,
    tone: str,
    use_emojis_social: bool,
    targets: List[str],
):
    prompt = _prompt(city, mayor, audience, topics, source_text, photo_url,
                     add_hashtags, add_call_to_action, tone, use_emojis_social, targets)

    chat = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Sei un assistente di comunicazione pubblico italiano."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )

    text = chat.choices[0].message.content

    def _extract(tag: str) -> str:
        marker = f">>>{tag}<<<"
        if marker in text:
            after = text.split(marker, 1)[1]
            for t in ["COMUNICATO_STAMPA", "SITO_ISTITUZIONALE", "SOCIAL_FB_IG", "SOCIAL_LI", "SOCIAL_X"]:
                m = f">>>{t}<<<"
                if m in after and t != tag:
                    after = after.split(m, 1)[0]
            return after.strip()
        return ""

    return {
        "press_release": _extract("COMUNICATO_STAMPA"),
        "website_article": _extract("SITO_ISTITUZIONALE"),
        "social_fb_ig": _extract("SOCIAL_FB_IG"),
        "social_li": _extract("SOCIAL_LI"),  # non prodotto ora, ma manteniamo la chiave
        "social_x": _extract("SOCIAL_X"),
    }
