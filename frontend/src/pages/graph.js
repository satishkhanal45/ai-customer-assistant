/* Unified knowledge-graph explorer — 2D/3D force-graph, with value search,
   type filters, export, deep-linking, stats and shortcuts. */
(function (NS) {
  'use strict';

  var nodeTypes = {}, nodesArr = [], linksArr = [], edgeIds = {}, pathNodeIds = {}, pathEdgeIds = {};
  var hiddenTypes = {};   // entity_type -> true (hidden in canvas)
  var hiddenRelations = {};
  var mode = '2d';
  var g2 = null, g3 = null;
  var forceParams = { charge: -80, linkDistance: 80, gravity: 0.5 };
  var enginesLoaded = { fg: false, three: false, fg3d: false };
  var roots = {};
  /* What the canvas leaves out by default, and why.

     Measured on this corpus: 479 entities, 507 relations, average degree
     2.12. **116 entities (24%) have no relations at all** and 200 more (42%)
     have exactly one -- so two thirds of the nodes are an unconnected cloud
     or hair on a hub, and one node (Alpinist Studios, degree 78) carries 15%
     of every edge. A force layout draws that faithfully and it is unreadable.

     Neither of these hides information: the counts are shown and both are one
     click away. They change what the canvas leads with. */
  var showIsolated = false;
  var collapseLeaves = true;
  var LEAF_FOLD_MIN = 3;
  var LEGEND_TOP_N = 10;

  var radialMode = false;
  var rootNodeId = null;
  var depthLimit = 2; // default: show 2 hops around selected node

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
      '<section><h4>View</h4><div id="viewOpts"></div></section>' +
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
      var fGravity = document.getElementById('fGravity');
      fCharge.addEventListener('input', function () { forceParams.charge = Number(fCharge.value); applyForce(); });
      fDist.addEventListener('input', function () { forceParams.linkDistance = Number(fDist.value); applyForce(); });
      fGravity.addEventListener('input', function () { forceParams.gravity = Number(fGravity.value); applyForce(); });
      document.getElementById('depth').addEventListener('change', function () {
        depthLimit = parseInt(document.getElementById('depth').value, 10);
        renderActive();
      });
      document.getElementById('searchMode').addEventListener('change', function () {
        document.getElementById('search').value = ''; closeSuggest();
      });
      document.getElementById('btnStats').addEventListener('click', updateStats);
      document.getElementById('btnRadial').addEventListener('click', toggleRadial);
      document.getElementById('btnCluster').addEventListener('click', toggleCluster);
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
      '<div class="gc-group"><button id="btnRadial" class="btn btn-ghost btn-sm">Radial</button></div>' +
      '<div class="gc-group"><button id="btnCluster" class="btn btn-ghost btn-sm">Cluster</button></div>' +
      '<div class="gc-group" id="ctlForce"><label>CHARGE</label><input id="fCharge" type="range" min="-120" max="0" value="-80">' +
      '<label>DIST</label><input id="fDist" type="range" min="20" max="140" value="80">' +
      '<label>GRAV</label><input id="fGravity" type="range" min="0" max="1" step="0.01" value="0.5"></div>';
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
      nodesArr.push({
        id: n.id, name: n.label, entity_type: n.entity_type,
        fact_count: n.fact_count || 0
      });
      changed = true;
    });
    if (changed) { recomputeTopology(); renderActive(); }
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
    if (changed) { recomputeTopology(); renderActive(); }
  }

  /* Degree, and which leaf hangs off which hub.

     Recomputed when the node or link set changes rather than on every lookup:
     `visibleLinks` used to scan `nodesArr` twice per link to find its
     endpoints, which on this graph is ~485,000 comparisons per render for an
     answer that does not change between them. */
  var topo = { degree: {}, leafHub: {}, hubLeaves: {}, byId: {} };

  /* force-graph rewrites `link.source`/`target` from an id to the node object
     once it has laid out, so every read has to tolerate both. */
  function idOf(end) { return (end && end.id) ? end.id : end; }

  function recomputeTopology() {
    var degree = {}, byId = {}, neighbour = {};
    nodesArr.forEach(function (n) { byId[n.id] = n; degree[n.id] = 0; });
    linksArr.forEach(function (l) {
      var a = idOf(l.source), b = idOf(l.target);
      degree[a] = (degree[a] || 0) + 1;
      degree[b] = (degree[b] || 0) + 1;
      neighbour[a] = b;
      neighbour[b] = a;
    });

    var hubLeaves = {}, leafHub = {};
    Object.keys(degree).forEach(function (id) {
      if (degree[id] !== 1) return;
      var hub = neighbour[id];
      if (!hub || hub === id) return;
      (hubLeaves[hub] = hubLeaves[hub] || []).push(id);
    });
    Object.keys(hubLeaves).forEach(function (hub) {
      if (hubLeaves[hub].length < LEAF_FOLD_MIN) { delete hubLeaves[hub]; return; }
      hubLeaves[hub].forEach(function (leaf) { leafHub[leaf] = hub; });
    });

    topo = { degree: degree, leafHub: leafHub, hubLeaves: hubLeaves, byId: byId };
  }

  function isolatedCount() {
    return Object.keys(topo.degree).filter(function (id) {
      return topo.degree[id] === 0;
    }).length;
  }

  function foldedCount() { return Object.keys(topo.leafHub).length; }

  function nodeHidden(n) {
    if (!n) return false;
    if (hiddenTypes[n.entity_type]) return true;
    if (!showIsolated && topo.degree[n.id] === 0) return true;
    if (collapseLeaves && topo.leafHub[n.id]) return true;
    return false;
  }

  function visibleNodes() {
    return nodesArr.filter(function (n) { return !nodeHidden(n); });
  }

  function visibleLinks() {
    return linksArr.filter(function (l) {
      if (hiddenRelations[l.label]) return false;
      return !nodeHidden(topo.byId[idOf(l.source)]) &&
             !nodeHidden(topo.byId[idOf(l.target)]);
    });
  }

  function updateHud() {
    var n = roots.hudNodes, l = roots.hudLinks, m = roots.hudMode;
    /* What is on screen, with what was loaded behind it. "NODES 163/479"
       answers "is the canvas hiding things?" without opening a panel. */
    var vn = visibleNodes().length, vl = visibleLinks().length;
    if (n) n.textContent = vn === nodesArr.length ? vn : vn + '/' + nodesArr.length;
    if (l) l.textContent = vl === linksArr.length ? vl : vl + '/' + linksArr.length;
    if (m) m.textContent = 'MODE ' + mode.toUpperCase();
  }

  function updateLegend() {
    var leg = roots.legend;
    if (!leg) return;
    var counts = {};
    Object.keys(nodeTypes).forEach(function (id) { var t = nodeTypes[id]; counts[t] = (counts[t] || 0) + 1; });
    var relCounts = {};
    linksArr.forEach(function (l) { relCounts[l.label] = (relCounts[l.label] || 0) + 1; });

    /* Ranked by count and truncated. Extraction invents a type whenever none
       of the canonical ones fit, so this corpus carries 67 of them and 18
       have a single member -- an alphabetical list of all 67 is a directory,
       not a legend. The tail stays reachable through the Stats panel. */
    var ranked = Object.keys(counts).sort(function (a, b) {
      return counts[b] - counts[a] || a.localeCompare(b);
    });
    var restTypes = ranked.slice(LEGEND_TOP_N);

    var html = '<div class="legend-group"><b>Types</b></div>';
    ranked.slice(0, LEGEND_TOP_N).forEach(function (t) {
      var hidden = !!hiddenTypes[t];
      html += '<div class="legend-row type-toggle" data-type="' + NS.utils.esc(t) + '" title="' + (hidden ? 'Click to show' : 'Click to hide') + '">' +
        '<span class="dot" style="color:' + colorFor(t) + ';background:' + colorFor(t) + '"></span>' +
        '<span' + (hidden ? ' style="opacity:.35;text-decoration:line-through"' : '') + '>' + NS.utils.esc(t) + ' · ' + counts[t] + '</span></div>';
    });
    if (restTypes.length) {
      var restTotal = restTypes.reduce(function (sum, t) { return sum + counts[t]; }, 0);
      html += '<div class="legend-row legend-rest" title="' + NS.utils.esc(restTypes.join(', ')) + '">' +
        '<span class="dot" style="background:var(--text-faint)"></span>' +
        '<span>' + restTypes.length + ' more types · ' + restTotal + '</span></div>';
    }

    /* Relation types get the same treatment, and need it just as badly. */
    var rankedRels = Object.keys(relCounts).sort(function (a, b) {
      return relCounts[b] - relCounts[a] || a.localeCompare(b);
    });
    var restRels = rankedRels.slice(LEGEND_TOP_N);

    html += '<div class="legend-group"><b>Relations</b></div>';
    rankedRels.slice(0, LEGEND_TOP_N).forEach(function (r) {
      var hidden = !!hiddenRelations[r];
      html += '<div class="legend-row rel-toggle" data-rel="' + NS.utils.esc(r) + '" title="Click to ' + (hidden ? 'show' : 'hide') + '">' +
        '<span class="edge-line"' + (hidden ? ' style="opacity:.25"' : '') + '></span>' +
        '<span' + (hidden ? ' style="opacity:.35;text-decoration:line-through"' : '') + '>' + NS.utils.esc(r) + ' · ' + relCounts[r] + '</span></div>';
    });
    if (restRels.length) {
      var relRest = restRels.reduce(function (sum, r) { return sum + relCounts[r]; }, 0);
      html += '<div class="legend-row legend-rest" title="' + NS.utils.esc(restRels.join(', ')) + '">' +
        '<span class="edge-line"></span>' +
        '<span>' + restRels.length + ' more relations · ' + relRest + '</span></div>';
    }
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

  /* The two defaults, with their counts and a way to undo them.

     A view that silently drops a quarter of the graph is lying; one that says
     "116 unconnected · show" is summarising. The counts double as a read on
     the corpus itself -- a large folded number means extraction produced
     leaves rather than structure, which is a problem in `ingestion`. */
  function updateViewOptions() {
    var host = roots.viewOpts;
    if (!host) return;
    if (!nodesArr.length) { host.innerHTML = '<div class="hint">—</div>'; return; }

    host.innerHTML =
      '<label class="view-opt"><input type="checkbox" id="optIsolated"' +
      (showIsolated ? ' checked' : '') + '>' +
      '<span>Unconnected entities <b>' + isolatedCount() + '</b></span></label>' +
      '<label class="view-opt"><input type="checkbox" id="optCollapse"' +
      (collapseLeaves ? ' checked' : '') + '>' +
      '<span>Fold single-link neighbours <b>' + foldedCount() + '</b></span></label>' +
      '<div class="hint" style="margin-top:6px">Node size shows how many facts ' +
      'an entity carries.</div>';

    var iso = document.getElementById('optIsolated');
    var col = document.getElementById('optCollapse');
    if (iso) iso.addEventListener('change', function () {
      showIsolated = iso.checked; renderActive();
    });
    if (col) col.addEventListener('change', function () {
      collapseLeaves = col.checked; renderActive();
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
    roots.viewOpts = document.getElementById('viewOpts');
    roots.stats = document.getElementById('stats');
    roots.detail = document.getElementById('detail');
  }

  /* ---------------- renderers ---------------- */

  function renderActive() {
    updateHudRefs();
    var visible = visibleNodes();
    var links = visibleLinks();
    var filtered = filterByDepth(visible, links);
    if (mode === '2d') { create2D(); if (g2) g2.graphData({ nodes: filtered.nodes, links: filtered.links }); }
    else { create3D(); if (g3) g3.graphData({ nodes: filtered.nodes, links: filtered.links }); }
    updateHud(); updateLegend(); updateViewOptions();
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

  /* Radius by how much the entity knows, not uniformly.

     226 of 479 entities are connected but hold no facts at all, so a canvas
     of identical dots makes half of them look worth a click they do not
     repay. Square-rooted because the counts are long-tailed, and clamped so
     an empty node is still comfortably clickable. */
  function radiusOf(node) {
    var facts = Number(node.fact_count) || 0;
    if (!(facts > 0)) return 5;
    return Math.min(14, 5 + Math.sqrt(facts) * 1.6);
  }

  function drawNode2D(node, ctx, gs) {
    var cx = node.x, cy = node.y;
    /* force-graph paints a node before the simulation has placed it, so on
       the first frame after a graph is seeded the coordinates are undefined.
       `createRadialGradient` throws on a non-finite argument, and one throw
       inside the render loop takes the whole canvas down. */
    if (!isFinite(cx) || !isFinite(cy)) return;
    var r = radiusOf(node);
    var c = nodeColorOf(node);
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

    /* What was folded away, said out loud. A hub that quietly drops 43
       neighbours is hiding data; one that says "+43" is summarising it. */
    var folded = collapseLeaves && topo.hubLeaves[node.id];
    if (folded && folded.length) {
      var badge = '+' + folded.length;
      var bs = Math.max(8, 9.5 / gs);
      ctx.font = '700 ' + bs + 'px Inter, sans-serif';
      var bw = ctx.measureText(badge).width + 7 / gs;
      var bh = bs + 5 / gs;
      var bx = cx + r * 0.75, by = cy - r - bh * 0.6;
      ctx.fillStyle = c;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, bh / 2);
      else ctx.rect(bx, by, bw, bh);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(badge, bx + bw / 2, by + bh / 2);
    }
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

  function toggleRadial() {
    radialMode = !radialMode;
    document.getElementById('btnRadial').classList.toggle('active', radialMode);
    if (radialMode) {
      rootNodeId = getSelectedNodeId();
      applyRadialLayout(rootNodeId);
    } else {
      // Reset to force-directed layout
      if (mode === '2d' && g2) g2.graphData({ nodes: visibleNodes(), links: visibleLinks() });
      if (mode === '3d' && g3) g3.graphData({ nodes: visibleNodes(), links: visibleLinks() });
    }
    flash(radialMode ? 'Radial layout from root.' : 'Force-directed layout.');
  }

  function toggleCluster() {
    clusterMode = !clusterMode;
    document.getElementById('btnCluster').classList.toggle('active', clusterMode);
    if (clusterMode) {
      applyClusterLayout();
    } else {
      // Reset to force-directed layout
      if (mode === '2d' && g2) g2.graphData({ nodes: visibleNodes(), links: visibleLinks() });
      if (mode === '3d' && g3) g3.graphData({ nodes: visibleNodes(), links: visibleLinks() });
    }
    flash(clusterMode ? 'Community clustering enabled.' : 'Community clustering disabled.');
  }

  function getSelectedNodeId() {
    // Return the id of the currently selected/active node, or the first visible node
    var visible = visibleNodes();
    if (!visible.length) return null;
    // Try to find a node that's been clicked/expanded recently
    // For now, return the first visible node as root
    return visible[0] ? visible[0].id : null;
  }

  function applyRadialLayout(rootId) {
    var nodes = visibleNodes();
    var links = visibleLinks();
    
    if (!rootId) {
      // No root, just do simple circular layout
      applySimpleCircularLayout(nodes);
      return;
    }
    
    // BFS to compute depth and order from root
    var nodeMap = {};
    nodes.forEach(function (n) { nodeMap[n.id] = n; });
    
    var depth = {};
    var order = {};
    var queue = [{ id: rootId, depth: 0, parent: null }];
    var visited = { [rootId]: true };
    var idx = 0;
    
    while (queue.length > 0) {
      var curr = queue.shift();
      var n = nodeMap[curr.id];
      if (!n) continue;
      depth[n.id] = curr.depth;
      order[n.id] = idx++;
      
      // Find neighbors (connected nodes)
      var neighbors = [];
      links.forEach(function (l) {
        if (l.source === curr.id && !visited[l.target]) {
          neighbors.push(l.target);
          visited[l.target] = true;
        }
        if (l.target === curr.id && !visited[l.source]) {
          neighbors.push(l.source);
          visited[l.source] = true;
        }
      });
      
      neighbors.forEach(function (nid) {
        queue.push({ id: nid, depth: curr.depth + 1, parent: curr.id });
      });
    }
    
    // Apply radial positions
    var maxDepth = 0;
    for (var d in depth) { if (depth[d] > maxDepth) maxDepth = depth[d]; }
    var radiusFactor = 30; // pixels per depth level
    var angleStep = 2 * Math.PI / Math.max(nodes.length, 1);
    
    nodes.forEach(function (n) {
      var d = depth[n.id] !== undefined ? depth[n.id] : 0;
      var o = order[n.id] !== undefined ? order[n.id] : 0;
      var radius = d * radiusFactor;
      var theta = o * angleStep;
      n.x = radius * Math.cos(theta);
      n.y = radius * Math.sin(theta);
    });
    
    if (mode === '2d' && g2) {
      g2.graphData({ nodes: nodes, links: links });
    }
    if (mode === '3d' && g3) {
      g3.graphData({ nodes: nodes, links: links });
    }
  }

  function applySimpleCircularLayout(nodes) {
    var angleStep = 2 * Math.PI / Math.max(nodes.length, 1);
    nodes.forEach(function (n, i) {
      var theta = i * angleStep;
      n.x = 200 * Math.cos(theta);
      n.y = 200 * Math.sin(theta);
    });
    if (mode === '2d' && g2) g2.graphData({ nodes: nodes, links: links });
    if (mode === '3d' && g3) g3.graphData({ nodes: nodes, links: links });
  }

  function applyClusterLayout() {
    var nodes = visibleNodes();
    var links = visibleLinks();
    
    // Group nodes by entity_type
    var groups = {};
    nodes.forEach(function (n) {
      var t = n.entity_type;
      if (!groups[t]) groups[t] = [];
      groups[t].push(n);
    });
    
    // Create cluster nodes for each group
    var clusterNodes = [];
    var clusterLinks = [];
    var nodeIdMap = {};
    
    var xOffset = 0;
    var groupIdx = 0;
    
    for (var type in groups) {
      var groupNodes = groups[type];
      var clusterId = 'cluster-' + groupIdx;
      var centerX = (groupIdx % 5) * 150;
      var centerY = Math.floor(groupIdx / 5) * 150;
      
      // Add cluster center node
      clusterNodes.push({
        id: clusterId,
        name: type + ' (' + groupNodes.length + ')',
        entity_type: type,
        x: centerX,
        y: centerY,
        _isCluster: true,
        _memberIds: groupNodes.map(function (n) { return n.id; })
      });
      
      // Add links from cluster center to member nodes
      groupNodes.forEach(function (n) {
        clusterLinks.push({
          id: 'cluster-' + n.id + '-' + clusterId,
          source: clusterId,
          target: n.id,
          label: n.entity_type
        });
        nodeIdMap[n.id] = n.id;
      });
      
      groupIdx++;
    }
    
    // Combine cluster nodes with non-cluster nodes
    var allNodes = [...clusterNodes, ...nodes];
    var allLinks = [...clusterLinks, ...links];
    
    // Update node positions - cluster centers at grid positions, members at their force positions
    allNodes.forEach(function (n) {
      if (n._isCluster) return;
      // Keep original node positions or assign based on cluster
      if (!n.x && !n.y) {
        n.x = Math.random() * 400 - 200;
        n.y = Math.random() * 400 - 200;
      }
    });
    
    if (mode === '2d' && g2) g2.graphData({ nodes: allNodes, links: allLinks });
    if (mode === '3d' && g3) g3.graphData({ nodes: allNodes, links: allLinks });
  }

  function filterByDepth(nodes, links) {
    if (depthLimit <= 0 || !rootNodeId) return { nodes: nodes, links: links };
    
    var nodeMap = {};
    nodes.forEach(function (n) { nodeMap[n.id] = n; });
    
    // BFS to compute depth from root
    var depth = {};
    var queue = [{ id: rootNodeId, depth: 0 }];
    var visited = { [rootNodeId]: true };
    
    while (queue.length > 0) {
      var curr = queue.shift();
      var d = depth[curr.id];
      
      if (d >= depthLimit) continue; // don't explore beyond depth limit
      
      links.forEach(function (l) {
        var neighborId = null;
        if (l.source === curr.id) neighborId = l.target;
        if (l.target === curr.id) neighborId = l.source;
        
        if (neighborId && !visited[neighborId] && nodeMap[neighborId]) {
          visited[neighborId] = true;
          depth[neighborId] = d + 1;
          queue.push({ id: neighborId });
        }
      });
    }
    
    // Filter nodes and links
    var filteredNodes = nodes.filter(function (n) { return depth[n.id] !== undefined && depth[n.id] <= depthLimit; });
    var filteredLinks = links.filter(function (l) {
      return depth[l.source] !== undefined && depth[l.source] <= depthLimit &&
             depth[l.target] !== undefined && depth[l.target] <= depthLimit;
    });
    
    return { nodes: filteredNodes, links: filteredLinks };
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
