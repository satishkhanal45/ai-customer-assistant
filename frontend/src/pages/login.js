/* Sign-in page — for staff.

   Reached from the quiet "Staff sign in" link in the header, not by being
   bounced here: chat is the front door and a visitor never sees this page.
   Accounts are created by an administrator; there is no self-signup, which
   is why there is nothing on this form but a credential. Two things it deliberately does not do:

   * It does not tell the caller whether an address has an account. The
     server answers a wrong password and an unknown address identically, and
     this page shows whatever it is given rather than trying to be more
     helpful — being more helpful here means confirming which addresses are
     worth attacking.
   * It does not store anything. On success the server has set two HttpOnly
     cookies; there is no token for this page to keep.

   Visually it is a two-panel card: the brand side carries the same drifting
   lattice motif as the header and the Overview hero, so the first screen of
   the product looks like the rest of it. Below 880px the brand side is
   dropped rather than stacked — on a phone it is decoration competing with
   the only thing on screen that matters.
*/
(function (NS) {
  'use strict';

  var handlers = [];

  /* The same lattice used by the header and the Overview hero band. Drawn
     with the shared .gf-* classes so it picks up theme colours and the drift
     animation for free. */
  function lattice() {
    return '<div class="graph-field" aria-hidden="true">' +
      '<svg viewBox="0 0 300 420" preserveAspectRatio="xMidYMid slice">' +
      '<g class="gf-drift">' +
      '<line class="gf-edge" x1="40" y1="60" x2="130" y2="110"/>' +
      '<line class="gf-edge" x1="130" y1="110" x2="70" y2="200"/>' +
      '<line class="gf-edge" x1="130" y1="110" x2="240" y2="80"/>' +
      '<line class="gf-edge" x1="70" y1="200" x2="160" y2="260"/>' +
      '<line class="gf-edge" x1="240" y1="80" x2="260" y2="190"/>' +
      '<line class="gf-edge" x1="260" y1="190" x2="160" y2="260"/>' +
      '<line class="gf-edge" x1="160" y1="260" x2="110" y2="350"/>' +
      '<line class="gf-edge" x1="260" y1="190" x2="230" y2="330"/>' +
      '<line class="gf-edge" x1="110" y1="350" x2="230" y2="330"/>' +
      '<circle class="gf-node gf-pulse" cx="40" cy="60" r="4"/>' +
      '<circle class="gf-node alt gf-pulse" cx="130" cy="110" r="5" style="animation-delay:.5s"/>' +
      '<circle class="gf-node gf-pulse" cx="70" cy="200" r="4" style="animation-delay:1s"/>' +
      '<circle class="gf-node alt gf-pulse" cx="240" cy="80" r="4" style="animation-delay:1.5s"/>' +
      '<circle class="gf-node gf-pulse" cx="260" cy="190" r="5" style="animation-delay:2s"/>' +
      '<circle class="gf-node alt gf-pulse" cx="160" cy="260" r="4" style="animation-delay:2.5s"/>' +
      '<circle class="gf-node gf-pulse" cx="110" cy="350" r="4" style="animation-delay:3s"/>' +
      '<circle class="gf-node alt gf-pulse" cx="230" cy="330" r="5" style="animation-delay:3.5s"/>' +
      '</g></svg></div>';
  }

  function point(icon, text) {
    return '<li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      icon + '</svg><span>' + text + '</span></li>';
  }

  function init(container) {
    container.innerHTML =
      '<div class="login-shell">' +

      '<div class="login-card">' +

      '<aside class="login-brand">' +
      lattice() +
      '<div class="login-brand-inner">' +
      '<div class="login-mark">' +
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="6" cy="6" r="2.3" fill="currentColor"/><circle cx="18" cy="7" r="2.3" fill="currentColor"/><circle cx="12" cy="18" r="2.3" fill="currentColor"/><path d="M7.8 7.2 10.2 16 M16.2 8.2 13 16.3 M8.2 6 16 6.8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" opacity=".85"/></svg>' +
      '</div>' +
      '<h2 class="login-brand-title">AI Customer<br>Assistant</h2>' +
      '<p class="login-brand-sub">Grounded answers from your own documents.</p>' +
      '<ul class="login-points">' +
      point('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>', 'Ask in plain language') +
      point('<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7 7.3 10.3 16 M17 7.3 13.7 16 M7.5 6h9"/>', 'Answers cite their source') +
      point('<path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3M17 8l-5-5-5 5M12 3v12"/>', 'Upload or crawl to teach it') +
      '</ul>' +
      '</div>' +
      '</aside>' +

      '<form class="login-form" id="loginForm" autocomplete="on" novalidate>' +

      '<h1 class="login-title">Sign in</h1>' +
      '<p class="login-sub">For staff accounts. Visitors can use the assistant without signing in.</p>' +

      '<label class="login-label" for="loginEmail">Email</label>' +
      '<input class="login-input" id="loginEmail" type="email" name="email"' +
      ' autocomplete="username" placeholder="you@example.com" required>' +

      '<label class="login-label" for="loginPassword">Password</label>' +
      '<div class="login-password">' +
      '<input class="login-input" id="loginPassword" type="password" name="password"' +
      ' autocomplete="current-password" placeholder="••••••••••••" required>' +
      '<button type="button" class="login-reveal" id="loginReveal"' +
      ' title="Show password" aria-label="Show password">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<path d="M1.5 12S5 5.5 12 5.5 22.5 12 22.5 12 19 18.5 12 18.5 1.5 12 1.5 12z"/>' +
      '<circle cx="12" cy="12" r="3"/></svg>' +
      '</button>' +
      '</div>' +

      '<div class="login-error" id="loginError" hidden></div>' +
      '<button class="btn btn-primary btn-full login-btn" id="loginSubmit" type="submit">Sign in</button>' +
      '<p class="login-foot"><a href="#/chat">Back to the assistant</a></p>' +
      '</form>' +

      '</div>' +
      '</div>';

    var form = document.getElementById('loginForm');
    var submit = document.getElementById('loginSubmit');
    var error = document.getElementById('loginError');
    var password = document.getElementById('loginPassword');
    var reveal = document.getElementById('loginReveal');

    /* Listeners are tracked so `destroy` can remove them: the router tears
       this page down on navigation, and a listener left behind on a node that
       no longer exists is a leak per visit. */
    function on(node, event, fn) {
      node.addEventListener(event, fn);
      handlers.push([node, event, fn]);
    }

    /* A reveal toggle rather than a "show password" checkbox: people mistype
       long passwords, and the alternative is a failed attempt that counts
       against the five-per-fifteen-minutes limit. */
    on(reveal, 'click', function () {
      var showing = password.type === 'text';
      password.type = showing ? 'password' : 'text';
      reveal.classList.toggle('on', !showing);
      reveal.title = showing ? 'Show password' : 'Hide password';
      reveal.setAttribute('aria-label', reveal.title);
      password.focus();
    });

    on(form, 'submit', function (event) {
      event.preventDefault();
      var email = document.getElementById('loginEmail').value.trim();
      if (!email || !password.value) {
        error.textContent = 'Enter your email and password.';
        error.hidden = false;
        return;
      }

      error.hidden = true;
      submit.disabled = true;
      submit.classList.add('is-busy');
      submit.textContent = 'Signing in…';

      NS.session.login(email, password.value).then(function () {
        NS.router.go(NS.router.afterLogin());
      }, function (err) {
        // The password field is cleared on failure so a shared screen does
        // not leave one sitting in the DOM.
        password.value = '';
        password.type = 'password';
        reveal.classList.remove('on');
        error.textContent = (err && err.message) || 'Sign-in failed.';
        error.hidden = false;
        submit.disabled = false;
        submit.classList.remove('is-busy');
        submit.textContent = 'Sign in';
        password.focus();
      });
    });

    document.getElementById('loginEmail').focus();
  }

  function destroy() {
    handlers.forEach(function (h) { h[0].removeEventListener(h[1], h[2]); });
    handlers = [];
  }

  NS.pages = NS.pages || {};
  NS.pages.login = { init: init, destroy: destroy };
})(window.ACA);
