/* Hash router — renders page modules into #view. Loaded after session.js.

   Since P0-3 the router is also the route guard, on two axes: every page
   except `login` requires a signed-in user, and pages marked `role:
   'admin'` require that role. The guard is here rather than in each page
   because a page that forgets to check is a page that renders its shell,
   fires its requests, and shows a screen full of errors instead of a
   login form.

   It is a usability layer, not a security boundary. Nothing here protects
   any data: the API refuses unauthorised requests on its own, and a
   determined caller skips the browser entirely. This only decides what is
   worth drawing.
*/
(function (NS) {
  'use strict';

  var current = null;
  var currentEl = null;

  // Where to land after signing in: whatever was asked for before the
  // redirect, so a bookmarked #/graph survives an expired session.
  var intended = null;

  var ICONS = {
    overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    graph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7 7.3 10.3 16 M17 7.3 13.7 16 M7.5 6h9"/></svg>',
    ingest: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3M17 8l-5-5-5 5M12 3v12"/></svg>',
    prompt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/></svg>',
    admin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
    apikeys: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="7.5" cy="15.5" r="3.5"/><path d="m10 13 8.5-8.5"/><path d="m16 7 2 2"/><path d="m19 4 2 2"/></svg>'
  };

  /* `role` is the *minimum* tier a page needs, mirroring `auth/roles.py`.
     Anything below it is hidden from the sidebar and refused by `render` if
     reached by URL — though the guard that matters is the backend's; this
     one only decides what to draw.

     The note that used to sit here said "only two roles exist, so `mayView`
     is a boolean; if a middle tier is ever added this has to become a rank
     comparison rather than isAdmin()". A tier was added — `visitor`, below
     member — so it is one now.

     Where the lines fall: a **visitor** is a stranger who signed themselves
     up, so they get the conversation and nothing else. A **member** is a
     colleague, so they get the pages that add to and inspect the corpus. An
     **admin** gets the system's own controls, including Prompt, because
     editing the agent's prompts changes how it answers everyone. */
  var LINKS = [
    { key: 'overview', label: 'Overview', title: 'AI Customer Assistant — Overview', role: 'member' },
    { key: 'chat', label: 'Chat', title: 'AI Customer Assistant — Chat' },
    { key: 'graph', label: 'Graph', title: 'AI Customer Assistant — Knowledge Graph', role: 'member' },
    { key: 'ingest', label: 'Ingest', title: 'AI Customer Assistant — Ingest', role: 'member' },
    { key: 'prompt', label: 'Prompt', title: 'AI Customer Assistant — Agent Prompts', role: 'admin' },
    { key: 'admin', label: 'Admin', title: 'AI Customer Assistant — Admin', role: 'admin' },
    { key: 'apikeys', label: 'API Keys', title: 'AI Customer Assistant — API Keys', role: 'admin' }
  ];

  var DEFAULT_PAGE = 'chat';

  function linkFor(key) {
    for (var i = 0; i < LINKS.length; i++) if (LINKS[i].key === key) return LINKS[i];
    return null;
  }

  /* Reachable without an account. One entry today, named rather than
     hardcoded at the call site so adding a second is a visible decision --
     the same reason the backend keeps an explicit public-route allowlist. */
  var PUBLIC_PAGES = ['chat'];

  function isPublic(key) {
    return PUBLIC_PAGES.indexOf(key) !== -1;
  }

  function mayView(key) {
    var link = linkFor(key);
    return !link || !link.role || NS.session.hasRole(link.role);
  }

  function afterLogin() {
    var target = intended || DEFAULT_PAGE;
    intended = null;
    return target;
  }

  function parseHash() {
    var h = (location.hash || '').replace(/^#\/?/, '');
    var first = h.split(/[&?]/)[0];
    return first || 'chat';
  }

  function renderNav(activeKey) {
    var wrap = document.getElementById('navItems');
    if (!wrap) return;
    // Nothing to navigate to until someone is signed in.
    if (!NS.session.isSignedIn()) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = LINKS.filter(function (l) { return mayView(l.key); }).map(function (l) {
      return '<button class="nav-item' + (l.key === activeKey ? ' active' : '') + '" data-page="' + l.key + '">' +
        (ICONS[l.key] || '') + '<span>' + NS.utils.esc(l.label) + '</span></button>';
    }).join('');
    Array.prototype.forEach.call(wrap.querySelectorAll('.nav-item'), function (b) {
      b.addEventListener('click', function () { NS.router.go(b.getAttribute('data-page')); });
    });
  }

  function destroyCurrent() {
    if (current && typeof current.destroy === 'function') {
      try { current.destroy(); } catch (e) { /* ignore */ }
    }
    if (currentEl) { currentEl.innerHTML = ''; currentEl.remove(); }
    current = null; currentEl = null;
  }

  function render() {
    var key = parseHash();
    var signedIn = NS.session.isSignedIn();

    /* Chat is the front door, not a page behind a gate. A signed-out caller
       asking for it gets it; one asking for a staff page is sent to sign in,
       with where they were going remembered.

       The distinction matters both ways. Bouncing an anonymous visitor to a
       login form is the friction this flow exists to remove, and dropping
       someone who deep-linked to `#/graph` into a chat window instead would
       look like the app ignoring them. */
    if (!signedIn && key !== 'login' && !isPublic(key)) {
      intended = key;
      NS.router.go('login');
      return;
    }
    if (signedIn && key === 'login') { NS.router.go(afterLogin()); return; }

    /* Reached an admin page without the role -- a typed URL, a stale
       bookmark, or a demotion since the link was drawn. Say so rather than
       bouncing silently, which reads as the app being broken. */
    if (signedIn && !mayView(key) && key !== DEFAULT_PAGE) {
      NS.utils.status('That page is for administrators.', true);
      NS.router.go(DEFAULT_PAGE);
      return;
    }

    var page = NS.pages && NS.pages[key];
    if (!page) {
      key = DEFAULT_PAGE;
      page = NS.pages[key];
    }

    destroyCurrent();
    renderNav(key);
    var app = document.querySelector('.app');
    app.classList.toggle('signed-out', !signedIn);
    /* A visitor has one page, so the workspace chrome around it is furniture
       with nothing to navigate. Stripping it is what turns "an internal tool
       with the menu removed" into something that reads as a customer
       assistant. Same mechanism the login page already uses. */
    app.classList.toggle('visitor-mode', signedIn && !NS.session.hasRole('member'));

    var view = document.getElementById('view');
    currentEl = document.createElement('div');
    currentEl.id = 'page-' + key;
    currentEl.className = 'page';
    view.appendChild(currentEl);
    current = page;
    document.title = (LINKS.filter(function (l) { return l.key === key; })[0] || {}).title || document.title;

    try {
      page.init(currentEl);
    } catch (e) {
      currentEl.innerHTML = '<div class="hint" style="padding:20px">Failed to load ' + key + ': ' + NS.utils.esc(e.message) + '</div>';
      console.error(e);
    }
  }

  function wireSidebar() {
    var btn = document.getElementById('sidebarCollapseBtn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var app = document.querySelector('.app');
      var collapsed = app.classList.toggle('sidebar-collapsed');
      try { localStorage.setItem('aica-sidebar-collapsed', collapsed ? '1' : '0'); } catch (e) {}
    });
    try {
      if (localStorage.getItem('aica-sidebar-collapsed') === '1') {
        document.querySelector('.app').classList.add('sidebar-collapsed');
      }
    } catch (e) {}
  }

  function checkHealth() { /* removed — the "System Online" pill was dropped from the header */ }

  NS.router = {
    init: function () {
      wireSidebar();
      window.addEventListener('hashchange', render);
      // Ask the server who we are before drawing anything. Rendering first
      // would flash the chat page and then replace it with a login form
      // for anyone whose cookies had expired.
      NS.session.bootstrap().then(render, render);
    },
    parseHash: parseHash,
    afterLogin: afterLogin,
    go: function (key) {
      if (parseHash() === key) { render(); return; }
      location.hash = '#/' + key;
    }
  };
})(window.ACA);
