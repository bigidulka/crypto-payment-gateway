"""
Arbitron Payment Gateway - Main Application.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api.merchant.router import router as merchant_router
from src.api.hosted.router import router as hosted_router
from src.api.admin.router import router as admin_router
from src.api.wallet.router import router as wallet_router
from src.api.public.router import router as public_router
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.db.session import close_db
from src.db.redis import get_redis, close_redis
from src.blockchain.rpc_manager import init_all_rpc_managers, close_all_rpc_managers
from src.blockchain.chains import get_all_chains

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    settings = get_settings()

    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Debug mode: {settings.debug}")

    # Инициализация Multi-RPC managers для всех сетей
    await init_rpc_managers()

    # Синхронизация Redis счётчика deposit address с БД
    await sync_deposit_address_counter()

    yield

    # Shutdown
    logger.info("Shutting down...")
    await close_all_rpc_managers()
    logger.info("RPC managers closed")
    await close_redis()
    logger.info("Redis connections closed")
    await close_db()
    logger.info("Database connections closed")


async def init_rpc_managers():
    """Инициализировать RPC managers для всех сетей."""
    settings = get_settings()

    # Собираем RPC endpoints для каждой сети
    rpc_config = {}
    for chain in get_all_chains():
        urls = settings.get_rpc_urls(chain)
        if urls:
            rpc_config[chain] = urls
            logger.info(f"[{chain}] Configured {len(urls)} RPC endpoints")

    if rpc_config:
        await init_all_rpc_managers(rpc_config)
        logger.info(f"RPC managers initialized for {len(rpc_config)} chains")


async def sync_deposit_address_counter():
    """Синхронизировать Redis счётчик с максимальным индексом в БД."""
    from sqlalchemy import func, select
    from src.db.models import DepositAddress
    from src.db.session import get_session_factory

    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(func.max(DepositAddress.derivation_index))
            )
            max_index = result.scalar()

        # Используем Redis пул вместо нового подключения
        redis_client = await get_redis()

        if max_index is not None:
            # Устанавливаем счётчик на max_index + 1 (для следующего адреса)
            await redis_client.set("deposit_address:next_index", max_index + 1)
            logger.info(f"Redis deposit counter synced to {max_index + 1}")
        else:
            # Нет адресов в БД - начинаем с 0
            current = await redis_client.get("deposit_address:next_index")
            if current is None:
                await redis_client.set("deposit_address:next_index", 0)
                logger.info("Redis deposit counter initialized to 0")
            else:
                logger.info(f"Redis deposit counter already set to {current}")
    except Exception as e:
        logger.error(f"Failed to sync deposit address counter: {e}")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Crypto Payment Gateway API",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(
        merchant_router,
        prefix="/v1",
        tags=["Merchant API"],
    )

    app.include_router(
        hosted_router,
        prefix="",
        tags=["Hosted Pages"],
    )

    app.include_router(
        admin_router,
        prefix="/v1/admin",
        tags=["Admin API"],
    )

    app.include_router(
        wallet_router,
        prefix="/v1",
        tags=["User Wallets"],
    )

    app.include_router(
        public_router,
        prefix="/v1",
        tags=["Public"],
    )

    # Admin Panel HTML
    @app.get("/admin", tags=["Admin Panel"], include_in_schema=False)
    async def admin_panel():
        """Admin panel page."""
        return _render_admin_panel()

    # Health check
    @app.get("/health", tags=["Health"])
    async def health_check():
        """Health check endpoint."""
        return {
            "status": "healthy",
            "version": settings.app_version,
        }

    # Ready check (включает проверку зависимостей)
    @app.get("/ready", tags=["Health"])
    async def ready_check():
        """Readiness check endpoint."""
        # TODO: Add database and Redis health checks
        return {
            "status": "ready",
            "version": settings.app_version,
        }

    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()

    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )


def _render_admin_panel() -> str:
    """Render admin panel HTML."""
    from fastapi.responses import HTMLResponse

    html = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arbitron Admin Panel</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --primary: #3b82f6;
            --primary-dark: #2563eb;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --bg: #f8fafc;
            --card: #ffffff;
            --text: #1e293b;
            --text-muted: #64748b;
            --border: #e2e8f0;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
        }

        /* Login Page */
        .login-wrapper {
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }

        .login-card {
            background: white;
            border-radius: 16px;
            padding: 48px;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }

        .login-header {
            text-align: center;
            margin-bottom: 32px;
        }

        .login-header h1 {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--text);
        }

        .login-header p {
            color: var(--text-muted);
            font-size: 14px;
        }

        .form-group {
            margin-bottom: 20px;
        }

        .form-group label {
            display: block;
            font-weight: 600;
            margin-bottom: 8px;
            font-size: 14px;
            color: var(--text);
        }

        .form-group input {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid var(--border);
            border-radius: 8px;
            font-size: 15px;
            transition: all 0.2s;
        }

        .form-group input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }

        .error-message {
            background: #fee2e2;
            color: #991b1b;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
            text-align: center;
        }

        .btn {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(59, 130, 246, 0.3);
        }

        .btn:active {
            transform: translateY(0);
        }

        /* Dashboard */
        .dashboard {
            display: none;
        }

        .dashboard.active {
            display: block;
        }

        .header {
            background: white;
            border-bottom: 1px solid var(--border);
            padding: 16px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        }

        .header-title {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 20px;
            font-weight: 700;
            color: var(--text);
        }

        .header-actions {
            display: flex;
            gap: 12px;
        }

        .btn-sm {
            padding: 8px 16px;
            font-size: 14px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-refresh {
            background: var(--bg);
            color: var(--text);
        }

        .btn-refresh:hover {
            background: var(--border);
        }

        .btn-logout {
            background: var(--danger);
            color: white;
        }

        .btn-logout:hover {
            background: #dc2626;
        }

        .tabs {
            background: white;
            border-bottom: 1px solid var(--border);
            display: flex;
            gap: 4px;
            padding: 0 24px;
        }

        .tab {
            padding: 14px 20px;
            border: none;
            background: transparent;
            cursor: pointer;
            font-size: 15px;
            font-weight: 500;
            color: var(--text-muted);
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
        }

        .tab:hover {
            color: var(--text);
            background: var(--bg);
        }

        .tab.active {
            color: var(--primary);
            border-bottom-color: var(--primary);
        }

        .content {
            padding: 24px;
            max-width: 1600px;
            margin: 0 auto;
        }

        .tab-content {
            display: none;
        }

        .tab-content.active {
            display: block;
        }

        /* Table */
        .table-wrapper {
            background: white;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }

        .table-header {
            padding: 20px 24px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .table-header h2 {
            font-size: 18px;
            font-weight: 600;
        }

        .search-box {
            padding: 8px 12px;
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 14px;
            width: 250px;
        }

        .search-box:focus {
            outline: none;
            border-color: var(--primary);
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th, td {
            padding: 14px 24px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }

        th {
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            font-weight: 600;
            background: var(--bg);
            cursor: pointer;
            user-select: none;
        }

        th:hover {
            background: var(--border);
        }

        tr:hover {
            background: var(--bg);
        }

        tr:last-child td {
            border-bottom: none;
        }

        .badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }

        .badge-active {
            background: #d1fae5;
            color: #065f46;
        }

        .badge-inactive {
            background: #fee2e2;
            color: #991b1b;
        }

        .badge-created { background: #fef3c7; color: #92400e; }
        .badge-awaiting_payment { background: #fef3c7; color: #92400e; }
        .badge-seen_onchain { background: #dbeafe; color: #1e40af; }
        .badge-confirmed { background: #d1fae5; color: #065f46; }
        .badge-expired { background: #fee2e2; color: #991b1b; }

        .btn-action {
            padding: 6px 12px;
            font-size: 13px;
            border-radius: 6px;
            border: 1px solid var(--border);
            background: white;
            color: var(--primary);
            cursor: pointer;
            font-weight: 500;
            transition: all 0.2s;
            margin-right: 6px;
        }

        .btn-action:hover {
            background: var(--primary);
            color: white;
            border-color: var(--primary);
        }

        .balance {
            font-weight: 600;
            font-family: 'SF Mono', monospace;
        }

        .balance-positive {
            color: var(--success);
        }

        .balance-zero {
            color: var(--text-muted);
        }

        .mono {
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 13px;
        }

        .loading {
            text-align: center;
            padding: 60px;
            color: var(--text-muted);
        }

        .empty {
            text-align: center;
            padding: 60px;
            color: var(--text-muted);
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .stat-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }

        .stat-label {
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 8px;
        }

        .stat-value {
            font-size: 28px;
            font-weight: 700;
            color: var(--text);
        }
    </style>
</head>
<body>
    <!-- Login Screen -->
    <div id="loginScreen" class="login-wrapper">
        <div class="login-card">
            <div class="login-header">
                <h1>🔷 Arbitron Admin</h1>
                <p>Введите учетные данные</p>
            </div>
            <div id="loginError" class="error-message" style="display: none;"></div>
            <form id="loginForm">
                <div class="form-group">
                    <label for="username">Логин</label>
                    <input type="text" id="username" value="admin" required autofocus>
                </div>
                <div class="form-group">
                    <label for="password">Секретный ключ (ADMIN_SECRET_KEY)</label>
                    <input type="password" id="password" placeholder="Введите ADMIN_SECRET_KEY из .env" required>
                </div>
                <button type="submit" class="btn">Войти</button>
            </form>
        </div>
    </div>

    <!-- Dashboard -->
    <div id="dashboard" class="dashboard">
        <div class="header">
            <div class="header-title">
                🔷 Arbitron Admin Panel
            </div>
            <div class="header-actions">
                <button class="btn-sm btn-refresh" onclick="refreshCurrentTab()">🔄 Обновить</button>
                <button class="btn-sm btn-logout" onclick="logout()">Выйти</button>
            </div>
        </div>

        <div class="tabs">
            <button class="tab active" onclick="showTab('merchants')">Мерчанты</button>
            <button class="tab" onclick="showTab('wallets')">Кошельки</button>
            <button class="tab" onclick="showTab('invoices')">Инвойсы</button>
        </div>

        <div class="content">
            <!-- Merchants Tab -->
            <div id="tab-merchants" class="tab-content active">
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-label">Всего мерчантов</div>
                        <div class="stat-value" id="stat-merchants">-</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Всего инвойсов</div>
                        <div class="stat-value" id="stat-invoices">-</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Всего кошельков</div>
                        <div class="stat-value" id="stat-wallets">-</div>
                    </div>
                </div>

                <div class="table-wrapper">
                    <div class="table-header">
                        <h2>Мерчанты</h2>
                        <input type="text" class="search-box" placeholder="Поиск..." onkeyup="filterMerchants(this.value)">
                    </div>
                    <table>
                        <thead>
                            <tr>
                                <th onclick="sortTable('merchants', 0)">ID</th>
                                <th onclick="sortTable('merchants', 1)">Имя</th>
                                <th onclick="sortTable('merchants', 2)">Email</th>
                                <th onclick="sortTable('merchants', 3)">API Key</th>
                                <th onclick="sortTable('merchants', 4)">Статус</th>
                                <th onclick="sortTable('merchants', 5)">Инвойсов</th>
                                <th onclick="sortTable('merchants', 6)">Объем</th>
                                <th onclick="sortTable('merchants', 7)">Создан</th>
                                <th>Действия</th>
                            </tr>
                        </thead>
                        <tbody id="merchantsTable">
                            <tr><td colspan="9" class="loading">Загрузка...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Wallets Tab -->
            <div id="tab-wallets" class="tab-content">
                <div class="table-wrapper">
                    <div class="table-header">
                        <h2>Кошельки</h2>
                        <input type="text" class="search-box" placeholder="Поиск..." onkeyup="filterWallets(this.value)">
                    </div>
                    <table>
                        <thead>
                            <tr>
                                <th onclick="sortTable('wallets', 0)">Адрес</th>
                                <th onclick="sortTable('wallets', 1)">Сеть</th>
                                <th onclick="sortTable('wallets', 2)">USDT</th>
                                <th onclick="sortTable('wallets', 3)">USDC</th>
                                <th onclick="sortTable('wallets', 4)">Native</th>
                                <th onclick="sortTable('wallets', 5)">Мерчант</th>
                            </tr>
                        </thead>
                        <tbody id="walletsTable">
                            <tr><td colspan="6" class="loading">Загрузка...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Invoices Tab -->
            <div id="tab-invoices" class="tab-content">
                <div class="table-wrapper">
                    <div class="table-header">
                        <h2>Инвойсы</h2>
                        <input type="text" class="search-box" placeholder="Поиск..." onkeyup="filterInvoices(this.value)">
                    </div>
                    <table>
                        <thead>
                            <tr>
                                <th onclick="sortTable('invoices', 0)">Public ID</th>
                                <th onclick="sortTable('invoices', 1)">Сумма</th>
                                <th onclick="sortTable('invoices', 2)">Токен</th>
                                <th onclick="sortTable('invoices', 3)">Статус</th>
                                <th onclick="sortTable('invoices', 4)">Сеть</th>
                                <th onclick="sortTable('invoices', 5)">Мерчант</th>
                                <th onclick="sortTable('invoices', 6)">Создан</th>
                            </tr>
                        </thead>
                        <tbody id="invoicesTable">
                            <tr><td colspan="7" class="loading">Загрузка...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        let authToken = localStorage.getItem('adminToken');
        let currentTab = 'merchants';
        let merchantsData = [];
        let walletsData = [];
        let invoicesData = [];
        let currentMerchantFilter = null;

        // Auth
        if (authToken) {
            document.getElementById('loginScreen').style.display = 'none';
            document.getElementById('dashboard').classList.add('active');
            loadAllData();
        }

        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;

            try {
                const response = await fetch('/v1/admin/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });

                const data = await response.json();

                if (data.success) {
                    authToken = data.token;
                    localStorage.setItem('adminToken', authToken);
                    document.getElementById('loginScreen').style.display = 'none';
                    document.getElementById('dashboard').classList.add('active');
                    loadAllData();
                } else {
                    document.getElementById('loginError').textContent = data.message || 'Ошибка входа';
                    document.getElementById('loginError').style.display = 'block';
                }
            } catch (error) {
                document.getElementById('loginError').textContent = 'Ошибка соединения';
                document.getElementById('loginError').style.display = 'block';
            }
        });

        function logout() {
            localStorage.removeItem('adminToken');
            location.reload();
        }

        // API Helper - передаёт Bearer token в заголовках
        async function api(endpoint, options = {}) {
            const headers = {
                'Content-Type': 'application/json',
                ...options.headers
            };
            
            // Добавляем Authorization header если есть токен
            if (authToken) {
                headers['Authorization'] = 'Bearer ' + authToken;
            }
            
            const response = await fetch(endpoint, {
                ...options,
                headers
            });
            
            // Если 401 - токен истёк, выходим
            if (response.status === 401) {
                logout();
                throw new Error('Session expired');
            }
            
            if (!response.ok) throw new Error('API Error: ' + response.status);
            return response.json();
        }

        // Load Data
        async function loadAllData() {
            await Promise.all([
                loadMerchants(),
                loadWallets(),
                loadInvoices()
            ]);
            updateStats();
        }

        function updateStats() {
            document.getElementById('stat-merchants').textContent = merchantsData.length;
            document.getElementById('stat-invoices').textContent = invoicesData.length;
            document.getElementById('stat-wallets').textContent = walletsData.length;
        }

        async function loadMerchants() {
            try {
                const data = await api('/v1/admin/merchants?limit=500');
                merchantsData = data.items;
                renderMerchants();
            } catch (error) {
                document.getElementById('merchantsTable').innerHTML = '<tr><td colspan="9" class="empty">Ошибка загрузки</td></tr>';
            }
        }

        function renderMerchants() {
            const tbody = document.getElementById('merchantsTable');
            if (merchantsData.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" class="empty">Нет данных</td></tr>';
                return;
            }

            let html = '';
            merchantsData.forEach(m => {
                const date = new Date(m.created_at).toLocaleDateString('ru');
                html += `
                    <tr>
                        <td class="mono">${m.id.substring(0, 8)}...</td>
                        <td><strong>${m.name}</strong></td>
                        <td>${m.email}</td>
                        <td class="mono">${m.api_key_preview || '-'}...</td>
                        <td><span class="badge badge-${m.is_active ? 'active' : 'inactive'}">${m.is_active ? 'Активен' : 'Неактивен'}</span></td>
                        <td>${m.invoices_count}</td>
                        <td>$${parseFloat(m.total_volume).toFixed(2)}</td>
                        <td>${date}</td>
                        <td>
                            <button class="btn-action" onclick="viewMerchantWallets('${m.id}', '${m.name}')">Кошельки</button>
                            <button class="btn-action" onclick="viewMerchantInvoices('${m.id}', '${m.name}')">Инвойсы</button>
                        </td>
                    </tr>
                `;
            });
            tbody.innerHTML = html;
        }

        async function loadWallets() {
            try {
                const data = await api('/v1/admin/wallets/balances?with_balance_only=false');
                walletsData = data.items || [];
                renderWallets();
            } catch (error) {
                document.getElementById('walletsTable').innerHTML = '<tr><td colspan="6" class="empty">Ошибка загрузки</td></tr>';
            }
        }

        function renderWallets() {
            const tbody = document.getElementById('walletsTable');
            if (walletsData.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="empty">Нет данных</td></tr>';
                return;
            }

            let html = '';
            walletsData.forEach(w => {
                const usdt = w.tokens?.find(t => t.token === 'USDT')?.balance || '0';
                const usdc = w.tokens?.find(t => t.token === 'USDC')?.balance || '0';
                const hasBalance = parseFloat(usdt) > 0 || parseFloat(usdc) > 0;

                html += `
                    <tr>
                        <td class="mono">${w.address.substring(0, 10)}...${w.address.substring(38)}</td>
                        <td><strong>${w.chain.toUpperCase()}</strong></td>
                        <td class="balance ${parseFloat(usdt) > 0 ? 'balance-positive' : 'balance-zero'}">${parseFloat(usdt).toFixed(2)}</td>
                        <td class="balance ${parseFloat(usdc) > 0 ? 'balance-positive' : 'balance-zero'}">${parseFloat(usdc).toFixed(2)}</td>
                        <td class="mono">${parseFloat(w.native_balance).toFixed(4)} ${w.native_symbol}</td>
                        <td>${w.merchant_name || '-'}</td>
                    </tr>
                `;
            });
            tbody.innerHTML = html;
        }

        async function loadInvoices() {
            try {
                const data = await api('/v1/admin/invoices?limit=500');
                invoicesData = data.items;
                renderInvoices();
            } catch (error) {
                document.getElementById('invoicesTable').innerHTML = '<tr><td colspan="7" class="empty">Ошибка загрузки</td></tr>';
            }
        }

        function renderInvoices(filterMerchantId = null) {
            const tbody = document.getElementById('invoicesTable');
            let filtered = invoicesData;
            
            if (filterMerchantId) {
                filtered = invoicesData.filter(i => i.merchant_id === filterMerchantId);
            }

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="empty">Нет данных</td></tr>';
                return;
            }

            let html = '';
            filtered.forEach(inv => {
                const date = new Date(inv.created_at).toLocaleString('ru');
                html += `
                    <tr>
                        <td class="mono">${inv.public_id}</td>
                        <td><strong>${inv.amount} ${inv.asset}</strong></td>
                        <td>${inv.token || inv.asset}</td>
                        <td><span class="badge badge-${inv.status.toLowerCase()}">${inv.status}</span></td>
                        <td>${inv.chain || '-'}</td>
                        <td>${inv.merchant_name || '-'}</td>
                        <td>${date}</td>
                    </tr>
                `;
            });
            tbody.innerHTML = html;
        }

        // Navigation
        function showTab(tabName) {
            currentTab = tabName;
            currentMerchantFilter = null;

            // Update tab buttons
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            event.target.classList.add('active');

            // Update tab content
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            document.getElementById(`tab-${tabName}`).classList.add('active');

            // Re-render without filter
            if (tabName === 'invoices') {
                renderInvoices();
            } else if (tabName === 'wallets') {
                renderWallets();
            }
        }

        function viewMerchantWallets(merchantId, merchantName) {
            currentMerchantFilter = merchantId;
            showTab('wallets');
            // В будущем можно добавить фильтрацию
        }

        function viewMerchantInvoices(merchantId, merchantName) {
            currentMerchantFilter = merchantId;
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            document.querySelectorAll('.tab')[2].classList.add('active');
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            document.getElementById('tab-invoices').classList.add('active');
            renderInvoices(merchantId);
        }

        function refreshCurrentTab() {
            if (currentTab === 'merchants') loadMerchants();
            else if (currentTab === 'wallets') loadWallets();
            else if (currentTab === 'invoices') loadInvoices();
        }

        // Search
        function filterMerchants(query) {
            const rows = document.querySelectorAll('#merchantsTable tr');
            query = query.toLowerCase();
            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(query) ? '' : 'none';
            });
        }

        function filterWallets(query) {
            const rows = document.querySelectorAll('#walletsTable tr');
            query = query.toLowerCase();
            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(query) ? '' : 'none';
            });
        }

        function filterInvoices(query) {
            const rows = document.querySelectorAll('#invoicesTable tr');
            query = query.toLowerCase();
            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(query) ? '' : 'none';
            });
        }

        // Sort (simple version)
        let sortDirections = {};
        function sortTable(table, column) {
            const key = `${table}-${column}`;
            sortDirections[key] = !sortDirections[key];
            // TODO: Implement sorting
        }
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)
