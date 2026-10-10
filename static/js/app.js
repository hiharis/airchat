let ws;
let username = localStorage.getItem('username');

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
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws/${username}`);
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'history') {
            data.messages.forEach(msg => appendMessage(msg));
            scrollToBottom();
        } else if (data.type === 'message') {
            appendMessage(data);
            scrollToBottom();
        } else if (data.type === 'presence') {
            updateUserList(data.users);
        }
    };
}

function appendMessage(data) {
    const messagesDiv = document.getElementById('messages');
    const msgDiv = document.createElement('div');
    const isSelf = data.username === username;
    msgDiv.className = `message-wrapper ${isSelf ? 'sent' : 'received'}`;
    
    let contentHtml = '';
    let copyAction = '';
    const msgType = data.msg_type || data.type;

    if (msgType === 'text') {
        contentHtml = escapeHtml(data.content);
        copyAction = `<button class="copy-btn" onclick="copyText(this)" title="Copy text">📋</button>`;
    } else if (msgType === 'file') {
        if (data.content.match(/\.(jpg|jpeg|png)$/i)) {
            // Lazy load images to save mobile RAM and CPU
            contentHtml = `<a href="/uploads/${data.content}" target="_blank" rel="noopener"><img class="message-img" src="/uploads/${data.content}" alt="Attachment" loading="lazy"></a>`;
            copyAction = `<button class="copy-btn" onclick="copyImage(this, '/uploads/${data.content}')" title="Copy image">📋</button>`;
        } else {
            contentHtml = `<a href="/uploads/${data.content}" target="_blank">📄 ${data.content}</a>`;
        }
    }
    
    const senderHtml = !isSelf ? `<div class="message-sender">${escapeHtml(data.username)}</div>` : '';
    
    const now = new Date();
    // TODO: server-side timestamp not yet in payload; shows render time for history
    const timeStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
    
    msgDiv.innerHTML = `
        ${senderHtml}
        <div class="message-bubble">
            <div class="msg-content">${contentHtml}</div>
            <span class="msg-time">${timeStr}</span>
            ${copyAction}
        </div>
    `;
    
    messagesDiv.appendChild(msgDiv);
    
    // MEMORY OPTIMIZATION: Keep only the 50 most recent messages in the browser
    // This prevents phones from lagging or crashing during heavy chat sessions
    while (messagesDiv.children.length > 50) {
        messagesDiv.removeChild(messagesDiv.firstChild);
    }
}

function scrollToBottom() {
    const messagesDiv = document.getElementById('messages');
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function updateUserList(users) {
    const ul = document.getElementById('user-list');
    ul.innerHTML = '';
    users.forEach(user => {
        const li = document.createElement('li');
        li.textContent = user;
        ul.appendChild(li);
    });
}

function sendMessage() {
    const input = document.getElementById('message-input');
    const content = input.value.trim();
    if (content && ws) {
        ws.send(content);
        input.value = '';
    }
}

// --- EVENT LISTENERS ---
document.getElementById('message-input').addEventListener('keypress', function (e) {
    if (e.key === 'Enter') sendMessage();
});

document.getElementById('file-input').addEventListener('change', async function(e) {
    const file = e.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);
    formData.append('username', username);

    try {
        await fetch('/upload', {
            method: 'POST',
            body: formData
        });
    } catch (err) {
        console.error('Upload failed', err);
        alert('File upload failed. Max size is 25MB.');
    }
});

// --- UI & SETTINGS LOGIC ---
function openSettings() {
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

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.toggle('open');
    overlay.classList.toggle('show');
}

function toggleFullScreen() {
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(err => {
            console.log(`Error attempting to enable fullscreen: ${err.message}`);
        });
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        }
    }
}

// Security
function escapeHtml(unsafe) {
    return unsafe
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
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
    const orig = btn.innerHTML;
    btn.innerHTML = '✅';
    setTimeout(() => { btn.innerHTML = orig; }, 1500);
}
// End of app.js
