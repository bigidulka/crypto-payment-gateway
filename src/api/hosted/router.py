"""
Hosted Payment Pages Router.
Публичные эндпоинты для плательщиков.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from src.api.deps import SessionDep
from src.api.hosted.schemas import (
    InvoiceInfoResponse,
    PaymentSelectRequest,
    PaymentSelectResponse,
    PaymentStatusResponse,
)
from src.blockchain.chains import get_chain_config
from src.core.exceptions import InvoiceExpiredError, InvoiceNotFoundError, PaymentError
from src.db.models import InvoiceStatus
from src.services.invoice_service import InvoiceService
from src.services.payment_service import PaymentService

router = APIRouter(prefix="/pay", tags=["Hosted Payment"])


def _is_expired(expires_at: datetime) -> bool:
    """Проверка истечения срока с учётом timezone-naive дат из SQLite."""
    if expires_at.tzinfo is None:
        # SQLite возвращает naive datetime, считаем что это UTC
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > expires_at


@router.get(
    "/{public_id}/info",
    response_model=InvoiceInfoResponse,
    summary="Информация об инвойсе",
)
async def get_invoice_info(
    public_id: str,
    session: SessionDep,
) -> InvoiceInfoResponse:
    """Получить информацию об инвойсе для hosted страницы."""
    invoice_service = InvoiceService(session)

    try:
        invoice = await invoice_service.get_invoice_by_public_id(public_id)
    except InvoiceNotFoundError:
        raise HTTPException(status_code=404, detail="Invoice not found")

    is_expired = _is_expired(invoice.expires_at)

    # Ищем активную payment session
    selected_chain = None
    selected_chain_name = None
    selected_token = None
    deposit_address = None
    token_contract = None
    explorer_address_url = None
    explorer_token_url = None

    for ps in invoice.payment_sessions:
        selected_chain = ps.chain
        selected_token = ps.token

        chain_config = get_chain_config(ps.chain)
        selected_chain_name = chain_config.name

        if ps.deposit_address:
            deposit_address = ps.deposit_address.address
            explorer_address_url = chain_config.get_explorer_address_url(
                deposit_address
            )

        token_config = chain_config.get_token(ps.token)
        if token_config:
            token_contract = token_config.contract_address
            explorer_token_url = chain_config.get_explorer_address_url(token_contract)
        break

    return InvoiceInfoResponse(
        public_id=invoice.public_id,
        amount=invoice.amount,
        asset=invoice.asset,
        status=invoice.status.value,
        allowed_chains=invoice.allowed_chains,
        expires_at=invoice.expires_at,
        is_expired=is_expired,
        merchant_name=invoice.merchant.name if invoice.merchant else "Unknown",
        ttl_minutes=invoice.ttl_minutes,
        selected_chain=selected_chain,
        selected_chain_name=selected_chain_name,
        selected_token=selected_token,
        deposit_address=deposit_address,
        token_contract=token_contract,
        explorer_address_url=explorer_address_url,
        explorer_token_url=explorer_token_url,
    )


@router.post(
    "/{public_id}/select",
    response_model=PaymentSelectResponse,
    summary="Выбрать сеть для оплаты",
)
async def select_payment_option(
    public_id: str,
    request: PaymentSelectRequest,
    session: SessionDep,
) -> PaymentSelectResponse:
    """
    Выбрать сеть и токен для оплаты.
    Возвращает депозитный адрес и данные для QR кода.
    """
    invoice_service = InvoiceService(session)
    payment_service = PaymentService(session)

    try:
        invoice = await invoice_service.get_invoice_by_public_id(public_id)
    except InvoiceNotFoundError:
        raise HTTPException(status_code=404, detail="Invoice not found")

    try:
        payment_session = await payment_service.select_payment_option(
            invoice=invoice,
            chain=request.chain,
            token=request.token,
        )
    except InvoiceExpiredError:
        raise HTTPException(status_code=410, detail="Invoice has expired")
    except PaymentError as e:
        raise HTTPException(status_code=400, detail=str(e))

    chain_config = get_chain_config(payment_session.chain)
    deposit_address = payment_session.deposit_address.address

    # Формируем QR data (EIP-681 формат для ERC20)
    token_config = chain_config.get_token(payment_session.token)
    token_contract_address = token_config.contract_address if token_config else ""
    qr_data = f"ethereum:{deposit_address}@{chain_config.chain_id}"

    return PaymentSelectResponse(
        deposit_address=deposit_address,
        amount=invoice.amount,
        chain=payment_session.chain,
        token=payment_session.token,
        chain_name=chain_config.name,
        qr_data=qr_data,
        explorer_address_url=chain_config.get_explorer_address_url(deposit_address),
        token_contract=token_contract_address,
        explorer_token_url=chain_config.get_explorer_address_url(
            token_contract_address
        ),
    )


@router.get(
    "/{public_id}/status",
    response_model=PaymentStatusResponse,
    summary="Статус оплаты",
)
async def get_payment_status(
    public_id: str,
    session: SessionDep,
) -> PaymentStatusResponse:
    """
    Получить текущий статус оплаты.
    Используется для polling со стороны hosted страницы.
    """
    invoice_service = InvoiceService(session)

    try:
        invoice = await invoice_service.get_invoice_by_public_id(public_id)
    except InvoiceNotFoundError:
        raise HTTPException(status_code=404, detail="Invoice not found")

    is_expired = _is_expired(invoice.expires_at)

    response = PaymentStatusResponse(
        invoice_id=str(invoice.id),
        public_id=invoice.public_id,
        status=invoice.status.value,
        amount=invoice.amount,
        asset=invoice.asset,
        expires_at=invoice.expires_at,
        is_expired=is_expired,
    )

    # Добавляем данные о платеже если есть
    if invoice.payment_sessions:
        ps = invoice.payment_sessions[0]
        chain_config = get_chain_config(ps.chain)
        token_config = chain_config.get_token(ps.token)

        response.chain = ps.chain
        response.chain_name = chain_config.name
        response.token = ps.token
        response.deposit_address = (
            ps.deposit_address.address if ps.deposit_address else None
        )
        response.required_confirmations = chain_config.confirmations

        # Добавляем данные о контракте токена
        if token_config:
            response.token_contract = token_config.contract_address
            response.explorer_token_url = chain_config.get_explorer_address_url(
                token_config.contract_address
            )

        # Explorer URL для депозитного адреса
        if ps.deposit_address:
            response.explorer_address_url = chain_config.get_explorer_address_url(
                ps.deposit_address.address
            )

        # Ищем транзакцию
        if ps.onchain_txs:
            tx = ps.onchain_txs[0]  # Берём первую
            response.tx_hash = tx.tx_hash
            response.confirmations = tx.confirmations
            response.explorer_tx_url = chain_config.get_explorer_tx_url(tx.tx_hash)

    return response


def _render_payment_page(invoice, is_expired: bool) -> str:
    """Рендер HTML страницы оплаты с выбором сети/токена, QR кодом и realtime статусом."""
    status_class = "expired" if is_expired else invoice.status.value.lower()

    # Собираем данные о сетях
    chains_data = []
    for chain in invoice.allowed_chains:
        chain_config = get_chain_config(chain)
        chains_data.append(
            {
                "id": chain,
                "name": chain_config.name,
                "symbol": chain_config.native_symbol,
            }
        )

    # Проверяем есть ли уже выбранная сессия
    selected_session = None
    deposit_address = None
    if invoice.payment_sessions:
        selected_session = invoice.payment_sessions[0]
        if selected_session.deposit_address:
            deposit_address = selected_session.deposit_address.address

    return f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Оплата - {invoice.public_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        :root {{
            --primary: #6366f1;
            --primary-dark: #4f46e5;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --info: #3b82f6;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }}

        .container {{
            background: white;
            border-radius: 20px;
            padding: 32px;
            max-width: 440px;
            width: 100%;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }}

        .merchant {{
            text-align: center;
            font-size: 14px;
            color: #6b7280;
            margin-bottom: 8px;
        }}

        .header {{
            text-align: center;
            margin-bottom: 24px;
            padding-bottom: 24px;
            border-bottom: 1px solid #e5e7eb;
        }}

        .amount {{
            font-size: 42px;
            font-weight: 700;
            color: #1a1a2e;
            line-height: 1.2;
        }}

        .asset {{
            font-size: 20px;
            color: var(--primary);
            font-weight: 600;
        }}

        .status-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 16px;
            border-radius: 24px;
            font-size: 14px;
            font-weight: 600;
            margin-top: 16px;
        }}

        .status-badge.created, .status-badge.awaiting_payment {{
            background: #fef3c7;
            color: #92400e;
        }}

        .status-badge.seen_onchain {{
            background: #dbeafe;
            color: #1e40af;
        }}

        .status-badge.confirmed {{
            background: #d1fae5;
            color: #065f46;
        }}

        .status-badge.expired {{
            background: #fee2e2;
            color: #991b1b;
        }}

        /* Selection Step */
        .selection-step {{
            margin-bottom: 24px;
        }}

        .step-title {{
            font-size: 14px;
            font-weight: 600;
            color: #374151;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .step-number {{
            width: 24px;
            height: 24px;
            background: var(--primary);
            color: white;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: 700;
        }}

        /* Chain Selection */
        .chain-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
        }}

        .chain-option {{
            padding: 14px 12px;
            border: 2px solid #e5e7eb;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
        }}

        .chain-option:hover {{
            border-color: var(--primary);
            background: #f8fafc;
        }}

        .chain-option.selected {{
            border-color: var(--primary);
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(79, 70, 229, 0.1) 100%);
        }}

        .chain-option .name {{
            font-weight: 600;
            font-size: 14px;
            color: #1f2937;
        }}

        .chain-option .symbol {{
            font-size: 12px;
            color: #6b7280;
            margin-top: 2px;
        }}

        .chain-option.disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}

        /* Token Selection */
        .token-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-top: 12px;
        }}

        .token-option {{
            padding: 16px;
            border: 2px solid #e5e7eb;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }}

        .token-option:hover {{
            border-color: var(--primary);
        }}

        .token-option.selected {{
            border-color: var(--primary);
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(79, 70, 229, 0.1) 100%);
        }}

        .token-option .icon {{
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 12px;
        }}

        .token-option.usdt .icon {{
            background: #26A17B;
            color: white;
        }}

        .token-option.usdc .icon {{
            background: #2775CA;
            color: white;
        }}

        .token-option .name {{
            font-weight: 600;
            font-size: 16px;
        }}

        /* Button */
        .btn {{
            width: 100%;
            padding: 16px;
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            margin-top: 20px;
        }}

        .btn:hover:not(:disabled) {{
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(99, 102, 241, 0.3);
        }}

        .btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }}

        .btn .loading {{
            display: inline-block;
            width: 18px;
            height: 18px;
            border: 2px solid rgba(255,255,255,0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin-right: 8px;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        /* Payment Info */
        .payment-info {{
            background: #f8fafc;
            border-radius: 16px;
            padding: 24px;
            margin-top: 20px;
        }}

        .payment-info.hidden {{
            display: none;
        }}

        .qr-container {{
            text-align: center;
            margin-bottom: 20px;
        }}

        .qr-code {{
            background: white;
            padding: 16px;
            border-radius: 12px;
            display: inline-block;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);
        }}

        .qr-code canvas {{
            display: block;
        }}

        .address-container {{
            margin-top: 16px;
        }}

        .address-label {{
            font-size: 12px;
            color: #6b7280;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .address-box {{
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 10px;
            padding: 14px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .address {{
            flex: 1;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 13px;
            word-break: break-all;
            color: #374151;
        }}

        .copy-btn {{
            background: var(--primary);
            color: white;
            border: none;
            padding: 10px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            white-space: nowrap;
            transition: background 0.2s;
        }}

        .copy-btn:hover {{
            background: var(--primary-dark);
        }}

        .copy-btn.copied {{
            background: var(--success);
        }}

        /* Payment Details */
        .payment-details {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid #e5e7eb;
        }}

        .detail-item {{
            background: white;
            padding: 12px;
            border-radius: 8px;
        }}

        .detail-item .label {{
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .detail-item .value {{
            font-weight: 600;
            font-size: 14px;
            color: #1f2937;
            margin-top: 2px;
        }}

        /* Confirmations */
        .confirmations {{
            margin-top: 16px;
            padding: 16px;
            background: white;
            border-radius: 12px;
        }}

        .confirmations.hidden {{
            display: none;
        }}

        .confirmations-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}

        .confirmations-title {{
            font-weight: 600;
            font-size: 14px;
        }}

        .confirmations-count {{
            font-size: 14px;
            color: var(--info);
            font-weight: 600;
        }}

        .progress-bar {{
            height: 8px;
            background: #e5e7eb;
            border-radius: 4px;
            overflow: hidden;
        }}

        .progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, var(--info), var(--success));
            border-radius: 4px;
            transition: width 0.5s ease;
        }}

        .tx-link {{
            display: block;
            margin-top: 12px;
            text-align: center;
            color: var(--primary);
            font-size: 13px;
            text-decoration: none;
        }}

        .tx-link:hover {{
            text-decoration: underline;
        }}

        /* Timer */
        .timer {{
            text-align: center;
            margin-top: 20px;
            padding: 12px;
            background: #fef3c7;
            border-radius: 10px;
            color: #92400e;
            font-size: 14px;
        }}

        .timer.expired {{
            background: #fee2e2;
            color: #991b1b;
        }}

        .timer strong {{
            font-weight: 700;
        }}

        /* Success State */
        .success-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(16, 185, 129, 0.95);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 100;
            opacity: 0;
            visibility: hidden;
            transition: all 0.3s;
        }}

        .success-overlay.show {{
            opacity: 1;
            visibility: visible;
        }}

        .success-content {{
            text-align: center;
            color: white;
        }}

        .success-icon {{
            width: 80px;
            height: 80px;
            background: white;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 24px;
        }}

        .success-icon svg {{
            width: 40px;
            height: 40px;
            color: var(--success);
        }}

        .success-title {{
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
        }}

        .success-subtitle {{
            font-size: 16px;
            opacity: 0.9;
        }}

        /* Explorer Link */
        .explorer-link {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            margin-top: 12px;
            color: var(--primary);
            font-size: 13px;
            text-decoration: none;
        }}

        .explorer-link:hover {{
            text-decoration: underline;
        }}
    </style>
    <!-- QR Code Library -->
    <script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
</head>
<body>
    <div class="container">
        <div class="merchant">{invoice.merchant.name if invoice.merchant else ''}</div>

        <div class="header">
            <div class="amount">{invoice.amount}</div>
            <div class="asset">{invoice.asset}</div>
            <div class="status-badge {status_class}" id="statusBadge">
                <span id="statusText">{_get_status_text(invoice.status, is_expired)}</span>
            </div>
        </div>

        <!-- Selection UI (hidden if already selected or expired/confirmed) -->
        <div id="selectionUI" class="{'hidden' if is_expired or invoice.status in [InvoiceStatus.CONFIRMED, InvoiceStatus.EXPIRED] or deposit_address else ''}">
            <div class="selection-step">
                <div class="step-title">
                    <span class="step-number">1</span>
                    Выберите сеть
                </div>
                <div class="chain-grid" id="chainGrid">
                    {"".join([f'''
                    <div class="chain-option" data-chain="{c['id']}" onclick="selectChain('{c['id']}')">
                        <div class="name">{c['name']}</div>
                        <div class="symbol">{c['symbol']}</div>
                    </div>
                    ''' for c in chains_data])}
                </div>
            </div>

            <div class="selection-step">
                <div class="step-title">
                    <span class="step-number">2</span>
                    Выберите токен
                </div>
                <div class="token-grid">
                    <div class="token-option usdt {'selected' if invoice.asset == 'USDT' else ''}" data-token="USDT" onclick="selectToken('USDT')">
                        <div class="icon">₮</div>
                        <div class="name">USDT</div>
                    </div>
                    <div class="token-option usdc {'selected' if invoice.asset == 'USDC' else ''}" data-token="USDC" onclick="selectToken('USDC')">
                        <div class="icon">$</div>
                        <div class="name">USDC</div>
                    </div>
                </div>
            </div>

            <button class="btn" id="confirmBtn" onclick="confirmSelection()" disabled>
                Получить адрес для оплаты
            </button>
        </div>

        <!-- Payment Info (shown after selection) -->
        <div class="payment-info {'hidden' if not deposit_address else ''}" id="paymentInfo">
            <div class="qr-container">
                <div class="qr-code">
                    <canvas id="qrCanvas"></canvas>
                </div>
            </div>

            <div class="address-container">
                <div class="address-label">Адрес для оплаты</div>
                <div class="address-box">
                    <div class="address" id="addressText">{deposit_address or ''}</div>
                    <button class="copy-btn" id="copyBtn" onclick="copyAddress()">Копировать</button>
                </div>
            </div>

            <div class="payment-details">
                <div class="detail-item">
                    <div class="label">Сеть</div>
                    <div class="value" id="selectedChainName">{get_chain_config(selected_session.chain).name if selected_session else '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="label">Токен</div>
                    <div class="value" id="selectedTokenName">{selected_session.token if selected_session else '-'}</div>
                </div>
            </div>

            <a href="#" class="explorer-link" id="explorerLink" target="_blank">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                    <polyline points="15 3 21 3 21 9"/>
                    <line x1="10" y1="14" x2="21" y2="3"/>
                </svg>
                Посмотреть в эксплорере
            </a>
        </div>

        <!-- Confirmations Progress -->
        <div class="confirmations hidden" id="confirmationsBox">
            <div class="confirmations-header">
                <span class="confirmations-title">Подтверждения</span>
                <span class="confirmations-count" id="confirmationsCount">0/12</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill" style="width: 0%"></div>
            </div>
            <a href="#" class="tx-link" id="txLink" target="_blank">
                Посмотреть транзакцию →
            </a>
        </div>

        <!-- Timer -->
        <div class="timer {'expired' if is_expired else ''}" id="timer">
            {'Время истекло' if is_expired else f'Осталось: <strong id="countdown"></strong>'}
        </div>
    </div>

    <!-- Success Overlay -->
    <div class="success-overlay" id="successOverlay">
        <div class="success-content">
            <div class="success-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                    <polyline points="20 6 9 17 4 12"/>
                </svg>
            </div>
            <div class="success-title">Оплата получена!</div>
            <div class="success-subtitle">Спасибо за оплату</div>
        </div>
    </div>

    <script>
        const publicId = "{invoice.public_id}";
        const expiresAt = new Date("{invoice.expires_at.isoformat()}");
        const initialAsset = "{invoice.asset}";
        const initialAddress = "{deposit_address or ''}";

        let selectedChain = "{selected_session.chain if selected_session else ''}";
        let selectedToken = "{selected_session.token if selected_session else invoice.asset}";
        let isPolling = false;

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            // Set initial token selection
            document.querySelectorAll('.token-option').forEach(el => {{
                el.classList.toggle('selected', el.dataset.token === selectedToken);
            }});

            // If already have address, start polling and generate QR
            if (initialAddress) {{
                generateQR(initialAddress);
                startPolling();
            }}

            updateConfirmBtn();
            updateCountdown();
            setInterval(updateCountdown, 1000);
        }});

        // Chain Selection
        function selectChain(chain) {{
            selectedChain = chain;
            document.querySelectorAll('.chain-option').forEach(el => {{
                el.classList.toggle('selected', el.dataset.chain === chain);
            }});
            updateConfirmBtn();
        }}

        // Token Selection
        function selectToken(token) {{
            selectedToken = token;
            document.querySelectorAll('.token-option').forEach(el => {{
                el.classList.toggle('selected', el.dataset.token === token);
            }});
            updateConfirmBtn();
        }}

        // Update Button State
        function updateConfirmBtn() {{
            const btn = document.getElementById('confirmBtn');
            btn.disabled = !selectedChain || !selectedToken;
        }}

        // Confirm Selection
        async function confirmSelection() {{
            if (!selectedChain || !selectedToken) return;

            const btn = document.getElementById('confirmBtn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span>Загрузка...';

            try {{
                const response = await fetch('/pay/' + publicId + '/select', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ chain: selectedChain, token: selectedToken }})
                }});

                if (!response.ok) {{
                    const err = await response.json();
                    throw new Error(err.detail || 'Ошибка');
                }}

                const data = await response.json();

                // Update UI
                document.getElementById('selectionUI').classList.add('hidden');
                document.getElementById('paymentInfo').classList.remove('hidden');
                document.getElementById('addressText').textContent = data.deposit_address;
                document.getElementById('selectedChainName').textContent = data.chain_name;
                document.getElementById('selectedTokenName').textContent = data.token;
                document.getElementById('explorerLink').href = data.explorer_address_url;

                // Generate QR
                generateQR(data.deposit_address);

                // Update status
                document.getElementById('statusBadge').className = 'status-badge awaiting_payment';
                document.getElementById('statusText').textContent = 'Ожидание оплаты';

                // Start polling
                startPolling();
            }} catch (e) {{
                alert('Ошибка: ' + e.message);
                btn.disabled = false;
                btn.textContent = 'Получить адрес для оплаты';
            }}
        }}

        // Generate QR Code
        function generateQR(address) {{
            const canvas = document.getElementById('qrCanvas');
            if (typeof QRCode !== 'undefined') {{
                QRCode.toCanvas(canvas, address, {{
                    width: 180,
                    margin: 1,
                    color: {{ dark: '#1f2937', light: '#ffffff' }}
                }});
            }}
        }}

        // Copy Address
        function copyAddress() {{
            const address = document.getElementById('addressText').textContent;
            navigator.clipboard.writeText(address).then(() => {{
                const btn = document.getElementById('copyBtn');
                btn.textContent = 'Скопировано!';
                btn.classList.add('copied');
                setTimeout(() => {{
                    btn.textContent = 'Копировать';
                    btn.classList.remove('copied');
                }}, 2000);
            }});
        }}

        // Countdown Timer
        function updateCountdown() {{
            const countdown = document.getElementById('countdown');
            if (!countdown) return;

            const now = new Date();
            const diff = expiresAt - now;

            if (diff <= 0) {{
                countdown.textContent = 'Истекло';
                document.getElementById('timer').classList.add('expired');
                return;
            }}

            const hours = Math.floor(diff / 3600000);
            const minutes = Math.floor((diff % 3600000) / 60000);
            const seconds = Math.floor((diff % 60000) / 1000);

            if (hours > 0) {{
                countdown.textContent = hours + ':' + minutes.toString().padStart(2, '0') + ':' + seconds.toString().padStart(2, '0');
            }} else {{
                countdown.textContent = minutes + ':' + seconds.toString().padStart(2, '0');
            }}
        }}

        // Poll Status
        function startPolling() {{
            if (isPolling) return;
            isPolling = true;
            pollStatus();
        }}

        async function pollStatus() {{
            try {{
                const response = await fetch('/pay/' + publicId + '/status');
                const data = await response.json();

                // Update status badge
                const badge = document.getElementById('statusBadge');
                const statusText = document.getElementById('statusText');

                if (data.status === 'CONFIRMED') {{
                    badge.className = 'status-badge confirmed';
                    statusText.textContent = 'Оплачено ✓';
                    document.getElementById('successOverlay').classList.add('show');
                    return; // Stop polling
                }}

                if (data.status === 'SEEN_ONCHAIN') {{
                    badge.className = 'status-badge seen_onchain';
                    statusText.textContent = 'Подтверждение...';

                    // Show confirmations
                    const confBox = document.getElementById('confirmationsBox');
                    confBox.classList.remove('hidden');

                    const current = data.confirmations || 0;
                    const required = data.required_confirmations || 12;
                    const percent = Math.min(100, (current / required) * 100);

                    document.getElementById('confirmationsCount').textContent = current + '/' + required;
                    document.getElementById('progressFill').style.width = percent + '%';

                    if (data.explorer_tx_url) {{
                        document.getElementById('txLink').href = data.explorer_tx_url;
                        document.getElementById('txLink').style.display = 'block';
                    }}
                }}

                if (data.is_expired) {{
                    badge.className = 'status-badge expired';
                    statusText.textContent = 'Истёк';
                    return; // Stop polling
                }}

                // Continue polling (faster for seen_onchain)
                const interval = data.status === 'SEEN_ONCHAIN' ? 3000 : 5000;
                setTimeout(pollStatus, interval);
            }} catch (e) {{
                setTimeout(pollStatus, 5000);
            }}
        }}
    </script>
</body>
</html>
    """


def _get_status_text(status, is_expired: bool) -> str:
    """Получить текст статуса."""
    if is_expired:
        return "Время истекло"
    status_texts = {
        InvoiceStatus.CREATED: "Ожидание выбора сети",
        InvoiceStatus.AWAITING_PAYMENT: "Ожидание оплаты",
        InvoiceStatus.SEEN_ONCHAIN: "Подтверждение...",
        InvoiceStatus.CONFIRMED: "Оплачено ✓",
        InvoiceStatus.EXPIRED: "Время истекло",
    }
    return status_texts.get(status, status.value)


def _render_error_page(message: str) -> str:
    """Рендер страницы ошибки."""
    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Ошибка</title>
    <style>
        body {{
            font-family: sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            background: #f3f4f6;
        }}
        .error {{
            background: white;
            padding: 40px;
            border-radius: 8px;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        h1 {{ color: #ef4444; }}
    </style>
</head>
<body>
    <div class="error">
        <h1>Ошибка</h1>
        <p>{message}</p>
    </div>
</body>
</html>
    """
