"""Local-document retrieval and optional grounded answer generation."""
from dataclasses import asdict, dataclass
import hashlib
import html
import os
from pathlib import Path
import re
import threading
import zipfile
from xml.etree import ElementTree

import requests

# Also protect direct Flask/Gunicorn starts that do not go through
# passenger_wsgi.py.  The values are set before NumPy is loaded by scikit-learn.
for _thread_env in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_thread_env, "1")

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except Exception:  # Keep Passenger online when optional ML wheels are unavailable.
    TfidfVectorizer = None
    cosine_similarity = None

SUPPORTED_SUFFIXES = {".md", ".txt", ".rst", ".docx", ".html"}
DEFAULT_EXCLUDES = {".git", ".venv", "node_modules", "mysql-data", "data"}


@dataclass(frozen=True)
class RagChunk:
    text: str
    source: str
    chunk: int
    score: float = 0.0

    def to_dict(self):
        return asdict(self)


def workspace_root():
    return Path(__file__).resolve().parents[2]


def project_root():
    return Path(__file__).resolve().parents[1]


def configured_roots():
    raw = os.environ.get("RAG_DOCUMENT_PATHS", "").strip()
    project = project_root()
    workspace = workspace_root()
    if not raw:
        return [project / "docs", project / "README.md", workspace / "docs", workspace / "README.md"]
    result = []
    for value in raw.split(os.pathsep):
        path = Path(value.strip()).expanduser()
        result.append((project / path).resolve() if not path.is_absolute() else path.resolve())
    return result


def public_configured_roots():
    """Return only documents approved for anonymous website answers."""
    raw = os.environ.get("PUBLIC_RAG_DOCUMENT_PATHS", "").strip()
    if raw:
        project = project_root()
        roots = []
        for value in raw.split(os.pathsep):
            path = Path(value.strip()).expanduser()
            roots.append((project / path).resolve() if not path.is_absolute() else path.resolve())
        return roots
    project, workspace = project_root(), workspace_root()
    return [
        project / "docs" / "CHATBOT_KNOWLEDGE_BASE.md",
        project / "docs" / "CUSTOMER_FAQ.md",
        project / "docs" / "CUSTOMER_INSTALLATION_GUIDE_EN_HI.html",
        project / "docs" / "INSTALLATION_GUIDE_EN_HI.md",
        project / "docs" / "customer_sources",
        workspace / "docs" / "SALEWELL_FEATURES_EN.md",
        workspace / "docs" / "SALEWELL_FEATURES_HI.md",
        workspace / "docs" / "INSTALLATION_RULE_BOOK_HI_EN.md",
        workspace / "docs" / "COMPONENTS_AND_BOM.md",
        workspace / "docs" / "MODULAR_PRODUCT_ARCHITECTURE.md",
        workspace / "Smart_Water_Tank_100_Device_Estimation.docx",
        workspace / "Smart_Water_Tank_Controller_BOM.docx",
    ]


def discover_documents(roots=None):
    files = []
    for root in roots or configured_roots():
        if root.is_file() and root.suffix.lower() in SUPPORTED_SUFFIXES:
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES and not DEFAULT_EXCLUDES.intersection(path.parts))
    return sorted(set(files))


def _plain_chunks(text, source, first_chunk, chunk_size=1200, overlap=180):
    """Split a single logical section without starting on a partial line."""
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + chunk_size // 2:
                end = boundary + 1
        value = text[start:end].strip()
        if value:
            chunks.append(RagChunk(value, source, first_chunk + len(chunks)))
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        line_start = text.find("\n", next_start, min(len(text), end + 120))
        start = line_start + 1 if line_start >= 0 else next_start
    return chunks


def chunk_text(text, source, chunk_size=1200, overlap=180):
    """Keep Markdown question/heading sections intact for accurate retrieval."""
    clean = re.sub(r"\r\n?", "\n", text).strip()
    if not clean:
        return []
    # Each Markdown heading and its body is a retrieval unit. This prevents a
    # price or troubleshooting answer from being mixed with adjacent sections.
    sections = re.split(r"(?=^#{2,4}\s+\S)", clean, flags=re.MULTILINE) if Path(source).suffix.lower() == ".md" else [clean]
    chunks = []
    for section in sections:
        section = section.strip()
        if section:
            chunks.extend(_plain_chunks(section, source, len(chunks), chunk_size, overlap))
    return chunks


def normalize_search_query(query):
    value = str(query or "").strip()
    lowered = value.lower()
    expansions = []
    if re.search(r"\b(min|minimum|lowest|cheapest)\b", lowered):
        expansions.append("lowest price plan Home Basic")
    if re.search(r"\b(max|maximum|highest|costliest|most expensive)\b", lowered):
        expansions.append("highest price plan Enterprise Modular")
    if "stale" in lowered:
        expansions.append("remote data stale cloud last seen connectivity")
    if re.search(r"\b(install|installation|sensor|valve|contactor|panel|wiring|setup)\b", lowered):
        expansions.append("customer installation guide easy safe installation sensors valves electrical panel contactor")
    if re.search(r"\b(troubleshoot|problem|wrong|incorrect|not start|not stop|offline|cannot connect|can't connect)\b", lowered):
        expansions.append("installation guide troubleshooting customer safety checks")
    return " ".join([value] + expansions)


def read_document(path):
    if path.suffix.lower() != ".docx":
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".html":
            text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<[^>]+>", "\n", text)
            text = html.unescape(text)
            text = re.sub(r"\n{3,}", "\n\n", text)
        return text
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        text = "".join(node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


class RagIndex:
    def __init__(self, roots=None):
        self.roots, self._lock = roots, threading.Lock()
        self._fingerprint = self._chunks = self._matrix = self._vectorizer = None

    def refresh(self, force=False):
        files = discover_documents(self.roots)
        state = "\n".join(f"{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in files)
        fingerprint = hashlib.sha256(state.encode()).hexdigest()
        if not force and fingerprint == self._fingerprint:
            return len(self._chunks)
        with self._lock:
            chunks, root = [], workspace_root().resolve()
            for path in files:
                try:
                    source = str(path.resolve().relative_to(root)).replace("\\", "/")
                except ValueError:
                    source = str(path.resolve())
                chunks.extend(chunk_text(read_document(path), source))
            vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=30000) if TfidfVectorizer else None
            self._matrix = vectorizer.fit_transform([c.text for c in chunks]) if chunks and vectorizer else None
            self._chunks, self._vectorizer, self._fingerprint = chunks, vectorizer, fingerprint
        return len(self._chunks)

    @staticmethod
    def _lexical_score(query, text):
        query_words = set(re.findall(r"[a-z0-9]+", query.lower()))
        text_words = set(re.findall(r"[a-z0-9]+", text.lower()))
        if not query_words:
            return 0.0
        return len(query_words.intersection(text_words)) / len(query_words)

    def search(self, query, limit=5):
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        self.refresh()
        search_query = normalize_search_query(query)
        if not self._chunks:
            return []
        if self._matrix is None or self._vectorizer is None or cosine_similarity is None:
            scored = [(self._lexical_score(search_query, chunk.text), chunk) for chunk in self._chunks]
            scored.sort(key=lambda item: item[0], reverse=True)
            return [RagChunk(chunk.text, chunk.source, chunk.chunk, round(score, 6)) for score, chunk in scored[:max(1, min(int(limit), 10))] if score > 0]
        scores = cosine_similarity(self._vectorizer.transform([search_query]), self._matrix)[0]
        ranked = scores.argsort()[::-1][:max(1, min(int(limit), 10))]
        return [RagChunk(self._chunks[i].text, self._chunks[i].source, self._chunks[i].chunk, round(float(scores[i]), 6)) for i in ranked if scores[i] > 0]


def _output_text(payload):
    if payload.get("output_text"):
        return payload["output_text"]
    return "\n".join(part.get("text", "") for item in payload.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text").strip()


def concise_answer(text, question="", max_sentences=3):
    clean = re.sub(r"```.*?```", " ", str(text or ""), flags=re.DOTALL)
    # Headings are retrieval metadata, not part of the customer-facing answer.
    clean = re.sub(r"^\s{0,3}#{1,6}\s+.*$", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"[`*_>|]", "", clean)
    clean = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", clean)
    # Markdown wraps prose across physical lines. Reflow each paragraph before
    # selecting sentences so prices and answers are never cut mid-sentence.
    paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", clean) if item.strip()]
    candidates = [item.strip(" -\t\r\n") for paragraph in paragraphs for item in re.split(r"(?<=[.!?])\s+", paragraph) if item.strip(" -\t\r\n")]
    candidates = [item for item in candidates if len(item) <= 280 and not item.startswith(("RAG_", "PUBLIC_RAG_", "OPENAI_"))]
    query_words = set(re.findall(r"[a-z0-9]+", str(question).lower()))
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (-len(query_words.intersection(re.findall(r"[a-z0-9]+", item[1].lower()))), item[0]),
    )
    selected_indexes = sorted(index for index, _ in ranked[:max_sentences])
    selected = [candidates[index] for index in selected_indexes]
    return "\n".join(selected)[:600].strip()


def answer_question(question, limit=5, index=None):
    matches = (index or default_index).search(question, limit)
    citations = [{"source": x.source, "chunk": x.chunk, "score": x.score} for x in matches]
    if not matches:
        return {"answer": "I do not have enough verified SaleWell information to answer that. Please ask about our products, plans, installation, pump control, or support.", "citations": [], "generated": False}
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"answer": concise_answer(matches[0].text, question), "citations": citations, "generated": False}
    context = "\n\n".join(f"[{i+1}] {x.source}#chunk-{x.chunk}\n{x.text}" for i, x in enumerate(matches))
    try:
        response = requests.post(
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": os.environ.get("RAG_OPENAI_MODEL", "gpt-5-mini"), "instructions": "Answer only from the context in 2 or 3 short lines and at most 70 words. Give only the directly relevant answer; never include setup, configuration, code, or unrelated details. If context is insufficient, say so briefly.", "input": f"Question: {question}\n\nContext:\n{context}"},
            timeout=float(os.environ.get("RAG_LLM_TIMEOUT_SECONDS", "30")),
        )
        response.raise_for_status()
        generated_answer = concise_answer(_output_text(response.json()), question)
        if generated_answer:
            return {"answer": generated_answer, "citations": citations, "generated": True}
    except (requests.RequestException, ValueError, KeyError):
        # Product help must remain useful when the provider key, model, quota,
        # network, or response is temporarily unavailable.
        pass
    return {"answer": concise_answer(matches[0].text, question), "citations": citations, "generated": False}


default_index = RagIndex()
public_index = RagIndex(public_configured_roots())
