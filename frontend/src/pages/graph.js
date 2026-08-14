/* Unified knowledge-graph explorer — 2D/3D force-graph, with value search,
   type filters, export, deep-linking, stats and shortcuts. */
(function (NS) {
  'use strict';

  var nodeTypes = {}, nodesArr = [], linksArr = [], edgeIds = {}, pathNodeIds = {}, pathEdgeIds = {};
  var hiddenTypes = {};   // entity_type -> true (hidden in canvas)
  var hiddenRelations = {};
  var mode = '2d';
  var g2 = null, g3 = null;
  var forceParams = { charge: -40, linkDistance: 50, gravity: 0.25 };
  var enginesLoaded = { fg: false, three: false, fg3d: false };
  var roots = {};

  function cssVar(name, fb) { return NS.utils.cssVar(name, fb); }
  function highlightColor() { return cssVar('--accent', '#3b5bfd'); }
  function textColor() { return cssVar('--text', '#e8eaed'); }
  function borderColor() { return cssVar('--border', 'rgba(255,255,255,.2)'); }

  function colorFor(t) { return NS.utils.colorFor(t); }
  function nodeColorOf(n) { return pathNodeIds[n.id] ? highlightColor() : colorFor(n.entity_type); }
  function hexToRgb(h) {
    var m = /^#([0-9a-f]{6})$/i.exec(h || ''); if (!m) return [100, 116, 139];
    var v = parseInt(m[1], 16); return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }
  function rgba(h, a) { var r = hexToRgb(h); return 'rgba(' + r[0] + ',' + r[1] + ',' + r[2] + ',' + a + ')'; }

  /* ---------------- init / shell ---------------- */

  function init(container) {
    container.innerHTML = '';
    container.style.padding = '0';

    var toolbar = NS.utils.el('div', { class: 'graph-controls' });
    container.appendChild(toolbar);
    buildToolbar(toolbar);

    var shell = NS.utils.el('div', { class: 'graph-shell' });
    container.appendChild(shell);

    var sidePanel = NS.utils.el('aside', { class: 'side-panel' });
    var canvasWrap = NS.utils.el('div', { class: 'graph-canvas-wrap' });
    shell.appendChild(sidePanel);
    shell.appendChild(canvasWrap);

    var viewport = NS.utils.el('div', { class: 'graph-viewport' });
    canvasWrap.appendChild(viewport);

    var views = NS.utils.el('div', { class: 'graph-views' });
    var view2d = NS.utils.el('div', { class: 'gv', id: 'view2d' });
    var view3d = NS.utils.el('div', { class: 'gv hidden', id: 'view3d' });
    views.appendChild(view2d); views.appendChild(view3d);
    viewport.appendChild(views);

    roots.hud = NS.utils.el('div', { class: 'hud' },
      '<div class="hud-chip">NODES <b id="hudNodes">0</b></div>' +
      '<div class="hud-chip">LINKS <b id="hudLinks">0</b></div>' +
      '<div class="hud-chip" id="hudMode">MODE 2D</div>');
    viewport.appendChild(roots.hud);

    roots.container = container;
    roots.view2d = view2d;
    roots.view3d = view3d;
    roots.toolbar = toolbar;
    roots.statusbar = NS.utils.el('div', { class: 'graph-statusbar' });
    viewport.appendChild(roots.statusbar);
    roots.banner = NS.utils.el('div', { class: 'graph-banner' });
    viewport.appendChild(roots.banner);

    roots.sidebar = sidePanel;
    sidePanel.innerHTML =
      '<section><h4>Entity Details</h4><div id="detail"><div class="hint">Click a node to inspect it.</div></div></section>' +
      '<section><h4>Legend / Filters</h4><div id="legend"><div class="hint">Load a graph to see types.</div></div></section>' +
      '<section><h4>Stats</h4><div id="stats"><div class="hint">—</div></div></section>' +
      '<section><div class="hint">• Search a node to seed the graph.<br>• <b>Click</b> a node: details + expand.<br>• 2D: drag nodes, scroll zoom.<br>• 3D: drag orbit, scroll zoom.<br>• Path A/B → Find path.<br>• <b>e</b> export · <b>f</b> fit · <b>Esc</b> deselect.</div></section>';

    loadEngines().then(function () {
      if (!window._FG2D && !window._FG3D) { showBanner('Graph engines failed to load — check internet access to unpkg.com.'); return; }
      if (!window._FG3D) { document.getElementById('btn3d').disabled = true; }
      if (!window._FG2D) { document.getElementById('btn2d').disabled = true; setMode('3d'); }
      attachSearch('search', 'suggest-search', function (item) {
        document.getElementById('search').value = item.label;
        closeSuggest(); seedItem(item);
      });
      attachSearch('pathA', 'suggest-A', function (item) {
        document.getElementById('pathA').value = 'A: ' + item.label + ' \u2014 ' + item.id;
        closeSuggest();
      });
      attachSearch('pathB', 'suggest-B', function (item) {
        document.getElementById('pathB').value = 'B: ' + item.label + ' \u2014 ' + item.id;
        closeSuggest();
      });
      document.getElementById('btnPath').addEventListener('click', runPath);
      document.getElementById('btnExpand').addEventListener('click', expandAll);
      document.getElementById('btnClear').addEventListener('click', clearAll);
      document.getElementById('btnRotate').addEventListener('click', toggleRotate);
      document.getElementById('btn2d').addEventListener('click', function () { setMode('2d'); });
      document.getElementById('btn3d').addEventListener('click', function () { setMode('3d'); });
      document.getElementById('btnExport').addEventListener('click', exportGraph);
      document.getElementById('btnShare').addEventListener('click', shareLink);
      var fCharge = document.getElementById('fCharge');
      var fDist = document.getElementById('fDist');
      fCharge.addEventListener('input', function () { forceParams.charge = Number(fCharge.value); applyForce(); });
      fDist.addEventListener('input', function () { forceParams.linkDistance = Number(fDist.value); applyForce(); });
      document.getElementById('searchMode').addEventListener('change', function () {
        document.getElementById('search').value = ''; closeSuggest();
      });
      document.getElementById('btnStats').addEventListener('click', updateStats);
      create2D();
      renderActive();
      restoreFromHash();
      window.addEventListener('resize', onResize);
      window.addEventListener('keydown', onKey);
      window.addEventListener('aca:theme', onThemeChange);
      flash('Ready.');
    }).catch(function (err) {
      showBanner('Engine load failed: ' + err.message);
    });
  }

  function onThemeChange() {
    renderActive();
    if (mode === '3d' && g3) {
      try { g3._destructor && g3._destructor(); } catch (e) {}
      g3 = null;
      create3D();
      if (g3) g3.graphData({ nodes: visibleNodes(), links: visibleLinks() });
    }
  }

  function destroy() {
    if (g2) { try { g2._destructor && g2._destructor(); } catch (e) {} g2 = null; }
    if (g3) { try { g3._destructor && g3._destructor(); } catch (e) {} g3 = null; }
    window.removeEventListener('resize', onResize);
    window.removeEventListener('keydown', onKey);
    window.removeEventListener('aca:theme', onThemeChange);
    nodeTypes = {}; nodesArr = []; linksArr = []; edgeIds = {}; pathNodeIds = {}; pathEdgeIds = {};
    hiddenTypes = {}; hiddenRelations = {};
  }

  function buildToolbar(toolbar) {
    var html =
      '<div class="view-toggle"><button id="btn2d" class="active">2D</button><button id="btn3d">3D</button></div>' +
      '<div class="gc-divider"></div>' +
      '<div class="field"><input class="gc-input" id="search" type="text" placeholder="Search entities…" autocomplete="off" spellcheck="false"><div class="suggest" id="suggest-search"></div></div>' +
      '<select id="searchMode" class="gc-select" title="Search mode"><option value="name">by name</option><option value="value">by value</option></select>' +
      '<div class="gc-divider"></div>' +
      '<div class="gc-group"><div class="field"><input class="gc-input" id="pathA" type="text" placeholder="Path A…" autocomplete="off" spellcheck="false"><div class="suggest" id="suggest-A"></div></div></div>' +
      '<div class="gc-group"><div class="field"><input class="gc-input" id="pathB" type="text" placeholder="Path B…" autocomplete="off" spellcheck="false"><div class="suggest" id="suggest-B"></div></div></div>' +
      '<button id="btnPath" class="btn btn-primary btn-sm">Find Path</button>' +
      '<div class="gc-divider"></div>' +
      '<div class="gc-group">' +
      '<button id="btnExpand" class="btn btn-ghost btn-sm">Expand all</button>' +
      '<button id="btnClear" class="btn btn-ghost btn-sm">Clear</button>' +
      '<button id="btnRotate" class="btn btn-ghost btn-sm">Auto-rotate</button>' +
      '<button id="btnExport" class="btn btn-ghost btn-sm">Export</button>' +
      '<button id="btnShare" class="btn btn-ghost btn-sm">Share</button>' +
      '<button id="btnStats" class="btn btn-ghost btn-sm">Stats</button>' +
      '</div>' +
      '<div class="gc-group" id="ctlDepth"><label>DEPTH</label><select class="gc-select" id="depth"><option value="1">1</option><option value="2" selected>2</option><option value="3">3</option></select></div>' +
      '<div class="gc-group" id="ctlForce"><label>CHARGE</label><input id="fCharge" type="range" min="-120" max="0" value="-40">' +
      '<label>DIST</label><input id="fDist" type="range" min="20" max="140" value="50"></div>';
    toolbar.innerHTML = html;
  }

  function loadEngines() {
    var c = NS.config.engines;
    var p = Promise.resolve();
    if (!window._FG2D) {
      p = p.then(function () { return NS.utils.loadScript(c.forceGraph); }).then(function () { window._FG2D = window.ForceGraph; });
    }
    if (!window._FG3D) {
      p = p.then(function () { return NS.utils.loadScript(c.three); })
        .then(function () { return NS.utils.loadScript(c.forceGraph3d); })
        .then(function () { window._FG3D = window.ForceGraph3D || window.ForceGraph; });
    }
    return p;
  }

  function flash(msg, isError) { NS.utils.status(msg, isError); }

  function showBanner(msg) {
    var b = roots.banner;
    if (b) { b.textContent = msg; b.classList.add('show'); }
  }

  /* ---------------- data ---------------- */

  function addNodes(nodes) {
    var changed = false;
    (nodes || []).forEach(function (n) {
      if (!n || !n.id || nodeTypes[n.id]) return;
      nodeTypes[n.id] = n.entity_type;
      nodesArr.push({ id: n.id, name: n.label, entity_type: n.entity_type });
      changed = true;
    });
    if (changed) renderActive();
  }

  function addEdges(edges) {
    var changed = false;
    (edges || []).forEach(function (e) {
      if (!e || !e.id || edgeIds[e.id]) return;
      if (!nodeTypes[e.source_entity_id] || !nodeTypes[e.target_entity_id]) return;
      edgeIds[e.id] = true;
      linksArr.push({ id: e.id, source: e.source_entity_id, target: e.target_entity_id, label: e.relation_type });
      changed = true;
    });
    if (changed) renderActive();
  }

  function visibleNodes() {
    return nodesArr.filter(function (n) { return !hiddenTypes[n.entity_type]; });
  }

  function visibleLinks() {
    return linksArr.filter(function (l) {
      if (hiddenRelations[l.label]) return false;
      var a = nodesArr.filter(function (n) { return n.id === l.source; })[0];
      var b = nodesArr.filter(function (n) { return n.id === l.target; })[0];
      if (a && hiddenTypes[a.entity_type]) return false;
      if (b && hiddenTypes[b.entity_type]) return false;
      return true;
    });
  }

  function updateHud() {
    var n = roots.hudNodes, l = roots.hudLinks, m = roots.hudMode;
    if (n) n.textContent = nodesArr.length;
    if (l) l.textContent = linksArr.length;
    if (m) m.textContent = 'MODE ' + mode.toUpperCase();
  }

  function updateLegend() {
    var leg = roots.legend;
    if (!leg) return;
    var counts = {};
    Object.keys(nodeTypes).forEach(function (id) { var t = nodeTypes[id]; counts[t] = (counts[t] || 0) + 1; });
    var relCounts = {};
    linksArr.forEach(function (l) { relCounts[l.label] = (relCounts[l.label] || 0) + 1; });

    var html = '<div class="legend-group"><b>Types</b></div>';
    Object.keys(counts).sort().forEach(function (t) {
      var hidden = !!hiddenTypes[t];
      html += '<div class="legend-row type-toggle" data-type="' + NS.utils.esc(t) + '" title="' + (hidden ? 'Click to show' : 'Click to hide') + '">' +
        '<span class="dot" style="color:' + colorFor(t) + ';background:' + colorFor(t) + '"></span>' +
        '<span' + (hidden ? ' style="opacity:.35;text-decoration:line-through"' : '') + '>' + NS.utils.esc(t) + ' · ' + counts[t] + '</span></div>';
    });
    html += '<div class="legend-group"><b>Relations</b></div>';
    Object.keys(relCounts).sort().forEach(function (r) {
      var hidden = !!hiddenRelations[r];
      html += '<div class="legend-row rel-toggle" data-rel="' + NS.utils.esc(r) + '" title="Click to ' + (hidden ? 'show' : 'hide') + '">' +
        '<span class="edge-line"' + (hidden ? ' style="opacity:.25"' : '') + '></span>' +
        '<span' + (hidden ? ' style="opacity:.35;text-decoration:line-through"' : '') + '>' + NS.utils.esc(r) + ' · ' + relCounts[r] + '</span></div>';
    });
    if (!Object.keys(counts).length) html = '<div class="hint">Load a graph to see types.</div>';
    leg.innerHTML = html;

    Array.prototype.forEach.call(leg.querySelectorAll('.type-toggle'), function (row) {
      row.addEventListener('click', function () {
        var t = row.getAttribute('data-type');
        hiddenTypes[t] = hiddenTypes[t] ? null : true;
        if (!hiddenTypes[t]) delete hiddenTypes[t];
        renderActive(); updateLegend();
      });
    });
    Array.prototype.forEach.call(leg.querySelectorAll('.rel-toggle'), function (row) {
      row.addEventListener('click', function () {
        var r = row.getAttribute('data-rel');
        hiddenRelations[r] = hiddenRelations[r] ? null : true;
        if (!hiddenRelations[r]) delete hiddenRelations[r];
        renderActive(); updateLegend();
      });
    });
  }

  function updateStats() {
    var el = roots.stats;
    if (!el) return;
    if (!nodesArr.length) { el.innerHTML = '<div class="hint">—</div>'; return; }
    var byType = {};
    nodesArr.forEach(function (n) { byType[n.entity_type] = (byType[n.entity_type] || 0) + 1; });
    var byRel = {};
    linksArr.forEach(function (l) { byRel[l.label] = (byRel[l.label] || 0) + 1; });
    var typeHtml = Object.keys(byType).sort().map(function (t) {
      return '<div class="stat-row"><span>' + NS.utils.esc(t) + '</span><b>' + byType[t] + '</b></div>';
    }).join('');
    var relHtml = Object.keys(byRel).sort().map(function (r) {
      return '<div class="stat-row"><span>' + NS.utils.esc(r) + '</span><b>' + byRel[r] + '</b></div>';
    }).join('');
    var density = nodesArr.length > 1 ? (linksArr.length / (nodesArr.length * (nodesArr.length - 1))).toFixed(4) : '0';
    el.innerHTML =
      '<div class="stat-line">Nodes <b>' + nodesArr.length + '</b> · Links <b>' + linksArr.length + '</b> · Density <b>' + density + '</b></div>' +
      '<div class="stat-section">' + typeHtml + '</div>' +
      '<div class="stat-section">' + relHtml + '</div>';
  }

  function updateHudRefs() {
    roots.hudNodes = document.getElementById('hudNodes');
    roots.hudLinks = document.getElementById('hudLinks');
    roots.hudMode = document.getElementById('hudMode');
    roots.legend = document.getElementById('legend');
    roots.stats = document.getElementById('stats');
    roots.detail = document.getElementById('detail');
  }

  /* ---------------- renderers ---------------- */

  function renderActive() {
    updateHudRefs();
    if (mode === '2d') { create2D(); if (g2) g2.graphData({ nodes: visibleNodes(), links: visibleLinks() }); }
    else { create3D(); if (g3) g3.graphData({ nodes: visibleNodes(), links: visibleLinks() }); }
    updateHud(); updateLegend();
  }

  function create2D() {
    if (g2) return;
    if (!window._FG2D) { showBanner('2D engine failed to load.'); return; }
    try {
      g2 = window._FG2D()(roots.view2d)
        .backgroundColor('rgba(0,0,0,0)')
        .nodeId('id').linkSource('source').linkTarget('target')
        .nodeLabel(function (n) { return n.name + '  (' + n.entity_type + ')'; })
        .linkLabel(function (l) { return l.label; })
        .linkColor(function (l) { return pathEdgeIds[l.id] ? highlightColor() : borderColor(); })
        .linkWidth(function (l) { return pathEdgeIds[l.id] ? 2.6 : 1; })
        .linkDirectionalParticles(function (l) { return pathEdgeIds[l.id] ? 5 : 2; })
        .linkDirectionalParticleColor(function () { return highlightColor(); })
        .linkDirectionalParticleWidth(2)
        .nodeCanvasObjectMode(function () { return 'replace'; })
        .nodeCanvasObject(drawNode2D)
        .onNodeClick(function (n) { showDetail(n); expandNode(n.id); })
        .onBackgroundClick(closeSuggest)
        .cooldownTicks(80)
        .width(roots.view2d.clientWidth || 800)
        .height(roots.view2d.clientHeight || 600);
      applyForce();
    } catch (err) {
      console.error('2D init failed:', err); showBanner('2D engine failed to initialize.'); g2 = null;
    }
  }

  function drawNode2D(node, ctx, gs) {
    var r = 6.5;
    var c = nodeColorOf(node);
    var cx = node.x, cy = node.y;
    var grad = ctx.createRadialGradient(cx, cy, r * 0.3, cx, cy, r * 3);
    grad.addColorStop(0, rgba(c, 0.55));
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.arc(cx, cy, r * 3, 0, 2 * Math.PI); ctx.fill();
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    ctx.fillStyle = c; ctx.fill();
    ctx.lineWidth = 1.4 / gs; ctx.strokeStyle = borderColor(); ctx.stroke();
    var fs = Math.max(9, 11 / gs);
    ctx.font = '600 ' + fs + 'px Inter, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    ctx.fillStyle = textColor();
    ctx.shadowColor = c; ctx.shadowBlur = 6 / gs;
    ctx.fillText(node.name, cx, cy + r + 4 / gs);
    ctx.shadowBlur = 0;
  }

  function applyForce() {
    if (mode === '2d' && g2) {
      var charge = g2.d3Force('charge');
      if (charge) { charge.strength(forceParams.charge); }
      var link = g2.d3Force('link');
      if (link) { link.distance(forceParams.linkDistance); }
      var cent = g2.d3Force('center');
      if (cent) cent.strength(forceParams.gravity);
    }
    if (mode === '3d' && g3) {
      if (typeof g3.d3AlphaDecay === 'function') { /* no-op */ }
    }
  }

  function textSprite(text) {
    var c = document.createElement('canvas');
    c.width = 512; c.height = 96;
    var ctx = c.getContext('2d');
    ctx.font = '600 44px Inter, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.shadowColor = highlightColor(); ctx.shadowBlur = 12;
    ctx.fillStyle = textColor();
    ctx.fillText(text, 256, 48);
    var tex = new window.THREE.CanvasTexture(c);
    tex.needsUpdate = true;
    var sp = new window.THREE.Sprite(new window.THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
    sp.scale.set(90, 17, 1);
    return sp;
  }

  function nodeObject3D(node) {
    var c = nodeColorOf(node);
    var group = new window.THREE.Group();
    var halo = new window.THREE.Mesh(
      new window.THREE.SphereGeometry(13, 24, 24),
      new window.THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: 0.16, depthWrite: false }));
    var core = new window.THREE.Mesh(
      new window.THREE.SphereGeometry(6, 24, 24),
      new window.THREE.MeshBasicMaterial({ color: c }));
    var wire = new window.THREE.Mesh(
      new window.THREE.SphereGeometry(6.01, 16, 16),
      new window.THREE.MeshBasicMaterial({ color: cssVar('--bg', '#0a0d15'), wireframe: true, transparent: true, opacity: 0.5 }));
    group.add(halo); group.add(core); group.add(wire);
    var label = textSprite(node.name);
    label.position.y = 17;
    group.add(label);
    return group;
  }

  function create3D() {
    if (g3) return;
    if (!window._FG3D) { showBanner('3D engine failed to load — using 2D.'); setMode('2d'); return; }
    try {
      g3 = window._FG3D()(roots.view3d)
        .backgroundColor('rgba(0,0,0,0)')
        .nodeId('id').linkSource('source').linkTarget('target')
        .nodeLabel(function (n) { return n.name + '  (' + n.entity_type + ')'; })
        .linkLabel(function (l) { return l.label; })
        .linkColor(function (l) { return pathEdgeIds[l.id] ? highlightColor() : borderColor(); })
        .linkWidth(function (l) { return pathEdgeIds[l.id] ? 2.5 : 1; })
        .linkOpacity(0.55)
        .linkDirectionalParticles(2)
        .linkDirectionalParticleColor(function () { return highlightColor(); })
        .linkDirectionalParticleWidth(2)
        .nodeThreeObject(nodeObject3D)
        .onNodeClick(function (n) { showDetail(n); expandNode(n.id); })
        .onBackgroundClick(closeSuggest)
        .cooldownTicks(80)
        .width(roots.view3d.clientWidth || 800)
        .height(roots.view3d.clientHeight || 600);
    } catch (err) {
      console.error('3D init failed:', err);
      showBanner('3D engine failed to initialize — using 2D.');
      g3 = null; setMode('2d');
    }
  }

  function setMode(m) {
    if (m === mode) return;
    if (m === '2d' && !window._FG2D) { flash('2D engine unavailable.', true); return; }
    if (m === '3d' && !window._FG3D) { flash('3D engine unavailable.', true); return; }
    mode = m;
    document.getElementById('btn2d').classList.toggle('active', m === '2d');
    document.getElementById('btn3d').classList.toggle('active', m === '3d');
    document.getElementById('btnRotate').style.display = m === '3d' ? '' : 'none';
    roots.view2d.classList.toggle('hidden', m !== '2d');
    roots.view3d.classList.toggle('hidden', m !== '3d');
    if (m === '2d') { create2D(); if (g2) { g2.width(roots.view2d.clientWidth).height(roots.view2d.clientHeight); } }
    else { create3D(); if (g3) { g3.width(roots.view3d.clientWidth).height(roots.view3d.clientHeight); } }
    renderActive();
    if (m === '3d' && g3) g3.cameraPosition({ x: 240, y: 180, z: 300 }, { x: 0, y: 0, z: 0 }, 0);
    if (m === '2d' && g2) g2.zoomToFit(600, 60);
    flash(m === '2d' ? '2D view.' : '3D view.');
  }

  /* ---------------- actions ---------------- */

  function expandNode(id) {
    NS.api.get('/graph/entities/' + id + '/neighbors', { depth: 1 }).then(function (frag) {
      addNodes(frag.nodes); addEdges(frag.edges);
      flash('Expanded ' + (frag.nodes || []).length + ' neighbors.');
    }).catch(function () {});
  }

  function seedItem(item) { addNodes([item]); expandNode(item.id); }

  function showDetail(n) {
    var d = roots.detail;
    if (!d) return;
    d.innerHTML = '<div class="entity-name">' + NS.utils.esc(n.name) + '</div>' +
      '<span class="entity-type-badge">' + NS.utils.esc(n.entity_type) + '</span>' +
      '<div class="entity-facts" id="facts-' + n.id + '"></div>';
    NS.api.get('/graph/entities/' + n.id).then(function (body) {
      var f = document.getElementById('facts-' + n.id);
      if (!f) return;
      var facts = body.facts || [];
      if (!facts.length) { f.innerHTML = '<div class="fact"><div class="v">No attributes recorded.</div></div>'; return; }
      var html = '';
      facts.forEach(function (x) {
        html += '<div class="fact"><div class="k">' + NS.utils.esc(x.attribute_name) +
          (x.multivalue ? ' (multi)' : '') + '</div><div class="v">' + NS.utils.esc(x.value) + '</div></div>';
      });
      f.innerHTML = html;
    }).catch(function () {});
  }

  function attachSearch(inputId, suggestId, onPick) {
    var inp = document.getElementById(inputId);
    var sug = document.getElementById(suggestId);
    var timer = null;
    var lastQ = '';
    function open(list) {
      if (inp.value.trim() !== lastQ) return;
      if (!list || !list.length) { sug.classList.remove('open'); sug.innerHTML = ''; return; }
      sug.innerHTML = list.map(function (it) {
        return '<div class="item" data-id="' + it.id + '">' +
          '<span class="dot" style="color:' + colorFor(it.entity_type) + ';background:' + colorFor(it.entity_type) + '"></span>' +
          '<span>' + NS.utils.esc(it.label) + '</span>' +
          '<span class="t">' + NS.utils.esc(it.entity_type) + '</span></div>';
      }).join('');
      sug.classList.add('open');
      Array.prototype.forEach.call(sug.querySelectorAll('.item'), function (el) {
        el.addEventListener('mousedown', function (ev) {
          ev.preventDefault();
          clearTimeout(timer);
          lastQ = '';
          var item = list.filter(function (it) { return it.id === el.getAttribute('data-id'); })[0];
          onPick(item);
        });
      });
    }
    function query() {
      var q = inp.value.trim();
      lastQ = q;
      if (!q) { sug.classList.remove('open'); sug.innerHTML = ''; return; }
      clearTimeout(timer);
      timer = setTimeout(function () {
        var byValue = document.getElementById('searchMode') && document.getElementById('searchMode').value === 'value';
        var path = byValue ? '/graph/search_value' : '/graph/search';
        NS.api.get(path, { q: q, limit: 20 }).then(open).catch(function () {});
      }, 220);
    }
    inp.addEventListener('input', query);
    inp.addEventListener('focus', query);
  }

  function closeSuggest() {
    var list = document.querySelectorAll('.suggest');
    Array.prototype.forEach.call(list, function (s) { s.classList.remove('open'); });
  }

  function pathId(tag) {
    var v = document.getElementById('path' + tag).value.trim();
    var m = v.match(/\u2014 ([0-9a-fA-F-]+)$/);
    return m ? m[1] : null;
  }

  function runPath() {
    var a = pathId('A'), b = pathId('B');
    if (!a || !b) { flash('Pick both path endpoints from search.', true); return; }
    if (a === b) { flash('Pick two different nodes.', true); return; }
    NS.api.get('/graph/path', { source: a, target: b, max_depth: 6 }).then(function (path) {
      if (!path || !path.length) { flash('No path found between those nodes.', true); return; }
      pathNodeIds = {}; pathEdgeIds = {};
      addNodes(path);
      for (var i = 0; i < path.length - 1; i++) {
        var pid = 'path-' + path[i].id + '-' + path[i + 1].id;
        if (edgeIds[pid]) continue;
        edgeIds[pid] = true; pathEdgeIds[pid] = true;
        linksArr.push({ id: pid, source: path[i].id, target: path[i + 1].id, label: 'step' });
      }
      path.forEach(function (e) { pathNodeIds[e.id] = true; });
      renderActive();
      flash('Path (' + (path.length - 1) + ' hop' + (path.length - 1 === 1 ? '' : 's') + '): ' +
        path.map(function (e) { return e.label; }).join(' \u2192 '));
    }).catch(function () {});
  }

  function expandAll() {
    var depth = parseInt(document.getElementById('depth').value, 10);
    var frontier = nodesArr.map(function (n) { return n.id; });
    if (!frontier.length) { flash('Seed a node first.', true); return; }
    var seen = {};
    function level() {
      if (depth <= 0) { flash('Expansion complete.'); return; }
      depth--;
      var ids = frontier.slice();
      frontier = [];
      Promise.all(ids.map(function (id) {
        if (seen[id]) return Promise.resolve();
        seen[id] = true;
        return NS.api.get('/graph/entities/' + id + '/neighbors', { depth: 1 }).then(function (frag) {
          addNodes(frag.nodes); addEdges(frag.edges);
          frag.nodes.forEach(function (n) { if (!seen[n.id]) frontier.push(n.id); });
        }).catch(function () {});
      })).then(level);
    }
    flash('Expanding…');
    level();
  }

  function clearAll() {
    nodeTypes = {}; nodesArr = []; linksArr = []; edgeIds = {};
    pathNodeIds = {}; pathEdgeIds = {}; hiddenTypes = {}; hiddenRelations = {};
    if (g2) g2.graphData({ nodes: [], links: [] });
    if (g3) g3.graphData({ nodes: [], links: [] });
    var d = roots.detail; if (d) d.innerHTML = '<div class="hint">Click a node to inspect it.</div>';
    updateLegend(); updateStats(); updateHud();
    flash('Cleared.');
  }

  function toggleRotate() {
    if (!g3) return;
    var btn = document.getElementById('btnRotate');
    var on = !btn.classList.contains('on');
    g3.autoRotate(on);
    btn.classList.toggle('on', on);
    flash(on ? 'Auto-rotate on.' : 'Auto-rotate off.');
  }

  /* ---------------- export / share / stats ---------------- */

  function exportGraph() {
    var opt = window.prompt('Export as:\n1) PNG (image)\n2) JSON (graph data)\n\nEnter 1 or 2 (default 1)', '1');
    if (opt === null) return;
    if (opt === '2') {
      var blob = new Blob([JSON.stringify({ nodes: visibleNodes(), links: visibleLinks() }, null, 2)], { type: 'application/json' });
      downloadBlob(blob, 'knowledge-graph.json');
      return;
    }
    var view = mode === '3d' ? roots.view3d : roots.view2d;
    var canvas = view.querySelector('canvas');
    if (!canvas) { flash('No canvas to export.', true); return; }
    var link = document.createElement('a');
    link.download = 'knowledge-graph.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
    flash('Exported PNG.');
  }

  function downloadBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 100);
  }

  function serializeState() {
    return {
      mode: mode,
      nodes: nodesArr,
      links: linksArr,
      hiddenTypes: Object.keys(hiddenTypes),
      hiddenRelations: Object.keys(hiddenRelations)
    };
  }

  function restoreFromHash() {
    var m = location.hash.match(/[&?]s=([A-Za-z0-9\-_]+)/);
    if (!m) return;
    try {
      var state = JSON.parse(decodeURIComponent(escape(atob(m[1].replace(/-/g, '+').replace(/_/g, '/')))));
      nodesArr = state.nodes || [];
      linksArr = state.links || [];
      nodeTypes = {}; edgeIds = {}; pathNodeIds = {}; pathEdgeIds = {};
      nodesArr.forEach(function (n) { nodeTypes[n.id] = n.entity_type; });
      linksArr.forEach(function (l) { edgeIds[l.id] = true; });
      (state.hiddenTypes || []).forEach(function (t) { hiddenTypes[t] = true; });
      (state.hiddenRelations || []).forEach(function (r) { hiddenRelations[r] = true; });
      if (state.mode) setMode(state.mode);
      renderActive();
      flash('Restored shared graph.');
    } catch (e) { /* invalid payload */ }
  }

  function shareLink() {
    var state = serializeState();
    var b64 = btoa(unescape(encodeURIComponent(JSON.stringify(state)))).replace(/\+/g, '-').replace(/\//g, '_');
    var url = location.href.split('#')[0] + '#/graph&s=' + b64;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () { flash('Share link copied.'); });
        return;
      }
    } catch (e) {}
    flash(url);
  }

  /* ---------------- events ---------------- */

  function onResize() {
    if (mode === '2d' && g2) g2.width(roots.view2d.clientWidth).height(roots.view2d.clientHeight);
    if (mode === '3d' && g3) g3.width(roots.view3d.clientWidth).height(roots.view3d.clientHeight);
  }

  function onKey(e) {
    if (e.target && /input|textarea|select/i.test(e.target.tagName)) return;
    if (e.key === 'Escape') { if (mode === '2d' && g2) g2.zoomToFit(400, 40); }
    if (e.key === 'e' || e.key === 'E') exportGraph();
    if (e.key === 'f' || e.key === 'F') { if (mode === '2d' && g2) g2.zoomToFit(600, 60); if (mode === '3d' && g3) g3.cameraPosition({ x: 240, y: 180, z: 300 }, { x: 0, y: 0, z: 0 }, 400); }
  }

  NS.pages = NS.pages || {};
  NS.pages.graph = { init: init, destroy: destroy };
})(window.ACA);
