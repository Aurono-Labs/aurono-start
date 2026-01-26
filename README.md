# Aurono — Local Crypto Trading Automation
Aurono Start / PoC (macOS · Linux · Raspberry Pi)
Author: Aurono Labs

<!-- OTA test v5.05 -->

Aurono is a local, plug-and-play trading automation system that runs entirely on your own machine (Mac or Raspberry Pi).
It connects to trusted exchanges (Bitvavo, Kraken, Coinbase), executes rule-based strategies, and reports transparently — while funds always remain on your exchange.

Instant kill switch: unplug the device.

--------------------------------------------------

## CORE PRINCIPLES

- Non-custodial — Aurono never holds funds
- Rule-based trading — deterministic and explainable
- Runs locally — no cloud dependency
- Auto-updatable — safe OTA updates
- Transparent — dashboard, logs, reports

--------------------------------------------------

## KEY FEATURES

### Trading Engine
- Multi-exchange architecture
  - ✅ Bitvavo (stable)
  - ✅ Kraken (stable)
  - 🚧 Coinbase (beta, JWT / ECDSA)
- Exchange-agnostic strategy engine
- Deterministic **limit-order execution**
- Accurate tick-size and precision handling
- Fee-aware P&L calculations
- Per-strategy capital allocation tracking

### Strategies
- Buy on drops (pullbacks)
- Sell on rises (profit-taking)
- Timeframes: 1h, 4h, 1d, 1w
- Multiple strategies per symbol
- Independent capital allocations (budget) per strategy

### Dashboard
- Local web UI
- Runs on http://aurono-beta.local:8000 (Pi) or http://localhost:8000 (Mac)
- Total Porfolio Overview and Liquidity Summery: exchange balances vs allocated capital
- Strategies Overview: price, balance, value in EUR, current capital allocation per strategy
- Recent trades per strategy
- Last trades

### Reporting
- Daily HTML report
- Weekly HTML report
- Structured JSON schemas
- Generated automatically via cron / systemd timers

--------------------------------------------------

## PROJECT STRUCTURE

aurono-poc/
├── src/
│   ├── main functionality python files (trading engine, exchange classes, report builders)
│   ├── routes/
│   ├── schemas/
│   ├── templates/
│   └── tools/
├── config/
│   └── config.yaml
├── data/
│   ├── reports/
│   ├── trades.db
│   └── aurono_log.txt
├── systemd/
│   ├── aurono-trader.service
│   ├── aurono-dashboard.service
│   └── aurono-update.timer
├── aurono-update/
│   └── updater.sh
└── aurono_install_first_ship_installer.sh
└── VERSION

--------------------------------------------------

## QUICK INSTALL (RECOMMENDED)

macOS / Linux / Raspberry Pi

Run:
bash aurono_install_first_ship_installer.sh

Installer behavior:
- Detects OS
- Installs system dependencies
- Creates Python virtual environment
- Installs Python requirements
- Preserves config, database and logs or creates new config, database and log file when clean install
- Optionally installs systemd services
- Enables OTA auto-update mechanism

--------------------------------------------------

### FIRST RUN

Open the dashboard on Mac:
http://localhost:8000

On Raspberry Pi:
http://aurono-beta.local:8000

Setup steps:
1. Add exchange API keys
2. Create strategies
3. Allocate capital
4. Aurono starts trading automatically

--------------------------------------------------

## MANUAL INSTALLATION (ADVANCED)

macOS:

cd aurono-poc
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python3 src/tools/create_trades_db.py
python3 src/dashboard.py

--------------------------------------------------

## SERVICE MODE (LINUX / RASPBERRY PI)

Aurono runs as two services:
- aurono-trader.service
- aurono-dashboard.service

Enable and start:

sudo systemctl daemon-reload
sudo systemctl enable aurono-trader aurono-dashboard
sudo systemctl start aurono-trader aurono-dashboard

Check status:
systemctl status aurono-trader
systemctl status aurono-dashboard

View logs:
journalctl -u aurono-trader -f
journalctl -u aurono-dashboard -f

--------------------------------------------------

## AUTO UPDATE (OTA)

- Updates pulled from GitHub releases
- Code updated in place
- Config, database and logs preserved
- Services restarted automatically

## Manual update:
sudo systemctl start aurono-update.service


--------------------------------------------------

## SECURITY MODEL

- API keys encrypted at rest
- No cloud dependency
- No fund custody
- Exchange permissions:
  - Read balances
  - Place trades
  - No withdrawals

--------------------------------------------------

## SUPPORTED EXCHANGES

Bitvavo  - Stable
Kraken   - Stable
Coinbase - Beta (JWT / ECDSA)

--------------------------------------------------

## DISCLAIMER

Aurono does not provide financial advice.
Crypto trading involves risk.
You are responsible for your strategies and allocations.

--------------------------------------------------
