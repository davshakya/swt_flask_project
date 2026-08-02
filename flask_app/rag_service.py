"""Local-document retrieval and optional grounded answer generation."""
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import threading
import zipfile
from xml.etree import ElementTree

import requests
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except Exception:  # Keep Passenger online when optional ML wheels are unavailable.
    TfidfVectorizer = None
    cosine_similarity = None

SUPPORTED_SUFFIXES = {".md", ".txt", ".rst", ".docx"}
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


def chunk_text(text, source, chunk_size=1200, overlap=180):
    clean = re.sub(r"\r\n?", "\n", text).strip()
    chunks, start = [], 0
    while start < len(clean):
        end = min(len(clean), start + chunk_size)
        if end < len(clean):
            boundary = max(clean.rfind("\n", start, end), clean.rfind(". ", start, end))
            if boundary > start + chunk_size // 2:
                end = boundary + 1
        value = clean[start:end].strip()
        if value:
            chunks.append(RagChunk(value, source, len(chunks)))
        if end >= len(clean):
            break
        start = max(start + 1, end - overlap)
    return chunks


def read_document(path):
    if path.suffix.lower() != ".docx":
        return path.read_text(encoding="utf-8", errors="replace")
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
        if not self._chunks:
            return []
        if self._matrix is None or self._vectorizer is None or cosine_similarity is None:
            scored = [(self._lexical_score(query, chunk.text), chunk) for chunk in self._chunks]
            scored.sort(key=lambda item: item[0], reverse=True)
            return [RagChunk(chunk.text, chunk.source, chunk.chunk, round(score, 6)) for score, chunk in scored[:max(1, min(int(limit), 10))] if score > 0]
        scores = cosine_similarity(self._vectorizer.transform([query]), self._matrix)[0]
        ranked = scores.argsort()[::-1][:max(1, min(int(limit), 10))]
        return [RagChunk(self._chunks[i].text, self._chunks[i].source, self._chunks[i].chunk, round(float(scores[i]), 6)) for i in ranked if scores[i] > 0]


def _output_text(payload):
    if payload.get("output_text"):
        return payload["output_text"]
    return "\n".join(part.get("text", "") for item in payload.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text").strip()


def concise_answer(text, question="", max_sentences=3):
    clean = re.sub(r"```.*?```", " ", str(text or ""), flags=re.DOTALL)
    clean = re.sub(r"^\s{0,3}#{1,6}\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"[`*_>|]", "", clean)
    clean = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", clean)
    candidates = [item.strip(" -\t\r\n") for item in re.split(r"(?<=[.!?])\s+|\n+", clean) if item.strip(" -\t\r\n")]
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
    response = requests.post(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": os.environ.get("RAG_OPENAI_MODEL", "gpt-5-mini"), "instructions": "Answer only from the context in 2 or 3 short lines and at most 70 words. Give only the directly relevant answer; never include setup, configuration, code, or unrelated details. If context is insufficient, say so briefly.", "input": f"Question: {question}\n\nContext:\n{context}"},
        timeout=float(os.environ.get("RAG_LLM_TIMEOUT_SECONDS", "30")),
    )
    response.raise_for_status()
    return {"answer": concise_answer(_output_text(response.json()), question), "citations": citations, "generated": True}


default_index = RagIndex()
public_index = RagIndex(public_configured_roots())
