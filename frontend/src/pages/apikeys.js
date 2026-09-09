/* API keys — one row per LLM provider.

   The server never sends a key back. What it sends is whether a provider is
   configured, where that configuration came from (a key saved here, or an
   environment variable that was always set), and the last four characters
   of a saved key. So this page can show you *which* key is in place and can
   never show you the key — including to whoever is looking over your
   shoulder, and including in a screenshot.

   `source` is the field that earns its place. A saved key shadows the
   environment variable, so without showing which one is in effect,
   "I changed the key and nothing happened" is an unanswerable question.
*/
(function (NS) {
  'use strict';

  var roots = {};
  var state = { providers: [], default: null, busy: false };

  function init(container) {
    container.innerHTML =
      '<h1 class="page-title">API Keys</h1>' +
      '<p class="page-sub">Credentials for the language-model providers. Keys are encrypted before they are stored and are never sent back to the browser.</p>' +
      '<div id="providerList" class="provider-list"></div>' +
      '<p class="hint provider-foot">A key saved here overrides the matching environment variable. Clearing one falls back to the environment rather than switching the provider off.</p>';

    roots.list = container.querySelector('#providerList');
    load();
  }

  function destroy() { roots = {}; state = { providers: [], default: null, busy: false }; }

  function load() {
    roots.list.innerHTML = '<div class="hint" style="padding:16px">Loading…</div>';
    NS.api.get('/admin/llm-providers').then(function (data) {
      state.providers = (data && data.providers) || [];
      state.default = data && data.default;
      render();
    }).catch(function (err) {
      roots.list.innerHTML = '<div class="card admin-empty">' +
        '<div class="admin-empty-title">' +
        (err && err.status === 403 ? 'Administrator access required' : 'Could not load providers') +
        '</div><div class="hint">' + NS.utils.esc(err.message || err) + '</div></div>';
    });
  }

  function statusCell(p) {
    if (p.source === 'saved') {
      return '<span class="badge badge-ok">Saved</span>' +
        '<span class="key-mask">••••••••' + NS.utils.esc(p.last4 || '') + '</span>';
    }
    if (p.source === 'environment') {
      /* Worth distinguishing loudly: this key works, but it is not editable
         from here — it lives in the process environment. */
      return '<span class="badge badge-muted">From environment</span>' +
        '<code class="key-env">' + NS.utils.esc(p.env_var) + '</code>';
    }
    return '<span class="badge badge-warn">Not configured</span>';
  }

  function row(p) {
    var isDefault = p.name === state.default;
    return '<div class="provider-row' + (isDefault ? ' is-default' : '') + '" data-provider="' + NS.utils.esc(p.name) + '">' +
      '<div class="provider-head">' +
      '<div class="provider-name">' + NS.utils.esc(p.label) +
      (isDefault ? '<span class="badge badge-accent">Default</span>' : '') + '</div>' +
      '<div class="provider-status">' + statusCell(p) + '</div>' +
      '</div>' +

      '<div class="provider-controls">' +
      '<input type="password" class="provider-input" placeholder="Paste a new key…"' +
      ' autocomplete="off" spellcheck="false" data-role="key">' +
      '<button class="btn btn-primary btn-sm" data-role="save">Save</button>' +
      (p.source === 'saved'
        ? '<button class="btn btn-ghost btn-sm" data-role="clear">Clear</button>'
        : '') +
      (isDefault
        ? ''
        : '<button class="btn btn-ghost btn-sm" data-role="default">Make default</button>') +
      '</div>' +

      '<div class="provider-meta">' +
      (p.docs_url ? '<a href="' + NS.utils.esc(p.docs_url) + '" target="_blank" rel="noopener">Get a key ↗</a>' : '') +
      (p.updated_at ? '<span>Updated ' + NS.utils.esc(NS.utils.formatDate(p.updated_at)) + '</span>' : '') +
      '</div>' +
      '</div>';
  }

  function render() {
    roots.list.innerHTML = state.providers.map(row).join('');
    Array.prototype.forEach.call(roots.list.querySelectorAll('.provider-row'), function (el) {
      var name = el.getAttribute('data-provider');
      var input = el.querySelector('[data-role="key"]');
      wire(el, 'save', function () { save(name, input); });
      wire(el, 'clear', function () { clear(name); });
      wire(el, 'default', function () { makeDefault(name); });
      if (input) {
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') { e.preventDefault(); save(name, input); }
        });
      }
    });
  }

  function wire(el, role, fn) {
    var btn = el.querySelector('[data-role="' + role + '"]');
    if (btn) btn.addEventListener('click', fn);
  }

  function guard(promise, okMessage) {
    if (state.busy) return;
    state.busy = true;
    return promise.then(function () {
      NS.utils.status(okMessage);
      load();
    }).catch(function (err) {
      NS.utils.status(err.message || 'Request failed.', true);
    }).then(function () { state.busy = false; });
  }

  function save(name, input) {
    var key = (input && input.value || '').trim();
    if (!key) { NS.utils.status('Paste a key first.', true); return; }
    /* Cleared immediately, whatever happens next: a secret should not sit
       in the DOM waiting for a response that may take a moment. */
    input.value = '';
    guard(NS.api.put('/admin/llm-providers/' + encodeURIComponent(name), { api_key: key }),
      'Key saved.');
  }

  function clear(name) {
    guard(NS.api.del('/admin/llm-providers/' + encodeURIComponent(name)),
      'Key cleared — falling back to the environment.');
  }

  function makeDefault(name) {
    guard(NS.api.post('/admin/llm-providers/' + encodeURIComponent(name) + '/default', {}),
      'Default provider updated.');
  }

  NS.pages = NS.pages || {};
  NS.pages.apikeys = { init: init, destroy: destroy };
})(window.ACA);
