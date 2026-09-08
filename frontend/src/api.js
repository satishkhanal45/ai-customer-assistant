/* Shared API client — GET/POST/UPLOAD with timeout, error mapping, retry,
   and transparent re-authentication.

   Credentials are cookies the browser sets for itself: the access and
   refresh tokens are HttpOnly, so nothing in this file ever sees a token.
   `credentials: 'include'` is what attaches them; without it fetch omits
   cookies and every call 401s.

   The access token lasts fifteen minutes and a chat turn can take a minute,
   so expiry mid-session is normal rather than exceptional. A 401 therefore
   triggers one refresh and one replay before it is treated as "signed out".
*/
(function (NS) {
  'use strict';

  var DEFAULT_TIMEOUT = 15000;

  function base() { return NS.config.apiBase; }

  /* Endpoints that must never trigger the refresh-and-replay path: a 401
     from /auth/refresh is the definition of "the session is over", and
     retrying it would be an infinite loop. */
  var AUTH_PATHS = ['/auth/login', '/auth/refresh', '/auth/logout'];

  function isAuthPath(path) {
    for (var i = 0; i < AUTH_PATHS.length; i++) {
      if (path.indexOf(AUTH_PATHS[i]) === 0) return true;
    }
    return false;
  }

  /* One refresh at a time, shared by every caller waiting on it.

     This coalescing is not an optimisation, it is a correctness
     requirement. Refresh tokens ROTATE: presenting one revokes it. A page
     that fires four requests in parallel gets four simultaneous 401s, and
     four independent refreshes would present the same token four times.
     The server treats a re-presented refresh token as evidence of theft and
     revokes every session the user has — so without this, an ordinary
     parallel page load would log the user out and look like an attack. */
  var refreshInFlight = null;

  function refreshSession() {
    if (refreshInFlight) return refreshInFlight;

    refreshInFlight = fetch(base() + '/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: '{}'
    }).then(function (response) {
      if (!response.ok) {
        var err = new Error('Session expired.');
        err.status = response.status;
        throw err;
      }
      return response.json().catch(function () { return null; });
    });

    var settle = function () { refreshInFlight = null; };
    refreshInFlight.then(settle, settle);
    return refreshInFlight;
  }

  function onSessionLost() {
    if (NS.session && typeof NS.session.expire === 'function') NS.session.expire();
  }

  /* Run `attempt`; on a 401, refresh once and run it again. */
  function withReauth(path, attempt) {
    return attempt().catch(function (err) {
      if (!err || err.status !== 401 || isAuthPath(path)) throw err;
      return refreshSession().then(attempt, function () {
        onSessionLost();
        throw err;
      });
    });
  }

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
    var init = { method: method, headers: headers, body: body, credentials: 'include' };
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

  /* Server-sent events over POST.
   *
   * EventSource cannot do this — it is GET-only and cannot carry a body — so
   * the stream is read straight off fetch's ReadableStream. `onEvent` is
   * called with each parsed JSON payload as it arrives.
   *
   * `idleTimeout` bounds *silence*, not total duration, which is the whole
   * point of streaming: a turn may legitimately run for a minute, but half a
   * minute with nothing on the wire means the connection is dead. The server
   * sends a heartbeat every few seconds, so any real gap is a fault.
   */
  function stream(path, body, options) {
    var opts = options || {};
    var idleTimeout = opts.idleTimeout || 30000;
    var onEvent = opts.onEvent || function () {};
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var idleTimer = null;

    function resetIdle(reject) {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        if (controller) controller.abort();
        var e = new Error('Request timed out.');
        e.timeout = true;
        reject(e);
      }, idleTimeout);
    }

    return new Promise(function (resolve, reject) {
      var init = {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body)
      };
      if (controller) init.signal = controller.signal;

      fetch(base() + path, init).then(function (response) {
        if (!response.ok) {
          var httpError = new Error('HTTP ' + response.status);
          httpError.status = response.status;
          throw httpError;
        }
        if (!response.body || !response.body.getReader) {
          // No streaming support in this browser — the caller falls back.
          var e = new Error('streaming unsupported');
          e.unsupported = true;
          throw e;
        }
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        resetIdle(reject);

        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              if (idleTimer) clearTimeout(idleTimer);
              resolve();
              return;
            }
            resetIdle(reject);
            buffer += decoder.decode(chunk.value, { stream: true });
            // SSE frames are separated by a blank line; anything after the
            // last one is a partial frame and stays in the buffer.
            var frames = buffer.split('\n\n');
            buffer = frames.pop();
            frames.forEach(function (frame) {
              frame.split('\n').forEach(function (line) {
                if (line.indexOf('data:') !== 0) return;
                try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) { /* ignore a malformed frame */ }
              });
            });
            return pump();
          });
        }
        return pump();
      }).catch(function (err) {
        if (idleTimer) clearTimeout(idleTimer);
        if (err && err.name === 'AbortError') return;   // already rejected above
        reject(err);
      });
    });
  }

  NS.api = {
    /* A request that deliberately does NOT re-authenticate: the auth
       endpoints themselves, and anything that wants to see a raw 401. */
    raw: request,
    refreshSession: refreshSession,
    stream: function (path, body, options) {
      return withReauth(path, function () { return stream(path, body, options); });
    },
    get: function (path, params) {
      var q = '';
      if (params) {
        var parts = Object.keys(params).filter(function (k) { return params[k] != null && params[k] !== ''; })
          .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); });
        if (parts.length) q = '?' + parts.join('&');
      }
      return withReauth(path, function () {
        return retryable(function () { return request('GET', path + q); });
      });
    },
    post: function (path, body, options) {
      var opts = options || {};
      return withReauth(path, function () {
        return retryable(function () { return request('POST', path, { body: body, timeout: opts.timeout }); });
      });
    },
    upload: function (path, file, extraFields) {
      return withReauth(path, function () {
        /* Rebuilt per attempt: a FormData that has been sent once cannot be
           relied on to send again after the refresh. */
        var fd = new FormData();
        fd.append('file', file);
        if (extraFields) {
          Object.keys(extraFields).forEach(function (k) { if (extraFields[k] != null) fd.append(k, extraFields[k]); });
        }
        return request('POST', path, { body: fd, json: false, timeout: 60000 });
      });
    }
  };
})(window.ACA);