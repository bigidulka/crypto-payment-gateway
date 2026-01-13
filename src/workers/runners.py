#!/usr/bin/env python3
"""
Simplified Worker Runners с логированием
Используются в run_full_stack.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, and_, update
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

logger = logging.getLogger(__name__)


class EVMLogPoller:
    """Простой Poller для проверки платежей"""

    async def initialize(self):
        """Инициализация"""
        logger.info("🔍 Инициализирую Poller")
        self.db_url = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./arbitron_payment.db"
        )
        self.engine = create_async_engine(self.db_url, echo=False)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession)

    async def poll_once(self):
        """Один цикл polling"""
        try:
            async with self.session_maker() as session:
                # Проверяем статус инвойсов
                from src.db.models.invoice import Invoice, InvoiceStatus

                result = await session.execute(
                    select(Invoice).where(
                        Invoice.status.in_(
                            [InvoiceStatus.CREATED, InvoiceStatus.AWAITING_PAYMENT]
                        )
                    )
                )
                invoices = result.scalars().all()

                if invoices:
                    logger.info(f"📊 Найдено активных инвойсов: {len(invoices)}")

                    for inv in invoices:
                        if inv.is_expired:
                            logger.warning(f"⏰ Инвойс {inv.public_id} истёк")
                            # Можно обновить статус на EXPIRED

        except Exception as e:
            logger.error(f"❌ Ошибка в поллере: {e}")


class Sweeper:
    """Простой Sweeper для вывода токенов"""

    async def initialize(self):
        """Инициализация"""
        logger.info("💰 Инициализирую Sweeper")
        self.db_url = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./arbitron_payment.db"
        )
        self.engine = create_async_engine(self.db_url, echo=False)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession)

    async def process_once(self):
        """Один цикл sweep"""
        try:
            async with self.session_maker() as session:
                # Проверяем unified sweep jobs
                from src.db.models import UnifiedSweepJob, SweepState

                result = await session.execute(
                    select(UnifiedSweepJob).where(
                        UnifiedSweepJob.state.in_(
                            [
                                SweepState.PENDING_GAS,
                                SweepState.FUNDING,
                                SweepState.SWEEPING,
                            ]
                        )
                    )
                )
                jobs = result.scalars().all()

                if jobs:
                    logger.info(f"💼 Найдено задач на вывод: {len(jobs)}")

                    for job in jobs:
                        logger.info(f"  → {job.state.value}: {job.id}")

        except Exception as e:
            logger.error(f"❌ Ошибка в sweeper: {e}")


class WebhookDispatcher:
    """Простой Webhook Dispatcher"""

    async def initialize(self):
        """Инициализация"""
        logger.info("🔔 Инициализирую Webhook Dispatcher")
        self.db_url = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./arbitron_payment.db"
        )
        self.engine = create_async_engine(self.db_url, echo=False)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession)

    async def dispatch_once(self):
        """Один цикл отправки вебхуков"""
        try:
            async with self.session_maker() as session:
                # Проверяем очередь вебхуков
                from src.db.models.sweep import OutboxWebhook, OutboxStatus

                result = await session.execute(
                    select(OutboxWebhook).where(
                        OutboxWebhook.status == OutboxStatus.PENDING
                    )
                )
                webhooks = result.scalars().all()

                if webhooks:
                    logger.info(f"📨 Найдено вебхуков к отправке: {len(webhooks)}")

                    for webhook in webhooks:
                        logger.info(f"  → {webhook.event_type}: {webhook.invoice_id}")

        except Exception as e:
            logger.error(f"❌ Ошибка в webhook dispatcher: {e}")
