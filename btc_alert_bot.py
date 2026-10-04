#!/usr/bin/env python3
"""
btc_alert_bot.py — Alerte Telegram quand un scénario de trading BTC est validé.

Ce que fait le bot :
  - lit les bougies 15 min du BTCUSDT perpétuel (MEXC par défaut, bascule auto
    sur Binance, Bybit puis Coinbase si une source ne répond pas) ;
  - reconstruit les bougies 1 h et 4 h (alignées sur l'heure UTC, comme les exchanges) ;
  - fait tourner une machine d'état par scénario défini dans config.toml :
    en attente → armé → déclenché / annulé / expiré ;
  - au déclenchement, envoie le trade exact : sens, entrée, zone valide, levier,
    taille, SL, TP (prix + % de la position), liquidation estimée ;
  - suit chaque signal en « trade virtuel » et publie le résultat en R,
    pour mesurer si les setups ont réellement un avantage (/stats).

Aucune exécution automatique : le bot n'a aucun accès à ton compte d'exchange.
Python 3.11+, aucune dépendance externe.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

M15 = 15 * 60_000
TF_MS = {"15m": M15, "1h": 3_600_000, "4h": 14_400_000}
CLOSE_GRACE_MS = 10_000          # on attend 10 s après la clôture avant de lire la bougie
FETCH_LIMIT = 300                # 300 bougies 15 min = 75 h d'historique
KEEP_CANDLES = 600
UA = {"User-Agent": "btc-alert-bot/1.0"}
LT, GT = "&lt;", "&gt;"   # Telegram HTML : < et > doivent être échappés


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {msg}", flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


# ───────────────────────────── HTTP ─────────────────────────────
def http_json(url, params=None, payload=None, timeout=15):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = dict(UA)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ───────────────────────────── Bougies ─────────────────────────────
@dataclass
class Candle:
    t: int      # heure d'ouverture, ms UTC
    o: float
    h: float
    l: float
    c: float

    def is_closed(self, now: int, tf_ms: int = M15) -> bool:
        return now >= self.t + tf_ms + CLOSE_GRACE_MS


def _fetch_mexc(sym, limit):
    end = int(time.time())
    j = http_json(f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                  {"interval": "Min15", "start": end - limit * 900, "end": end})
    if not j.get("success"):
        raise RuntimeError(f"réponse MEXC invalide : {str(j)[:200]}")
    d = j["data"]
    return [Candle(int(t) * 1000, float(o), float(h), float(l), float(c))
            for t, o, h, l, c in zip(d["time"], d["open"], d["high"], d["low"], d["close"])]


def _fetch_binance(sym, limit):
    rows = http_json("https://fapi.binance.com/fapi/v1/klines",
                     {"symbol": sym, "interval": "15m", "limit": limit})
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def _fetch_bybit(sym, limit):
    j = http_json("https://api.bybit.com/v5/market/kline",
                  {"category": "linear", "symbol": sym, "interval": "15", "limit": limit})
    if j.get("retCode") != 0:
        raise RuntimeError(f"réponse Bybit invalide : {str(j)[:200]}")
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in j["result"]["list"]]


def _fetch_coinbase(sym, limit):
    rows = http_json(f"https://api.exchange.coinbase.com/products/{sym}/candles", {"granularity": 900})
    # format Coinbase : [time, low, high, open, close, volume]
    return [Candle(int(r[0]) * 1000, float(r[3]), float(r[2]), float(r[1]), float(r[4])) for r in rows]


SOURCES = {
    # nom : (fonction, clé de symbole dans [data], libellé)
    "mexc": (_fetch_mexc, "symbol_mexc", "MEXC perp"),
    "binance": (_fetch_binance, "symbol_binance", "Binance perp"),
    "bybit": (_fetch_bybit, "symbol_bybit", "Bybit perp"),
    "coinbase": (_fetch_coinbase, "symbol_coinbase", "Coinbase spot"),
}


def fetch_mexc_contract_size(sym):
    j = http_json("https://contract.mexc.com/api/v1/contract/detail", {"symbol": sym})
    return float(j["data"]["contractSize"])


# ───────────────────────────── Format ─────────────────────────────
def fnum(x, dec=0):
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")


def fp(x):
    return fnum(x)


def fusd(x):
    return f"{'−' if x < 0 else ''}{fnum(abs(x))} $"


def fsusd(x):
    return f"{'+' if x >= 0 else '−'}{fnum(abs(x))} $"


def fpct(x):
    return f"{'+' if x >= 0 else '−'}{fnum(abs(x), 2)} %"


def fsr(x):
    return f"{'+' if x >= 0 else '−'}{fnum(abs(x), 2)}R"


def fbtc(q):
    return f"{fnum(q, 3)} BTC"


def esc(s):
    return html.escape(str(s), quote=False)


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ───────────────────────────── Config ─────────────────────────────
DEFAULTS = {
    "timezone": "Europe/Paris",
    "poll_seconds": 30,
    "replay_hours": 6,
    "max_alert_age_min": 20,
    "valid_until": None,
    "data": {
        "sources": ["mexc", "binance", "bybit", "coinbase"],
        "symbol_mexc": "BTC_USDT",
        "symbol_binance": "BTCUSDT",
        "symbol_bybit": "BTCUSDT",
        "symbol_coinbase": "BTC-USD",
    },
    "account": {
        "capital_usd": 2000,
        "margin_usage": 0.95,
        "mmr": 0.004,
        "taker_fee": 0.0005,
        "max_leverage": 50,
        "min_liq_ratio": 2.5,
        "qty_step": 0.001,
        "move_sl_to_be_after_tp1": True,
        "min_final_r": 1.5,
        "virtual_trade_max_hours": 72,
    },
    "touch_alerts": {"enabled": False, "levels": [], "rearm_pct": 0.3},
    "scenarios": {},
}

REQUIRED = {
    "sweep_reversal": ["direction", "sweep_level", "trigger_tf", "trigger_close",
                       "entry_zone", "sl", "tps", "risk_usd", "expire_hours"],
    "breakout_retest": ["direction", "breakout_tf", "breakout_close", "retest_touch",
                        "retest_hold_close", "fail_close", "entry_zone", "sl", "tps",
                        "risk_usd", "expire_hours"],
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "scenarios":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = deep_merge(DEFAULTS, raw)
    if cfg["valid_until"]:
        dt = datetime.fromisoformat(str(cfg["valid_until"]))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cfg["valid_until_ms"] = int(dt.timestamp() * 1000)
    else:
        cfg["valid_until_ms"] = None
    for name in cfg["data"]["sources"]:
        if name not in SOURCES:
            raise ValueError(f"source inconnue : {name} (choix : {', '.join(SOURCES)})")
    for key, sc in cfg["scenarios"].items():
        _validate_scenario(key, sc)
    return cfg


def _validate_scenario(key, sc):
    typ = sc.get("type")
    if typ not in REQUIRED:
        raise ValueError(f"[{key}] type doit être 'sweep_reversal' ou 'breakout_retest'")
    missing = [k for k in REQUIRED[typ] if k not in sc]
    if missing:
        raise ValueError(f"[{key}] champs manquants : {', '.join(missing)}")
    if sc["direction"] not in ("long", "short"):
        raise ValueError(f"[{key}] direction doit être 'long' ou 'short'")
    for tfk in ("trigger_tf", "cancel_tf", "breakout_tf"):
        if tfk in sc and sc[tfk] not in TF_MS:
            raise ValueError(f"[{key}] {tfk} doit être 15m, 1h ou 4h")
    lo, hi = sc["entry_zone"]
    if lo >= hi:
        raise ValueError(f"[{key}] entry_zone : [bas, haut]")
    sign = 1 if sc["direction"] == "long" else -1
    if (sign == 1 and sc["sl"] >= lo) or (sign == -1 and sc["sl"] <= hi):
        raise ValueError(f"[{key}] le SL est du mauvais côté de la zone d'entrée")
    if not sc["tps"]:
        raise ValueError(f"[{key}] au moins un TP")
    for price, pct in sc["tps"]:
        if pct <= 0:
            raise ValueError(f"[{key}] % de TP doit être > 0")
        if (sign == 1 and price <= hi) or (sign == -1 and price >= lo):
            raise ValueError(f"[{key}] TP {price} du mauvais côté de la zone d'entrée")


def scenario_hash(sc):
    return hashlib.sha1(json.dumps(sc, sort_keys=True).encode()).hexdigest()[:12]


# ───────────────────────────── Calcul du trade ─────────────────────────────
def build_trade(direction, entry, sl, tps, risk_usd, acc):
    """Taille la position à partir du risque en $ et de la distance au SL.

    Le levier n'est qu'une conséquence : c'est le plus bas qui fait tenir la marge
    dans le budget, plafonné pour que la liquidation reste au moins
    `min_liq_ratio` fois plus loin que le SL.
    """
    sign = 1 if direction == "long" else -1
    risk_pts = (entry - sl) * sign
    if risk_pts <= 0:
        return {"error": "le SL est du mauvais côté du prix"}
    tps = sorted(tps, key=lambda tp: tp[0] * sign)
    if (tps[0][0] - entry) * sign <= 0:
        return {"error": f"le prix a déjà dépassé le TP1 ({fp(tps[0][0])})"}

    sl_pct = risk_pts / entry
    mmr = acc["mmr"]
    max_margin = acc["capital_usd"] * acc["margin_usage"]
    lev_cap = max(1, min(int(acc["max_leverage"]),
                         math.floor(1 / (acc["min_liq_ratio"] * sl_pct + mmr))))
    notional = risk_usd / risk_pts * entry
    lev = max(1, math.ceil(notional / max_margin))
    reduced = False
    if lev > lev_cap:
        lev = lev_cap
        notional = max_margin * lev
        reduced = True

    step = acc["qty_step"]
    qty = round(math.floor(notional / entry / step + 1e-9) * step, 8)
    if qty <= 0:
        return {"error": "taille calculée nulle (risque trop faible pour le pas de quantité)"}
    notional = qty * entry
    margin = notional / lev
    if sign == 1:
        liq = (entry * qty - margin) / (qty * (1 - mmr))
    else:
        liq = (entry * qty + margin) / (qty * (1 + mmr))

    total_pct = sum(p for _, p in tps)
    legs, remaining = [], qty
    for i, (price, pct) in enumerate(tps):
        if i == len(tps) - 1:
            q = remaining
        else:
            q = round(math.floor(qty * pct / total_pct / step + 1e-9) * step, 8)
        remaining = round(remaining - q, 8)
        move = (price - entry) * sign
        legs.append({
            "price": price,
            "pct": pct * 100 / total_pct,
            "qty": q,
            "r": move / risk_pts,
            "usd": q * move,
            "move_pct": (price - entry) / entry * 100,
        })
    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "sl_pct": (sl - entry) / entry * 100,
        "risk_pts": risk_pts,
        "qty": qty,
        "notional": notional,
        "lev": lev,
        "margin": margin,
        "liq": liq,
        "liq_ratio": abs(entry - liq) / risk_pts,
        "risk": qty * risk_pts,
        "risk_wanted": risk_usd,
        "reduced": reduced,
        "fees": notional * acc["taker_fee"] * 2,
        "legs": legs,
        "final_r": legs[-1]["r"],
        "avg_r": sum(l["pct"] / 100 * l["r"] for l in legs),
        "total_usd": sum(l["usd"] for l in legs),
    }


# ───────────────────────────── Telegram ─────────────────────────────
class Telegram:
    def __init__(self, token, chat_id, dry_run=False):
        self.token = token
        self.chat_id = str(chat_id)
        self.dry_run = dry_run
        self.sent: list[str] = []

    @property
    def can_receive(self):
        return bool(self.token) and not self.dry_run

    def send(self, text):
        self.sent.append(text)
        if self.dry_run:
            print("\n" + "─" * 44 + "\n" + strip_html(text) + "\n" + "─" * 44, flush=True)
            return
        for i in range(0, len(text), 4000):
            try:
                http_json(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          payload={"chat_id": self.chat_id, "text": text[i:i + 4000],
                                   "parse_mode": "HTML", "disable_web_page_preview": True})
            except Exception as e:  # on log, on ne crashe pas
                log(f"Telegram : échec d'envoi ({e})")

    def get_updates(self, offset):
        params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        j = http_json(f"https://api.telegram.org/bot{self.token}/getUpdates", params=params)
        return j.get("result", []) if j.get("ok") else []


# ───────────────────────────── Moteur ─────────────────────────────
STATUS_ICON = {"idle": "⏳", "armed": "🟡", "triggered": "✅", "cancelled": "❌",
               "expired": "⌛", "missed": "⚪️", "skipped": "⚪️"}
STATUS_TXT = {"triggered": "déclenché", "cancelled": "annulé", "expired": "expiré",
              "missed": "manqué / hors zone", "skipped": "R:R insuffisant"}


class Engine:
    def __init__(self, cfg_path, data_dir, tg, fetch=None, clock=None):
        self.cfg_path = cfg_path
        self.data_dir = data_dir
        self.tg = tg
        self.fetch_override = fetch
        self.clock = clock or now_ms
        os.makedirs(data_dir, exist_ok=True)
        self.state_path = os.path.join(data_dir, "state.json")
        self.csv_path = os.path.join(data_dir, "signals.csv")
        self.candles: dict[int, Candle] = {}
        self.contract_size = None
        self._cs_tried = 0.0
        self._announced = False
        self.state = self._load_state()
        self.cfg = load_config(cfg_path)
        self.cfg_mtime = os.path.getmtime(cfg_path)
        self.tz = self._tz()
        self._reconcile_scenarios(notify=False)

    # ── état ──
    def _load_state(self):
        base = {"version": 1, "last_t": None, "last_price": None, "source": None,
                "scenarios": {}, "open_trades": [], "history": [], "touch": {},
                "tg_offset": None, "paused": False, "stale_notified": False, "fail_count": 0}
        try:
            with open(self.state_path) as f:
                base.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"state.json illisible, on repart de zéro ({e})")
        return base

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    def _tz(self):
        if ZoneInfo:
            try:
                return ZoneInfo(self.cfg["timezone"])
            except Exception:
                log(f"fuseau {self.cfg['timezone']} introuvable, affichage en UTC")
        return timezone.utc

    def fmt_time(self, ms, with_date=False):
        dt = datetime.fromtimestamp(ms / 1000, self.tz)
        return dt.strftime("%d/%m %H:%M" if with_date else "%H:%M")

    def notify(self, text, silent=False):
        if silent:
            log("(silencieux) " + strip_html(text).replace("\n", " | "))
        else:
            self.tg.send(text)

    # ── config ──
    def _reconcile_scenarios(self, notify=True):
        scs = self.state["scenarios"]
        for key, sc in self.cfg["scenarios"].items():
            h = scenario_hash(sc)
            if key not in scs or scs[key].get("hash") != h:
                existed = key in scs
                scs[key] = {"status": "idle", "hash": h}
                if notify and existed:
                    self.notify(f"🔁 <b>{self.label(key)}</b> : paramètres modifiés, scénario réinitialisé.")
        for key in list(scs):
            if key not in self.cfg["scenarios"]:
                del scs[key]

    def maybe_reload_config(self):
        try:
            mtime = os.path.getmtime(self.cfg_path)
        except OSError:
            return
        if mtime == self.cfg_mtime:
            return
        self.cfg_mtime = mtime
        try:
            new = load_config(self.cfg_path)
        except Exception as e:
            self.notify(f"⚠️ config.toml invalide, je garde l'ancienne : {esc(e)}")
            return
        old_valid = self.cfg.get("valid_until_ms")
        self.cfg = new
        self.tz = self._tz()
        if new.get("valid_until_ms") != old_valid:
            self.state["stale_notified"] = False
        self._reconcile_scenarios(notify=True)
        self.notify("🔄 config.toml rechargée.")

    def label(self, key):
        sc = self.cfg["scenarios"].get(key, {})
        name = sc.get("name")
        return f"{key} · {esc(name)}" if name else key

    def levels_stale(self, now):
        vu = self.cfg.get("valid_until_ms")
        return vu is not None and now > vu

    # ── données ──
    def fetch_candles(self):
        if self.fetch_override:
            return self.fetch_override()
        errors = []
        for name in self.cfg["data"]["sources"]:
            fn, sym_key, _ = SOURCES[name]
            try:
                candles = fn(self.cfg["data"][sym_key], FETCH_LIMIT)
                if candles:
                    return candles, name
                errors.append(f"{name}: vide")
            except urllib.error.HTTPError as e:
                errors.append(f"{name}: HTTP {e.code}")
            except Exception as e:
                errors.append(f"{name}: {e}")
        raise RuntimeError("aucune source ne répond — " + " ; ".join(errors))

    def _maybe_contract_size(self, source):
        if source != "mexc" or self.contract_size or self.fetch_override:
            return
        if time.time() - self._cs_tried < 3600:
            return
        self._cs_tried = time.time()
        try:
            self.contract_size = fetch_mexc_contract_size(self.cfg["data"]["symbol_mexc"])
        except Exception as e:
            log(f"taille de contrat MEXC indisponible ({e})")

    def closes_for(self, c):
        """Bougies qui se clôturent avec cette bougie 15 min (15m, et 1h/4h aux frontières)."""
        res = {"15m": c}
        end = c.t + M15
        for tf in ("1h", "4h"):
            ms = TF_MS[tf]
            if end % ms == 0:
                start = end - ms
                parts = [self.candles[t] for t in range(start, end, M15) if t in self.candles]
                if parts:
                    res[tf] = Candle(start, parts[0].o, max(p.h for p in parts),
                                     min(p.l for p in parts), parts[-1].c)
        return res

    # ── boucle ──
    def tick(self):
        now = self.clock()
        self.maybe_reload_config()
        try:
            candles, source = self.fetch_candles()
        except Exception as e:
            self.state["fail_count"] += 1
            log(str(e))
            limit = max(1, math.ceil(300 / self.cfg["poll_seconds"]))
            if self.state["fail_count"] == limit:
                self.notify(f"⚠️ Plus de données de prix depuis ~5 min.\n{esc(e)}")
            self.save()
            return False
        if self.state["fail_count"] >= max(1, math.ceil(300 / self.cfg["poll_seconds"])):
            self.notify("✅ Données de prix revenues.")
        self.state["fail_count"] = 0

        if self.state["source"] and source != self.state["source"]:
            note = " (spot : écart possible avec le perp)" if source == "coinbase" else ""
            self.notify(f"ℹ️ Source de prix : bascule sur {SOURCES[source][2]}{note}.")
        self.state["source"] = source
        self._maybe_contract_size(source)

        for c in candles:
            self.candles[c.t] = c
        if len(self.candles) > KEEP_CANDLES:
            for t in sorted(self.candles)[:-KEEP_CANDLES]:
                del self.candles[t]

        live = self.candles[max(self.candles)]
        self.touch_alerts(live)

        closed = [self.candles[t] for t in sorted(self.candles) if self.candles[t].is_closed(now)]
        if not closed:
            self.save()
            return True
        last_t = self.state["last_t"]
        if last_t is None:
            start = now - self.cfg["replay_hours"] * 3_600_000
            todo = [c for c in closed if c.t >= start]
        else:
            todo = [c for c in closed if c.t > last_t]

        stale = self.levels_stale(now)
        if stale and not self.state["stale_notified"]:
            self.state["stale_notified"] = True
            self.notify("⏰ Les niveaux de config.toml sont périmés (valid_until dépassé). "
                        "Je n'émets plus de signaux : mets à jour les niveaux avec une heatmap récente.")

        for c in todo:
            silent = (now - (c.t + M15)) > self.cfg["max_alert_age_min"] * 60_000
            self._update_trades(c, silent)
            if not stale:
                for key, sc in self.cfg["scenarios"].items():
                    if sc.get("enabled", True):
                        self._eval(key, sc, c, silent, now)
        self.state["last_t"] = closed[-1].t
        self.state["last_price"] = live.c

        if not self._announced:
            self._announced = True
            self.notify("🤖 <b>Bot démarré</b>\n" + self.status_text())
        self.save()
        return True

    # ── alertes de niveau ──
    def touch_alerts(self, live):
        ta = self.cfg["touch_alerts"]
        if not ta.get("enabled"):
            return
        st = self.state["touch"]
        price = live.c
        for lvl in ta["levels"]:
            k = str(lvl)
            s = st.setdefault(k, {"armed": True, "candle": None})
            if s["armed"] and live.l <= lvl <= live.h and s["candle"] != live.t:
                s.update(armed=False, candle=live.t)
                if self.state["last_price"] is not None:   # pas d'alerte au tout premier passage
                    self.notify(f"🔔 Niveau {fp(lvl)} touché (prix {fp(price)}).")
            elif not s["armed"] and live.t != s["candle"] and abs(price - lvl) / lvl * 100 >= ta["rearm_pct"]:
                s["armed"] = True

    # ── scénarios ──
    def _eval(self, key, sc, c, silent, now):
        st = self.state["scenarios"][key]
        closes = self.closes_for(c)
        if sc["type"] == "sweep_reversal":
            self._eval_sweep(key, sc, st, c, closes, silent, now)
        else:
            self._eval_breakout(key, sc, st, c, closes, silent, now)

    def _expired(self, sc, st, c):
        return c.t + M15 - st["armed_t"] >= sc["expire_hours"] * 3_600_000

    def _eval_sweep(self, key, sc, st, c, closes, silent, now):
        long = sc["direction"] == "long"
        lvl = sc["sweep_level"]
        if st["status"] == "idle":
            if (c.l <= lvl) if long else (c.h >= lvl):
                st.update(status="armed", armed_t=c.t, extreme=c.l if long else c.h)
                side = "sous" if long else "au-dessus de"
                cmp_ = GT if long else LT
                cancel = ""
                if sc.get("cancel_tf"):
                    cancel = (f"\nAnnulé si clôture {sc['cancel_tf']} "
                              f"{LT if long else GT} {fp(sc['cancel_close'])}.")
                self.notify(f"🟡 <b>{self.label(key)}</b> armé : mèche à {fp(st['extreme'])} "
                            f"{side} {fp(lvl)}.\nJ'attends une clôture {sc['trigger_tf']} "
                            f"{cmp_} {fp(sc['trigger_close'])}.{cancel}", silent)
        if st["status"] != "armed":
            return
        st["extreme"] = min(st["extreme"], c.l) if long else max(st["extreme"], c.h)

        ctf = sc.get("cancel_tf")
        if ctf and ctf in closes:
            cc = closes[ctf].c
            if (cc < sc["cancel_close"]) if long else (cc > sc["cancel_close"]):
                st["status"] = "cancelled"
                self.notify(f"❌ <b>{self.label(key)}</b> annulé : clôture {ctf} à {fp(cc)} "
                            f"{'sous' if long else 'au-dessus de'} {fp(sc['cancel_close'])}.", silent)
                return

        ttf = sc["trigger_tf"]
        if ttf in closes:
            tc = closes[ttf].c
            if (tc > sc["trigger_close"]) if long else (tc < sc["trigger_close"]):
                sl = sc["sl"]
                buf = sc.get("sl_buffer", 0)
                if buf:
                    sl = min(sl, st["extreme"] - buf) if long else max(sl, st["extreme"] + buf)
                detail = (f"mèche à {fp(st['extreme'])}, puis clôture {ttf} à {fp(tc)} "
                          f"{'au-dessus de' if long else 'sous'} {fp(sc['trigger_close'])}")
                self._fire(key, sc, st, tc, sl, c, detail, silent, now)
                return

        if self._expired(sc, st, c):
            st["status"] = "expired"
            self.notify(f"⌛ <b>{self.label(key)}</b> expiré : pas de déclenchement en "
                        f"{sc['expire_hours']} h après la mèche.", silent)

    def _eval_breakout(self, key, sc, st, c, closes, silent, now):
        long = sc["direction"] == "long"
        btf = sc["breakout_tf"]
        if st["status"] == "idle":
            if btf in closes:
                bc = closes[btf].c
                if (bc > sc["breakout_close"]) if long else (bc < sc["breakout_close"]):
                    st.update(status="armed", armed_t=c.t, touched=False, retest_extreme=None)
                    self.notify(f"🟡 <b>{self.label(key)}</b> armé : clôture {btf} à {fp(bc)} "
                                f"{'au-dessus de' if long else 'sous'} {fp(sc['breakout_close'])}.\n"
                                f"J'attends un retest de {fp(sc['retest_touch'])} qui tient "
                                f"(clôture 15m {'≥' if long else '≤'} {fp(sc['retest_hold_close'])}). "
                                f"Ne chasse pas la bougie.", silent)
            return
        if st["status"] != "armed" or c.t <= st["armed_t"]:
            return
        if (c.c < sc["fail_close"]) if long else (c.c > sc["fail_close"]):
            st["status"] = "cancelled"
            self.notify(f"❌ <b>{self.label(key)}</b> annulé : cassure ratée, clôture 15m à {fp(c.c)} "
                        f"{'sous' if long else 'au-dessus de'} {fp(sc['fail_close'])}.", silent)
            return
        touch = (c.l <= sc["retest_touch"]) if long else (c.h >= sc["retest_touch"])
        if touch:
            st["touched"] = True
            ext = c.l if long else c.h
            prev = st.get("retest_extreme")
            st["retest_extreme"] = ext if prev is None else (min(prev, ext) if long else max(prev, ext))
        if st["touched"] and ((c.c >= sc["retest_hold_close"]) if long else (c.c <= sc["retest_hold_close"])):
            detail = (f"cassure validée, retest à {fp(st['retest_extreme'])} tenu, "
                      f"clôture 15m à {fp(c.c)}")
            self._fire(key, sc, st, c.c, sc["sl"], c, detail, silent, now)
            return
        if self._expired(sc, st, c):
            st["status"] = "expired"
            self.notify(f"⌛ <b>{self.label(key)}</b> expiré : pas de retest en {sc['expire_hours']} h. "
                        f"Ne chasse pas.", silent)

    def _fire(self, key, sc, st, entry, sl, c, detail, silent, now):
        lo, hi = sc["entry_zone"]
        st["fired_t"] = c.t
        when = self.fmt_time(c.t + M15)
        if not (lo <= entry <= hi):
            st["status"] = "missed"
            self.notify(f"⚪️ <b>{self.label(key)}</b> déclenché à {when}, mais le prix ({fp(entry)}) est "
                        f"hors zone d'entrée ({fp(lo)} – {fp(hi)}).\nTu ne prends pas, tu ne chasses pas.",
                        silent)
            return
        acc = self.cfg["account"]
        trade = build_trade(sc["direction"], entry, sl, sc["tps"], sc["risk_usd"], acc)
        if "error" in trade:
            st["status"] = "skipped"
            self.notify(f"⚪️ <b>{self.label(key)}</b> déclenché, pas de trade : {esc(trade['error'])}.", silent)
            return
        if trade["final_r"] < acc["min_final_r"]:
            st["status"] = "skipped"
            self.notify(f"⚪️ <b>{self.label(key)}</b> déclenché, mais R:R insuffisant : dernier TP à "
                        f"{fnum(trade['final_r'], 1)}R (minimum {fnum(acc['min_final_r'], 1)}R). "
                        f"SL trop loin ({fp(sl)}). Pas de trade.", silent)
            return

        st["status"] = "triggered"
        st["entry"] = entry
        missed = silent
        self.state["open_trades"].append({
            "id": f"{key}-{c.t}", "scenario": key, "direction": sc["direction"],
            "entry": entry, "sl": sl, "sl_cur": sl, "risk_pts": trade["risk_pts"],
            "risk_usd": trade["risk"], "opened_t": c.t, "r": 0.0, "remaining": 100.0,
            "legs": [{"price": l["price"], "pct": l["pct"], "hit": False} for l in trade["legs"]],
            "events": [], "missed": missed,
        })
        if missed:
            self.notify(f"⚪️ <b>{self.label(key)}</b> déclenché à {when} pendant une coupure du bot : "
                        f"trop tard pour entrer. Suivi virtuel quand même.", silent)
        elif self.state["paused"]:
            self.notify(f"⏸ <b>{self.label(key)}</b> déclenché à {when} (bot en pause : détails non envoyés, "
                        f"suivi virtuel actif).")
        else:
            self.notify(self.trade_message(key, sc, trade, detail, when))

    # ── message de trade ──
    def trade_message(self, key, sc, t, detail, when, test=False):
        long = t["direction"] == "long"
        lo, hi = sc["entry_zone"]
        head = ("🧪 TEST — " if test else "") + ("🟢 LONG" if long else "🔴 SHORT")
        limit = sc.get("entry_limit")
        limit_txt = ""
        if limit and lo <= limit <= hi and ((limit < t["entry"]) if long else (limit > t["entry"])):
            limit_txt = f" ou limite à {fp(limit)} (même taille, risque un peu plus faible)"
        contracts = ""
        if self.contract_size and self.state.get("source") == "mexc":
            contracts = f" · ≈ {fnum(t['qty'] / self.contract_size)} contrats MEXC"
        lines = [
            f"{head} BTCUSDT — <b>{self.label(key)}</b>",
            f"Déclencheur : {detail} ({when}).",
            "",
            f"▶️ <b>Entrée</b> : au marché ≈ {fp(t['entry'])}{limit_txt}",
            f"Zone valide : {fp(lo)} – {fp(hi)}. Prix sorti de la zone → tu ne prends pas.",
            f"⚙️ <b>Levier</b> : x{t['lev']} isolé · marge ≈ {fusd(t['margin'])}",
            f"📦 <b>Taille</b> : {fbtc(t['qty'])} · {fusd(t['notional'])} notionnels{contracts}",
            "",
            f"🛑 <b>SL</b> : {fp(t['sl'])} ({fpct(t['sl_pct'])}) → {fsusd(-t['risk'])} "
            f"(+ frais ≈ {fusd(t['fees'])})",
        ]
        for i, l in enumerate(t["legs"], 1):
            lines.append(f"🎯 <b>TP{i}</b> : {fp(l['price'])} ({fpct(l['move_pct'])}) · "
                         f"{fnum(l['pct'])} % ({fbtc(l['qty'])}) · {fsusd(l['usd'])} · {fnum(l['r'], 1)}R")
        if self.cfg["account"]["move_sl_to_be_after_tp1"] and len(t["legs"]) > 1:
            lines.append(f"➡️ Après TP1 : SL remonté à l'entrée ({fp(t['entry'])}).")
        lines += [
            "",
            f"☠️ Liquidation estimée ≈ {fp(t['liq'])} ({fnum(t['liq_ratio'], 1)}× la distance du SL)",
            f"Tous les TP touchés : {fsusd(t['total_usd'])} (≈ {fnum(t['avg_r'], 1)}R)",
        ]
        if t["reduced"]:
            lines.append(f"⚠️ Taille réduite pour garder la liquidation loin du SL : risque réel "
                         f"{fusd(t['risk'])} au lieu de {fusd(t['risk_wanted'])}.")
        if self.state.get("source") == "coinbase":
            lines.append("⚠️ Source : Coinbase spot, écart possible avec le perp.")
        lines.append("Pose le SL avec l'ordre. Un seul trade à la fois.")
        return "\n".join(lines)

    # ── suivi virtuel ──
    def _update_trades(self, c, silent):
        acc = self.cfg["account"]
        for vt in list(self.state["open_trades"]):
            if c.t <= vt["opened_t"]:
                continue
            sign = 1 if vt["direction"] == "long" else -1
            lbl = self.label(vt["scenario"])
            adverse = c.l if sign == 1 else c.h
            if (adverse - vt["sl_cur"]) * sign <= 0:      # SL d'abord (hypothèse prudente)
                vt["r"] += vt["remaining"] / 100 * (vt["sl_cur"] - vt["entry"]) * sign / vt["risk_pts"]
                vt["events"].append("SL à l'entrée" if vt["sl_cur"] == vt["entry"] else "SL")
                vt["remaining"] = 0
                self._close_trade(vt, c, silent)
                continue
            fav = c.h if sign == 1 else c.l
            for i, leg in enumerate(vt["legs"]):
                if leg["hit"]:
                    continue
                if (fav - leg["price"]) * sign < 0:
                    break
                leg["hit"] = True
                vt["r"] += leg["pct"] / 100 * (leg["price"] - vt["entry"]) * sign / vt["risk_pts"]
                vt["remaining"] -= leg["pct"]
                vt["events"].append(f"TP{i + 1}")
                be = ""
                if i == 0 and acc["move_sl_to_be_after_tp1"] and vt["remaining"] > 1e-6:
                    vt["sl_cur"] = vt["entry"]
                    be = f" Remonte le SL à l'entrée ({fp(vt['entry'])})."
                if vt["remaining"] > 1e-6:
                    self.notify(f"📈 <b>{lbl}</b> : TP{i + 1} touché à {fp(leg['price'])}. "
                                f"Cumul {fsr(vt['r'])}.{be}", silent or vt["missed"])
            if vt["remaining"] <= 1e-6:
                self._close_trade(vt, c, silent)
                continue
            if c.t + M15 - vt["opened_t"] >= acc["virtual_trade_max_hours"] * 3_600_000:
                vt["r"] += vt["remaining"] / 100 * (c.c - vt["entry"]) * sign / vt["risk_pts"]
                vt["events"].append(f"clôturé à {fp(c.c)} (durée max)")
                vt["remaining"] = 0
                self._close_trade(vt, c, silent)

    def _close_trade(self, vt, c, silent):
        self.state["open_trades"].remove(vt)
        usd = vt["r"] * vt["risk_usd"]
        rec = {"id": vt["id"], "scenario": vt["scenario"], "direction": vt["direction"],
               "opened_t": vt["opened_t"], "closed_t": c.t + M15, "entry": vt["entry"],
               "sl": vt["sl"], "events": vt["events"], "r": round(vt["r"], 3),
               "usd": round(usd, 2), "missed": vt["missed"]}
        self.state["history"].append(rec)
        new = not os.path.exists(self.csv_path)
        with open(self.csv_path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ouverture", "cloture", "scenario", "sens", "entree", "sl",
                            "evenements", "R", "usd", "manque"])
            w.writerow([self.fmt_time(rec["opened_t"] + M15, True), self.fmt_time(rec["closed_t"], True),
                        rec["scenario"], rec["direction"], rec["entry"], rec["sl"],
                        " → ".join(rec["events"]), rec["r"], rec["usd"], int(rec["missed"])])
        icon = "🏁" if vt["r"] > 0 else ("➖" if abs(vt["r"]) < 1e-9 else "🛑")
        self.notify(f"{icon} <b>{self.label(vt['scenario'])}</b> clôturé (suivi virtuel) : "
                    f"{' → '.join(vt['events'])} = {fsr(vt['r'])} ≈ {fsusd(usd)}.\n/stats pour le bilan.",
                    silent or vt["missed"])

    # ── textes ──
    def describe(self, key, sc, st):
        s = st["status"]
        long = sc["direction"] == "long"
        if s == "idle":
            if sc["type"] == "sweep_reversal":
                return f"en attente d'une mèche {'≤' if long else '≥'} {fp(sc['sweep_level'])}"
            return f"en attente d'une clôture {sc['breakout_tf']} {GT if long else LT} {fp(sc['breakout_close'])}"
        if s == "armed":
            if sc["type"] == "sweep_reversal":
                return (f"ARMÉ (mèche {fp(st['extreme'])}) → attend clôture {sc['trigger_tf']} "
                        f"{GT if long else LT} {fp(sc['trigger_close'])}")
            txt = f"ARMÉ (cassure) → attend retest {fp(sc['retest_touch'])} tenu"
            return txt + (" · retest touché" if st.get("touched") else "")
        return f"terminé : {STATUS_TXT.get(s, s)} — /reset {key} pour réarmer"

    def status_text(self):
        now = self.clock()
        src = SOURCES.get(self.state.get("source") or "", (None, None, "?"))[2]
        price = self.state.get("last_price")
        lines = [f"Prix : {fp(price) if price else '?'} ({src})"]
        vu = self.cfg.get("valid_until_ms")
        if vu:
            lines.append(("⏰ Niveaux PÉRIMÉS depuis " if now > vu else "Niveaux valables jusqu'au ")
                         + self.fmt_time(vu, True))
        lines.append("")
        for key, sc in self.cfg["scenarios"].items():
            st = self.state["scenarios"][key]
            sens = "long" if sc["direction"] == "long" else "short"
            off = " (désactivé)" if not sc.get("enabled", True) else ""
            lines.append(f"{STATUS_ICON.get(st['status'], '•')} <b>{self.label(key)}</b> ({sens}){off} : "
                         f"{self.describe(key, sc, st)}")
        ot = self.state["open_trades"]
        lines.append("")
        lines.append(f"Trades virtuels ouverts : {len(ot)}")
        for vt in ot:
            left = [fp(l["price"]) for l in vt["legs"] if not l["hit"]]
            lines.append(f" · {vt['scenario']} {vt['direction']} @ {fp(vt['entry'])} · SL {fp(vt['sl_cur'])} "
                         f"· TP restants {', '.join(left)} · réalisé {fsr(vt['r'])}")
        if self.state["paused"]:
            lines.append("⏸ Bot en pause (/resume pour réactiver les détails de trade).")
        return "\n".join(lines)

    def stats_text(self):
        hist = self.state["history"]
        if not hist:
            return "📊 Aucun signal clôturé pour l'instant."
        def block(rows):
            n = len(rows)
            wins = sum(1 for r in rows if r["r"] > 0)
            tot = sum(r["r"] for r in rows)
            return f"{n} signaux · {wins} gagnants ({fnum(wins / n * 100)} %) · R moyen {fsr(tot / n)} · cumul {fsr(tot)}"
        lines = ["📊 <b>Bilan des signaux</b> (suivi virtuel, hors frais)", "Total : " + block(hist)]
        for key in sorted({r["scenario"] for r in hist}):
            lines.append(f"{key} : " + block([r for r in hist if r["scenario"] == key]))
        if len(hist) < 30:
            lines.append(f"\nÉchantillon trop petit pour conclure ({len(hist)}/30 minimum).")
        return "\n".join(lines)

    def send_test(self):
        key = next(iter(self.cfg["scenarios"]), None)
        if not key:
            self.tg.send("🧪 Aucun scénario dans config.toml.")
            return
        sc = self.cfg["scenarios"][key]
        lo, hi = sc["entry_zone"]
        entry = sc.get("entry_limit") or (lo + hi) / 2
        t = build_trade(sc["direction"], entry, sc["sl"], sc["tps"], sc["risk_usd"], self.cfg["account"])
        if "error" in t:
            self.tg.send(f"🧪 Test impossible : {esc(t['error'])}")
            return
        self.tg.send(self.trade_message(key, sc, t, "exemple de message, aucun signal réel",
                                        self.fmt_time(self.clock()), test=True))

    # ── commandes Telegram ──
    HELP = ("<b>Commandes</b>\n/status — état des scénarios\n/stats — bilan des signaux\n"
            "/pause — n'envoie plus les détails de trade (tu es déjà en position)\n/resume — réactive\n"
            "/reset — réarme tous les scénarios · /reset A — un seul\n/test — message de trade d'exemple\n"
            "/help — cette aide")

    def handle_commands(self):
        if not self.tg.can_receive:
            return
        try:
            updates = self.tg.get_updates(self.state.get("tg_offset"))
        except Exception as e:
            log(f"Telegram getUpdates : {e}")
            return
        for u in updates:
            self.state["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id", "")) != self.tg.chat_id:
                continue
            parts = (msg.get("text") or "").strip().split()
            if not parts:
                continue
            cmd = parts[0].split("@")[0].lower()
            arg = parts[1].upper() if len(parts) > 1 else None
            if cmd == "/status":
                self.tg.send("📋 <b>État</b>\n" + self.status_text())
            elif cmd == "/stats":
                self.tg.send(self.stats_text())
            elif cmd == "/pause":
                self.state["paused"] = True
                self.tg.send("⏸ Pause : je continue à surveiller et à suivre les signaux, "
                             "sans t'envoyer les détails de trade.")
            elif cmd == "/resume":
                self.state["paused"] = False
                self.tg.send("▶️ Reprise des alertes de trade.")
            elif cmd == "/reset":
                keys = [arg] if arg else list(self.cfg["scenarios"])
                done = []
                for k in keys:
                    if k in self.cfg["scenarios"]:
                        self.state["scenarios"][k] = {"status": "idle",
                                                      "hash": scenario_hash(self.cfg["scenarios"][k])}
                        done.append(k)
                self.tg.send(f"🔁 Réarmé : {', '.join(done) if done else 'aucun (clé inconnue)'}.")
            elif cmd == "/test":
                self.send_test()
            elif cmd in ("/help", "/start"):
                self.tg.send(self.HELP)
        if updates:
            self.save()

    def run(self):
        log(f"démarrage — config {self.cfg_path}, données {self.data_dir}")
        last = 0.0
        while True:
            try:
                if time.time() - last >= self.cfg["poll_seconds"]:
                    last = time.time()
                    self.tick()
                self.handle_commands()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"erreur : {e}\n{traceback.format_exc()}")
                time.sleep(10)
            time.sleep(3)


# ───────────────────────────── CLI ─────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Alertes Telegram de setups BTC (aucune exécution automatique).")
    ap.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.toml"))
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--dry-run", action="store_true", help="affiche les messages au lieu de les envoyer")
    ap.add_argument("--once", action="store_true", help="un seul passage puis sortie (cron)")
    ap.add_argument("--test-message", action="store_true", help="envoie un message de trade d'exemple")
    ap.add_argument("--get-chat-id", action="store_true", help="affiche les chat_id qui ont écrit au bot")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if args.get_chat_id:
        if not token:
            sys.exit("Définis TELEGRAM_BOT_TOKEN, envoie /start à ton bot, puis relance.")
        tg = Telegram(token, "")
        ups = tg.get_updates(None)
        if not ups:
            print("Aucun message reçu. Envoie /start à ton bot dans Telegram, puis relance.")
        seen = set()
        for u in ups:
            ch = (u.get("message") or {}).get("chat", {})
            if ch.get("id") and ch["id"] not in seen:
                seen.add(ch["id"])
                print(f"chat_id = {ch['id']}  ({ch.get('first_name') or ch.get('title') or ''})")
        return

    dry = args.dry_run or not (token and chat_id)
    if dry and not args.dry_run:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents → mode dry-run (messages affichés ici)")
    eng = Engine(args.config, args.data_dir, Telegram(token, chat_id, dry_run=dry))

    if args.test_message:
        eng.send_test()
        return
    if args.once:
        eng.tick()
        eng.handle_commands()
        eng.save()
        return
    eng.run()


if __name__ == "__main__":
    main()
