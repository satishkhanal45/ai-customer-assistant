/* Admin dashboard — Sources / Jobs / Stats / Tickets.
   Backend endpoints are not wired yet (see frontend_plan.md §6.2); the UI
   renders a graceful "not available" state until they exist. */
(function (NS) {
  'use strict';

  var ENDPOINTS = {
    sources: '/admin/knowledge-sources',
    jobs: '/admin/jobs',
    stats: '/admin/stats',
    tickets: '/admin/tickets'
  };

  var STATUS_BADGES = {
    PENDING: 'badge-muted', PROCESSING: 'badge-accent', INDEXED: 'badge-ok',
    FAILED: 'badge-bad', STALE: 'badge-warn', ARCHIVED: 'badge-muted',
    QUEUED: 'badge-muted', RUNNING: 'badge-accent', SUCCEEDED: 'badge-ok'
  };

  var roots = {};
  var activeTab = 'sources';

  function init(container) {
    container.innerHTML = '';
    container.style.display = 'flex';
    container.style.minHeight = '0';

    var wrap = NS.utils.el('div', { class: 'admin-wrap' });
    container.appendChild(wrap);

    var tabs = NS.utils.el('div', { class: 'seg admin-tabs' },
      '<button data-tab="sources">Sources</button>' +
      '<button data-tab="jobs">Jobs</button>' +
      '<button data-tab="stats">Stats</button>' +
      '<button data-tab="tickets">Tickets</button>');
    wrap.appendChild(tabs);

    roots.tabs = tabs;
    roots.body = NS.utils.el('div', { class: 'admin-body' });
    wrap.appendChild(roots.body);

    Array.prototype.forEach.call(tabs.querySelectorAll('button'), function (b) {
      b.addEventListener('click', function () {
        activeTab = b.getAttribute('data-tab');
        renderTab();
      });
    });
    renderTab();
  }

  function destroy() { roots = {}; }

  function renderTab() {
    Array.prototype.forEach.call(roots.tabs.querySelectorAll('button'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-tab') === activeTab);
    });
    roots.body.innerHTML = '';
    fetchTab(activeTab).then(function (result) {
      if (result.ok) renderData(activeTab, result.data);
      else renderUnavailable(activeTab, result.error);
    });
  }

  function fetchTab(tab) {
    return NS.api.get(ENDPOINTS[tab]).then(function (data) {
      return { ok: true, data: data };
    }).catch(function (err) {
      return { ok: false, error: err };
    });
  }

  function renderUnavailable(tab, err) {
    roots.body.innerHTML =
      '<div class="card admin-empty">' +
      '<div class="admin-empty-title">Endpoint not available yet</div>' +
      '<div class="hint">GET ' + NS.utils.esc(ENDPOINTS[tab]) + ' failed: ' + NS.utils.esc(err.message || err) + '</div>' +
      '<div class="hint">This view activates once the backend admin endpoints are wired (see <b>frontend_plan.md §6.2</b>: wire <code>ingestion/storage/api.py</code> deps, add <code>api/admin.py</code>).</div>' +
      '<button class="action" id="adminRetry">Retry</button></div>';
    var retry = roots.body.querySelector('#adminRetry');
    retry.addEventListener('click', renderTab);
  }

  function renderData(tab, data) {
    if (tab === 'sources') return renderSources(data);
    if (tab === 'jobs') return renderJobs(data);
    if (tab === 'stats') return renderStats(data);
    if (tab === 'tickets') return renderTickets(data);
  }

  function badge(status) {
    var cls = STATUS_BADGES[String(status).toUpperCase()] || 'badge-muted';
    return '<span class="badge ' + cls + '">' + NS.utils.esc(status == null ? '—' : status) + '</span>';
  }

  function table(headers, rows) {
    if (!rows || !rows.length) return '<div class="hint" style="padding:14px">No records.</div>';
    var thead = headers.map(function (h) { return '<th>' + NS.utils.esc(h) + '</th>'; }).join('');
    var tbody = rows.map(function (row) { return '<tr>' + row + '</tr>'; }).join('');
    return '<div class="tbl"><table><thead><tr>' + thead + '</tr></thead><tbody>' + tbody + '</tbody></table></div>';
  }

  function renderSources(data) {
    var rows = (Array.isArray(data) ? data : (data && data.sources) || []).map(function (s) {
      return '<td>' + NS.utils.esc(s.source_name || s.source_id || s.name || '—') + '</td>' +
        '<td>' + NS.utils.esc(s.source_type || '—') + '</td>' +
        '<td>' + NS.utils.esc(s.category_name || s.category || '—') + '</td>' +
        '<td>' + badge(s.version_status || s.status || s.current_version_status) + '</td>' +
        '<td>' + NS.utils.esc(NS.utils.formatDate(s.updated_at || s.created_at)) + '</td>' +
        '<td>' + (s.is_active === false ? 'no' : 'yes') + '</td>';
    });
    roots.body.innerHTML = table(['Name', 'Type', 'Category', 'Status', 'Updated', 'Active'], rows);
  }

  function renderJobs(data) {
    var rows = (Array.isArray(data) ? data : (data && data.jobs) || []).map(function (j) {
      return '<td>' + NS.utils.esc(j.job_type || '—') + '</td>' +
        '<td>' + badge(j.status) + '</td>' +
        '<td>' + NS.utils.esc(j.chunks_created_count == null ? '—' : j.chunks_created_count) + '</td>' +
        '<td>' + NS.utils.esc(j.entities_created_count == null ? '—' : j.entities_created_count) + '</td>' +
        '<td>' + NS.utils.esc(NS.utils.formatDate(j.started_at)) + '</td>' +
        '<td>' + NS.utils.esc(NS.utils.formatDate(j.completed_at)) + '</td>' +
        '<td>' + (j.error_details ? '<span class="err-detail" title="' + NS.utils.esc(j.error_details) + '">' + NS.utils.esc(String(j.error_details).slice(0, 60)) + '</span>' : '—') + '</td>';
    });
    roots.body.innerHTML = table(['Type', 'Status', 'Chunks', 'Entities', 'Started', 'Completed', 'Error'], rows);
  }

  function renderStats(data) {
    var html = '<div class="stats-grid">';
    if (data && data.entities) {
      html += statCard('Entities', data.entities.total || 0);
    }
    if (data && data.relations) {
      html += statCard('Relations', data.relations.total || 0);
    }
    if (data && data.sources) html += statCard('Knowledge sources', data.sources.total || 0);
    if (data && data.chunks) html += statCard('Chunks', data.chunks.total || 0);
    if (data && data.by_type && data.by_type.length) {
      html += '<div class="card"><h3>Entities by type</h3>' +
        data.by_type.map(function (r) {
          return '<div class="bar-row"><span>' + NS.utils.esc(r.entity_type || r.type) + '</span>' +
            '<div class="bar"><i style="width:' + Math.min(100, ((r.count || 0) / Math.max(1, data.entities.total)) * 100) + '%"></i></div>' +
            '<b>' + r.count + '</b></div>';
        }).join('') + '</div>';
    }
    html += '</div>';
    if (!data || !Object.keys(data).length) {
      html = '<div class="hint">No stats returned by the endpoint.</div>';
    }
    roots.body.innerHTML = html;
  }

  function statCard(label, value) {
    return '<div class="card stat-card"><div class="stat-num">' + NS.utils.esc(value) + '</div><div class="stat-label">' + NS.utils.esc(label) + '</div></div>';
  }

  function renderTickets(data) {
    var rows = (Array.isArray(data) ? data : (data && data.tickets) || []).map(function (t) {
      return '<td>' + NS.utils.esc(String(t.ticket_id || t.id || '').slice(0, 8)) + '</td>' +
        '<td>' + NS.utils.esc(t.email || '—') + '</td>' +
        '<td>' + NS.utils.esc(String(t.query || '').slice(0, 60)) + '</td>' +
        '<td>' + NS.utils.esc(t.priority || '—') + '</td>' +
        '<td>' + badge(t.status) + '</td>' +
        '<td>' + NS.utils.esc(NS.utils.formatDate(t.created_at)) + '</td>';
    });
    roots.body.innerHTML = table(['ID', 'Email', 'Query', 'Priority', 'Status', 'Created'], rows);
  }

  var STYLE = document.createElement('style');
  STYLE.textContent = [
    '.admin-wrap { flex: 1; overflow-y: auto; padding: 24px; max-width: 1120px; width: 100%; margin: 0 auto; }',
    '.admin-tabs { margin-bottom: 20px; }',
    '.admin-body { display: flex; flex-direction: column; gap: 16px; }',
    '.admin-empty { text-align: center; padding: 40px; }',
    '.admin-empty-title { font-size: 15px; font-weight: 600; margin-bottom: 8px; }',
    '.admin-empty .hint { margin-bottom: 6px; }',
    '.admin-empty .action { margin-top: 12px; }',
    '.tbl { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--r-lg); background: var(--panel); box-shadow: var(--shadow-sm); }',
    '.tbl table { width: 100%; border-collapse: collapse; font-size: 12.5px; }',
    '.tbl th { text-align: left; font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); padding: 12px 16px; border-bottom: 1px solid var(--border); white-space: nowrap; }',
    '.tbl td { padding: 11px 16px; border-bottom: 1px solid var(--border); white-space: nowrap; }',
    '.tbl tbody tr:last-child td { border-bottom: 0; }',
    '.tbl tbody tr:hover { background: var(--accent-soft); }',
    '.tbl td:last-child, .tbl th:last-child { white-space: normal; }',
    '.badge { display: inline-block; font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; font-weight: 700; padding: 2px 8px; border-radius: 999px; }',
    '.badge-ok { color: var(--on-accent); background: var(--accent); }',
    '.badge-accent { color: var(--on-accent); background: var(--accent); }',
    '.badge-warn { color: var(--on-accent); background: var(--accent-dim); }',
    '.badge-bad { color: var(--text); background: transparent; border: 1px solid var(--accent); }',
    '.badge-muted { color: var(--muted); background: var(--border); }',
    '.err-detail { color: var(--accent); cursor: help; }',
    '.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; }',
    '.stat-card { text-align: center; padding: 24px; }',
    '.stat-num { font-size: 28px; font-weight: 700; letter-spacing: -.02em; }',
    '.stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; margin-top: 6px; }',
    '.stats-grid .card h3 { margin: 0 0 10px; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }',
    '.stats-grid .card { grid-column: 1 / -1; }',
    '.bar-row { display: grid; grid-template-columns: 160px 1fr 40px; align-items: center; gap: 12px; font-size: 12px; padding: 6px 0; }',
    '.bar { background: var(--border); border-radius: 999px; height: 8px; overflow: hidden; }',
    '.bar i { display: block; height: 100%; background: var(--accent); border-radius: 999px; }'
  ].join('\n');
  document.head.appendChild(STYLE);

  NS.pages = NS.pages || {};
  NS.pages.admin = { init: init, destroy: destroy };
})(window.ACA);