# Polymarket US 15-minute paper trader

Watches the 15-minute **Up or Down** markets for **BTC, ETH, SOL and XRP** on Polymarket US,
logs every order book once a second, and paper trades "bid both sides" strategies at **$1 per order**.
No account, no API key, no real money.

## What it paper trades (every window, every coin)

| Strategy    | What it does |
|-------------|--------------|
| maker_49    | Rests a $1 buy on Up at 49¢ AND a $1 buy on Down at 49¢ at window open. Cancels anything unfilled at 12:00. |
| maker_48    | Same at 48¢ |
| maker_46    | Same at 46¢ |
| maker_44    | Same at 44¢ |
| taker_both  | Baseline: instantly buys $1 of Up and $1 of Down at window open, paying the taker fee |

If both sides fill at 48¢ you hold 2.08 Up + 2.08 Down shares for $2.00, and one side pays $2.08: +$0.08.
If only one side fills, you're holding a straight bet that can win or lose.

Each fill is scored two ways: **cautious** (price traded *through* your order) and **hopeful**
(price *touched* your order). Real results land in between.

To change prices, the cancel time, or the $1 size, edit the SETTINGS block at the top of `logger.py`.

## Setup (about 15 minutes, no coding)

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
