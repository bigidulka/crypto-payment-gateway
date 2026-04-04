from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models import Check, Payment, PaymentStatus
from app.schemas import CheckCreate, CheckResponse
from app.services.blockchain import blockchain_service
import uuid
from typing import List

router = APIRouter()

# Менеджер соединений WebSocket
class ConnectionManager:
    def __init__(self):
        # check_id -> list of websockets
        self.active_connections: dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, check_id: str):
        await websocket.accept()
        if check_id not in self.active_connections:
            self.active_connections[check_id] = []
        self.active_connections[check_id].append(websocket)

    def disconnect(self, websocket: WebSocket, check_id: str):
        if check_id in self.active_connections:
            if websocket in self.active_connections[check_id]:
                self.active_connections[check_id].remove(websocket)

    async def broadcast(self, check_id: str, message: dict):
        if check_id in self.active_connections:
            for connection in self.active_connections[check_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    # Если соединение мертвое, можно удалить (упрощенно)
                    pass

manager = ConnectionManager()

@router.post("/", response_model=CheckResponse)
async def create_check(check_in: CheckCreate, db: AsyncSession = Depends(get_db)):
    """Создание нового чека владельцем."""
    new_check = Check(
        amount=check_in.amount,
        currency=check_in.currency,
        chain=check_in.chain,
        description=check_in.description
    )
    db.add(new_check)
    await db.commit()
    await db.refresh(new_check)
    return new_check

@router.get("/{check_id}", response_model=CheckResponse)
async def get_check(check_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Получение информации о чеке."""
    result = await db.execute(select(Check).where(Check.id == check_id).outerjoin(Payment))
    check = result.scalars().first()
    if not check:
        raise HTTPException(status_code=404, detail="Check not found")
    return check

@router.post("/{check_id}/pay", response_model=CheckResponse)
async def initiate_payment(check_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Пользователь нажимает 'Оплатить', генерируется кошелек."""
    result = await db.execute(select(Check).where(Check.id == check_id).outerjoin(Payment))
    check = result.scalars().first()
    if not check:
        raise HTTPException(status_code=404, detail="Check not found")
    
    if check.payment:
        return check # Уже есть платеж, возвращаем его
    
    # Генерируем кошелек
    address, private_key = blockchain_service.create_wallet()
    
    new_payment = Payment(
        check_id=check.id,
        wallet_address=address,
        wallet_private_key=private_key,
        status=PaymentStatus.PENDING
    )
    
    db.add(new_payment)
    await db.commit()
    await db.refresh(check) # Обновляем check, чтобы подтянулся payment
    
    return check

@router.websocket("/ws/{check_id}")
async def websocket_endpoint(websocket: WebSocket, check_id: str):
    """WebSocket для отслеживания статуса."""
    await manager.connect(websocket, check_id)
    try:
        while True:
            # Просто держим соединение, данные пушим из монитора (в идеале)
            # Или клиент может пинговать
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, check_id)

# Функция для вызова из монитора при смене статуса
async def notify_payment_update(check_id: str, status: str, amount: float):
    await manager.broadcast(check_id, {"status": status, "amount_received": amount})
