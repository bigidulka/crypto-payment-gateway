from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from datetime import datetime
from app.models import ChainType, PaymentStatus

class CheckCreate(BaseModel):
    amount: float
    currency: str
    chain: ChainType
    description: Optional[str] = None

class PaymentResponse(BaseModel):
    id: UUID
    wallet_address: str
    status: PaymentStatus
    amount_received: float
    tx_hash_in: Optional[str]
    tx_hash_out: Optional[str]

    class Config:
        from_attributes = True

class CheckResponse(BaseModel):
    id: UUID
    amount: float
    currency: str
    chain: ChainType
    description: Optional[str]
    created_at: datetime
    payment: Optional[PaymentResponse] = None

    class Config:
        from_attributes = True
