/**
 * Payment Page Application
 * Безопасный и надёжный интерфейс оплаты
 * 
 * Особенности:
 * - Корректная обработка времени жизни инвойса
 * - Динамический выбор сети и токена с учётом ограничений мерчанта
 * - Real-time обновление статуса
 * - Безопасное взаимодействие с API
 */

(function() {
    'use strict';

    // ===== Configuration =====
    const CONFIG = {
        // API endpoints
        API_BASE: '/pay',
        
        // Polling intervals (ms)
        POLL_INTERVAL_AWAITING: 5000,    // Ожидание оплаты
        POLL_INTERVAL_CONFIRMING: 3000,  // Подтверждение транзакции
        
        // Timer update interval (ms)
        TIMER_INTERVAL: 1000,
        
        // QR code settings
        QR_SIZE: 180,
        QR_MARGIN: 1,
        QR_COLORS: {
            dark: '#1f2937',
            light: '#ffffff'
        }
    };

    // ===== State =====
    const state = {
        publicId: null,
        invoiceData: null,
        selectedChain: null,
        selectedToken: null,
        expiresAt: null,
        ttlMinutes: null,
        isPolling: false,
        pollTimer: null,
        countdownTimer: null,
        isExpired: false
    };

    // ===== DOM Elements =====
    const elements = {};

    // ===== Initialization =====
    function init() {
        // Get public_id from URL
        const pathParts = window.location.pathname.split('/');
        state.publicId = pathParts[pathParts.length - 1];
        
        if (!state.publicId) {
            showError('Неверная ссылка', 'ID инвойса не найден в URL');
            return;
        }

        // Cache DOM elements
        cacheElements();
        
        // Setup event listeners
        setupEventListeners();
        
        // Load invoice data
        loadInvoiceData();
    }

    function cacheElements() {
        // States
        elements.loadingState = document.getElementById('loadingState');
        elements.errorState = document.getElementById('errorState');
        elements.paymentContainer = document.getElementById('paymentContainer');
        
        // Error
        elements.errorTitle = document.getElementById('errorTitle');
        elements.errorMessage = document.getElementById('errorMessage');
        
        // Header
        elements.merchantName = document.getElementById('merchantName');
        elements.invoicePublicId = document.getElementById('invoicePublicId');
        
        // Amount
        elements.amountValue = document.getElementById('amountValue');
        elements.amountCurrency = document.getElementById('amountCurrency');
        elements.statusBadge = document.getElementById('statusBadge');
        elements.statusText = document.getElementById('statusText');
        
        // Timer
        elements.timerSection = document.getElementById('timerSection');
        elements.timerValue = document.getElementById('timerValue');
        elements.timerProgressBar = document.getElementById('timerProgressBar');
        
        // Selection
        elements.selectionSection = document.getElementById('selectionSection');
        elements.chainGrid = document.getElementById('chainGrid');
        elements.tokenGrid = document.getElementById('tokenGrid');
        elements.confirmSelectionBtn = document.getElementById('confirmSelectionBtn');
        
        // Payment
        elements.paymentSection = document.getElementById('paymentSection');
        elements.selectedChainBadge = document.getElementById('selectedChainBadge');
        elements.selectedTokenBadge = document.getElementById('selectedTokenBadge');
        elements.qrCanvas = document.getElementById('qrCanvas');
        elements.depositAddress = document.getElementById('depositAddress');
        elements.copyAddressBtn = document.getElementById('copyAddressBtn');
        elements.sendAmountValue = document.getElementById('sendAmountValue');
        elements.sendAmountCurrency = document.getElementById('sendAmountCurrency');
        elements.copyAmountBtn = document.getElementById('copyAmountBtn');
        elements.explorerLink = document.getElementById('explorerLink');
        elements.noteToken = document.getElementById('noteToken');
        elements.noteChain = document.getElementById('noteChain');
        
        // Token Contract
        elements.tokenContractNote = document.getElementById('tokenContractNote');
        elements.tokenContractAddress = document.getElementById('tokenContractAddress');
        elements.copyContractBtn = document.getElementById('copyContractBtn');
        elements.tokenExplorerLink = document.getElementById('tokenExplorerLink');
        
        // Confirmation
        elements.confirmationSection = document.getElementById('confirmationSection');
        elements.confirmationProgressBar = document.getElementById('confirmationProgressBar');
        elements.confirmationsCurrentText = document.getElementById('confirmationsCurrentText');
        elements.confirmationsRequiredText = document.getElementById('confirmationsRequiredText');
        elements.txLink = document.getElementById('txLink');
        elements.txHashText = document.getElementById('txHashText');
        elements.copyTxHashBtn = document.getElementById('copyTxHashBtn');
        elements.blocksRemainingText = document.getElementById('blocksRemainingText');
        elements.timeRemainingText = document.getElementById('timeRemainingText');
        
        // Success
        elements.successSection = document.getElementById('successSection');
        elements.successTxLink = document.getElementById('successTxLink');
        
        // Expired
        elements.expiredSection = document.getElementById('expiredSection');
    }

    function setupEventListeners() {
        // Confirm selection button
        elements.confirmSelectionBtn.addEventListener('click', handleConfirmSelection);
        
        // Copy buttons
        elements.copyAddressBtn.addEventListener('click', () => copyToClipboard('depositAddress', elements.copyAddressBtn));
        elements.copyAmountBtn.addEventListener('click', () => copyToClipboard('sendAmountValue', elements.copyAmountBtn));
        
        // Contract copy button (if exists)
        if (elements.copyContractBtn) {
            elements.copyContractBtn.addEventListener('click', () => copyToClipboard('tokenContractAddress', elements.copyContractBtn));
        }
        
        // TX Hash copy button (if exists)
        if (elements.copyTxHashBtn) {
            elements.copyTxHashBtn.addEventListener('click', () => copyToClipboard('txHashText', elements.copyTxHashBtn));
        }
    }

    // ===== API Calls =====
    async function loadInvoiceData() {
        try {
            const response = await fetch(`${CONFIG.API_BASE}/${state.publicId}/info`);
            
            if (!response.ok) {
                if (response.status === 404) {
                    showError('Инвойс не найден', 'Проверьте правильность ссылки');
                } else {
                    const data = await response.json().catch(() => ({}));
                    showError('Ошибка загрузки', data.detail || 'Не удалось загрузить данные инвойса');
                }
                return;
            }
            
            const data = await response.json();
            state.invoiceData = data;
            
            // Парсим дату истечения - убеждаемся что она в правильном формате
            // Если сервер не отправляет timezone, добавляем Z (UTC)
            let expiresAtStr = data.expires_at;
            if (expiresAtStr && !expiresAtStr.endsWith('Z') && !expiresAtStr.includes('+')) {
                expiresAtStr += 'Z';
            }
            state.expiresAt = new Date(expiresAtStr);
            
            state.ttlMinutes = data.ttl_minutes || 60;
            state.isExpired = data.is_expired;
            
            // Debug logging
            console.log('Invoice data:', {
                expires_at_raw: data.expires_at,
                expires_at_parsed: state.expiresAt.toISOString(),
                now: new Date().toISOString(),
                is_expired_from_server: data.is_expired,
                diff_ms: state.expiresAt - new Date()
            });
            
            // Check if already have selected chain/token
            if (data.selected_chain) {
                state.selectedChain = data.selected_chain;
            }
            if (data.selected_token) {
                state.selectedToken = data.selected_token;
            } else {
                // Default to invoice asset
                state.selectedToken = data.asset;
            }
            
            renderInvoice(data);
            
        } catch (error) {
            console.error('Error loading invoice:', error);
            showError('Ошибка сети', 'Проверьте подключение к интернету');
        }
    }

    async function selectPaymentOption(chain, token) {
        const response = await fetch(`${CONFIG.API_BASE}/${state.publicId}/select`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ chain, token })
        });
        
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || 'Ошибка выбора сети');
        }
        
        return response.json();
    }

    async function pollStatus() {
        if (!state.isPolling) return;
        
        try {
            const response = await fetch(`${CONFIG.API_BASE}/${state.publicId}/status`);
            
            if (!response.ok) {
                throw new Error('Failed to fetch status');
            }
            
            const data = await response.json();
            handleStatusUpdate(data);
            
        } catch (error) {
            console.error('Error polling status:', error);
        }
        
        // Schedule next poll
        if (state.isPolling) {
            const interval = state.invoiceData?.status === 'SEEN_ONCHAIN' 
                ? CONFIG.POLL_INTERVAL_CONFIRMING 
                : CONFIG.POLL_INTERVAL_AWAITING;
            state.pollTimer = setTimeout(pollStatus, interval);
        }
    }

    // ===== Rendering =====
    function renderInvoice(data) {
        // Hide loading, show container
        elements.loadingState.classList.add('hidden');
        elements.paymentContainer.classList.remove('hidden');
        
        // Header
        elements.merchantName.textContent = data.merchant_name || '';
        elements.invoicePublicId.textContent = data.public_id;
        
        // Amount
        elements.amountValue.textContent = formatAmount(data.amount);
        elements.amountCurrency.textContent = data.asset;
        
        // Update status
        updateStatus(data.status, data.is_expired);
        
        // Check states
        if (data.is_expired || data.status === 'EXPIRED') {
            showExpiredState();
            return;
        }
        
        if (data.status === 'CONFIRMED') {
            showSuccessState();
            return;
        }
        
        // Start countdown
        startCountdown();
        
        // If already have deposit address, show payment section
        if (data.deposit_address) {
            showPaymentSection(data);
            startPolling();
        } else {
            // Show selection UI
            renderChainSelection(data.allowed_chains);
            renderTokenSelection(data.asset);
        }
    }

    function renderChainSelection(allowedChains) {
        // Chain configurations
        const chainConfigs = {
            base: { name: 'Base', symbol: 'ETH' },
            arbitrum: { name: 'Arbitrum One', symbol: 'ETH' },
            bsc: { name: 'BNB Chain', symbol: 'BNB' },
            polygon: { name: 'Polygon', symbol: 'MATIC' },
            avax: { name: 'Avalanche', symbol: 'AVAX' },
            optimism: { name: 'Optimism', symbol: 'ETH' }
        };
        
        elements.chainGrid.innerHTML = '';
        
        allowedChains.forEach(chainId => {
            const config = chainConfigs[chainId] || { name: chainId, symbol: '' };
            
            const chainEl = document.createElement('div');
            chainEl.className = 'chain-option';
            chainEl.dataset.chain = chainId;
            chainEl.tabIndex = 0;
            chainEl.setAttribute('role', 'button');
            chainEl.setAttribute('aria-pressed', 'false');
            
            chainEl.innerHTML = `
                <div class="chain-name">${escapeHtml(config.name)}</div>
                <div class="chain-symbol">${escapeHtml(config.symbol)}</div>
            `;
            
            chainEl.addEventListener('click', () => selectChain(chainId));
            chainEl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    selectChain(chainId);
                }
            });
            
            elements.chainGrid.appendChild(chainEl);
        });
    }

    function renderTokenSelection(invoiceAsset) {
        // Токен фиксирован для инвойса - показываем только его без возможности выбора
        const tokenConfigs = {
            'USDT': { name: 'Tether USDT', icon: '₮', class: 'usdt' },
            'USDC': { name: 'USD Coin', icon: '$', class: 'usdc' }
        };
        
        const token = tokenConfigs[invoiceAsset] || { name: invoiceAsset, icon: '$', class: 'default' };
        
        elements.tokenGrid.innerHTML = '';
        
        const tokenEl = document.createElement('div');
        tokenEl.className = `token-option ${token.class} selected`;
        tokenEl.dataset.token = invoiceAsset;
        tokenEl.setAttribute('aria-selected', 'true');
        
        tokenEl.innerHTML = `
            <div class="token-icon">${token.icon}</div>
            <div class="token-name">${escapeHtml(token.name)}</div>
            <div class="token-fixed">Фиксированный токен для оплаты</div>
        `;
        
        // Токен уже выбран и не меняется
        state.selectedToken = invoiceAsset;
        
        elements.tokenGrid.appendChild(tokenEl);
    }

    function showPaymentSection(data) {
        // Hide selection, show payment
        elements.selectionSection.classList.add('hidden');
        elements.paymentSection.classList.remove('hidden');
        
        // Update badges
        const chainConfigs = {
            base: 'Base',
            arbitrum: 'Arbitrum',
            bsc: 'BNB Chain',
            polygon: 'Polygon',
            avax: 'Avalanche',
            optimism: 'Optimism'
        };
        
        // Унифицируем данные из /info и /select ответов
        const chain = data.chain || data.selected_chain;
        const token = data.token || data.selected_token || data.asset;
        const chainName = data.chain_name || data.selected_chain_name || chainConfigs[chain] || chain;
        const amount = data.amount;
        
        elements.selectedChainBadge.textContent = chainName;
        elements.selectedTokenBadge.textContent = token;
        
        // Deposit address
        elements.depositAddress.textContent = data.deposit_address;
        
        // Amount
        elements.sendAmountValue.textContent = formatAmount(amount);
        elements.sendAmountCurrency.textContent = token;
        
        // Notes
        elements.noteToken.textContent = token;
        elements.noteChain.textContent = chainName;
        
        // Generate QR code with deposit address
        generateQRCode(data.deposit_address);
        
        // Explorer link for deposit address
        if (data.explorer_address_url) {
            elements.explorerLink.href = data.explorer_address_url;
            elements.explorerLink.classList.remove('hidden');
        }
        
        // Token contract info
        if (data.token_contract && elements.tokenContractAddress) {
            elements.tokenContractAddress.textContent = data.token_contract;
            if (elements.tokenExplorerLink && data.explorer_token_url) {
                elements.tokenExplorerLink.href = data.explorer_token_url;
            }
            if (elements.tokenContractNote) {
                elements.tokenContractNote.classList.remove('hidden');
            }
        }
    }

    // Среднее время блока по сетям (в секундах)
    const BLOCK_TIMES = {
        base: 2,
        optimism: 2,
        polygon: 2,
        bsc: 3,
        avalanche: 2,
        ethereum: 12,
        arbitrum: 0.25,
        default: 3
    };

    function formatTimeRemaining(seconds) {
        if (seconds < 60) {
            return `${Math.ceil(seconds)} сек`;
        } else if (seconds < 3600) {
            const mins = Math.ceil(seconds / 60);
            return `${mins} мин`;
        } else {
            const hours = Math.floor(seconds / 3600);
            const mins = Math.ceil((seconds % 3600) / 60);
            return `${hours} ч ${mins} мин`;
        }
    }

    function showConfirmationSection(data) {
        elements.confirmationSection.classList.remove('hidden');
        
        const current = data.confirmations || 0;
        const required = data.required_confirmations || 12;
        const remaining = Math.max(0, required - current);
        const percent = Math.min(100, (current / required) * 100);
        
        // Update confirmation counts
        elements.confirmationsCurrentText.textContent = current;
        elements.confirmationsRequiredText.textContent = required;
        elements.confirmationProgressBar.style.width = `${percent}%`;
        
        // Update blocks remaining
        if (elements.blocksRemainingText) {
            elements.blocksRemainingText.textContent = remaining;
        }
        
        // Estimate time remaining based on chain
        if (elements.timeRemainingText && state.invoiceData) {
            const chain = state.invoiceData.selected_chain || data.chain || 'default';
            const blockTime = BLOCK_TIMES[chain] || BLOCK_TIMES.default;
            const secondsRemaining = remaining * blockTime;
            elements.timeRemainingText.textContent = formatTimeRemaining(secondsRemaining);
        }
        
        // Display TX hash
        if (data.tx_hash && elements.txHashText) {
            elements.txHashText.textContent = data.tx_hash;
        }
        
        // Explorer link
        if (data.explorer_tx_url) {
            elements.txLink.href = data.explorer_tx_url;
            elements.txLink.classList.remove('hidden');
        }
    }

    function showSuccessState(txUrl) {
        // Hide other sections
        elements.selectionSection.classList.add('hidden');
        elements.paymentSection.classList.add('hidden');
        elements.confirmationSection.classList.add('hidden');
        elements.timerSection.classList.add('hidden');
        elements.expiredSection.classList.add('hidden');
        
        // Show success
        elements.successSection.classList.remove('hidden');
        
        if (txUrl) {
            elements.successTxLink.href = txUrl;
            elements.successTxLink.classList.remove('hidden');
        }
        
        // Stop polling
        stopPolling();
        stopCountdown();
    }

    function showExpiredState() {
        // Hide other sections
        elements.selectionSection.classList.add('hidden');
        elements.paymentSection.classList.add('hidden');
        elements.confirmationSection.classList.add('hidden');
        elements.successSection.classList.add('hidden');
        
        // Show expired
        elements.expiredSection.classList.remove('hidden');
        
        // Update timer
        elements.timerSection.classList.add('expired');
        elements.timerValue.textContent = 'Истекло';
        elements.timerProgressBar.style.width = '0%';
        
        // Update status
        updateStatus('EXPIRED', true);
        
        // Stop polling
        stopPolling();
        stopCountdown();
    }

    function updateStatus(status, isExpired) {
        const statusTexts = {
            'CREATED': 'Ожидание выбора сети',
            'AWAITING_PAYMENT': 'Ожидание оплаты',
            'SEEN_ONCHAIN': 'Подтверждение...',
            'CONFIRMED': 'Оплачено ✓',
            'EXPIRED': 'Время истекло'
        };
        
        const displayStatus = isExpired ? 'EXPIRED' : status;
        
        elements.statusText.textContent = statusTexts[displayStatus] || status;
        elements.statusBadge.className = `status-badge status-${displayStatus.toLowerCase()}`;
        
        // Update state
        if (state.invoiceData) {
            state.invoiceData.status = status;
        }
    }

    // ===== Event Handlers =====
    function selectChain(chainId) {
        state.selectedChain = chainId;
        
        // Update UI
        document.querySelectorAll('.chain-option').forEach(el => {
            const isSelected = el.dataset.chain === chainId;
            el.classList.toggle('selected', isSelected);
            el.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
        });
        
        updateConfirmButton();
    }

    function selectToken(tokenId) {
        state.selectedToken = tokenId;
        
        // Update UI
        document.querySelectorAll('.token-option').forEach(el => {
            const isSelected = el.dataset.token === tokenId;
            el.classList.toggle('selected', isSelected);
            el.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
        });
        
        updateConfirmButton();
    }

    function updateConfirmButton() {
        const canConfirm = state.selectedChain && state.selectedToken && !state.isExpired;
        elements.confirmSelectionBtn.disabled = !canConfirm;
    }

    async function handleConfirmSelection() {
        if (!state.selectedChain || !state.selectedToken) return;
        
        const btn = elements.confirmSelectionBtn;
        
        // Show loading state
        btn.classList.add('loading');
        btn.disabled = true;
        btn.querySelector('.btn-text').classList.add('hidden');
        btn.querySelector('.btn-loading').classList.remove('hidden');
        
        try {
            const data = await selectPaymentOption(state.selectedChain, state.selectedToken);
            
            // Update state
            state.invoiceData.selected_chain = data.chain;
            state.invoiceData.selected_token = data.token;
            state.invoiceData.deposit_address = data.deposit_address;
            
            // Show payment section
            showPaymentSection({
                ...state.invoiceData,
                selected_chain: data.chain,
                selected_token: data.token,
                deposit_address: data.deposit_address,
                amount: data.amount
            });
            
            // Update explorer link
            if (data.explorer_address_url) {
                elements.explorerLink.href = data.explorer_address_url;
                elements.explorerLink.classList.remove('hidden');
            }
            
            // Update status
            updateStatus('AWAITING_PAYMENT', false);
            
            // Start polling
            startPolling();
            
        } catch (error) {
            console.error('Error selecting payment option:', error);
            
            // Show error (could be expired)
            if (error.message.includes('expired')) {
                showExpiredState();
            } else {
                alert('Ошибка: ' + error.message);
            }
            
            // Reset button
            btn.classList.remove('loading');
            btn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('hidden');
            btn.querySelector('.btn-loading').classList.add('hidden');
        }
    }

    function handleStatusUpdate(data) {
        // Check for expiration
        if (data.is_expired) {
            showExpiredState();
            return;
        }
        
        // Update status
        updateStatus(data.status, data.is_expired);
        
        // Handle different states
        switch (data.status) {
            case 'CONFIRMED':
                showSuccessState(data.explorer_tx_url);
                break;
                
            case 'SEEN_ONCHAIN':
                showConfirmationSection(data);
                break;
        }
    }

    // ===== Utilities =====
    function generateQRCode(address) {
        console.log('Generating QR code for:', address);
        console.log('QRCode library available:', typeof QRCode !== 'undefined');
        console.log('Canvas element:', elements.qrCanvas);
        
        if (!elements.qrCanvas) {
            console.error('QR Canvas element not found');
            return;
        }
        
        if (typeof QRCode === 'undefined') {
            console.error('QRCode library not loaded, using fallback');
            // Fallback: show address as text
            const parent = elements.qrCanvas.parentElement;
            if (parent) {
                const fallback = document.createElement('div');
                fallback.className = 'qr-fallback';
                fallback.innerHTML = `
                    <div class="qr-fallback-text">QR код недоступен</div>
                    <code class="qr-fallback-address">${escapeHtml(address)}</code>
                `;
                elements.qrCanvas.style.display = 'none';
                parent.appendChild(fallback);
            }
            return;
        }
        
        try {
            QRCode.toCanvas(elements.qrCanvas, address, {
                width: CONFIG.QR_SIZE,
                margin: CONFIG.QR_MARGIN,
                color: CONFIG.QR_COLORS
            }, (error) => {
                if (error) {
                    console.error('Error generating QR code:', error);
                } else {
                    console.log('QR code generated successfully');
                }
            });
        } catch (err) {
            console.error('Exception generating QR code:', err);
        }
    }

    function copyToClipboard(elementId, button) {
        const element = document.getElementById(elementId);
        if (!element) return;
        
        const text = element.textContent;
        
        navigator.clipboard.writeText(text).then(() => {
            // Visual feedback
            const originalText = button.querySelector('.copy-text');
            if (originalText) {
                const original = originalText.textContent;
                originalText.textContent = 'Скопировано!';
                button.classList.add('copied');
                
                setTimeout(() => {
                    originalText.textContent = original;
                    button.classList.remove('copied');
                }, 2000);
            } else {
                button.classList.add('copied');
                setTimeout(() => button.classList.remove('copied'), 2000);
            }
        }).catch(err => {
            console.error('Failed to copy:', err);
            // Fallback: select text
            const range = document.createRange();
            range.selectNode(element);
            window.getSelection().removeAllRanges();
            window.getSelection().addRange(range);
        });
    }

    function startCountdown() {
        if (state.countdownTimer) {
            clearInterval(state.countdownTimer);
        }
        
        updateCountdown();
        state.countdownTimer = setInterval(updateCountdown, CONFIG.TIMER_INTERVAL);
    }

    function stopCountdown() {
        if (state.countdownTimer) {
            clearInterval(state.countdownTimer);
            state.countdownTimer = null;
        }
    }

    function updateCountdown() {
        if (!state.expiresAt) return;
        
        const now = new Date();
        const diff = state.expiresAt - now;
        
        if (diff <= 0) {
            state.isExpired = true;
            showExpiredState();
            return;
        }
        
        // Format time
        const hours = Math.floor(diff / 3600000);
        const minutes = Math.floor((diff % 3600000) / 60000);
        const seconds = Math.floor((diff % 60000) / 1000);
        
        let timeStr;
        if (hours > 0) {
            timeStr = `${hours}:${pad(minutes)}:${pad(seconds)}`;
        } else {
            timeStr = `${minutes}:${pad(seconds)}`;
        }
        
        elements.timerValue.textContent = timeStr;
        
        // Update progress bar
        // Calculate total duration based on TTL
        const ttlMs = state.ttlMinutes * 60 * 1000;
        const elapsed = ttlMs - diff;
        const progress = Math.max(0, Math.min(100, 100 - (elapsed / ttlMs * 100)));
        elements.timerProgressBar.style.width = `${progress}%`;
        
        // Visual warning when less than 5 minutes left
        if (diff < 5 * 60 * 1000) {
            elements.timerSection.classList.add('warning');
        }
    }

    function startPolling() {
        if (state.isPolling) return;
        
        state.isPolling = true;
        pollStatus();
    }

    function stopPolling() {
        state.isPolling = false;
        if (state.pollTimer) {
            clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function showError(title, message) {
        elements.loadingState.classList.add('hidden');
        elements.paymentContainer.classList.add('hidden');
        elements.errorState.classList.remove('hidden');
        
        elements.errorTitle.textContent = title;
        elements.errorMessage.textContent = message;
    }

    function formatAmount(amount) {
        // Parse and format decimal
        const num = parseFloat(amount);
        if (isNaN(num)) return amount;
        
        // Remove trailing zeros but keep at least 2 decimal places
        return num.toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 18
        });
    }

    function pad(num) {
        return String(num).padStart(2, '0');
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
