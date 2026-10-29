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
