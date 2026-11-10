"""PlagiaScan backend.

Compares uploaded text with:
1) the user's local reference library (SQLite),
2) publicly indexed web pages (Brave Search or Google Custom Search),
3) discoverable open-access academic works (OpenAlex; Crossref metadata fallback).

Similarity is an indicator for review, not a legal or academic finding of plagiarism.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from email.message import EmailMessage
from functools import wraps
import io
import ipaddress
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import threading
import time
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, session
try:
    from flask_cors import CORS
except ImportError:  # CORS is only needed when the frontend is hosted elsewhere
    CORS = None
from sklearn.feature_extraction.text import TfidfVectorizer
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default

def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default

SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "brave").strip().lower()
MATCH_THRESHOLD = env_float("MATCH_THRESHOLD", 0.50, 0.1, 0.99)
MAX_QUERIES = env_int("MAX_QUERIES", 6, 1, 12)
RESULTS_PER_QUERY = env_int("RESULTS_PER_QUERY", 4, 1, 10)
MAX_SOURCES = env_int("MAX_SOURCES", 30, 1, 60)
MAX_TEXT_CHARS = env_int("MAX_TEXT_CHARS", 60000, 1000, 200000)
MAX_UPLOAD_BYTES = env_int("MAX_UPLOAD_MB", 10, 1, 50) * 1024 * 1024
MAX_PAGE_BYTES = 1_500_000
USE_EMBEDDINGS = os.getenv("USE_EMBEDDINGS", "0").strip() == "1"
API_KEY = os.getenv("API_KEY", "").strip()
ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "")
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "")
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "plagiascan.db")))
if not DB_PATH.is_absolute():
    DB_PATH = (BASE_DIR / DB_PATH).resolve()
FRONTEND_DIR = BASE_DIR.parent / "frontend"
USER_AGENT = {"User-Agent": "PlagiaScan/1.0 (academic text similarity checker)"}

app = Flask(__name__)
app.config["SECRET_KEY"] = SESSION_SECRET_KEY or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(hours=8)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0").strip() == "1"
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
if CORS is not None:
    CORS(app, origins=os.getenv("ALLOWED_ORIGIN", "*").split(","))

# ------------------------------ database
def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created TEXT NOT NULL,
            words INTEGER NOT NULL,
            similarity INTEGER NOT NULL,
            result TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','admin')),
            created_at TEXT NOT NULL,
            auth_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reference_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            text TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            words INTEGER NOT NULL,
            created TEXT NOT NULL
        );
    """)
    scan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "user_id" not in scan_columns:
        conn.execute("ALTER TABLE scans ADD COLUMN user_id INTEGER REFERENCES users(id)")
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "auth_version" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0")
    if ADMIN_INITIAL_PASSWORD:
        if len(ADMIN_INITIAL_PASSWORD) < 12 or len(ADMIN_INITIAL_PASSWORD) > 128:
            raise RuntimeError("ADMIN_INITIAL_PASSWORD must be between 12 and 128 characters.")
        admin = conn.execute(
            "SELECT id,role FROM users WHERE username=? OR email=?",
            ("superadmin", "admin@yourdomain.com"),
        ).fetchone()
        if admin is None:
            conn.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                ("superadmin", "admin@yourdomain.com",
                 generate_password_hash(ADMIN_INITIAL_PASSWORD), "admin",
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
            )
        elif admin["role"] != "admin":
            raise RuntimeError(
                "The configured initial admin username or email is already owned by a non-admin account."
            )
    return conn

@contextmanager
def db_session():
    conn = connect_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ------------------------------ text extraction / preparation
def extract_document(filename: str, raw: bytes) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".txt":
        return raw.decode("utf-8-sig", errors="replace")
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(raw))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        # Include text in tables, which is commonly used in reports.
        for table in doc.tables:
            for row in table.rows:
                paragraphs.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(paragraphs)
    raise ValueError("Unsupported file type. Upload PDF, DOCX or TXT.")

def split_sentences(text: str, min_words: int = 5) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", text or "")
    chunks = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    return [part.strip() for part in chunks if len(part.split()) >= min_words]

STOPWORDS = set("""a about above after again all also am an and any are as at be because been before being below between both but by can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the their theirs them then there these they this those through to too under until up very was we were what when where which while who whom why will with would you your""".split())

def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower())

def pick_sentences(sents: list[str], count: int) -> list[str]:
    if not sents:
        return []
    count = min(count, len(sents))
    idx = sorted({round(i * (len(sents) - 1) / max(1, count - 1)) for i in range(count)})
    return [sents[i] for i in idx]

def make_queries(sents: list[str]) -> list[dict]:
    """Build both an exact-phrase query and a keyword query per sampled sentence."""
    queries = []
    for sentence in pick_sentences(sents, MAX_QUERIES):
        words = re.findall(r"[\w'-]+", sentence)
        if len(words) < 5:
            continue
        keywords = [w for w in words if w.lower() not in STOPWORDS and len(w) > 2]
        keywords = sorted(set(keywords), key=lambda w: -len(w))[:8]
        queries.append({
            "phrase": " ".join(words[:12]),
            "keywords": " ".join(keywords) or " ".join(words[:8]),
        })
    return queries

# ------------------------------ optional semantic model
_embedding_model = None
_embedding_attempted = False

def get_embedding_model():
    global _embedding_model, _embedding_attempted
    if not USE_EMBEDDINGS or _embedding_attempted:
        return _embedding_model
    _embedding_attempted = True
    try:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"))
    except Exception as exc:
        app.logger.warning("Semantic model unavailable; TF-IDF only: %s", exc)
        _embedding_model = None
    return _embedding_model

def embed(texts: list[str]):
    model = get_embedding_model()
    if model is None or not texts:
        return None
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

# ------------------------------ online source discovery
# Every provider returns a list of dicts: url, title, type, optional text/abstract.
# Providers never raise; failures are recorded in PROVIDER_ERRORS for diagnostics.
PROVIDER_ERRORS: dict[str, str] = {}

def http_get(url: str, provider: str, **kwargs):
    kwargs.setdefault("timeout", 15)
    headers = {**USER_AGENT, **kwargs.pop("headers", {})}
    try:
        response = requests.get(url, headers=headers, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        PROVIDER_ERRORS[provider] = str(exc)[:200]
        app.logger.warning("%s request failed: %s", provider, exc)
        return None

def search_keyed_web(query: str) -> list[dict]:
    """Brave or Google Custom Search (only when an API key is configured)."""
    if SEARCH_PROVIDER == "google":
        key, cx = os.getenv("GOOGLE_API_KEY", ""), os.getenv("GOOGLE_CSE_ID", "")
        if not key or not cx:
            return []
        response = http_get("https://www.googleapis.com/customsearch/v1", "google",
                            params={"key": key, "cx": cx, "q": query, "num": min(RESULTS_PER_QUERY, 10)})
        items = response.json().get("items", []) if response else []
        return [{"url": i.get("link", ""), "title": i.get("title", ""), "type": "web"} for i in items if i.get("link")]
    key = os.getenv("BRAVE_API_KEY", "")
    if not key:
        return []
    response = http_get("https://api.search.brave.com/res/v1/web/search", "brave",
                        headers={"X-Subscription-Token": key, "Accept": "application/json"},
                        params={"q": query, "count": RESULTS_PER_QUERY})
    items = response.json().get("web", {}).get("results", []) if response else []
    return [{"url": i.get("url", ""), "title": i.get("title", ""), "type": "web"} for i in items if i.get("url")]

def search_duckduckgo(query: str) -> list[dict]:
    """Keyless public-web discovery through DuckDuckGo's HTML endpoint."""
    try:
        response = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                                 headers={**USER_AGENT, "User-Agent": "Mozilla/5.0 (PlagiaScan)"}, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        PROVIDER_ERRORS["duckduckgo"] = str(exc)[:200]
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for link in soup.select("a.result__a")[:RESULTS_PER_QUERY]:
        href = link.get("href", "")
        if "uddg=" in href:
            from urllib.parse import parse_qs, unquote
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        elif href.startswith("//"):
            href = "https:" + href
        if href.startswith("http"):
            results.append({"url": href, "title": link.get_text(" ", strip=True), "type": "web"})
    return results

def search_wikipedia(query: str) -> list[dict]:
    """Keyless: returns full plain-text article extracts directly from the API."""
    response = http_get("https://en.wikipedia.org/w/api.php", "wikipedia", params={
        "action": "query", "list": "search", "srsearch": query, "srlimit": 3, "format": "json"})
    if not response:
        return []
    results = []
    for hit in response.json().get("query", {}).get("search", []):
        title = hit.get("title", "")
        page = http_get("https://en.wikipedia.org/w/api.php", "wikipedia", params={
            "action": "query", "prop": "extracts", "explaintext": 1, "titles": title,
            "format": "json", "redirects": 1})
        if not page:
            continue
        pages = page.json().get("query", {}).get("pages", {})
        extract = next(iter(pages.values()), {}).get("extract", "") if pages else ""
        if extract:
            results.append({"url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                            "title": f"Wikipedia: {title}", "type": "web",
                            "text": extract[:MAX_TEXT_CHARS], "content_kind": "full_text"})
    return results

def search_arxiv(query: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    response = http_get("https://export.arxiv.org/api/query", "arxiv", params={
        "search_query": "all:" + " AND all:".join(query.split()[:6]), "max_results": 3})
    if not response:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return []
    results = []
    for entry in root.findall("a:entry", ns):
        summary = (entry.findtext("a:summary", "", ns) or "").strip()
        link = entry.findtext("a:id", "", ns)
        if summary and link:
            results.append({"url": link, "title": " ".join((entry.findtext("a:title", "", ns) or "arXiv paper").split()),
                            "type": "academic", "text": summary, "content_kind": "abstract"})
    return results

def search_openalex(query: str) -> list[dict]:
    params = {"search": query, "per-page": 5,
              "select": "id,display_name,doi,open_access,primary_location,best_oa_location,abstract_inverted_index"}
    if os.getenv("OPENALEX_EMAIL"):
        params["mailto"] = os.getenv("OPENALEX_EMAIL")
    response = http_get("https://api.openalex.org/works", "openalex", params=params)
    if not response:
        return []
    results = []
    for work in response.json().get("results", []):
        oa = work.get("best_oa_location") or work.get("primary_location") or {}
        url = oa.get("pdf_url") or oa.get("landing_page_url") or work.get("doi") or work.get("id") or ""
        abstract = ""
        inverted = work.get("abstract_inverted_index") or {}
        if inverted:
            positioned = [(pos, word) for word, positions in inverted.items() for pos in positions]
            abstract = " ".join(word for _, word in sorted(positioned))
        if url or abstract:
            results.append({"url": url, "title": work.get("display_name") or "Academic work",
                            "type": "academic", "abstract": abstract})
    return results

def search_crossref(query: str) -> list[dict]:
    response = http_get("https://api.crossref.org/works", "crossref", params={"query": query, "rows": 4})
    if not response:
        return []
    results = []
    for item in response.json().get("message", {}).get("items", []):
        title = (item.get("title") or ["Academic publication"])[0]
        link = next((x.get("URL") for x in item.get("link") or [] if x.get("URL")), "") or item.get("URL", "")
        abstract = BeautifulSoup(item.get("abstract") or "", "html.parser").get_text(" ", strip=True)
        if link or len(abstract.split()) >= 8:
            results.append({"url": link, "title": title, "type": "academic", "abstract": abstract})
    return results

# ------------------------------ safe webpage retrieval
def is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (OSError, ValueError):
        return False

def fetch_page_text(url: str) -> str:
    """Fetch public HTML/PDF pages with bounded size and redirect validation."""
    current = url
    browser_headers = {**USER_AGENT, "User-Agent": "Mozilla/5.0 (compatible; PlagiaScan/1.0)",
                       "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"}
    try:
        for _ in range(4):
            if not is_public_http_url(current):
                return ""
            response = requests.get(current, headers=browser_headers, timeout=12,
                                    stream=True, allow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    return ""
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                response.close()
                return ""
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.raw.read(MAX_PAGE_BYTES + 1, decode_content=True)
            response.close()
            raw = raw[:MAX_PAGE_BYTES]
            if "pdf" in content_type or raw[:4] == b"%PDF":
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(raw))
                    return "\n".join(page.extract_text() or "" for page in reader.pages)[:MAX_TEXT_CHARS]
                except Exception:
                    return ""
            if "html" not in content_type and b"<html" not in raw[:1000].lower():
                return ""
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
                tag.decompose()
            return soup.get_text("\n", strip=True)[:MAX_TEXT_CHARS]
    except requests.RequestException as exc:
        app.logger.debug("Page fetch failed for %s: %s", current, exc)
    return ""

# ------------------------------ similarity engine
def shingles(words: list[str], n: int = 3) -> set:
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}

def analyze_text(text: str, sources: list[dict]) -> dict:
    """Score each document sentence against every source.

    score = max(TF-IDF cosine against the closest source sentence,
                word-3-gram containment against the whole source text).
    Containment catches copied text even when the source splits it across lines;
    cosine catches reworded text. The TF-IDF model is fitted once on everything.
    """
    doc_sentences = split_sentences(text)
    usable = [s for s in sources if s.get("sentences")]
    if not doc_sentences:
        return {"similarity": 0, "matches": [], "sentences": 0, "engine": "tfidf+shingles"}
    best = [(0.0, "", None) for _ in doc_sentences]
    if usable:
        # --- cosine over all source sentences at once
        flat, owner = [], []
        for si, source in enumerate(usable):
            for sentence in source["sentences"]:
                flat.append(sentence)
                owner.append(si)
        cosine = None
        try:
            vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
            matrix = vec.fit_transform(doc_sentences + flat)
            cosine = (matrix[:len(doc_sentences)] @ matrix[len(doc_sentences):].T).toarray()
        except ValueError:
            cosine = None
        document_embeddings = embed(doc_sentences)
        if cosine is not None and document_embeddings is not None:
            source_embeddings = embed(flat)
            if source_embeddings is not None:
                cosine = cosine.clip(0, 1) * 0.55 + source_embeddings.dot(document_embeddings.T).T.clip(0, 1) * 0.45
        # --- containment against each full source
        source_shingles = [shingles(tokens(" ".join(s["sentences"]))) for s in usable]
        for di, sentence in enumerate(doc_sentences):
            doc_sh = shingles(tokens(sentence))
            for si, source in enumerate(usable):
                contain = len(doc_sh & source_shingles[si]) / len(doc_sh) if doc_sh else 0.0
                cos_best, cos_sentence = 0.0, ""
                if cosine is not None:
                    cols = [k for k, o in enumerate(owner) if o == si]
                    if cols:
                        row = cosine[di, cols]
                        j = int(row.argmax())
                        cos_best, cos_sentence = float(row[j]), flat[cols[j]]
                score = max(contain, cos_best)
                if score > best[di][0]:
                    evidence = cos_sentence
                    if contain >= cos_best or not evidence:
                        # pick the source sentence sharing the most 3-grams
                        evidence = max(source["sentences"], key=lambda s: len(doc_sh & shingles(tokens(s))), default="")
                    best[di] = (score, evidence, source)
    total_words = sum(len(s.split()) for s in doc_sentences) or 1
    matched_words, matches = 0.0, []
    for sentence, (score, evidence, source) in zip(doc_sentences, best):
        if source and score >= MATCH_THRESHOLD:
            matched_words += len(sentence.split()) * min(1.0, score)
            matches.append({
                "text": sentence,
                "source": source.get("title") or source.get("url") or "Unknown source",
                "url": source.get("url", ""),
                "source_type": source.get("type", "web"),
                "score": round(min(1.0, score) * 100),
                "source_text": evidence[:350],
            })
    return {
        "similarity": min(100, round(matched_words / total_words * 100)),
        "matches": matches,
        "sentences": len(doc_sentences),
        "engine": "tfidf+shingles+sentence-transformers" if embedding_model_ready() else "tfidf+shingles",
    }

def embedding_model_ready() -> bool:
    return USE_EMBEDDINGS and _embedding_model is not None

def local_reference_sources() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute("SELECT id,name,text FROM reference_documents ORDER BY id DESC").fetchall()
    return [{
        "url": f"local://reference/{row['id']}",
        "title": row["name"],
        "type": "local",
        "sentences": split_sentences(row["text"], min_words=5),
    } for row in rows]

def discover_candidates(queries: list[dict]) -> tuple[list[dict], dict]:
    """Run all providers in parallel. Returns (candidates, per-provider counts)."""
    jobs = []  # (provider, future)
    with ThreadPoolExecutor(max_workers=10) as executor:
        for q in queries:
            jobs.append(("search_api", executor.submit(search_keyed_web, f'"{q["phrase"]}"')))
            jobs.append(("duckduckgo", executor.submit(search_duckduckgo, f'"{q["phrase"]}"')))
            jobs.append(("wikipedia", executor.submit(search_wikipedia, q["keywords"])))
            jobs.append(("arxiv", executor.submit(search_arxiv, q["keywords"])))
            jobs.append(("openalex", executor.submit(search_openalex, q["keywords"])))
            jobs.append(("crossref", executor.submit(search_crossref, q["keywords"])))
        candidates, counts = [], {}
        for provider, job in jobs:
            try:
                found = job.result()
            except Exception as exc:  # a provider must never break the scan
                PROVIDER_ERRORS[provider] = str(exc)[:200]
                found = []
            counts[provider] = counts.get(provider, 0) + len(found)
            candidates.extend(found)
    return candidates, counts

def scan_document(text: str) -> dict:
    PROVIDER_ERRORS.clear()
    doc_sentences = split_sentences(text)
    queries = make_queries(doc_sentences)
    warnings: list[str] = []
    candidates, provider_counts = discover_candidates(queries)

    unique: dict[str, dict] = {}
    for item in candidates:
        key = (item.get("url") or "").strip() or f"title:{item.get('title')}"
        if key not in unique:
            unique[key] = item
        elif item.get("text") or item.get("abstract"):
            unique[key] = {**unique[key], **{k: v for k, v in item.items() if v}}
    candidates = list(unique.values())[:MAX_SOURCES]

    # Fetch pages only for candidates that carry no text of their own.
    need_fetch = [c for c in candidates if not c.get("text") and c.get("url", "").startswith("http")]
    with ThreadPoolExecutor(max_workers=8) as executor:
        fetched_texts = list(executor.map(lambda c: fetch_page_text(c["url"]), need_fetch))
    page_text = {c["url"]: t for c, t in zip(need_fetch, fetched_texts)}

    fetched = []
    for item in candidates:
        full = item.get("text") or page_text.get(item.get("url", ""), "")
        abstract = item.get("abstract", "")
        content = full if len(full.split()) >= 8 else abstract
        sentences = split_sentences(content, min_words=5)
        if sentences:
            kind = "full_text" if content == full and full else "abstract"
            fetched.append({**item, "content_kind": kind, "sentences": sentences})

    local_sources = local_reference_sources()
    all_sources = local_sources + fetched
    result = analyze_text(text, all_sources)
    counts = {t: sum(s["type"] == t for s in all_sources) for t in ("local", "web", "academic")}

    if not fetched and not local_sources:
        if PROVIDER_ERRORS:
            warnings.append("Could not reach online sources (" + ", ".join(sorted(PROVIDER_ERRORS)) +
                            "). Check your internet/firewall/proxy, then open /diagnose in the browser.")
        else:
            warnings.append("No online source text was retrieved for this document, so 0% only means nothing was found to compare against.")
    elif PROVIDER_ERRORS:
        warnings.append("Some providers were unreachable: " + ", ".join(sorted(PROVIDER_ERRORS)) + ". Coverage is partial.")
    if any(s.get("content_kind") == "abstract" for s in fetched):
        warnings.append("Some academic comparisons use abstracts rather than full papers; coverage is partial.")
    if not local_sources:
        warnings.append("The local reference library is empty. Add reference documents in About → Local reference library for guaranteed comparison.")
    result.update({
        "sources_checked": len(all_sources),
        "source_counts": counts,
        "provider_results": provider_counts,
        "warnings": warnings,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    return result

# ------------------------------ API authentication and routes
_auth_attempts: dict[tuple[str, str], tuple[float, int]] = {}
_auth_attempts_lock = threading.Lock()
_auth_window_seconds = 900
_auth_max_attempts = 10


def current_user():
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT id,username,email,role,created_at,auth_version FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if row is None or session.get("auth_version") != row["auth_version"]:
        return None
    return dict(row)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return jsonify(error="Please log in to continue."), 401
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="Admin login required."), 401
        if user["role"] != "admin":
            return jsonify(error="Admin access required."), 403
        return view(*args, **kwargs)
    return wrapped


def auth_rate_limited(action: str, address: str) -> bool:
    now = time.monotonic()
    key = (action, address)
    with _auth_attempts_lock:
        started, attempts = _auth_attempts.get(key, (now, 0))
        if now - started >= _auth_window_seconds:
            _auth_attempts.pop(key, None)
            return False
        return attempts >= _auth_max_attempts


def record_auth_attempt(action: str, address: str, *, success: bool = False) -> None:
    now = time.monotonic()
    key = (action, address)
    with _auth_attempts_lock:
        if success:
            _auth_attempts.pop(key, None)
            return
        started, attempts = _auth_attempts.get(key, (now, 0))
        if now - started >= _auth_window_seconds:
            started, attempts = now, 0
        _auth_attempts[key] = (started, attempts + 1)


@app.before_request
def require_api_key():
    public_pages = {
        "/", "/favicon.ico", "/login", "/register", "/dashboard", "/admin/login",
        "/admin/dashboard", "/health",
    }
    if (request.method == "OPTIONS" or request.path in public_pages
            or request.path.startswith("/static/")
            or request.path.startswith("/api/auth/")):
        return None
    if not API_KEY:
        return None
    if request.headers.get("Authorization", "") != f"Bearer {API_KEY}":
        return jsonify(error="Unauthorized. Check the API key in About → API settings."), 401
    return None

@app.before_request
def protect_admin_pages():
    if not request.path.startswith("/admin/") or request.path == "/admin/login":
        return None
    user = current_user()
    if user is None:
        if request.path == "/admin/dashboard":
            return redirect("/admin/login")
        return jsonify(error="Admin login required."), 401
    if user["role"] != "admin":
        if request.path == "/admin/dashboard":
            return redirect("/dashboard")
        return jsonify(error="Admin access required."), 403
    return None


@app.get("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")

@app.get("/favicon.ico")
def favicon():
    return send_from_directory(str(Path(app.static_folder)), "favicon.ico")

@app.get("/login")
@app.get("/register")
@app.get("/admin/login")
def authentication_page():
    return send_from_directory(str(FRONTEND_DIR), "index.html")

@app.get("/dashboard")
def user_dashboard_page():
    user = current_user()
    if user is None:
        return redirect("/login")
    if user["role"] != "user":
        return redirect("/admin/dashboard")
    return send_from_directory(str(FRONTEND_DIR), "index.html")

@app.get("/admin/dashboard")
def admin_dashboard_page():
    return send_from_directory(str(FRONTEND_DIR), "index.html")

@app.get("/api/auth/session")
def auth_session_route():
    user = current_user()
    if user is None:
        return jsonify(authenticated=False)
    return jsonify(authenticated=True, user={
        "username": user["username"], "email": user["email"], "role": user["role"]
    })

@app.post("/api/auth/register")
def register_route():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Provide registration details."), 400
    address = request.remote_addr or "unknown"
    if auth_rate_limited("register", address):
        return jsonify(error="Too many registration attempts. Try again later."), 429

    username = str(body.get("username") or "").strip()
    email_address = str(body.get("email") or "").strip().lower()
    password = body.get("password")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        record_auth_attempt("register", address)
        return jsonify(error="Username must be 3–32 characters using letters, numbers, dots, dashes or underscores."), 400
    if len(email_address) > 254 or not re.fullmatch(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email_address):
        record_auth_attempt("register", address)
        return jsonify(error="Enter a valid email address."), 400
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        record_auth_attempt("register", address)
        return jsonify(error="Password must be between 12 and 128 characters."), 400
    if username.casefold() == "superadmin" or email_address == "admin@yourdomain.com":
        record_auth_attempt("register", address)
        return jsonify(error="That username or email is reserved."), 400

    try:
        with db_session() as conn:
            cursor = conn.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (username, email_address, generate_password_hash(password), "user",
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        record_auth_attempt("register", address)
        return jsonify(error="That username or email is already registered."), 409

    record_auth_attempt("register", address, success=True)
    session.clear()
    session["user_id"] = user_id
    session["auth_version"] = 0
    session.permanent = True
    return jsonify(authenticated=True, user={"username": username, "email": email_address, "role": "user"}), 201

def authenticate_route(required_role: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Provide your username/email and password."), 400
    address = request.remote_addr or "unknown"
    action = f"login:{required_role}"
    if auth_rate_limited(action, address):
        return jsonify(error="Too many login attempts. Try again later."), 429

    identifier = str(body.get("identifier") or "").strip()
    password = body.get("password")
    user = None
    if identifier and len(identifier) <= 254:
        with db_session() as conn:
            row = conn.execute(
                "SELECT id,username,email,password_hash,role,auth_version FROM users "
                "WHERE username=? COLLATE NOCASE OR email=? COLLATE NOCASE",
                (identifier, identifier),
            ).fetchone()
        user = dict(row) if row is not None else None

    valid_password = False
    if user is not None and isinstance(password, str) and len(password) <= 1024:
        try:
            valid_password = check_password_hash(user["password_hash"], password)
        except (ValueError, TypeError):
            valid_password = False
    if user is None or not valid_password or user["role"] != required_role:
        record_auth_attempt(action, address)
        return jsonify(error="Invalid credentials for this login."), 401

    record_auth_attempt(action, address, success=True)
    session.clear()
    session["user_id"] = user["id"]
    session["auth_version"] = user["auth_version"]
    session.permanent = True
    return jsonify(authenticated=True, user={
        "username": user["username"], "email": user["email"], "role": user["role"]
    })

@app.post("/api/auth/login")
def user_login_route():
    return authenticate_route("user")

@app.post("/api/auth/admin-login")
def admin_login_route():
    return authenticate_route("admin")

@app.post("/api/auth/logout")
def logout_route():
    session.clear()
    return jsonify(authenticated=False)

@app.post("/api/auth/password")
@login_required
def change_password_route():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Provide your current and new passwords."), 400
    current_password = body.get("current_password")
    new_password = body.get("new_password")
    user = current_user()
    if (not isinstance(current_password, str) or not isinstance(new_password, str)
            or not 12 <= len(new_password) <= 128):
        return jsonify(error="The new password must be between 12 and 128 characters."), 400
    with db_session() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if row is None or not check_password_hash(row["password_hash"], current_password):
            return jsonify(error="Current password is incorrect."), 401
        conn.execute(
            "UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id=?",
            (generate_password_hash(new_password), user["id"]),
        )
    session.clear()
    return jsonify(message="Password changed successfully.")

@app.get("/health")
def health():
    return jsonify(status="ok", search_provider=SEARCH_PROVIDER,
                   embeddings_enabled=USE_EMBEDDINGS, api_key_required=bool(API_KEY))

@app.get("/diagnose")
def diagnose_route():
    """Check which discovery providers are reachable from this machine."""
    PROVIDER_ERRORS.clear()
    probes = {
        "wikipedia": lambda: search_wikipedia("plagiarism detection"),
        "openalex": lambda: search_openalex("plagiarism detection"),
        "crossref": lambda: search_crossref("plagiarism detection"),
        "arxiv": lambda: search_arxiv("plagiarism detection"),
        "duckduckgo": lambda: search_duckduckgo("plagiarism detection"),
        "search_api": lambda: search_keyed_web("plagiarism detection"),
    }
    report = {}
    for name, probe in probes.items():
        try:
            n = len(probe())
        except Exception as exc:
            PROVIDER_ERRORS[name] = str(exc)[:200]
            n = 0
        report[name] = {"ok": n > 0, "results": n, "error": PROVIDER_ERRORS.get(name, "")}
    report["search_api"]["configured"] = bool(os.getenv("BRAVE_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    return jsonify(report)

@app.post("/extract")
def extract_route():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify(error="Choose a file first."), 400
    try:
        raw = file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return jsonify(error=f"File is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB."), 413
        text = extract_document(file.filename, raw)[:MAX_TEXT_CHARS]
        if not text.strip():
            return jsonify(error="No readable text was found. Scanned image-only PDFs need OCR before checking."), 422
        return jsonify(name=Path(file.filename).name, text=text, words=len(text.split()))
    except ValueError as exc:
        return jsonify(error=str(exc)), 415
    except Exception as exc:
        app.logger.exception("Document extraction failed")
        return jsonify(error=f"Could not read the document: {exc}"), 422

@app.post("/scan")
def scan_route():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    if len(text.split()) < 8:
        return jsonify(error="Please provide at least 8 words of readable text."), 400
    result = scan_document(text)
    name = Path(str(body.get("name") or "Pasted_text.txt")).name[:200]
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO scans(name,created,words,similarity,result,user_id) VALUES(?,?,?,?,?,?)",
            (name, dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             len(text.split()), result["similarity"], json.dumps(result),
             session.get("user_id") if isinstance(session.get("user_id"), int) else None),
        )
        scan_id = cursor.lastrowid
    return jsonify(id=scan_id, name=name, **result)

@app.post("/contact")
def contact_route():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Please provide contact details."), 400

    name = str(body.get("name") or "").strip()
    email_address = str(body.get("email") or "").strip()
    document = str(body.get("document") or "").strip()
    message = str(body.get("message") or "").strip()
    if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
        return jsonify(error="Enter a valid name (up to 120 characters)."), 400
    if len(email_address) > 254 or not re.fullmatch(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email_address):
        return jsonify(error="Enter a valid email address."), 400
    if len(document) > 200 or any(ord(char) < 32 for char in document):
        return jsonify(error="Document name must be 200 characters or fewer."), 400
    if len(message) < 10 or len(message) > 5000:
        return jsonify(error="Describe your request in 10 to 5000 characters."), 400

    admin_email = os.getenv("CONTACT_ADMIN_EMAIL", "").strip()
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not admin_email or not smtp_host:
        return jsonify(error="Contact email is not configured. Please try again later."), 503

    email_message = EmailMessage()
    email_message["Subject"] = "PlagiaScan content support request"
    email_message["From"] = os.getenv("SMTP_FROM", "").strip() or os.getenv("SMTP_USERNAME", "").strip() or admin_email
    email_message["To"] = admin_email
    email_message["Reply-To"] = email_address
    email_message.set_content(
        f"Content support request from {name}\n"
        f"Email: {email_address}\n"
        f"Document: {document or 'Not provided'}\n\n"
        f"Request:\n{message}\n"
    )

    try:
        smtp_port = env_int("SMTP_PORT", 587, 1, 65535)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            if os.getenv("SMTP_STARTTLS", "1").strip() == "1":
                server.starttls()
                server.ehlo()
            smtp_username = os.getenv("SMTP_USERNAME", "").strip()
            if smtp_username:
                server.login(smtp_username, os.getenv("SMTP_PASSWORD", ""))
            rejected = server.send_message(email_message)
            if rejected:
                raise smtplib.SMTPRecipientsRefused(rejected)
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Contact request email delivery failed")
        return jsonify(error="Could not send your request right now. Please try again later."), 502

    return jsonify(message="Your request was sent.")

@app.get("/history")
@login_required
def history_route():
    user = current_user()
    with db_session() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                "SELECT id,name,created,words,similarity FROM scans ORDER BY id DESC LIMIT 200"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,name,created,words,similarity FROM scans "
                "WHERE user_id=? ORDER BY id DESC LIMIT 200",
                (user["id"],),
            ).fetchall()
    return jsonify([dict(row) for row in rows])

@app.get("/history/<int:scan_id>")
@login_required
def history_item_route(scan_id: int):
    user = current_user()
    with db_session() as conn:
        if user["role"] == "admin":
            row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM scans WHERE id=? AND user_id=?",
                (scan_id, user["id"]),
            ).fetchone()
    if row is None:
        return jsonify(error="Scan not found."), 404
    item = dict(row)
    item["result"] = json.loads(item["result"])
    return jsonify(item)

@app.get("/stats")
@login_required
def stats_route():
    user = current_user()
    with db_session() as conn:
        if user["role"] == "admin":
            total, average, flagged = conn.execute(
                "SELECT COUNT(*), COALESCE(AVG(similarity),0), "
                "COALESCE(SUM(CASE WHEN similarity >= 40 THEN 1 ELSE 0 END),0) FROM scans"
            ).fetchone()
        else:
            total, average, flagged = conn.execute(
                "SELECT COUNT(*), COALESCE(AVG(similarity),0), "
                "COALESCE(SUM(CASE WHEN similarity >= 40 THEN 1 ELSE 0 END),0) "
                "FROM scans WHERE user_id=?",
                (user["id"],),
            ).fetchone()
    return jsonify(total=total, average=round(average), flagged=flagged, cleared=total-flagged)

@app.get("/reference-documents")
def list_reference_documents():
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id,name,created,words FROM reference_documents ORDER BY id DESC"
        ).fetchall()
    return jsonify([dict(row) for row in rows])

@app.post("/reference-documents")
def add_reference_document():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify(error="Choose a reference document first."), 400
    try:
        raw = file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return jsonify(error="Reference file exceeds the upload size limit."), 413
        text = extract_document(file.filename, raw)[:MAX_TEXT_CHARS]
        if len(text.split()) < 8:
            return jsonify(error="The reference document must contain at least 8 readable words."), 422
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        name = Path(file.filename).name[:200]
        with db_session() as conn:
            conn.execute(
                "INSERT INTO reference_documents(name,text,sha256,words,created) VALUES(?,?,?,?,?)",
                (name, text, digest, len(text.split()), dt.datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify(id=new_id, name=name, words=len(text.split()), message="Reference document added."), 201
    except sqlite3.IntegrityError:
        return jsonify(error="This reference document is already in the library."), 409
    except ValueError as exc:
        return jsonify(error=str(exc)), 415
    except Exception as exc:
        app.logger.exception("Reference upload failed")
        return jsonify(error=f"Could not add reference document: {exc}"), 422

@app.delete("/reference-documents/<int:document_id>")
def delete_reference_document(document_id: int):
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM reference_documents WHERE id=?", (document_id,))
        deleted = cursor.rowcount
    if not deleted:
        return jsonify(error="Reference document not found."), 404
    return jsonify(message="Reference document deleted.")

@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(error=f"Upload too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB."), 413

@app.errorhandler(500)
def internal_error(_error):
    app.logger.exception("Unhandled server error")
    return jsonify(error="An unexpected server error occurred. Check the backend terminal for details."), 500

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=env_int("PORT", 5000, 1, 65535), debug=False)
