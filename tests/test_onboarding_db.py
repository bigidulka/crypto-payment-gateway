"""Real isolated-PostgreSQL acceptance tests for admin merchant onboarding."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.api.admin.router import create_merchant
from src.api.admin.schemas import MerchantCreateRequest
from src.db.models import ApiKey, Merchant


@pytest.mark.asyncio
async def test_concurrent_normalized_email_creates_exactly_one_merchant_and_key(test_session):
    """The database unique constraint, not a pre-check, decides the race."""
    session_factory = async_sessionmaker(test_session.bind, expire_on_commit=False)
    payloads = (
        MerchantCreateRequest(name="First", email="Race@Example.TEST"),
        MerchantCreateRequest(name="Second", email=" race@example.test "),
    )

    async def create(payload):
        async with session_factory() as session:
            try:
                return await create_merchant(True, session, payload)
            except HTTPException as exc:
                return exc

    first, second = await asyncio.gather(*(create(payload) for payload in payloads))
    outcomes = (first, second)
    successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409

    merchant_count = await test_session.scalar(
        select(func.count()).select_from(Merchant).where(Merchant.email == "race@example.test")
    )
    key_count = await test_session.scalar(select(func.count()).select_from(ApiKey))
    assert merchant_count == 1
    assert key_count == 1
