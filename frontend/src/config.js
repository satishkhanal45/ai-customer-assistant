/* App configuration — resolves the API base URL. Loaded after utils.js. */
(function (NS) {
  'use strict';

  var LS_KEY = 'aca.apiBase';

  function defaultApiBase() {
    // When served from the backend (same origin), use it. When opened as a
    // local file, fall back to the dev backend.
    if (window.__APP_CONFIG__ && window.__APP_CONFIG__.apiBase) return window.__APP_CONFIG__.apiBase;
    if (location.protocol === 'file:') return 'http://127.0.0.1:8000';
    return location.origin;
  }

  NS.config = {
    apiBase: (function () {
      try { return localStorage.getItem(LS_KEY) || defaultApiBase(); }
      catch (e) { return defaultApiBase(); }
    })(),
    setApiBase: function (value) {
      var v = (value || '').replace(/\/+$/, '');
      NS.config.apiBase = v;
      try { localStorage.setItem(LS_KEY, v); } catch (e) {}
    },
    engines: {
      forceGraph: 'https://unpkg.com/force-graph/dist/force-graph.min.js',
      three: 'https://unpkg.com/three@0.128.0/build/three.min.js',
      forceGraph3d: 'https://unpkg.com/3d-force-graph@1.71.2/dist/3d-force-graph.min.js'
    }
  };
})(window.ACA);