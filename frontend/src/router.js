/* Hash router — renders page modules into #view. Loaded after config.js. */
(function (NS) {
  'use strict';

  var current = null;
  var currentEl = null;

  var ICONS = {
    overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    graph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7 7.3 10.3 16 M17 7.3 13.7 16 M7.5 6h9"/></svg>',
    ingest: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3M17 8l-5-5-5 5M12 3v12"/></svg>',
    admin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>'
  };

  var LINKS = [
    { key: 'overview', label: 'Overview', title: 'AI Customer Assistant — Overview' },
    { key: 'chat', label: 'Chat', title: 'AI Customer Assistant — Chat' },
    { key: 'graph', label: 'Graph', title: 'AI Customer Assistant — Knowledge Graph' },
    { key: 'ingest', label: 'Ingest', title: 'AI Customer Assistant — Ingest' },
    { key: 'admin', label: 'Admin', title: 'AI Customer Assistant — Admin' }
  ];

  function parseHash() {
    var h = (location.hash || '').replace(/^#\/?/, '');
    var first = h.split(/[&?]/)[0];
    return first || 'chat';
  }

  function renderNav(activeKey) {
    var wrap = document.getElementById('navItems');
    if (!wrap) return;
    wrap.innerHTML = LINKS.map(function (l) {
      return '<button class="nav-item' + (l.key === activeKey ? ' active' : '') + '" data-page="' + l.key + '">' +
        (ICONS[l.key] || '') + '<span>' + NS.utils.esc(l.label) + '</span></button>';
    }).join('');
    Array.prototype.forEach.call(wrap.querySelectorAll('.nav-item'), function (b) {
      b.addEventListener('click', function () { NS.router.go(b.getAttribute('data-page')); });
    });
  }

  function destroyCurrent() {
    if (current && typeof current.destroy === 'function') {
      try { current.destroy(); } catch (e) { /* ignore */ }
    }
    if (currentEl) { currentEl.innerHTML = ''; currentEl.remove(); }
    current = null; currentEl = null;
  }

  function render() {
    var key = parseHash();
    var page = NS.pages && NS.pages[key];
    if (!page) { key = 'chat'; page = NS.pages.chat; }

    destroyCurrent();
    renderNav(key);

    var view = document.getElementById('view');
    currentEl = document.createElement('div');
    currentEl.id = 'page-' + key;
    currentEl.className = 'page';
    view.appendChild(currentEl);
    current = page;
    document.title = (LINKS.filter(function (l) { return l.key === key; })[0] || {}).title || document.title;

    try {
      page.init(currentEl);
    } catch (e) {
      currentEl.innerHTML = '<div class="hint" style="padding:20px">Failed to load ' + key + ': ' + NS.utils.esc(e.message) + '</div>';
      console.error(e);
    }
  }

  function wireSidebar() {
    var btn = document.getElementById('sidebarCollapseBtn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var app = document.querySelector('.app');
      var collapsed = app.classList.toggle('sidebar-collapsed');
      try { localStorage.setItem('aica-sidebar-collapsed', collapsed ? '1' : '0'); } catch (e) {}
    });
    try {
      if (localStorage.getItem('aica-sidebar-collapsed') === '1') {
        document.querySelector('.app').classList.add('sidebar-collapsed');
      }
    } catch (e) {}
  }

  function checkHealth() {
    var pill = document.getElementById('sysStatus');
    if (!pill) return;
    var dot = pill.querySelector('.dot');
    var label = pill.querySelector('span:last-child');
    function set(state, cls) {
      if (dot) dot.style.background = 'var(--' + cls + ')';
      if (label) label.textContent = state;
    }
    if (!NS.api) { return; }
    NS.api.get('/health').then(function (r) {
      set((r && r.status === 'ok') ? 'System Online' : 'System Offline', (r && r.status === 'ok') ? 'green' : 'red');
    }).catch(function () { set('System Offline', 'red'); });
  }

  NS.router = {
    init: function () {
      wireSidebar();
      checkHealth();
      window.addEventListener('hashchange', render);
      render();
    },
    parseHash: parseHash,
    go: function (key) { location.hash = '#/' + key; }
  };
})(window.ACA);
