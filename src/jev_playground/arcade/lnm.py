"""LN Markets v3 for Sober Satoshi. Public market data always; signed trading only when explicitly enabled.

Trading needs all of: LNM_KEY, LNM_SECRET, LNM_PASSPHRASE, LNM_TRADE_TOKEN, and LNM_LIVE=true.
LNM_NETWORK defaults to signet (test sats). Every order is clamped to LNM_MAX_QTY_USD and LNM_MAX_LEVERAGE.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

NETWORK = os.environ.get("LNM_NETWORK", "signet").strip().lower()
BASES = {"mainnet": "https://api.lnmarkets.com", "signet": "https://api.signet.lnmarkets.com"}
MARKET_BASE = BASES["mainnet"]
TRADE_BASE = BASES.get(NETWORK, BASES["signet"])
MAX_QTY_USD = int(os.environ.get("LNM_MAX_QTY_USD", "50"))
MAX_LEVERAGE = int(os.environ.get("LNM_MAX_LEVERAGE", "10"))


class LnmError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _creds() -> tuple[str, str, str] | None:
    key = os.environ.get("LNM_KEY", "").strip()
    secret = os.environ.get("LNM_SECRET", "").strip()
    passphrase = os.environ.get("LNM_PASSPHRASE", "").strip()
    return (key, secret, passphrase) if key and secret and passphrase else None


def live_enabled() -> bool:
    return bool(_creds()) and os.environ.get("LNM_LIVE", "").lower() == "true" and bool(os.environ.get("LNM_TRADE_TOKEN"))


def token_ok(token: str | None) -> bool:
    expected = os.environ.get("LNM_TRADE_TOKEN", "")
    return bool(expected) and bool(token) and hmac.compare_digest(expected, token)


def status() -> dict:
    return {
        "network": NETWORK,
        "live": live_enabled(),
        "keys_present": bool(_creds()),
        "max_qty_usd": MAX_QTY_USD,
        "max_leverage": MAX_LEVERAGE,
    }


def _request(base: str, method: str, path: str, params: dict | None = None, body: dict | None = None, signed: bool = False):
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body, separators=(",", ":")) if body is not None else ""
    url = f"{base}/v3/{path}{query}"
    headers = {"Accept": "application/json", "User-Agent": "jev-arcade/sober-satoshi"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if signed:
        creds = _creds()
        if not creds:
            raise LnmError(503, "LN Markets keys are not configured on the server.")
        key, secret, passphrase = creds
        timestamp = str(int(time.time() * 1000))
        pathname = urllib.parse.urlparse(url).path
        payload = f"{timestamp}{method.lower()}{pathname}{data or query}"
        signature = base64.b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()).decode()
        headers.update({
            "lnm-access-key": key,
            "lnm-access-passphrase": passphrase,
            "lnm-access-timestamp": timestamp,
            "lnm-access-signature": signature,
        })
    request = urllib.request.Request(url, data=data.encode() if data else None, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode(errors="replace")
        raise LnmError(exc.code, f"LN Markets {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise LnmError(502, f"LN Markets unreachable: {exc.reason}") from None
    return json.loads(raw) if raw else None


# ---------- market features: arithmetic stays here, Jev only sees named buckets ----------

def _ema(values: list[float], span: int) -> float:
    k = 2 / (span + 1)
    ema = values[0]
    for value in values[1:]:
        ema = value * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> float:
    gains, losses = [], []
    for a, b in zip(closes[-period - 1:-1], closes[-period:]):
        change = b - a
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _bucket(value: float, edges: list[tuple[float, str]], top: str) -> str:
    for edge, label in edges:
        if value < edge:
            return label
    return top


def _pct(a: float, b: float) -> float:
    return (b - a) / a * 100 if a else 0.0


class Market:
    TTL = 15

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cached: tuple[float, dict] | None = None

    def snapshot(self) -> dict:
        with self.lock:
            if self.cached and time.time() - self.cached[0] < self.TTL:
                return self.cached[1]
            ticker = _request(MARKET_BASE, "GET", "futures/ticker")
            now = datetime.now(timezone.utc)
            hourly = _request(MARKET_BASE, "GET", "futures/candles", {"from": (now - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"), "range": "1h", "limit": 100})["data"]
            fives = _request(MARKET_BASE, "GET", "futures/candles", {"from": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), "range": "5m", "limit": 60})["data"]
            hourly = sorted(hourly, key=lambda c: c["time"])
            fives = sorted(fives, key=lambda c: c["time"])
            snap = {"ticker": ticker, "hourly": hourly, "fives": fives, "features": self.features(ticker, hourly, fives), "at": now.isoformat()}
            self.cached = (time.time(), snap)
            return snap

    @staticmethod
    def features(ticker: dict, hourly: list[dict], fives: list[dict]) -> dict:
        closes = [c["close"] for c in hourly]
        last = float(ticker.get("lastPrice") or closes[-1])
        day = hourly[-24:]
        high24, low24 = max(c["high"] for c in day), min(c["low"] for c in day)
        chg1h = _pct(closes[-2], last) if len(closes) > 1 else 0
        chg4h = _pct(closes[-5], last) if len(closes) > 4 else 0
        chg24h = _pct(closes[-25], last) if len(closes) > 24 else 0
        rets = [_pct(a, b) for a, b in zip(closes[-25:-1], closes[-24:])]
        vol = math.sqrt(sum(r * r for r in rets) / max(1, len(rets)))
        ema20, ema50 = _ema(closes[-60:], 20), _ema(closes[-60:], 50)
        rsi = _rsi(closes) if len(closes) > 15 else 50
        range_pos = (last - low24) / (high24 - low24) if high24 > low24 else 0.5
        five_closes = [c["close"] for c in fives]
        chg30m = _pct(five_closes[-7], last) if len(five_closes) > 6 else 0
        top = (ticker.get("prices") or [{}])[0]
        spread_bps = (top.get("askPrice", last) - top.get("bidPrice", last)) / last * 10000
        funding = float(ticker.get("fundingRate") or 0)
        move = [(-3, "falling hard"), (-1, "falling"), (-0.25, "slipping"), (0.25, "flat"), (1, "rising"), (3, "rising fast")]
        numbers = {
            "last_price_usd": round(last, 1),
            "change_30m_pct": round(chg30m, 2),
            "change_1h_pct": round(chg1h, 2),
            "change_4h_pct": round(chg4h, 2),
            "change_24h_pct": round(chg24h, 2),
            "hourly_volatility_pct": round(vol, 2),
            "rsi_1h": round(rsi, 1),
            "ema20_vs_ema50_pct": round(_pct(ema50, ema20), 2),
            "position_in_24h_range": round(range_pos, 2),
            "high_24h": high24,
            "low_24h": low24,
            "funding_rate": funding,
            "spread_bps": round(spread_bps, 2),
        }
        words = {
            "last 30 minutes": _bucket(chg30m, move, "surging"),
            "last hour": _bucket(chg1h, move, "surging"),
            "last 4 hours": _bucket(chg4h, move, "surging"),
            "last 24 hours": _bucket(chg24h, move, "surging"),
            "hourly volatility": _bucket(vol, [(0.25, "very calm"), (0.5, "calm"), (0.9, "normal"), (1.5, "choppy")], "wild"),
            "momentum (RSI 1h)": _bucket(rsi, [(25, "deeply oversold"), (35, "oversold"), (45, "soft"), (55, "neutral"), (65, "firm"), (75, "overbought")], "extremely overbought"),
            "trend (EMA20 vs EMA50, 1h)": _bucket(_pct(ema50, ema20), [(-1, "clear downtrend"), (-0.2, "mild downtrend"), (0.2, "no trend"), (1, "mild uptrend")], "clear uptrend"),
            "price within 24h range": _bucket(range_pos, [(0.1, "at the 24h low"), (0.35, "lower third"), (0.65, "middle"), (0.9, "upper third")], "at the 24h high"),
            "funding": _bucket(funding * 10000, [(-1, "shorts pay longs heavily"), (-0.1, "shorts pay longs"), (0.1, "neutral"), (1, "longs pay shorts")], "longs pay shorts heavily"),
            "spread": _bucket(spread_bps, [(0.5, "tight"), (2, "normal")], "wide"),
        }
        return {"numbers": numbers, "words": words}


MARKET = Market()


# ---------- signed calls ----------

def account() -> dict:
    return _request(TRADE_BASE, "GET", "account", signed=True)


def running() -> list:
    return _request(TRADE_BASE, "GET", "futures/isolated/trades/running", signed=True)


def open_trade(side: str, quantity: int, leverage: int, stoploss: float | None, takeprofit: float | None) -> dict:
    if side not in ("buy", "sell"):
        raise LnmError(400, "Side must be buy or sell.")
    quantity = max(1, min(int(quantity), MAX_QTY_USD))
    leverage = max(1, min(int(leverage), MAX_LEVERAGE))
    body = {"type": "market", "side": side, "quantity": quantity, "leverage": leverage}
    if stoploss:
        body["stoploss"] = round(float(stoploss) * 2) / 2
    if takeprofit:
        body["takeprofit"] = round(float(takeprofit) * 2) / 2
    return _request(TRADE_BASE, "POST", "futures/isolated/trade", body=body, signed=True)


def close_trade(trade_id: str) -> dict:
    return _request(TRADE_BASE, "POST", "futures/isolated/trade/close", body={"id": str(trade_id)}, signed=True)
