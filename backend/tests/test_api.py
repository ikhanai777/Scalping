"""API smoke tests (Starlette app, engine not started — no network)."""
import pytest
from starlette.testclient import TestClient

from scalper.api import create_app
from scalper.config import load_config
from scalper.engine import Engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCALPER_LOCAL_CONFIG_DIR", str(tmp_path))
    cfg = load_config({"storage": {"data_dir": str(tmp_path / "data")}})
    eng = Engine(cfg)
    with TestClient(create_app(cfg, eng, start_engine=False)) as c:
        yield c, eng


def test_read_endpoints(client):
    c, _ = client
    assert c.get("/api/status").json()["phase"] == "init"
    assert c.get("/api/watchlist").json() == []
    assert c.get("/api/account").json()["equity"] == pytest.approx(10000)
    assert set(c.get("/api/signals").json()) == {"active", "recent", "rejected"}
    assert c.get("/api/chart/NOPEUSDT").status_code == 404
    assert c.get("/api/strategies").json()["strategies"]


def test_order_validation_and_paper_order(client):
    c, eng = client
    assert c.post("/api/orders", json={"symbol": "BTCUSDT"}).status_code == 400
    assert c.post("/api/orders", json={"symbol": "BTCUSDT", "side": "UP"}).status_code == 400
    r = c.post("/api/orders", json={"symbol": "BTCUSDT", "side": "LONG", "stop": 99})
    assert r.status_code == 400 and "price" in r.json()["detail"]
    eng.broker.last_price["BTCUSDT"] = 100.0
    r = c.post("/api/orders", json={"symbol": "BTCUSDT", "side": "LONG", "stop": 99, "targets": [101, 102]})
    assert r.status_code == 200 and r.json()["qty"] > 0
    pid = r.json()["id"]
    assert c.post(f"/api/positions/{pid}/close").status_code == 200


def test_settings_roundtrip(client, tmp_path):
    c, _ = client
    r = c.put("/api/settings", json={"risk.equity": "2500", "universe.trend_universe_size": 15,
                                     "alerts.telegram_token": "secret-token"})
    assert r.status_code == 200 and (tmp_path / "local.yaml").exists()
    assert c.put("/api/settings", json={"storage.data_dir": "/"}).status_code == 400
    assert c.put("/api/settings", json={"execution.market": "margin"}).status_code == 400
    cfg = load_config()
    assert cfg.risk.equity == 2500 and cfg.universe.trend_universe_size == 15

def test_settings_get_masks_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SCALPER_LOCAL_CONFIG_DIR", str(tmp_path))
    cfg = load_config({"storage": {"data_dir": str(tmp_path / "data")}, "alerts": {"telegram_token": "secret-token"}})
    with TestClient(create_app(cfg, Engine(cfg), start_engine=False)) as c:
        s = c.get("/api/settings").json()["settings"]
    assert s["alerts.telegram_token"] == "set" and "secret-token" not in str(s)


def test_nan_is_serialised_as_null(client):
    from scalper.api import SafeJSON
    assert SafeJSON({"x": float("nan"), "y": [float("inf"), 1.0]}).body == b'{"x":null,"y":[null,1.0]}'
