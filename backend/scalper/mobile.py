"""Entry point for the Android app (and for testing the mobile profile on a desktop).

The Android foreground service calls ``start(...)`` on a background thread through Chaquopy. It runs
the normal engine + API server on 127.0.0.1 with the ``mobile`` config profile; the app's WebView
shows the UI from that server. Desktop test:

    python -m scalper.mobile --dir /tmp/scalper-mobile
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from pathlib import Path

_server = None
_lock = threading.Lock()


def _android_notifier():
    """Post Android notifications through the app's Java Notifier class, when running on Android."""
    try:
        from java import jclass  # provided by Chaquopy
    except ImportError:
        return None
    notifier = jclass("com.scalping.terminal.Notifier")
    return lambda title, body: notifier.show(title, body)


def start(files_dir: str, conf_dir: str | None = None, ui_dir: str | None = None, port: int = 8765) -> None:
    """Run the terminal until ``stop()`` is called. Blocking."""
    global _server
    import uvicorn

    files = Path(files_dir)
    files.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SCALPER_PROFILE", "mobile")
    os.environ["SCALPER_LOCAL_CONFIG_DIR"] = str(files)
    os.environ["SCALPER_STORAGE__DATA_DIR"] = str(files / "data")
    if conf_dir:
        os.environ["SCALPER_CONFIG_DIR"] = conf_dir
    log_file = files / "engine.log"
    os.environ["SCALPER_LOG_FILE"] = str(log_file)
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=512_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Imported after the environment is set: config paths are resolved at import time.
    from . import config as config_mod
    from .api import create_app
    from .engine import Engine

    config_mod.CONFIG_DIR = Path(os.environ.get("SCALPER_CONFIG_DIR", config_mod.CONFIG_DIR))
    cfg = config_mod.load_config()
    engine = Engine(cfg)
    hook = _android_notifier()
    if hook:
        engine.alerts.local_hook = hook
    app = create_app(cfg, engine, ui_dir=Path(ui_dir) if ui_dir else None)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", http="h11",
                                           ws="auto", lifespan="on", log_level="warning",
                                           access_log=False))
    with _lock:
        _server = server
    logging.getLogger("scalper.mobile").info("starting mobile engine on 127.0.0.1:%s", port)
    server.run()


def android_main(files_dir: str, port: int = 8765) -> None:
    """Called by the Android EngineService. Config and UI ship as extracted Python packages."""
    import scalper_conf
    import scalper_webui
    start(files_dir, os.path.dirname(scalper_conf.__file__), os.path.dirname(scalper_webui.__file__), int(port))


def stop() -> None:
    with _lock:
        if _server is not None:
            _server.should_exit = True


def running() -> bool:
    with _lock:
        return _server is not None and _server.started and not _server.should_exit


if __name__ == "__main__":
    import argparse

    from .config import REPO_ROOT

    ap = argparse.ArgumentParser(description="Run the terminal with the mobile profile")
    ap.add_argument("--dir", required=True, help="writable directory (settings, data, logs)")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    start(a.dir, str(REPO_ROOT / "config"), str(REPO_ROOT / "frontend" / "dist"), a.port)
