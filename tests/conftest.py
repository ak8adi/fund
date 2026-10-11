import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Jeder Test mit eigener Datenbank und ohne echte Netzwerkzugriffe für ntfy."""
    from fundus import config

    monkeypatch.setenv("FUNDUS_CONFIG", str(tmp_path / "nope.yaml"))
    monkeypatch.setenv("FUNDUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FUNDUS_REQUEST_DELAY", "0,0")
    monkeypatch.setenv("FUNDUS_SCHEDULE_TIMES", "")
    config.get_config.cache_clear()
    from fundus import db

    db.init_db()
    yield
    config.get_config.cache_clear()
