"""Sign-in gate: off by default, then Firebase tokens and the allow list."""
from fastapi.testclient import TestClient

from ops_agent.web import auth
from ops_agent.web.app import app

client = TestClient(app)


def test_open_without_firebase(monkeypatch):
    monkeypatch.delenv("OPS_FIREBASE_PROJECT", raising=False)
    assert client.get("/api/companies").status_code == 200
    assert client.get("/api/config").json()["firebase"] is None


def test_gate(monkeypatch):
    monkeypatch.setenv("OPS_FIREBASE_PROJECT", "demo-proj")
    monkeypatch.setenv("OPS_FIREBASE_WEB_CONFIG", '{"apiKey": "k", "projectId": "demo-proj"}')
    monkeypatch.setenv("OPS_ALLOWED_DOMAINS", "2ucenter.com")
    monkeypatch.setenv("OPS_ALLOWED_EMAILS", "boss@gmail.com")
    tokens = {"a": {"email": "op@2ucenter.com", "email_verified": True},
              "b": {"email": "boss@gmail.com", "email_verified": True},
              "c": {"email": "someone@gmail.com", "email_verified": True},
              "d": {"email": "op2@2ucenter.com", "email_verified": False}}

    def fake_verify(t):
        if t not in tokens:
            raise ValueError("bad token")
        return tokens[t]
    monkeypatch.setattr(auth, "_verify", fake_verify)
    auth._cache.clear()
    get = lambda tok=None: client.get("/api/companies", headers={"Authorization": f"Bearer {tok}"} if tok else {})  # noqa: E731
    assert client.get("/api/config").json()["firebase"]["projectId"] == "demo-proj"
    assert get().status_code == 401
    assert get("zzz").status_code == 401
    assert get("a").status_code == 200 and get("b").status_code == 200
    assert get("c").status_code == 403 and get("d").status_code == 403
    assert client.get("/").status_code == 200  # the page itself loads, to show the sign-in box


def test_env_files(tmp_path, monkeypatch):
    from ops_agent.web import envfiles
    monkeypatch.delenv("OPS_DB_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPS_AGENT_KEY", "from-env")
    (tmp_path / ".env.local").write_text('# neon\nDATABASE_URL="postgresql://u:p@h/db?sslmode=require"\n')
    (tmp_path / ".env.ops").write_text("OPS_AGENT_KEY=from-file\n")
    envfiles.load(tmp_path)
    import os
    assert os.environ["OPS_DB_DSN"] == "postgresql://u:p@h/db?sslmode=require"
    assert os.environ["OPS_AGENT_KEY"] == "from-env"
