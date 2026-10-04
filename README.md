# BTC Alert Bot

Le bot surveille le BTC et t'envoie un message Telegram quand un de tes scénarios est validé. Le message contient le trade exact :

- le sens et la zone d'entrée valide ;
- le levier, la marge et la taille (en BTC, en notionnel et en contrats MEXC) ;
- le SL ;
- les TP, chacun avec son prix et son % de la position ;
- la liquidation estimée.

Il suit aussi chaque signal en **trade virtuel** et publie son résultat en R. La commande `/stats` dit ainsi, au bout de 30 signaux ou plus, si les setups ont vraiment un avantage.

Il **n'exécute rien** : il n'a pas accès à ton compte. C'est toi qui passes les ordres.

Fichiers :

| Fichier | Rôle |
|---|---|
| `btc_alert_bot.py` | le bot (Python 3.11+, aucune dépendance) |
| `config.toml` | tes niveaux, ton risque, tes TP — **le seul fichier à modifier** |
| `test_scenarios.py` | simulation hors ligne des scénarios A, B et C |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | hébergement |

---

## 1. Créer le bot Telegram (5 min)

1. Dans Telegram, ouvre **@BotFather**, envoie `/newbot` et choisis un nom. Il te donne un **token** (`123456:ABC...`).
2. Ouvre la conversation avec ton nouveau bot et envoie-lui `/start`.
3. Récupère ton `chat_id` :
   ```bash
   TELEGRAM_BOT_TOKEN="123456:ABC..." python3 btc_alert_bot.py --get-chat-id
   ```

## 2. Tester sur ta machine

```bash
python3 test_scenarios.py                 # simule A, B, C et affiche les messages que tu recevrais
python3 btc_alert_bot.py --dry-run --once # vérifie que les prix arrivent depuis ta machine
```

Puis, avec tes identifiants :

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"
python3 btc_alert_bot.py --test-message   # reçoit un message de trade d'exemple sur Telegram
python3 btc_alert_bot.py                  # lance le bot en continu (Ctrl+C pour arrêter)
```

## 3. Héberger 24 h/24

Le bot doit tourner en permanence : un téléphone ou un PC qui se met en veille ne suffit pas.

**Option recommandée : un petit VPS en Europe avec Docker** (quelques euros par mois chez Hetzner, OVH, Scaleway, etc.). Héberge-le en Europe : Binance et Bybit bloquent les IP américaines. MEXC reste la source principale, mais tu perdrais les secours.

```bash
# depuis ton ordinateur
scp -r btc-alert-bot utilisateur@ip-du-vps:~

# sur le VPS (Docker installé)
cd ~/btc-alert-bot
cp .env.example .env && nano .env       # colle le token et le chat_id
docker compose up -d --build
docker compose logs -f                  # tu dois voir « Bot démarré » et le recevoir sur Telegram
```

Ensuite :

- **mettre à jour les niveaux** : `nano config.toml`. Le bot recharge le fichier tout seul et te confirme sur Telegram ;
- **voir les logs** : `docker compose logs -f` ;
- **arrêter** : `docker compose down`.

**Sans Docker** (Raspberry Pi, serveur Linux) : copie le dossier dans `/opt/btc-alert-bot`, puis crée `/etc/systemd/system/btc-alert-bot.service` :

```ini
[Unit]
Description=BTC Alert Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/btc-alert-bot
EnvironmentFile=/opt/btc-alert-bot/.env
ExecStart=/usr/bin/python3 btc_alert_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now btc-alert-bot
journalctl -u btc-alert-bot -f
```

## 4. Mettre à jour les niveaux (chaque jour)

Les niveaux viennent d'une heatmap et **se périment vite**. Passé `valid_until`, le bot n'émet plus de signal et te prévient.

Avec une nouvelle heatmap :

1. Modifie les niveaux des scénarios dans `config.toml`.
2. Repousse `valid_until`.
3. Pour ajouter un scénario, copie un bloc `[scenarios.X]` et change la lettre.

Tout scénario dont tu modifies un paramètre est réarmé automatiquement.

Il existe deux types de scénarios :

- `sweep_reversal` : une mèche au-delà d'un niveau (le balayage), puis une clôture qui revient de l'autre côté. On trade dans le sens du retour. Ce sont les scénarios A et C.
- `breakout_retest` : une clôture au-delà d'un niveau, puis un retest qui tient. On trade dans le sens de la cassure. C'est le scénario B.

Chaque paramètre est commenté dans `config.toml`.

## 5. Commandes Telegram

| Commande | Effet |
|---|---|
| `/status` | état de chaque scénario et trades virtuels en cours |
| `/stats` | bilan des signaux : taux de réussite, R moyen, R cumulé |
| `/pause` · `/resume` | coupe ou réactive les détails de trade (quand tu es déjà en position) |
| `/reset` · `/reset A` | réarme tous les scénarios, ou un seul |
| `/test` | envoie un message de trade d'exemple |

Le bot ne répond qu'à ton `chat_id`.

## Comment le trade est calculé

- **Taille** = risque en $ ÷ distance au SL.
- **Levier** : c'est le plus bas qui fait tenir la marge dans 95 % du budget. Il est plafonné pour que la liquidation reste au moins 2,5 fois plus loin que le SL. Si c'est impossible, la taille est réduite et le message le signale.
- **SL des balayages** : il est placé au-delà de la mèche réelle (mèche + 150 $). Si cela rend le R:R insuffisant (dernier TP sous 1,5R), le bot dit « pas de trade ».
- **Entrée** : si le prix a quitté la zone d'entrée au moment du déclenchement, le bot t'envoie « hors zone, tu ne chasses pas » au lieu d'un trade.
- **Retard** : un déclenchement vieux de plus de 20 min (bot coupé) n'est pas envoyé comme trade, mais il est suivi virtuellement.

## Limites, à lire

- **Le bot applique des règles, il ne les rend pas rentables.** Ces trois setups n'ont jamais été backtestés. Le suivi virtuel est là pour le mesurer : ne conclus rien avant une trentaine de signaux.
- **Les alertes arrivent à la clôture** de la bougie 15 min ou 1 h, avec au plus ~30 s de délai.
- **La liquidation affichée est une estimation** (marge de maintenance 0,4 %, sans les frais). C'est la plateforme qui fait foi.
- **Le suivi virtuel est prudent** : si le SL et un TP sont touchés dans la même bougie, il compte le SL. Il ne compte pas les frais.
- **La heatmap vient de Binance alors que tu trades sur MEXC** : les prix collent à quelques dollars près. La source Coinbase (dernier secours) est du spot, et le bot le signale quand il l'utilise.
