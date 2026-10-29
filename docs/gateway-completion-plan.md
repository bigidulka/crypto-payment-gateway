# Полная реализация платёжной платформы Arbitron

Дата начала: 2026-10-08. Основа: план отделения шлюза от 2026-09-04 и
последующие разработки ledger / provider ingestion / runtime-role.
Реализация: ветка `feat/gateway-completion`, один писатель.

## Текущее состояние

Последнее поручение: **полностью довести остаток интеграций**, без goal.
Разрешённые SDK установлены в отдельное `.venv-chains`; исходное `.venv`
сохранено. Изменения находятся в двух project-local worktree:
`feat/gateway-completion` и `payment_bot:feat/gateway-integration`.
Commit, push, deploy, production backfill и реальные выплаты не выполнялись.

| Этап | Реализовано и локально проверено | Внешний критерий, который ещё не подтверждён |
|---|---|---|
| M1 | CryptoBot, xRocket, Wallet Pay; exact gross/net, capabilities, ограниченный HTTP, encrypted credentials | Приёмка на реальных provider accounts |
| M2 | Durable create/mapping, poller, hosted checkout, атомарные net ledger/event/outbox и SQL proof | Реальный платёж, поздний settlement и provider lookup |
| M3 | Verified available, резервы, provider и on-chain payout, receipt/reconciliation; отдельный operator gas | Реальная выплата и сверка исторического opening balance |
| M4 | Solana: finalized evidence, SPL/native signing, ATA/rent, broadcast/readback, scan/ledger/funding/sweep | Mainnet/devnet smoke и проверка конкретного RPC |
| M5 | TON: настоящий StateInit v3r2/v4r2, TEP-74, master-derived wallets, completed destination proof, signed BOC, scan/ledger/funding/sweep | Реальный TON USDT smoke и полнота выбранного индексатора |
| M6 | SDK 0.2.0 wheel, отдельный product example, payment_bot net credit, manager onboarding/balance/payout/replay | Приёмка в Telegram с новой конфигурацией merchant и production data |
| M7 | Миграции 0012–0015, grants, opt-in workers, CI-конфигурация, документация, локальные образы и ревью | Удалённый CI, реальная финансовая приёмка и rollout |

Протокол и rollout: [provider-payments.md](provider-payments.md).
Подключение ботов: `payment_bot/docs/gateway-integration.md` в соседнем проекте.
Текущие проверки и ограничения описаны в заключительной записи журнала ниже.
Ни один внешний критерий не считается закрытым по результатам локальных тестов.

## Цель и границы

Самостоятельный сервис для своих и внешних продуктов: merchant API, Python SDK,
hosted checkout, подписанные webhook, приём и выплаты через EVM, CryptoBot,
xRocket, Wallet Pay, Solana USDT/USDC и TON USDT. Подключение проектов без
копирования платёжной реализации в каждый бот. Manager управляет мерчантами,
ключами, webhook и подключёнными способами оплаты.

Платёж считается подтверждённым только по проверенному факту провайдера или
блокчейна. Запрошенная сумма счёта не является полученной суммой. Баланс на вывод
отделён от выручки, резервов и исторических корректировок. Нельзя смешивать
одноимённые токены разных сетей или деньги разных мерчантов.

Разрешены локальная реализация и изолированная проверка. Этот план не включает
массовое изменение исторических балансов, production backfill, выдачу чужих
промокодов, сообщения пользователям или движение реальных средств. Развёртывание
и включение финансовых путей проходят отдельно после готового проверяемого релиза.

## Подтверждённая исходная точка

- Checkout основан на `868298b`; до начала tracked-изменений не было.
- Merchant/Public API, Python SDK, EVM scanners, адресные lease, webhook outbox
  и ограниченная runtime-роль уже существуют.
- Ledger и атомарный ingestion существуют, но ingestion выключен и привязан к
  CryptoBot; создания provider mapping из публичного пути пока нет.
- В реестре адаптеров зарегистрирован только CryptoBot. xRocket / Wallet Pay
  отсутствуют. Solana / TON отправка средств содержит незавершённые реализации.
- Производственные подключения и доступность API провайдеров сейчас не проверены.

## Этапы и критерии приёмки

### M1. Контракт способов оплаты и реальные адаптеры

- [x] Единый каталог возможностей: активы, сети, приём, выплаты, provider limits.
- [x] CryptoBot, xRocket и Wallet Pay: create / lookup / server-side verification.
- [x] Баланс возвращается только по авторитетному ответу; недоступность = UNKNOWN.
- [x] Конфигурация credentials закрыта, секреты зашифрованы и не возвращаются API.
- [x] Внешние вызовы ограничены timeout; после неоднозначного create/payout нет
      автоматического повторения операции.
- [ ] Контракты проверены по актуальной документации; реальные smoke отдельно
      от mock/локальных контрактных тестов.

Файлы: `src/payments/rails/`, `src/api/merchant/`, `src/core/config.py`,
`tests/test_rails*`. Проверка: успешные, ошибочные, timeout и malformed ответы,
неверный asset / invoice ID / amount, без сетевых денежных операций.

### M2. Полный цикл provider invoice и учёт

- [x] Merchant подключает собственный provider account и задаёт доступные методы.
- [x] Invoice выбирает метод; один повторный запрос возвращает прежнюю операцию,
      а смена payload с тем же ключом отклоняется.
- [x] Durable attempt записан до I/O; provider mapping связывает invoice,
      merchant, account, network, canonical asset и точную atomic amount.
- [x] Poller / webhook подтверждают оплату только server-side проверкой.
- [x] Факт, двойная проводка, invoice event и outbox появляются атомарно;
      poll/webhook/concurrency не создают второго зачисления.
- [x] Просроченные, чужие, недоплаченные и конфликтующие платежи требуют сверки,
      не получают автоматического зачисления по запрошенной сумме.

Файлы: additive Alembic migration, `src/db/models/`,
`src/services/provider_payment_ingestion.py`, orchestration service / worker,
hosted / merchant API. Проверка: отдельный PostgreSQL под non-owner ролью,
rollback и конкурентные повторы, tenant isolation, подпись webhook.

### M3. Балансы, выплаты и сверка

- [x] Отдельный merchant API: ledger entitlement, reserved, provider liquidity,
      confirmed available и причина UNKNOWN / unavailable.
- [x] Идемпотентный payout: reserve → submitting → completed / failed / unknown.
- [x] Неоднозначный ответ сохраняет резерв и требует provider readback;
      повтор запроса не создаёт второй чек / перевод.
- [x] Отказ без выполнения освобождает резерв; успешная выплата создаёт
      неизменяемую проводку и событие. Fee и gas отражаются отдельно.
- [x] CryptoBot check, xRocket transfer и on-chain transfer; Wallet Pay остаётся
      явно manual-only, пока провайдер не подтвердит поддерживаемый payout API.
- [x] Legacy backfill и включение ledger не происходят автоматически.

Проверка: insufficient funds, два параллельных payout, неизвестный ответ,
повтор после рестарта, authoritative receipt, резерв при отказе lookup.

### M4. Solana USDT / USDC

- [x] Канонический mint, decimals, finalized transaction / signature identity.
- [x] Scanner правильно учитывает несколько token accounts, inner instructions,
      исходящие переводы и пагинацию; ошибочный или неполный RPC не продвигает
      checkpoint.
- [x] SPL transfer, ATA creation/rent, подпись и broadcast, проверка receipt.
- [x] Native SOL gas и treasury не смешиваются с EVM конфигурацией.
- [ ] E2E на локальном validator: exact / underpay / overpay / topup / wrong mint,
      failed transaction, duplicate, sweep. Mainnet smoke только отдельно.

### M5. TON USDT

- [x] Канонический jetton master, проверка sender/destination/amount/memo.
- [x] Matching по invoice reference; bounced/failed перевод не зачисляется.
- [x] Jetton-wallet address проверяется через chain getter; wallet state/seqno,
      signing, transfer body и broadcast реализованы через проверенный SDK.
- [x] Native TON gas и treasury отдельные; polling не пропускает события.
- [ ] Локальный E2E: valid / wrong jetton / wrong memo / bounce / duplicate /
      payout; real smoke выделен отдельным критерием.

### M6. Подключение своих продуктов

- [x] SDK: методы оплаты, invoice selection, verified receipt, balances, payouts,
      явные idempotency keys и UNKNOWN.
- [x] `payment_bot`: provider logic перенесена за SDK, отображается только
      проверенная сумма на вывод; rollback через feature configuration.
- [x] `manager_bot`: onboarding мерчанта, encrypted keys и rail configuration;
      webhook настраивает оператор через существующий admin API.
- [x] Минимальный отдельный пример продукта принимает оплату и подтверждает
      подписанный event без импорта кода Telegram-бота.
- [ ] Полная совместимость существующих EVM API / SDK потребителей: legacy
      invoice create/replay/get/list и SDK проверены, боевые боты ещё не проверены.

### M7. Полная приёмка и поставка

- [x] Обновлены API docs, quickstart, настройки и operational runbook.
- [x] CI настроен с изолированными БД, runtime role, Redis, provider contracts,
      VM harness и SDK. Удалённый запуск остаётся отдельной проверкой.
- [ ] Независимое ревью финансового/state/API diff; исправлены блокирующие находки.
- [x] Каждый метод имеет explicit статус: реализован / локально проверен /
      проверен с провайдером / развёрнут. Наличие класса не считается готовностью.
- [x] Локальный reviewable release: wheel/checksum, три Docker image,
      проверенные журналы и release-evidence.json.
- [ ] Production rollout / rollback по сервисам и проверка на сервере.

## Порядок и условия остановки

M1 → M2 → M3; M4 и M5 используют тот же payment/ledger/payout контракт;
M6 по мере готовности API, затем M7. Не подключать незавершённый способ оплаты
к публичному checkout. Недостающие merchant credentials, provider approval,
validator SDK/runtime или полномочия для реального smoke фиксируются как точные
блокеры, при этом независимая локальная работа продолжается.

План принят прямым поручением пользователя на реализацию; повторного согласования
обычных обратимых инженерных решений не требуется.

## Журнал выполнения

2026-10-08, продолжение без goal: добавлены provider orchestration, net ledger,
balances/reserves/payout receipts, публичный checkout и SDK. SQL-ревью выявило
возможность поздней подмены settlement; миграция 0014 вводит общий row lock и
запрет поздней net-записи. Исправлены округление JSON/Decimal, HTTP 500 для
ожидаемых ошибок и совместимость legacy event INSERT с 0010.

Браузерная проверка на локальном HTML/JS стенде: выбор провайдера, безопасное
отображение label, UNKNOWN без ссылки, восстановление после reload без второго
create, автоматическое появление ссылки и статус «Оплачено». Настоящие аккаунты,
средства и Telegram-рассылка не затрагивались.

Итоговая локальная проверка текущего этапа: **167 passed, 6 warnings**, без
skip, 10.89 секунды. Запуск через task-owned `run-isolated-checks.py`, отдельные
БД 0010/0011/head=0014 и non-owner runtime role. Проверялись gateway orchestration,
provider ingestion, contracts/rails, HTTP API failures, legacy API, runtime
Compose/admission, SDK и Solana evidence. Ruff новых модулей и `git diff --check`
прошли. Это выборка затронутых путей, а не весь репозиторий и не provider acceptance.

Первое независимое финансовое ревью завершилось: четыре P2 исправлены и
воспроизведения закрыты проверками. Узкий повтор после исправлений не завершился
за 240 секунд; заключение повторного reviewer отсутствует. Финальная приёмка
изменённого SQL/concurrency proof остаётся отдельным незакрытым критерием M7.

Следующая запись ниже — историческая контрольная точка до возобновления;
актуальная стадия приведена в начале документа.

2026-10-08: создан active goal; восстановлен исторический план; проверено дерево
и создан project-local worktree. Реализация начата. Production не изменялся.

### Контрольная точка: бюджет goal исчерпан

Статус goal: `budget_limited`, задача не завершена. Новые этапы не запускались
после сообщения harness об исчерпании бюджета.

Внесено в локальную ветку:

- Расширен интерфейс Rail: capabilities, balances, account identity, durable
  payout operation ID, фактическая net settlement отдельно от gross payment.
- Добавлен адаптер текущего xRocket Pay API (не Legacy): invoice create/read,
  payment evidence, balances, payout/readback по client operation ID.
- Добавлен Wallet/Walt Pay Store API 1.2.0: create/read, settlement через
  reconciliation, привязка к Telegram customer. Автовывод и spendable balance
  не заявляются; отражены ручной вывод и стандартное удержание 48 часов.
- Добавлены CryptoBot getMe/getBalance и чтение net settlement при наличии
  авторитетных fee fields; отсутствие fee не превращается в нулевую комиссию.
- Адаптеры зарегистрированы; credentials по-прежнему encrypted/token-only,
  пользовательский base URL и network через credentials запрещены.
- Provider HTTP не следует redirect, не повторяет денежные вызовы автоматически
  и сохраняет unknown outcome для неоднозначных mutating failures.

Свежие источники контрактов (публичные, без credentials):

- `https://pay.api.xrocket.exchange/api/docs-json` (174315 bytes);
- `https://docs.walt.io/pay/integration-schema.yaml` (32361 bytes);
- `https://help.cr.bot/ru/articles/10279948-crypto-pay-api`.

Проверка в worktree:

- `python -m pytest tests/test_rails.py tests/test_provider_rail_contracts.py -q`:
  **52 passed, 3 skipped**. Три skip — PostgreSQL rail ORM проверки, отдельная
  БД для этого запуска не задавалась.
- Новые контрактные проверки используют локальный HTTP server: gross/net
  payment evidence, mutating 503 без повторения, запрет redirect с credentials,
  реальные advertised ограничения Wallet Pay. Это не provider acceptance.
- Ruff четырёх новых Python-файлов: passed; compileall Rail package: passed;
  `git diff --check`: passed. Весь проект и интеграции ботов не проверены.

Не выполнено: независимое ревью, live/provider testnet smoke, public API wiring,
provider invoice mapping/worker, ledger net-fee integration, reserves/payout
orchestration, Solana/TON, SDK расширение и миграция ботов, deploy.
M1 целиком не закрыт. M2–M7 остаются открытыми.

Следующий шаг: закончить M1 — проверить Wallet order create/reconciliation,
xRocket create/readback и payout response binding контрактными тестами,
CryptoBot balances/fee regression и независимым ревью. Затем M2: durable
operation + canonical provider mapping + atomic ledger/outbox. Не включать
новые адаптеры в боевые пути до этих проверок.


## Финальная локальная контрольная точка — 2026-10-08

Последнее разрешение пользователя сняло блокер установки SDK. M4–M6 доведены
до локальной реализации. Установлены pinned solders/tonsdk и TON VM harness
в отдельных project-local средах; основные настройки и main checkouts сохранены.
Goal не использовался.

- Gateway focused suite: **254 passed, 0 skipped**, 29.69 s. Включены provider
  orchestration/ingestion/contracts/API, chain receipt/ledger/reserve, actual
  Anvil/Solana LiteSVM/TON VM signing/execution, legacy API/RPC, runtime Compose
  и SDK. Это затронутые пути, не весь репозиторий.
- Bot focused suite: **28 passed**, 2.42 s. Product receipt/net/replay, manager
  owner/earnings/liquidity/reserve/readback, malformed/late reply, concurrent
  rejection, ready tenant binding и старые runner/runtime пути.
- Все три Docker production images собраны. Проверены imports/routes/adapters,
  установленный SDK 0.2.0 и инициализация обеих SQLite баз с inert credentials,
  без сети/Telegram polling. Product запускает этот путь без legacy CryptoBot token.
- Внутри gateway image: **45 passed**, 13.15 s, на task-owned PostgreSQL под
  ограниченной ролью. Это повтор API/SDK/provider orchestration под Python 3.12
  и зависимостями собранного образа.
- Исправлен фактический GET hosted URL, merchant HTML escaping и запрет
  legacy адреса для receipt-required счёта при выключенном новом учёте.
  QR передаёт сеть, asset и точную сумму; actual inline JavaScript проверен
  при reload и выборе. Страница предлагает только asset самого invoice.
- Ruff новых исполняемых модулей и `git diff --check` прошли. SDK wheel package
  bytes и METADATA сверены с источником; checksum/provenance сохранены в vendor.

Независимое чтение финансового кода выявило missing-byte replay, gas из чужой
native liquidity и снятие локального reserve после потерянного ответа/паузы.
Они исправлены; повторный разбор нашёл concurrent вариант, закрытый HTTP-тестом.
Rent-гипотеза о неизбежном Solana failure не подтвердилась в LiteSVM; добавлен
консервативный rent reserve и проверка второго sweep. Дополнительно проверено,
что rollback feature flag не передаёт enrolled jobs старым workers. Последний
узкий reviewer не выдал заключение за 150 s. Финальный независимый acceptance
остаётся UNKNOWN; успешное ревью всего diff не заявляется.

Документация/образы/wheel образуют локальный reviewable результат. Commit/push,
публикация образов, remote CI, production rollout, historical balance backfill,
реальные provider/chain платежи и Telegram user-flow acceptance не выполнялись.
Wallet Pay имеет ручной payout; unsupported spendable API не заявлен. Для Solana
поддержан classic SPL, а oversized multi-account transfer требует консолидации.
Точные новые settlement facts не заменяют сверку прежних Float caches ботов.
Локальные VM, RPC evidence, worker, SQL и bot проверки выполнялись по слоям;
полного единого RPC-validator → worker → Telegram сценария не было.
Совместимость QR с конкретным приложением кошелька также не проверялась.

Локальная сводка образов и проверок: task artifact `release-evidence.json`.
Рабочий протокол: `docs/provider-payments.md`; ботам —
`payment_bot/docs/gateway-integration.md` в соседнем checkout.
