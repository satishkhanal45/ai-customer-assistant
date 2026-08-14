/* Overview dashboard — hero band with live-ish stats, system status and
   module launchers. Falls back to graceful dashes when admin endpoints
   are not wired yet. */
(function (NS) {
  'use strict';

  var roots = {};

  function init(container) {
    container.innerHTML = '';
    container.style.minHeight = '0';

    var scroll = NS.utils.el('div', { class: 'overview-scroll' });
    container.appendChild(scroll);

    scroll.innerHTML =
      '<h1 class="page-title">Overview</h1>' +
      '<p class="page-sub">A high-level summary of your AI Customer Assistant workspace.</p>' +

      '<div class="hero-band">' +
      heroField() +
      '<div class="stat-grid">' +
      statCard('blue', 'Knowledge Sources', '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>', 'srcCount', 'Documents indexed') +
      statCard('purple', 'Entities', '<circle cx="12" cy="6" r="3"/><circle cx="5" cy="18" r="3"/><circle cx="19" cy="18" r="3"/><path d="M12 9v4M9 15.5 10.5 13M15 15.5 13.5 13"/>', 'entityCount', 'Entities in knowledge graph') +
      statCard('blue', 'Relationships', '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8 8l8 8"/>', 'relCount', 'Graph relationships') +
      statCard('green', 'Conversations', '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>', 'convCount', 'Total conversations') +
      '</div>' +
      '<div class="status-banner" id="statusBanner">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 12 2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>' +
      '<span>Checking system status…</span>' +
      '</div>' +
      '</div>' +

      '<h2 class="section-title">Modules</h2>' +
      '<div class="module-grid">' +
      moduleCard('chat', 'Chat', 'Ask questions and interact with the AI Customer Assistant.', '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>') +
      moduleCard('graph', 'Knowledge Graph', 'Explore entities and relationships visually.', '<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7 7.3 10.3 16 M17 7.3 13.7 16 M7.5 6h9"/>') +
      moduleCard('ingest', 'Data Ingestion', 'Upload documents or crawl web content into the knowledge base.', '<path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3M17 8l-5-5-5 5M12 3v12"/>') +
      moduleCard('admin', 'Admin', 'Monitor sources, jobs and system statistics.', '<path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>') +
      '</div>';

    roots.scroll = scroll;
    loadStats();
    loadHealth();
    document.querySelectorAll('.module-card .btn').forEach(function (b) {
      b.addEventListener('click', function () { NS.router.go(b.getAttribute('data-go')); });
    });
  }

  function destroy() { roots = {}; }

  function heroField() {
    return '<div class="graph-field" aria-hidden="true">' +
      '<svg viewBox="0 0 900 260" preserveAspectRatio="none">' +
      '<g class="gf-drift">' +
      '<line class="gf-edge" x1="60" y1="40" x2="180" y2="90"/><line class="gf-edge" x1="180" y1="90" x2="120" y2="180"/>' +
      '<line class="gf-edge" x1="180" y1="90" x2="320" y2="60"/><line class="gf-edge" x1="320" y1="60" x2="420" y2="150"/>' +
      '<line class="gf-edge" x1="420" y1="150" x2="300" y2="220"/><line class="gf-edge" x1="320" y1="60" x2="480" y2="30"/>' +
      '<line class="gf-edge" x1="480" y1="30" x2="600" y2="100"/><line class="gf-edge" x1="600" y1="100" x2="560" y2="210"/>' +
      '<line class="gf-edge" x1="600" y1="100" x2="740" y2="70"/><line class="gf-edge" x1="740" y1="70" x2="820" y2="160"/>' +
      '<circle class="gf-node gf-pulse" cx="60" cy="40" r="4"/><circle class="gf-node alt gf-pulse" cx="180" cy="90" r="5" style="animation-delay:.3s"/>' +
      '<circle class="gf-node gf-pulse" cx="120" cy="180" r="4" style="animation-delay:.6s"/><circle class="gf-node alt gf-pulse" cx="320" cy="60" r="5" style="animation-delay:.9s"/>' +
      '<circle class="gf-node gf-pulse" cx="420" cy="150" r="4" style="animation-delay:1.2s"/><circle class="gf-node alt gf-pulse" cx="300" cy="220" r="4" style="animation-delay:1.5s"/>' +
      '<circle class="gf-node gf-pulse" cx="480" cy="30" r="4" style="animation-delay:1.8s"/><circle class="gf-node alt gf-pulse" cx="600" cy="100" r="5" style="animation-delay:2.1s"/>' +
      '<circle class="gf-node gf-pulse" cx="560" cy="210" r="4" style="animation-delay:2.4s"/><circle class="gf-node alt gf-pulse" cx="740" cy="70" r="4" style="animation-delay:2.7s"/>' +
      '<circle class="gf-node gf-pulse" cx="820" cy="160" r="4" style="animation-delay:3s"/>' +
      '</g></svg></div>';
  }

  function statCard(color, label, paths, id, sub) {
    return '<div class="card stat-card" style="--stat-accent: var(--' + color + ');">' +
      '<div class="stat-top">' +
      '<span class="stat-label">' + label + '</span>' +
      '<div class="stat-icon ' + color + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' + paths + '</svg></div>' +
      '</div>' +
      '<div class="stat-value" id="' + id + '">—</div>' +
      '<div class="stat-label">' + sub + '</div>' +
      '</div>';
  }

  function moduleCard(key, title, desc, paths) {
    return '<div class="module-card">' +
      '<div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' + paths + '</svg></div>' +
      '<h3>' + title + '</h3>' +
      '<p>' + desc + '</p>' +
      '<button class="btn btn-primary btn-full" data-go="' + key + '">Open</button>' +
      '</div>';
  }

  function setCount(id, value) {
    var el = document.getElementById(id);
    if (!el) return;
    if (value == null || isNaN(Number(value))) { el.textContent = '—'; return; }
    animate(el, Number(value));
  }

  function animate(el, target) {
    var start = performance.now();
    var dur = 700;
    function tick(t) {
      var p = Math.min((t - start) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased).toLocaleString();
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  function loadStats() {
    // conversations from local thread store
    var conv = 0;
    try { var arr = JSON.parse(localStorage.getItem('aca.chat.threads.v1')) || []; conv = arr.length; } catch (e) {}
    setCount('convCount', conv);

    var set = {
      srcCount: null,
      entityCount: null,
      relCount: null
    };

    // Prefer /admin/stats when the backend exposes it.
    NS.api.get('/admin/stats').then(function (d) {
      if (d && d.entities && d.entities.total != null) set.entityCount = d.entities.total;
      if (d && d.relations && d.relations.total != null) set.relCount = d.relations.total;
      if (d && d.sources && d.sources.total != null) set.srcCount = d.sources.total;
      apply(set);
    }).catch(function () {
      // Fallbacks: count sources via the list endpoint, entities via search.
      NS.api.get('/admin/knowledge-sources').then(function (d) {
        set.srcCount = (Array.isArray(d) ? d : (d && d.sources) || []).length;
        apply(set);
      }).catch(function () { apply(set); });
      NS.api.get('/graph/search', { q: '', limit: 200 }).then(function (list) {
        if (Array.isArray(list) && list.length) set.entityCount = list.length;
        apply(set);
      }).catch(function () {});
    });
  }

  function apply(set) {
    setCount('srcCount', set.srcCount);
    setCount('entityCount', set.entityCount);
    setCount('relCount', set.relCount);
  }

  function loadHealth() {
    var banner = document.getElementById('statusBanner');
    if (!banner) return;
    NS.api.get('/health').then(function (r) {
      if (r && r.status === 'ok') {
        banner.classList.remove('error');
        banner.querySelector('span').textContent = 'All systems operational';
      } else {
        banner.classList.add('error');
        banner.querySelector('span').textContent = 'Backend reporting issues';
      }
    }).catch(function () {
      banner.classList.add('error');
      banner.querySelector('span').textContent = 'Backend offline — connect to the API server';
    });
  }

  NS.pages = NS.pages || {};
  NS.pages.overview = { init: init, destroy: destroy };
})(window.ACA);
