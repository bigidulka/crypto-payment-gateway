# Отделение провайдеров: контрольная точка 28.10.2026

Изменения находятся в project-local ветках `codex/gateway-separation-20261028`
проектов arbitron-payment и payment_bot. Исходные незавершённые worktree от
8 октября сохранены; изменения CA PaymentBot c2411c3 включены в новую сборку
исходников. Это локальная реализация, не переключение production authority.

## Реализовано

- В шлюзе: адаптеры CryptoBot/xRocket, durable provider operations, отдельные
  merchant/account/network binding, receipts/ledger/outbox, polling и резервы.
  TON/Solana сохраняют отдельные флаги, checkpoints и проверку chain evidence.
- SDK 0.3.0: framework-independent ProductPayments, заказ до HTTP, неизменный
  idempotency key, hosted checkout, подпись webhook и merchant-owned receipt,
  polling fallback, durable fulfillment inbox с ручным acknowledge.
- PaymentBot: меню, callbacks и ввод суммы идут через gateway; direct CryptoBot
  polling и старый admin payout отключены в managed mode. Даже присутствующий
  legacy token не попадает в managed runtime config. Импорт runtime проверен
  в отдельном процессе с запрещённым импортом provider SDK.
- Отдельный Dockerfile.gateway и минимальные зависимости без aiocryptopay/web3.
  Generic legacy manager/runner образы сохранены, wheel обновлён до 0.3.0.
- xRocket Current OpenAPI заново прочитана 28 октября; все пути адаптера есть
  в текущей спецификации. Это не проверка конкретного provider account.

## Проверено сейчас

- 164 gateway/SDK/API/chain checks passed, 3 PostgreSQL rail checks skipped.
  Это выбранные затронутые проверки, не весь репозиторий.
- В них включены TON sandbox и Solana LiteSVM: реальные подписи/исполнение в
  локальных VM, без mainnet средств. Отдельный chain набор: 56 passed.
- 69 PaymentBot checks passed с vendored wheel: product/manager/runner,
  receipt/replay/reserve, сохранение прежнего delivery fix и webhook guard,
  доступность managed меню, старых кнопок и запуск без provider SDK/token.
- 12 SDK checks passed из собранного wheel, отдельно от исходников SDK.
- SDK wheel bytes сверены с источниками, Alembic имеет одну голову 0015.
  Diff checks пройдены; исходные worktree сохранены.

## Незакрыто

Локальная Docker service остановлена; system settings и прежние контейнеры
не запускались. Свежие PostgreSQL migration/runtime-role/concurrency проверки
не выполнены. Исторические результаты от 8 октября их не заменяют.
Новый Dockerfile.gateway не собран. Remote CI, production rollout и реальный
provider/chain → gateway → bot платёж не выполнялись. Нет финальной независимой
приёмки всего большого исходного diff.

До выпуска: изолированный PostgreSQL, migrations/grants/atomicity/tenant tests;
сборка образов на Python 3.12; включение одного отдельного merchant и сверка
одного реального платежа. Перед миграцией существующего продукта закрыть его
legacy pending/unknown операции. Балансы и provider credentials автоматически
не переносятся, неизвестные выплаты не повторяются. TON/Solana mainnet и новые
provider accounts не включаются по результатам unit/VM тестов.

Интеграция: [BOT_INTEGRATION.md](BOT_INTEGRATION.md).
