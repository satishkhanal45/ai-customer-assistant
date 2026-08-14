/* Chat portal — multi-turn conversational UI against POST /chat. */
(function (NS) {
  'use strict';

  var LS_KEY = 'aca.chat.threads.v1';
  var threads = [];
  var activeId = null;
  var pendingRetry = null;   // { threadId, message } when the last send failed
  var sending = false;

  var roots = {}; // container element refs

  var ICON_USER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>';
  var ICON_BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4c0 1.5-.8 2.7-2 3.4V11h2a4 4 0 0 1 4 4v1a4 4 0 0 1-4 4h-.5a2.5 2.5 0 0 1-5 0H10a2.5 2.5 0 0 1-5 0H4a4 4 0 0 1-4-4v-1a4 4 0 0 1 4-4h2V9.4C4.8 8.7 4 7.5 4 6a4 4 0 0 1 8-4z"/></svg>';
  var ICON_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

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

  function relTime(ts) {
    if (!ts) return '';
    var s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    if (s < 60) return 'Just now';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    var d = Math.floor(h / 24);
    if (d < 7) return d + 'd ago';
    return NS.utils.formatDate(ts);
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
    container.style.padding = '0';

    var shell = NS.utils.el('div', { class: 'chat-shell' });
    container.appendChild(shell);

    var convPanel = NS.utils.el('aside', { class: 'conv-panel' });
    var chatMain = NS.utils.el('div', { class: 'chat-main' });
    shell.appendChild(convPanel);
    shell.appendChild(chatMain);

    var head = NS.utils.el('div', { class: 'conv-panel-head' });
    var newBtn = NS.utils.el('button', { class: 'new-chat-btn' },
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>New Chat');
    newBtn.addEventListener('click', function () { activeId = newThread().id; renderThreadList(); renderMessages(); focusComposer(); });
    head.appendChild(newBtn);
    convPanel.appendChild(head);

    roots.threadList = NS.utils.el('div', { class: 'conv-list' });
    convPanel.appendChild(roots.threadList);

    convPanel.appendChild(NS.utils.el('div', { class: 'conv-hint' },
      'History is stored on your device. The backend keeps the conversation state per thread id.'));

    var chatHead = NS.utils.el('div', { class: 'chat-head' });
    chatHead.innerHTML = '<h3 id="chatHeadTitle">New conversation</h3><span class="chat-head-badge">AI Assistant</span>';
    chatMain.appendChild(chatHead);

    roots.messages = NS.utils.el('div', { class: 'chat-messages' });
    chatMain.appendChild(roots.messages);

    var inputBar = NS.utils.el('div', { class: 'chat-input-bar' });
    var input = NS.utils.el('input');
    input.placeholder = 'Type a message…';
    input.setAttribute('autocomplete', 'off');
    var send = NS.utils.el('button', { class: 'send-btn', title: 'Send' },
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>');
    send.addEventListener('click', sendMessage);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); sendMessage(); } });
    inputBar.appendChild(input);
    inputBar.appendChild(send);
    chatMain.appendChild(inputBar);

    roots.input = input;
    roots.sendBtn = send;
    roots.headTitle = chatHead.querySelector('#chatHeadTitle');
  }

  function renderThreadList() {
    var list = roots.threadList;
    if (!list) return;
    list.innerHTML = '';
    if (!threads.length) {
      list.innerHTML = '<div class="hint" style="padding:10px">No conversations yet.</div>';
      return;
    }
    threads.forEach(function (t) {
      var item = NS.utils.el('div', { class: 'conv-item' + (t.id === activeId ? ' active' : '') });
      item.innerHTML = '<div class="conv-main">' +
        '<div class="conv-title">' + NS.utils.esc(titleFor(t)) + '</div>' +
        '<div class="conv-time">' + NS.utils.esc(relTime(t.updatedAt)) + '</div>' +
        '</div>' +
        '<div class="conv-menu" title="Delete">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>' +
        '</div>';
      item.addEventListener('click', function () { activeId = t.id; renderThreadList(); renderMessages(); focusComposer(); });
      var del = item.querySelector('.conv-menu');
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        threads = threads.filter(function (x) { return x.id !== t.id; });
        save();
        if (activeId === t.id) { activeId = threads.length ? threads[0].id : null; }
        renderThreadList();
        renderMessages();
      });
      list.appendChild(item);
    });
  }

  function renderMessages() {
    var area = roots.messages;
    if (!area) return;
    var t = thread(activeId);
    if (roots.headTitle) roots.headTitle.textContent = titleFor(t || {});
    area.innerHTML = '';
    if (!t || !t.messages.length) {
      area.appendChild(NS.utils.el('div', { class: 'welcome' },
        '<div class="welcome-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>' +
        '<div class="welcome-title">How can I help you?</div>' +
        '<div class="hint">Ask about our knowledge base, or say "create a ticket" to open a support request.</div>' +
        '<div class="welcome-prompts">' +
        '<button class="btn btn-ghost" data-p="What support plans do you offer?">What support plans do you offer?</button>' +
        '<button class="btn btn-ghost" data-p="How do I request a refund?">How do I request a refund?</button>' +
        '<button class="btn btn-ghost" data-p="Create a ticket">Create a ticket</button>' +
        '</div>'));
      var prompBtns = area.querySelectorAll('.welcome-prompts button');
      Array.prototype.forEach.call(prompBtns, function (b) {
        b.addEventListener('click', function () { roots.input.value = b.getAttribute('data-p'); sendMessage(); });
      });
      return;
    }
    t.messages.forEach(function (m) {
      area.appendChild(buildMessage(m));
    });
    area.scrollTop = area.scrollHeight;
  }

  function buildMessage(m) {
    var row = NS.utils.el('div', { class: 'msg-row ' + (m.role === 'user' ? 'user' : 'assistant') });
    row.innerHTML = '<div class="msg-avatar">' + (m.role === 'user' ? ICON_USER : ICON_BOT) + '</div><div class="msg-col"></div>';
    var col = row.querySelector('.msg-col');

    if (m.role === 'user') {
      col.innerHTML = '<div class="msg-bubble">' + NS.utils.esc(m.content).replace(/\n/g, '<br>') + '</div>';
    } else if (m.error) {
      col.innerHTML = '<div class="msg-bubble msg-bubble-error">' + NS.utils.esc(m.content) + '</div>' +
        (m.retriable ? '<button class="btn btn-ghost btn-sm retry">Retry</button>' : '');
      var rb = col.querySelector('.retry');
      if (rb) rb.addEventListener('click', function () {
        pendingRetry = { threadId: activeId, message: m.resendMessage || m.content };
        sendMessage();
      });
    } else {
      var html = '<div class="msg-bubble">' + renderAssistant(m.content) + '</div>';
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
      html += '<div class="msg-actions">' +
        '<button class="msg-action-btn" title="Copy" data-text="' + NS.utils.esc(m.content).replace(/"/g, '&quot;') + '">' + ICON_COPY + '</button>' +
        '</div>';
      col.innerHTML = html;
      var copy = col.querySelector('.msg-action-btn');
      if (copy) copy.addEventListener('click', function () { copyMessage(copy); });
    }
    return row;
  }

  function copyMessage(btn) {
    var text = btn.getAttribute('data-text');
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).catch(function () {});
    var original = btn.innerHTML;
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 12 2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>';
    setTimeout(function () { btn.innerHTML = original; }, 1200);
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
      roots.messages.appendChild(NS.utils.el('div', { class: 'msg-row assistant msg-typing' },
        '<div class="msg-avatar">' + ICON_BOT + '</div>' +
        '<div class="msg-col"><div class="msg-bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div></div>'));
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
    if (roots.input) roots.input.focus();
  }

  NS.pages = NS.pages || {};
  NS.pages.chat = { init: init, destroy: destroy };
})(window.ACA);
