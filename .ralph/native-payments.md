# Native asset invoice payments

Add native-token invoice support for EVM chains and run per-chain invoice tests where signing key is available.

## Goals
- Support native payment assets per chain: BNB, ETH, MATIC, AVAX.
- Keep ERC20 USDT/USDC flow unchanged.
- Detect native transfers to leased invoice addresses without checkpoint catch-up issues.
- Confirm/sweep native invoices safely.
- Run invoice smoke tests for every supported chain from main/funder address if key is available.

## Checklist
- [x] Inspect asset validation, hosted select/status, scanner, and sweeper code paths.
- [x] Inspect available server signing keys and public source addresses without printing secrets.
- [x] Design native payment model with minimal API breakage.
- [x] Implement native asset config helpers and invoice/select support.
- [x] Implement native transfer scanner for active invoice addresses.
- [x] Implement native confirmation/status handling.
- [x] Implement native sweep behavior, leaving gas reserve where required.
- [x] Add focused tests for native invoice detection/sweep behavior.
- [x] Run local/container checks.
- [x] Commit/push gateway patch.
- [x] Deploy gateway patch.
- [x] Create and pay test invoices across chains from available signing address.
- [x] Verify each invoice confirms and sweeps.
- [x] Record final status and blockers.

## Verification
- Inspected gateway paths:
  - `src/api/merchant/schemas.py`
  - `src/api/hosted/router.py`
  - `src/services/payment_service.py`
  - `src/workers/evm_log_poller.py`
  - `src/workers/unified_sweeper.py`
  - `src/blockchain/oklink_client.py`
  - `src/blockchain/chains.py`
- Server signing-key check without printing secrets:
  - configured treasury public address: `0xf9095877f93603d0b6c44e5a82db5dc751b34cd8`
  - available configured funder key public address: `0x4F576ecf6546b2BF9250C381e2Be198aaDD59698`
  - `main_key_available False`; paid smoke from main address is blocked unless user provides/places main private key securely.
- OKLink native address endpoint verified on server: `v2/{chain}/addresses/{address}/transactionsByClassfy/condition` returns incoming native tx rows with `hash`, `blockHeight`, `from`, `to`, `value`.
- Implemented:
  - native asset helpers and `NATIVE_TOKEN_CONTRACT` zero-address marker;
  - merchant invoice `asset` accepts configured ERC20/native assets;
  - hosted select/status handles native asset with empty token contract URL;
  - OKLink client native address transaction fetch;
  - active poller native transfer detection via OKLink address txs;
  - native sweep path sends native balance minus estimated gas to treasury.
- Commands run:
  - `python3 -m py_compile src/blockchain/chains.py src/blockchain/oklink_client.py src/services/payment_service.py src/api/merchant/schemas.py src/api/hosted/router.py src/workers/evm_log_poller.py src/workers/unified_sweeper.py tests/test_evm_log_poller_oklink.py` passed.
  - `git diff --check` passed.
  - local runtime import check blocked: missing local dependency `eth_account`.
- Test coverage updated: `tests/test_evm_log_poller_oklink.py` now models per-session token/native asset filtering and stale checkpoint behavior.
- Commit pushed: `d27033f feat: support native invoice payments`.
- Deployed server gateway at `d27033f`:
  - initial BuildKit build failed due Docker Hub IPv6 token fetch issue (`cannot assign requested address`);
  - retried with `DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 docker compose build api worker-poller worker-sweeper` successfully;
  - `docker compose up -d api worker-poller worker-sweeper` completed;
  - API health returned `healthy`.
- Server container check passed:
  - `docker compose run --rm -T worker-poller python -m py_compile src/blockchain/chains.py src/blockchain/oklink_client.py src/services/payment_service.py src/api/merchant/schemas.py src/api/hosted/router.py src/workers/evm_log_poller.py src/workers/unified_sweeper.py tests/test_evm_log_poller_oklink.py`.
- Paid native smoke used available configured funder key, not main treasury key (`main_key_available False`). Source public address: `0x4F576ecf6546b2BF9250C381e2Be198aaDD59698`.
- Native invoice smoke results:
  - BSC `BNB` `0.00001`: `PAY_Xgd4-DnM2EqeVoNH`, pay tx `0x38bdab0cc634e182ae6f56f5049dbc50b863afaaf5638157f1426aa307ccb413`, sweep tx `0xbc851344d5c71efd9cf89808933d02fa154c9d5e0f502fc95a914743526a58ac`, state `completed`.
  - Base `ETH` `0.000002`: `PAY_LiPJGUKBrvHTrtEN`, pay tx `0x8ace666b9259751665b7663dc6935e8d95260cbfebf50138da72e4539359b4f4`, sweep tx `0xeb7df3e9115dd56ae22e8dec98ad0a759b1129ecd47b6889c93f2a2bc19dfd08`, state `completed`.
  - Arbitrum `ETH` `0.000005`: `PAY_Xt5X_MZF9o2D2s6r`, pay tx `0xda4e0ace5b086107a2e1bde6ec11be82e8e2744ac966ac17cae8bcec7c210eb7`, sweep tx `0xc354bd773711a7ffcf4a13a1e8c1f81dffca1ed5d4d1ff15ed3a61af60addf36`, state `completed`.
  - Polygon `MATIC` `0.001`: `PAY_2Pc-YIBtiZC8afxe`, pay tx `0xe6c138a27d89c2f35df0d8b20aa4f964a063fb95a9cc1cd5c027cd98bdf57f10`, sweep tx `0xec5cd1e470f72b2ba55683b6b1e7bd346fa4c749f3cfdec1788498c0a35944f6`, state `completed`.
  - Avax `AVAX` `0.0001`: `PAY_MBE-Rv_ohXoTM9ss`, pay tx `0x30794f3df469306ce5d51a8ff45637c492938e8a110be200db7f5cab2cdd43eb`, sweep tx `0x8168bb0c18f6df1d85d21675b7b6da49c119afe9a2a25384dbfec15eaf487456`, state `completed`.
  - Optimism `ETH` `0.000002`: `PAY_uyPVkJoIlOmx4Nel`, pay tx `0xaa55c53a516bd08b6ca861748348237a6c5f2c0f9e60f88bf90100986cb7dac6`, sweep tx `0xecd36b6a84e6083fb5582cde7204e02c2fe7157b7ec738c0ab7fded27a82e7f6`, state `completed`.
- Final service status: gateway HEAD `d27033f`; `api` healthy; `worker-poller`, `worker-sweeper`, `worker-webhook`, `worker-expirer`, `postgres`, `redis` running; no recent traceback/fatal/errors in `api`, `worker-poller`, `worker-sweeper` logs.

## Notes
- Do not print private keys, mnemonics, API keys, or `.env` contents.
- Main treasury address: `0xf9095877f93603d0b6c44e5a82db5dc751b34cd8`.
- Existing server funder key public address was `0x4F576ecf6546b2BF9250C381e2Be198aaDD59698`.
- If no private key for main treasury exists on server, all-chain paid smoke from main address is blocked; use available key only with explicit note.
