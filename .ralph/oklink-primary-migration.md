# OKLink Primary Deposit Scanner Migration

Перенос persistent deposit scanner с RPC `eth_getLogs` на OKLink как основной источник входящих ERC-20/BEP-20 депозитов. RPC оставить для head block, balances, gas, signed tx/sweeps.

## Goals
- OKLink primary scanner для incoming persistent deposits.
- RPC log scanner убрать из hot path, не использовать как fallback автоматически.
- Checkpoint двигать только после complete OKLink scan.
- Config-driven включение scanner provider, без секретов в коде.
- Тесты для scanner, poller integration, checkpoint behavior.

## Checklist
- [x] Зафиксировать текущую структуру `persistent_poller.py`, config, tests.
- [x] Добавить config для OKLink scanner/provider.
- [x] Подключить OKLink scanner в `persistent_poller.py` как primary path.
- [x] Убрать automatic RPC fallback из active path при `scanner_provider=oklink`.
- [x] Добавить tests на OKLink poller path и checkpoint.
- [x] Запустить targeted ruff/pytest.
- [x] Подготовить инструкции server rollout/backfill.
- [x] Проверить полный diff на лишние изменения и config gaps.
- [x] Финальная проверка working tree + оформить next actions для deploy.

## Verification
- `PYTHONPATH=. uv run --no-project --with ruff ruff check --select E501,F401,I001 src/blockchain/oklink_client.py src/blockchain/chains.py src/core/config.py src/workers/persistent_poller.py tests/test_oklink_client.py tests/test_persistent_poller_oklink.py` — PASS.
- `git diff --check -- .env.example config/chains.toml src/blockchain/chains.py src/core/config.py src/workers/persistent_poller.py src/blockchain/oklink_client.py tests/test_oklink_client.py tests/test_persistent_poller_oklink.py` — PASS.
- `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with httpx --with web3 --with pydantic-settings --with sqlalchemy --with asyncpg --with greenlet --with cryptography --with mnemonic --with hdwallets --with redis pytest tests/test_oklink_client.py tests/test_persistent_poller_oklink.py -q` — `8 passed, 1 warning`.
- Live OKLink small BSC scan via `OKLinkTransferLogFetcher` with tight caps: `{'complete': False, 'failed': 1, 'logs': 1, 'method': 'oklink_address_token_transfers'}`. Это ожидаемо: completeness guard не двигает checkpoint при page-cap truncation, но raw log найден.
- `rg -n "TODO|FIXME|placeholder|stub" ...` on changed code/config/test paths — no matches.

## Rollout / Backfill Plan
1. Local/GH only: commit + push code changes. Server source edits forbidden.
2. Server preflight: verify Docker/load/disk; check current `arbitron-payment` branch/status.
3. Configure server env before restart: `OKLINK_BASE_URL`, `OKLINK_API_PREFIX`, `OKLINK_REFERER`, `OKLINK_USER_AGENT`, `OKLINK_WEB_KEY`, `OKLINK_API_KEY_TIME_SHIFT_MS`, `OKLINK_REQUEST_TIMEOUT_SECONDS`.
4. Deploy worker image: `git pull --ff-only`, rebuild `worker-persistent`, restart only persistent worker first.
5. BSC canary: temporarily run/restart only BSC persistent worker if compose supports per-service split; otherwise monitor logs for BSC scan completion and checkpoint advance.
6. Backfill: from current stuck `wallet_addresses.last_scanned_block`/last BSC deposit to current safe block using OKLink path; idempotent by `(chain, tx_hash, log_index)`. Keep checkpoints unmoved on OKLink errors or OKLink page-cap truncation.
7. Monitor: deposits inserted, `unified_sweep_jobs` created, no `eth_getLogs` RPC overload, OKLink 429/403 rate, scanner lag blocks.
8. Expand: after BSC stable, keep all configured EVM chains on OKLink provider.

## Notes
- Добавлен `src/blockchain/oklink_client.py`.
- Добавлен `tests/test_persistent_poller_oklink.py`; старый CRLF `tests/test_persistent_poller_checkpoint.py` восстановлен без изменений, чтобы не шуметь diff.
- `config/chains.toml`: active EVM chains переведены на `scanner_provider = "oklink"` с OKLink chain slug и page/rate limits.
- `persistent_poller.py`: при `scanner_provider=oklink` не вызывает RPC resilient/adaptor fallback для logs; ошибка OKLink = incomplete scan + checkpoint не двигается.
- OKLink completeness guard добавлен: если address pages или tx log pages упёрлись в configured cap, result incomplete, checkpoint не двигается.
- `OKLINK_API_KEY_TIME_SHIFT_MS` вынесен в env/settings, чтобы не хардкодить алгоритмический offset.
- `.env.example`: добавлены OKLink env placeholders; реальные значения не печатались.
- Есть pre-existing staged изменения в `.env.example` и `src/core/config.py`; наши изменения поверх них unstaged. Не трогать staging без решения пользователя.
- Server source of truth остаётся `/home/server/Projects/arbitron-payment`; локально готовим код, не правим server напрямую.

## Final Working Tree Facts
- Modified tracked: `.env.example`, `config/chains.toml`, `src/blockchain/chains.py`, `src/core/config.py`, `src/workers/persistent_poller.py`.
- New files: `src/blockchain/oklink_client.py`, `tests/test_oklink_client.py`, `tests/test_persistent_poller_oklink.py`, `.ralph/oklink-primary-migration.md`.
- Pre-existing staged entries remain: `.env.example`, `src/core/config.py`.
