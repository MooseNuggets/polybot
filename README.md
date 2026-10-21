# Polymarket US 15-minute paper trader

Watches the 15-minute **Up or Down** markets for **BTC, ETH, SOL and XRP** on Polymarket US,
logs every order book once a second, and paper trades "bid both sides" strategies at **$1 per order**.
No account, no API key, no real money. Each strategy has its own **$100 paper account** and stops trading if it runs out.

## What it paper trades: the closing-minute strategy

Each 15-minute window settles on the **average** coin price over its final 60 seconds. So near the
close, much of the result is already known. In the last 2 minutes of every window the bot:

1. Reads the live coin price from Coinbase every second (corrected for the small gap to the
   CF Benchmarks index Polymarket settles on, which it learns at each window open).
2. Works out the real odds of Up from how far the price is from the open, how much of the
   averaging minute is already in, and how much the coin usually moves per second.
3. Buys $1 of whichever side the market prices too cheaply, if the gap after fees is big enough.
   At most one bet per window. Most windows it doesn't bet.

| Account         | Rule |
|-----------------|------|
| closer          | $1 bets. Favorites only: market price 70¢+, model 85%+, edge 3¢+ after fees |
| closer_strict   | $1 bets. Favorites only: market price 80¢+, model 95%+, edge 5¢+ after fees |
| closer_5        | Same as closer, $5 bets |
| closer_strict_5 | Same as closer_strict, $5 bets |
| closer_10       | Same as closer, $10 bets |
| closer_strict_10 | Same as closer_strict, $10 bets |

The **Model check** on the dashboard compares the model's odds with the market's own prices
30 seconds before each close. If the model's score isn't lower than the market's, there's no edge.

## Live trading (real money)

Set these in Railway → your service → **Variables**. Never put keys in GitHub or in chat.

| Variable | What it does |
|---|---|
| `PM_KEY_ID` | Your Polymarket US API Key ID |
| `PM_SECRET` | Your Polymarket US API Secret Key |
| `LIVE_ENABLED` | Leave unset (or 0) for **dry-run**: checks the keys, shows your balance, logs the bets it *would* place. Set to `1` to send real orders. |
| `LIVE_BOTS` | Which paper bot to copy (default `closer`) |
| `LIVE_USD` | Dollars per real bet (default `1`) |
| `LIVE_DAILY_LOSS_LIMIT` | Stops for the day after this loss (default `10`) |
| `LIVE_MAX_BETS_PER_DAY` | Stops for the day after this many bets (default `40`) |

Real orders are fill-or-kill at the exact price the paper bot saw: all or nothing, never worse.
The dashboard's **Stop live trading** button halts it instantly.

## Setup on Railway (easiest from a phone)

1. Go to **railway.com**, sign in with GitHub, and pick the **Hobby** plan (~$5/month).
2. **New Project → Deploy from GitHub repo → MooseNuggets/polybot.** Allow Railway access to the repo if asked.
3. Open the service, then **Variables → New Variable**: `REPORT_KEY` = a password you make up.
4. Right-click (long-press) the service, or use **+ Create → Volume**. Attach a volume to the service with mount path **`/data`**. This keeps your data when it redeploys.
5. **Settings → Networking → Generate Domain.** That's your report page.
6. Open `https://YOUR-DOMAIN/?key=YOUR_REPORT_KEY` on your phone and bookmark it.

The page has buttons for the report, each coin, the last 24 hours, the live log, and a market check.
Every time new code is pushed to GitHub, Railway redeploys automatically.

## Backup option: run it on a DigitalOcean server instead of Railway

Everything works the same (dashboard, paper bots, live trading). Your history is stored on the
server's own disk, so updates never wipe it. About 15 minutes, all doable from a phone.

**1. Make a GitHub access token** (the repo is private, so the server needs a read-only key)
- github.com → profile photo → **Settings → Developer settings → Personal access tokens →
  Fine-grained tokens → Generate new token**
- Repository access: **Only select repositories → polybot**. Permissions: **Contents → Read-only**
- Generate and copy it (starts with `github_pat_`). It's only shown once.

**2. Create the server**
- digitalocean.com → **Create → Droplets** → Region: any US → Image: **Ubuntu 24.04** →
  Size: Basic / Regular, cheapest (~$6/month) → Authentication: **Password** (make one up)
- **Create Droplet**, wait for the green dot, then open it and tap **Console**

**3. Install** - paste this one line, with your token in place of `TOKEN`:
```
git clone https://MooseNuggets:TOKEN@github.com/MooseNuggets/polybot && cd polybot && sudo bash setup.sh
```
It asks a few questions. Press Enter to accept the value in [brackets]:
- **Dashboard password** - make one up
- **API Key ID / Secret Key** - from polymarket.us/developer (the secret is hidden as you type).
  Press Enter to skip both for paper-only.
- **Send REAL orders?** - `0` = dry-run first (recommended), `1` = live
- bet size, daily loss limit, daily bet cap - Enter keeps the defaults ($1, $10, 40)

At the end it prints your dashboard link: `http://SERVER-IP/?key=YOUR-PASSWORD`

**Later, in the droplet Console:**

| To… | Paste |
|-----|-------|
| Change any setting (e.g. turn live on) | `cd ~/polybot && sudo bash setup.sh` |
| Install updates from GitHub | `cd ~/polybot && git pull && sudo systemctl restart polylog` |
| Watch the live log | `journalctl -u polylog -f` (Ctrl+C to stop) |
| Stop the bot completely | `sudo systemctl stop polylog` |

Don't run Railway and DigitalOcean with live trading on at the same time, or every real bet is placed twice.

