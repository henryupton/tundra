/*
 * tundra docs: the catalogue in index.html as pages and a graph. Graphs are laid out
 * in the browser by Graphviz (Viz.js), so the page needs no server and no network.
 * Arrows run from a grantee to what it's granted: user -> role -> inherited role ->
 * warehouse or database.
 */
(function () {
  'use strict';

  const C = JSON.parse(document.getElementById('tundra-catalogue').textContent);
  const NODES = C.nodes;
  const EDGES = C.edges;
  const LIVE = C.metadata.live;

  // One fixed colour per family, so a family looks the same in every spec. Custom
  // families take yellow then red, and any after that the functional hue with a doubled
  // border. Object types also differ by shape, so the greys stay apart.
  const COLOURS = {
    people: '#6250d6',
    service: '#e87ba4',
    system: '#2a78d6',
    functional: '#1baf7a',
    managed: '#52514e',
    warehouse: '#008300',
    database: '#eb6834',
    integration: '#8a8984',
    external_volume: '#8a8984',
  };
  const CUSTOM_COLOURS = ['#eda100', '#e34948'];
  const MUTED = '#52514e';
  const DRIFT = '#d03b3b';
  const DOUBLE = {};
  const FAMILY = {};
  C.families.filter((f) => f.custom).forEach((f, i) => {
    COLOURS[f.id] = CUSTOM_COLOURS[i] || COLOURS.functional;
    if (i >= CUSTOM_COLOURS.length) DOUBLE[f.id] = true;
  });
  C.families.forEach((f) => { FAMILY[f.id] = f; });

  const SHAPES = { warehouse: 'box3d', database: 'cylinder', integration: 'hexagon', external_volume: 'folder' };
  const OBJECT_TYPES = { warehouse: 1, database: 1, integration: 1, external_volume: 1 };
  const TYPE_LABEL = {
    role: 'Role',
    database_role: 'Database role',
    application_role: 'Application role',
    user: 'User',
    warehouse: 'Warehouse',
    database: 'Database',
    integration: 'Integration',
    external_volume: 'External volume',
  };
  // Nav sections, keyed as metadata.counts counts them, so a section's length and the
  // count shown anywhere else are the same number
  const SECTIONS = [
    ['roles', 'Roles'],
    ['managed_roles', 'Database and application roles'],
    ['users', 'Users'],
    ['warehouses', 'Warehouses'],
    ['databases', 'Databases'],
    ['integrations', 'Integrations'],
    ['external_volumes', 'External volumes'],
  ];
  const COUNTED_AS = {
    role: 'roles',
    database_role: 'managed_roles',
    application_role: 'managed_roles',
    user: 'users',
    warehouse: 'warehouses',
    database: 'databases',
    integration: 'integrations',
    external_volume: 'external_volumes',
  };
  const COUNTS = C.metadata.counts;
  const STATUS = {
    both: { icon: '✓', label: 'applied', text: 'in the spec and on Snowflake' },
    spec_only: { icon: '○', label: 'not applied', text: 'in the spec, not on Snowflake' },
    live_only: { icon: '!', label: 'drift', text: 'on Snowflake, not in the spec' },
  };
  const EDGE_TEXT = {
    spec: 'in the spec',
    builtin: 'Snowflake built in',
    owner: 'owner',
    both: STATUS.both.text,
    spec_only: STATUS.spec_only.text,
    live_only: STATUS.live_only.text,
  };

  // Indexes

  const OUT = {};
  const IN = {};
  const push = (map, key, value) => (map[key] || (map[key] = [])).push(value);
  EDGES.forEach((e) => { push(OUT, e.source, e); push(IN, e.target, e); });
  const GROUP_OF = {};
  C.user_groups.forEach((g) => g.users.forEach((u) => { GROUP_OF[u] = g; }));

  // Helpers

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const prefixOf = (id) => id.slice(0, id.indexOf(':'));
  const tint = (hex, alpha) => hex + Math.round(alpha * 255).toString(16).padStart(2, '0');
  const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : (many || one + 's')}`;
  const by = (key) => (a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0);
  const sortName = (id) => (NODES[id] ? NODES[id].name : id).replace(/"/g, '').toLowerCase();
  const byName = by(sortName);

  function href(id) {
    const i = id.indexOf(':');
    return '#/' + id.slice(0, i) + '/' + encodeURIComponent(id.slice(i + 1));
  }
  const graphHref = (id) => '#/graph/' + href(id).slice(2);
  const nameOf = (id) => (NODES[id] ? NODES[id].name : id);
  const colourOf = (node) => COLOURS[node.family] || MUTED;
  // Snowflake owns database and application roles, so no spec ever defines one
  const MANAGED = { database_role: 1, application_role: 1 };
  const undefinedHere = (node) => !node.defined && !MANAGED[node.type];

  function swatch(node) {
    const c = colourOf(node);
    return `<span class="dot${undefinedHere(node) ? ' undefined' : ''}" style="border-color:${c};background:${tint(c, 0.22)}"></span>`;
  }
  function link(id) {
    const node = NODES[id];
    if (!node) return esc(id);
    return `<a class="entry" href="${href(id)}">${swatch(node)}${esc(node.name)}</a>`;
  }
  function badge(edge) {
    if (edge.origin === 'builtin') return '<span class="badge builtin" title="Snowflake sets this up itself">built in</span>';
    if (!LIVE || !edge.status) return '';
    const s = STATUS[edge.status];
    let html = `<span class="badge ${edge.status}" title="${esc(s.text)}">${s.icon} ${s.label}</span>`;
    if (edge.status === 'both' && edge.missing && edge.missing.length) {
      html += ` <span class="privs">missing ${esc(edge.missing.join(', '))}</span>`;
    }
    if (edge.extra && edge.extra.length && edge.status !== 'live_only') {
      html += ` <span class="privs">also holds ${esc(edge.extra.join(', '))}</span>`;
    }
    return html;
  }
  const privileges = (edge) => (edge.privileges && edge.privileges.length
    ? `<span class="privs">${esc(edge.privileges.join(', '))}</span>` : '');

  function edgesOut(id, type, targetType) {
    return (OUT[id] || []).filter((e) => e.type === type && (!targetType || prefixOf(e.target) === targetType));
  }
  function edgesIn(id, type, sourceType) {
    return (IN[id] || []).filter((e) => e.type === type && (!sourceType || prefixOf(e.source) === sourceType));
  }

  // Everything reachable along member edges (and usage edges at the end, going down)
  function walk(start, dir, types) {
    const seen = {};
    const found = [];
    const stack = [start];
    while (stack.length) {
      const id = stack.pop();
      ((dir === 'down' ? OUT[id] : IN[id]) || []).forEach((e) => {
        if (types.indexOf(e.type) < 0) return;
        const next = dir === 'down' ? e.target : e.source;
        if (next !== start && !seen[next]) {
          seen[next] = true;
          found.push(next);
          stack.push(next);
        }
      });
    }
    return found;
  }

  function list(items, render, empty, single) {
    if (!items.length) return `<p class="muted">${empty}</p>`;
    return `<ul class="links${single ? ' single' : ''}">${items.map((i) => `<li>${render(i)}</li>`).join('')}</ul>`;
  }
  const edgeItem = (side) => (e) => `${link(e[side])} ${privileges(e)} ${badge(e)}`;

  function metaTable(meta) {
    const keys = Object.keys(meta || {});
    if (!keys.length) return '';
    const rows = keys.map((k) => {
      const v = meta[k];
      const shown = v !== null && typeof v === 'object' ? `<code>${esc(JSON.stringify(v, null, 1))}</code>` : esc(v);
      return `<tr><th>${esc(k)}</th><td>${shown}</td></tr>`;
    }).join('');
    return `<h2>Meta</h2><table class="props">${rows}</table>`;
  }
  function props(rows) {
    const shown = rows.filter((r) => r[1] !== null && r[1] !== undefined && r[1] !== '');
    if (!shown.length) return '';
    return `<table class="props">${shown.map((r) => `<tr><th>${r[0]}</th><td>${r[1]}</td></tr>`).join('')}</table>`;
  }
  function chips(node) {
    const out = [`<span class="chip">${swatch(node)}${esc((FAMILY[node.family] || {}).label || node.family)}</span>`];
    if (MANAGED[node.type]) out.push(`<span class="chip">${TYPE_LABEL[node.type]}</span>`);
    if (undefinedHere(node)) out.push('<span class="chip">Not defined in the spec</span>');
    if (node.missing) out.push('<span class="chip" style="color:#d03b3b">Missing on Snowflake</span>');
    return `<div class="chips">${out.join('')}</div>`;
  }
  function heading(node) {
    const comment = node.comment ? `<p class="comment">${esc(node.comment)}</p>` : '';
    return `<h1>${esc(node.name)}</h1>${chips(node)}${comment}`;
  }

  // Navigation

  const nav = document.getElementById('nav');
  const search = document.getElementById('search');

  function buildNav() {
    const bySection = {};
    Object.keys(NODES).forEach((id) => push(bySection, COUNTED_AS[NODES[id].type], id));
    nav.innerHTML = SECTIONS.filter((s) => bySection[s[0]]).map(([section, label]) => {
      const ids = bySection[section].slice().sort(byName);
      const items = ids.map((id) => `<li data-name="${esc(NODES[id].name.toLowerCase())}"><a href="${href(id)}" data-id="${esc(id)}">${swatch(NODES[id])}${esc(NODES[id].name)}</a></li>`).join('');
      const open = ids.length <= 60 || section === 'roles' ? ' open' : '';
      return `<details${open} data-section="${section}"><summary>${label}<span class="count">${ids.length}</span></summary><ul>${items}</ul></details>`;
    }).join('') + '<div class="empty" hidden>Nothing matches.</div>';
  }

  function filterNav() {
    const q = search.value.trim().toLowerCase();
    let any = false;
    nav.querySelectorAll('details').forEach((section) => {
      let shown = 0;
      section.querySelectorAll('li').forEach((li) => {
        const hit = !q || li.dataset.name.indexOf(q) >= 0;
        li.hidden = !hit;
        if (hit) shown++;
      });
      section.hidden = shown === 0;
      section.querySelector('.count').textContent = shown;
      if (q) section.open = true;
      any = any || shown > 0;
    });
    nav.querySelector('.empty').hidden = any;
  }

  search.addEventListener('input', filterNav);
  search.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      const first = nav.querySelector('li:not([hidden]) a');
      if (first) location.hash = first.getAttribute('href');
    }
    if (ev.key === 'Escape') { search.value = ''; filterNav(); search.blur(); }
  });
  document.addEventListener('keydown', (ev) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((ev.target.tagName || ''));
    if (ev.key === '/' && !typing) { ev.preventDefault(); search.focus(); search.select(); }
  });

  // Pages

  const main = document.getElementById('main');

  function overview() {
    const stat = (n, label, target) => `<a class="stat link" href="${target}"><b>${n.toLocaleString()}</b><span>${label}</span></a>`;
    const firstOf = (section) => {
      const ids = Object.keys(NODES).filter((id) => COUNTED_AS[NODES[id].type] === section).sort(byName);
      return ids.length ? href(ids[0]) : '#/';
    };
    const stats = [
      stat(COUNTS.roles, 'roles', firstOf('roles')),
      COUNTS.managed_roles ? stat(COUNTS.managed_roles, 'database and application roles', firstOf('managed_roles')) : '',
      stat(COUNTS.people, 'people', firstOf('users')),
      stat(COUNTS.service_users, 'service users', firstOf('users')),
      stat(C.user_groups.length, 'distinct role sets among people', '#/graph'),
      stat(COUNTS.warehouses, 'warehouses', firstOf('warehouses')),
      stat(COUNTS.databases, 'databases', firstOf('databases')),
      COUNTS.integrations ? stat(COUNTS.integrations, 'integrations', firstOf('integrations')) : '',
      COUNTS.external_volumes ? stat(COUNTS.external_volumes, 'external volumes', firstOf('external_volumes')) : '',
    ].filter(Boolean);

    const generated = new Date(C.metadata.generated_at);
    let html = `<div class="page"><h1>${esc(C.metadata.spec)}</h1>
      <div class="chips"><span class="chip">${LIVE ? 'Spec and live grants' : 'Spec only'}</span>
      <span class="chip">Generated ${esc(generated.toLocaleString())}</span>
      <span class="chip">tundra ${esc(C.metadata.tundra_version)}</span></div>
      <p class="comment">${LIVE
        ? 'Read from the spec and from the account\'s grants, as <code>tundra run</code> reads them. Each membership and usage grant is marked applied, not applied (in the spec only) or drift (on Snowflake only).'
        : 'Read from the spec alone, with <code>member_of: ["*"]</code> and wildcards resolved as <code>tundra run</code> resolves them. <code>tundra docs generate --live</code> adds what\'s on Snowflake.'}</p>
      <div class="grid">${stats.join('')}</div>
      <p><a class="button" href="#/graph">Open the graph</a></p>`;

    if (LIVE) {
      const rows = (status) => EDGES.filter((e) => e.status === status && e.type !== 'owner');
      const table = (items) => (items.length
        ? `<table class="drift">${items.map((e) => `<tr><td>${link(e.source)}</td><td>→</td><td>${link(e.target)}</td><td>${privileges(e)}</td></tr>`).join('')}</table>`
        : '<p class="muted">None.</p>');
      const missing = Object.keys(NODES).filter((id) => NODES[id].missing).sort(byName);
      html += `<h2>On Snowflake, not in the spec <span class="muted">(${rows('live_only').length})</span></h2>
        <p class="note">What <code>tundra run</code> would revoke.</p>${table(rows('live_only'))}
        <h2>In the spec, not on Snowflake <span class="muted">(${rows('spec_only').length})</span></h2>
        <p class="note">What <code>tundra run</code> would grant.</p>${table(rows('spec_only'))}
        <h2>Missing on Snowflake <span class="muted">(${missing.length})</span></h2>${list(missing, link, 'None.')}`;
    }

    const groups = C.user_groups.filter((g) => g.users.length > 1);
    if (groups.length) {
      html += `<h2>People who hold the same roles</h2>${list(groups, (g) => `<span>${plural(g.users.length, 'person', 'people')}:</span> ${g.roles.length ? g.roles.map(link).join(' ') : '<span class="muted">no roles</span>'}`, '', true)}`;
    }
    const undefinedRoles = Object.keys(NODES).filter((id) => NODES[id].type === 'role' && !NODES[id].defined).sort(byName);
    if (undefinedRoles.length) {
      html += `<h2>Roles the spec refers to but doesn't define</h2>${list(undefinedRoles, link, '')}`;
    }
    return html + '</div>';
  }

  function lineage(id) {
    return `<h2>Lineage <a class="button" style="float:right;margin-top:-6px" href="${graphHref(id)}">Open in the graph</a></h2>
      <p class="note">Everything that reaches it and everything it reaches. Hover to trace, click to pin, pinch or ⌘-scroll to zoom, drag to pan.</p>
      <div class="lineage"><div class="graph" data-focus="${esc(id)}"></div></div>`;
  }

  function rolePage(id, node) {
    const inherits = edgesOut(id, 'member');
    const direct = {};
    inherits.forEach((e) => { direct[e.target] = true; });
    const through = walk(id, 'down', ['member']).filter((r) => !direct[r]).sort(byName);
    const inheritedBy = edgesIn(id, 'member', 'role');
    const users = edgesIn(id, 'member', 'user');
    const directUsers = {};
    users.forEach((e) => { directUsers[e.source] = true; });
    const indirectUsers = walk(id, 'up', ['member']).filter((u) => prefixOf(u) === 'user' && !directUsers[u]).sort(byName);
    const defaultFor = Object.keys(NODES).filter((u) => NODES[u].default_role === id).sort(byName);

    let html = `<div class="page">${heading(node)}${props([
      ['Owner', node.owner ? link(node.owner) : null],
      ['Default role of', defaultFor.length ? defaultFor.map(link).join(' ') : null],
    ])}${lineage(id)}`;

    html += `<h2>Inherits <span class="muted">(member of)</span></h2>
      ${list(inherits.sort(by((e) => sortName(e.target))), edgeItem('target'), 'Nothing.')}`;
    if (through.length) html += `<h3>Through those, also inherits</h3>${list(through, link, '')}`;

    html += `<h2>Inherited by</h2><h3>Roles</h3>
      ${list(inheritedBy.sort(by((e) => sortName(e.source))), edgeItem('source'), 'No role inherits it.')}
      <h3>Users who hold it</h3>
      ${list(users.sort(by((e) => sortName(e.source))), edgeItem('source'), 'No user holds it directly.')}`;
    if (indirectUsers.length) html += `<h3>Users who hold it through other roles <span class="muted">(${indirectUsers.length})</span></h3>${list(indirectUsers, link, '')}`;

    html += `<h2>Privileges</h2>${privilegeSections(id, node)}`;
    return html + metaTable(node.meta) + '</div>';
  }

  function privilegeSections(id, node) {
    const parts = [];
    const usage = (type, title, ids) => {
      const edges = edgesOut(id, 'usage', type);
      const listed = {};
      edges.forEach((e) => { listed[e.target] = e; });
      (ids || []).forEach((t) => { if (!listed[t]) listed[t] = { target: t, privileges: [] }; });
      const items = Object.keys(listed).sort(byName).map((t) => listed[t]);
      if (items.length) parts.push(`<h3>${title}</h3>${list(items, edgeItem('target'), '')}`);
    };
    usage('warehouse', 'Warehouses', node.warehouses);

    const dbEdges = {};
    edgesOut(id, 'usage', 'database').forEach((e) => { dbEdges[e.target] = e; });
    const dbs = (node.privileges || {}).databases || {};
    const dbRows = [];
    ['read', 'write'].forEach((access) => (dbs[access] || []).forEach((entry) => {
      const e = dbEdges[entry.database];
      delete dbEdges[entry.database];
      dbRows.push(`${link(entry.database)} <span class="badge note">${access}</span> ${e ? privileges(e) + ' ' + badge(e) : ''}`);
    }));
    Object.keys(dbEdges).sort(byName).forEach((t) => dbRows.push(`${link(t)} ${privileges(dbEdges[t])} ${badge(dbEdges[t])}`));
    if (dbRows.length) parts.push(`<h3>Databases</h3>${list(dbRows, (r) => r, '')}`);

    const entries = (title, section) => {
      if (!section) return;
      ['read', 'write'].forEach((access) => {
        const items = section[access] || [];
        if (items.length) {
          parts.push(`<h3>${title} <span class="muted">${access}</span></h3>${list(items, (entry) => `<code>${esc(entry.name)}</code>${entry.pattern ? ' <span class="badge pattern" title="Matches every object the glob names, now and in future">wildcard</span>' : ''}`, '')}`);
        }
      });
    };
    const privs = node.privileges || {};
    entries('Schemas', privs.schemas);
    Object.keys(privs).filter((k) => k !== 'databases' && k !== 'schemas').forEach((k) => entries(k[0].toUpperCase() + k.slice(1), privs[k]));
    usage('integration', 'Integrations', node.integrations);
    usage('external_volume', 'External volumes', node.external_volumes);
    if ((node.account_privileges || []).length) {
      parts.push(`<h3>Account privileges</h3>${list(node.account_privileges, (p) => `<code>${esc(p)}</code>`, '')}`);
    }
    const owns = node.owns || {};
    Object.keys(owns).forEach((k) => {
      parts.push(`<h3>Owns ${esc(k)}</h3>${list(owns[k], (entry) => `<code>${esc(entry.name)}</code>`, '')}`);
    });
    const owned = edgesOut(id, 'owner');
    if (owned.length) parts.push(`<h3>Owner of</h3>${list(owned.map((e) => e.target).sort(byName), link, '')}`);
    if (!parts.length) {
      return node.defined
        ? '<p class="muted">No privileges of its own in the spec. It has what it inherits.</p>'
        : '<p class="muted">Managed outside the spec.</p>';
    }
    return parts.join('');
  }

  function userPage(id, node) {
    const roles = edgesOut(id, 'member');
    const direct = {};
    roles.forEach((e) => { direct[e.target] = true; });
    const all = walk(id, 'down', ['member']);
    const through = all.filter((r) => !direct[r]).sort(byName);
    const reach = {};
    all.forEach((r) => edgesOut(r, 'usage').forEach((e) => { push(reach, e.target, r); }));
    const reachable = Object.keys(reach).sort(byName);
    const group = GROUP_OF[id];
    const peers = group ? group.users.filter((u) => u !== id) : [];
    const tags = Object.keys(node.tags || {}).map((k) => `<code>${esc(k)}</code> = ${esc(node.tags[k])}`).join('<br>');
    const parameters = node.parameters ? Object.keys(node.parameters).map((k) => `<code>${esc(k)}</code> = ${esc(node.parameters[k])}`).join('<br>') : null;

    let html = `<div class="page">${heading(node)}${props([
      ['Default role', node.default_role ? link(node.default_role) : null],
      ['Default warehouse', node.default_warehouse ? link(node.default_warehouse) : null],
      ['Default namespace', node.default_namespace ? `<code>${esc(node.default_namespace)}</code>` : null],
      ['Default secondary roles', node.default_secondary_roles ? esc(node.default_secondary_roles.join(', ')) : null],
      ['Can log in', node.can_login === false ? 'No' : node.can_login ? 'Yes' : null],
      ['Owner', node.owner ? link(node.owner) : null],
      ['Tags', tags || null],
      ['Parameters', parameters],
    ])}${lineage(id)}`;

    html += `<h2>Roles</h2>${list(roles.sort(by((e) => sortName(e.target))), edgeItem('target'), 'No roles.')}`;
    if (through.length) html += `<h3>Through those, also holds</h3>${list(through, link, '')}`;
    html += `<h2>Reaches</h2>${list(reachable, (t) => `${link(t)} <span class="privs">through ${reach[t].sort(byName).map((r) => esc(nameOf(r))).join(', ')}</span>`, 'No warehouses or databases.')}`;
    if (peers.length) html += `<h2>Holds exactly the same roles as</h2>${list(peers.sort(byName), link, '')}`;
    return html + metaTable(node.meta) + '</div>';
  }

  function objectPage(id, node) {
    const users = edgesIn(id, 'usage');
    const direct = {};
    users.forEach((e) => { direct[e.source] = true; });
    const reachers = walk(id, 'up', ['usage', 'member']);
    const roles = reachers.filter((r) => prefixOf(r) === 'role' && !direct[r]).sort(byName);
    const people = reachers.filter((r) => prefixOf(r) === 'user').sort(byName);
    const owners = edgesIn(id, 'owner').map((e) => e.source);

    let html = `<div class="page">${heading(node)}${props([
      ['Size', node.size ? esc(node.size) : null],
      ['Shared', node.shared === undefined ? null : node.shared ? 'Yes, imported from a share' : 'No'],
      ['Category', node.category ? esc(node.category) : null],
      ['Storage provider', node.storage_provider ? esc(node.storage_provider) : null],
      ['Owner', owners.length ? owners.map(link).join(' ') : null],
    ])}${lineage(id)}`;

    html += `<h2>Roles granted it</h2>${list(users.sort(by((e) => sortName(e.source))), edgeItem('source'), 'No role is granted it.')}`;
    if (roles.length) html += `<h3>Roles that reach it by inheritance <span class="muted">(${roles.length})</span></h3>${list(roles, link, '')}`;
    html += `<h3>Users who reach it <span class="muted">(${people.length})</span></h3>${list(people, link, 'Nobody.')}`;

    if (node.type === 'database') {
      const inside = [];
      Object.keys(NODES).sort(byName).forEach((rid) => {
        const r = NODES[rid];
        const sections = Object.assign({}, r.privileges || {});
        delete sections.databases;
        const found = [];
        Object.keys(sections).forEach((section) => ['read', 'write'].forEach((access) => {
          (sections[section][access] || []).forEach((entry) => {
            if (entry.database === id) found.push(`<code>${esc(entry.name)}</code> <span class="privs">${esc(section)} ${access}</span>`);
          });
        }));
        Object.keys(r.owns || {}).forEach((section) => (r.owns[section] || []).forEach((entry) => {
          if (entry.database === id && section !== 'databases') found.push(`<code>${esc(entry.name)}</code> <span class="privs">owns ${esc(section)}</span>`);
        }));
        if (found.length) inside.push(`${link(rid)} <span>${found.join(', ')}</span>`);
      });
      html += `<h2>Privileges inside it</h2>${list(inside, (r) => r, 'No role has schema, table or task privileges in it.', true)}`;
    }
    return html + metaTable(node.meta) + '</div>';
  }

  function nodePage(id) {
    const node = NODES[id];
    if (node.type === 'user') return userPage(id, node);
    if (OBJECT_TYPES[node.type]) return objectPage(id, node);
    return rolePage(id, node);
  }

  // Graph model: catalogue nodes and edges as drawn, people optionally grouped

  const graphOptions = { group: true, owners: false, objects: true };

  function nodeView(node) {
    const id = node.id;
    let sub;
    if (node.type === 'user') {
      sub = node.family === 'service' ? 'service user' : 'person';
      if (node.can_login === false) sub += " · can't log in";
    } else if (node.type === 'warehouse') {
      sub = node.size ? `warehouse · ${node.size}` : 'warehouse';
    } else if (node.type === 'database') {
      sub = node.shared ? 'shared database' : 'database';
    } else if (node.type === 'integration') {
      sub = node.category ? `integration · ${node.category}` : 'integration';
    } else if (node.type === 'external_volume') {
      sub = 'external volume';
    } else if (node.type !== 'role') {
      sub = TYPE_LABEL[node.type].toLowerCase();
    } else {
      sub = plural(edgesIn(id, 'member', 'user').length, 'user');
    }
    if (undefinedHere(node)) sub += ' · not in the spec';
    if (node.missing) sub += ' · missing on Snowflake';
    return {
      id: id,
      ref: id,
      label: node.name,
      sub: sub,
      family: node.family,
      type: node.type,
      dashed: undefinedHere(node),
      size: edgesIn(id, 'member', 'user').length,
    };
  }

  function groupView(group) {
    const roles = group.roles.map(nameOf);
    let sub = roles.slice(0, 3).join(' + ') + (roles.length > 3 ? ` + ${roles.length - 3} more` : '');
    if (!roles.length) sub = 'no roles';
    const single = group.users.length === 1;
    return {
      id: group.id,
      ref: single ? group.users[0] : null,
      label: single ? nameOf(group.users[0]) : plural(group.users.length, 'person', 'people'),
      sub: single ? 'person' : sub,
      family: 'people',
      type: 'group',
      dashed: false,
      size: group.users.length,
      members: group.users,
    };
  }

  function graphModel(options) {
    const drawnAs = {};
    const nodes = {};
    Object.keys(NODES).forEach((id) => {
      const node = NODES[id];
      if (!options.objects && OBJECT_TYPES[node.type] && id !== options.focus) return;
      const group = GROUP_OF[id];
      if (options.group && group && node.family === 'people') {
        drawnAs[id] = group.id;
        if (!nodes[group.id]) nodes[group.id] = groupView(group);
        return;
      }
      drawnAs[id] = id;
      nodes[id] = nodeView(node);
    });

    const merged = {};
    EDGES.forEach((e) => {
      if (e.type === 'owner' && !options.owners) return;
      const source = drawnAs[e.source];
      const target = drawnAs[e.target];
      if (!source || !target) return;
      const key = `${source}\u0000${target}\u0000${e.type}`;
      (merged[key] || (merged[key] = { source: source, target: target, type: e.type, edges: [] })).edges.push(e);
    });
    let edges = Object.keys(merged).map((k) => summarise(merged[k]));

    const focus = options.focus ? drawnAs[options.focus] : null;
    if (focus) {
      const keep = {};
      keep[focus] = true;
      const out = {};
      const inn = {};
      edges.forEach((e) => { push(out, e.source, e); push(inn, e.target, e); });
      [['down', out, 'target'], ['up', inn, 'source']].forEach(([, index, side]) => {
        const stack = [focus];
        const seen = {};
        while (stack.length) {
          const id = stack.pop();
          (index[id] || []).forEach((e) => {
            const next = e[side];
            if (!seen[next]) { seen[next] = true; keep[next] = true; stack.push(next); }
          });
        }
      });
      Object.keys(nodes).forEach((id) => { if (!keep[id]) delete nodes[id]; });
      edges = edges.filter((e) => keep[e.source] && keep[e.target]);
    }
    return { nodes: nodes, edges: edges, focus: focus };
  }

  // One drawn edge for one or more catalogue edges (a group's people share one)
  function summarise(merged) {
    const statuses = {};
    merged.edges.forEach((e) => { statuses[e.status || e.origin] = (statuses[e.status || e.origin] || 0) + 1; });
    let kind = 'spec';
    if (merged.type === 'owner') kind = 'owner';
    else if (statuses.live_only) kind = 'live_only';
    else if (statuses.spec_only) kind = 'spec_only';
    else if (statuses.builtin) kind = 'builtin';
    else if (statuses.both) kind = 'both';
    const privs = [];
    merged.edges.forEach((e) => (e.privileges || []).forEach((p) => { if (privs.indexOf(p) < 0) privs.push(p); }));
    const missing = [];
    merged.edges.forEach((e) => (e.missing || []).forEach((p) => { if (missing.indexOf(p) < 0) missing.push(p); }));
    return {
      source: merged.source,
      target: merged.target,
      type: merged.type,
      kind: kind,
      count: merged.edges.length,
      statuses: statuses,
      privileges: privs,
      missing: kind === 'both' ? missing : [],
    };
  }

  // DOT

  const quote = (s) => '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';

  function dot(model) {
    const ids = Object.keys(model.nodes);
    const dotId = {};
    ids.forEach((id, i) => { dotId[id] = 'n' + i; });
    const connected = {};
    model.edges.forEach((e) => { connected[e.source] = connected[e.target] = true; });
    const lines = [
      'digraph tundra {',
      ' graph [rankdir=LR, newrank=true, splines=spline, nodesep=0.14, ranksep=1.2, fontname="Helvetica", bgcolor="transparent", pad=0.4, outputorder=edgesfirst];',
      ' node [shape=box, style="rounded,filled", fontname="Helvetica", fontcolor="#0b0b0b", penwidth=1.2, margin="0.12,0.06"];',
      ' edge [arrowsize=0.6];',
    ];
    ids.filter((id) => connected[id]).forEach((id) => lines.push(nodeLine(model.nodes[id], dotId[id], id === model.focus)));
    const isolated = ids.filter((id) => !connected[id]);
    if (isolated.length) {
      lines.push(' subgraph cluster_isolated { label="no grants either way"; style="rounded,dashed"; color="#c3c2b7"; fontcolor="#52514e"; fontsize=11; fontname="Helvetica";');
      isolated.forEach((id) => lines.push(nodeLine(model.nodes[id], dotId[id], id === model.focus)));
      lines.push(' }');
    }
    model.edges.forEach((e, i) => lines.push(edgeLine(e, model, dotId, i)));
    lines.push('}');
    const nodeOf = {};
    ids.forEach((id) => { nodeOf[dotId[id]] = id; });
    return { source: lines.join('\n'), nodeOf: nodeOf };
  }

  function nodeLine(view, id, focused) {
    const colour = COLOURS[view.family] || MUTED;
    const size = 10 + Math.min(5, Math.log2(Math.max(view.size || 1, 1)));
    const shape = SHAPES[view.type] || 'box';
    const style = [shape === 'box' ? 'rounded' : null, 'filled', view.dashed ? 'dashed' : null].filter(Boolean).join(',');
    const fill = view.dashed ? '#ffffff' : tint(colour, focused ? 0.34 : 0.16);
    const extra = (DOUBLE[view.family] ? ', peripheries=2' : '') + (focused ? ', penwidth=2.6' : '');
    const label = `<<b>${esc(view.label)}</b><br/><font point-size="${(size - 2).toFixed(0)}" color="${MUTED}">${esc(view.sub)}</font>>`;
    return `  ${id} [label=${label}, fontsize=${size.toFixed(1)}, shape=${shape}, style="${style}", fillcolor="${fill}", color="${colour}", id="${id}", class="fam-${view.family}"${extra}];`;
  }

  function edgeLine(edge, model, dotId, i) {
    const sourceColour = COLOURS[model.nodes[edge.source].family] || MUTED;
    const width = 0.7 + 0.5 * Math.log2(Math.max(edge.count, 1));
    const style = {
      spec: [tint(sourceColour, 0.6), 'solid', width],
      both: [tint(sourceColour, 0.6), 'solid', width],
      builtin: [tint(MUTED, 0.5), 'solid', width],
      owner: [tint(MUTED, 0.28), 'solid', 0.6],
      live_only: [tint(DRIFT, 0.9), 'dashed', Math.max(width, 1.2)],
      spec_only: [tint(MUTED, 0.75), 'dotted', Math.max(width, 1.2)],
    }[edge.kind];
    const arrow = edge.kind === 'spec_only' ? 'empty' : edge.kind === 'owner' ? 'odot' : 'normal';
    return `  ${dotId[edge.source]} -> ${dotId[edge.target]} [color="${style[0]}", style=${style[1]}, penwidth=${style[2].toFixed(2)}, arrowhead=${arrow}, id="e${i}"];`;
  }

  // Rendering and interaction

  let vizReady = null;
  const viz = () => vizReady || (vizReady = Viz.instance());
  const tooltip = document.getElementById('tooltip');

  function showTooltip(ev, text) {
    tooltip.textContent = text;
    tooltip.hidden = false;
    const pad = 14;
    const w = tooltip.offsetWidth;
    const h = tooltip.offsetHeight;
    tooltip.style.left = Math.min(ev.clientX + pad, window.innerWidth - w - 8) + 'px';
    tooltip.style.top = Math.min(ev.clientY + pad, window.innerHeight - h - 8) + 'px';
  }
  const hideTooltip = () => { tooltip.hidden = true; };

  function nodeText(view) {
    const lines = [view.label, view.sub];
    if (view.members) {
      const shown = view.members.slice(0, 12).map(nameOf).join(', ');
      lines.push(shown + (view.members.length > 12 ? `, and ${view.members.length - 12} more` : ''));
    } else if (NODES[view.ref] && NODES[view.ref].comment) {
      lines.push(NODES[view.ref].comment);
    }
    lines.push(view.ref ? 'click to pin · double-click to open' : 'click to pin');
    return lines.join('\n');
  }

  function edgeText(edge, model) {
    const name = (id) => model.nodes[id].label;
    let text = `${name(edge.source)} → ${name(edge.target)}\n${edge.type === 'member' ? 'member of' : edge.type}`;
    if (edge.privileges.length) text += `: ${edge.privileges.join(', ')}`;
    const counts = Object.keys(edge.statuses).map((s) => (edge.count > 1 ? `${edge.statuses[s]} ` : '') + (EDGE_TEXT[s] || s));
    text += `\n${counts.join(', ')}`;
    if (edge.missing.length) text += `\nmissing ${edge.missing.join(', ')}`;
    return text;
  }

  function renderGraph(el, model, options) {
    const count = Object.keys(model.nodes).length;
    el.innerHTML = `<div class="loading">Laying out ${plural(count, 'node')}…</div>`;
    const built = dot(model);
    return viz().then((instance) => {
      const svg = instance.renderSVGElement(built.source);
      el.innerHTML = '';
      el.appendChild(svg);
      return interact(el, svg, model, built.nodeOf, options || {});
    }).catch((error) => {
      el.innerHTML = `<div class="error">Couldn't lay out the graph: ${esc(error && error.message ? error.message : error)}</div>`;
    });
  }

  function interact(el, svg, model, nodeOf, options) {
    const drawnNodes = {};
    svg.querySelectorAll('g.node').forEach((g) => {
      const id = nodeOf[g.id];
      if (id) drawnNodes[id] = g;
    });
    const drawnEdges = model.edges.map((e, i) => ({ edge: e, g: svg.querySelector('#e' + i) }));
    const state = { pinned: null, hover: null, families: {} };
    let moved = false;

    function trace(start, dir, hi) {
      const seen = {};
      const stack = [start];
      while (stack.length) {
        const name = stack.pop();
        if (seen[name]) continue;
        seen[name] = true;
        hi.nodes[name] = true;
        drawnEdges.forEach((d, i) => {
          if (dir === 'up' && d.edge.target === name) { hi.edges[i] = true; stack.push(d.edge.source); }
          if (dir === 'down' && d.edge.source === name) { hi.edges[i] = true; stack.push(d.edge.target); }
        });
      }
    }

    function render() {
      svg.querySelectorAll('.hi, .nb, .pinned').forEach((g) => g.classList.remove('hi', 'nb', 'pinned'));
      const focus = state.pinned || state.hover;
      const filtering = Object.keys(state.families).some((k) => state.families[k]);
      svg.classList.toggle('focus', !!focus || filtering);
      if (state.pinned && drawnNodes[state.pinned]) drawnNodes[state.pinned].classList.add('pinned');
      if (options.onState) options.onState(state);
      if (!focus && !filtering) return;
      const hi = { nodes: {}, edges: {} };
      if (focus) {
        trace(focus, 'up', hi);
        trace(focus, 'down', hi);
      } else {
        Object.keys(drawnNodes).forEach((id) => { if (state.families[model.nodes[id].family]) hi.nodes[id] = true; });
        drawnEdges.forEach((d, i) => {
          if (hi.nodes[d.edge.source] || hi.nodes[d.edge.target]) {
            hi.edges[i] = true;
            [d.edge.source, d.edge.target].forEach((n) => { if (!hi.nodes[n] && drawnNodes[n]) drawnNodes[n].classList.add('nb'); });
          }
        });
      }
      Object.keys(hi.nodes).forEach((id) => { if (drawnNodes[id]) drawnNodes[id].classList.add('hi'); });
      Object.keys(hi.edges).forEach((i) => { if (drawnEdges[i].g) drawnEdges[i].g.classList.add('hi'); });
    }

    Object.keys(drawnNodes).forEach((id) => {
      const g = drawnNodes[id];
      const view = model.nodes[id];
      g.addEventListener('mouseenter', (ev) => { state.hover = id; render(); showTooltip(ev, nodeText(view)); });
      g.addEventListener('mousemove', (ev) => showTooltip(ev, nodeText(view)));
      g.addEventListener('mouseleave', () => { state.hover = null; render(); hideTooltip(); });
      g.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (moved) return;
        state.pinned = state.pinned === id ? null : id;
        render();
      });
      g.addEventListener('dblclick', (ev) => {
        ev.stopPropagation();
        if (view.ref) location.hash = href(view.ref);
      });
    });
    drawnEdges.forEach((d) => {
      if (!d.g) return;
      d.g.addEventListener('mousemove', (ev) => showTooltip(ev, edgeText(d.edge, model)));
      d.g.addEventListener('mouseleave', hideTooltip);
    });
    svg.addEventListener('click', () => { if (moved) return; state.pinned = null; render(); });

    // Wheel zoom and drag pan act on the viewBox; a drag that moves isn't a click
    const vb = svg.viewBox.baseVal;
    const initial = { x: vb.x, y: vb.y, width: vb.width, height: vb.height };
    let drag = null;
    svg.removeAttribute('width');
    svg.removeAttribute('height');
    svg.addEventListener('wheel', (ev) => {
      if (options.mini && !(ev.ctrlKey || ev.metaKey)) return;
      ev.preventDefault();
      const k = Math.exp(Math.max(-0.5, Math.min(0.5, ev.deltaY * 0.0015)));
      const r = svg.getBoundingClientRect();
      const scale = Math.max(vb.width / r.width, vb.height / r.height);
      const mx = vb.x + (ev.clientX - r.left - (r.width - vb.width / scale) / 2) * scale;
      const my = vb.y + (ev.clientY - r.top - (r.height - vb.height / scale) / 2) * scale;
      vb.x = mx - (mx - vb.x) * k;
      vb.y = my - (my - vb.y) * k;
      vb.width *= k;
      vb.height *= k;
    }, { passive: false });
    svg.addEventListener('mousedown', (ev) => {
      drag = { x: ev.clientX, y: ev.clientY, vx: vb.x, vy: vb.y };
      moved = false;
    });
    const onMove = (ev) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect();
      const scale = Math.max(vb.width / r.width, vb.height / r.height);
      if (Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 3) {
        moved = true;
        el.classList.add('dragging');
        hideTooltip();
      }
      vb.x = drag.vx - (ev.clientX - drag.x) * scale;
      vb.y = drag.vy - (ev.clientY - drag.y) * scale;
    };
    const onUp = () => { drag = null; el.classList.remove('dragging'); };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);

    render();
    return {
      state: state,
      render: render,
      reset: () => { Object.assign(vb, initial); },
      pin: (id) => { state.pinned = id; render(); },
      toggleFamily: (family) => { state.families[family] = !state.families[family]; render(); },
      dispose: () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
      },
    };
  }

  // Graph page

  let current = null;

  function shapeIcon(type, colour, dashed) {
    const stroke = `stroke="${colour}" stroke-width="1.5"${dashed ? ' stroke-dasharray="3 2"' : ''}`;
    const fill = dashed ? '#ffffff' : tint(colour, 0.22);
    const shapes = {
      database: `<path d="M3 4.5 v8 a6 2.5 0 0 0 12 0 v-8" fill="${fill}" ${stroke}/><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="${fill}" ${stroke}/>`,
      warehouse: `<rect x="2.5" y="5" width="11" height="9" fill="${fill}" ${stroke}/><path d="M2.5 5 l3 -3 h11 v9 l-3 3 M13.5 5 l3 -3" fill="none" ${stroke}/>`,
      integration: `<polygon points="5,3 13,3 17,9 13,15 5,15 1,9" fill="${fill}" ${stroke}/>`,
      external_volume: `<path d="M2 5 h5 l2 -2 h7 v12 h-14 z" fill="${fill}" ${stroke}/>`,
    };
    const body = shapes[type] || `<rect x="2" y="4" width="14" height="10" rx="3" fill="${fill}" ${stroke}/>`;
    return `<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">${body}</svg>`;
  }

  function legend(model) {
    const counts = {};
    const drawnFamilies = {};
    let dashed = false;
    Object.values(model.nodes).forEach((n) => {
      counts[n.family] = (counts[n.family] || 0) + (n.members ? n.members.length : 1);
      if (!drawnFamilies[n.family] || n.type !== 'group') drawnFamilies[n.family] = n.type;
      dashed = dashed || n.dashed;
    });
    const rows = C.families.filter((f) => drawnFamilies[f.id]).map((f) => {
      const type = drawnFamilies[f.id] === 'group' ? 'user' : drawnFamilies[f.id];
      return `<button type="button" data-family="${esc(f.id)}" title="Highlight ${esc(f.label)}">${shapeIcon(type, COLOURS[f.id] || MUTED)}${esc(f.label)} <span class="count">(${(counts[f.id] || 0).toLocaleString()})</span></button>`;
    }).join('');
    const kinds = {};
    model.edges.forEach((e) => { kinds[e.kind] = (kinds[e.kind] || 0) + 1; });
    const line = (colour, dash, label, n) => `<div><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke="${colour}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ''}/></svg>${label}${n === undefined ? '' : ` <span class="count">(${n})</span>`}</div>`;
    let lines = '';
    if (LIVE) {
      lines += line(COLOURS.functional, '', 'in the spec and on Snowflake', kinds.both || 0);
      lines += line(DRIFT, '5 3', 'on Snowflake, not in the spec', kinds.live_only || 0);
      lines += line(MUTED, '1.5 3', 'in the spec, not on Snowflake', kinds.spec_only || 0);
    }
    if (kinds.builtin) lines += line(tint(MUTED, 0.5), '', 'Snowflake built in');
    if (kinds.owner) lines += line(tint(MUTED, 0.4), '', 'owner');
    if (dashed) lines += `<div>${shapeIcon('role', MUTED, true)} not defined in the spec</div>`;
    return `<div class="legend"><h4>Key <span class="hint">· click a row to highlight</span></h4>${rows}
      <div class="lines">${lines}<div class="hint">arrows run from a grantee to what it's granted</div></div></div>`;
  }

  function infoPanel(id, model) {
    const view = model.nodes[id];
    if (!view) return '';
    let body = `<b>${esc(view.label)}</b><div class="muted">${esc(view.sub)}</div>`;
    if (view.members) body += `<div style="margin-top:6px">${view.members.map(link).join(', ')}</div>`;
    const actions = [];
    if (view.ref) actions.push(`<a class="button" href="${href(view.ref)}">Open page</a>`);
    if (view.ref && model.focus !== view.ref) actions.push(`<a class="button" href="${graphHref(view.ref)}">Focus on it</a>`);
    if (actions.length) body += `<div class="actions">${actions.join('')}</div>`;
    return `<div class="info">${body}</div>`;
  }

  function graphPage(focusId) {
    const node = focusId ? NODES[focusId] : null;
    const options = Object.assign({}, graphOptions, { focus: node ? focusId : null });
    if (node && node.type === 'user') options.group = false;
    const toggle = (key, label) => `<label><input type="checkbox" data-option="${key}"${graphOptions[key] ? ' checked' : ''}${key === 'group' && node && node.type === 'user' ? ' disabled' : ''}> ${label}</label>`;
    main.innerHTML = `<div class="graph-page">
      <div class="toolbar">
        ${node ? `<span class="focus">Focused on ${link(focusId)}</span><a class="button" href="#/graph">Whole graph</a>` : '<span class="focus">Whole graph</span>'}
        ${toggle('group', 'Group people by role set')}
        ${toggle('objects', 'Warehouses, databases and integrations')}
        ${toggle('owners', 'Owners')}
        <span class="spacer"></span>
        <button type="button" class="button" data-action="reset">Fit</button>
      </div>
      <div class="stage"><div class="canvas"><div class="graph"></div><div class="info-slot"></div></div><div class="legend-slot"></div></div>
    </div>`;
    const el = main.querySelector('.graph');
    const model = graphModel(options);
    main.querySelector('.legend-slot').outerHTML = legend(model);
    const infoSlot = main.querySelector('.info-slot');
    renderGraph(el, model, {
      onState: (state) => {
        infoSlot.innerHTML = state.pinned ? infoPanel(state.pinned, model) : '';
        main.querySelectorAll('.legend button').forEach((b) => b.classList.toggle('active', !!state.families[b.dataset.family]));
      },
    }).then((controller) => {
      if (!controller) return;
      current = controller;
      if (model.focus) controller.pin(model.focus);
      main.querySelectorAll('.legend button').forEach((b) => b.addEventListener('click', () => controller.toggleFamily(b.dataset.family)));
      main.querySelector('[data-action="reset"]').addEventListener('click', controller.reset);
    });
    main.querySelectorAll('[data-option]').forEach((input) => input.addEventListener('change', () => {
      graphOptions[input.dataset.option] = input.checked;
      graphPage(focusId);
    }));
  }

  function mountLineage() {
    const el = main.querySelector('.lineage .graph');
    if (!el) return;
    const id = el.dataset.focus;
    const node = NODES[id];
    const model = graphModel({ group: node.type !== 'user', owners: false, objects: true, focus: id });
    renderGraph(el, model, { mini: true }).then((controller) => {
      if (!controller) return;
      current = controller;
      controller.pin(model.focus);
    });
  }

  // Routing

  function route() {
    if (current) { current.dispose(); current = null; }
    hideTooltip();
    const hash = location.hash.replace(/^#\/?/, '');
    const parts = hash.split('/');
    let view = 'overview';
    let id = null;
    if (parts[0] === 'graph') {
      view = 'graph';
      if (parts.length > 2) id = parts[1] + ':' + decodeURIComponent(parts.slice(2).join('/'));
    } else if (parts.length > 1) {
      view = 'node';
      id = parts[0] + ':' + decodeURIComponent(parts.slice(1).join('/'));
    }
    if (id && !NODES[id]) {
      main.innerHTML = `<div class="page"><h1>Not found</h1><p class="comment">Nothing in this catalogue is called <code>${esc(id)}</code>.</p></div>`;
      view = 'missing';
    } else if (view === 'graph') {
      graphPage(id);
    } else if (view === 'node') {
      main.innerHTML = nodePage(id);
      mountLineage();
    } else {
      main.innerHTML = overview();
    }
    if (view !== 'graph') main.scrollTop = 0;
    document.querySelectorAll('.views a').forEach((a) => a.classList.toggle('active', a.dataset.view === view || (view === 'overview' && a.dataset.view === 'overview')));
    nav.querySelectorAll('a.current').forEach((a) => a.classList.remove('current'));
    if (id) {
      const active = nav.querySelector(`a[data-id="${CSS.escape(id)}"]`);
      if (active) active.classList.add('current');
    }
  }

  const summary = document.getElementById('summary');
  summary.textContent = [
    plural(COUNTS.roles, 'role'),
    COUNTS.managed_roles ? plural(COUNTS.managed_roles, 'database or application role', 'database and application roles') : '',
    plural(COUNTS.users, 'user'),
    LIVE ? 'spec and live grants' : 'spec only',
    `generated ${new Date(C.metadata.generated_at).toLocaleString()}`,
  ].filter(Boolean).join(' · ');

  buildNav();
  window.addEventListener('hashchange', route);
  route();
})();
