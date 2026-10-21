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

## Setup on Railway (easiest from a phone)

1. Go to **railway.com**, sign in with GitHub, and pick the **Hobby** plan (~$5/month).
2. **New Project → Deploy from GitHub repo → MooseNuggets/polybot.** Allow Railway access to the repo if asked.
3. Open the service, then **Variables → New Variable**: `REPORT_KEY` = a password you make up.
4. Right-click (long-press) the service, or use **+ Create → Volume**. Attach a volume to the service with mount path **`/data`**. This keeps your data when it redeploys.
5. **Settings → Networking → Generate Domain.** That's your report page.
6. Open `https://YOUR-DOMAIN/?key=YOUR_REPORT_KEY` on your phone and bookmark it.

The page has buttons for the report, each coin, the last 24 hours, the live log, and a market check.
Every time new code is pushed to GitHub, Railway redeploys automatically.

## Alternative: setup on a DigitalOcean server

**1. Put these files on GitHub**
- github.com → New repository → name it `polybot` → Create
- "uploading an existing file" → drag in `logger.py`, `analyze.py`, `setup.sh`, `README.md`
- Click **Commit changes** (uploading alone does nothing)

**2. Rent a small server**
- DigitalOcean (or Hetzner/Vultr) → Create Droplet → **Ubuntu 24.04**, cheapest plan (~$4–6/month), any US region
- When it's ready, open the droplet and click **Console** (a terminal in your browser, no setup needed)

**3. Install (paste one line into the console, swap in your GitHub username)**
```
git clone https://github.com/YOUR-USERNAME/polybot && cd polybot && sudo bash setup.sh
```
It installs, starts the logger, and prints a market check. **Copy that check output back to Claude**
so it can confirm everything is being read correctly.

The logger now runs 24/7 and restarts itself after reboots or crashes.

## Daily use (in the droplet Console)

| To… | Paste |
|-----|-------|
| See the paper trading report | `cd ~/polybot && venv/bin/python analyze.py` |
| Report for one coin | `cd ~/polybot && venv/bin/python analyze.py --coin btc` |
| Last 24 hours only | `cd ~/polybot && venv/bin/python analyze.py --hours 24` |
| Watch it live | `journalctl -u polylog -f` (Ctrl+C to stop watching) |
| Is it running? | `systemctl status polylog` |
| Install an update from GitHub | `cd ~/polybot && git pull && sudo systemctl restart polylog` |

Let it run **at least 3–5 days** before reading much into the numbers.
