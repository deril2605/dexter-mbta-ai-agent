"""Milestone 6 — FastAPI service (graph injected; no live calls)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dexter.service.app import create_app


class FakeGraph:
    """Records ainvoke calls and returns a canned state."""

    def __init__(self, reply: str = "ok", needs_input: bool = False):
        self.reply = reply
        self.needs_input = needs_input
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, payload: dict, config: dict) -> dict:
        self.calls.append((payload, config))
        return {"reply": f"{self.reply}: {payload['message']}", "needs_input": self.needs_input}


def test_health_ok():
    with TestClient(create_app(graph=FakeGraph())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_returns_reply_and_needs_input():
    fake = FakeGraph(reply="echo", needs_input=True)
    with TestClient(create_app(graph=fake)) as client:
        response = client.post("/chat", json={"session_id": "s1", "message": "hi"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"reply": "echo: hi", "needs_input": True}


def test_chat_threads_session_id_as_thread_id():
    fake = FakeGraph()
    with TestClient(create_app(graph=fake)) as client:
        client.post("/chat", json={"session_id": "abc123", "message": "next 116"})

    _payload, config = fake.calls[0]
    assert config["configurable"]["thread_id"] == "abc123"


def test_chat_survives_graph_error():
    class BoomGraph:
        async def ainvoke(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    with TestClient(create_app(graph=BoomGraph())) as client:
        response = client.post("/chat", json={"session_id": "s", "message": "hi"})

    assert response.status_code == 200
    assert "problem" in response.json()["reply"].lower()
    assert response.json()["needs_input"] is False


def test_chat_validates_request_body():
    with TestClient(create_app(graph=FakeGraph())) as client:
        response = client.post("/chat", json={"message": "missing session"})
    assert response.status_code == 422  # session_id required


# --- Beta web client: passcode gate + static UI (both off by default) ---


def test_chat_open_when_no_passcode():
    # Default state: no passcode configured, so no header is required.
    with TestClient(create_app(graph=FakeGraph())) as client:
        response = client.post("/chat", json={"session_id": "s", "message": "hi"})
    assert response.status_code == 200


def test_chat_rejects_missing_or_wrong_passcode():
    app = create_app(graph=FakeGraph())
    app.state.passcode = "let-me-in"
    with TestClient(app) as client:
        missing = client.post("/chat", json={"session_id": "s", "message": "hi"})
        wrong = client.post(
            "/chat",
            json={"session_id": "s", "message": "hi"},
            headers={"X-Dexter-Passcode": "nope"},
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_chat_accepts_correct_passcode():
    app = create_app(graph=FakeGraph(reply="echo"))
    app.state.passcode = "let-me-in"
    with TestClient(app) as client:
        response = client.post(
            "/chat",
            json={"session_id": "s", "message": "hi"},
            headers={"X-Dexter-Passcode": "let-me-in"},
        )
    assert response.status_code == 200
    assert response.json()["reply"] == "echo: hi"


def test_index_404_when_web_disabled():
    # serve_web defaults to False, so the root path is not served.
    with TestClient(create_app(graph=FakeGraph())) as client:
        assert client.get("/").status_code == 404


def test_index_serves_page_when_web_enabled():
    app = create_app(graph=FakeGraph())
    app.state.serve_web = True
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "dexter" in response.text.lower()
