/* Shared utilities — global namespace ACA.utils (classic scripts; works over file://) */
window.ACA = window.ACA || {};

(function (NS) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function formatDate(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  }

  function formatBytes(n) {
    if (n == null) return '—';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = function () { reject(new Error('Failed to load ' + src)); };
      document.head.appendChild(s);
    });
  }

  function el(tag, attrs, html) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'style') node.setAttribute('style', attrs[k]);
        else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2), attrs[k]);
        else node.setAttribute(k, attrs[k]);
      });
    }
    if (html != null) node.innerHTML = html;
    return node;
  }

  function status(msg, isError) {
    var s = document.getElementById('statusbar');
    if (!s) return;
    s.textContent = msg;
    s.classList.toggle('error', !!isError);
    s.classList.add('show');
    clearTimeout(NS.utils._statusTimer);
    NS.utils._statusTimer = setTimeout(function () { s.classList.remove('show'); }, 3200);
  }

  NS.utils = {
    esc: esc,
    debounce: debounce,
    uuid: uuid,
    formatDate: formatDate,
    formatBytes: formatBytes,
    loadScript: loadScript,
    el: el,
    status: status,
    cssVar: function (name, fb) {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fb;
    },
    colorFor: function (t) {
      return NS.utils.cssVar('--accent', '#5b8def');
    },
    theme: {
      current: function () { return document.documentElement.getAttribute('data-theme') || 'dark'; },
      set: function (t) {
        t = t === 'light' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', t);
        try { localStorage.setItem('aca.theme', t); } catch (e) {}
        NS.utils.theme.refreshButton();
        document.dispatchEvent(new CustomEvent('aca:theme', { detail: { theme: t } }));
      },
      toggle: function () { NS.utils.theme.set(NS.utils.theme.current() === 'dark' ? 'light' : 'dark'); },
      refreshButton: function () {
        var b = document.getElementById('themeToggle');
        if (b) b.textContent = NS.utils.theme.current() === 'dark' ? 'Light' : 'Dark';
      }
    }
  };
  NS.utils.theme.refreshButton();
  var tbtn = document.getElementById('themeToggle');
  if (tbtn) tbtn.addEventListener('click', function () { NS.utils.theme.toggle(); });
})(window.ACA);