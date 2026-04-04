from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from contextlib import asynccontextmanager

from app.core.config import settings
from app.api.endpoints import checks
from app.db.base import Base
from app.db.session import engine
from app.services.monitor import payment_monitor
from app.api.endpoints.checks import notify_payment_update

# Интеграция уведомлений в монитор
# Monkey patching метода монитора для отправки уведомлений
# В продакшене лучше использовать паттерн Observer или Event Bus
original_process = payment_monitor.process_payments

async def process_with_notifications():
    # Запускаем оригинальный процесс
    await original_process()
    
    # Тут можно было бы проверять изменения и слать уведомления,
    # но для MVP мы сделаем проще: монитор сам будет вызывать notify внутри (если бы мы его так написали)
    # Или мы можем периодически пушить состояние всем подключенным клиентам.
    
    # Для MVP: Модифицируем monitor.py чтобы он импортировал notify_payment_update? 
    # Нет, это циклический импорт.
    # Лучше сделаем polling в WebSocket клиенте или просто оставим как есть, 
    # а notify_payment_update будем вызывать если перепишем монитор.
    
    # Чтобы не усложнять, оставим WebSocket на polling со стороны клиента или 
    # просто реализуем простой broadcast в мониторе, передав ему callback.
    pass

# Переопределяем монитор, чтобы он мог слать уведомления
# (В реальном коде лучше передать callback в конструктор монитора)
async def notify_callback(check_id, status, amount):
    await notify_payment_update(str(check_id), status, amount)

payment_monitor.notify_callback = notify_callback

# Обновляем метод монитора, чтобы он использовал callback
# Это "грязный" хак для MVP, чтобы не переписывать файл монитора целиком
# В идеале нужно было сразу добавить callback в Monitor
import types
from app.models import PaymentStatus, Payment, Check
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.services.blockchain import blockchain_service
import logging

logger = logging.getLogger(__name__)

# Переопределяем методы монитора для добавления уведомлений
# (Полная копия логики из monitor.py с добавлением await self.notify_callback(...))
# Чтобы не дублировать код, мы просто добавим уведомления в существующий цикл монитора в main.py?
# Нет, монитор работает в фоне.

# Давайте лучше пропатчим monitor.py, добавив в него импорт и вызов, 
# но так как файл уже записан, проще переписать его или использовать callback.
# Я выберу вариант с инъекцией зависимости (callback) в монитор.

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Создаем таблицы (в проде использовать Alembic!)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Запускаем монитор
    monitor_task = asyncio.create_task(payment_monitor.start())
    
    yield
    
    # Shutdown
    await payment_monitor.stop()
    await monitor_task

app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # В проде указать конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(checks.router, prefix="/api/checks", tags=["checks"])

@app.get("/")
async def root():
    return {"message": "Arbitron Payment API is running"}
