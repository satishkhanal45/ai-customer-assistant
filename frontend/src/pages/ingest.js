/* Document ingestion UI — upload (file), single-page crawl (PAGE), and site
   crawl (SITE: discover → review → confirm) against the /ingest/* API
   documented in crawler_frontend_integration.md. */
(function (NS) {
  'use strict';

  var ACCEPT_MIME = ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/markdown'];
  var CRAWL_TIMEOUT = 180000;

  var WAIT_STRATEGIES = [
    { value: 'fixed_timeout', label: 'Fixed timeout', hint: 'Captures right after the page loads. Fine for plain sites — may catch a loading shell on SPAs.' },
    { value: 'networkidle', label: 'Network idle', hint: 'Waits for the network to settle. Recommended for client-rendered (SPA) pages.' },
    { value: 'selector', label: 'CSS selector', hint: 'Waits for a CSS selector element to appear before capturing.' }
  ];

  var roots = {};
  var discovery = null; // { discovery_id, source, page_count, document_count, pages }

  function init(container) {
    container.innerHTML = '';
    container.style.minHeight = '0';

    var wrap = NS.utils.el('div', { class: 'ingest-wrap' });
    container.appendChild(wrap);

    wrap.innerHTML =
      '<h1 class="page-title">Knowledge Ingestion</h1>' +
      '<p class="page-sub">Add documents and web content to the AI Customer Assistant knowledge base.</p>' +

      '<div class="ingest-grid">' +

      '  <div class="ingest-card">' +
      '    <h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3M17 8l-5-5-5 5M12 3v12"/></svg> Upload File</h3>' +
      '    <p class="desc">Add documents directly from your device. PDF, DOCX or Markdown — chunked, embedded and extracted into the knowledge graph.</p>' +
      '    <div class="dropzone" id="dropzone">' +
      '      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3M17 8l-5-5-5 5M12 3v12"/></svg>' +
      '      <h4>Drag &amp; drop your file here</h4>' +
      '      <p>or <span class="browse">browse files</span></p>' +
      '    </div>' +
      '    <input type="file" id="fileInput" accept=".pdf,.docx,.md,application/pdf,text/markdown" hidden>' +
      '    <div class="formats-row">' +
      '      <span class="format-chip">PDF</span>' +
      '      <span class="format-chip">DOCX</span>' +
      '      <span class="format-chip">MD</span>' +
      '    </div>' +
      '    <div class="file-meta" id="fileMeta"></div>' +
      '    <button id="btnUpload" class="btn btn-primary btn-full" disabled>Upload &amp; ingest</button>' +
      '  </div>' +

      '  <div class="ingest-card">' +
      '    <h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20Z"/></svg> Crawl URL</h3>' +
      '    <p class="desc">Crawl a single page, or discover a whole site and review the pages before ingestion.</p>' +
      '    <div class="seg crawl-scope" id="scopeSeg">' +
      '      <button data-scope="page" class="on">Single page</button>' +
      '      <button data-scope="site">Whole site</button>' +
      '    </div>' +
      '    <div class="crawl-input-row">' +
      '      <input type="text" id="crawlUrl" placeholder="https://example.com/page">' +
      '      <button id="btnCrawl" class="btn btn-primary">Start Crawl</button>' +
      '    </div>' +
      '    <div class="wait-row">' +
      '      <select id="waitStrategy" class="gc-select" title="How long to wait for client-rendered content before capturing">' +
      '        <option value="fixed_timeout">Wait: fixed timeout</option>' +
      '        <option value="networkidle">Wait: network idle</option>' +
      '        <option value="selector">Wait: CSS selector</option>' +
      '      </select>' +
      '      <input type="text" id="waitSelector" placeholder="Selector, e.g. #app" style="display:none">' +
      '    </div>' +
      '    <div class="wait-hint" id="waitHint"></div>' +
      '    <div class="crawl-status" id="crawlStatus">' +
      '      <div class="label" id="crawlLabel">' +
      '        <svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>' +
      '        <span id="crawlLabelText">Working…</span>' +
      '      </div>' +
      '      <div class="crawl-stats" id="crawlStats" style="display:none;">' +
      '        <div class="crawl-stat"><div class="n" id="statChunks">0</div><div class="l">Chunks created</div></div>' +
      '        <div class="crawl-stat"><div class="n" id="statEntities">0</div><div class="l">Entities indexed</div></div>' +
      '      </div>' +
      '    </div>' +
      '    <div class="review-panel" id="reviewPanel" hidden></div>' +
      '  </div>' +
      '</div>' +
      '<div id="result" class="card result" hidden></div>';

    roots.dropzone = wrap.querySelector('#dropzone');
    roots.fileInput = wrap.querySelector('#fileInput');
    roots.fileMeta = wrap.querySelector('#fileMeta');
    roots.btnUpload = wrap.querySelector('#btnUpload');
    roots.url = wrap.querySelector('#crawlUrl');
    roots.btnCrawl = wrap.querySelector('#btnCrawl');
    roots.scopeSeg = wrap.querySelector('#scopeSeg');
    roots.waitStrategy = wrap.querySelector('#waitStrategy');
    roots.waitSelector = wrap.querySelector('#waitSelector');
    roots.waitHint = wrap.querySelector('#waitHint');
    roots.crawlStatus = wrap.querySelector('#crawlStatus');
    roots.crawlLabel = wrap.querySelector('#crawlLabel');
    roots.crawlStats = wrap.querySelector('#crawlStats');
    roots.statChunks = wrap.querySelector('#statChunks');
    roots.statEntities = wrap.querySelector('#statEntities');
    roots.reviewPanel = wrap.querySelector('#reviewPanel');
    roots.result = wrap.querySelector('#result');
    roots.selectedFile = null;
    roots.scope = 'page';

    updateWaitHint();

    roots.dropzone.addEventListener('click', function () { roots.fileInput.click(); });
    roots.dropzone.addEventListener('dragover', function (e) { e.preventDefault(); roots.dropzone.classList.add('drag'); });
    roots.dropzone.addEventListener('dragleave', function () { roots.dropzone.classList.remove('drag'); });
    roots.dropzone.addEventListener('drop', function (e) {
      e.preventDefault();
      roots.dropzone.classList.remove('drag');
      if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
    });
    roots.fileInput.addEventListener('change', function () {
      if (roots.fileInput.files.length) selectFile(roots.fileInput.files[0]);
    });
    roots.btnUpload.addEventListener('click', uploadFile);
    roots.btnCrawl.addEventListener('click', crawlUrl);

    Array.prototype.forEach.call(roots.scopeSeg.querySelectorAll('button'), function (b) {
      b.addEventListener('click', function () {
        roots.scope = b.getAttribute('data-scope');
        Array.prototype.forEach.call(roots.scopeSeg.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === b); });
        roots.btnCrawl.textContent = roots.scope === 'site' ? 'Discover' : 'Start Crawl';
        roots.url.placeholder = roots.scope === 'site' ? 'https://example.com' : 'https://example.com/page';
        hideReview();
        hideCrawlStatus();
      });
    });

    roots.waitStrategy.addEventListener('change', updateWaitHint);
    roots.waitSelector.addEventListener('input', updateWaitHint);
  }

  function destroy() { roots = {}; discovery = null; }

  function updateWaitHint() {
    var s = roots.waitStrategy.value;
    var opt = null;
    for (var i = 0; i < WAIT_STRATEGIES.length; i++) if (WAIT_STRATEGIES[i].value === s) opt = WAIT_STRATEGIES[i];
    roots.waitSelector.style.display = s === 'selector' ? '' : 'none';
    if (roots.waitHint) roots.waitHint.textContent = opt ? opt.hint : '';
  }

  function waitPayload() {
    var payload = { wait_strategy: roots.waitStrategy.value };
    if (payload.wait_strategy === 'selector') {
      var sel = roots.waitSelector.value.trim();
      if (!sel) { NS.utils.status('Enter a CSS selector to wait for.', true); return null; }
      payload.wait_selector = sel;
    } else {
      payload.wait_selector = null;
    }
    return payload;
  }

  function crawlUrl() {
    var url = roots.url.value.trim();
    if (!/^https?:\/\/.+/i.test(url)) { NS.utils.status('Enter a valid http(s) URL.', true); return; }
    if (roots.scope === 'site') discoverSite(url);
    else crawlPage(url);
  }

  function crawlPage(url) {
    var wp = waitPayload();
    if (!wp) return;
    hideReview();
    hideCrawlStatus();
    setBusy(true, roots.btnCrawl, 'Start Crawl');
    NS.api.post('/ingest/crawl', {
      url: url,
      scope: 'PAGE',
      wait_strategy: wp.wait_strategy,
      wait_selector: wp.wait_selector
    }, { timeout: CRAWL_TIMEOUT }).then(function (data) {
      if (data.status === 'duplicate_skipped') {
        renderResult(data);
        NS.utils.status('Duplicate skipped.');
        setBusy(false, roots.btnCrawl, 'Start Crawl');
        return;
      }
      if (data.status !== 'submitted') {
        renderResult(data);
        setBusy(false, roots.btnCrawl, 'Start Crawl');
        return;
      }
      showCrawlLabel('Submitted — ingesting in background…', false);
      NS.utils.status('Crawl submitted — ingesting in background…');
      pollJob(data.job_id).then(function (result) {
        setBusy(false, roots.btnCrawl, 'Start Crawl');
        if (result.status === 'succeeded') {
          renderResult({ status: 'ok', job_id: data.job_id, source_id: data.source_id, version_id: data.version_id, chunks_created_count: result.chunks, entities_created_count: result.entities });
          showCrawlDone(result.chunks, result.entities);
          NS.utils.status('Ingestion complete.');
        } else if (result.status === 'error') {
          renderResult({ status: 'error', error: result.error });
          showCrawlLabel('Crawl failed', true);
          NS.utils.status('Ingestion failed', true);
        } else {
          renderResult({ status: 'pending', job_id: data.job_id });
          showCrawlLabel('Still processing in the background.', false);
          NS.utils.status('Still processing in the background.');
        }
      });
    }).catch(function (err) {
      renderResult({ status: 'error', error: err.message });
      showCrawlLabel('Crawl failed', true);
      NS.utils.status('Crawl failed', true);
      setBusy(false, roots.btnCrawl, 'Start Crawl');
    });
  }

  function discoverSite(rootUrl) {
    var wp = waitPayload();
    if (!wp) return;
    hideReview();
    hideCrawlStatus();
    renderResult(null);
    setBusy(true, roots.btnCrawl, 'Discovering…');
    NS.api.post('/ingest/crawl/discover', {
      root_url: rootUrl,
      wait_strategy: wp.wait_strategy,
      wait_selector: wp.wait_selector
    }, { timeout: CRAWL_TIMEOUT }).then(function (data) {
      setBusy(false, roots.btnCrawl, 'Discover');
      discovery = data;
      renderReview(data);
    }).catch(function (err) {
      setBusy(false, roots.btnCrawl, 'Discover');
      renderResult({ status: 'error', error: err.message });
      NS.utils.status('Discovery failed', true);
    });
  }

  function renderReview(d) {
    var panel = roots.reviewPanel;
    var pages = d.pages || [];
    var total = (d.page_count || 0) + (d.document_count || 0);
    var sourceBadge = d.source === 'SITEMAP'
      ? '<span class="source-badge sitemap">SITEMAP</span>'
      : '<span class="source-badge bfs">BFS FALLBACK</span>';

    var list = pages.map(function (p) {
      var kind = p.kind === 'DOCUMENT'
        ? '<span class="kind-badge doc">DOC</span>'
        : '<span class="kind-badge page">PAGE</span>';
      var ft = p.file_type ? '<span class="file-type">' + NS.utils.esc(p.file_type) + '</span>' : '';
      return '<label class="review-item" title="' + NS.utils.esc(p.url) + '">' +
        '<input type="checkbox" checked>' +
        kind + '<span class="url">' + NS.utils.esc(p.url) + '</span>' + ft +
        '</label>';
    }).join('');

    panel.hidden = false;
    panel.innerHTML =
      '<div class="review-head">' +
      '<div class="review-title">Site discovery' + sourceBadge + '</div>' +
      '<div class="review-counts">' +
      '<span><b>' + NS.utils.esc(d.page_count || 0) + '</b> pages</span>' +
      '<span><b>' + NS.utils.esc(d.document_count || 0) + '</b> docs</span>' +
      '</div>' +
      '</div>' +
      '<div class="review-list">' + (list || '<div class="hint" style="padding:12px 14px">No pages discovered.</div>') + '</div>' +
      '<div class="review-actions">' +
      '<button id="btnConfirm" class="btn btn-primary">Confirm &amp; ingest (' + total + ')</button>' +
      '<button id="btnRediscover" class="btn btn-ghost">Re-discover</button>' +
      '<span class="review-note">All discovered items are ingested together on confirm.</span>' +
      '</div>';

    var confirm = panel.querySelector('#btnConfirm');
    confirm.disabled = total === 0;
    confirm.addEventListener('click', confirmSite);
    panel.querySelector('#btnRediscover').addEventListener('click', function () {
      discoverSite(roots.url.value.trim());
    });
  }

  function confirmSite() {
    if (!discovery || !discovery.discovery_id) return;
    var confirmBtn = roots.reviewPanel.querySelector('#btnConfirm');
    setBusy(true, confirmBtn, 'Confirm & ingest');
    NS.api.post('/ingest/crawl/' + discovery.discovery_id + '/confirm', { category_id: null }, { timeout: CRAWL_TIMEOUT }).then(function (data) {
      if (data.status !== 'submitted') {
        setBusy(false, confirmBtn, 'Confirm & ingest');
        renderResult(data);
        return;
      }
      var items = data.results || [];
      var jobs = items.filter(function (r) { return r.status === 'submitted'; });
      var failed = items.filter(function (r) { return r.status === 'failed'; });

      if (failed.length) {
        renderResult({ status: 'error', error: failed[0].error || failed.length + ' item(s) failed to crawl.' });
      }
      if (!jobs.length) {
        setBusy(false, confirmBtn, 'Confirm & ingest');
        if (!failed.length) renderResult({ status: 'pending' });
        return;
      }
      NS.utils.status('Ingesting ' + jobs.length + ' item(s)…');
      Promise.all(jobs.map(function (j) { return pollJob(j.job_id); })).then(function (results) {
        setBusy(false, confirmBtn, 'Confirm & ingest');
        var ok = results.filter(function (r) { return r.status === 'succeeded'; }).length;
        var errs = results.filter(function (r) { return r.status === 'error'; });
        var pending = results.filter(function (r) { return r.status === 'pending'; }).length;
        if (errs.length) {
          renderResult({ status: 'error', error: ok + ' succeeded, ' + errs.length + ' failed. ' + (errs[0].error || '') });
          NS.utils.status('Some items failed', true);
        } else if (pending) {
          renderResult({ status: 'pending' });
          NS.utils.status('Still processing in the background.');
        } else {
          renderResult({
            status: 'ok',
            chunks_created_count: results.reduce(function (a, r) { return a + r.chunks; }, 0),
            entities_created_count: results.reduce(function (a, r) { return a + r.entities; }, 0)
          });
          NS.utils.status('Ingestion complete (' + ok + ' items).');
        }
      });
    }).catch(function (err) {
      setBusy(false, confirmBtn, 'Confirm & ingest');
      if (err.status === 404) {
        NS.utils.status('Discovery expired — re-running discovery…', true);
        renderResult({ status: 'error', error: 'Discovery cache expired (10 min). Re-running discovery…' });
        discoverSite(roots.url.value.trim());
      } else {
        renderResult({ status: 'error', error: err.message });
        NS.utils.status('Confirm failed', true);
      }
    });
  }

  function pollJob(jobId) {
    return new Promise(function (resolve) {
      var tries = 0;
      var MAX_TRIES = 150; // 2s * 150 = 5 min
      var timer = setInterval(function () {
        NS.api.get('/ingest/jobs/' + jobId).then(function (st) {
          if (st.status === 'SUCCEEDED') {
            clearInterval(timer);
            resolve({ status: 'succeeded', chunks: st.chunks_created_count || 0, entities: st.entities_created_count || 0 });
          } else if (st.status === 'FAILED') {
            clearInterval(timer);
            resolve({ status: 'error', error: st.error_details || 'Ingestion failed.' });
          } else if (++tries >= MAX_TRIES) {
            clearInterval(timer);
            resolve({ status: 'pending' });
          }
        }).catch(function () {});
      }, 2000);
    });
  }

  function setBusy(busy, btn, doneLabel) {
    if (!btn) return;
    btn.disabled = busy;
    btn.textContent = busy ? 'Working…' : (doneLabel || 'Ingest');
    if (busy) showLoading();
    else hideLoading();
  }

  function showLoading() {
    var l = document.getElementById('loading');
    if (l) l.classList.add('show');
  }
  function hideLoading() {
    var l = document.getElementById('loading');
    if (l) l.classList.remove('show');
  }

  function selectFile(file) {
    var ok = ACCEPT_MIME.indexOf(file.type) >= 0 ||
      /\.(pdf|docx|md)$/i.test(file.name);
    if (!ok) {
      roots.fileMeta.innerHTML = '<span class="err">Unsupported type: ' + NS.utils.esc(file.type || file.name) + ' (expected PDF, DOCX or Markdown).</span>';
      roots.btnUpload.disabled = true;
      roots.selectedFile = null;
      return;
    }
    roots.selectedFile = file;
    roots.fileMeta.innerHTML = '<span>' + NS.utils.esc(file.name) + ' · ' + NS.utils.esc(NS.utils.formatBytes(file.size)) + '</span>';
    roots.btnUpload.disabled = false;
  }

  function uploadFile() {
    if (!roots.selectedFile) return;
    setBusy(true, roots.btnUpload, 'Upload & ingest');
    NS.api.upload('/ingest/upload', roots.selectedFile).then(function (data) {
      if (data.status === 'duplicate_skipped') {
        renderResult(data);
        NS.utils.status('Duplicate skipped.');
        setBusy(false, roots.btnUpload, 'Upload & ingest');
        return;
      }
      if (data.status !== 'submitted') {
        renderResult(data);
        setBusy(false, roots.btnUpload, 'Upload & ingest');
        return;
      }
      NS.utils.status('Uploaded — ingesting in background…');
      pollJob(data.job_id).then(function (result) {
        setBusy(false, roots.btnUpload, 'Upload & ingest');
        if (result.status === 'succeeded') {
          renderResult({ status: 'ok', job_id: data.job_id, source_id: data.source_id, version_id: data.version_id, chunks_created_count: result.chunks, entities_created_count: result.entities });
          NS.utils.status('Ingestion complete.');
        } else if (result.status === 'error') {
          renderResult({ status: 'error', error: result.error });
          NS.utils.status('Ingestion failed', true);
        } else {
          renderResult({ status: 'pending', job_id: data.job_id });
          NS.utils.status('Still processing in the background.');
        }
      });
    }).catch(function (err) {
      renderResult({ status: 'error', error: err.message });
      NS.utils.status('Upload failed', true);
      setBusy(false, roots.btnUpload, 'Upload & ingest');
    });
  }

  function hideCrawlStatus() {
    if (roots.crawlStatus) roots.crawlStatus.classList.remove('show');
  }

  function showCrawlLabel(text, error) {
    if (!roots.crawlLabel) return;
    roots.crawlStatus.classList.add('show');
    roots.crawlStats.style.display = 'none';
    roots.crawlLabel.classList.toggle('done', !error);
    roots.crawlLabel.innerHTML = (error ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>' :
      '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>') +
      '<span>' + NS.utils.esc(text) + '</span>';
  }

  function showCrawlDone(chunks, entities) {
    if (roots.crawlLabel) {
      roots.crawlLabel.classList.add('done');
      roots.crawlLabel.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 12 2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg><span>Crawl completed</span>';
    }
    if (roots.crawlStats) {
      roots.statChunks.textContent = chunks == null ? '0' : chunks;
      roots.statEntities.textContent = entities == null ? '0' : entities;
      roots.crawlStats.style.display = 'grid';
    }
  }

  function hideReview() {
    discovery = null;
    if (roots.reviewPanel) roots.reviewPanel.hidden = true;
  }

  function renderResult(data) {
    if (!data || typeof data !== 'object' || !data.status) {
      roots.result.hidden = true;
      return;
    }
    roots.result.hidden = false;
    var html;
    if (data.status === 'duplicate_skipped') {
      html = '<div class="result-ok"><b>Duplicate detected</b> — this content was already ingested, nothing indexed.</div>';
    } else if (data.status === 'error') {
      html = '<div class="result-ok err"><b>Request failed.</b> ' + NS.utils.esc(data.error || 'Unknown error.') + '</div>';
    } else if (data.status === 'ok') {
      html = '<div class="result-ok"><b>Ingestion complete.</b></div>' +
        '<div class="hint">' + NS.utils.esc(String(data.chunks_created_count || 0)) + ' chunks, ' +
        NS.utils.esc(String(data.entities_created_count || 0)) + ' entities indexed.</div>' +
        (data.job_id ? '<div class="result-actions">' +
          '<button class="btn btn-ghost btn-sm" data-copy="job_id">Copy job id</button>' +
          (data.source_id ? '<button class="btn btn-ghost btn-sm" data-copy="source_id">Copy source id</button>' : '') +
          (data.version_id ? '<button class="btn btn-ghost btn-sm" data-copy="version_id">Copy version id</button>' : '') +
          '</div>' : '');
    } else if (data.status === 'pending') {
      html = '<div class="result-ok"><b>Submitted for ingestion.</b></div>' +
        '<div class="hint">Still processing in the background (embeddings + graph extraction). Check the admin tab or retry in a moment.</div>' +
        (data.job_id ? '<div class="result-actions"><button class="btn btn-ghost btn-sm" data-copy="job_id">Copy job id</button></div>' : '');
    } else if (data.status === 'submitted') {
      html = '<div class="result-ok"><b>Submitted for ingestion.</b></div>' +
        '<pre class="code">' + NS.utils.esc(JSON.stringify({
          status: data.status,
          job_id: data.job_id,
          source_id: data.source_id,
          version_id: data.version_id
        }, null, 2)) + '</pre>' +
        '<div class="result-actions"><button class="btn btn-ghost btn-sm" data-copy="job_id">Copy job id</button>' +
        '<button class="btn btn-ghost btn-sm" data-copy="source_id">Copy source id</button>' +
        '<button class="btn btn-ghost btn-sm" data-copy="version_id">Copy version id</button></div>';
    } else {
      html = '<pre class="code">' + NS.utils.esc(JSON.stringify(data, null, 2)) + '</pre>';
    }
    roots.result.innerHTML = html;
    Array.prototype.forEach.call(roots.result.querySelectorAll('[data-copy]'), function (b) {
      b.addEventListener('click', function () {
        var key = b.getAttribute('data-copy');
        copyText(String(data[key] || ''));
        NS.utils.status('Copied ' + key + '.');
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).catch(function () {}); }
    else { var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  }

  NS.pages = NS.pages || {};
  NS.pages.ingest = { init: init, destroy: destroy };
})(window.ACA);
