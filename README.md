# Tick Collector

24/7 top-of-book (best bid/ask) collector for Binance and Bybit perpetual futures. Writes tick-level data to CSV files organized by exchange, symbol, and UTC date.

Built for backtesting: captures real spreads, realistic market order execution, and slippage estimation.

## What it collects

Top-of-book quotes for any USDT perpetual futures pairs on Binance and Bybit. Symbols are configured via `BINANCE_SYMBOLS` and `BYBIT_SYMBOLS` in `.env`.

Each row: `ts,bid,ask,bid_size,ask_size` -- prices stored as strings (no float rounding).

## File layout

```
data/
  binance/
    BTCUSDT/
      2026-03-15.csv
      2026-03-14.csv.zst
  bybit/
    BTCUSDT/
      2026-03-15.csv
      2026-03-14.csv.zst
```

One file = one exchange + one symbol + one UTC day. Files rotate at UTC midnight.

## Quick start

```bash
git clone https://github.com/pavelyanu/tick-collector.git
cd tick-collector
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env with your settings

./venv/bin/python3 collector.py
```

## Configuration

All settings are in `.env`:

```
DATA_DIR=data
BINANCE_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT
BYBIT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT
LOG_LEVEL=INFO
WS_TEST_MODE=false
RETENTION_DAYS=10
MAIL_USER=your@mail.ru
MAIL_APP_PASSWORD=your_app_password
MAIL_WEBDAV_URL=https://webdav.cloud.mail.ru
```

| Variable | Description |
|---|---|
| `DATA_DIR` | Directory for CSV files |
| `BINANCE_SYMBOLS` | Comma-separated Binance symbols |
| `BYBIT_SYMBOLS` | Comma-separated Bybit symbols |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING) |
| `WS_TEST_MODE` | Disable TLS verification for toxiproxy testing |
| `RETENTION_DAYS` | Days to keep compressed files locally |
| `MAIL_USER` | Mail.ru Cloud email |
| `MAIL_APP_PASSWORD` | Mail.ru Cloud app password |
| `MAIL_WEBDAV_URL` | Mail.ru Cloud WebDAV endpoint |

All variables are required.

## Nightly maintenance

`maintain.py` compresses closed CSV files (zstd, ~12x ratio), uploads them to Mail.ru Cloud, and deletes local files older than `RETENTION_DAYS`.

```bash
./venv/bin/python3 maintain.py
```

## Deploy as a systemd service

### 1. Install

```bash
sudo mkdir -p /home/user/GitHub/tick-collector
sudo cp -r . /home/user/GitHub/tick-collector/
cd /home/user/GitHub/tick-collector
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
sudo cp .env.example .env
sudo nano .env  # fill in your settings
```

### 2. Create the service

```bash
sudo tee /etc/systemd/system/tick-collector.service << 'EOF'
[Unit]
Description=Tick Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=user
Group=user
WorkingDirectory=/home/user/GitHub/tick-collector
EnvironmentFile=/home/user/GitHub/tick-collector/.env
ExecStart=/home/user/GitHub/tick-collector/venv/bin/python3 collector.py
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
```

### 3. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable tick-collector
sudo systemctl start tick-collector
sudo systemctl status tick-collector
```

### 4. View logs

```bash
sudo journalctl -u tick-collector -f
```

## Schedule nightly maintenance

Create a systemd timer to run `maintain.py` at 00:01 UTC every day. Unlike cron, systemd timers support UTC natively.

```bash
sudo tee /etc/systemd/system/tick-maintain.service << 'EOF'
[Unit]
Description=Tick Collector Maintenance
After=network-online.target

[Service]
Type=oneshot
User=user
Group=user
WorkingDirectory=/home/user/GitHub/tick-collector
EnvironmentFile=/home/user/GitHub/tick-collector/.env
ExecStart=/home/user/GitHub/tick-collector/venv/bin/python3 maintain.py
EOF

sudo tee /etc/systemd/system/tick-maintain.timer << 'EOF'
[Unit]
Description=Run tick maintenance daily at 00:01 UTC

[Timer]
OnCalendar=*-*-* 00:01:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now tick-maintain.timer
```

Check status:

```bash
sudo systemctl list-timers tick-maintain.timer
sudo journalctl -u tick-maintain -f
```

This will:
- Compress all closed CSV files from previous days (zstd level 19)
- Upload compressed files to Mail.ru Cloud
- Delete local files older than `RETENTION_DAYS`

## Architecture

```
Binance WS (bookTicker) ──┐
                           ├── dedup ── asyncio.Queue ── writer ── data/{exchange}/{symbol}/{date}.csv
Bybit WS (tickers.*)  ────┘
```

Single Python process. Three async tasks:
- `binance_stream()` -- combined bookTicker stream
- `bybit_stream()` -- V5 tickers with snapshot/delta merge
- `writer_task()` -- single writer, no file races

Key behaviors:
- Deduplication: skips unchanged top-of-book
- Auto-reconnect with exponential backoff (3s to 30s)
- UTC date rollover for file rotation
- Graceful shutdown on SIGINT/SIGTERM (drains queue, flushes files)

## Chaos testing

Requires [toxiproxy](https://github.com/Shopify/toxiproxy):

```bash
./setup_toxiproxy.sh    # start toxiproxy container
./test_resilience.sh    # run 7 chaos scenarios
```

Scenarios: connection drop, freeze, high latency, slow bandwidth, both exchanges down, rapid flapping.

## Tests

```bash
./venv/bin/pip install pytest
./venv/bin/python3 -m pytest tests/ -v
```
