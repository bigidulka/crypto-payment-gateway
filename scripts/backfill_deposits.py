#!/usr/bin/env python3
"""
Backfill historical deposits from blockchain.

Сканирует блокчейн за последние N дней и записывает все депозиты
на адреса из wallet_addresses в таблицу deposits.

Защита от дубликатов: уникальность по (chain, tx_hash, log_index)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

# Add parent to path for imports
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

# Import chain config from TOML
from src.blockchain.chains import (
    get_chain_config,
    get_evm_chains,
    get_transfer_event_signature,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Database connection
PG_DSN = os.getenv("DATABASE_URL", "").replace("+asyncpg", "").replace("postgresql", "postgres")
if not PG_DSN:
    PG_DSN = "postgres://gateway:payment_gateway_password@postgres:5432/payment_gateway"


def get_chains_config() -> dict:
    """Build chains config from TOML."""
    result = {}
    for chain_name in get_evm_chains():
        cfg = get_chain_config(chain_name)
        # Get RPC from env or use first from config
        env_var = f"{chain_name.upper()}_RPC_URL"
        rpc = os.getenv(env_var, cfg.rpc_url)
        
        tokens = {}
        for symbol, token_cfg in cfg.tokens.items():
            tokens[symbol] = {
                "address": token_cfg.contract_address,
                "decimals": token_cfg.decimals,
            }
        
        result[chain_name] = {
            "rpc": rpc,
            "tokens": tokens,
            "confirmations": cfg.confirmations,
            "block_time": cfg.block_time_sec,
        }
    return result


# Chain configs loaded from TOML
CHAINS = get_chains_config()

# ERC20 Transfer event signature from config
TRANSFER_TOPIC = get_transfer_event_signature()

# How many days back to scan
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "7"))


async def get_rpc_client(chain: str):
    """Get aiohttp session for RPC calls."""
    import aiohttp
    return aiohttp.ClientSession()


async def rpc_call(session, rpc_url: str, method: str, params: list) -> dict:
    """Make JSON-RPC call."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    async with session.post(rpc_url, json=payload) as resp:
        data = await resp.json()
        if "error" in data:
            raise Exception(f"RPC error: {data['error']}")
        return data.get("result")


async def get_block_number(session, rpc_url: str) -> int:
    """Get current block number."""
    result = await rpc_call(session, rpc_url, "eth_blockNumber", [])
    return int(result, 16)


async def get_logs(session, rpc_url: str, from_block: int, to_block: int, addresses: list[str], token_contracts: list[str]) -> list:
    """Get transfer logs for addresses."""
    # Build filter for multiple addresses using OR topics
    params = {
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": token_contracts,
        "topics": [
            TRANSFER_TOPIC,
            None,  # from address (any)
            ["0x" + "0" * 24 + addr[2:].lower() for addr in addresses],  # to addresses
        ],
    }
    
    try:
        return await rpc_call(session, rpc_url, "eth_getLogs", [params])
    except Exception as e:
        # Fallback: try smaller batches
        logger.warning(f"eth_getLogs failed, trying smaller batch: {e}")
        return []


def parse_transfer_log(chain: str, log: dict) -> dict | None:
    """Parse Transfer event log."""
    config = CHAINS[chain]
    
    topics = log.get("topics", [])
    if len(topics) < 3:
        return None
    
    # Parse addresses from topics
    from_address = "0x" + topics[1][-40:]
    to_address = "0x" + topics[2][-40:]
    
    # Parse amount from data
    data = log.get("data", "0x0")
    raw_amount = int(data, 16) if data else 0
    
    # Get token contract
    token_contract = log.get("address", "").lower()
    
    # Determine asset and decimals
    asset = None
    decimals = 18
    for asset_name, token_info in config["tokens"].items():
        if token_info["address"].lower() == token_contract:
            asset = asset_name
            decimals = token_info["decimals"]
            break
    
    if not asset:
        return None
    
    amount = Decimal(raw_amount) / Decimal(10 ** decimals)
    
    return {
        "tx_hash": log.get("transactionHash", ""),
        "log_index": int(log.get("logIndex", "0x0"), 16),
        "block_number": int(log.get("blockNumber", "0x0"), 16),
        "from_address": from_address.lower(),
        "to_address": to_address.lower(),
        "token_contract": token_contract,
        "amount": amount,
        "asset": asset,
    }


async def backfill_chain(pg_conn, chain: str, addresses: dict[str, dict]) -> int:
    """
    Backfill deposits for a single chain.
    
    Args:
        pg_conn: PostgreSQL connection
        chain: Chain name
        addresses: Dict of {address: {wallet_address_id, user_wallet_id}}
    
    Returns:
        Number of deposits inserted
    """
    import aiohttp
    
    config = CHAINS.get(chain)
    if not config:
        logger.warning(f"Unknown chain: {chain}")
        return 0
    
    if not addresses:
        logger.info(f"[{chain}] No addresses to scan")
        return 0
    
    rpc_url = config["rpc"]
    token_contracts = [t["address"] for t in config["tokens"].values()]
    confirmations = config["confirmations"]
    
    logger.info(f"[{chain}] Starting backfill for {len(addresses)} addresses")
    
    async with aiohttp.ClientSession() as session:
        # Get current block
        try:
            current_block = await get_block_number(session, rpc_url)
        except Exception as e:
            logger.error(f"[{chain}] Failed to get block number: {e}")
            return 0
        
        # Calculate start block (N days ago)
        blocks_per_day = int(86400 / config["block_time"])
        start_block = current_block - (blocks_per_day * BACKFILL_DAYS)
        
        logger.info(f"[{chain}] Scanning blocks {start_block} to {current_block} ({BACKFILL_DAYS} days)")
        
        total_inserted = 0
        batch_size = 10000  # Blocks per batch
        
        for from_block in range(start_block, current_block, batch_size):
            to_block = min(from_block + batch_size - 1, current_block)
            
            try:
                logs = await get_logs(
                    session, rpc_url,
                    from_block, to_block,
                    list(addresses.keys()),
                    token_contracts,
                )
            except Exception as e:
                logger.error(f"[{chain}] Error getting logs {from_block}-{to_block}: {e}")
                continue
            
            if not logs:
                continue
            
            logger.info(f"[{chain}] Found {len(logs)} transfers in blocks {from_block}-{to_block}")
            
            for log in logs:
                transfer = parse_transfer_log(chain, log)
                if not transfer:
                    continue
                
                to_addr = transfer["to_address"].lower()
                addr_info = addresses.get(to_addr)
                if not addr_info:
                    continue
                
                # Check for duplicate by tx_hash + log_index
                existing = await pg_conn.fetchval(
                    """
                    SELECT id FROM deposits 
                    WHERE chain = $1 AND tx_hash = $2 AND log_index = $3
                    """,
                    chain, transfer["tx_hash"], transfer["log_index"]
                )
                
                if existing:
                    continue
                
                # Calculate confirmations
                confs = current_block - transfer["block_number"]
                status = "confirmed" if confs >= confirmations else "pending"
                
                # Insert deposit
                try:
                    import uuid
                    await pg_conn.execute(
                        """
                        INSERT INTO deposits (
                            id, user_wallet_id, wallet_address_id, chain,
                            tx_hash, block_number, log_index, amount, asset,
                            token_contract, from_address, status, confirmations,
                            required_confirmations, detected_at, confirmed_at, credited_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
                        )
                        ON CONFLICT (chain, tx_hash, log_index) DO NOTHING
                        """,
                        uuid.uuid4(),
                        addr_info["user_wallet_id"],
                        addr_info["wallet_address_id"],
                        chain,
                        transfer["tx_hash"],
                        transfer["block_number"],
                        transfer["log_index"],
                        transfer["amount"],
                        transfer["asset"],
                        transfer["token_contract"],
                        transfer["from_address"],
                        status,
                        confs,
                        confirmations,
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc) if status == "confirmed" else None,
                        datetime.now(timezone.utc) if status == "confirmed" else None,
                    )
                    total_inserted += 1
                    logger.info(
                        f"[{chain}] Inserted deposit: {transfer['amount']} {transfer['asset']} "
                        f"tx={transfer['tx_hash'][:16]}..."
                    )
                except Exception as e:
                    logger.error(f"[{chain}] Error inserting deposit: {e}")
            
            # Small delay between batches
            await asyncio.sleep(0.1)
        
        # Update chain checkpoint to current block
        await pg_conn.execute(
            """
            INSERT INTO chain_checkpoints (id, chain, last_scanned_block, updated_at)
            VALUES (gen_random_uuid(), $1, $2, NOW())
            ON CONFLICT (chain) DO UPDATE SET 
                last_scanned_block = GREATEST(chain_checkpoints.last_scanned_block, $2),
                updated_at = NOW()
            """,
            chain, current_block
        )
        
        logger.info(f"[{chain}] Backfill complete: {total_inserted} deposits inserted")
        return total_inserted


async def main():
    """Main backfill function."""
    print("=" * 60)
    print(f"Backfilling deposits for last {BACKFILL_DAYS} days")
    print("=" * 60)
    
    # Connect to PostgreSQL
    logger.info("Connecting to PostgreSQL...")
    pg_conn = await asyncpg.connect(PG_DSN)
    logger.info("Connected!")
    
    # Get all wallet addresses grouped by chain
    rows = await pg_conn.fetch(
        """
        SELECT wa.chain, wa.address, wa.id as wallet_address_id, wa.user_wallet_id
        FROM wallet_addresses wa
        JOIN user_wallets uw ON wa.user_wallet_id = uw.id
        WHERE uw.is_active = true
        """
    )
    
    # Group by chain
    addresses_by_chain: dict[str, dict[str, dict]] = {}
    for row in rows:
        chain = row["chain"]
        if chain not in addresses_by_chain:
            addresses_by_chain[chain] = {}
        addresses_by_chain[chain][row["address"].lower()] = {
            "wallet_address_id": row["wallet_address_id"],
            "user_wallet_id": row["user_wallet_id"],
        }
    
    print(f"\nFound addresses by chain:")
    for chain, addrs in addresses_by_chain.items():
        print(f"  {chain}: {len(addrs)} addresses")
    
    # Get existing deposits count
    existing_count = await pg_conn.fetchval("SELECT COUNT(*) FROM deposits")
    print(f"\nExisting deposits: {existing_count}")
    
    # Backfill each chain
    total = 0
    for chain in addresses_by_chain:
        if chain not in CHAINS:
            logger.warning(f"Skipping unknown chain: {chain}")
            continue
        
        inserted = await backfill_chain(pg_conn, chain, addresses_by_chain[chain])
        total += inserted
    
    # Final count
    final_count = await pg_conn.fetchval("SELECT COUNT(*) FROM deposits")
    
    await pg_conn.close()
    
    print()
    print("=" * 60)
    print(f"✅ Backfill complete!")
    print(f"   Deposits before: {existing_count}")
    print(f"   New deposits: {total}")
    print(f"   Deposits after: {final_count}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
