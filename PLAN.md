# Plan: Tick Collector for Binance & Bybit Perpetual Futures

## Context

Собственная тиковая история top-of-book (`bid/ask`) для точного бэктестинга: реальный спред, более реалистичное
исполнение market/marketable-лимитных ордеров, оценка проскальзывания.

Коллектор работает 24/7, пишет в CSV-файлы по UTC-дате, переживает сетевые сбои и автоматически переподключается.
Устойчивость проверяется через `toxiproxy`.

Важно: этот процесс **только собирает и пишет CSV**. Он **не делает gzip**, **не загружает файлы в облако** и **не
чистит старые файлы**. Эти задачи выполняет отдельный ночной скрипт по cron.

## Symbols

### Binance

- `BTCUSDT`
- `ETHUSDT`
- `SOLUSDT`
- `XRPUSDT`
- `DOGEUSDT`
- `BNBUSDT`

### Bybit

- `BTCUSDT`
- `ETHUSDT`
- `SOLUSDT`
- `XRPUSDT`
- `DOGEUSDT`

## What exactly is stored

Храним только лучший bid/ask и их размеры.

CSV schema:

```csv
ts,bid,ask,bid_size,ask_size
```

Where:

- `ts` — timestamp биржи в миллисекундах
- `bid`, `ask`, `bid_size`, `ask_size` — записываются как строки, без `float`-округления в Python

### Timestamp mapping

Для единообразия между биржами:

- **Binance**: использовать `E` (`event time`), а не `T`
- **Bybit**: использовать `ts`

Причина: `Bybit ts` — системный timestamp сообщения; `Binance E` ближе по смыслу, чем `T`.

## File layout

Один файл = одна биржа + один символ + один UTC-день.

```text
data/
  binance/
    BTCUSDT/
      2026-03-15.csv
    ETHUSDT/
      2026-03-15.csv
  bybit/
    BTCUSDT/
      2026-03-15.csv
```

Правила:

1. `exchange` и `symbol` не пишутся в каждую строку — только в пути файла.
2. Новый файл создаётся при первом тике дня.
3. Заголовок CSV пишется один раз при создании файла.
4. При смене UTC-даты старый файл закрывается.
5. На следующий тик открывается новый файл с новой UTC-датой.

### Что такое UTC rollover

**UTC rollover** — это момент смены суток по UTC, то есть переход с `23:59:59.xxx UTC` на `00:00:00.000 UTC`.

Для коллектора это значит:

- пока UTC-дата равна, например, `2026-03-15`, пишем в `2026-03-15.csv`
- как только пришёл тик уже с UTC-датой `2026-03-16`, коллектор закрывает старый файл и начинает писать в
  `2026-03-16.csv`

Это делается **по UTC**, а не по локальному часовому поясу сервера.

## Architecture

```text
[Binance WS combined bookTicker] ──┐
                                   ├──▶ normalizer/deduper ──▶ asyncio.Queue ──▶ single writer ──▶ data/{exchange}/{symbol}/{date_UTC}.csv
[Bybit WS tickers.*] ──────────────┘
```

Один Python-процесс.

Короутины:

1. `binance_stream()`
2. `bybit_stream()`
3. `writer_task()`

### Important design rule

**Писать в файлы должен только один writer-task.**

Причина:

- не будет гонок между несколькими корутинами
- проще flush/rotate
- проще контролировать backpressure

## Repository structure

```text
tick-collector/
├── collector.py               # main: launch tasks, graceful shutdown
├── settings.py                # env parsing and validation (minimal helper)
├── writer.py                  # queue consumer, CSV append, UTC rollover
├── exchanges/
│   ├── binance.py             # Binance Futures bookTicker reader + parser
│   └── bybit.py               # Bybit V5 tickers reader + parser + delta merge
├── requirements.txt           # websockets, python-dotenv, etc.
├── .env.example               # symbols, data dir, ws overrides, logging level
├── toxiproxy.sh               # wrapper over toxiproxy-cli
├── setup_toxiproxy.sh         # proxy bootstrap
└── test_resilience.sh         # chaos test scenarios
```

## Configuration approach

Полноценный `config.py` для этого проекта не нужен.

Используем:

- `.env` как источник значений
- маленький `settings.py` только для чтения, парсинга и базовой валидации

Что делает `settings.py`:

- читает `.env`
- преобразует строки в нужные типы (`Path`, списки символов, int)
- задаёт дефолты
- валидирует обязательные переменные

Что он **не** делает:

- многоуровневую конфигурацию
- inheritance профилей
- отдельную архитектуру конфигов

Пример `.env`:

```dotenv
DATA_DIR=/home/user/csv-data
BINANCE_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT
BYBIT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT
LOG_LEVEL=INFO
BINANCE_WS_URL=
BYBIT_WS_URL=
WS_TEST_MODE=false
```

## WebSocket API

### Binance USDⓈ-M Futures

- Base URL: `wss://fstream.binance.com`
- Combined stream URL:
    - `wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/...`
- Symbols must be lowercase in stream names
- Combined stream message wrapper:

```json
{
  "stream": "btcusdt@bookTicker",
  "data": {
    ...
  }
}
```

- Relevant fields inside `data`:
    - `E` — event time (ms)
    - `b` — best bid price
    - `B` — best bid qty
    - `a` — best ask price
    - `A` — best ask qty
    - `s` — symbol

Notes:

- отдельный `subscribe()` для Binance **не нужен**, если используется combined stream URL
- соединение нужно уметь переживать и регулярно пересоздавать; Binance может разорвать его примерно через 24 часа
- нужно корректно отвечать на ping/pong на уровне WebSocket-библиотеки

### Bybit Linear V5

- URL: `wss://stream.bybit.com/v5/public/linear`
- Subscribe message:

```json
{
  "op": "subscribe",
  "args": [
    "tickers.BTCUSDT",
    "tickers.ETHUSDT"
  ]
}
```

- Relevant fields:
    - `ts`
    - `data.symbol`
    - `data.bid1Price`
    - `data.bid1Size`
    - `data.ask1Price`
    - `data.ask1Size`

Notes:

- первое сообщение по символу может быть `snapshot`
- дальше идут `delta`
- если поле в delta отсутствует, его значение не изменилось
- нужно хранить полное текущее состояние top-of-book по каждому символу и применять partial updates
- отправлять `{"op":"ping"}` каждые 20 секунд

## Parsing examples

### Binance example payload

```json
{
  "e": "bookTicker",
  "u": 400900217,
  "E": 1568014460893,
  "T": 1568014460891,
  "s": "BNBUSDT",
  "b": "25.35190000",
  "B": "31.21000000",
  "a": "25.36520000",
  "A": "40.66000000"
}
```

CSV row:

```csv
ts,bid,ask,bid_size,ask_size
1568014460893,25.35190000,25.36520000,31.21000000,40.66000000
```

### Bybit example payload

```json
{
  "topic": "tickers.BTCUSDT",
  "type": "snapshot",
  "data": {
    "symbol": "BTCUSDT",
    "bid1Price": "66666.60",
    "bid1Size": "23789.165",
    "ask1Price": "66666.70",
    "ask1Size": "23775.469"
  },
  "ts": 1760325052630
}
```

CSV row:

```csv
ts,bid,ask,bid_size,ask_size
1760325052630,66666.60,66666.70,23789.165,23775.469
```

## Core logic

### Reader loop skeleton

```python
async def exchange_loop(queue):
    backoff = 3
    while True:
        try:
            async with websockets.connect(...) as ws:
                backoff = 3
                ...
                async for raw in ws:
                    tick = parse_and_normalize(raw)
                    if tick is None:
                        continue
                    await queue.put(tick)
        except (ConnectionClosed, ConnectionError, TimeoutError, OSError) as e:
            log.warning("stream disconnected: %s; reconnect in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
```

### Normalized tick object

```python
{
    "exchange": "binance",
    "symbol": "BTCUSDT",
    "ts": 1760325052630,
    "bid": "66666.60",
    "ask": "66666.70",
    "bid_size": "23789.165",
    "ask_size": "23775.469",
}
```

### Deduplication

Держим:

```python
last_state[(exchange, symbol)] = (bid, bid_size, ask, ask_size)
```

В очередь и в файл пишем только если изменилось хотя бы одно из четырёх значений.

Это сокращает шум и размер архива.

### Gap visibility

Нужны метрики/логи:

- `last_msg_monotonic[(exchange, symbol)]`
- warning, если по символу нет апдейтов дольше заданного порога
- reconnect counter per exchange

Важно: коллектор **не умеет восстанавливать исторические пропуски** через REST. Он только логирует gap и продолжает
поток.

## Writer behavior

### Required behavior

- принимает нормализованные тики из `asyncio.Queue`
- лениво открывает файловые хендлы
- пишет CSV rows append-only
- flush по таймеру или батчами
- при переходе UTC-даты:
    1. flush
    2. close old file
    3. open new file on next tick

### Recommended flush policy

Достаточно одной из стратегий:

- каждые `N` строк, например 100-1000
- или каждые 1-2 секунды

`fsync` на каждый тик не нужен.

## Separate nightly maintenance script

Отдельный ночной скрипт запускается по cron, например в `00:01 UTC`, и делает уже не realtime-задачи:

- gzip закрытых дневных CSV-файлов
- загрузку недостающих архивов в облако
- удаление лишних локальных файлов по retention policy
- сохранение только сжатых файлов за последний месяц

Этот скрипт **не входит** в текущий scope tick collector.

## Shutdown behavior

При SIGTERM / SIGINT:

1. остановить приём новых сообщений
2. дописать очередь
3. flush/close все открытые файлы
4. завершиться с кодом 0

Это важно для systemd restart и для ручного deploy.

## Deployment

### systemd unit

```ini
[Unit]
Description = Tick Collector
After = network-online.target
Wants = network-online.target

[Service]
Type = simple
WorkingDirectory = /opt/tick-collector
EnvironmentFile = /opt/tick-collector/.env
ExecStart = /opt/tick-collector/venv/bin/python3 collector.py
Restart = always
RestartSec = 5
KillSignal = SIGINT
TimeoutStopSec = 30

[Install]
WantedBy = multi-user.target
```

## Toxiproxy testing

### Bootstrap

```bash
docker run -d --name toxiproxy --restart always \
  -p 8474:8474 -p 29080:29080 -p 29081:29081 \
  ghcr.io/shopify/toxiproxy:latest

toxiproxy-cli create binance_ws -l 0.0.0.0:29080 -u fstream.binance.com:443
toxiproxy-cli create bybit_ws   -l 0.0.0.0:29081 -u stream.bybit.com:443
```

### Important TLS note

Для `wss://127.0.0.1:29080/...` через TCP-proxy будет проблема с TLS hostname verification, потому что сертификат
удалённого сервера не выписан на `127.0.0.1`.

Поэтому в test-mode нужно одно из решений:

1. отдельный SSL context с отключённой hostname verification **только для локального chaos-test**
2. или явно передавать корректный `server_hostname`
3. или использовать локальный DNS alias вместо `127.0.0.1`

В production это не нужно.

### Env overrides

```dotenv
BINANCE_WS_URL=wss://127.0.0.1:29080/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/solusdt@bookTicker/xrpusdt@bookTicker/dogeusdt@bookTicker/bnbusdt@bookTicker
BYBIT_WS_URL=wss://127.0.0.1:29081/v5/public/linear
WS_TEST_MODE=true
```

### Scenarios

| # | Scenario        | Command                                                 | Expected result                          |
|---|-----------------|---------------------------------------------------------|------------------------------------------|
| 1 | Baseline        | —                                                       | rows keep appearing                      |
| 2 | Drop Binance    | `toxiproxy.sh binance_ws drop` → 10s → `normal`         | Binance reconnects and rows resume       |
| 3 | Freeze Bybit    | `toxiproxy.sh bybit_ws freeze` → 20s → `normal`         | Bybit reconnects and rows resume         |
| 4 | High latency    | `toxiproxy.sh binance_ws lag 2000 500` → 15s → `normal` | process alive, rows continue/resume      |
| 5 | Slow connection | `toxiproxy.sh bybit_ws slow` → 15s → `normal`           | process alive, rows continue/resume      |
| 6 | Both down       | both `drop` → 15s → both `normal`                       | both reconnect                           |
| 7 | Rapid flapping  | 5× (`drop` 3s → `normal` 5s)                            | no crash, reconnect loop remains healthy |

### One-hour stability test

После хаос-тестов — запуск минимум на 1 час.

Success criteria:

- процесс жив всё время
- writer task жив всё время
- reconnect работает
- для каждого `(exchange, symbol)` `last_msg_age` не выходит за разумный порог надолго
- новые строки продолжают появляться после каждого сетевого сбоя

## Implementation order

1. Создать репозиторий и каркас файлов
2. `config.py`
3. `writer.py` с queue + file rotation
4. `exchanges/binance.py`
5. `exchanges/bybit.py`
6. `collector.py` (`asyncio.gather`)
7. Ручной запуск и smoke test
8. `setup_toxiproxy.sh` + `test_resilience.sh`
9. Исправление проблем после chaos tests
10. systemd deploy
11. one-hour stability run

## Non-goals for v1

Не делаем в первой версии:

- PostgreSQL
- восстановление пропусков через REST
- order book depth > 1
- trade stream
- metrics backend типа Prometheus
- gzip файлов
- cloud backup uploader
- retention cleanup

## Final acceptance criteria

Готово, если:

1. коллектор 24/7 собирает Binance + Bybit
2. пишет данные в раздельные CSV по бирже/символу/дню
3. корректно применяет Bybit snapshot/delta
4. корректно парсит Binance combined stream wrapper
5. не пишет дубликаты одинакового top-of-book
6. переживает временные сетевые сбои и переподключается
7. корректно ротирует файлы по UTC
8. корректно завершает работу без потери уже принятых данных
9. не занимается gzip, backup и cleanup — только realtime-сбором и записью CSV