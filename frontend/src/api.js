/* Shared API client — GET/POST/UPLOAD with timeout, error mapping, retry. */
(function (NS) {
  'use strict';

  var DEFAULT_TIMEOUT = 15000;

  function base() { return NS.config.apiBase; }

  function parseError(response, body) {
    var detail = body && (body.detail || body.message);
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map(function (d) { return d.msg || JSON.stringify(d); }).join('; ');
    return 'HTTP ' + response.status;
  }

  function request(method, path, options) {
    options = options || {};
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, options.timeout || DEFAULT_TIMEOUT) : null;
    var headers = options.headers || { Accept: 'application/json' };
    var body = options.body;

    if (options.json !== false && body !== undefined && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    var init = { method: method, headers: headers, body: body };
    if (controller) init.signal = controller.signal;

    return fetch(base() + path, init).then(function (response) {
      return response.text().then(function (text) {
        var parsed = null;
        try { parsed = text ? JSON.parse(text) : null; } catch (e) { parsed = null; }
        if (!response.ok) {
          var err = new Error(parseError(response, parsed));
          err.status = response.status;
          throw err;
        }
        return parsed;
      });
    }).then(function (value) {
      if (timer) clearTimeout(timer);
      return value;
    }, function (err) {
      if (timer) clearTimeout(timer);
      if (err && err.name === 'AbortError') { var e = new Error('Request timed out.'); e.timeout = true; throw e; }
      throw err;
    });
  }

  function retryable(fn) {
    var attempts = 0;
    function attempt() {
      return fn().catch(function (err) {
        if (err && (err.status === 502 || err.status === 503) && attempts < 2) {
          attempts++;
          return new Promise(function (resolve) { setTimeout(resolve, 400 * attempts); }).then(attempt);
        }
        throw err;
      });
    }
    return attempt();
  }

  NS.api = {
    get: function (path, params) {
      var q = '';
      if (params) {
        var parts = Object.keys(params).filter(function (k) { return params[k] != null && params[k] !== ''; })
          .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); });
        if (parts.length) q = '?' + parts.join('&');
      }
      return retryable(function () { return request('GET', path + q); });
    },
    post: function (path, body, options) {
      var opts = options || {};
      return retryable(function () { return request('POST', path, { body: body, timeout: opts.timeout }); });
    },
    upload: function (path, file, extraFields) {
      var fd = new FormData();
      fd.append('file', file);
      if (extraFields) {
        Object.keys(extraFields).forEach(function (k) { if (extraFields[k] != null) fd.append(k, extraFields[k]); });
      }
      return request('POST', path, { body: fd, json: false, timeout: 60000 });
    }
  };
})(window.ACA);