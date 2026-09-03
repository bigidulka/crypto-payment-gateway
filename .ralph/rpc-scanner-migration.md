# RPC OR-topics deposit scanner migration

Перевод сканера депозитов инвойсов с OKLink на `eth_getLogs` с OR-фильтром по
получателям через keyed read-only RPC plane. Отменяет
`.ralph/oklink-primary-migration.md`.

## Почему

Per-check address lease ограничил число сканируемых адресов активными
инвойсами, но та же миграция выставила `scanner_provider=oklink`. OKLink-путь
стоит один HTTP-запрос на адрес на страницу с паузой `scanner_request_delay_ms`
(1500 мс) после каждого. Стоимость круга снова стала пропорциональна числу
активных инвойсов, плюс 429 от провайдера. При 20 активных инвойсах пол цикла —
30 с на сеть; один упавший адрес выставлял `is_complete=False`, и чекпойнт
переставал двигаться.

RPC-путь с OR-topics уже был реализован в `ResilientLogFetcher`, но его
использовал только `persistent_poller` (legacy, отключён профилем). Сканер
инвойсов шёл через `EvmAdapter.get_transfer_logs_batch`, который тоже
перебирает адреса по одному.

## Goals

- Один запрос на сеть за цикл независимо от числа активных инвойсов.
- Нативные депозиты работают без OKLink.
- Keyed RPC plane отделён от `rpc_urls`, через которые уходят sweep-транзакции.
- Stale chain checkpoint не скрывает свежий invoice transfer на RPC-пути.

## Checklist

- [x] `evm_log_poller` rpc-ветка использует `ResilientLogFetcher`.
- [x] Address-first `from_block` распространён на rpc-путь.
- [x] `fetch_native_transfers`: поиск нативных депозитов по балансу с бинарным
      поиском блока, ~log2(window) запросов на адрес.
- [x] `RpcEndpointSpec` с заголовками; `build_endpoint_specs` ставит keyed RPC
      первым, публичные — следом.
- [x] `SCANNER_RPC_BASE_URL` / `SCANNER_RPC_API_KEY` / `SCANNER_RPC_API_KEY_HEADER`.
- [x] `scanner_provider = "rpc"` во всех шести EVM-сетях.
- [x] `close_resilient_fetchers` закрывает HTTP-сессии провайдеров.
- [x] Тесты: `tests/test_evm_log_poller_rpc.py`, `tests/test_native_scanner.py`.
- [x] Деплой шлюза, запуск `api`, `worker-webhook`, `worker-expirer`, `worker-poller`.
- [x] Smoke-инвойс: лизинг адреса, скан, истечение, возврат адреса в пул.
- [x] `enabled = true` для arbitron в payment_bot, деплой бота.
- [x] Чистка: `proxies.txt`/`PROXY_FILE` у поллера, `worker-persistent` из compose,
      OKLINK_* из `.env.example`.
- [x] Два бага, вскрытых деплоем (см. ниже), исправлены и проверены на проде.

## Баги, которые нашёл деплой

Оба существовали и до миграции, но проявлялись только на RPC-пути, где
`from_block` уходит прямо в `eth_getLogs`.

1. **Окно скана не было ограничено** (`cbaf117`). `to_block` всегда равнялся
   head, `from_block` считался от возраста инвойса. На base это дало диапазон в
   2.6 млн блоков: keyed plane ответил `HTTP 413`, публичный узел — «maximum
   1000 blocks», скан падал каждый круг и checkpoint не двигался.
   Теперь `from_block = max(invoice lower bound, checkpoint + 1)`, а `to_block`
   ограничен `from_block + scan_window`; отставший сканер догоняет по окну за
   круг, при этом устаревший checkpoint по-прежнему не может скрыть свежий
   инвойс.

2. **Cooldown воскрешал чужие сессии** (`f385b7c`). Адреса переиспользуются из
   пула, а late-window матчил любую истёкшую сессию адреса, стоящего в
   cooldown. Аренда адреса под новый BSC-инвойс поднимала его июльскую сессию на
   base и июньскую на polygon: каждая сеть видела фиктивный активный адрес, а
   `from_block` считался от даты того старого инвойса. Теперь доwatchивается
   только самая свежая сессия адреса.
   Проверено на проде в момент активного cooldown: старый предикат матчил
   4 сессии на base/bsc/polygon, новый — одну, ту, что реально держит аренду.

## Verification

- Локальные тесты: 122 passed, 33 skipped. Довалидационный baseline — 106 passed
  при тех же 9 failed / 5 errors (pre-existing: SecretStr в e2e, устаревший
  импорт `SweepState`, тесты, требующие БД). Регрессий нет, +16 новых тестов.
- Живой прогон `ResilientLogFetcher` против хаба: `fetchers ready: 6/6`,
  `method=or_topics`, `complete=True` на base/arbitrum/bsc/polygon/avax/optimism;
  на bsc найдено 34 реальных ERC-20 лога за 500 блоков.
- Хаб отдаёт `eth_getLogs` с окном 5000 блоков (в конфиге `scan_window = 2000`).
- Сервер, поллер: `ResilientLogFetcher ready for 6/6 chains`,
  `[bsc] https://hub.arbitron.dev/rpc/bsc supports OR topics ✓`,
  `Scanning blocks ... (active addresses: 1, provider=rpc)` каждые ~5.7 с.
- Smoke `PAY_Vj4bgtPGjhq28yA5`: адрес `0x1fed22a22df733053089829d2fd8f4cfb83a12ae`
  (bsc/USDT) выдан из свежего пула, просканирован, истёк в 23:26:30, webhook
  outbox создан, пул вернулся к 199 available + 1 cooldown.
- Логи поллера: 0 упоминаний OKLink, 0 ошибок.
- После обоих фиксов, второй smoke `PAY_P06nSWw_1QBmljTn`, адрес
  `0xa03d3a2511e98da2691e9f90bc19481c3802801b`: сканируется только `bsc`,
  окна по 12 блоков идут за головой, base/polygon/остальные простаивают,
  0 ошибок и 0 catch-up.
- Gateway HEAD `f385b7c`; сервисы: `api` healthy, `worker-poller`,
  `worker-webhook`, `worker-expirer`, `worker-sweeper`, `postgres`, `redis`.
- payment_bot HEAD `b688526`; `manager_bot` и `bot_runner` пересобраны.
- Ответы шлюза за 20 минут: 42×200, 1×201, 1×422 (мой кривой payload в smoke).
  10×401 — это тестовый бот из ветки `new_design`, остановленный в 23:20;
  после его удаления 401 больше нет.

## Осталось вручную

- Реальный платёж на минимальную сумму: проверить CONFIRMED, однократное
  начисление баланса и sweep. Без движения средств этот шаг не закрыть.
- Резервные копии секретов в `/home/server/Projects/VERS2/payment_bot/.env*.bak`
  содержат ротированные значения — решение об удалении за владельцем.
- `/home/server/Projects/payment_bot` — старый VERS1-деплой, контейнеров нет.

## Notes

- Хаб read-only: `eth_sendRawTransaction` не выдан. Sweep-транзакции остаются на
  публичных RPC из `chains.toml`, поэтому keyed plane настраивается отдельно.
- Не печатать секреты, `.env`, приватные ключи. Публичные адреса и tx-хеши можно.
