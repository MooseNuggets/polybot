# Polymarket US 15-minute paper trader

Watches the 15-minute **Up or Down** markets for **BTC, ETH, SOL and XRP** on Polymarket US,
logs every order book once a second, and paper trades "bid both sides" strategies at **$1 per order**.
No account, no API key, no real money. Each strategy has its own **$100 paper account** and stops trading if it runs out.

## What it paper trades (every window, every coin)

| Strategy    | What it does |
|-------------|--------------|
| maker_49    | Simple reference: rests a $1 buy on Up AND Down at 49¢ at window open, cancels at 12:00 |
| follow_2h   | Keeps both bids 2¢ under the live price; after one side fills, works the other side to lock a profit, or buys it outright once the pair costs ≤ $1.02 (caps losses) |
| follow_2hr  | follow_2h, optimized: only enters when the market is between 35/65 and 65/35, no new pairs after 10:00, and repeats up to 5 pairs per window. Never more than one pair working at a time (no laddering) |

If both sides fill at 48¢ you hold 2.08 Up + 2.08 Down shares for $2.00, and one side pays $2.08: +$0.08.
If only one side fills, you're holding a straight bet that can win or lose.

Each fill is scored two ways: **cautious** (price traded *through* your order) and **hopeful**
(price *touched* your order). Real results land in between.

To change prices, the cancel time, or the $1 size, edit the SETTINGS block at the top of `logger.py`.

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
