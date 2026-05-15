/* Viessmann Chat v2 — frontend logic.
   Same look as v1, plus a sources panel with page numbers + rerank scores. */

const API_BASE = '';

const messagesEl    = document.getElementById('messages');
const inputEl       = document.getElementById('chatInput');
const sendBtn       = document.getElementById('sendBtn');
const welcomeScreen = document.getElementById('welcomeScreen');
const loginScreen   = document.getElementById('loginScreen');
const loginForm     = document.getElementById('loginForm');
const loginError    = document.getElementById('loginError');
const loginBtn      = document.getElementById('loginBtn');
const logoutBtn     = document.getElementById('logoutBtn');

let isLoading   = false;
let chatHistory = [];

// ─── Auth ─────────────────────────────────────────────────────────────────────
async function checkAuth() {
  try {
    const r = await fetch(`${API_BASE}/api/check-auth`, { credentials: 'include' });
    const d = await r.json();
    (d.logged_in ? showChat : showLogin)();
  } catch { showLogin(); }
}
function showLogin() { loginScreen.style.display = 'flex'; }
function showChat()  { loginScreen.style.display = 'none'; }

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = document.getElementById('loginUsername').value.trim();
  const password = document.getElementById('loginPassword').value.trim();
  loginBtn.disabled = true; loginBtn.textContent = 'Prijava...'; loginError.textContent = '';
  try {
    const r = await fetch(`${API_BASE}/api/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    });
    const d = await r.json();
    if (r.ok && d.ok) showChat();
    else loginError.textContent = d.error || 'Prijava nije uspjela.';
  } catch {
    loginError.textContent = 'Greška pri povezivanju s poslužiteljem.';
  } finally {
    loginBtn.disabled = false; loginBtn.textContent = 'Prijava';
  }
});

logoutBtn.addEventListener('click', async () => {
  await fetch(`${API_BASE}/api/logout`, { method: 'POST', credentials: 'include' });
  chatHistory = [];
  messagesEl.innerHTML = '';
  if (welcomeScreen) welcomeScreen.style.display = '';
  showLogin();
});

// ─── Chat input ──────────────────────────────────────────────────────────────
inputEl.addEventListener('input', () => {
  inputEl.style.height = '54px';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
});
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

function askSuggestion(btn) { inputEl.value = btn.textContent; sendMessage(); }
window.askSuggestion = askSuggestion;

function getTime() {
  return new Date().toLocaleTimeString('hr-HR', { hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function renderAnswer(s) {
  // Preserve newlines, bold **text**, and inline `code`.
  let html = escapeHtml(s);
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function addUserMessage(text) {
  if (welcomeScreen) welcomeScreen.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'message user';
  div.innerHTML = `
    <div class="msg-avatar">VI</div>
    <div class="msg-content">
      <div class="msg-bubble">${escapeHtml(text).replace(/\n/g,'<br>')}</div>
      <div class="msg-time">${getTime()}</div>
    </div>`;
  messagesEl.appendChild(div); scrollToBottom();
}

function addAssistantMessage(answer, _sources, isError = false) {
  // Sources are intentionally NOT rendered in the UI — citations stay inline
  // inside the answer text. (Backend still ships the `sources` array so eval
  // and metrics can use it; the frontend just ignores it.)
  if (welcomeScreen) welcomeScreen.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="msg-avatar">AI</div>
    <div class="msg-content">
      <div class="msg-bubble${isError ? ' msg-error' : ''}">${renderAnswer(answer)}</div>
      <div class="msg-time">${getTime()}</div>
    </div>`;
  messagesEl.appendChild(div); scrollToBottom();
}

function showTyping() {
  const div = document.createElement('div');
  div.className = 'typing-indicator'; div.id = 'typingIndicator';
  div.innerHTML = `
    <div class="msg-avatar" style="background:var(--red);color:white;width:32px;height:32px;border-radius:2px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex-shrink:0;margin-top:2px;">AI</div>
    <div class="typing-dots"><span></span><span></span><span></span></div>`;
  messagesEl.appendChild(div); scrollToBottom();
}
function hideTyping() { document.getElementById('typingIndicator')?.remove(); }
function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

/* Mount a fresh assistant bubble to fill as tokens arrive. */
function startStreamingMessage() {
  if (welcomeScreen) welcomeScreen.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="msg-avatar">AI</div>
    <div class="msg-content">
      <div class="msg-bubble streaming"></div>
      <div class="msg-time">${getTime()}</div>
    </div>`;
  messagesEl.appendChild(div);
  scrollToBottom();
  return { bubble: div.querySelector('.msg-bubble') };
}

async function sendMessage() {
  const question = inputEl.value.trim();
  if (!question || isLoading) return;

  isLoading = true; sendBtn.disabled = true;
  inputEl.value = ''; inputEl.style.height = '54px';

  addUserMessage(question);
  showTyping();

  let mount = null;          // { bubble } — created on first event
  let answerBuf = '';

  try {
    const r = await fetch(`${API_BASE}/api/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        question,
        product_line: null,
        document_type: null,
        history: chatHistory,
      }),
    });
    if (r.status === 401) { hideTyping(); showLogin(); return; }
    if (!r.ok && !r.body) throw new Error(`HTTP ${r.status}`);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n\n')) !== -1) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 2);
        if (!raw.startsWith('data:')) continue;
        let ev;
        try { ev = JSON.parse(raw.slice(5).trim()); } catch { continue; }

        if (ev.type === 'sources') {
          // Sources are intentionally not rendered — citations stay inline
          // in the answer text. Mount the bubble early so the typing
          // indicator clears as soon as retrieval finishes.
          if (!mount) { hideTyping(); mount = startStreamingMessage(); }
        } else if (ev.type === 'token') {
          if (!mount) { hideTyping(); mount = startStreamingMessage(); }
          answerBuf += ev.content || '';
          mount.bubble.innerHTML = renderAnswer(answerBuf);
          scrollToBottom();
        } else if (ev.type === 'done') {
          if (mount) mount.bubble.classList.remove('streaming');
        } else if (ev.type === 'error') {
          hideTyping();
          addAssistantMessage(ev.error || 'Greška pri komunikaciji s asistentom.', [], true);
          return;
        }
      }
    }

    if (!mount) {
      hideTyping();
      addAssistantMessage('Nema odgovora.', null, true);
      return;
    }

    chatHistory.push({ role: 'user',      content: question });
    chatHistory.push({ role: 'assistant', content: answerBuf || 'Nema odgovora.' });
    if (chatHistory.length > 10) chatHistory = chatHistory.slice(-10);
  } catch (err) {
    hideTyping();
    if (!mount) {
      addAssistantMessage('Greška pri komunikaciji s asistentom. Pokušajte ponovo.', [], true);
    }
    console.error(err);
  } finally {
    isLoading = false; sendBtn.disabled = false; inputEl.focus();
  }
}
window.sendMessage = sendMessage;

checkAuth();
