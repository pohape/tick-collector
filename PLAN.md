# Plan: Tick Collector for Binance & Bybit Perpetual Futures

## Context

Собственная тиковая история с bid/ask для точного бэктестинга (реальный спред, проскальзывание).
Коллектор работает 24/7, пишет в CSV-файл с сегодняшней датой в имени.
Должен пережить все сетевые проблемы (тестируем через toxiproxy `toxiproxy.sh`).

## Пары

Обе биржи: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `DOGEUSDT`
Только Binance: `BNBUSDT`

## Архитектура

```
[Binance WS bookTicker] ──┐
                          ├──▶ asyncio collector ──▶ дописываем в файл data/{exchange}/{ticker}/{date_UTC}.csv
[Bybit WS tickers.*]   ───┘
```

Один Python-процесс, два asyncio-таска (по одному на биржу), каждый управляет одним WS-соединением с подпиской на 5/6
символов.

## Структура репозитория

```
tick-collector/
├── collector.py              # main: asyncio.gather(binance, bybit, flusher)
├── exchanges/
│   ├── binance.py            # WS bookTicker
│   └── bybit.py              # WS tickers (snapshot+delta)
├── requirements.txt          # websockets и так далее
├── .env.example          # символы для мониторинга, путь до папки с данными
├── toxiproxy.sh          # обёртка над toxiproxy-cli для управления прокси (drop, freeze, lag, slow, normal)
├── setup_toxiproxy.sh    # создание прокси
└── test_resilience.sh    # автоматический хаос-тест
```

## Структура CSV-файла

ts,bid,ask,bid_size,ask_size

Где:
ts — timestamp биржи в миллисекундах
bid, ask, bid_size, ask_size — как строки/decimal, не float в памяти при записи

### Пример CSV для Binance:

Пример CSV
Binance

Поток <symbol>@bookTicker выглядит так:

```
{
  "e":"bookTicker",
  "u":400900217,
  "E":1568014460893,
  "T":1568014460891,
  "s":"BNBUSDT",
  "b":"25.35190000",
  "B":"31.21000000",
  "a":"25.36520000",
  "A":"40.66000000"
}
```

Из него в CSV пишем так:

```
ts,bid,ask,bid_size,ask_size
1568014460893,25.35190000,25.36520000,31.21000000,40.66000000
1568014461897,25.35200000,25.36530000,28.11000000,39.42000000
```

### Пример CSV для Bybit:

Для derivatives ticker stream пример содержит:

```
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

Из него в CSV пишем так:

```
ts,bid,ask,bid_size,ask_size
1760325052630,66666.60,66666.70,23789.165,23775.469
1760325052730,66666.50,66666.60,23801.240,23760.112
```

## WebSocket API

### Binance Futures bookTicker

- URL: `wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/...`
- Символы lowercase
- Поля: `b` (bid_price), `B` (bid_qty), `a` (ask_price), `A` (ask_qty), `T` (timestamp ms)
- Keepalive не нужен (сервер шлёт ping)

### Bybit Linear tickers

- URL: `wss://stream.bybit.com/v5/public/linear`
- Subscribe: `{"op": "subscribe", "args": ["tickers.BTCUSDT", ...]}`
- Поля: `bid1Price`, `bid1Size`, `ask1Price`, `ask1Size`, `ts`
- **Snapshot+delta**: первое сообщение полное, далее только изменённые поля — коллектор хранит полное состояние,
  применяет дельты
- **Ping**: отправлять `{"op": "ping"}` каждые 20с

## Ключевая логика

### Каждый exchange stream

```python
async def binance_stream(db):
    backoff = 3
    while True:
        try:
            async with websockets.connect(URL, ssl=...) as ws:
                backoff = 3  # reset on success
                await subscribe(ws)
                async for msg in ws:
                    tick = parse(msg)
                    if not is_changed(tick):  # дедупликация
                        continue
                    db.buffer(tick)
        except (ConnectionClosed, ConnectionError, TimeoutError, OSError) as e:
            log.warning("Binance disconnected: %s, reconnect in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
```

### Дедупликация

- Dict `last_state[(exchange, symbol_id)]` → `(bid_price, bid_qty, ask_price, ask_qty)`
- Пишем в буфер только при изменении любого из 4 значений

## Деплой

**Systemd unit** `/etc/systemd/system/tick-collector.service`:

```ini
[Unit]
Description = Tick Collector

[Service]
Type = simple
WorkingDirectory = /opt/tick-collector
EnvironmentFile = /opt/tick-collector/.env
ExecStart = /opt/tick-collector/venv/bin/python3 collector.py
Restart = always
RestartSec = 5

[Install]
WantedBy = multi-user.target
```

## Тестирование через Toxiproxy

### Подготовка

```bash
docker run -d --name toxiproxy --restart always \
  -p 8474:8474 -p 29080:29080 -p 29081:29081 \
  ghcr.io/shopify/toxiproxy:latest

toxiproxy-cli create binance_ws -l 0.0.0.0:29080 -u fstream.binance.com:443
toxiproxy-cli create bybit_ws -l 0.0.0.0:29081 -u stream.bybit.com:443
```

Env override: `BINANCE_WS_URL=wss://127.0.0.1:29080/...`, `BYBIT_WS_URL=wss://127.0.0.1:29081/...`

### Сценарии (`test_resilience.sh`)

| # | Сценарий        | Команда                                                 | Ожидание                    |
|---|-----------------|---------------------------------------------------------|-----------------------------|
| 1 | Baseline        | —                                                       | тики текут 30с              |
| 2 | Drop Binance    | `toxiproxy.sh binance_ws drop` → 10с → `normal`         | тики Binance возобновляются |
| 3 | Freeze Bybit    | `toxiproxy.sh bybit_ws freeze` → 20с → `normal`         | тики Bybit возобновляются   |
| 4 | High latency    | `toxiproxy.sh binance_ws lag 2000 500` → 15с → `normal` | тики текут                  |
| 5 | Slow connection | `toxiproxy.sh bybit_ws slow` → 15с → `normal`           | тики текут                  |
| 6 | Both down       | оба `drop` → 15с → оба `normal`                         | оба возобновляются          |
| 7 | Rapid flapping  | 5× (drop 3с → normal 5с)                                | процесс жив, тики текут     |

### Часовой тест стабильности

После прохождения хаос-тестов — запуск на 1 час с мониторингом каждые 60с. Критерий успеха: обе биржи показывают
непрерывные тики, нет пропусков > 30с.

## Порядок реализации

1. Создать репозиторий, структуру файлов
2. `config.py` — конфигурация
3. `exchanges/binance.py` — WS клиент Binance
4. `exchanges/bybit.py` — WS клиент Bybit (snapshot+delta)
5. `collector.py` — main, asyncio.gather
6. Ручной запуск, проверка что тики пишутся
7. Toxiproxy setup + `test_resilience.sh` — хаос-тесты
8. Исправление проблем найденных хаос-тестами
9. Systemd unit, часовой тест стабильности

## Дополнительно

1. один файл = одна биржа + одна пара + один день
2. exchange и symbol не писать в каждую строку, держать их в пути файла
3. timestamp хранить как integer ms
4. цены и размеры писать как пришли строками, без округления