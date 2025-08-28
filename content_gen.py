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


def _tone_instruction(tone: str, use_emojis: bool) -> str:
    mapping = {
        "istituzionale": "Tono istituzionale, formale, sobrio.",
        "istituzionale_vicino": "Tono istituzionale ma vicino alle persone, chiaro e concreto.",
        "colloquiale": "Tono colloquiale, amichevole ma rispettoso del ruolo.",
        "tecnico": "Tono tecnico e preciso, ma comprensibile ai non addetti.",
    }
    base = mapping.get(tone, mapping["istituzionale_vicino"])
    emoji_note = "Non usare emoticon o emoji." if not use_emojis else "Puoi usare 1-2 emoji pertinenti nei post social (mai nel comunicato)."
    return f"{base} {emoji_note}"


def _prompt(city: str, mayor: str, audience: str, topics: str, source_text: str, photo_url: Optional[str], add_hashtags: bool, add_call_to_action: bool, tone: str, use_emojis: bool) -> str:
    return f"""
Sei l'ufficio comunicazione del Comune di {city}. Scrivi in italiano. Il portavoce è {mayor}.

**Istruzioni di stile**: {_tone_instruction(tone, use_emojis)}

Contesto da documenti/articoli (usa solo se rilevante, evita ripetizioni):

{source_text}

Temi aggiuntivi richiesti: {topics or '—'}
Pubblico principale: {audience}
Foto allegata: {photo_url or 'no'}

Produci:
1) COMUNICATO_STAMPA (600-900 parole), con titolo, occhiello, corpo, citazioni del portavoce.
2) SITO_ISTITUZIONALE (400-700 parole), con H2/H3, punti elenco operativi ove utile.
3) SOCIAL_FB_IG (max 900 caratteri), tono coerente allo stile{ ' + 2-4 hashtag pertinenti' if add_hashtags else ''}{ ' + chiusa con invito/CTA (date, link, partecipazione)' if add_call_to_action else ''}.
4) SOCIAL_LI (max 700 caratteri), più istituzionale, adatto a una pagina comunale.
5) SOCIAL_X (max 280 caratteri), incisivo e chiaro.

Regole:
- Evita tecnicismi non necessari; spiega con semplicità.
- Nessuna promessa non verificata.
- Mantieni coerenza con il ruolo istituzionale.
- Inserisci riferimenti temporali/concreti se presenti nel contesto.
- Formatta ciascuna sezione iniziando con >>>NOME_SEZIONE<<< su una riga.
"""


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
    use_emojis: bool,
):
    prompt = _prompt(city, mayor, audience, topics, source_text, photo_url, add_hashtags, add_call_to_action, tone, use_emojis)

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
        "social_li": _extract("SOCIAL_LI"),
        "social_x": _extract("SOCIAL_X"),
    }
