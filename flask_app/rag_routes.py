import os
import secrets
import threading
import time
from flask import Blueprint, jsonify, request, session
from .rag_service import answer_question, default_index, public_index

rag_blueprint = Blueprint("rag", __name__, url_prefix="/api/rag")
public_chat_blueprint = Blueprint("public_rag_chat", __name__)
_rate_lock = threading.Lock()
_rate_windows = {}


@rag_blueprint.before_request
def require_rag_auth():
    if os.environ.get("RAG_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        return jsonify({"error": "RAG is disabled"}), 404
    configured = os.environ.get("RAG_API_KEY", "").strip()
    authorization = request.headers.get("Authorization", "").strip()
    supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not session.get("logged_in") and not (configured and supplied and secrets.compare_digest(configured, supplied)):
        return jsonify({"error": "authentication required"}), 401


@rag_blueprint.route("/search", methods=["POST"])
def search():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"results": [x.to_dict() for x in default_index.search(payload.get("query"), payload.get("limit", 5))]})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@rag_blueprint.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(answer_question(payload.get("question"), payload.get("limit", 5)))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "answer generation failed"}), 502


@rag_blueprint.route("/refresh", methods=["POST"])
def refresh():
    return jsonify({"chunks": default_index.refresh(force=True)})


def _public_chat_rate_allowed(client_id):
    now = time.monotonic()
    window_seconds = max(10, int(os.environ.get("PUBLIC_RAG_RATE_WINDOW_SECONDS", "60")))
    request_limit = max(1, int(os.environ.get("PUBLIC_RAG_RATE_LIMIT", "12")))
    with _rate_lock:
        recent = [stamp for stamp in _rate_windows.get(client_id, []) if now - stamp < window_seconds]
        if len(recent) >= request_limit:
            _rate_windows[client_id] = recent
            return False
        recent.append(now)
        _rate_windows[client_id] = recent
        if len(_rate_windows) > 5000:
            cutoff = now - window_seconds
            for key in list(_rate_windows):
                if not _rate_windows[key] or _rate_windows[key][-1] < cutoff:
                    _rate_windows.pop(key, None)
        return True


@public_chat_blueprint.route("/chatbot/ask", methods=["POST"])
def public_chat():
    if os.environ.get("RAG_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        return jsonify({"error": "The knowledge assistant is currently unavailable."}), 503
    client_id = request.remote_addr or "unknown"
    if not _public_chat_rate_allowed(client_id):
        return jsonify({"error": "Too many questions. Please wait a minute and try again."}), 429
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or "").strip()
    max_length = max(100, int(os.environ.get("PUBLIC_RAG_MAX_QUESTION_LENGTH", "600")))
    if not question:
        return jsonify({"error": "question is required"}), 400
    if len(question) > max_length:
        return jsonify({"error": f"question must be {max_length} characters or fewer"}), 400
    try:
        result = answer_question(question, limit=4, index=public_index)
    except Exception:
        return jsonify({"error": "I could not search the product information just now."}), 502
    # Public clients receive useful source names, but not internal similarity data.
    citations = [{"source": item["source"], "chunk": item["chunk"]} for item in result.get("citations", [])]
    return jsonify({"answer": result.get("answer", ""), "citations": citations, "generated": bool(result.get("generated"))})
