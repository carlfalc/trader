"""A tiny local web server for the dashboards. Serves this folder on 127.0.0.1 only."""

from __future__ import annotations

import json
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class _Handler(SimpleHTTPRequestHandler):
    page = "loop.html"  # which dashboard "/" opens

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _control(self, action: str, rest: str = "") -> None:
        """Dashboard buttons drop a sentinel file the trading bot watches (flatten / stop / resume)."""
        msg = "unknown"
        try:
            if action == "flatten":
                (ROOT / "FLATTEN").write_text("1"); msg = "close-all requested"
            elif action == "flatten-buys":
                (ROOT / "FLATTEN_BUYS").write_text("1"); msg = "close-all-buys requested"
            elif action == "flatten-sells":
                (ROOT / "FLATTEN_SELLS").write_text("1"); msg = "close-all-sells requested"
            elif action == "stop":
                (ROOT / "STOP").write_text("1"); msg = "stop requested"
            elif action == "resume":
                for f in ("STOP", "FLATTEN"):
                    (ROOT / f).unlink(missing_ok=True)
                msg = "resumed"
            elif action == "brain-off":
                (ROOT / "BRAIN_OFF").write_text("1"); msg = "brain off"
            elif action == "brain-on":
                (ROOT / "BRAIN_OFF").unlink(missing_ok=True); msg = "brain on"
            elif action == "primary":
                sym = rest.split("sym=", 1)[1].split("&")[0] if "sym=" in rest else ""
                if sym:
                    (ROOT / "PRIMARY").write_text(sym); msg = f"primary → {sym}"
            elif action == "direction":
                d = rest.split("d=", 1)[1].split("&")[0] if "d=" in rest else ""
                if d in ("both", "buy", "sell"):
                    (ROOT / "DIRECTION").write_text(d); msg = f"direction → {d}"
            elif action == "strategy":
                m = rest.split("m=", 1)[1].split("&")[0] if "m=" in rest else ""
                if m == "jev":
                    (ROOT / "STRATEGY").write_text("jev"); msg = "strategy → Jev"
                elif m == "ours":
                    (ROOT / "STRATEGY").unlink(missing_ok=True); msg = "strategy → ours"
        except Exception as exc:
            msg = str(exc)[:120]
        body = json.dumps({"ok": True, "action": action, "msg": msg}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/control/"):
            rest = self.path.split("/control/", 1)[1]
            return self._control(rest.split("?")[0], rest)
        if self.path in ("/", "/index.html"):
            self.path = f"/dashboard/{self.page}"
        return super().do_GET()

    def log_message(self, *args):  # keep the terminal clean
        pass


def serve(port: int = 8765, open_browser: bool = True, page: str = "loop.html") -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"page": page})
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), partial(handler, directory=str(ROOT)))
    except OSError:
        raise SystemExit(f"  port {port} is busy. Stop the other dashboard (Ctrl+C) or add --port {port + 1}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"  dashboard: {url}")
    if open_browser:
        webbrowser.open(url)
    return server
