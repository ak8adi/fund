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
    from fundus import db, fx

    # Keine Live-Wechselkurse in Tests: feste Kurse aus der Konfiguration
    monkeypatch.setitem(fx._cache, "ts", float("inf"))
    monkeypatch.setitem(fx._cache, "rates", {})

    from fundus import delivery
    monkeypatch.setattr(delivery, "ENABLED", False)  # keine Fahrzeit-Abfragen ins Netz

    db.init_db()
    yield
    config.get_config.cache_clear()
