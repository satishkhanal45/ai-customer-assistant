/* Document ingestion UI — POST /ingest/upload (file) and POST /ingest/crawl (URL). */
(function (NS) {
  'use strict';

  var ACCEPT_MIME = ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/markdown'];
  var roots = {};

  function init(container) {
    container.innerHTML = '';
    container.style.display = 'flex';
    container.style.minHeight = '0';

    var wrap = NS.utils.el('div', { class: 'ingest-wrap' });
    container.appendChild(wrap);

    wrap.innerHTML =
      '<div class="ingest-grid">' +
      '  <div class="card">' +
      '    <h2>Upload file</h2>' +
      '    <p class="hint">PDF, DOCX or Markdown. The file is chunked, embedded and extracted into the knowledge graph.</p>' +
      '    <div id="dropzone" class="dropzone"><div class="dz-label">Drop a file here, or click to browse</div></div>' +
      '    <input type="file" id="fileInput" accept=".pdf,.docx,.md,application/pdf,text/markdown" hidden>' +
      '    <div id="fileMeta" class="file-meta"></div>' +
      '    <button id="btnUpload" class="action primary" disabled>Upload &amp; ingest</button>' +
      '  </div>' +
      '  <div class="card">' +
      '    <h2>Crawl URL</h2>' +
      '    <p class="hint">Fetch an HTML page, PDF or Office document from a public URL and ingest it.</p>' +
      '    <div class="field"><input id="crawlUrl" class="text" type="text" placeholder="https://example.com/page" style="width:100%"></div>' +
      '    <button id="btnCrawl" class="action primary">Crawl &amp; ingest</button>' +
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
    roots.btnUpload.dataset.label = 'Upload & ingest';
    roots.btnCrawl.dataset.label = 'Crawl & ingest';
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
    btn.textContent = busy ? 'Working…' : btn.getAttribute('data-label');
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
        '<div class="result-actions"><button class="action" data-copy="job_id">Copy job id</button></div>';
    } else if (data.status === 'pending') {
      html = '<div class="result-ok"><b>Submitted for ingestion.</b></div>' +
        '<div class="hint">Still processing in the background (embeddings + graph extraction). Check the admin tab or retry in a moment.</div>' +
        '<div class="result-actions"><button class="action" data-copy="job_id">Copy job id</button></div>';
    } else if (data.status === 'submitted') {
      html = '<div class="result-ok"><b>Submitted for ingestion.</b></div>' +
        '<pre class="code">' + NS.utils.esc(JSON.stringify({
          status: data.status,
          job_id: data.job_id,
          source_id: data.source_id,
          version_id: data.version_id
        }, null, 2)) + '</pre>' +
        '<div class="result-actions"><button class="action" data-copy="job_id">Copy job id</button>' +
        '<button class="action" data-copy="source_id">Copy source id</button>' +
        '<button class="action" data-copy="version_id">Copy version id</button></div>';
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
      NS.utils.status('Submitted — ingesting in background…');
      pollJob(data.job_id, function (result) {
        setBusy(false, roots.btnCrawl);
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
      NS.utils.status('Crawl failed', true);
      setBusy(false, roots.btnCrawl);
    });
  }

  function crawlUrl() {
    var url = roots.crawlUrl.value.trim();
    if (!/^https?:\/\/.+/i.test(url)) { NS.utils.status('Enter a valid http(s) URL.', true); return; }
    handleSubmit(url, function () {
      return NS.api.post('/ingest/crawl', { url: url }, { timeout: 180000 });
    });
  }

  var STYLE = document.createElement('style');
  STYLE.textContent = [
    '.ingest-wrap { flex: 1; overflow-y: auto; padding: 24px; max-width: 1020px; width: 100%; margin: 0 auto; }',
    '.ingest-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }',
    '.ingest-wrap h2 { margin: 0 0 6px; font-size: 14px; }',
    '.ingest-wrap .hint { margin: 0; }',
    '.dropzone { border: 1.5px dashed var(--border); border-radius: var(--r-lg); padding: 40px 24px; text-align: center; cursor: pointer; color: var(--muted); font-size: 13px; transition: all .15s ease; margin: 16px 0 12px; }',
    '.dropzone:hover, .dropzone.drag { border-color: var(--accent); background: var(--accent-soft); color: var(--text); }',
    '.file-meta { font-size: 12px; min-height: 20px; margin-bottom: 12px; }',
    '.file-meta .err { color: var(--accent); font-weight: 600; }',
    '.result { margin-top: 20px; } .result[hidden] { display: none; }',
    '.result-ok { color: var(--accent); font-size: 13px; margin-bottom: 10px; font-weight: 600; }',
    '.result-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }',
    '.result-actions .action { font-size: 11.5px; height: 30px; padding: 0 12px; }',
    '@media (max-width: 1080px) { .ingest-grid { grid-template-columns: 1fr; } }'
  ].join('\n');
  document.head.appendChild(STYLE);

  NS.pages = NS.pages || {};
  NS.pages.ingest = { init: init, destroy: destroy };
})(window.ACA);