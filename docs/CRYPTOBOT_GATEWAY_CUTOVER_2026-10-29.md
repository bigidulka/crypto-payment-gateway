# CryptoBot: перенос из PaymentBot в шлюз (29.10.2026)

## Модель
- Платформенный CryptoBot app 414425 подключён к шлюзу как rail `613ad3fa-…` мерчанта PaymentBot `21704f22-…`.
  Аккаунт принадлежит ровно одному мерчанту (UNIQUE identity); LLMASS и Q используют свои app.
- Боты и manager держат только merchant key и `ARBITRON_CRYPTOBOT_RAIL_ID`; токена CryptoBot у них нет.
- Метод `cryptobot` = `shared/payments/gateway_cryptobot.py`: счёт `POST /v1/invoices` + `POST /v1/invoices/{id}/provider`,
  зачисление только по квитанции шлюза на показанную сумму, выплата `POST /v1/payouts` с ключом
  `manager-withdrawal:{id}` (реконсилятор manager повторяет тот же ключ при неизвестном исходе).
- Комиссия: клиент платит +3% сервиса, зачисляется запрошенная сумма; комиссию CryptoBot несёт платформа.
- Средства, лежавшие на аккаунте до шлюза, тратятся только через `gateway_liquidity_recognitions`:
  излишек = provider_available − reserve − (ledger − held) − открытые счета, одна запись на выплату, проверка в БД.
- Неоплаченный provider-счёт закрывается только по EXPIRED от провайдера; проверенная поздняя оплата зачисляется.

## Выпуск
Gateway schema 0016; api и worker-providers на `infra/ovh-ca-gateway-20261029`; остальные финансовые
воркеры на прежнем образе. PaymentBot manager+runner на `infra/ovh-ca-paymentbot-20261029`.
Receipt: `~/Projects/_tmp/gateway-cutover-20261029/ACCEPTANCE.json`.

## Живая приёмка
Платёж 1.03 USDT в `@test_t2m_bot` → квитанция (net 0.9991) → +1.00 на баланс один раз.
Вывод владельца через manager 148.89 USDT → выплата шлюза с признанием 147.8909 → чек активирован.

## Rollback
Закрыть приём: убрать `ARBITRON_CRYPTOBOT_RAIL_ID`/вернуть старый образ PaymentBot возможно только вместе с
возвратом `CRYPTO_BOT_TOKEN` из `pre-gateway-cutover-20261029`; ledger/квитанции/операции шлюза не удалять.
Незавершённые payout-операции сначала сверить (`POST /v1/operations/{id}/reconcile`).

## Этап 2: xRocket, TON, Solana, Telegram-кошельки (29.10.2026)

### xRocket
- Отдельное приложение xRocket Pay «Relaxed Violet» (Current API, JWT), включены только «Трансферы».
  Подключено к платформенному мерчанту: rail `a35a0764-…`, активы USDT/TON, выплата — transfer владельцу.
- Боты: метод `xrocket` (`GatewayXRocketPaymentMethod`, +3% сервиса), свой ключ учёта; manager выводит
  xRocket-доход переводом в кошелёк xRocket владельца. Живой оплаты ещё не было.

### TON / Solana
- Шлюз: `ONCHAIN_GATEWAY_CHAINS` ограничивает новый путь квитанций сетями `ton,solana`; EVM остаётся на прежнем пути.
- CA: тестовые devnet/testnet RPC заменены на mainnet; сгенерированы отдельные BIP-39 seed'ы (депозиты и
  treasury для каждой сети) только в `/home/server/.config/gateway-ca/nonevm-mainnet-20261029` (600).
  Стартовые блоки записаны, чтение mainnet и сканирование окна проверены.
- TON-адаптер ограничивает частоту запросов (`TON_MIN_REQUEST_INTERVAL_SECONDS`), 429 повторяется безопасно;
  mainnet-ключ toncenter можно добавить в `TON_API_KEY` позже.
- **Активировано 29.10.2026 после живой проверки:** `NON_EVM_ENABLED=true`, `ONCHAIN_GATEWAY_ENABLED=true`,
  `ONCHAIN_GATEWAY_CHAINS=ton,solana`, `worker-onchain` запущен; выплаты (`ONCHAIN_PAYOUT_ENABLED`) выключены.
- Живая приёмка: средства с `main4` (Ethereum) забриджены Relay/Symbiosis на одноразовые кошельки;
  в `@test_t2m_bot` оплачено по 1.00 USDT в TON (jetton) и Solana (SPL) → сессии `paid`, счета `CONFIRMED`,
  `onchain_receipts` по 1000000, бот зачислил 1.00 в `arbitron:ton:USDT` и `arbitron:solana:USDT` и уведомил.
- Исправлено по ходу: уникальность индекса депозитного адреса теперь в пределах группы сетей (миграция 0017),
  счётчик Redis синхронизируется по группам; нулевой нативный баланс депозита больше не роняет sweep.
- Сбор с депозитных адресов ждёт отдельных funder-ключей (`TON_FUNDER_PRIVATE_KEY`, `SOLANA_FUNDER_PRIVATE_KEY`)
  с газом: задачи в `pending_gas / separate_gas_funder_required`, средства на депозитах.
- UX: депозитный TON-адрес бот показывает в raw-форме `0:…`; удобнее показывать `UQ…`.

### Telegram Wallet → Walt Pay
- Wallet Pay с 28.09.2026 называется Walt Pay; адаптер уже на `pay.walt.io` и Store API 1.2.0.
- Нужен ключ магазина после заявки и KYB в мерчант-портале Walt; API выплат нет, холд 48 ч.
- Self-custodial Gram wallet merchant API не имеет; его пользователи платят on-chain через TON-путь.
