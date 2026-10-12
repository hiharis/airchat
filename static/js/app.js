let ws;
let username = localStorage.getItem('username');
let maxUploadBytes = 20 * 1024 * 1024; // Fallback 20 MB

// --- RECONNECT & DELTA STATE ---
let lastSeenId = null; // Stored in memory only (never localStorage)
const renderedIds = new Set();
let reconnectTimer = null;
let reconnectAttempts = 0;
const offlineTextQueue = [];

// --- NOTIFICATION & AUDIO STATE ---
let soundEnabled = true;
try {
    const s = localStorage.getItem('sound_enabled');
    if (s !== null) soundEnabled = (s === 'true');
} catch (_) {}

let desktopAlertsEnabled = false;
try {
    const d = localStorage.getItem('desktop_alerts_enabled');
    if (d !== null) desktopAlertsEnabled = (d === 'true');
} catch (_) {}

let unreadCount = 0;
const originalDocTitle = 'AirChat';
let audioCtx = null;
let lastSoundTime = 0;

// --- TYPING INDICATOR STATE ---
let lastTypingSent = 0;
const typers = new Map(); // username -> expiry timestamp
let typingExpiryTimer = null;

// --- HD TOGGLE & REACTION STATE ---
let hdActive = false;
const ALLOWED_EMOJIS = ['👍', '❤️', '😂', '😮', '😢', '🔥'];
let activePaletteMsgId = null;

// Feature check for Notification API and secure context
function isNotificationSupported() {
    return !!(window.isSecureContext && ('Notification' in window));
}

// Lazy/Safe Web Audio Context initialization
function initAudioContext() {
    try {
        if (!audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (AudioContextClass) {
                audioCtx = new AudioContextClass();
            }
        }
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().catch(() => {});
        }
    } catch (_) {}
}

// Unlock audio context on user interaction
['click', 'keydown', 'touchstart'].forEach(evt => {
    window.addEventListener(evt, () => {
        initAudioContext();
    }, { passive: true });
});

// Play a short, soft synthesized chime ping (offline-first, no audio file/CDN)
function playNotificationSound() {
    if (!soundEnabled) return;
    const now = Date.now();
    if (now - lastSoundTime < 1000) return; // Rate-limit: max 1 ping per second
    lastSoundTime = now;

    try {
        initAudioContext();
        if (!audioCtx) return;

        const doPlay = () => {
            try {
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                const t = audioCtx.currentTime;

                osc.type = 'sine';
                // Soft chime: starts at 880Hz (A5) and gently decays to 587.33Hz (D5)
                osc.frequency.setValueAtTime(880, t);
                osc.frequency.exponentialRampToValueAtTime(587.33, t + 0.15);

                // Gain envelope: fast 20ms attack to avoid clicks, exponential decay
                gain.gain.setValueAtTime(0.0001, t);
                gain.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);

                osc.connect(gain);
                gain.connect(audioCtx.destination);

                osc.start(t);
                osc.stop(t + 0.36);

                osc.onended = () => {
                    try {
                        osc.disconnect();
                        gain.disconnect();
                    } catch (_) {}
                };
            } catch (_) {}
        };

        if (audioCtx.state === 'suspended') {
            audioCtx.resume().then(doPlay).catch(() => {});
        } else {
            doPlay();
        }
    } catch (_) {}
}

// Vibration on mobile devices
function triggerVibration() {
    try {
        if ('vibrate' in navigator && typeof navigator.vibrate === 'function') {
            navigator.vibrate(80);
        }
    } catch (_) {}
}

// Tab focus & visibility state
function isTabInactive() {
    return document.hidden || !document.hasFocus();
}

function updateTitleBadge() {
    if (unreadCount > 0) {
        document.title = `(${unreadCount}) ${originalDocTitle}`;
    } else {
        document.title = originalDocTitle;
    }
}

function clearTitleBadge() {
    if (unreadCount > 0) {
        unreadCount = 0;
        updateTitleBadge();
    }
}

// Reset badge on user returning to window / tab
window.addEventListener('focus', clearTitleBadge);
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        clearTitleBadge();
    }
});
window.addEventListener('click', clearTitleBadge);

// Native desktop notification (only in secure context when permission granted)
function showDesktopNotification(data) {
    if (!desktopAlertsEnabled) return;
    if (!isNotificationSupported()) return;
    if (Notification.permission !== 'granted') return;

    try {
        const title = data.username || 'AirChat';
        let bodyText = '';
        const msgType = data.msg_type || data.type;
        if (msgType === 'file') {
            const fileName = data.original_name || deriveDisplayName(data.content || '');
            bodyText = `Sent a file: ${fileName}`;
        } else {
            bodyText = (data.content || '').toString();
        }

        if (bodyText.length > 100) {
            bodyText = bodyText.slice(0, 100) + '…';
        }

        const notif = new Notification(title, {
            body: bodyText,
            icon: 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E✨%3C/text%3E%3C/svg%3E',
            tag: 'airchat-msg',
            renotify: true
        });

        // Auto close after 5 seconds
        const timer = setTimeout(() => {
            try { notif.close(); } catch (_) {}
        }, 5000);

        notif.onclick = function() {
            clearTimeout(timer);
            try { window.focus(); } catch (_) {}
            try { notif.close(); } catch (_) {}
        };
    } catch (_) {}
}

// Master handler for incoming message notifications
function handleIncomingNotification(data) {
    // Only notify for live messages from another user while tab/window is inactive
    if (!isTabInactive()) return;
    if (data.username === username) return;

    unreadCount++;
    updateTitleBadge();
    playNotificationSound();
    triggerVibration();
    showDesktopNotification(data);
}

// --- SETTINGS INITIALIZATION ---
const savedTheme = localStorage.getItem('theme') || 'dark';
const savedFont = localStorage.getItem('font') || 'medium';
document.documentElement.setAttribute('data-theme', savedTheme);
document.documentElement.setAttribute('data-font', savedFont);

window.onload = () => {
    if (username) {
        document.getElementById('username-input').value = username;
    }
    document.getElementById('theme-select').value = savedTheme;
    document.getElementById('font-select').value = savedFont;
    updateThemeButton();
    updateSettingsModalControls();
    updateHdButton();

    // Fetch server max upload size dynamically
    fetch('/api/config')
        .then(res => res.json())
        .then(data => {
            if (data && typeof data.max_upload_bytes === 'number') {
                maxUploadBytes = data.max_upload_bytes;
            }
        })
        .catch(() => {});
};

// --- CHAT LOGIC ---
function joinChat() {
    username = document.getElementById('username-input').value.trim();
    if (!username) return;

    localStorage.setItem('username', username);
    document.getElementById('login-modal').style.display = 'none';
    document.getElementById('app').style.display = 'flex';

    connectWebSocket();
}

function connectWebSocket() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let url = `${protocol}//${window.location.host}/ws/${encodeURIComponent(username)}`;
    if (lastSeenId !== null && Number.isInteger(lastSeenId)) {
        url += `?after=${lastSeenId}`;
    }

    ws = new WebSocket(url);

    ws.onopen = () => {
        reconnectAttempts = 0;
        const pill = document.getElementById('connection-pill');
        if (pill) pill.style.display = 'none';

        // Send queued offline messages
        while (offlineTextQueue.length > 0) {
            const queued = offlineTextQueue.shift();
            try {
                ws.send(JSON.stringify({ type: 'message', content: queued }));
            } catch (_) {
                offlineTextQueue.unshift(queued);
                break;
            }
        }
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'history') {
                const messagesDiv = document.getElementById('messages');
                const wasAtBottom = messagesDiv ? (messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight < 50) : true;

                if (data.mode === 'delta') {
                    if (Array.isArray(data.messages)) {
                        data.messages.forEach(msg => {
                            if (msg.id && renderedIds.has(msg.id)) return;
                            appendMessage(msg);
                            if (msg.id) {
                                renderedIds.add(msg.id);
                                lastSeenId = Math.max(lastSeenId || 0, msg.id);
                            }
                        });
                    }
                    if (data.reactions && typeof data.reactions === 'object') {
                        for (const [midStr, rxMap] of Object.entries(data.reactions)) {
                            updateMessageReactions(Number(midStr), rxMap);
                        }
                    }
                    if (wasAtBottom) scrollToBottom();
                } else {
                    // mode === 'full' (first load or DB reset)
                    if (messagesDiv) messagesDiv.replaceChildren();
                    renderedIds.clear();
                    if (Array.isArray(data.messages)) {
                        data.messages.forEach(msg => {
                            appendMessage(msg);
                            if (msg.id) {
                                renderedIds.add(msg.id);
                                lastSeenId = Math.max(lastSeenId || 0, msg.id);
                            }
                        });
                    }
                    scrollToBottom();
                }
            } else if (data.type === 'message') {
                if (data.id && renderedIds.has(data.id)) return;
                if (data.id) {
                    renderedIds.add(data.id);
                    lastSeenId = Math.max(lastSeenId || 0, data.id);
                }
                appendMessage(data);
                scrollToBottom();
                handleIncomingNotification(data);
                removeTyper(data.username);
            } else if (data.type === 'typing') {
                handleRemoteTyping(data.username);
            } else if (data.type === 'reaction') {
                applySingleReaction(data.id, data.emoji, data.username, data.active);
            } else if (data.type === 'presence') {
                updateUserList(data.users || []);
            }
        } catch (e) {
            console.error('Failed to parse WebSocket message', e);
        }
    };

    ws.onclose = (event) => {
        if (event && event.reason === 'Room is full') {
            const pill = document.getElementById('connection-pill');
            if (pill) {
                pill.textContent = 'Room is full';
                pill.style.display = 'block';
            }
            showToast('Room is full (max 50 users)');
            return;
        }

        if (username && document.getElementById('app').style.display === 'flex') {
            const pill = document.getElementById('connection-pill');
            if (pill) {
                pill.textContent = 'Reconnecting…';
                pill.style.display = 'block';
            }

            // First retry waits random 0-3 s (jitter) so phones don't reconnect all at once
            let delay;
            if (reconnectAttempts === 0) {
                delay = Math.random() * 3000;
            } else {
                delay = Math.min(10000, Math.pow(2, reconnectAttempts - 1) * 1000 + Math.random() * 1000);
            }
            reconnectAttempts++;
            reconnectTimer = setTimeout(() => {
                if (!ws || ws.readyState === WebSocket.CLOSED) {
                    connectWebSocket();
                }
            }, delay);
        }
    };
}

// Immediate reconnection on network recovery
window.addEventListener('online', () => {
    if (username && (!ws || ws.readyState === WebSocket.CLOSED)) {
        connectWebSocket();
    }
});
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && username && (!ws || ws.readyState === WebSocket.CLOSED)) {
        connectWebSocket();
    }
});

// Helper: Format file sizes
function formatSize(bytes) {
    if (bytes === undefined || bytes === null || isNaN(bytes)) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Helper: Derive clean display name if missing
function deriveDisplayName(storedName) {
    if (!storedName) return 'file';
    const compound = ['.tar.gz', '.tar.bz2', '.tar.xz'];
    const lower = storedName.toLowerCase();
    for (const ce of compound) {
        if (lower.endsWith(ce)) {
            const base = storedName.slice(0, -ce.length).replace(/_[0-9a-f]{8}$/i, '');
            return base + ce;
        }
    }
    const lastDot = storedName.lastIndexOf('.');
    if (lastDot > 0) {
        const ext = storedName.slice(lastDot);
        const stem = storedName.slice(0, lastDot).replace(/_[0-9a-f]{8}$/i, '');
        return stem + ext;
    }
    return storedName.replace(/_[0-9a-f]{8}$/i, '');
}

// Helper: File extension detection
function getExtension(filename) {
    if (!filename) return '';
    const lower = filename.toLowerCase();
    if (lower.endsWith('.tar.gz')) return '.tar.gz';
    if (lower.endsWith('.tar.bz2')) return '.tar.bz2';
    if (lower.endsWith('.tar.xz')) return '.tar.xz';
    const idx = lower.lastIndexOf('.');
    return idx >= 0 ? lower.slice(idx) : '';
}

function isImageExtension(ext) {
    return ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.bmp'].includes(ext);
}

function isCompressibleImage(ext, mimeType) {
    const lowerExt = (ext || '').toLowerCase();
    const lowerMime = (mimeType || '').toLowerCase();
    if (lowerExt === '.gif' || lowerExt === '.svg') return false;
    if (lowerMime === 'image/gif' || lowerMime === 'image/svg+xml') return false;
    return ['.jpg', '.jpeg', '.png', '.webp', '.bmp'].includes(lowerExt) ||
           ['image/jpeg', 'image/png', 'image/webp', 'image/bmp'].includes(lowerMime);
}

// Category Icon SVGs
function getFileIconSvg(ext) {
    switch (ext) {
        case '.pdf':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>`;
        case '.doc':
        case '.docx':
        case '.odt':
        case '.rtf':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`;
        case '.xls':
        case '.xlsx':
        case '.csv':
        case '.ods':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/></svg>`;
        case '.ppt':
        case '.pptx':
        case '.odp':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h20v14H2z"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>`;
        case '.zip':
        case '.tar':
        case '.gz':
        case '.tar.gz':
        case '.tar.bz2':
        case '.tar.xz':
        case '.7z':
        case '.rar':
        case '.bz2':
        case '.xz':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v3"/><path d="M21 16v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3"/><rect x="2" y="8" width="20" height="8" rx="1"/><line x1="10" y1="12" x2="14" y2="12"/></svg>`;
        case '.py':
        case '.js':
        case '.ts':
        case '.html':
        case '.css':
        case '.c':
        case '.cpp':
        case '.h':
        case '.java':
        case '.json':
        case '.sql':
        case '.sh':
        case '.rs':
        case '.go':
        case '.php':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`;
        case '.txt':
        case '.log':
        case '.md':
        case '.ini':
        case '.yaml':
        case '.yml':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`;
        case '.mp3':
        case '.wav':
        case '.ogg':
        case '.m4a':
        case '.flac':
        case '.aac':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`;
        case '.mp4':
        case '.mkv':
        case '.webm':
        case '.avi':
        case '.mov':
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2.18"/><line x1="7" y1="4" x2="7" y2="20"/><line x1="17" y1="4" x2="17" y2="20"/><line x1="2" y1="12" x2="22" y2="12"/></svg>`;
        default:
            return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
    }
}

// Toast notification
function showToast(message) {
    let toast = document.getElementById('toast-msg');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast-msg';
        toast.className = 'glass-panel toast-message';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toast._timeout);
    toast._timeout = setTimeout(() => {
        toast.classList.remove('show');
    }, 4000);
}

// Create safe DOM File Card
function createFileCard(storedName, originalName, size, ext) {
    const card = document.createElement('div');
    card.className = 'file-card';

    // Left: Icon tile
    const iconTile = document.createElement('div');
    iconTile.className = 'file-icon-tile';
    iconTile.innerHTML = getFileIconSvg(ext);

    // Middle: Info
    const infoDiv = document.createElement('div');
    infoDiv.className = 'file-info';

    const nameDiv = document.createElement('div');
    nameDiv.className = 'file-name';
    nameDiv.textContent = originalName;
    nameDiv.title = originalName;

    const metaDiv = document.createElement('div');
    metaDiv.className = 'file-meta';
    const extLabel = ext ? ext.toUpperCase().replace('.', '') : 'FILE';
    const sizeLabel = size ? formatSize(size) : '';
    metaDiv.textContent = sizeLabel ? `${sizeLabel} · ${extLabel}` : extLabel;

    infoDiv.appendChild(nameDiv);
    infoDiv.appendChild(metaDiv);

    // Right: Actions
    const actionsDiv = document.createElement('div');
    actionsDiv.className = 'file-actions';

    if (ext === '.pdf') {
        const openLink = document.createElement('a');
        openLink.className = 'file-action-btn file-open-btn';
        openLink.href = `/uploads/${encodeURIComponent(storedName)}`;
        openLink.target = '_blank';
        openLink.rel = 'noopener';
        openLink.textContent = 'Open';
        actionsDiv.appendChild(openLink);
    }

    const downloadLink = document.createElement('a');
    downloadLink.className = 'file-action-btn file-download-btn';
    downloadLink.href = `/uploads/${encodeURIComponent(storedName)}?download=1`;
    downloadLink.setAttribute('download', originalName);
    downloadLink.textContent = 'Download';
    actionsDiv.appendChild(downloadLink);

    card.appendChild(iconTile);
    card.appendChild(infoDiv);
    card.appendChild(actionsDiv);
    return card;
}

// Append message using safe DOM creation (strictly no innerHTML with user data)
function appendMessage(data) {
    const messagesDiv = document.getElementById('messages');
    if (!messagesDiv) return;

    const msgDiv = document.createElement('div');
    const isSelf = data.username === username;
    msgDiv.className = `message-wrapper ${isSelf ? 'sent' : 'received'}`;
    if (data.id) {
        msgDiv.dataset.msgId = data.id;
    }
    msgDiv._reactions = data.reactions || {};

    if (!isSelf && data.username) {
        const senderDiv = document.createElement('div');
        senderDiv.className = 'message-sender';
        senderDiv.textContent = data.username;
        msgDiv.appendChild(senderDiv);
    }

    const bubbleDiv = document.createElement('div');
    bubbleDiv.className = 'message-bubble';

    const contentDiv = document.createElement('div');
    contentDiv.className = 'msg-content';

    const msgType = data.msg_type || data.type;
    let copyBtn = null;
    let reactBtn = null;

    if (msgType === 'text') {
        const textSpan = document.createElement('span');
        textSpan.textContent = data.content || '';
        contentDiv.appendChild(textSpan);

        copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'copy-btn';
        copyBtn.title = 'Copy text';
        copyBtn.setAttribute('aria-label', 'Copy text');
        copyBtn.textContent = '📋';
        copyBtn.onclick = function(e) { e.stopPropagation(); copyText(copyBtn); };
    } else if (msgType === 'file') {
        const storedName = data.content || '';
        const originalName = data.original_name || deriveDisplayName(storedName);
        const size = data.size;
        const ext = getExtension(originalName || storedName);

        if (isImageExtension(ext)) {
            const isLargeImage = (typeof size === 'number' && size > 400 * 1024);

            if (isLargeImage) {
                const placeholder = document.createElement('div');
                placeholder.className = 'image-placeholder glass-panel';
                placeholder.title = originalName || 'Image';

                const nameEl = document.createElement('div');
                nameEl.className = 'image-placeholder-name';
                nameEl.textContent = originalName || 'Image';

                const metaEl = document.createElement('div');
                metaEl.className = 'image-placeholder-meta';
                metaEl.textContent = formatSize(size);

                const actionEl = document.createElement('div');
                actionEl.className = 'image-placeholder-action';
                actionEl.textContent = 'Tap to load';

                placeholder.appendChild(nameEl);
                placeholder.appendChild(metaEl);
                placeholder.appendChild(actionEl);

                placeholder.onclick = function(e) {
                    e.stopPropagation();
                    const link = document.createElement('a');
                    link.href = `/uploads/${encodeURIComponent(storedName)}`;
                    link.target = '_blank';
                    link.rel = 'noopener';

                    const img = document.createElement('img');
                    img.className = 'message-img';
                    img.src = `/uploads/${encodeURIComponent(storedName)}`;
                    img.alt = originalName || 'Attachment';
                    img.loading = 'lazy';
                    img.decoding = 'async';
                    img.onload = () => scrollToBottom();

                    link.appendChild(img);
                    placeholder.replaceWith(link);

                    if (!copyBtn) {
                        copyBtn = document.createElement('button');
                        copyBtn.type = 'button';
                        copyBtn.className = 'copy-btn';
                        copyBtn.title = 'Copy image';
                        copyBtn.setAttribute('aria-label', 'Copy image');
                        copyBtn.textContent = '📋';
                        copyBtn.onclick = function(ev) { ev.stopPropagation(); copyImage(copyBtn, `/uploads/${encodeURIComponent(storedName)}`); };
                        bubbleDiv.appendChild(copyBtn);
                    }
                };

                contentDiv.appendChild(placeholder);
            } else {
                const link = document.createElement('a');
                link.href = `/uploads/${encodeURIComponent(storedName)}`;
                link.target = '_blank';
                link.rel = 'noopener';

                const img = document.createElement('img');
                img.className = 'message-img';
                img.src = `/uploads/${encodeURIComponent(storedName)}`;
                img.alt = originalName || 'Attachment';
                img.loading = 'lazy';
                img.decoding = 'async';
                img.onload = () => scrollToBottom();

                link.appendChild(img);
                contentDiv.appendChild(link);

                copyBtn = document.createElement('button');
                copyBtn.type = 'button';
                copyBtn.className = 'copy-btn';
                copyBtn.title = 'Copy image';
                copyBtn.setAttribute('aria-label', 'Copy image');
                copyBtn.textContent = '📋';
                copyBtn.onclick = function(e) { e.stopPropagation(); copyImage(copyBtn, `/uploads/${encodeURIComponent(storedName)}`); };
            }
        } else {
            // Render structured file card
            const card = createFileCard(storedName, originalName, size, ext);
            contentDiv.appendChild(card);
        }
    }

    // React button for desktop hover/focus (sits beside bubble, keyboard reachable)
    if (data.id) {
        reactBtn = document.createElement('button');
        reactBtn.type = 'button';
        reactBtn.className = 'react-btn';
        reactBtn.title = 'React with emoji';
        reactBtn.setAttribute('aria-label', 'React with emoji');
        reactBtn.textContent = '😊';
        reactBtn.onclick = function(e) {
            e.stopPropagation();
            openReactionPalette(data.id, bubbleDiv);
        };
    }

    bubbleDiv.appendChild(contentDiv);

    // Reaction chips container
    const chipsDiv = document.createElement('div');
    chipsDiv.className = 'reaction-chips';
    bubbleDiv.appendChild(chipsDiv);

    const timeSpan = document.createElement('span');
    timeSpan.className = 'msg-time';
    const now = new Date();
    timeSpan.textContent = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
    bubbleDiv.appendChild(timeSpan);

    if (copyBtn) {
        bubbleDiv.appendChild(copyBtn);
    }
    if (reactBtn) {
        bubbleDiv.appendChild(reactBtn);
    }

    msgDiv.appendChild(bubbleDiv);
    messagesDiv.appendChild(msgDiv);

    if (data.id && data.reactions) {
        updateMessageReactions(data.id, data.reactions);
    }

    // Keep up to 200 most recent messages in DOM
    while (messagesDiv.children.length > 200) {
        messagesDiv.removeChild(messagesDiv.firstChild);
    }
}

function scrollToBottom() {
    const messagesDiv = document.getElementById('messages');
    if (messagesDiv) {
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }
}

function updateUserList(users) {
    const ul = document.getElementById('user-list');
    if (!ul) return;
    ul.replaceChildren();
    users.forEach(user => {
        const li = document.createElement('li');
        li.textContent = user;
        ul.appendChild(li);
    });
}

function sendMessage() {
    const input = document.getElementById('message-input');
    const content = input.value.trim();
    if (!content) return;

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'message', content: content }));
        input.value = '';
    } else {
        // Queue text messages typed while offline (max 20)
        if (offlineTextQueue.length < 20) {
            offlineTextQueue.push(content);
            showToast('Offline. Message queued and will send on reconnect.');
        } else {
            showToast('Offline queue is full (max 20).');
        }
        input.value = '';
    }
}

// --- TYPING INDICATOR LOGIC ---
function sendTypingEvent() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const now = Date.now();
    if (now - lastTypingSent < 2500) return; // at most once every 2.5s
    lastTypingSent = now;
    try {
        ws.send(JSON.stringify({ type: 'typing' }));
    } catch (_) {}
}

function handleRemoteTyping(typerUsername) {
    if (!typerUsername || typerUsername === username) return;
    typers.set(typerUsername, Date.now() + 3500);
    renderTypingIndicator();
    if (!typingExpiryTimer) {
        typingExpiryTimer = setInterval(checkTypingExpiries, 500);
    }
}

function removeTyper(typerUsername) {
    if (typers.has(typerUsername)) {
        typers.delete(typerUsername);
        renderTypingIndicator();
    }
}

function checkTypingExpiries() {
    const now = Date.now();
    let changed = false;
    for (const [u, exp] of typers.entries()) {
        if (now >= exp) {
            typers.delete(u);
            changed = true;
        }
    }
    if (changed) renderTypingIndicator();
    if (typers.size === 0 && typingExpiryTimer) {
        clearInterval(typingExpiryTimer);
        typingExpiryTimer = null;
    }
}

function renderTypingIndicator() {
    const el = document.getElementById('typing-indicator');
    const textEl = document.getElementById('typing-text');
    if (!el || !textEl) return;

    const names = Array.from(typers.keys());
    if (names.length === 0) {
        el.style.display = 'none';
        textEl.textContent = '';
        return;
    }

    let msg = '';
    if (names.length === 1) {
        msg = `${names[0]} is typing`;
    } else if (names.length === 2) {
        msg = `${names[0]} and ${names[1]} are typing`;
    } else {
        msg = `${names.length} people are typing`;
    }

    textEl.textContent = msg;
    el.style.display = 'flex';
}

// --- REACTIONS LOGIC ---
function openReactionPalette(msgId, bubbleEl) {
    activePaletteMsgId = msgId;
    const palette = document.getElementById('reaction-palette');
    if (!palette) return;

    palette.style.display = 'flex';

    // Position from bubble bounding box, clamped to viewport
    const rect = bubbleEl.getBoundingClientRect();
    const palWidth = 280;
    const palHeight = 52;

    let top = rect.top - palHeight - 8;
    if (top < 10) {
        top = rect.bottom + 8;
    }
    let left = rect.left;
    if (left + palWidth > window.innerWidth - 12) {
        left = window.innerWidth - palWidth - 12;
    }
    if (left < 12) left = 12;

    palette.style.top = `${Math.round(top)}px`;
    palette.style.left = `${Math.round(left)}px`;
}

function closeReactionPalette() {
    const palette = document.getElementById('reaction-palette');
    if (palette) palette.style.display = 'none';
    activePaletteMsgId = null;
}

function toggleReaction(msgId, emoji) {
    if (!msgId || !emoji) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
        try {
            ws.send(JSON.stringify({ type: 'react', id: msgId, emoji: emoji }));
        } catch (_) {}
    }
}

function updateMessageReactions(msgId, reactionsMap) {
    const msgWrapper = document.querySelector(`.message-wrapper[data-msg-id="${msgId}"]`);
    if (!msgWrapper) return;

    msgWrapper._reactions = reactionsMap || {};
    const chipsContainer = msgWrapper.querySelector('.reaction-chips');
    if (!chipsContainer) return;

    chipsContainer.replaceChildren();

    const rx = msgWrapper._reactions;
    for (const [emoji, users] of Object.entries(rx)) {
        if (!Array.isArray(users) || users.length === 0) continue;
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'reaction-chip';
        chip.dataset.emoji = emoji;
        if (username && users.includes(username)) {
            chip.classList.add('reacted');
        }
        chip.setAttribute('aria-label', `${users.length} reacted with ${emoji}`);

        const emojiSpan = document.createElement('span');
        emojiSpan.textContent = emoji;

        const countSpan = document.createElement('span');
        countSpan.textContent = users.length;

        chip.appendChild(emojiSpan);
        chip.appendChild(countSpan);
        chipsContainer.appendChild(chip);
    }
}

function applySingleReaction(msgId, emoji, reactorUsername, active) {
    const msgWrapper = document.querySelector(`.message-wrapper[data-msg-id="${msgId}"]`);
    if (!msgWrapper) return;

    const rx = msgWrapper._reactions || {};
    let users = rx[emoji] ? [...rx[emoji]] : [];
    if (active) {
        if (!users.includes(reactorUsername)) users.push(reactorUsername);
        rx[emoji] = users;
    } else {
        users = users.filter(u => u !== reactorUsername);
        if (users.length > 0) {
            rx[emoji] = users;
        } else {
            delete rx[emoji];
        }
    }
    updateMessageReactions(msgId, rx);
}

// Global Reaction Palette & Messages event delegation
document.querySelectorAll('.react-emoji-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const emo = btn.dataset.emoji;
        if (activePaletteMsgId && emo) {
            toggleReaction(activePaletteMsgId, emo);
        }
        closeReactionPalette();
    });
});

document.addEventListener('click', (e) => {
    const palette = document.getElementById('reaction-palette');
    if (palette && palette.style.display !== 'none') {
        if (!palette.contains(e.target) && !e.target.closest('.react-btn')) {
            closeReactionPalette();
        }
    }
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeReactionPalette();
    }
});

// Single delegated listener on #messages for reaction interactions
const messagesContainer = document.getElementById('messages');
if (messagesContainer) {
    messagesContainer.addEventListener('click', (e) => {
        const chip = e.target.closest('.reaction-chip');
        if (chip) {
            e.stopPropagation();
            const msgWrapper = chip.closest('.message-wrapper');
            if (msgWrapper && msgWrapper.dataset.msgId) {
                toggleReaction(Number(msgWrapper.dataset.msgId), chip.dataset.emoji);
            }
            return;
        }

        const reactBtn = e.target.closest('.react-btn');
        if (reactBtn) {
            e.stopPropagation();
            const msgWrapper = reactBtn.closest('.message-wrapper');
            const bubble = msgWrapper ? msgWrapper.querySelector('.message-bubble') : null;
            if (msgWrapper && msgWrapper.dataset.msgId && bubble) {
                openReactionPalette(Number(msgWrapper.dataset.msgId), bubble);
            }
            return;
        }

        // Tap on text or file bubble opens palette on mobile
        const bubble = e.target.closest('.message-bubble');
        if (bubble && !e.target.closest('a') && !e.target.closest('button') && !e.target.closest('input')) {
            if (!bubble.querySelector('.message-img')) {
                const msgWrapper = bubble.closest('.message-wrapper');
                if (msgWrapper && msgWrapper.dataset.msgId) {
                    openReactionPalette(Number(msgWrapper.dataset.msgId), bubble);
                }
            }
        }
    });

    // Touch long-press on images (~500ms) opens reaction palette without breaking native tap
    let touchTimer = null;
    messagesContainer.addEventListener('touchstart', (e) => {
        const img = e.target.closest('.message-img');
        if (img) {
            const bubble = img.closest('.message-bubble');
            const msgWrapper = bubble ? bubble.closest('.message-wrapper') : null;
            if (msgWrapper && msgWrapper.dataset.msgId) {
                touchTimer = setTimeout(() => {
                    touchTimer = null;
                    openReactionPalette(Number(msgWrapper.dataset.msgId), bubble);
                }, 500);
            }
        }
    }, { passive: true });

    const cancelTouch = () => {
        if (touchTimer) {
            clearTimeout(touchTimer);
            touchTimer = null;
        }
    };
    messagesContainer.addEventListener('touchend', cancelTouch, { passive: true });
    messagesContainer.addEventListener('touchmove', cancelTouch, { passive: true });
    messagesContainer.addEventListener('touchcancel', cancelTouch, { passive: true });
}

// --- HD TOGGLE LOGIC ---
function toggleHd() {
    hdActive = !hdActive;
    updateHdButton();
}

function updateHdButton() {
    const btn = document.getElementById('hd-toggle');
    if (btn) {
        btn.setAttribute('aria-pressed', String(hdActive));
    }
}

// --- IMAGE COMPRESSION LOGIC ---
async function compressImage(file) {
    let bitmap = null;
    let width = 0;
    let height = 0;

    try {
        if ('createImageBitmap' in window) {
            bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
            width = bitmap.width;
            height = bitmap.height;
        }
    } catch (_) {
        bitmap = null;
    }

    if (!bitmap) {
        try {
            const url = URL.createObjectURL(file);
            const img = new Image();
            await new Promise((resolve, reject) => {
                img.onload = resolve;
                img.onerror = reject;
                img.src = url;
            });
            URL.revokeObjectURL(url);
            width = img.naturalWidth;
            height = img.naturalHeight;
        } catch (_) {
            return file; // Can't decode, upload unchanged
        }
    }

    const maxDim = Math.max(width, height);
    if (file.size <= 300 * 1024 && maxDim <= 1600) {
        if (bitmap && bitmap.close) bitmap.close();
        return file;
    }

    let targetW = width;
    let targetH = height;
    if (maxDim > 1600) {
        const scale = 1600 / maxDim;
        targetW = Math.round(width * scale);
        targetH = Math.round(height * scale);
    }

    if (bitmap && bitmap.close) {
        bitmap.close();
        bitmap = null;
    }

    let source = null;
    try {
        if ('createImageBitmap' in window) {
            source = await createImageBitmap(file, {
                imageOrientation: 'from-image',
                resizeWidth: targetW,
                resizeHeight: targetH,
                resizeQuality: 'high'
            });
        }
    } catch (_) {
        source = null;
    }

    const canvas = document.createElement('canvas');
    canvas.width = targetW;
    canvas.height = targetH;
    const ctx = canvas.getContext('2d');

    if (source) {
        ctx.drawImage(source, 0, 0, targetW, targetH);
        if (source.close) source.close();
    } else {
        const url = URL.createObjectURL(file);
        const img = new Image();
        await new Promise((resolve, reject) => {
            img.onload = resolve;
            img.onerror = reject;
            img.src = url;
        });
        ctx.drawImage(img, 0, 0, targetW, targetH);
        URL.revokeObjectURL(url);
    }

    let blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', 0.82));
    let ext = '.webp';
    let mime = 'image/webp';

    if (!blob || blob.type !== 'image/webp') {
        blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.82));
        ext = '.jpg';
        mime = 'image/jpeg';
    }

    if (!blob || blob.size >= file.size) {
        return file;
    }

    const dotIdx = file.name.lastIndexOf('.');
    const baseName = dotIdx > 0 ? file.name.slice(0, dotIdx) : file.name;
    const newName = `${baseName}${ext}`;

    return new File([blob], newName, { type: mime, lastModified: Date.now() });
}

// --- UPLOAD QUEUE & PROGRESS TRAY ---
const uploadQueue = [];
let activeUploadItem = null;

function renderUploadTray() {
    const tray = document.getElementById('upload-tray');
    if (!tray) return;

    if (!uploadQueue.length) {
        tray.style.display = 'none';
        tray.replaceChildren();
        return;
    }

    tray.style.display = 'flex';
    tray.replaceChildren();

    uploadQueue.forEach(item => {
        const row = document.createElement('div');
        row.className = `upload-tray-row ${item.status}`;

        const details = document.createElement('div');
        details.className = 'upload-row-details';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'upload-row-name';
        nameSpan.textContent = item.file.name;
        nameSpan.title = item.file.name;

        const statusSpan = document.createElement('span');
        statusSpan.className = 'upload-row-status';

        if (item.status === 'compressing') {
            statusSpan.textContent = 'Compressing…';
        } else if (item.status === 'uploading') {
            statusSpan.textContent = item.displayInfo ? `${item.displayInfo} (${item.progress}%)` : `${item.progress}%`;
        } else if (item.status === 'queued') {
            statusSpan.textContent = item.displayInfo || 'Queued';
        } else if (item.status === 'done') {
            statusSpan.textContent = 'Completed';
        } else if (item.status === 'error') {
            statusSpan.textContent = item.error || 'Failed';
        } else if (item.status === 'canceled') {
            statusSpan.textContent = 'Canceled';
        }

        details.appendChild(nameSpan);
        details.appendChild(statusSpan);
        row.appendChild(details);

        // Progress bar
        const bar = document.createElement('div');
        bar.className = 'upload-progress-bar';
        const fill = document.createElement('div');
        fill.className = 'upload-progress-fill';
        fill.style.width = `${item.progress}%`;
        bar.appendChild(fill);
        row.appendChild(bar);

        // Cancel / Dismiss button
        if (item.status === 'uploading' || item.status === 'queued' || item.status === 'compressing' || item.status === 'retrying') {
            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'upload-cancel-btn';
            cancelBtn.setAttribute('aria-label', 'Cancel upload');
            cancelBtn.textContent = '✕';
            cancelBtn.onclick = function() { cancelUpload(item); };
            row.appendChild(cancelBtn);
        }

        tray.appendChild(row);
    });
}

function cancelUpload(item) {
    item.canceled = true;
    if (item.retryTimer) {
        clearTimeout(item.retryTimer);
        item.retryTimer = null;
    }
    if (item.status === 'uploading' && item.xhr) {
        item.xhr.abort();
    } else if (item.status === 'queued' || item.status === 'compressing' || item.status === 'retrying') {
        item.status = 'canceled';
        if (activeUploadItem === item) activeUploadItem = null;
        renderUploadTray();
        processUploadQueue();
    }
}

function queueFiles(fileList) {
    if (!fileList || !fileList.length) return;

    const sendOriginal = hdActive;
    if (hdActive) {
        hdActive = false;
        updateHdButton();
    }

    for (let i = 0; i < fileList.length; i++) {
        const file = fileList[i];
        if (file.size > 100 * 1024 * 1024) {
            showToast(`${file.name} is over 100 MB. Please choose a smaller file.`);
            continue;
        }
        if (file.size === 0) {
            showToast(`${file.name} is empty (0 bytes).`);
            continue;
        }

        const ext = getExtension(file.name);
        const willCompress = !sendOriginal && isCompressibleImage(ext, file.type);

        if (!willCompress && file.size > maxUploadBytes) {
            const limitMb = Math.round(maxUploadBytes / (1024 * 1024));
            showToast(`${file.name} is ${formatSize(file.size)} — the limit is ${limitMb} MB.`);
            continue;
        }

        uploadQueue.push({
            id: 'up_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
            file: file,
            status: willCompress ? 'compressing' : 'queued',
            displayInfo: sendOriginal && isImageExtension(ext) ? 'Original' : '',
            progress: 0,
            xhr: null,
            error: null,
            canceled: false
        });
    }

    renderUploadTray();
    processUploadQueue();
}

function processUploadQueue() {
    if (activeUploadItem) return;

    const next = uploadQueue.find(item => item.status === 'queued' || item.status === 'compressing');
    if (!next) {
        const hasActive = uploadQueue.some(item => item.status === 'queued' || item.status === 'uploading' || item.status === 'compressing');
        if (!hasActive && uploadQueue.length > 0) {
            setTimeout(() => {
                for (let i = uploadQueue.length - 1; i >= 0; i--) {
                    if (uploadQueue[i].status === 'done' || uploadQueue[i].status === 'canceled') {
                        uploadQueue.splice(i, 1);
                    }
                }
                renderUploadTray();
            }, 1200);
        }
        return;
    }

    activeUploadItem = next;

    if (next.status === 'compressing') {
        renderUploadTray();
        (async () => {
            try {
                const origSize = next.file.size;
                const compressed = await compressImage(next.file);
                if (next.canceled) {
                    next.status = 'canceled';
                    activeUploadItem = null;
                    renderUploadTray();
                    processUploadQueue();
                    return;
                }
                if (compressed.size > maxUploadBytes) {
                    next.status = 'error';
                    next.error = 'Too large (max 20 MB)';
                    showToast(`${next.file.name} exceeds 20 MB.`);
                    activeUploadItem = null;
                    renderUploadTray();
                    processUploadQueue();
                    return;
                }
                next.file = compressed;
                next.status = 'uploading';
                if (compressed.size < origSize) {
                    next.displayInfo = `${formatSize(origSize)} → ${formatSize(compressed.size)}`;
                } else {
                    next.displayInfo = 'Original';
                }
                renderUploadTray();
                startUploadXHR(next);
            } catch (e) {
                if (next.canceled) {
                    next.status = 'canceled';
                    activeUploadItem = null;
                    renderUploadTray();
                    processUploadQueue();
                } else {
                    next.status = 'uploading';
                    renderUploadTray();
                    startUploadXHR(next);
                }
            }
        })();
        return;
    }

    next.status = 'uploading';
    renderUploadTray();
    startUploadXHR(next);
}

function startUploadXHR(item) {
    const formData = new FormData();
    formData.append('file', item.file);
    formData.append('username', username || 'Anonymous');

    const xhr = new XMLHttpRequest();
    item.xhr = xhr;

    xhr.upload.onprogress = function(e) {
        if (e.lengthComputable) {
            item.progress = Math.round((e.loaded / e.total) * 100);
            renderUploadTray();
        }
    };

    xhr.onload = function() {
        if (xhr.status >= 200 && xhr.status < 300) {
            item.status = 'done';
            item.progress = 100;
            activeUploadItem = null;
            renderUploadTray();
            processUploadQueue();
            return;
        }

        // 503 Server busy retry: max 3 attempts after random 2-5 s
        if (xhr.status === 503 && (item.retries || 0) < 3 && !item.canceled) {
            item.retries = (item.retries || 0) + 1;
            item.status = 'retrying';
            item.displayInfo = `Server busy, retrying (${item.retries}/3)...`;
            renderUploadTray();
            const retryDelay = Math.random() * 3000 + 2000; // 2000ms - 5000ms (2-5 s)
            item.retryTimer = setTimeout(() => {
                if (!item.canceled) {
                    item.status = 'uploading';
                    renderUploadTray();
                    startUploadXHR(item);
                }
            }, retryDelay);
            return;
        }

        item.status = 'error';
        let errMsg = 'Upload failed';
        try {
            const res = JSON.parse(xhr.responseText);
            if (res && res.detail) errMsg = res.detail;
        } catch (_) {}
        item.error = errMsg;
        showToast(`${item.file.name}: ${errMsg}`);
        activeUploadItem = null;
        renderUploadTray();
        processUploadQueue();
    };

    xhr.onerror = function() {
        item.status = 'error';
        item.error = 'Network error';
        showToast(`${item.file.name}: Network error`);
        activeUploadItem = null;
        renderUploadTray();
        processUploadQueue();
    };

    xhr.onabort = function() {
        item.status = 'canceled';
        activeUploadItem = null;
        renderUploadTray();
        processUploadQueue();
    };

    xhr.open('POST', '/upload', true);
    xhr.send(formData);
}

// --- EVENT LISTENERS ---
document.getElementById('message-input').addEventListener('keypress', function (e) {
    if (e.key === 'Enter') sendMessage();
});

document.getElementById('message-input').addEventListener('input', function(e) {
    if (e.target.value.trim().length > 0) {
        sendTypingEvent();
    }
});

document.getElementById('file-input').addEventListener('change', function(e) {
    if (e.target.files && e.target.files.length) {
        queueFiles(e.target.files);
        e.target.value = ''; // Reset input to allow re-selecting the same file
    }
});

// Drag & Drop
const dropOverlay = document.getElementById('drop-overlay');
let dragCounter = 0;

window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    if (dropOverlay) dropOverlay.style.display = 'flex';
});

window.addEventListener('dragover', (e) => {
    e.preventDefault();
});

window.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
        dragCounter = 0;
        if (dropOverlay) dropOverlay.style.display = 'none';
    }
});

window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    if (dropOverlay) dropOverlay.style.display = 'none';
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        queueFiles(e.dataTransfer.files);
    }
});

// Paste support
document.addEventListener('paste', (e) => {
    if (e.clipboardData && e.clipboardData.files && e.clipboardData.files.length) {
        queueFiles(e.clipboardData.files);
        e.preventDefault();
    }
});

// --- UI & SETTINGS LOGIC ---
function openSettings() {
    updateSettingsModalControls();
    document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettings() {
    document.getElementById('settings-modal').style.display = 'none';
}

function applySettings() {
    const theme = document.getElementById('theme-select').value;
    const font = document.getElementById('font-select').value;

    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-font', font);

    localStorage.setItem('theme', theme);
    localStorage.setItem('font', font);

    updateThemeButton();
}

function toggleTheme() {
    const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
    const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
    document.getElementById('theme-select').value = newTheme;
    applySettings();
}

function updateThemeButton() {
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) {
        const currentTheme = document.documentElement.getAttribute('data-theme');
        btn.textContent = currentTheme === 'dark' ? '☀️' : '🌙';
    }
}

function toggleSoundSetting(checked) {
    soundEnabled = checked;
    try {
        localStorage.setItem('sound_enabled', String(checked));
    } catch (_) {}
    if (checked) {
        initAudioContext();
    }
}

async function toggleDesktopAlerts(checked) {
    const toggle = document.getElementById('desktop-alerts-toggle');
    if (!isNotificationSupported()) {
        if (toggle) {
            toggle.checked = false;
            toggle.disabled = true;
        }
        desktopAlertsEnabled = false;
        try {
            localStorage.setItem('desktop_alerts_enabled', 'false');
        } catch (_) {}
        return;
    }

    if (checked) {
        try {
            let permission = Notification.permission;
            if (permission === 'default') {
                permission = await Notification.requestPermission();
            }
            if (permission === 'granted') {
                desktopAlertsEnabled = true;
                try {
                    localStorage.setItem('desktop_alerts_enabled', 'true');
                } catch (_) {}
                if (toggle) toggle.checked = true;
            } else {
                desktopAlertsEnabled = false;
                try {
                    localStorage.setItem('desktop_alerts_enabled', 'false');
                } catch (_) {}
                if (toggle) toggle.checked = false;
                if (permission === 'denied') {
                    showToast('Notifications are blocked in your browser settings.');
                }
            }
        } catch (e) {
            console.error('Failed to request notification permission', e);
            desktopAlertsEnabled = false;
            if (toggle) toggle.checked = false;
        }
    } else {
        desktopAlertsEnabled = false;
        try {
            localStorage.setItem('desktop_alerts_enabled', 'false');
        } catch (_) {}
        if (toggle) toggle.checked = false;
    }
}

function updateSettingsModalControls() {
    const soundToggle = document.getElementById('sound-toggle');
    if (soundToggle) {
        soundToggle.checked = soundEnabled;
    }

    const desktopToggle = document.getElementById('desktop-alerts-toggle');
    const note = document.getElementById('desktop-alerts-note');
    if (!isNotificationSupported()) {
        if (desktopToggle) {
            desktopToggle.checked = false;
            desktopToggle.disabled = true;
        }
        if (note) {
            note.style.display = 'block';
        }
    } else {
        if (desktopToggle) {
            desktopToggle.disabled = false;
            desktopToggle.checked = desktopAlertsEnabled && Notification.permission === 'granted';
        }
        if (note) {
            note.style.display = 'none';
        }
    }
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.toggle('open');
    overlay.classList.toggle('show');
}

function toggleFullScreen() {
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(() => {});
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        }
    }
}

// --- COPY LOGIC ---
function copyText(btn) {
    const contentDiv = btn.parentElement.querySelector('.msg-content');
    const text = contentDiv.innerText || contentDiv.textContent;

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => showCopied(btn)).catch(() => fallbackCopyText(text, btn));
    } else {
        fallbackCopyText(text, btn);
    }
}

function fallbackCopyText(text, btn) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        showCopied(btn);
    } catch (err) {
        console.error('Fallback copy failed', err);
    }
    document.body.removeChild(ta);
}

async function copyImage(btn, imageUrl) {
    if (!navigator.clipboard || !navigator.clipboard.write) {
        alert("Direct image copying is blocked by your browser on local HTTP networks. Please long-press or right-click the image to copy it manually.");
        return;
    }
    try {
        const response = await fetch(imageUrl);
        const blob = await response.blob();
        await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
        showCopied(btn);
    } catch (err) {
        console.error('Image copy failed', err);
        alert("Image copy failed. Your browser might require HTTPS. Try long-pressing the image instead.");
    }
}

function showCopied(btn) {
    const orig = btn.textContent;
    btn.textContent = '✅';
    setTimeout(() => { btn.textContent = orig; }, 1500);
}
