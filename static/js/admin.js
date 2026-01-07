/**
 * Admin Panel JavaScript
 */

const API_BASE = '/v1/admin';

// State
let currentPage = 'dashboard';
let invoicesPage = 1;
let sweepsPage = 1;
let logsPage = 1;

// Navigation
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const page = item.dataset.page;
        switchPage(page);
    });
});

function switchPage(page) {
    // Update nav
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.page === page);
    });
    
    // Update pages
    document.querySelectorAll('.page').forEach(p => {
        p.classList.toggle('active', p.id === `page-${page}`);
    });
    
    currentPage = page;
    
    // Load data
    switch (page) {
        case 'dashboard':
            refreshDashboard();
            break;
        case 'invoices':
            loadInvoices();
            break;
        case 'sweeps':
            loadSweeps();
            break;
        case 'logs':
            loadLogs();
            break;
        case 'status':
            loadSystemStatus();
            break;
    }
}

// Dashboard
async function refreshDashboard() {
    try {
        const [dashboard, status] = await Promise.all([
            fetch(`${API_BASE}/dashboard`).then(r => r.json()),
            fetch(`${API_BASE}/system-status`).then(r => r.json())
        ]);
        
        // Stats
        document.getElementById('stat-total-invoices').textContent = dashboard.total_invoices;
        document.getElementById('stat-completed-24h').textContent = status.completed_invoices_24h;
        document.getElementById('stat-pending').textContent = status.pending_invoices;
        document.getElementById('stat-failed-sweeps').textContent = status.failed_sweeps;
        
        // Volume
        document.getElementById('volume-24h').textContent = `$${parseFloat(dashboard.volume_24h).toLocaleString()}`;
        document.getElementById('volume-7d').textContent = `$${parseFloat(dashboard.volume_7d).toLocaleString()}`;
        document.getElementById('volume-30d').textContent = `$${parseFloat(dashboard.volume_30d).toLocaleString()}`;
        
        // Errors
        document.getElementById('error-rpc').textContent = dashboard.rpc_errors;
        document.getElementById('error-sweep').textContent = dashboard.sweep_errors;
        document.getElementById('error-webhook').textContent = dashboard.webhook_errors;
        
        // Chain stats
        const chainStats = document.getElementById('chain-stats');
        chainStats.innerHTML = Object.entries(dashboard.invoices_by_chain || {})
            .map(([chain, count]) => `
                <div class="chain-stat">
                    <div class="chain-name">${chain.toUpperCase()}</div>
                    <div class="chain-count">${count}</div>
                </div>
            `).join('') || '<p>No data</p>';
            
    } catch (e) {
        console.error('Failed to load dashboard:', e);
    }
}

// Invoices
async function loadInvoices(page = 1) {
    invoicesPage = page;
    const status = document.getElementById('filter-status').value;
    const chain = document.getElementById('filter-chain').value;
    
    try {
        const params = new URLSearchParams({ page, per_page: 20 });
        if (status) params.set('status', status);
        if (chain) params.set('chain', chain);
        
        const data = await fetch(`${API_BASE}/invoices?${params}`).then(r => r.json());
        
        const tbody = document.getElementById('invoices-table');
        tbody.innerHTML = data.items.map(inv => `
            <tr>
                <td>
                    <div style="font-family: monospace; font-size: 0.75rem;">${inv.public_id}</div>
                </td>
                <td>
                    <span class="status-badge ${inv.status}">${inv.status}</span>
                    ${inv.is_expired ? '<span class="status-badge expired">expired</span>' : ''}
                </td>
                <td>${inv.amount} ${inv.asset}</td>
                <td>${inv.chain || '-'}</td>
                <td>${formatDate(inv.created_at)}</td>
                <td>
                    ${inv.tx_hash ? `
                        <span class="tx-hash" title="${inv.tx_hash}">${inv.tx_hash.slice(0, 10)}...</span>
                        <br><small>${inv.confirmations}/${inv.required_confirmations} conf</small>
                    ` : '-'}
                </td>
                <td>
                    ${inv.sweep_state ? `<span class="status-badge ${inv.sweep_state}">${inv.sweep_state}</span>` : '-'}
                </td>
            </tr>
        `).join('');
        
        renderPagination('invoices-pagination', data.page, data.pages, loadInvoices);
        
    } catch (e) {
        console.error('Failed to load invoices:', e);
    }
}

// Sweeps
async function loadSweeps(page = 1) {
    sweepsPage = page;
    const state = document.getElementById('filter-sweep-state').value;
    
    try {
        const params = new URLSearchParams({ page, per_page: 20 });
        if (state) params.set('state', state);
        
        const data = await fetch(`${API_BASE}/sweeps?${params}`).then(r => r.json());
        
        const tbody = document.getElementById('sweeps-table');
        tbody.innerHTML = data.items.map(sweep => `
            <tr>
                <td>
                    <div style="font-family: monospace; font-size: 0.75rem;">${sweep.id.slice(0, 8)}...</div>
                    <small>${sweep.invoice_public_id || ''}</small>
                </td>
                <td><span class="status-badge ${sweep.state}">${sweep.state}</span></td>
                <td>${sweep.chain}</td>
                <td>${sweep.amount} ${sweep.token}</td>
                <td>${sweep.attempts}/${sweep.max_attempts}</td>
                <td>
                    ${sweep.last_error ? `<span title="${sweep.last_error}">${sweep.last_error.slice(0, 30)}...</span>` : '-'}
                </td>
                <td>
                    ${sweep.state === 'failed' ? `
                        <button class="btn btn-sm btn-primary" onclick="retrySweep('${sweep.id}')">Retry</button>
                    ` : ''}
                    ${['pending_gas', 'funding', 'sweeping'].includes(sweep.state) ? `
                        <button class="btn btn-sm btn-secondary" onclick="resetSweep('${sweep.id}')">Reset</button>
                    ` : ''}
                </td>
            </tr>
        `).join('');
        
        renderPagination('sweeps-pagination', data.page, data.pages, loadSweeps);
        
    } catch (e) {
        console.error('Failed to load sweeps:', e);
    }
}

async function retrySweep(sweepId) {
    try {
        const res = await fetch(`${API_BASE}/sweeps/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sweep_id: sweepId })
        });
        const data = await res.json();
        alert(data.message);
        loadSweeps(sweepsPage);
    } catch (e) {
        alert('Failed to retry sweep');
    }
}

async function resetSweep(sweepId) {
    try {
        const res = await fetch(`${API_BASE}/sweeps/reset`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sweep_id: sweepId, reset_to_state: 'pending_gas' })
        });
        const data = await res.json();
        alert(data.message);
        loadSweeps(sweepsPage);
    } catch (e) {
        alert('Failed to reset sweep');
    }
}

// Logs
async function loadLogs(page = 1) {
    logsPage = page;
    const level = document.getElementById('filter-log-level').value;
    const source = document.getElementById('filter-log-source').value;
    
    try {
        const params = new URLSearchParams({ page, per_page: 50 });
        if (level) params.set('level', level);
        if (source) params.set('source', source);
        
        const data = await fetch(`${API_BASE}/logs?${params}`).then(r => r.json());
        
        const container = document.getElementById('logs-container');
        container.innerHTML = data.items.map(log => `
            <div class="log-entry ${log.level}">
                <div class="log-header">
                    <span class="log-timestamp">${formatDate(log.timestamp)}</span>
                    <span class="log-source">[${log.source}]${log.chain ? ` [${log.chain}]` : ''}</span>
                </div>
                <div class="log-message">${escapeHtml(log.message)}</div>
            </div>
        `).join('') || '<p>No logs found</p>';
        
        renderPagination('logs-pagination', data.page, Math.ceil(data.total / 50), loadLogs);
        
    } catch (e) {
        console.error('Failed to load logs:', e);
    }
}

// System Status
async function loadSystemStatus() {
    try {
        const data = await fetch(`${API_BASE}/system-status`).then(r => r.json());
        
        // Status badge
        const statusBadge = document.getElementById('system-status-badge');
        statusBadge.innerHTML = `
            <span class="status-indicator ${data.status}"></span>
            <span class="status-text">System ${data.status.charAt(0).toUpperCase() + data.status.slice(1)}</span>
        `;
        
        // Funder
        const funderEl = document.getElementById('funder-status');
        if (data.funder) {
            funderEl.innerHTML = `
                <div class="funder-address">${data.funder.address}</div>
                <div class="balance-grid">
                    ${Object.entries(data.funder.balances).map(([chain, balance]) => `
                        <div class="balance-item ${data.funder.low_balance_chains.includes(chain) ? 'low' : ''}">
                            <div class="balance-chain">${chain.toUpperCase()}</div>
                            <div class="balance-value">${balance.toFixed(4)}</div>
                        </div>
                    `).join('')}
                </div>
            `;
        } else {
            funderEl.innerHTML = '<p>Funder not configured</p>';
        }
        
        // Chains
        const chainsEl = document.getElementById('chains-status');
        chainsEl.innerHTML = data.chains.map(chain => `
            <div class="chain-status-card ${chain.is_healthy ? '' : 'unhealthy'}">
                <div class="chain-status-header">
                    <span class="chain-status-name">${chain.chain_name}</span>
                    <span class="status-badge ${chain.is_healthy ? 'completed' : 'failed'}">
                        ${chain.is_healthy ? 'Healthy' : 'Unhealthy'}
                    </span>
                </div>
                <div class="chain-status-details">
                    <span class="chain-detail-label">Last scanned:</span>
                    <span class="chain-detail-value">${chain.last_scanned_block || '-'}</span>
                    
                    <span class="chain-detail-label">Latest block:</span>
                    <span class="chain-detail-value">${chain.latest_block || '-'}</span>
                    
                    <span class="chain-detail-label">Blocks behind:</span>
                    <span class="chain-detail-value">${chain.blocks_behind || 0}</span>
                    
                    <span class="chain-detail-label">Gas price:</span>
                    <span class="chain-detail-value">${chain.gas_price_gwei ? chain.gas_price_gwei.toFixed(2) + ' gwei' : '-'}</span>
                </div>
                ${chain.last_error ? `<div style="color: var(--danger); font-size: 0.75rem; margin-top: 8px;">${chain.last_error}</div>` : ''}
            </div>
        `).join('');
        
    } catch (e) {
        console.error('Failed to load system status:', e);
    }
}

// Utilities
function formatDate(dateStr) {
    const date = new Date(dateStr);
    return date.toLocaleString();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function renderPagination(containerId, current, total, callback) {
    const container = document.getElementById(containerId);
    if (total <= 1) {
        container.innerHTML = '';
        return;
    }
    
    let html = '';
    
    if (current > 1) {
        html += `<button onclick="${callback.name}(${current - 1})">← Prev</button>`;
    }
    
    for (let i = Math.max(1, current - 2); i <= Math.min(total, current + 2); i++) {
        html += `<button class="${i === current ? 'active' : ''}" onclick="${callback.name}(${i})">${i}</button>`;
    }
    
    if (current < total) {
        html += `<button onclick="${callback.name}(${current + 1})">Next →</button>`;
    }
    
    container.innerHTML = html;
}

// Auto-refresh
let refreshInterval;

function startAutoRefresh() {
    refreshInterval = setInterval(() => {
        if (currentPage === 'dashboard') {
            refreshDashboard();
        } else if (currentPage === 'status') {
            loadSystemStatus();
        }
    }, 30000); // 30 seconds
}

function stopAutoRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    refreshDashboard();
    startAutoRefresh();
});

// Handle visibility change
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        stopAutoRefresh();
    } else {
        startAutoRefresh();
        // Refresh current page
        switchPage(currentPage);
    }
});
