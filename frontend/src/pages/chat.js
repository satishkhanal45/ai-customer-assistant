/* Chat portal — multi-turn conversational UI against POST /chat. */
(function (NS) {
  'use strict';

  var LS_KEY = 'aca.chat.threads.v1';
  var threads = [];
  var activeId = null;
  var pendingRetry = null;   // { threadId, message } when the last send failed
  var sending = false;

  var roots = {}; // container element refs

  function load() {
    try { threads = JSON.parse(localStorage.getItem(LS_KEY)) || []; }
    catch (e) { threads = []; }
    if (!Array.isArray(threads)) threads = [];
  }

  function save() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(threads)); } catch (e) {}
  }

  function thread(id) {
    for (var i = 0; i < threads.length; i++) if (threads[i].id === id) return threads[i];
    return null;
  }

  function newThread() {
    var t = { id: NS.utils.uuid(), title: '', createdAt: Date.now(), updatedAt: Date.now(), messages: [] };
    threads.unshift(t);
    save();
    return t;
  }

  function ensureThread(id) {
    var t = id ? thread(id) : null;
    if (!t) { t = newThread(); if (id) t.id = id; }
    return t;
  }

  function titleFor(t) {
    if (t.title) return t.title;
    var first = (t.messages || []).filter(function (m) { return m.role === 'user'; })[0];
    if (first) {
      var s = first.content.trim();
      t.title = s.length > 42 ? s.slice(0, 42) + '…' : s;
      return t.title;
    }
    return 'New conversation';
  }

  function init(container) {
    load();
    var current = null;
    if (activeId) current = thread(activeId);
    if (!current && threads.length) current = threads[0];
    if (!current) current = newThread();
    activeId = current.id;
    renderShell(container);
    renderThreadList();
    renderMessages();
    focusComposer();
  }

  function destroy() {
    roots = {};
  }

  function renderShell(container) {
    container.innerHTML = '';
    container.style.display = 'flex';
    container.style.minHeight = '0';

    var side = NS.utils.el('aside', { class: 'sidebar' });
    var main = NS.utils.el('div', { class: 'chat-main' });
    container.appendChild(side);
    container.appendChild(main);

    roots.side = side;
    roots.main = main;
    roots.messages = NS.utils.el('div', { class: 'chat-scroll' });
    roots.composerBar = NS.utils.el('div', { class: 'composer' });

    var newBtn = NS.utils.el('button', { class: 'action primary' }, 'New conversation');
    newBtn.addEventListener('click', function () { activeId = newThread().id; renderThreadList(); renderMessages(); focusComposer(); });
    side.appendChild(newBtn);

    roots.threadList = NS.utils.el('div', { class: 'thread-list' });
    side.appendChild(roots.threadList);

    var hint = NS.utils.el('div', { class: 'hint' }, 'History is stored on your device. The backend keeps the conversation state per thread id.');
    side.appendChild(hint);

    main.appendChild(roots.messages);

    var input = NS.utils.el('input', { class: 'text' });
    input.placeholder = 'Type a message…';
    input.setAttribute('autocomplete', 'off');
    var send = NS.utils.el('button', { class: 'action primary' }, 'Send');
    send.addEventListener('click', sendMessage);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); sendMessage(); } });

    var inner = NS.utils.el('div', { class: 'composer-inner' });
    inner.appendChild(input);
    inner.appendChild(send);
    roots.composerBar.appendChild(inner);
    main.appendChild(roots.composerBar);

    roots.input = input;
    roots.sendBtn = send;
  }

  function renderThreadList() {
    var list = roots.threadList;
    if (!list) return;
    list.innerHTML = '';
    if (!threads.length) {
      list.innerHTML = '<div class="hint" style="padding:8px">No conversations yet.</div>';
      return;
    }
    threads.forEach(function (t) {
      var item = NS.utils.el('div', { class: 'thread-item' + (t.id === activeId ? ' active' : '') });
      item.innerHTML = '<div class="ti-title">' + NS.utils.esc(titleFor(t)) + '</div>' +
        '<div class="ti-meta">' + NS.utils.esc(NS.utils.formatDate(t.updatedAt)) + '</div>';
      item.addEventListener('click', function () { activeId = t.id; renderThreadList(); renderMessages(); focusComposer(); });
      var del = NS.utils.el('span', { class: 'ti-del', title: 'Delete' }, '×');
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        threads = threads.filter(function (x) { return x.id !== t.id; });
        save();
        if (activeId === t.id) { activeId = threads.length ? threads[0].id : null; }
        renderThreadList();
        renderMessages();
      });
      item.appendChild(del);
      list.appendChild(item);
    });
  }

  function renderMessages() {
    var area = roots.messages;
    if (!area) return;
    var t = thread(activeId);
    area.innerHTML = '';
    if (!t || !t.messages.length) {
      area.appendChild(NS.utils.el('div', { class: 'welcome' },
        '<div class="welcome-title">How can I help you?</div>' +
        '<div class="hint">Ask about our knowledge base, or say "create a ticket" to open a support request.</div>' +
        '<div class="welcome-prompts">' +
        '<button class="action" data-p="What support plans do you offer?">What support plans do you offer?</button>' +
        '<button class="action" data-p="How do I request a refund?">How do I request a refund?</button>' +
        '<button class="action" data-p="Create a ticket">Create a ticket</button>' +
        '</div>'));
      var prompBtns = area.querySelectorAll('.welcome-prompts button');
      Array.prototype.forEach.call(prompBtns, function (b) {
        b.addEventListener('click', function () { roots.input.value = b.getAttribute('data-p'); sendMessage(); });
      });
      return;
    }
    t.messages.forEach(function (m) {
      var bubble = NS.utils.el('div', { class: 'msg ' + (m.role === 'user' ? 'msg-user' : 'msg-assistant') });
      if (m.role === 'user') {
        bubble.innerHTML = '<div class="bubble">' + NS.utils.esc(m.content).replace(/\n/g, '<br>') + '</div>';
      } else if (m.error) {
        bubble.innerHTML = '<div class="bubble bubble-error">' + NS.utils.esc(m.content) + '</div>' +
          (m.retriable ? '<button class="action retry">Retry</button>' : '');
        var rb = bubble.querySelector('.retry');
        if (rb) rb.addEventListener('click', function () {
          pendingRetry = { threadId: activeId, message: m.resendMessage || m.content };
          sendMessage();
        });
      } else {
        var html = '<div class="bubble">' + renderAssistant(m.content) + '</div>';
        if (m.citations && m.citations.length) {
          var cites = m.citations.map(function (c) {
            var parts = [];
            if (c.source_name) parts.push('<b>' + NS.utils.esc(c.source_name) + '</b>');
            if (c.page != null) parts.push('p.' + NS.utils.esc(String(c.page)));
            if (c.version_number != null) parts.push('v' + NS.utils.esc(String(c.version_number)));
            return '<div class="citation">' + parts.join(' · ') + '</div>';
          }).join('');
          html += '<div class="citations">' + cites + '</div>';
        }
        if (m.traceId) {
          html += '<div class="debug"><span class="trace" title="trace_id">' + NS.utils.esc(m.traceId) + '</span></div>';
        }
        bubble.innerHTML = html;
      }
      area.appendChild(bubble);
    });
    area.scrollTop = area.scrollHeight;
  }

  function linkify(text) {
    return NS.utils.esc(text).replace(
      /\bhttps?:\/\/[^\s<>"']+/g,
      '<a href="$&" target="_blank" rel="noopener">$&</a>'
    );
  }

  function renderAssistant(content) {
    var lines = String(content || '').split(/\n/);
    var html = lines.map(function (line) {
      var t = line.trim();
      if (t === '') return '';
      if (/^(#{1,3})\s/.test(t)) return '<div class="md-h">' + t.replace(/^#{1,3}\s/, '') + '</div>';
      if (/^\s*[-*]\s/.test(t)) return '<div class="md-li">• ' + t.replace(/^\s*[-*]\s/, '') + '</div>';
      if (/^\s*\d+[.)]\s/.test(t)) return '<div class="md-li">' + t + '</div>';
      return '<p>' + linkify(t) + '</p>';
    }).join('');
    return html || '<p>' + NS.utils.esc(content || '') + '</p>';
  }

  function setBusy(busy) {
    sending = busy;
    roots.sendBtn.disabled = busy;
    roots.input.disabled = busy;
    var typing = roots.messages.querySelector('.msg-typing');
    if (busy && !typing) {
      roots.messages.appendChild(NS.utils.el('div', { class: 'msg msg-assistant msg-typing' }, '<div class="bubble typing">thinking…</div>'));
      roots.messages.scrollTop = roots.messages.scrollHeight;
    }
    if (!busy && typing) typing.remove();
  }

  function sendMessage() {
    if (sending) return;
    var value = roots.input.value.trim();
    var retry = pendingRetry;
    pendingRetry = null;

    if (retry) { send(retry.threadId, retry.message); return; }
    if (!value) return;

    var t = ensureThread(activeId);
    t.messages.push({ role: 'user', content: value });
    t.updatedAt = Date.now();
    if (!t.title) titleFor(t);
    save();
    roots.input.value = '';
    renderThreadList();
    renderMessages();
    send(t.id, value);
  }

  function send(threadId, message) {
    setBusy(true);
    NS.api.post('/chat', { thread_id: threadId, message: message }).then(function (res) {
      var t = ensureThread(threadId);
      t.messages.push({ role: 'assistant', content: res.reply || '', traceId: res.trace_id, citations: res.citations || [] });
      t.updatedAt = Date.now();
      save();
      if (activeId === threadId) { renderThreadList(); renderMessages(); }
      setBusy(false);
    }).catch(function (err) {
      var t = ensureThread(threadId);
      t.messages.push({ role: 'assistant', content: 'Error: ' + (err.message || 'request failed'), error: true, retriable: true, resendMessage: message });
      t.updatedAt = Date.now();
      save();
      if (activeId === threadId) { renderThreadList(); renderMessages(); }
      setBusy(false);
      NS.utils.status('Chat request failed', true);
    });
  }

  function focusComposer() {
    if (roots.input) { roots.input.focus(); }
  }

  // Inline styles for the chat page (kept separate from the global theme).
  var STYLE = document.createElement('style');
  STYLE.textContent = [
    '.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }',
    '.chat-scroll { flex: 1; overflow-y: auto; padding: 24px 28px 32px; display: flex; flex-direction: column; gap: 14px; max-width: 900px; width: 100%; margin: 0 auto; }',
    '.composer { display: flex; padding: 22px; border-top: 4px solid var(--border); background: var(--panel-solid); margin: 0;}',
    '.composer-inner { display: flex; align-items: center; gap: 6px; width: 100%; max-width: 800px; margin: 0 auto; }',
    '.composer-inner input { flex: 0 1 700px; min-width: 0; }',
    '.composer-inner button { flex-shrink: 0; margin-left: auto; }',
    '.thread-list { display: flex; flex-direction: column; gap: 6px; }',
    '.thread-item { position: relative; padding: 10px 32px 10px 12px; border: 1px solid var(--border); border-radius: var(--r-md); cursor: pointer; transition: all .15s ease; background: transparent; }',
    '.thread-item:hover { border-color: var(--accent); background: var(--accent-soft); }',
    '.thread-item.active { border-color: var(--accent); background: var(--accent-soft); }',
    '.ti-title { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
    '.ti-meta { font-size: 10px; color: var(--muted); margin-top: 3px; }',
    '.ti-del { position: absolute; right: 9px; top: 50%; transform: translateY(-50%); color: var(--muted); cursor: pointer; font-size: 16px; line-height: 1; opacity: 0; transition: opacity .15s ease; }',
    '.thread-item:hover .ti-del { opacity: 1; }',
    '.ti-del:hover { color: var(--accent); }',
    '.msg { display: flex; flex-direction: column; max-width: 78%; }',
    '.msg-user { align-self: flex-end; align-items: flex-end; }',
    '.msg-assistant { align-self: flex-start; align-items: flex-start; }',
    '.bubble { padding: 10px 16px; border-radius: 14px; font-size: 13.5px; line-height: 1.6; word-break: break-word; box-shadow: var(--shadow-sm); }',
    '.msg-user .bubble { background: var(--panel-2); border: 1px solid var(--accent-dim); border-bottom-right-radius: 6px; }',
    '.msg-assistant .bubble { background: var(--panel); border: 1px solid var(--border); border-bottom-left-radius: 6px; }',
    '.bubble-error { border-color: var(--accent) !important; color: var(--text); font-weight: 600; }',
    '.citations { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; max-width: 78%; }',
    '.citation { font-size: 11px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 5px 9px; line-height: 1.5; }',
    '.citation b { color: var(--accent); font-weight: 600; }',
    '.bubble p { margin: 0 0 6px; } .bubble p:last-child { margin-bottom: 0; }',
    '.bubble a { color: var(--accent); }',
    '.md-h { font-weight: 700; margin: 6px 0 4px; font-size: 14px; }',
    '.md-li { padding-left: 4px; }',
    '.typing { color: var(--muted); font-style: italic; }',
    '.retry { margin-top: 6px; align-self: flex-start; }',
    '.debug { margin-top: 5px; } .trace { font-family: var(--mono); font-size: 10px; color: var(--muted); }',
    '.welcome { text-align: center; margin: auto; max-width: 560px; }',
    '.welcome-title { font-size: 20px; font-weight: 650; margin-bottom: 8px; }',
    '.welcome .hint { font-size: 13px; }',
    '.welcome-prompts { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 20px; }',
    '@media (max-width: 1080px) { .msg { max-width: 94%; } .chat-scroll, .composer { padding-left: 16px; padding-right: 16px; } }'
  ].join('\n');
  document.head.appendChild(STYLE);

  NS.pages = NS.pages || {};
  NS.pages.chat = { init: init, destroy: destroy };
})(window.ACA);