/**
 * Merchant Dashboard Application
 * Панель управления мерчанта с авторизацией по API ключу
 * 
 * Особенности:
 * - Безопасное хранение API ключа (только в памяти/sessionStorage)
 * - Авторизация через заголовок X-API-Key
 * - CRUD операции для инвойсов и webhooks
 */

(function() {
    'use strict';

    // ===== Configuration =====
    const CONFIG = {
        API_BASE: '/v1',
        STORAGE_KEY: 'merchant_api_key',
        INVOICES_PER_PAGE: 10
    };

    // ===== State =====
    const state = {
        apiKey: null,
        merchantName: null,
        invoices: [],
        webhooks: [],
        currentPage: 1,
        totalInvoices: 0,
        statusFilter: ''
    };

    // ===== DOM Elements =====
    const elements = {};

    // ===== Initialization =====
    function init() {
        cacheElements();
        setupEventListeners();
        
        // Check for stored API key
        const storedKey = sessionStorage.getItem(CONFIG.STORAGE_KEY);
        if (storedKey) {
            state.apiKey = storedKey;
            validateAndLogin();
        }
    }

    function cacheElements() {
        // Auth
        elements.authSection = document.getElementById('authSection');
        elements.authForm = document.getElementById('authForm');
        elements.apiKeyInput = document.getElementById('apiKey');
        elements.authBtn = document.getElementById('authBtn');
        elements.authError = document.getElementById('authError');
        
        // Dashboard
        elements.dashboardSection = document.getElementById('dashboardSection');
        elements.merchantName = document.getElementById('merchantName');
        elements.logoutBtn = document.getElementById('logoutBtn');
        
        // Navigation
        elements.navItems = document.querySelectorAll('.nav-item');
        
        // Sections
        elements.invoicesSection = document.getElementById('invoicesSection');
        elements.createSection = document.getElementById('createSection');
        elements.webhooksSection = document.getElementById('webhooksSection');
        
        // Invoices
        elements.filterStatus = document.getElementById('filterStatus');
        elements.refreshInvoicesBtn = document.getElementById('refreshInvoicesBtn');
        elements.invoicesTableBody = document.getElementById('invoicesTableBody');
        elements.invoicesPagination = document.getElementById('invoicesPagination');
        
        // Create Invoice
        elements.createInvoiceForm = document.getElementById('createInvoiceForm');
        elements.createInvoiceBtn = document.getElementById('createInvoiceBtn');
        elements.createError = document.getElementById('createError');
        elements.createdInvoiceResult = document.getElementById('createdInvoiceResult');
        
        // Webhooks
        elements.addWebhookBtn = document.getElementById('addWebhookBtn');
        elements.webhooksTableBody = document.getElementById('webhooksTableBody');
        elements.webhookModal = document.getElementById('webhookModal');
        elements.webhookForm = document.getElementById('webhookForm');
        elements.webhookError = document.getElementById('webhookError');
        
        // Toast
        elements.toastContainer = document.getElementById('toastContainer');
    }

    function setupEventListeners() {
        // Auth form
        elements.authForm.addEventListener('submit', handleAuth);
        
        // Logout
        elements.logoutBtn.addEventListener('click', handleLogout);
        
        // Navigation
        elements.navItems.forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const section = item.dataset.section;
                showSection(section);
            });
        });
        
        // Invoices
        elements.filterStatus.addEventListener('change', handleFilterChange);
        elements.refreshInvoicesBtn.addEventListener('click', loadInvoices);
        
        // Create Invoice
        elements.createInvoiceForm.addEventListener('submit', handleCreateInvoice);
        
        // Webhooks
        elements.addWebhookBtn.addEventListener('click', openWebhookModal);
        elements.webhookForm.addEventListener('submit', handleCreateWebhook);
    }

    // ===== API Client =====
    async function apiRequest(endpoint, options = {}) {
        const url = CONFIG.API_BASE + endpoint;
        
        const headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + state.apiKey,
            ...options.headers
        };
        
        const response = await fetch(url, {
            ...options,
            headers
        });
        
        if (response.status === 401) {
            handleLogout();
            throw new Error('Неверный API ключ');
        }
        
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || data.message || `HTTP ${response.status}`);
        }
        
        return response.json();
    }

    // ===== Auth =====
    async function handleAuth(e) {
        e.preventDefault();
        
        const apiKey = elements.apiKeyInput.value.trim();
        if (!apiKey) return;
        
        setButtonLoading(elements.authBtn, true);
        hideError(elements.authError);
        
        state.apiKey = apiKey;
        
        try {
            await validateAndLogin();
        } catch (error) {
            showError(elements.authError, error.message);
            state.apiKey = null;
        } finally {
            setButtonLoading(elements.authBtn, false);
        }
    }

    async function validateAndLogin() {
        // Try to get invoices to validate the API key
        try {
            const data = await apiRequest('/invoices?limit=1');
            
            // API key is valid, save it and show dashboard
            sessionStorage.setItem(CONFIG.STORAGE_KEY, state.apiKey);
            
            // Extract merchant name from first invoice if available
            if (data.items && data.items.length > 0) {
                // We don't have merchant name in invoice response, use placeholder
                state.merchantName = 'Merchant';
            } else {
                state.merchantName = 'Merchant';
            }
            
            elements.merchantName.textContent = state.merchantName;
            
            showDashboard();
            loadInvoices();
            
        } catch (error) {
            sessionStorage.removeItem(CONFIG.STORAGE_KEY);
            throw error;
        }
    }

    function handleLogout() {
        state.apiKey = null;
        state.merchantName = null;
        sessionStorage.removeItem(CONFIG.STORAGE_KEY);
        
        elements.dashboardSection.classList.add('hidden');
        elements.authSection.classList.remove('hidden');
        elements.apiKeyInput.value = '';
    }

    function showDashboard() {
        elements.authSection.classList.add('hidden');
        elements.dashboardSection.classList.remove('hidden');
    }

    // ===== Navigation =====
    window.showSection = function(sectionName) {
        // Update nav
        elements.navItems.forEach(item => {
            item.classList.toggle('active', item.dataset.section === sectionName);
        });
        
        // Hide all sections
        elements.invoicesSection.classList.add('hidden');
        elements.createSection.classList.add('hidden');
        elements.webhooksSection.classList.add('hidden');
        
        // Show selected section
        switch (sectionName) {
            case 'invoices':
                elements.invoicesSection.classList.remove('hidden');
                loadInvoices();
                break;
            case 'create':
                elements.createSection.classList.remove('hidden');
                elements.createdInvoiceResult.classList.add('hidden');
                elements.createInvoiceForm.classList.remove('hidden');
                break;
            case 'webhooks':
                elements.webhooksSection.classList.remove('hidden');
                loadWebhooks();
                break;
        }
    };

    // ===== Invoices =====
    async function loadInvoices() {
        const offset = (state.currentPage - 1) * CONFIG.INVOICES_PER_PAGE;
        let endpoint = `/invoices?limit=${CONFIG.INVOICES_PER_PAGE}&offset=${offset}`;
        
        if (state.statusFilter) {
            endpoint += `&status=${state.statusFilter}`;
        }
        
        elements.invoicesTableBody.innerHTML = `
            <tr class="loading-row">
                <td colspan="6">
                    <div class="spinner"></div>
                    Загрузка...
                </td>
            </tr>
        `;
        
        try {
            const data = await apiRequest(endpoint);
            state.invoices = data.items;
            state.totalInvoices = data.total;
            
            renderInvoices();
            renderPagination();
            
        } catch (error) {
            elements.invoicesTableBody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="6">Ошибка загрузки: ${escapeHtml(error.message)}</td>
                </tr>
            `;
        }
    }

    function renderInvoices() {
        if (state.invoices.length === 0) {
            elements.invoicesTableBody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="6">Нет инвойсов</td>
                </tr>
            `;
            return;
        }
        
        elements.invoicesTableBody.innerHTML = state.invoices.map(invoice => `
            <tr>
                <td>
                    <code title="${escapeHtml(invoice.id)}">${escapeHtml(invoice.public_id)}</code>
                </td>
                <td>
                    <strong>${formatAmount(invoice.amount)}</strong>
                    <span class="text-muted">${escapeHtml(invoice.asset)}</span>
                </td>
                <td>
                    <span class="status-badge status-${invoice.status.toLowerCase()}">
                        ${getStatusText(invoice.status)}
                    </span>
                </td>
                <td>${formatDate(invoice.created_at)}</td>
                <td>${formatDate(invoice.expires_at)}</td>
                <td>
                    <div class="table-actions">
                        <button class="action-btn" onclick="copyToClipboard('${escapeHtml(invoice.hosted_url)}')" title="Копировать ссылку">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                            </svg>
                        </button>
                        <a href="${escapeHtml(invoice.hosted_url)}" target="_blank" class="action-btn" title="Открыть страницу оплаты">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                                <polyline points="15 3 21 3 21 9"/>
                                <line x1="10" y1="14" x2="21" y2="3"/>
                            </svg>
                        </a>
                    </div>
                </td>
            </tr>
        `).join('');
    }

    function renderPagination() {
        const totalPages = Math.ceil(state.totalInvoices / CONFIG.INVOICES_PER_PAGE);
        
        if (totalPages <= 1) {
            elements.invoicesPagination.innerHTML = '';
            return;
        }
        
        let html = '';
        
        // Previous button
        html += `<button ${state.currentPage === 1 ? 'disabled' : ''} onclick="changePage(${state.currentPage - 1})">←</button>`;
        
        // Page numbers
        for (let i = 1; i <= totalPages; i++) {
            if (i === 1 || i === totalPages || (i >= state.currentPage - 1 && i <= state.currentPage + 1)) {
                html += `<button class="${i === state.currentPage ? 'active' : ''}" onclick="changePage(${i})">${i}</button>`;
            } else if (i === state.currentPage - 2 || i === state.currentPage + 2) {
                html += `<button disabled>...</button>`;
            }
        }
        
        // Next button
        html += `<button ${state.currentPage === totalPages ? 'disabled' : ''} onclick="changePage(${state.currentPage + 1})">→</button>`;
        
        elements.invoicesPagination.innerHTML = html;
    }

    window.changePage = function(page) {
        state.currentPage = page;
        loadInvoices();
    };

    function handleFilterChange() {
        state.statusFilter = elements.filterStatus.value;
        state.currentPage = 1;
        loadInvoices();
    }

    // ===== Create Invoice =====
    async function handleCreateInvoice(e) {
        e.preventDefault();
        
        const formData = new FormData(elements.createInvoiceForm);
        
        // Get selected chains
        const selectedChains = [];
        document.querySelectorAll('input[name="chains"]:checked').forEach(cb => {
            selectedChains.push(cb.value);
        });
        
        if (selectedChains.length === 0) {
            showError(elements.createError, 'Выберите хотя бы одну сеть');
            return;
        }
        
        const payload = {
            amount: formData.get('amount'),
            asset: formData.get('asset'),
            allowed_chains: selectedChains,
            ttl_minutes: parseInt(formData.get('ttl_minutes')) || 60
        };
        
        // Add metadata if provided
        const orderId = formData.get('order_id');
        const comment = formData.get('comment');
        if (orderId || comment) {
            payload.metadata = {};
            if (orderId) payload.metadata.order_id = orderId;
            if (comment) payload.metadata.comment = comment;
        }
        
        setButtonLoading(elements.createInvoiceBtn, true);
        hideError(elements.createError);
        
        try {
            const invoice = await apiRequest('/invoices', {
                method: 'POST',
                body: JSON.stringify(payload)
            });
            
            // Show result
            showInvoiceResult(invoice);
            showToast('Инвойс успешно создан', 'success');
            
        } catch (error) {
            showError(elements.createError, error.message);
        } finally {
            setButtonLoading(elements.createInvoiceBtn, false);
        }
    }

    function showInvoiceResult(invoice) {
        elements.createInvoiceForm.classList.add('hidden');
        elements.createdInvoiceResult.classList.remove('hidden');
        
        document.getElementById('resultInvoiceId').textContent = invoice.id;
        document.getElementById('resultPublicId').textContent = invoice.public_id;
        document.getElementById('resultAmount').textContent = `${formatAmount(invoice.amount)} ${invoice.asset}`;
        document.getElementById('resultExpires').textContent = formatDate(invoice.expires_at);
        document.getElementById('resultHostedUrl').value = invoice.hosted_url;
        document.getElementById('resultHostedLink').href = invoice.hosted_url;
    }

    window.createAnother = function() {
        elements.createdInvoiceResult.classList.add('hidden');
        elements.createInvoiceForm.classList.remove('hidden');
        elements.createInvoiceForm.reset();
        
        // Re-check all chain checkboxes
        document.querySelectorAll('input[name="chains"]').forEach(cb => {
            cb.checked = true;
        });
    };

    window.copyHostedUrl = function() {
        const url = document.getElementById('resultHostedUrl').value;
        copyToClipboard(url);
    };

    // ===== Webhooks =====
    async function loadWebhooks() {
        elements.webhooksTableBody.innerHTML = `
            <tr class="loading-row">
                <td colspan="5">
                    <div class="spinner"></div>
                    Загрузка...
                </td>
            </tr>
        `;
        
        try {
            const data = await apiRequest('/webhooks');
            state.webhooks = data.items;
            renderWebhooks();
            
        } catch (error) {
            elements.webhooksTableBody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="5">Ошибка загрузки: ${escapeHtml(error.message)}</td>
                </tr>
            `;
        }
    }

    function renderWebhooks() {
        if (state.webhooks.length === 0) {
            elements.webhooksTableBody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="5">Нет webhooks</td>
                </tr>
            `;
            return;
        }
        
        elements.webhooksTableBody.innerHTML = state.webhooks.map(webhook => `
            <tr>
                <td>
                    <code>${escapeHtml(webhook.url)}</code>
                </td>
                <td>
                    ${webhook.events.map(e => `<span class="status-badge status-seen_onchain">${escapeHtml(e)}</span>`).join(' ')}
                </td>
                <td>
                    <span class="status-badge ${webhook.is_active ? 'status-confirmed' : 'status-expired'}">
                        ${webhook.is_active ? 'Активен' : 'Неактивен'}
                    </span>
                </td>
                <td>${formatDate(webhook.created_at)}</td>
                <td>
                    <div class="table-actions">
                        <button class="action-btn" onclick="deleteWebhook('${webhook.id}')" title="Удалить">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"/>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                            </svg>
                        </button>
                    </div>
                </td>
            </tr>
        `).join('');
    }

    function openWebhookModal() {
        elements.webhookModal.classList.remove('hidden');
        hideError(elements.webhookError);
        elements.webhookForm.reset();
        
        // Check all events by default
        document.querySelectorAll('#webhookForm input[name="events"]').forEach(cb => {
            cb.checked = true;
        });
    }

    window.closeWebhookModal = function() {
        elements.webhookModal.classList.add('hidden');
    };

    async function handleCreateWebhook(e) {
        e.preventDefault();
        
        const formData = new FormData(elements.webhookForm);
        
        const selectedEvents = [];
        document.querySelectorAll('#webhookForm input[name="events"]:checked').forEach(cb => {
            selectedEvents.push(cb.value);
        });
        
        if (selectedEvents.length === 0) {
            showError(elements.webhookError, 'Выберите хотя бы одно событие');
            return;
        }
        
        const payload = {
            url: formData.get('url'),
            events: selectedEvents
        };
        
        try {
            await apiRequest('/webhooks', {
                method: 'POST',
                body: JSON.stringify(payload)
            });
            
            closeWebhookModal();
            loadWebhooks();
            showToast('Webhook успешно добавлен', 'success');
            
        } catch (error) {
            showError(elements.webhookError, error.message);
        }
    }

    window.deleteWebhook = async function(id) {
        if (!confirm('Вы уверены, что хотите удалить этот webhook?')) {
            return;
        }
        
        try {
            await apiRequest(`/webhooks/${id}`, {
                method: 'DELETE'
            });
            
            loadWebhooks();
            showToast('Webhook удалён', 'success');
            
        } catch (error) {
            showToast('Ошибка удаления: ' + error.message, 'error');
        }
    };

    // ===== Utilities =====
    function setButtonLoading(button, loading) {
        button.classList.toggle('loading', loading);
        button.disabled = loading;
    }

    function showError(element, message) {
        element.textContent = message;
        element.classList.remove('hidden');
    }

    function hideError(element) {
        element.classList.add('hidden');
    }

    function showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        
        elements.toastContainer.appendChild(toast);
        
        setTimeout(() => {
            toast.remove();
        }, 3000);
    }

    window.copyToClipboard = function(text) {
        navigator.clipboard.writeText(text).then(() => {
            showToast('Скопировано в буфер обмена', 'success');
        }).catch(() => {
            showToast('Ошибка копирования', 'error');
        });
    };

    function formatAmount(amount) {
        const num = parseFloat(amount);
        if (isNaN(num)) return amount;
        return num.toLocaleString('ru-RU', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    function formatDate(dateStr) {
        const date = new Date(dateStr);
        return date.toLocaleString('ru-RU', {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        });
    }

    function getStatusText(status) {
        const texts = {
            'CREATED': 'Создан',
            'AWAITING_PAYMENT': 'Ожидает',
            'SEEN_ONCHAIN': 'В блокчейне',
            'CONFIRMED': 'Подтверждён',
            'EXPIRED': 'Истёк'
        };
        return texts[status] || status;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ===== Start Application =====
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
