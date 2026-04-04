import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import AsyncSessionLocal
from app.models import Payment, PaymentStatus, Check
from app.services.blockchain import blockchain_service

logger = logging.getLogger(__name__)

class PaymentMonitor:
    def __init__(self):
        self.is_running = False
        self.notify_callback = None # Callback for websocket notifications

    async def start(self):
        self.is_running = True
        logger.info("Starting Payment Monitor...")
        while self.is_running:
            try:
                await self.process_payments()
            except Exception as e:
                logger.error(f"Error in payment monitor loop: {e}")
            await asyncio.sleep(10) # Пауза между проверками

    async def stop(self):
        self.is_running = False
        logger.info("Stopping Payment Monitor...")

    async def _notify(self, check_id, status, amount):
        if self.notify_callback:
            try:
                await self.notify_callback(check_id, status, amount)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")

    async def process_payments(self):
        async with AsyncSessionLocal() as session:
            # 1. Ищем PENDING платежи и проверяем баланс
            await self.check_pending_payments(session)
            
            # 2. Ищем DETECTED платежи (где мы отправили газ) и проверяем дошел ли газ
            await self.check_gas_arrival(session)
            
            # 3. Ищем GAS_SENT платежи и делаем sweep (вывод средств)
            
            # 4. Ищем COMPLETED (или ожидающие подтверждения вывода)
            await self.check_sweep_confirmation(session)

    async def check_pending_payments(self, session: AsyncSession):
        # Выбираем платежи, которые ждут оплаты
        result = await session.execute(
            select(Payment).options(joinedload(Payment.check)).where(Payment.status == PaymentStatus.PENDING)
        )
        payments = result.scalars().all()

        for payment in payments:
            try:
                # Проверяем баланс на кошельке
                balance = blockchain_service.get_balance(
                    payment.check.chain, 
                    payment.wallet_address, 
                    payment.check.currency
                )
                
                # Если баланс >= требуемой суммы (с небольшим допуском на погрешность float)
                if balance >= payment.check.amount:
                    logger.info(f"Payment detected for {payment.id}. Balance: {balance}")
                    payment.amount_received = balance
                    payment.status = PaymentStatus.DETECTED
                    
                    # Сразу отправляем газ для вывода
                    gas_amount = 0.0005 # Хардкод для MVP
                    
                    tx_hash = blockchain_service.send_gas(
                        payment.check.chain,
                        payment.wallet_address,
                        gas_amount
                    )
                    payment.tx_hash_gas = tx_hash
                    logger.info(f"Gas sent: {tx_hash}")
                    
                    await session.commit()
                    await self._notify(payment.check.id, payment.status, payment.amount_received)
            except Exception as e:
                logger.error(f"Error checking payment {payment.id}: {e}")

    async def check_gas_arrival(self, session: AsyncSession):
        # Платежи, где мы увидели деньги и отправили газ, но еще не вывели
        result = await session.execute(
            select(Payment).options(joinedload(Payment.check)).where(Payment.status == PaymentStatus.DETECTED)
        )
        payments = result.scalars().all()
        
        for payment in payments:
            try:
                # Проверяем, подтвердилась ли транзакция газа
                if payment.tx_hash_gas:
                    is_confirmed = blockchain_service.check_tx_status(payment.check.chain, payment.tx_hash_gas)
                    if is_confirmed:
                        logger.info(f"Gas confirmed for {payment.id}. Sweeping tokens...")
                        
                        # Выводим средства
                        tx_hash = blockchain_service.sweep_tokens(
                            payment.check.chain,
                            payment.wallet_private_key,
                            payment.check.currency,
                            payment.amount_received
                        )
                        
                        payment.tx_hash_out = tx_hash
                        payment.status = PaymentStatus.GAS_SENT 
                        await session.commit()
                        await self._notify(payment.check.id, payment.status, payment.amount_received)
            except Exception as e:
                logger.error(f"Error processing gas arrival for {payment.id}: {e}")

    async def check_sweep_confirmation(self, session: AsyncSession):
        # Проверяем подтверждение вывода
        result = await session.execute(
            select(Payment).options(joinedload(Payment.check)).where(Payment.status == PaymentStatus.GAS_SENT)
        )
        payments = result.scalars().all()
        
        for payment in payments:
            try:
                if payment.tx_hash_out:
                    is_confirmed = blockchain_service.check_tx_status(payment.check.chain, payment.tx_hash_out)
                    if is_confirmed:
                        logger.info(f"Sweep confirmed for {payment.id}. Payment Completed.")
                        payment.status = PaymentStatus.COMPLETED
                        await session.commit()
                        await self._notify(payment.check.id, payment.status, payment.amount_received)
            except Exception as e:
                logger.error(f"Error checking sweep for {payment.id}: {e}")

payment_monitor = PaymentMonitor()
