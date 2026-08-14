/* Document ingestion UI — POST /ingest/upload (file) and POST /ingest/crawl (URL). */
(function (NS) {
  'use strict';

  var ACCEPT_MIME = ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/markdown'];
  var roots = {};

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
      '    <p class="desc">Extract knowledge from a public webpage, PDF or Office document and ingest it.</p>' +
      '    <div class="crawl-input-row">' +
      '      <input type="text" id="crawlUrl" placeholder="https://example.com/page">' +
      '      <button id="btnCrawl" class="btn btn-primary">Start Crawl</button>' +
      '    </div>' +
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
      '  </div>' +
      '</div>' +
      '<div id="result" class="card result" hidden></div>';

    roots.dropzone = wrap.querySelector('#dropzone');
    roots.fileInput = wrap.querySelector('#fileInput');
    roots.fileMeta = wrap.querySelector('#fileMeta');
    roots.btnUpload = wrap.querySelector('#btnUpload');
    roots.crawlUrl = wrap.querySelector('#crawlUrl');
    roots.btnCrawl = wrap.querySelector('#btnCrawl');
    roots.result = wrap.querySelector('#result');
    roots.selectedFile = null;

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
  }

  function destroy() { roots = {}; }

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

  function setBusy(busy, btn) {
    btn.disabled = busy;
    btn.textContent = busy ? 'Working…' : 'Ingest';
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

  function renderResult(data) {
    roots.result.hidden = false;
    if (!data || typeof data !== 'object') {
      roots.result.innerHTML = '<div class="hint">Unexpected response.</div>';
      return;
    }
    var html;
    if (data.status === 'duplicate_skipped') {
      html = '<div class="result-ok"><b>Duplicate detected</b> — this content was already ingested, nothing indexed.</div>';
    } else if (data.status === 'ok') {
      html = '<div class="result-ok"><b>Ingestion complete.</b></div>' +
        '<div class="hint">' + NS.utils.esc(String(data.chunks_created_count || 0)) + ' chunks, ' +
        NS.utils.esc(String(data.entities_created_count || 0)) + ' entities indexed.</div>' +
        '<div class="result-actions"><button class="btn btn-ghost btn-sm" data-copy="job_id">Copy job id</button></div>';
    } else if (data.status === 'pending') {
      html = '<div class="result-ok"><b>Submitted for ingestion.</b></div>' +
        '<div class="hint">Still processing in the background (embeddings + graph extraction). Check the admin tab or retry in a moment.</div>' +
        '<div class="result-actions"><button class="btn btn-ghost btn-sm" data-copy="job_id">Copy job id</button></div>';
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

  function uploadFile() {
    if (!roots.selectedFile) return;
    setBusy(true, roots.btnUpload);
    NS.api.upload('/ingest/upload', roots.selectedFile).then(function (data) {
      if (data.status === 'duplicate_skipped') {
        renderResult(data);
        NS.utils.status('Duplicate skipped.');
        setBusy(false, roots.btnUpload);
        return;
      }
      NS.utils.status('Uploaded — ingesting in background…');
      pollJob(data.job_id, function (result) {
        setBusy(false, roots.btnUpload);
        if (result.status === 'succeeded') {
          renderResult({
            status: 'ok',
            job_id: data.job_id,
            chunks_created_count: result.chunks,
            entities_created_count: result.entities
          });
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
      setBusy(false, roots.btnUpload);
    });
  }

  function pollJob(jobId, onDone) {
    var tries = 0;
    var MAX_TRIES = 150; // 2s * 150 = 5 min
    var timer = setInterval(function () {
      NS.api.get('/ingest/jobs/' + jobId).then(function (st) {
        if (st.status === 'SUCCEEDED') {
          clearInterval(timer);
          onDone({ status: 'succeeded', chunks: st.chunks_created_count, entities: st.entities_created_count });
        } else if (st.status === 'FAILED') {
          clearInterval(timer);
          onDone({ status: 'error', error: st.error_details || 'Ingestion failed.' });
        } else if (++tries >= MAX_TRIES) {
          clearInterval(timer);
          onDone({ status: 'pending', jobId: jobId });
        }
      }).catch(function () {});
    }, 2000);
  }

  function handleSubmit(payload, submitFn) {
    setBusy(true, roots.btnCrawl);
    submitFn().then(function (data) {
      if (data.status === 'duplicate_skipped') {
        renderResult(data);
        NS.utils.status('Duplicate skipped.');
        setBusy(false, roots.btnCrawl);
        return;
      }
      showCrawlLabel('Submitted — ingesting in background…', false);
      pollJob(data.job_id, function (result) {
        setBusy(false, roots.btnCrawl);
        if (result.status === 'succeeded') {
          renderResult({
            status: 'ok',
            job_id: data.job_id,
            chunks_created_count: result.chunks,
            entities_created_count: result.entities
          });
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
      setBusy(false, roots.btnCrawl);
    });
  }

  function showCrawlLabel(text, error) {
    var label = document.getElementById('crawlLabel');
    var wrap = document.getElementById('crawlStatus');
    if (!label) return;
    wrap.classList.add('show');
    document.getElementById('crawlStats').style.display = 'none';
    label.classList.toggle('done', !error);
    label.innerHTML = (error ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>' :
      '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>') +
      '<span>' + NS.utils.esc(text) + '</span>';
  }

  function showCrawlDone(chunks, entities) {
    var label = document.getElementById('crawlLabel');
    if (label) {
      label.classList.add('done');
      label.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 12 2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg><span>Crawl completed</span>';
    }
    var stats = document.getElementById('crawlStats');
    if (stats) {
      document.getElementById('statChunks').textContent = chunks == null ? '0' : chunks;
      document.getElementById('statEntities').textContent = entities == null ? '0' : entities;
      stats.style.display = 'grid';
    }
  }

  function crawlUrl() {
    var url = roots.crawlUrl.value.trim();
    if (!/^https?:\/\/.+/i.test(url)) { NS.utils.status('Enter a valid http(s) URL.', true); return; }
    handleSubmit(url, function () {
      return NS.api.post('/ingest/crawl', { url: url }, { timeout: 180000 });
    });
  }

  NS.pages = NS.pages || {};
  NS.pages.ingest = { init: init, destroy: destroy };
})(window.ACA);
