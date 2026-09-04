# План: полное отделение платёжной платформы от бота

Статус: предложение. Основано на аудите от 2026-09-04 (контрактная развязка 9/10,
упаковка 5/10). Каждый этап самодостаточен и деплоится отдельно.

## Целевая архитектура

```
                        ┌─────────────────────────────────────────┐
  чужие проекты ──────▶ │          arbitron gateway               │
  payment_bot (31 бот)─▶│  единый Invoice API + webhook + SDK     │
  manager_bot ─────────▶│  (админ-панель платформы)               │
                        └───────────┬─────────────────────────────┘
                                    │ rails (провайдеры)
        ┌───────────────┬───────────┼───────────────┬──────────────┐
   on-chain EVM    on-chain SOL   on-chain TON   CryptoBot     xRocket / TG Wallet
   (готово)        (hub RPC)      (tonapi)       (app API)     (custodial API)
```

Принцип: **шлюз — единственный, кто знает про все рельсы.** Бот и чужие
проекты видят только `POST /v1/invoices → select → status + webhook`.
Кастодиальные рельсы (CryptoBot/xRocket/TG Wallet) — аккаунты мерчантов,
шлюз оркестрирует, но не хранит их деньги.

---

## Этап 0 — упаковка контракта (~1 неделя)

Цель: чужой проект подключается за день без копирования кода.

1. **Извлечь SDK**: `shared/payments/{base,arbitron}.py` → отдельный пакет
   `arbitron-sdk-py` (GitHub repo, private PyPI или git-зависимость).
   Клиент: create invoice, select chain, status, verify_webhook (HMAC),
   типы ChainInfo/InvoiceStatus. ~600 строк, зависимость только aiohttp.
2. **Публичный вход**: Caddy/nginx с TLS перед :8123, rate limit на
   публичные роуты, `X-API-Key` мерчанта наружу.
3. **Админ-сессии из памяти → подписанные токены** (`src/api/deps.py`:
   `_admin_sessions` dict умирает при рестарте) — JWT/HMAC-токен с TTL,
   без состояния.
4. **Онбординг мерчанта** (пока admin-only): POST /v1/admin/merchants,
   создание ключей и вебхуков — эндпоинты уже есть, собрать в сценарий.
5. **payment_bot переходит на SDK**: удалить `shared/payments/arbitron.py`
   из репы бота, оставить конфиг. `cryptobot.py` временно остаётся (уходит
   на этапе 2).
6. **Docs**: README шлюза с quickstart (3 шага: ключ → счёт → вебхук),
   спецификация webhook-полей, пример верификации подписи на 3 языках.

Приёмка: пустой python-проект принимает тестовый платёж через hosted
checkout и валидирует вебхук, не открывая репу бота.

## Этап 1 — мультирельсовость в шлюзе (~2 недели)

Цель: CryptoBot/xRocket/TG Wallet — такие же рельсы, как EVM.

1. **Таблица `rails`**: merchant_id, rail_type (evm|sol|ton|cryptobot|
   xrocket|tgwallet), credentials (зашифровано ENCRYPTION_KEY, как
   deposit-ключи), supported assets/networks, is_active.
2. **Интерфейс Rail** (в шлюзе, не в SDK):
   `create_invoice / poll_status / verify_payment / payout` + пассивный
   `incoming_webhook` для рельсов с push (xRocket).
3. **CryptoBot-рельс**: перенос `createInvoice/getInvoices/createCheck`
   из `shared/payments/cryptobot.py` в адаптер рельса. Проверка статусов —
   poller, как `notifications_checker` в боте, только в шлюзе.
4. **xRocket-рельс**: xrocket.pro API — invoice + webhook, USDT.
   Кастодиальный, деньги на аккаунте мерчанта-подключателя.
5. **TG Wallet-рельс**: Paylink API (crypto for telegram) — депозитные
   ссылки; payout пока ручной (OAuth приложения — отдельная история).
6. **Роутинг в invoice**: `allowed_rails`/`allowed_chains` объединяются;
   `/v1/public/chain-info` отдаёт юзеру и EVM-сети, и CryptoBot, и
   xRocket как равные способы оплаты.
7. **Тесты**: mock-адаптеры на все рельсы (у xRocket/TG Wallet нет
   песочниц) + contract-тесты интерфейса; staging-смоук на малых суммах.

Приёмка: счёт с `allowed_rails=[evm_bsc, cryptobot]` оплачивается обоими
способами, статус и вебхук едины.

## Этап 2 — Solana (~1.5–2 недели)

Хаб даёт весь read-RPC (проверено: getSlot, getSignaturesForAddress с
blockTime/finalized, 27 методов). Broadcast — публичные эндпоинты.

1. **Деривация**: ed25519, BIP44 `501'`; хранение — то же encrypted-хранилище
   deposit-ключей (`src/wallet/`), отдельный chain_group.
2. **solana_scanner worker**: на активный адрес — `getSignaturesForAddress`
   → `getTransaction` → diff `pre/postTokenBalances` (сумма+минт).
   Чекпоинты по слотам, зачисление после `finalized` (~13–32 слота).
   Классификатор сумм (доплата/недоплата/чужой токен) — переиспользуется
   целиком, он не EVM-специфичен.
3. **Балансы/доплата**: `getTokenAccountBalance` — та же логика
   «баланс адреса закрывает счёт», что уже работает на EVM.
4. **sweeper**: SPL transfer, ATA-rent (~0.002 SOL разово) с funder'а,
   комиссия ~0.000005 SOL. Broadcast мимо хаба — пул публичных RPC
   с ротацией (урок BSC-инцидента уже зашит в RpcManager).
5. **CI**: `solana-test-validator` в docker — те же E2E-сценарии
   (exact/overpay/underpay/topup/wrong-token), что на anvil.
6. **Ассеты**: USDT-SPL, USDC-SPL.

Приёмка: все 5 сценариев зелёные на тест-валидаторе; живой смоук
с funder-кошелька (подпись оператора).

## Этап 3 — TON (~2–3 недели, самый тяжёлый)

В хабе TON нет. Нужен tonapi.io (key) с fallback toncenter.

1. **Депозиты**: jetton-кошелёк на адрес; матчинг платежа по **комментарию**
   (короткий id инвойса) — уникальность, избыточные bounce'и возвращаются.
2. **scanner**: события jetton-трансферов по адресу (tonapi events),
   подтверждение — 5+ блоков/masterchain seqno.
3. **sweeper**: jetton transfer + forward-payload через tonutils
   (сборка boc, seqno, bounce-обработка), broadcast в tonapi.
4. **CI**: mylocalton / ton-local в docker для E2E.
5. **Ассет**: USDT-TON. Газ копеечный, но bounce-модель требует аккуратности.

Приёмка: тестовый цикл на локальной сети + смоук на малой сумме
с bounce-сценарием.

## Этап 4 — бот съезжает на шлюз (параллельно этапам 1–2)

1. `payment_bot/utils/payments.py` → всё через SDK; прямой код CryptoBot
   из бота удаляется.
2. Админ-выплаты (`admin_menu_handlers.py` payout) → payout-API шлюза
   (extend: `POST /v1/payouts` с rail-параметром; createCheck для
   CryptoBot-рельса уже есть в коде).
3. `manager_bot` → админ-панель платформы: UI создания мерчантов/ключей/
   вебхуков поверх admin API шлюза.

---

## Что остаётся в payment_bot навсегда

UX ботов, тарифы, FSM, рефералки, инстанс-менеджмент. Платёжный слой
сжимается до SDK-вызовов.

## Риски и решения

| риск | решение |
|---|---|
| xRocket/TG Wallet меняют API, нет песочниц | contract-тесты + версионированные адаптеры + staging-смоук в релизе |
| вебхук недоставлен (custodial push) | outbox уже есть; poller-фолбэк как в invoice_checker |
| TON bounce съедает платёж | коммент-матчинг + балансная сверка; недоплата→тот же UX, что на EVM |
| Solana rate limit на 100+ адресов | разнос цикла по адресам, batch getMultipleAccounts для балансов |
| деньги кастодиальных рельсов — не наши | rails хранят токены мерчанта; шлюз только оркестратор (юридически чище) |

## Порядок и вехи

| этап | время | чем заканчивается |
|---|---|---|
| 0 | 1 нед | чужой проект платит без кода бота |
| 1 | 2 нед | CryptoBot/xRocket/TG Wallet = рельсы |
| 2 | 1.5–2 нед | Solana USDT/USDC в бою |
| 3 | 2–3 нед | TON USDT в бою |
| 4 | параллельно | бот и manager на платформе |

Этапы 2 и 3 независимы от 1 — можно менять порядок под бизнес-приоритет.
