/* cao web UI — vanilla JS single-page app */
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'html') n.innerHTML = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  };
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const api = async (path, opts = {}) => {
    const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.detail?.problems ? j.detail.problems.join('\n') : (j.detail || JSON.stringify(j)); } catch (_) {}
      throw new Error(msg);
    }
    return res.headers.get('content-type')?.includes('json') ? res.json() : res.text();
  };
  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString() : '';
  const ago = (t) => { if (!t) return ''; const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${s | 0}s ago` : s < 3600 ? `${(s / 60) | 0}m ago` : s < 86400 ? `${(s / 3600) | 0}h ago` : `${(s / 86400) | 0}d ago`; };
  const money = (v) => v == null ? '–' : `$${Number(v).toFixed(4)}`;
  const scoreClass = (s, pass = 9) => s == null ? '' : s >= pass ? 'good' : s >= pass - 2 ? 'mid' : 'bad';
  const identity = (r) => `${r.backend}:${r.model || (r.backend === 'grok' ? 'grok-code-fast-1' : 'default')}`;

  let META = null;
  let es = null; // active EventSource
  const view = $('#view');

  // ---------------------------------------------------------------- boot
  async function boot() {
    META = await api('/api/meta');
    const chips = $('#backends');
    chips.replaceChildren(...META.backends.map((b) => el('span', { class: `chip ${b.available ? 'on' : 'off'}`, title: b.detail }, b.title)));
    if (META.tunnel_url) $('#tunnel').replaceChildren('tunnel: ', el('a', { href: META.tunnel_url, target: '_blank' }, META.tunnel_url));
    $('#btn-new').onclick = () => navigate('/new');
    window.addEventListener('popstate', route);
    route();
  }
  function navigate(path) { history.pushState({}, '', path); route(); }
  function route() {
    if (es) { es.close(); es = null; }
    const p = location.pathname;
    if (p === '/new') return renderForm();
    const m = p.match(/^\/tasks\/([^/]+)/);
    if (m) return renderDetail(m[1]);
    renderList();
  }
  function use(tplId) { view.replaceChildren($(tplId).content.cloneNode(true)); }

  // ---------------------------------------------------------------- list
  let listTimer = null;
  async function renderList() {
    use('#tpl-list');
    clearInterval(listTimer);
    const load = async () => {
      if (!document.body.contains($('#task-rows'))) { clearInterval(listTimer); return; }
      const rows = await api('/api/tasks');
      $('#list-count').textContent = `${rows.length} task${rows.length === 1 ? '' : 's'}`;
      $('#list-empty').classList.toggle('hidden', rows.length > 0);
      $('#task-rows').replaceChildren(...rows.map((r) => el('tr', { onclick: () => navigate(`/tasks/${r.id}`) },
        el('td', {}, el('span', { class: `pill ${r.running ? 'running' : r.status}` }, r.running ? 'running' : r.status)),
        el('td', {}, el('div', {}, r.title), el('div', { class: 'muted small' }, r.repo_path || '')),
        el('td', { class: 'small' }, `${identity(r.worker)} `, el('span', { class: 'muted' }, `(${r.worker.role || 'coder'})`), ' → ', identity(r.reviewer)),
        el('td', {}, `${r.iterations}/${r.max_iterations ?? '?'}`),
        el('td', {}, el('span', { class: `score ${scoreClass(r.last_score)}` }, r.last_score == null ? '–' : r.last_score.toFixed(1))),
        el('td', { class: 'small' }, money(r.total_cost_usd)),
        el('td', { class: 'muted small', title: fmtTime(r.updated_at) }, ago(r.updated_at)),
      )));
    };
    await load();
    listTimer = setInterval(load, 4000);
  }

  // ---------------------------------------------------------------- form
  function renderForm(prefill = null) {
    use('#tpl-form');
    const form = $('#task-form');
    const d = META.defaults;
    const fill = (sel, items, value) => { sel.replaceChildren(...items.map((i) => el('option', { value: i.value }, i.label))); if (value !== undefined) sel.value = value; };
    for (const sel of form.querySelectorAll('select[data-role]')) {
      fill(sel, META.backends.map((b) => ({ value: b.key, label: `${b.title}${b.available ? '' : '  (not available: ' + b.detail + ')'}` })),
        d[sel.dataset.role].backend);
    }
    for (const name of ['worker.effort', 'reviewer.effort']) {
      fill(form.elements[name], [{ value: '', label: '(CLI default)' }, ...META.efforts.map((e) => ({ value: e, label: e }))], d[name.split('.')[0]].effort || '');
    }
    fill(form.elements['worker.role'], META.roles.map((r) => ({ value: r.key, label: r.title })), d.worker.role);
    $('#rubric').replaceChildren(...META.criteria.map((c) => el('tr', {}, el('td', {}, c.title), el('td', {}, `×${c.weight}`), el('td', {}, c.description))));
    form.elements.repo_path.value = META.workspace;

    const syncModels = () => {
      for (const role of ['worker', 'reviewer']) {
        const b = META.backends.find((x) => x.key === form.elements[`${role}.backend`].value);
        $(`#models-${role}`).replaceChildren(...(b?.models || []).filter(Boolean).map((m) => el('option', { value: m })));
      }
      const w = { backend: form.elements['worker.backend'].value, model: form.elements['worker.model'].value };
      const r = { backend: form.elements['reviewer.backend'].value, model: form.elements['reviewer.model'].value };
      const same = identity(w) === identity(r);
      $('#cross-warn').classList.toggle('hidden', !same);
      const pairing = $('#pairing');
      pairing.classList.toggle('hidden', same);
      pairing.replaceChildren(el('span', { class: 'ok-mark' }, '✓'), ' cross-model pair: ', el('code', {}, identity(w)), ' → ', el('code', {}, identity(r)));
    };
    const syncRole = () => { $('#role-brief').textContent = META.roles.find((r) => r.key === form.elements['worker.role'].value)?.brief || ''; };
    form.addEventListener('input', syncModels);
    form.elements['worker.role'].addEventListener('change', syncRole);

    // repository browser
    const browse = $('#browse');
    const showDir = async (path) => {
      try {
        const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
        browse.classList.remove('hidden');
        browse.replaceChildren(
          el('div', { class: 'muted', onclick: () => showDir(data.parent) }, '↑ ', data.parent),
          el('div', { class: data.is_git ? 'git' : '', onclick: () => { form.elements.repo_path.value = data.path; browse.classList.add('hidden'); } }, '● use this directory: ', data.path, data.is_git ? '  (git)' : ''),
          ...data.dirs.map((x) => el('div', { class: x.is_git ? 'git' : '', onclick: () => showDir(x.path) }, x.is_git ? '◆ ' : '▸ ', x.name)),
        );
      } catch (e) { browse.classList.remove('hidden'); browse.replaceChildren(el('div', { class: 'error' }, e.message)); }
    };
    $('#btn-browse').onclick = () => browse.classList.contains('hidden') ? showDir(form.elements.repo_path.value || META.workspace) : browse.classList.add('hidden');
    $('#btn-cancel-form').onclick = () => navigate('/');

    if (prefill) applyPrefill(form, prefill);
    syncModels(); syncRole();

    form.onsubmit = async (ev) => {
      ev.preventDefault();
      $('#form-error').textContent = '';
      for (const [name, msg] of [['request', 'Describe what should be done.'], ['repo_path', 'Choose a repository path.']]) {
        const elm = form.elements[name];
        if (!elm.value.trim()) { $('#form-error').textContent = msg; elm.classList.add('touched'); elm.focus(); return; }
      }
      if (!$('#cross-warn').classList.contains('hidden')) { $('#form-error').textContent = 'Worker and reviewer must be different models.'; return; }
      $('#btn-submit').disabled = true;
      try {
        const body = collect(form);
        const res = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ ...body, start: true }) });
        navigate(`/tasks/${res.id}`);
      } catch (e) { $('#form-error').textContent = e.message; $('#btn-submit').disabled = false; }
    };
  }
  function collect(form) {
    const out = { worker: {}, reviewer: {}, loop: {} };
    for (const elm of form.elements) {
      if (!elm.name) continue;
      let v = elm.type === 'checkbox' ? elm.checked : elm.value;
      if (elm.type === 'number' && v !== '') v = Number(v);
      if (v === '') v = null;
      const [a, b] = elm.name.split('.');
      if (b) out[a][b] = v; else out[a] = v;
    }
    out.acceptance_criteria = (out.acceptance_criteria || '').split('\n').map((s) => s.replace(/^[\s\-*]+/, '').trim()).filter(Boolean);
    return out;
  }
  function applyPrefill(form, spec) {
    const set = (name, v) => { const e = form.elements[name]; if (!e || v == null) return; if (e.type === 'checkbox') e.checked = !!v; else e.value = v; };
    set('title', spec.title); set('request', spec.request); set('repo_path', spec.repo_path); set('base_branch', spec.base_branch);
    set('acceptance_criteria', (spec.acceptance_criteria || []).map((c) => `- ${c}`).join('\n'));
    for (const role of ['worker', 'reviewer']) for (const k of ['backend', 'model', 'effort', 'role', 'instructions', 'timeout']) set(`${role}.${k}`, spec[role]?.[k]);
    for (const k of Object.keys(spec.loop || {})) set(`loop.${k}`, spec.loop[k]);
  }

  // ---------------------------------------------------------------- detail
  async function renderDetail(id) {
    use('#tpl-detail');
    let data;
    try { data = await api(`/api/tasks/${id}`); } catch (e) { view.replaceChildren(el('p', { class: 'error' }, e.message)); return; }
    const spec = data.spec;
    $('#d-title').textContent = spec.title;
    $('#btn-clone').onclick = () => { history.pushState({}, '', '/new'); renderForm(spec); };
    $('#btn-cancel').onclick = async () => { try { await api(`/api/tasks/${id}/cancel`, { method: 'POST' }); } catch (e) { alert(e.message); } };
    $('#btn-rerun').onclick = async () => { if (!confirm('Re-run from scratch? The task branch and worktree will be recreated.')) return; try { await api(`/api/tasks/${id}/start`, { method: 'POST' }); renderDetail(id); } catch (e) { alert(e.message); } };
    $('#btn-delete').onclick = async () => { if (!confirm('Delete this task record? (git branch is kept)')) return; try { await api(`/api/tasks/${id}`, { method: 'DELETE' }); navigate('/'); } catch (e) { alert(e.message); } };

    const paint = (d) => {
      const running = d.running;
      const st = running ? 'running' : d.status;
      $('#d-status').className = `pill ${st}`; $('#d-status').textContent = st;
      const sc = d.final_score;
      $('#d-score').className = `score-big score ${scoreClass(sc, spec.loop.pass_score)}`;
      $('#d-score').textContent = sc == null ? '' : `${sc.toFixed(1)} / ${spec.loop.pass_score}`;
      $('#btn-cancel').classList.toggle('hidden', !running);
      $('#btn-rerun').classList.toggle('hidden', running || !['failed', 'cancelled', 'stopped', 'exhausted', 'pending'].includes(d.status));
      $('#btn-delete').classList.toggle('hidden', running);
      const tok = Object.entries(d.total_usage || {}).filter(([k]) => /token/.test(k)).map(([k, v]) => `${k.replace(/_tokens?/, '')}=${v}`).join(' ');
      $('#d-meta').replaceChildren(
        el('span', {}, 'worker ', el('code', {}, `${identity(spec.worker)} · ${spec.worker.role}${spec.worker.effort ? ' · ' + spec.worker.effort : ''}`)),
        el('span', {}, 'reviewer ', el('code', {}, `${identity(spec.reviewer)}${spec.reviewer.effort ? ' · ' + spec.reviewer.effort : ''}`)),
        el('span', {}, 'repo ', el('code', {}, spec.repo_path)),
        d.branch ? el('span', {}, 'branch ', el('code', {}, d.branch)) : null,
        el('span', {}, `iterations ${d.iterations.length}/${spec.loop.max_iterations}`),
        el('span', {}, `cost ${money(d.total_cost_usd)}`),
        tok ? el('span', {}, `tokens ${tok}`) : null,
        el('span', {}, `scoring ${spec.loop.scoring} · on success ${spec.loop.on_success}`),
      );
      paintIterations(d);
    };
    paint(data);

    // live log via SSE
    const log = $('#log');
    const logState = $('#log-state');
    const seen = new Set();
    const addLine = (line) => {
      const cls = /\b(OFFER|ACK|NACK|COMMIT)\b/.exec(line)?.[1];
      const span = el('span', { class: cls ? `hs-${cls}` : /-> (PASS|ITERATE|STOP)/.test(line) ? 'decision' : '' }, line + '\n');
      log.append(span);
      log.scrollTop = log.scrollHeight;
    };
    es = new EventSource(`/api/tasks/${id}/events`);
    logState.textContent = 'connecting…';
    es.onopen = () => { logState.textContent = 'live'; };
    es.addEventListener('log', (e) => { const d = JSON.parse(e.data); if (d.seq && seen.has(d.seq)) return; if (d.seq) seen.add(d.seq); addLine(d.line); });
    let refreshTimer = null;
    const refresh = async () => { try { const d = await api(`/api/tasks/${id}`); paint(d); } catch (_) {} };
    es.addEventListener('status', () => refresh());
    es.addEventListener('done', () => { logState.textContent = 'finished'; es.close(); clearInterval(refreshTimer); refresh(); });
    es.onerror = () => { logState.textContent = 'disconnected'; };
    refreshTimer = setInterval(() => { if (es.readyState === EventSource.CLOSED) clearInterval(refreshTimer); else refresh(); }, 5000);
  }

  const openTabs = {}; // iteration number -> tab key
  function paintIterations(d) {
    const spec = d.spec;
    const box = $('#iterations');
    const nodes = [];
    if (d.outcome && Object.keys(d.outcome).length) {
      const o = d.outcome;
      nodes.push(el('div', { class: 'outcome' },
        o.pr_url ? el('div', {}, '✅ Pull request: ', el('a', { href: o.pr_url, target: '_blank' }, o.pr_url)) : null,
        o.merged_into ? el('div', {}, `✅ Merged into ${o.merged_into} @ ${(o.merge_commit || '').slice(0, 10)}`) : null,
        o.finish_error ? el('div', { class: 'warn' }, `⚠ ${o.finish_error}`) : null,
        o.branch ? el('div', { class: 'muted small' }, 'branch ', el('code', {}, o.branch), o.report ? ` · report: ${o.report}` : '') : null,
        d.error ? el('div', { class: 'muted small' }, d.error) : null,
      ));
    } else if (d.error) nodes.push(el('div', { class: 'outcome warn' }, d.error));

    for (const it of [...d.iterations].reverse()) {
      const tab = openTabs[it.number] || (it.review ? 'review' : 'events');
      const tabs = [['review', 'Review'], ['diff', `Diff${it.diffstat ? ' · ' + it.diffstat.trim().split('\n').pop() : ''}`], ['events', `Hand-offs (${it.events.length})`], ['worker', 'Worker'], ['reviewer', 'Reviewer']];
      nodes.push(el('div', { class: 'iter' },
        el('div', { class: 'iter-head' },
          el('span', { class: 'n' }, `#${it.number}`),
          el('span', { class: `score ${scoreClass(it.score, spec.loop.pass_score)}` }, it.score == null ? (it.reviewer ? 'reviewing…' : it.worker ? 'working…' : '') : it.score.toFixed(2)),
          it.decision ? el('span', { class: `pill ${it.decision === 'pass' ? 'passed' : it.decision === 'stop' ? 'stopped' : 'pending'}` }, it.decision) : null,
          el('span', { class: 'who' }, `${it.worker?.identity || ''}${it.worker?.attempts > 1 ? ' ×' + it.worker.attempts : ''} → ${it.reviewer?.identity || '…'}${it.reviewer?.attempts > 1 ? ' ×' + it.reviewer.attempts : ''}`),
          el('span', { class: 'spacer' }),
          el('span', { class: 'muted small' }, `${money(it.cost_usd)} · ${((it.worker?.duration_s || 0) + (it.reviewer?.duration_s || 0)).toFixed(0)}s${it.commit ? ' · ' + it.commit.slice(0, 10) : ''}`),
        ),
        el('div', { class: 'iter-body' },
          el('div', { class: 'tabs' }, ...tabs.map(([k, label]) => el('span', { class: `tab ${tab === k ? 'on' : ''}`, onclick: () => { openTabs[it.number] = k; paintIterations(d); } }, label))),
          el('div', { class: 'panel' }, panelFor(tab, it, spec)),
        ),
      ));
    }
    box.replaceChildren(...nodes);
  }

  function panelFor(tab, it, spec) {
    if (tab === 'review') {
      if (!it.review) return el('p', { class: 'muted' }, it.reviewer?.error ? `reviewer error: ${it.reviewer.error}` : 'No review yet.');
      const r = it.review;
      const crit = META.criteria;
      return el('div', {},
        el('div', { class: 'bars' }, ...crit.flatMap((c) => { const v = r.scores[c.key]; return [el('span', { title: c.description }, c.title), el('div', { class: 'bar' }, el('i', { style: `width:${(v ?? 0) * 10}%; background:${v >= 9 ? 'var(--ok)' : v >= 7 ? 'var(--warn)' : 'var(--bad)'}` })), el('span', { class: 'score' }, v ?? '–')]; })),
        el('div', { class: 'kv' }, el('b', {}, 'overall (LLM)'), el('span', {}, r.overall_llm ?? '–'), el('b', {}, 'verdict'), el('span', {}, r.verdict || '–'), el('b', {}, 'tests'), el('span', {}, r.tests_observed || '–')),
        el('p', {}, r.summary),
        r.strengths?.length ? el('p', { class: 'muted small' }, '✓ ', r.strengths.join(' · ')) : null,
        ...(r.issues || []).map((i) => el('div', { class: `issue ${i.severity}` }, el('span', { class: 'sev' }, i.severity), i.file ? el('code', {}, `${i.file}${i.line ? ':' + i.line : ''} `) : null, i.description, i.suggestion ? el('div', { class: 'muted small' }, '→ ', i.suggestion) : null)),
      );
    }
    if (tab === 'diff') {
      if (!it.diff) return el('p', { class: 'muted' }, 'No diff yet.');
      const html = it.diff.split('\n').map((l) => {
        const cls = l.startsWith('+++') || l.startsWith('---') || l.startsWith('diff ') ? 'file' : l.startsWith('@@') ? 'hunk' : l.startsWith('+') ? 'add' : l.startsWith('-') ? 'del' : '';
        return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
      }).join('\n');
      return el('pre', { class: 'diff', html });
    }
    if (tab === 'events') {
      return el('ul', { class: 'events' }, ...it.events.map((e) => el('li', {}, el('span', { class: 'muted' }, new Date(e.at * 1000).toLocaleTimeString(), ' '), el('span', { class: 'muted' }, e.handoff.padEnd(9)), el('span', { class: `ph ${e.phase}` }, e.phase), e.detail)));
    }
    const st = tab === 'worker' ? it.worker : it.reviewer;
    if (!st) return el('p', { class: 'muted' }, 'Not started.');
    return el('div', {},
      el('div', { class: 'kv' }, el('b', {}, 'model'), el('span', {}, st.identity), el('b', {}, 'status'), el('span', {}, st.ok ? 'ok' : (st.error || 'pending')), el('b', {}, 'duration'), el('span', {}, `${st.duration_s.toFixed(1)}s · attempts ${st.attempts}`), el('b', {}, 'cost'), el('span', {}, money(st.cost_usd)), el('b', {}, 'usage'), el('span', {}, JSON.stringify(st.usage || {}))),
      el('details', { open: '' }, el('summary', {}, 'Response'), el('pre', {}, st.response || '(empty)')),
      el('details', {}, el('summary', {}, `Prompt (${st.prompt.length} chars)`), el('pre', {}, st.prompt)),
    );
  }

  boot().catch((e) => { view.replaceChildren(el('p', { class: 'error' }, `failed to load: ${e.message}`)); });
})();
