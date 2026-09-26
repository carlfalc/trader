"""RON as the bot's brain (Path 3, optional): use GAINEDGE's seven-agent decision engine
to set each instrument's bias, instead of the generic Claude read.

It calls GAINEDGE's `ron-decision-read` Supabase edge function for the instrument + 15m
timeframe and maps RON's `direction` / `recommendation` to long / short / flat. Same
interface as Brain (start / current), so fxbot can use either.

AUTH — two ways, pick one in .env:
  1. Sign in as your GAINEDGE user (no service key, no Supabase dashboard needed):
       GAINEDGE_EMAIL, GAINEDGE_PASSWORD        (+ GAINEDGE_SUPABASE_ANON_KEY)
     The brain logs in via Supabase Auth, uses the access token, and refreshes it itself.
  2. Or a service-role key, if you have it:
       GAINEDGE_SERVICE_KEY

IMPORTANT — RON's own stance: `ron-decision-read` hard-codes `execution_allowed: false`,
`execution_path: "signal_only"`, `probability_status: "not_calibrated"`. RON is telling you
it is a *read*, not a validated execution signal. Using it as a trade bias goes beyond what
RON claims for itself. Demo only until proven. Not financial advice.
"""

from __future__ import annotations

import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LONG_WORDS = {"long", "up", "bull", "bullish", "buy", "up_bias", "upside"}
SHORT_WORDS = {"short", "down", "bear", "bearish", "sell", "down_bias", "downside"}


def _map_direction(direction: str, recommendation: str) -> str:
    d, r = (direction or "").lower(), (recommendation or "").lower()
    if d in LONG_WORDS or r in {"buy", "long", "go_long"}:
        return "long"
    if d in SHORT_WORDS or r in {"sell", "short", "go_short"}:
        return "short"
    return "flat"  # neutral / no-trade / stand-aside


class RonBrain:
    """Polls GAINEDGE's ron-decision-read for one instrument and exposes its bias.
    Authenticates as your GAINEDGE user (email/password, auto-refreshed) or via a
    service-role key. Falls back to 'degraded' (no gate) if RON is unreachable."""

    def __init__(self, symbol: str, every_min: float):
        self.url = os.getenv("GAINEDGE_SUPABASE_URL", "").strip().rstrip("/")
        self.anon = os.getenv("GAINEDGE_SUPABASE_ANON_KEY", "").strip()
        self.service = os.getenv("GAINEDGE_SERVICE_KEY", "").strip()
        self.email = os.getenv("GAINEDGE_EMAIL", "").strip()
        self.password = os.getenv("GAINEDGE_PASSWORD", "")
        self.timeframe = os.getenv("RON_TIMEFRAME", "15m").strip() or "15m"
        self.symbol, self.every = symbol, every_min
        self._access, self._refresh, self._exp = None, None, 0.0
        self._alock = threading.Lock()
        self.state = {"bias": None, "confidence": None, "reason": "waiting for RON's read",
                      "t": None, "model": "RON", "ms": None, "error": None, "degraded": False}
        self.lock = threading.Lock()

    # ---- auth ---------------------------------------------------------------
    def _auth_post(self, grant: str, body: dict) -> dict:
        r = requests.post(f"{self.url}/auth/v1/token?grant_type={grant}",
                          headers={"apikey": self.anon, "Content-Type": "application/json"},
                          json=body, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"login {grant} HTTP {r.status_code}: {r.text[:120]}")
        return r.json()

    def _login(self) -> None:
        d = self._auth_post("password", {"email": self.email, "password": self.password})
        self._access, self._refresh = d["access_token"], d.get("refresh_token")
        self._exp = time.time() + float(d.get("expires_in") or 3600)

    def _bearer(self, force: bool = False) -> str:
        """Return the token for ron-decision-read: the service key, or a fresh user access token."""
        if self.service:
            return self.service
        with self._alock:
            if force:
                self._access = None
            if self._access and time.time() < self._exp - 60:
                return self._access
            try:
                if self._refresh:
                    d = self._auth_post("refresh_token", {"refresh_token": self._refresh})
                    self._access, self._refresh = d["access_token"], d.get("refresh_token", self._refresh)
                    self._exp = time.time() + float(d.get("expires_in") or 3600)
                else:
                    self._login()
            except Exception:
                self._login()  # refresh failed → full login
            return self._access

    # ---- read ---------------------------------------------------------------
    def _read(self) -> dict:
        def call(tok: str) -> requests.Response:
            return requests.post(
                f"{self.url}/functions/v1/ron-decision-read",
                headers={"Authorization": f"Bearer {tok}", "apikey": self.anon or tok,
                         "Content-Type": "application/json"},
                json={"instrument": self.symbol, "timeframe": self.timeframe}, timeout=30)
        r = call(self._bearer())
        if r.status_code == 401 and not self.service:  # token expired → one forced re-login
            r = call(self._bearer(force=True))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        return r.json()

    def think(self) -> None:
        t0 = time.time()
        try:
            if not self.url:
                raise RuntimeError("GAINEDGE_SUPABASE_URL not set in .env")
            if not self.service and not (self.email and self.password):
                raise RuntimeError("set GAINEDGE_EMAIL + GAINEDGE_PASSWORD (or GAINEDGE_SERVICE_KEY) in .env")
            d = self._read()
            ms = round((time.time() - t0) * 1000)
            if not d.get("decision_available"):
                with self.lock:
                    self.state.update(bias="flat", confidence=None, t=time.time(), ms=ms, error=None,
                                      degraded=False, reason=f"RON has no live {self.timeframe} decision — staying flat")
                return
            view = d.get("view") or {}
            dec = view.get("decision") or {}
            bias = _map_direction(str(dec.get("direction") or ""), str(dec.get("recommendation") or ""))
            why = (view.get("explanation") or {}).get("why")
            reason = (why if isinstance(why, str) and why else
                      f"RON: {dec.get('recommendation') or dec.get('direction') or 'read'} "
                      f"({dec.get('state', '')})")
            with self.lock:
                self.state.update(bias=bias, confidence=None, reason=str(reason)[:160], t=time.time(),
                                  ms=ms, error=None, degraded=False)
        except Exception as exc:
            with self.lock:
                self.state["error"] = str(exc)[:160]
                self.state["t"] = time.time()
                if self.state["bias"] is None:  # never got a read → don't stall, fall back to trend+Jev
                    self.state.update(bias=None, degraded=True,
                                      reason="RON unavailable — trading on trend + Jev only")

    def start(self, stop: threading.Event) -> None:
        def run():
            while not stop.is_set():
                self.think()
                # if RON hasn't given a real read yet, retry within a minute instead of waiting the full cycle
                degraded = self.state.get("degraded") or self.state.get("bias") is None
                stop.wait(60 if degraded else self.every * 60)
        threading.Thread(target=run, daemon=True).start()

    def current(self) -> dict:
        with self.lock:
            return dict(self.state)
