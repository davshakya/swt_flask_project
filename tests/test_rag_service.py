from flask_app.rag_service import RagIndex, chunk_text, project_root, public_configured_roots, read_document
from flask_app import rag_routes


def test_chunk_text_preserves_source():
    chunks = chunk_text("Pump safety guidance. " * 100, "guide.md", chunk_size=200, overlap=30)
    assert len(chunks) > 1
    assert all(chunk.source == "guide.md" for chunk in chunks)


def test_index_returns_grounded_results(tmp_path):
    (tmp_path / "installation.md").write_text("The upper tank sensor prevents overflow.", encoding="utf-8")
    (tmp_path / "pricing.md").write_text("The commercial plan supports multiple sites.", encoding="utf-8")
    results = RagIndex([tmp_path]).search("How is overflow prevented?", limit=2)
    assert results and results[0].source.endswith("installation.md")
    assert results[0].score > 0


def test_index_has_lexical_fallback_without_sklearn(tmp_path, monkeypatch):
    import flask_app.rag_service as service

    (tmp_path / "support.md").write_text("Pump contactor safety and manual stop guidance.", encoding="utf-8")
    monkeypatch.setattr(service, "TfidfVectorizer", None)
    monkeypatch.setattr(service, "cosine_similarity", None)
    results = service.RagIndex([tmp_path]).search("pump safety", limit=1)
    assert results and results[0].source.endswith("support.md")


def test_public_chat_uses_rag_without_exposing_scores(monkeypatch):
    from flask import Flask

    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setattr(rag_routes, "answer_question", lambda question, limit=4, index=None: {
        "answer": f"Grounded: {question}",
        "citations": [{"source": "docs/guide.md", "chunk": 2, "score": 0.9}],
        "generated": False,
    })
    rag_routes._rate_windows.clear()
    app = Flask(__name__)
    app.register_blueprint(rag_routes.public_chat_blueprint)
    response = app.test_client().post("/chatbot/ask", json={"question": "How does the pump work?"})
    assert response.status_code == 200
    assert response.get_json() == {
        "answer": "Grounded: How does the pump work?",
        "citations": [{"source": "docs/guide.md", "chunk": 2}],
    }


def test_old_public_rag_chat_url_is_not_registered(monkeypatch):
    from flask import Flask

    monkeypatch.setenv("RAG_ENABLED", "true")
    app = Flask(__name__)
    app.register_blueprint(rag_routes.public_chat_blueprint)
    assert app.test_client().post("/api/public/chat", json={"question": "test"}).status_code == 404


def test_deployable_chatbot_knowledge_document_is_discovered():
    from flask_app.rag_service import discover_documents, project_root

    expected = project_root() / "docs" / "CHATBOT_KNOWLEDGE_BASE.md"
    assert expected in discover_documents([project_root() / "docs"])


def test_public_corpus_excludes_internal_operations_documents():
    names = {path.name for path in public_configured_roots()}
    assert "CHATBOT_KNOWLEDGE_BASE.md" in names
    assert "SALEWELL_FEATURES_EN.md" in names
    assert "PRODUCTION_READINESS.md" not in names
    assert "JENKINS_WSL_PIPELINES.md" not in names


def test_docx_customer_documents_are_readable():
    docx_paths = [path for path in public_configured_roots() if path.suffix.lower() == ".docx" and path.exists()]
    assert docx_paths
    assert all(read_document(path).strip() for path in docx_paths)


def test_ftps_uploader_packages_customer_rag_sources():
    from scripts.upload_repo_ftps import CUSTOMER_RAG_UPLOADS, DEFAULT_LOCAL_ROOT, iter_upload_items

    expected = set(CUSTOMER_RAG_UPLOADS.values())
    uploaded = {item.relative_path for item in iter_upload_items(DEFAULT_LOCAL_ROOT)}
    assert expected <= uploaded


def test_homepage_template_does_not_require_rag_route_during_render():
    template = (project_root() / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    assert 'fetch("/chatbot/ask"' in template
    assert "url_for('public_rag_chat.public_chat')" not in template


def test_homepage_greeting_is_helpful_without_forcing_a_plan_sale():
    template = (project_root() / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    assert "Hi! Welcome to SaleWell." in template
    assert "Hello again!" in template
    assert "chatbotGreetingCount === 1" in template
