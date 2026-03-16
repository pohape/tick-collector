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

All settings are in `.env`. Restart the service after any change: `sudo systemctl restart tick-collector`.

```
DATA_DIR=data
BINANCE_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT
BYBIT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT
LOG_LEVEL=INFO
WS_TEST_MODE=false
LOCAL_STORAGE_MB=1024
WEBDAV_USER=your_account
WEBDAV_PASSWORD=your_app_password
WEBDAV_URL=https://webdav.cloud.mail.ru  # or https://webdav.yandex.ru
```

| Variable | Description |
|---|---|
| `DATA_DIR` | Directory for CSV files |
| `BINANCE_SYMBOLS` | Comma-separated Binance symbols |
| `BYBIT_SYMBOLS` | Comma-separated Bybit symbols |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING) |
| `WS_TEST_MODE` | Disable TLS verification for toxiproxy testing |
| `LOCAL_STORAGE_MB` | Max MB of compressed files to keep locally |
| `WEBDAV_USER` | WebDAV account username |
| `WEBDAV_PASSWORD` | WebDAV app password |
| `WEBDAV_URL` | WebDAV endpoint URL |

All variables are required.

## Data volume

With the default 11 symbols (6 Binance + 5 Bybit):

| Period | Raw CSV | Compressed (.zst) |
|---|---|---|
| Day | ~95 MB | ~18 MB |
| Month | -- | ~550 MB |
| Year | -- | ~6.5 GB |

Raw CSV files are compressed and deleted daily by `maintain.py`, so only one day of uncompressed data exists at any time.

## Nightly maintenance

Maintenance runs automatically at 00:01 UTC as part of the collector process. It compresses closed CSV files (zstd level 19, ~5x ratio), uploads them to a WebDAV cloud, and deletes the oldest local files when total compressed size exceeds `LOCAL_STORAGE_MB`.

No separate cron job or systemd timer needed.

To run maintenance manually (e.g., on first deploy):

```bash
./venv/bin/python3 maintain.py
```

### Cloud storage

Any WebDAV-compatible storage will work. Configure via `WEBDAV_USER`, `WEBDAV_PASSWORD`, and `WEBDAV_URL` in `.env`.

**Mail.ru Cloud** (8 GB free):

```
WEBDAV_USER=your@mail.ru
WEBDAV_PASSWORD=your_app_password
WEBDAV_URL=https://webdav.cloud.mail.ru
```

Generate an app password at Mail.ru: Settings > Security > App passwords.

**Yandex.Disk** (10 GB free):

```
WEBDAV_USER=your@yandex.ru
WEBDAV_PASSWORD=your_app_password
WEBDAV_URL=https://webdav.yandex.ru
```

Generate an app password at Yandex: ID > Security > App passwords.

At ~6.5 GB/year both free tiers are sufficient for over a year of data.

**Google Drive** does not support WebDAV and cannot be used.

To verify your WebDAV credentials before deploying:

```bash
./venv/bin/python3 check_webdav.py
```

This will test the connection, check free space, create and delete a test file, and report results.

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
- **Buffered dedup**: a row is written only when bid or ask price changes, and contains the final sizes observed at the previous price level (see below)
- Auto-reconnect with exponential backoff (3s to 30s)
- UTC date rollover for file rotation
- Graceful shutdown on SIGINT/SIGTERM (drains queue, flushes files)

### How buffered dedup works

Exchanges send many updates per millisecond. Most of them change only the order size, not the price. Writing every size change would produce massive files with little value for backtesting.

Instead, each stream keeps a one-tick buffer in memory:

1. A tick arrives with the same bid/ask as before -- the buffer is updated (RAM only, no I/O)
2. A tick arrives with a new bid or ask price -- the **buffered tick is flushed** to the writer (one file write), then the new tick becomes the buffer

This means each CSV row represents a **settled price level** with the **final liquidity** (bid_size/ask_size) that was available at that price before it moved. This is more useful for backtesting than the first (transient) sizes that appear when a price level is initially set.

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
