# Платёжный шлюз: провайдеры и блокчейны

Локальная реализация в `feat/gateway-completion`, 2026-10-08. Production и
реальные финансовые API в этих проверках не использовались. Включение по
умолчанию выключено; старые средства автоматически в ledger не импортируются.

| Метод | Создание и проверка счёта | Баланс провайдера | Автоматическая выплата | Восстановление потерянного ответа |
|---|---|---|---|---|
| CryptoBot / Crypto Pay | `createInvoice`, `getInvoices`, фактическая fee/net | `getBalance` | Привязанный к Telegram ID чек | Для потерянного create нет lookup по client ID; сохраняется UNKNOWN и нужна сверка |
| xRocket Pay, текущий API | Invoice + факты оплаченных attempts | Available/holds | Transfer | Lookup по client invoice/payout ID; повторной отправки нет |
| Wallet / Walt Pay Store API 1.2 | Order preview + settlement reconciliation | Spendable API не заявлен | Ручная | Потерянный create требует сверки; стандартное удержание 48 часов |

Точность активов задана в `config/providers.toml`. API принимает суммы точными
десятичными строками или целыми числами. Дробные JSON-числа отклоняются до
преобразования в Decimal. Gross, net и provider fee различаются; отсутствие
settlement evidence не считается нулевой комиссией или доступными деньгами.

## Подключение продукта

1. Оператор создаёт merchant, API key и webhook с отдельным секретом.
2. Merchant подключает свой provider account через `POST /v1/rails`:
   `type`, `token`, `network`, `assets`, необязательный `label`.
3. Product сохраняет заказ и постоянный idempotency key **до** сетевого запроса.
4. `POST /v1/invoices` принимает `allowed_rails`; для оплаты только через
   провайдера передайте `allowed_chains: []`. Для Wallet Pay merchant также
   передаёт доверенный `customer_telegram_id`. Публичный checkout не задаёт ID
   получателя или плательщика самостоятельно.
5. Покажите `hosted_url`, либо получите `GET /pay/{public_id}/methods` и выберите
   `POST /pay/{public_id}/select-provider` с `rail_id`.
6. Подпишите/проверьте webhook исходными байтами. На `invoice.confirmed` получите
   merchant-owned `GET /v1/invoices/{invoice_id}/receipt`, сопоставьте `event_id`,
   заказ, клиента, asset и gross. Выдача покупки и запись обработанного event
   должны быть одной транзакцией продукта.

Рабочий пример без импорта Telegram-кода: `examples/product_payment.py`.
Он сохраняет заказ в SQLite, сверяет receipt через SDK и дедуплицирует оплату
после перезапуска. Это пример границы платёжной интеграции; product handlers
сами проверяют авторизацию покупателя и назначают серверную цену.

## Баланс и выплата

- `GET /v1/rails/{rail_id}/balances`: `available` — проверенный остаток к выводу;
  это минимум непотраченного ledger entitlement и доступной ликвидности
  провайдера с учётом необработанных выплат. Большой баланс приложения не даёт
  merchant права вывести чужие средства.
- `available: null` с `availability: unknown/manual` означает отсутствие
  достаточного свидетельства. Продукт показывает «Не удалось проверить» либо
  ручной вывод; не подставляет ledger balance, requested invoice amount или 0.
- `POST /v1/payouts`: `rail_id`, `amount`, `asset`, `destination`,
  `destination_type`, постоянный `idempotency_key`. По умолчанию destination —
  numeric Telegram user ID; xRocket также допускает явный `user_id`.
- `GET /v1/operations/{id}` возвращает durable state; merchant может запустить
  readback через `POST /v1/operations/{id}/reconcile`. Внешняя отправка при
  этом не повторяется.
- `unknown`, `pending`, `reconciliation_required` сохраняют резерв.
  `completed` требует доказанной неизменяемой journal receipt; проверенный
  `failed` освобождает резерв. Повтор ключа с другим запросом отклоняется.

Провайдер может вернуть xRocket target в другом namespace (например, `user_id`
вместо запрошенного Telegram ID). Без подтверждённого соответствия получателей
операция требует сверки и сохраняет резерв. Угадывание соответствия запрещено.

Сбой после create/check/transfer не означает отсутствия операции. Crypto Pay
не предоставляет использованный здесь поиск чека по client operation ID;
при потерянном ответе оператор сопоставляет подтверждённый provider record
с durable operation. Новый чек с новым ключом для «повтора» не создаётся.

## EVM, Solana и TON

Современный путь использует те же invoice/receipt/ledger границы. Для каждой
сессии выделяется отдельный адрес и создаётся durable scan cursor. Старые TON
адреса, полученные прежней вычислительной заглушкой, не переиспользуются.

Продукт, которому нужен verified receipt, задаёт metadata
`gateway_receipt_required: true`. Боты делают это автоматически. Если merchant,
ledger или новый on-chain путь не включены, hosted checkout скрывает сети и API
отказывает **до выдачи адреса**. Такой счёт не переходит в legacy приём без
receipt. Обычные прежние invoice без этого признака сохраняют совместимость.

Выдаваемый `hosted_url` открывает HTML checkout; `/info`, `/methods`, `/select`
и `/status` обслуживают его состояние. Токен на странице фиксирован asset счёта.
QR при выборе и повторном открытии передаёт полный платёжный URI: ERC-681
с chain ID и atomic amount для EVM, Solana Pay с nominal decimal amount и
SPL mint, либо TON transfer с atomic amount и jetton master. Для native
SOL поле `spl-token` отсутствует. Округление дробных atomic units запрещено.
Форматы: [ERC-681](https://eips.ethereum.org/EIPS/eip-681),
[Solana Pay](https://docs.solanapay.com/spec),
[Tonkeeper](https://github.com/tonkeeper/wallet-api/blob/main/README.md).
Совместимость с конкретным пользовательским кошельком отдельно не проверена.

- EVM: ERC-20 и native transfer, подпись с chain ID/nonce, canonical receipt,
  exact log/destination binding и достаточная глубина блоков.
- Solana: classic SPL Token, USDT/USDC, все source token accounts, TransferChecked,
  ATA/rent, sig-verified simulation и finalized execution. Token-2022 не заявлен.
  Если список accounts не помещается в подписанный пакет 1232 bytes, перевод
  отклоняется до broadcast; оператор сначала консолидирует accounts.
- TON: wallet v3r2/v4r2, настоящий StateInit, seqno/signed BOC, canonical jetton
  master и оба wallet getters. Для USDT недостаточно исходящей transfer-записи:
  требуется успешный completed destination internal_transfer в полной trace.
  Bounced, aborted, незавершённые и не включённые в блок события не зачисляются.

`GET /v1/onchain/balances/{chain}/{asset}` возвращает проверенный `available`,
reservations и причину недоступности. `POST /v1/onchain/payouts` принимает `chain`,
`asset`, точную `amount`, `destination`, постоянный `idempotency_key`.
`GET /v1/onchain/operations/{id}` и `POST .../{id}/reconcile` принадлежат только
авторизованному merchant. Суммы и доказательства также доступны в SDK 0.2.0.

Перед подписью SQL фиксирует intent/reserve, а перед broadcast — зашифрованные
подписанные байты, immutable tx identity, context и fee budget. Потерянный ответ
не создаёт новую подпись: после cooldown повторяются только сохранённые байты.
Solana releases reserve по доказанному истечению blockhash только при доступной
истории; TON требует истёкший valid_until и неизменный synced seqno. Consumed EVM
nonce без найденного receipt освобождает очередь кошелька, но сохраняет денежный
резерв для сверки. Недоступная история/индексатор означает UNKNOWN.

Газ, ATA rent и прикреплённый TON оплачивает оператор. Treasury должен иметь
native liquidity сверх всех native обязательств перед всеми merchant. Пayout
не использует чужие native средства как газ. Фактический native расход проводится
отдельным `onchain_gas` journal, с проверкой tenant/asset/source/digest в SQL.
Funder должен отличаться от treasury и депозитного кошелька; его средства
предназначены только для операторского gas funding.

При паузе owned operation по прежнему ключу остаётся доступной даже после
отключения сети или смены treasury. Readback сохраняет факты; новый broadcast
при отключённых флагах запрещён. Legacy workers обходят enrolled sessions/jobs
по durable cursor даже после выключения feature flag. Admin retry запрещён для
неразрешённого attempt; подтверждённый failed job получает новый attempt key,
а прежний подписанный attempt сохраняется.

## Runtime и rollout

Это подготовленная последовательность, на production она не выполнялась.

1. Проверить backup, текущую Alembic revision и способ возврата feature flag.
2. Через отдельную migration-owner связь применить additive migrations 0012–0015.
   Runtime не выполняет миграции и не получает owner credential.
3. Выдать ограниченной роли проверенные DML grants на новые объекты через
   `scripts/provision_application_runtime_role.sql`. DDL/TRUNCATE не выдаются;
   новые grants, serialization triggers и on-chain proof guard входят в readiness.
4. Обновить API и включить `LEDGER_RAIL_ORCHESTRATION_ENABLED=true` только с
   конкретным `LEDGER_RAIL_ORCHESTRATION_MERCHANTS` allowlist. Отдельное приложение
   провайдера может быть связано только с одним merchant.
5. В runtime-only environment задать `GATEWAY_PROVIDER_POLL_INTERVAL_SECONDS`
   и `GATEWAY_PROVIDER_POLL_BATCH_SIZE` при необходимости. Значения по умолчанию
   — 30 секунд и 10 операций.
6. Запустить только `worker-providers` через opt-in Compose profile `providers`
   и ограниченный runtime override. Без флага worker завершится, не обрабатывая
   средства. `runtime_compose.py` проверяет конкретный image ID и команду worker.
7. Выполнить отдельный provider acceptance: account binding, exact invoice,
   actual fee/net, available balance, payout receipt и lookup. Это отдельный
   критерий; локальные HTTP fixtures его не заменяют.

Rollback: выключить feature flag и provider worker, сохранить операции,
receipt и резерв для сверки. Не удалять новые таблицы с финансовыми фактами.
Данные и флаг не позволяют автоматически повторить неизвестную отправку.

## Настройка on-chain worker

Новые пути по умолчанию выключены. После миграции и проверки runtime grants
оператор задаёт в закрытом runtime environment:

- `LEDGER_RAIL_ORCHESTRATION_ENABLED=true` и конкретный merchant allowlist;
- `ONCHAIN_GATEWAY_ENABLED=true`; отдельно `ONCHAIN_PAYOUT_ENABLED=true` для выплат;
- `NON_EVM_ENABLED=true` для Solana/TON;
- `NON_EVM_START_BLOCKS` как JSON с проверенными starting slot/block для **каждой**
  новой сети, включая EVM. Пустое значение не выбирает checkpoint автоматически;
- отдельные `SOLANA_WALLET_SEED`, `TON_WALLET_SEED`, treasury address/key и funder key;
  HD seeds — отдельные BIP-39 мнемоники; TON treasury/funder key — raw
  Ed25519 seed hex, Solana treasury/funder key — solders/base58 representation;
- `TON_RPC_URL`, `TON_INDEX_URL`, при необходимости `TON_API_KEY`; индексатор
  должен отдавать полные finalized traces и getters выбранного jetton master;
- `TON_WALLET_VERSION`/`TON_WORKCHAIN`, attach/forward amount и gas budget после
  проверки конкретного wallet contract и конфигурации сети.

`worker-onchain` запускается через opt-in Compose profile `onchain` вместе с
runtime-role override. `worker-providers` требует profile `providers`.
`ONCHAIN_REBROADCAST_INTERVAL_SECONDS` по умолчанию 60, диапазон 15–3600.
Запуск worker не импортирует исторические активы или остатки в ledger.

Rollback сохраняет миграции, cursors, receipts, signed attempts и reserves.
Отключить новые приём/выплаты и соответствующие workers; сопоставить уже
отправленные tx/provider records. Не отдавать enrolled jobs старому коду до
закрытия всех attempts. При возобновлении использовать те же durable ключи.

## Границы готовности

Изолированные PostgreSQL и runtime role проверяют receipt/journal, concurrent
replay, rollback и merchant isolation. Реальные подписанные тестовые переводы
исполняются в Anvil, Solana LiteSVM и TON sandbox; TON jetton transfer также
проверен независимой виртуальной машиной. Эти среды используют тестовые средства.
SDK wheel установлен в локально собранные образы ботов; private handlers и базы
проверяются без Telegram polling. Последние команды/числа — в плане реализации.

Remote CI, provider accounts, production opening balance, RPC eligibility и
платежи/выводы реальных средств не проверены. Образы не опубликованы и не
развёрнуты. Wallet Pay сохраняет ручной вывод и provider hold; неподтверждённый
spendable API не реализуется догадкой. Исторические Float balances ботов требуют
отдельной сверки: новые точные GatewayCredits не считаются автоматическим backfill.

Публичные источники контрактов, проверенные при реализации:

- https://help.cr.bot/ru/articles/10279948-crypto-pay-api
- https://pay.api.xrocket.exchange/api/docs-json
- https://docs.walt.io/pay/integration-schema.yaml
