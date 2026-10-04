#!/usr/bin/env python3
"""Simulation hors ligne des scénarios avec des bougies synthétiques (aucun réseau, aucun envoi).

    python3 test_scenarios.py          → affiche les messages que tu recevrais
    python3 test_scenarios.py -q       → n'affiche que le résultat des vérifications
"""
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_alert_bot as bot  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.toml")
QUIET = "-q" in sys.argv
T0 = int(datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 08:00 à Paris


ALL_SENT = []


def telegram_html_ok(text):
    """Telegram (parse_mode HTML) refuse tout < > & qui n'est ni une balise autorisée ni une entité."""
    body = text.replace("<b>", "").replace("</b>", "")
    if "<" in body or ">" in body:
        return False
    return all(re.match(r"&(lt|gt|amp|quot);", body[i:]) for i in range(len(body)) if body[i] == "&")


class QuietTelegram(bot.Telegram):
    def send(self, text):
        self.sent.append(text)
        ALL_SENT.append(text)
        if not QUIET:
            print("\n" + "─" * 44 + "\n" + bot.strip_html(text) + "\n" + "─" * 44)


def run(title, points, warmup=8):
    """points : liste de (open, high, low, close) en bougies 15 min consécutives depuis T0."""
    if not QUIET:
        print(f"\n\n████████ {title} ████████")
    tmp = tempfile.mkdtemp()
    candles = [bot.Candle(T0 + i * bot.M15, *p) for i, p in enumerate(points)]
    view = {"n": warmup, "now": 0}
    eng = bot.Engine(CONFIG, tmp, QuietTelegram("", "", dry_run=True),
                     fetch=lambda: (candles[:view["n"]], "mexc"),
                     clock=lambda: view["now"])
    eng.cfg["touch_alerts"]["enabled"] = False   # on teste les scénarios, pas les alertes de niveau
    for n in range(warmup, len(candles) + 1):
        view["n"] = n
        view["now"] = candles[n - 1].t + bot.M15 + 11_000
        eng.tick()
    shutil.rmtree(tmp)
    return eng


def flat(price, n, spread=50):
    return [(price, price + spread, price - spread, price)] * n


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))


# ── A : balayage de 85 250 qui échoue → short, TP1 puis TP2 ──
eng = run("A — balayage 85 250 échoué (short)", flat(84980, 8) + [
    (84990, 85120, 84960, 85080),
    (85080, 85320, 85050, 85180),   # mèche 85 320 → A armé
    (85180, 85200, 85020, 85060),   # clôture 85 060 < 85 100 → SHORT
    (85060, 85080, 84700, 84750),
    (84750, 84780, 84450, 84520),   # TP1 84 500
    (84520, 84560, 84100, 84150),
    (84150, 84200, 83650, 83720),   # TP2 83 700 (et C s'arme sur la mèche 83 650)
])
hist = eng.state["history"]
check("A déclenché", eng.state["scenarios"]["A"]["status"] == "triggered")
check("A clôturé TP1 + TP2 (≈ +2R)", hist and hist[0]["events"] == ["TP1", "TP2"] and 1.8 < hist[0]["r"] < 2.2)
check("C armé par la mèche 83 650", eng.state["scenarios"]["C"]["status"] == "armed")

# ── B : cassure 85 400, retest tenu → long, TP1 puis SL à l'entrée ──
eng = run("B — cassure 85 400 + retest (long)", flat(84980, 8) + [
    (84980, 85300, 84950, 85280),   # mèche 85 300 → A armé
    (85280, 85480, 85250, 85450),
    (85450, 85520, 85400, 85480),
    (85480, 85560, 85430, 85500),   # clôture 1h 85 500 > 85 400 → B armé, A annulé
    (85500, 85520, 85330, 85360),   # retest 85 330 tenu, clôture 85 360 → LONG
    (85360, 85900, 85340, 85850),
    (85850, 86250, 85800, 86200),   # TP1 86 200 → SL à l'entrée
    (86200, 86500, 86000, 86100),
    (86100, 86150, 85300, 85350),   # SL à l'entrée touché
])
hist = eng.state["history"]
check("A annulé par l'acceptation au-dessus de 85 400", eng.state["scenarios"]["A"]["status"] == "cancelled")
check("B déclenché", eng.state["scenarios"]["B"]["status"] == "triggered")
check("B clôturé TP1 puis SL à l'entrée (> 0R)", hist and hist[0]["events"] == ["TP1", "SL à l'entrée"] and hist[0]["r"] > 0)

# ── C : flush dans le bloc 83 600, reprise de 84 000 → long ──
eng = run("C — balayage 83 600 repris (long)", flat(84500, 8) + [
    (84500, 84520, 84000, 84050),
    (84050, 84080, 83560, 83620),   # mèche 83 560 → C armé
    (83620, 83900, 83580, 83850),
    (83850, 84150, 83800, 84080),   # clôture 1h 84 080 > 84 000 → LONG
    (84080, 84600, 84050, 84550),
    (84550, 85200, 84500, 85100),   # TP1 85 150
])
check("C déclenché", eng.state["scenarios"]["C"]["status"] == "triggered")
check("C : TP1 touché, trade toujours suivi", eng.state["open_trades"] and eng.state["open_trades"][0]["legs"][0]["hit"])

# ── C trop tard : reprise violente, clôture 1h hors zone → pas de trade ──
eng = run("C — reprise trop violente (hors zone)", flat(84500, 8) + [
    (84500, 84520, 84000, 84050),
    (84050, 84080, 83560, 83620),
    (83620, 84200, 83580, 84150),
    (84150, 84650, 84100, 84600),   # clôture 1h 84 600 > 84 400 → hors zone
])
check("C hors zone → manqué, aucun trade", eng.state["scenarios"]["C"]["status"] == "missed" and not eng.state["open_trades"])

# ── Sizing : vérification des chiffres du plan ──
acc = bot.DEFAULTS["account"]
t = bot.build_trade("long", 84050, 83150, [[85150, 40], [86200, 35], [87050, 25]], 600, acc)
check("C : 0,666 BTC, x30, liquidation sous le SL", abs(t["qty"] - 0.666) < 1e-9 and t["lev"] == 30 and t["liq"] < 83150)
t = bot.build_trade("short", 85060, 85550, [[84500, 50], [83700, 50]], 450, acc)
check("A : risque ≤ 450 $, liquidation ≥ 2,5× le SL", t["risk"] <= 450 and t["liq_ratio"] >= 2.5)

# ── Textes de commandes + format HTML de tous les messages ──
ALL_SENT.append(eng.status_text())
ALL_SENT.append(eng.stats_text())
eng.send_test()
ALL_SENT.append(bot.Engine.HELP)
bad = [m for m in ALL_SENT if not telegram_html_ok(m)]
check(f"{len(ALL_SENT)} messages au format HTML Telegram valide", not bad)
for m in bad:
    print("HTML INVALIDE :", m[:200])

print("\n\nVérifications :")
for name, ok in checks:
    print(f"  {'OK ' if ok else 'ÉCHEC'}  {name}")
failed = [n for n, ok in checks if not ok]
sys.exit(1 if failed else 0)
