/* Hash router — renders page modules into #view. Loaded after config.js. */
(function (NS) {
  'use strict';

  var current = null;
  var currentEl = null;

  var LINKS = [
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
    var nav = document.querySelector('nav.links');
    if (!nav) return;
    nav.innerHTML = LINKS.map(function (l) {
      return '<a href="#/' + l.key + '" class="' + (l.key === activeKey ? 'on' : '') + '">' + NS.utils.esc(l.label) + '</a>';
    }).join('');
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
    currentEl.style.flex = '1';
    currentEl.style.minHeight = '0';
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

  NS.router = {
    init: function () {
      window.addEventListener('hashchange', render);
      render();
    },
    parseHash: parseHash,
    go: function (key) { location.hash = '#/' + key; }
  };
})(window.ACA);