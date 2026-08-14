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

  NS.pages = NS.pages || {};
  NS.pages.admin = { init: init, destroy: destroy };
})(window.ACA);