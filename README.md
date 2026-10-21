# Polymarket → Kalshi Whale Copy Bot

Watches Polymarket wallets you choose. When one trades a market you've mapped to a
Kalshi equivalent, it mirrors the trade on Kalshi. Runs on Render with a
password-protected dashboard. **Starts in paper mode (no real orders).**

## What's here
| File | What it does |
|---|---|
| `app.py` | Web app: runs the bot in the background and serves the dashboard |
| `copybot.py` | The bot: watches whales, applies risk limits, places Kalshi orders |
| `config.yaml` | Whales to follow + risk limits |
| `mappings.yaml` | Which Polymarket market = which Kalshi ticker |
| `find_matches.py` | Suggests Kalshi matches for a whale's positions (run on your computer) |
| `render.yaml` | Render setup (one service + 1 GB disk) |

## 1. Put it on GitHub
1. Go to github.com → **New repository** → name it `pm-kalshi-copybot` → **Private** → Create.
2. Click **uploading an existing file**, drag in everything from the zip (keep `.gitignore`), and **Commit**.

## 2. Deploy on Render
1. render.com → **New** → **Blueprint** → connect GitHub → pick the repo.
2. Render reads `render.yaml` and creates the service. Click **Apply**.
   It runs on the Starter plan (~$7/mo) plus a 1 GB disk (~$0.25/mo).
   The free plan won't work: it sleeps when idle and can't have a disk.
3. When it's live, open the service → **Environment** → copy `DASHBOARD_PASSWORD`.
4. Open the service URL. Log in with any username and that password.

## 3. Add whales and markets (in GitHub, works from a phone)
- Edit `config.yaml` → `whales:`: add the wallet addresses to follow.
- Edit `mappings.yaml`: add each Polymarket market → Kalshi ticker you've verified.
- Commit. Render redeploys automatically in a couple of minutes.

Use `find_matches.py` on a computer to get mapping suggestions:
```bash
pip install -r requirements.txt
python find_matches.py 0xWHALEWALLET --min-usd 1000
```
**Check each match by hand.** Similar-looking markets often resolve on different dates or rules.

## 4. Going live (do these in order)
1. **Paper first.** Leave `BOT_MODE=paper` for a week or two and review the "Copied trades" table.
2. **Kalshi demo.** Make a demo account at demo.kalshi.co → Settings → API Keys → create a key.
   - In Render → **Environment**: set `KALSHI_KEY_ID` to the key ID.
   - In Render → **Environment → Secret Files**: add a file named `kalshi.pem` and paste the private key.
   - Set `BOT_MODE=live` (keep `KALSHI_ENV=demo`). The dashboard will show **LIVE · DEMO**.
3. **Real money.** Create a key on your real Kalshi account, replace the key ID and `kalshi.pem`,
   then set `KALSHI_ENV=prod`. The dashboard will show **LIVE · REAL MONEY**.
   Start with small limits in `config.yaml` (`fixed_usd`, `max_usd_per_day`).

Changing env vars on Render restarts the service.

## Safety switches
- **Pause copying** button on the dashboard. Trades during a pause are skipped, not queued.
- To stop everything: set `BOT_MODE=paper`, or suspend the service in Render.
- Daily, per-trade and per-market $ caps in `config.yaml`.
- On first start it ignores the whale's past trades and only copies new ones.

## Limits to know
- You always enter after the whale. Kalshi often has less volume, so many trades get skipped by `max_slippage`.
- Positions are tracked by the bot. If you trade the same Kalshi markets by hand, it won't know.
- Kalshi fees aren't counted in the trade table.
- Never commit your `.pem` key to GitHub. `.gitignore` blocks it, but double-check.
