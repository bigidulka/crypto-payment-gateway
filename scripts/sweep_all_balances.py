#!/usr/bin/env python3
"""
Скрипт для вывода ВСЕХ балансов со всех кошельков.

Функционал:
1. Находит все кошельки с балансами (стейблы и газ)
2. Выводит стейблы на treasury
3. Выводит оставшийся газ на funder wallet
4. Сбрасывает неудачные sweep jobs

Использование:
    # Dry run - только показать что будет сделано
    python scripts/sweep_all_balances.py --dry-run
    
    # Выполнить sweep всех балансов
    python scripts/sweep_all_balances.py --execute
    
    # Только сбросить неудачные jobs
    python scripts/sweep_all_balances.py --reset-failed
    
    # Только вывести газ
    python scripts/sweep_all_balances.py --sweep-gas-only --execute
"""

import asyncio
import argparse
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

# Добавляем путь к проекту
sys.path.insert(0, "/home/fsdf1234/Projects/crypto-payment-gateway")

from sqlalchemy import select, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.blockchain.chains import get_chain_config, get_evm_chains
from src.blockchain.evm_adapter import get_evm_adapter, EvmAdapter
from src.core.config import get_settings
from src.crypto.encryption import decrypt_private_key
from src.db.models import (
    Wallet, WalletType, 
    UnifiedSweepJob, SweepState,
    Deposit, DepositStatus,
)
from src.db.session import get_session_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class WalletBalance:
    """Баланс кошелька."""
    wallet_id: str
    address: str
    chain: str
    native_balance: Decimal  # ETH/BNB etc
    native_balance_wei: int
    token_balances: dict[str, tuple[Decimal, int, str]]  # symbol -> (amount, raw, contract)
    encrypted_private_key: bytes


@dataclass
class SweepPlan:
    """План sweep операций."""
    chain: str
    address: str
    token_sweeps: list[tuple[str, Decimal, str]]  # [(symbol, amount, contract), ...]
    gas_sweep: Optional[tuple[Decimal, int]]  # (amount, wei) - native token to sweep
    estimated_gas_cost: int
    private_key: str


class BalanceSweeper:
    """Класс для вывода всех балансов."""
    
    def __init__(self):
        self.settings = get_settings()
        self.encryption_key = self.settings.encryption_key.get_secret_value()
        self.funder_key = self.settings.funder_private_key.get_secret_value()
        self._adapters: dict[str, EvmAdapter] = {}
    
    def get_adapter(self, chain: str) -> EvmAdapter:
        if chain not in self._adapters:
            self._adapters[chain] = get_evm_adapter(chain)
        return self._adapters[chain]
    
    async def get_funder_address(self, chain: str) -> str:
        """Получить адрес funder кошелька."""
        adapter = self.get_adapter(chain)
        return adapter.private_key_to_address(self.funder_key)
    
    async def get_treasury_address(self, chain: str) -> str:
        """Получить treasury адрес для сети."""
        # Сначала проверяем есть ли в настройках
        # Если нет - используем funder как treasury
        config = get_chain_config(chain)
        if config.treasury_address:
            return config.treasury_address
        # Fallback на funder
        return await self.get_funder_address(chain)
    
    async def find_all_wallets_with_balance(self) -> list[WalletBalance]:
        """Найти все кошельки с балансами."""
        wallets_with_balance = []
        
        async with get_session_context() as session:
            # Получаем все активные EVM кошельки
            stmt = select(Wallet).where(
                Wallet.chain.in_(get_evm_chains()),
                Wallet.is_active == True,
            )
            result = await session.execute(stmt)
            wallets = result.scalars().all()
            
            logger.info(f"Found {len(wallets)} active wallets to check")
            
            # Группируем по chain для batch запросов
            by_chain: dict[str, list[Wallet]] = {}
            for w in wallets:
                if w.chain not in by_chain:
                    by_chain[w.chain] = []
                by_chain[w.chain].append(w)
            
            for chain, chain_wallets in by_chain.items():
                config = get_chain_config(chain)
                adapter = self.get_adapter(chain)
                
                addresses = [w.address for w in chain_wallets]
                token_contracts = [t.contract_address for t in config.tokens.values()]
                
                logger.info(f"[{chain}] Checking {len(addresses)} wallets...")
                
                # Batch запрос native балансов
                try:
                    native_balances = await adapter.get_native_balances_batch(addresses)
                except Exception as e:
                    logger.error(f"[{chain}] Failed to get native balances: {e}")
                    native_balances = {}
                
                # Batch запрос token балансов
                try:
                    token_balances = await adapter.get_balances_batch(addresses, token_contracts)
                except Exception as e:
                    logger.error(f"[{chain}] Failed to get token balances: {e}")
                    token_balances = {}
                
                for wallet in chain_wallets:
                    addr_lower = wallet.address.lower()
                    native = native_balances.get(addr_lower, Decimal(0))
                    tokens = token_balances.get(addr_lower, {})
                    
                    # Проверяем есть ли ненулевые балансы
                    has_native = native > Decimal("0.0000001")  # Dust threshold
                    has_tokens = any(
                        amount > Decimal("0.001") 
                        for amount in tokens.values()
                    )
                    
                    if has_native or has_tokens:
                        # Формируем token_balances dict
                        token_dict = {}
                        for symbol, token_config in config.tokens.items():
                            contract = token_config.contract_address.lower()
                            amount = tokens.get(contract, Decimal(0))
                            if amount > Decimal("0.001"):
                                raw = int(amount * Decimal(10 ** token_config.decimals))
                                token_dict[symbol] = (amount, raw, token_config.contract_address)
                        
                        wallets_with_balance.append(WalletBalance(
                            wallet_id=str(wallet.id),
                            address=wallet.address,
                            chain=chain,
                            native_balance=native,
                            native_balance_wei=int(native * Decimal(10**18)),
                            token_balances=token_dict,
                            encrypted_private_key=wallet.encrypted_private_key,
                        ))
        
        return wallets_with_balance
    
    async def create_sweep_plans(
        self, 
        wallets: list[WalletBalance],
        sweep_gas: bool = True,
    ) -> list[SweepPlan]:
        """Создать планы sweep для всех кошельков."""
        plans = []
        
        for wallet in wallets:
            adapter = self.get_adapter(wallet.chain)
            config = get_chain_config(wallet.chain)
            treasury = await self.get_treasury_address(wallet.chain)
            funder = await self.get_funder_address(wallet.chain)
            
            # Расшифровываем ключ
            try:
                privkey = decrypt_private_key(
                    wallet.encrypted_private_key, 
                    self.encryption_key
                )
            except Exception as e:
                logger.error(f"[{wallet.chain}] Failed to decrypt key for {wallet.address}: {e}")
                continue
            
            token_sweeps = []
            total_gas_needed = 0
            
            # Планируем sweep токенов
            for symbol, (amount, raw, contract) in wallet.token_balances.items():
                # Оцениваем газ для этого sweep
                gas_cost = await adapter.estimate_sweep_gas_cost(
                    token_contract=contract,
                    from_address=wallet.address,
                    to_address=treasury,
                    amount=raw,
                )
                if gas_cost:
                    total_gas_needed += gas_cost
                else:
                    total_gas_needed += config.max_gas_cost_wei
                
                token_sweeps.append((symbol, amount, contract))
            
            # Газ который останется после sweep токенов
            gas_remaining = wallet.native_balance_wei - total_gas_needed
            
            gas_sweep = None
            if sweep_gas and gas_remaining > 21000 * 5_000_000_000:  # > 0.000105 ETH
                # Вычисляем сколько можно вывести (за вычетом gas на сам transfer)
                # 21000 * gas_price * 1.5 (буфер)
                gas_price = await adapter.get_gas_price() or 5_000_000_000
                transfer_cost = int(21000 * gas_price * 1.5)
                sweepable_gas = gas_remaining - transfer_cost
                
                if sweepable_gas > 0:
                    gas_sweep = (
                        Decimal(sweepable_gas) / Decimal(10**18),
                        sweepable_gas,
                    )
            
            if token_sweeps or gas_sweep:
                plans.append(SweepPlan(
                    chain=wallet.chain,
                    address=wallet.address,
                    token_sweeps=token_sweeps,
                    gas_sweep=gas_sweep,
                    estimated_gas_cost=total_gas_needed,
                    private_key=privkey,
                ))
        
        return plans
    
    async def execute_sweep_plans(
        self, 
        plans: list[SweepPlan],
        dry_run: bool = True,
    ) -> tuple[int, int]:
        """Выполнить планы sweep."""
        success = 0
        failed = 0
        
        for plan in plans:
            adapter = self.get_adapter(plan.chain)
            config = get_chain_config(plan.chain)
            treasury = await self.get_treasury_address(plan.chain)
            funder = await self.get_funder_address(plan.chain)
            
            # Sweep токенов
            for symbol, amount, contract in plan.token_sweeps:
                if dry_run:
                    logger.info(
                        f"[{plan.chain}] DRY RUN: Would sweep {amount} {symbol} "
                        f"from {plan.address[:10]}... to {treasury[:10]}..."
                    )
                    success += 1
                else:
                    try:
                        # Получаем актуальный баланс
                        actual_raw = await adapter.get_erc20_balance_raw(
                            plan.address, contract
                        )
                        if actual_raw <= 0:
                            logger.warning(f"[{plan.chain}] {plan.address}: {symbol} balance already 0")
                            continue
                        
                        # Проверяем/отправляем газ если нужно
                        native_wei = await adapter.get_native_balance_wei(plan.address)
                        gas_cost = await adapter.estimate_sweep_gas_cost(
                            token_contract=contract,
                            from_address=plan.address,
                            to_address=treasury,
                            amount=actual_raw,
                        ) or config.max_gas_cost_wei
                        
                        if native_wei < gas_cost:
                            shortfall = gas_cost - native_wei
                            logger.info(f"[{plan.chain}] Funding {plan.address[:10]}... with {Decimal(shortfall)/Decimal(10**18):.6f} {config.native_symbol}")
                            
                            tx = await adapter.send_native_transfer(
                                self.funder_key,
                                plan.address,
                                int(shortfall * 1.05),
                            )
                            if tx:
                                logger.info(f"[{plan.chain}] Gas funding tx: {tx[:16]}...")
                                await asyncio.sleep(5)  # Ждём подтверждения
                            else:
                                logger.error(f"[{plan.chain}] Failed to fund gas")
                                failed += 1
                                continue
                        
                        # Sweep токена
                        tx_hash = await adapter.send_erc20_transfer(
                            from_private_key=plan.private_key,
                            token_contract=contract,
                            to_address=treasury,
                            amount=actual_raw,
                        )
                        
                        if tx_hash:
                            actual_amount = Decimal(actual_raw) / Decimal(10 ** config.tokens[symbol].decimals)
                            logger.info(
                                f"[{plan.chain}] Swept {actual_amount} {symbol} "
                                f"from {plan.address[:10]}... tx={tx_hash[:16]}..."
                            )
                            success += 1
                        else:
                            logger.error(f"[{plan.chain}] Failed to sweep {symbol} from {plan.address}")
                            failed += 1
                            
                    except Exception as e:
                        logger.error(f"[{plan.chain}] Error sweeping {symbol}: {e}")
                        failed += 1
            
            # Sweep газа
            if plan.gas_sweep:
                amount, wei = plan.gas_sweep
                if dry_run:
                    logger.info(
                        f"[{plan.chain}] DRY RUN: Would sweep {amount:.6f} {config.native_symbol} "
                        f"from {plan.address[:10]}... to {funder[:10]}..."
                    )
                    success += 1
                else:
                    try:
                        # Получаем актуальный баланс
                        native_wei = await adapter.get_native_balance_wei(plan.address)
                        gas_price = await adapter.get_gas_price() or 5_000_000_000
                        transfer_cost = int(21000 * gas_price * 1.5)
                        sweepable = native_wei - transfer_cost
                        
                        if sweepable > 0:
                            tx_hash = await adapter.send_native_transfer(
                                plan.private_key,
                                funder,
                                sweepable,
                            )
                            
                            if tx_hash:
                                actual_amount = Decimal(sweepable) / Decimal(10**18)
                                logger.info(
                                    f"[{plan.chain}] Swept {actual_amount:.6f} {config.native_symbol} "
                                    f"from {plan.address[:10]}... tx={tx_hash[:16]}..."
                                )
                                success += 1
                            else:
                                logger.error(f"[{plan.chain}] Failed to sweep gas from {plan.address}")
                                failed += 1
                        else:
                            logger.warning(f"[{plan.chain}] {plan.address}: not enough gas to sweep")
                            
                    except Exception as e:
                        logger.error(f"[{plan.chain}] Error sweeping gas: {e}")
                        failed += 1
        
        return success, failed
    
    async def reset_failed_jobs(self) -> int:
        """Сбросить все неудачные sweep jobs в PENDING_GAS."""
        async with get_session_context() as session:
            # Находим failed и stuck jobs
            stmt = (
                update(UnifiedSweepJob)
                .where(
                    or_(
                        UnifiedSweepJob.state == SweepState.FAILED,
                        # Stuck в FUNDING/SWEEPING более 1 часа
                        and_(
                            UnifiedSweepJob.state.in_([SweepState.FUNDING, SweepState.SWEEPING]),
                            UnifiedSweepJob.updated_at < datetime.now(timezone.utc).replace(
                                hour=datetime.now(timezone.utc).hour - 1
                            ),
                        ),
                    )
                )
                .values(
                    state=SweepState.PENDING_GAS,
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            
            count = result.rowcount
            logger.info(f"Reset {count} failed/stuck jobs to PENDING_GAS")
            return count
    
    async def show_summary(self, wallets: list[WalletBalance]):
        """Показать сводку по балансам."""
        print("\n" + "="*80)
        print("WALLET BALANCES SUMMARY")
        print("="*80)
        
        by_chain: dict[str, list[WalletBalance]] = {}
        for w in wallets:
            if w.chain not in by_chain:
                by_chain[w.chain] = []
            by_chain[w.chain].append(w)
        
        total_usd_value = Decimal(0)
        
        for chain, chain_wallets in sorted(by_chain.items()):
            config = get_chain_config(chain)
            print(f"\n{config.name} ({chain}):")
            print("-" * 60)
            
            chain_native = Decimal(0)
            chain_tokens: dict[str, Decimal] = {}
            
            for w in chain_wallets:
                chain_native += w.native_balance
                for symbol, (amount, _, _) in w.token_balances.items():
                    chain_tokens[symbol] = chain_tokens.get(symbol, Decimal(0)) + amount
            
            print(f"  Wallets with balance: {len(chain_wallets)}")
            print(f"  Native ({config.native_symbol}): {chain_native:.6f}")
            
            for symbol, amount in sorted(chain_tokens.items()):
                print(f"  {symbol}: {amount:.2f}")
                if symbol in ("USDT", "USDC"):
                    total_usd_value += amount
        
        print("\n" + "="*80)
        print(f"TOTAL STABLECOINS: ${total_usd_value:.2f}")
        print("="*80 + "\n")


async def main():
    parser = argparse.ArgumentParser(description="Sweep all balances from wallets")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be done")
    parser.add_argument("--execute", action="store_true", help="Actually execute sweeps")
    parser.add_argument("--reset-failed", action="store_true", help="Reset failed jobs")
    parser.add_argument("--sweep-gas-only", action="store_true", help="Only sweep native tokens")
    parser.add_argument("--no-gas-sweep", action="store_true", help="Don't sweep native tokens")
    
    args = parser.parse_args()
    
    if not args.dry_run and not args.execute and not args.reset_failed:
        print("Please specify --dry-run, --execute, or --reset-failed")
        parser.print_help()
        return
    
    sweeper = BalanceSweeper()
    
    if args.reset_failed:
        count = await sweeper.reset_failed_jobs()
        print(f"Reset {count} failed jobs")
        if not args.execute and not args.dry_run:
            return
    
    # Находим кошельки с балансами
    logger.info("Scanning wallets for balances...")
    wallets = await sweeper.find_all_wallets_with_balance()
    
    if not wallets:
        print("No wallets with balances found")
        return
    
    # Показываем сводку
    await sweeper.show_summary(wallets)
    
    # Создаём планы
    plans = await sweeper.create_sweep_plans(
        wallets,
        sweep_gas=not args.no_gas_sweep,
    )
    
    if args.sweep_gas_only:
        # Оставляем только gas sweep
        for plan in plans:
            plan.token_sweeps = []
    
    # Выполняем
    if args.dry_run:
        print("\nDRY RUN MODE - No actual transactions will be sent\n")
        success, failed = await sweeper.execute_sweep_plans(plans, dry_run=True)
        print(f"\nDry run complete: {success} operations would succeed")
    elif args.execute:
        confirm = input("\nThis will execute real transactions. Type 'yes' to confirm: ")
        if confirm.lower() != "yes":
            print("Aborted")
            return
        
        print("\nExecuting sweeps...\n")
        success, failed = await sweeper.execute_sweep_plans(plans, dry_run=False)
        print(f"\nSweep complete: {success} succeeded, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
