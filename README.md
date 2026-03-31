# POLYWATCH DATA COLLECTOR

Automated Polymarket data collection system. Polls every hour, builds a
SQLite dataset of market snapshots, volume spikes, price movements, and
cross-platform divergences (Polymarket vs Manifold). After 4-6 weeks,
bring the dataset back to Claude for strategy analysis.

---

## FILES

```
polywatch-collector/
├── collector.py      # Main collection script (runs hourly)
├── paper_trade.py    # Log hypothetical trades for backtesting
├── query.py          # Analyze the collected dataset
├── .github/
│   └── workflows/
│       └── collect.yml  # GitHub Actions: runs collector hourly for free
└── README.md
```

---

## QUICK START — GitHub Actions (Recommended, Free)

This method runs entirely in the cloud. No laptop needed.

### Step 1: Create a GitHub repo

```bash
git init polywatch-collector
cd polywatch-collector
# Copy all files here
git add .
git commit -m "Initial polywatch collector"
```

Create a new repo at https://github.com/new (make it **private**),
then push:

```bash
git remote add origin https://github.com/YOUR_USERNAME/polywatch-collector.git
git branch -M main
git push -u origin main
```

### Step 2: Enable GitHub Actions

1. Go to your repo on GitHub
2. Click **Actions** tab
3. Click **"I understand my workflows, go ahead and enable them"**
4. The `collect.yml` workflow will now run automatically every hour

### Step 3: Verify it's working

- Go to **Actions** tab
- You should see runs appearing every hour
- Click any run → check logs → look for "Collection run complete ✓"
- The database is saved as an **Artifact** after each run

### Step 4: Generate weekly summary

1. Go to **Actions** tab
2. Click **"Polywatch Hourly Collector"** in the left sidebar
3. Click **"Run workflow"** → select `summary` from dropdown → click green button
4. Download the `weekly-summary` artifact
5. Paste the `.txt` contents into Claude for analysis

---

## LOCAL SETUP (Alternative)

If you prefer to run on your own machine or a VPS:

### Requirements

```bash
python3 --version   # needs 3.8+
# No external packages needed — uses only stdlib
```

### Run manually

```bash
cd polywatch-collector
python3 collector.py          # one collection run
python3 collector.py summary  # generate weekly summary
```

### Schedule with cron (Linux/Mac)

```bash
crontab -e
# Add this line (runs every hour at :05):
5 * * * * cd /path/to/polywatch-collector && python3 collector.py >> cron.log 2>&1
```

### VPS Setup (DigitalOcean/Hetzner, ~$5/month)

```bash
# On Ubuntu 22.04
sudo apt update && sudo apt install python3 git -y
git clone https://github.com/YOUR_USERNAME/polywatch-collector.git
cd polywatch-collector
crontab -e
# Add cron line above
```

---

## QUERYING YOUR DATA

```bash
# Weekly summary (paste into Claude)
python3 query.py summary

# Top volume spikes this week
python3 query.py spikes

# Biggest price movers
python3 query.py movers

# High confidence markets (>80% YES)
python3 query.py highconf

# Recently resolved markets
python3 query.py resolved

# Cross-platform divergences (Poly vs Manifold)
python3 query.py divergences

# Full history for one market
python3 query.py market 1162940

# Export everything to CSV
python3 query.py export
```

---

## PAPER TRADING

Log hypothetical trades to track your signal performance:

```bash
# Add a new paper trade
python3 paper_trade.py add

# List open trades
python3 paper_trade.py list

# Check which trades have resolved
python3 paper_trade.py resolve

# See your win rate
python3 paper_trade.py stats
```

---

## WHAT GETS COLLECTED (every hour)

**Snapshots table** (one row per market per hour):
- YES probability, bid, ask, last trade price
- 24h volume, all-time volume, liquidity
- 1h price change, 24h price change
- Volume spike ratio (24h / all-time)
- Hours until resolution
- Whether market is featured/new

**Resolutions table** (when market reaches ~0% or ~100%):
- Final outcome (YES/NO)
- Final price
- Total volume traded

**Divergences table** (Polymarket vs Manifold keyword matches):
- Both market questions
- Both probabilities
- Spread size
- Matched keywords

---

## AFTER 4-6 WEEKS

Run `python3 query.py summary` and paste the output into Claude.

Claude will analyze:
- Which signal types (spikes, high-conf, divergences) were most predictive
- Market categories with consistent edges
- Optimal entry timing (how many hours before resolution)
- Win rate by probability bucket
- Whether cross-platform divergence is a reliable signal

From there, build the actual trading bot execution layer.

---

## DATABASE SCHEMA

```sql
-- Check what you have
sqlite3 polywatch.db ".tables"
sqlite3 polywatch.db "SELECT COUNT(*), MIN(captured_at), MAX(captured_at) FROM snapshots;"

-- Quick market history
sqlite3 polywatch.db "
  SELECT captured_at, yes_prob, chg_1h, volume_24h
  FROM snapshots WHERE market_id='1162940'
  ORDER BY captured_at;
"
```

---

## NOTES

- No API keys required — all public endpoints
- Database grows ~500KB/day at 200 markets/hour
- After 6 weeks: ~21MB — very manageable
- GitHub Actions free tier: 2,000 minutes/month. This uses ~720 min/month (fits free tier)
- The DB artifact is overwritten each run but **accumulates all data** inside SQLite
