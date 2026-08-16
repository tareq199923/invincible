import invincible.main as main


class _StubSessionStore:
    """Keeps lifespan tests off the real cwd database: SessionStore
    defaults to ./sessions.db when INVINCIBLE_DB_PATH is unset, which
    would litter the repo root during tests."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    async def init(self):
        pass

    async def close(self):
        pass


class _StubOAuthStore(_StubSessionStore):
    pass


async def test_lifespan_pending_actions_default_off(monkeypatch):
    """Without INVINCIBLE_PERSIST_PENDING_ACTIONS the lifespan must
    construct a memory-only PendingActionStore - the original design,
    where a restart orphans staged actions."""
    monkeypatch.delenv("INVINCIBLE_PERSIST_PENDING_ACTIONS", raising=False)
    monkeypatch.delenv("INVINCIBLE_DB_PATH", raising=False)
    monkeypatch.setattr(main, "SessionStore", _StubSessionStore)
    monkeypatch.setattr(main, "OAuthStore", _StubOAuthStore)

    async with main.lifespan(main.app):
        assert main.app.state.pending_actions._db is None


async def test_lifespan_pending_actions_opt_in(monkeypatch, tmp_path):
    """With INVINCIBLE_PERSIST_PENDING_ACTIONS set, the lifespan must
    pass the shared db file through so staged actions survive restarts."""
    monkeypatch.setenv("INVINCIBLE_PERSIST_PENDING_ACTIONS", "1")
    monkeypatch.setenv("INVINCIBLE_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(main, "SessionStore", _StubSessionStore)
    monkeypatch.setattr(main, "OAuthStore", _StubOAuthStore)

    async with main.lifespan(main.app):
        assert main.app.state.pending_actions._db is not None
