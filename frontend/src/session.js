/* Session state — who is signed in, and the transitions in and out.

   No token ever appears in this file, or anywhere else in the frontend. The
   access and refresh tokens are HttpOnly cookies: the browser attaches them
   and script cannot read them, which is the entire reason they are cookies
   rather than something kept in localStorage. This module holds only the
   *description* of the signed-in user — email and role — which is public
   information to the person it describes.

   Loaded after api.js (it uses NS.api) and before router.js (which asks it
   whether anyone is signed in before rendering a page).
*/
(function (NS) {
  'use strict';

  var current = null;

  function set(user) {
    current = user || null;
    render();
    return current;
  }

  /* Ask the server who we are, using the cookies the browser already holds.

     Deliberately built on NS.api.raw rather than NS.api.get: the normal
     client treats a 401 as "refresh, then replay, then give up and redirect
     to the login page". At startup that is wrong — not being signed in yet
     is the ordinary case, not a session that just died — so the refresh is
     attempted by hand here and a failure resolves to null instead of
     bouncing the page. */
  function bootstrap() {
    return NS.api.raw('GET', '/auth/me').then(set, function (err) {
      if (!err || err.status !== 401) return set(null);
      return NS.api.refreshSession()
        .then(function () { return NS.api.raw('GET', '/auth/me').then(set, function () { return set(null); }); },
              function () { return set(null); });
    });
  }

  function login(email, password) {
    return NS.api.raw('POST', '/auth/login', {
      body: { email: email, password: password }
    }).then(function (body) { return set(body && body.user); });
  }

  function logout() {
    /* The server revokes the refresh token and clears both cookies. The
       local state is cleared whatever it answers: a logout that appears to
       fail and leaves the user looking signed in is worse than one that is
       simply optimistic about the network. */
    var done = function () { set(null); NS.router.go('login'); };
    return NS.api.raw('POST', '/auth/logout', { body: {} }).then(done, done);
  }

  /* Called by api.js when a refresh has failed — the session is genuinely
     over rather than merely stale. */
  function expire() {
    if (!current) return;
    set(null);
    NS.router.go('login');
  }

  function user() { return current; }
  function isSignedIn() { return current !== null; }
  function isAdmin() { return !!current && current.role === 'admin'; }

  /* Header identity: the signed-in address and a way out. */
  function render() {
    var host = document.getElementById('sessionBox');
    if (!host) return;
    if (!current) { host.innerHTML = ''; return; }

    host.innerHTML =
      '<span class="session-email" title="' + NS.utils.esc(current.email) + '">' +
      NS.utils.esc(current.email) + '</span>' +
      (current.role === 'admin' ? '<span class="session-role">admin</span>' : '') +
      '<button class="icon-btn" id="signOutBtn" title="Sign out">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>' +
      '</svg></button>';

    var button = document.getElementById('signOutBtn');
    if (button) button.addEventListener('click', logout);
  }

  NS.session = {
    bootstrap: bootstrap,
    login: login,
    logout: logout,
    expire: expire,
    user: user,
    isSignedIn: isSignedIn,
    isAdmin: isAdmin
  };
})(window.ACA);
