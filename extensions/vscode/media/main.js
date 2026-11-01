// mini-ork dashboard webview.
//
// This page has no network access by design: it renders whatever the host
// pushes over postMessage and posts actions back. That keeps it compatible
// with a strict CSP and keeps auth tokens out of the DOM.
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();
  const view = document.getElementById('view');
  const backendPill = document.getElementById('backend');
  const refreshBtn = document.getElementById('refresh');
  const tabs = Array.from(document.querySelectorAll('.tab'));

  let snapshot = null;
  let activeTab = 'board';

  function esc(value) {
    return String(value === undefined || value === null ? '' : value).replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
    );
  }

  function money(usd) {
    return typeof usd === 'number' ? '$' + usd.toFixed(2) : '—';
  }

  function post(action) {
    vscode.postMessage({ type: 'action', payload: action });
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      activeTab = tab.getAttribute('data-tab');
      tabs.forEach((t) => t.classList.toggle('active', t === tab));
      render();
    });
  });

  refreshBtn.addEventListener('click', () => {
    refreshBtn.disabled = true;
    vscode.postMessage({ type: 'refresh' });
    setTimeout(() => (refreshBtn.disabled = false), 600);
  });

  window.addEventListener('message', (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'snapshot') {
      snapshot = msg.payload;
      refreshBtn.disabled = false;
      render();
    } else if (msg.type === 'backend') {
      const state = (msg.payload && msg.payload.state) || 'unknown';
      backendPill.textContent = state;
      backendPill.dataset.state = state;
    }
  });

  function render() {
    if (!snapshot) return;
    const state = snapshot.backend && snapshot.backend.state;
    backendPill.textContent = state || 'unknown';
    backendPill.dataset.state = state || 'unknown';

    let html = '';
    if (snapshot.errors && snapshot.errors.length) {
      html +=
        '<div class="warn">' +
        snapshot.errors.map((e) => '<div>' + esc(e) + '</div>').join('') +
        '</div>';
    }

    if (activeTab === 'board') html += renderBoard();
    else if (activeTab === 'runs') html += renderRuns();
    else if (activeTab === 'learning') html += renderLearning();
    else if (activeTab === 'recipes') html += renderRecipes();

    view.innerHTML = html || '<div class="empty">Nothing to show yet.</div>';
    wireRowActions();
  }

  function renderBoard() {
    const board = snapshot.board;
    if (!board || !board.header) return '<div class="empty">No board data.</div>';
    const h = board.header;
    const cells = [
      ['Needs you', h.needs_you],
      ['Working', h.working],
      ['Failed', h.failed],
      ['Done', h.done],
      ['Today', money(h.today_usd)],
      ['Cap', money(h.cap_usd)],
    ];
    let html = '<section class="kpis">';
    cells.forEach(([label, value]) => {
      html +=
        '<div class="kpi"><div class="kpi-label">' +
        esc(label) +
        '</div><div class="kpi-value">' +
        esc(value === undefined || value === null ? '—' : value) +
        '</div></div>';
    });
    html += '</section>';

    if (h.lanes && h.lanes.length) {
      html += '<h2>Lanes</h2><table class="grid"><thead><tr><th>Lane</th><th>Calls</th><th>Failed</th><th>USD</th><th>Last error</th></tr></thead><tbody>';
      h.lanes.forEach((lane) => {
        html +=
          '<tr><td>' +
          esc(lane.lane) +
          '</td><td>' +
          esc(lane.calls) +
          '</td><td>' +
          esc(lane.failed) +
          '</td><td>' +
          money(lane.usd) +
          '</td><td class="wrap">' +
          esc((lane.last_error || '').slice(0, 160)) +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }
    return html;
  }

  function renderRuns() {
    const runs = (snapshot.runs && snapshot.runs.runs) || [];
    if (!runs.length) return '<div class="empty">No runs.</div>';
    let html =
      '<table class="grid"><thead><tr><th>Run</th><th>State</th><th>Step</th><th>Recipe</th><th>Cost</th><th></th></tr></thead><tbody>';
    runs.forEach((run) => {
      html +=
        '<tr><td class="wrap"><strong>' +
        esc(run.title || run.id) +
        '</strong><div class="muted">' +
        esc(run.id) +
        '</div></td><td><span class="state ' +
        esc(run.state) +
        '">' +
        esc(run.state) +
        '</span></td><td>' +
        esc(run.step || '') +
        '</td><td>' +
        esc(run.recipe || '') +
        '</td><td>' +
        money(run.cost_usd) +
        '</td><td class="actions"><button data-act="openTrajectory" data-run="' +
        esc(run.id) +
        '">Open</button>' +
        (run.state === 'working'
          ? '<button data-act="pauseCost" data-run="' + esc(run.id) + '">Pause</button>'
          : '') +
        '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  function renderLearning() {
    const l = snapshot.learning;
    if (!l) return '<div class="empty">No learning data.</div>';
    const rows = Object.keys(l).map(
      (k) =>
        '<tr><td>' + esc(k) + '</td><td>' + esc(l[k]) + '</td></tr>',
    );
    return (
      '<h2>Learning loop</h2><table class="grid"><tbody>' + rows.join('') + '</tbody></table>'
    );
  }

  function renderRecipes() {
    const recipes = (snapshot.recipes && snapshot.recipes.recipes) || [];
    if (!recipes.length) return '<div class="empty">No recipes.</div>';
    let html =
      '<table class="grid"><thead><tr><th>Recipe</th><th>Task class</th><th></th></tr></thead><tbody>';
    recipes.forEach((r) => {
      html +=
        '<tr><td><strong>' +
        esc(r.recipe) +
        '</strong></td><td>' +
        esc(r.task_class) +
        '</td><td class="actions"><button data-act="launch" data-recipe="' +
        esc(r.recipe) +
        '">Launch</button></td></tr>';
    });
    return html + '</tbody></table>';
  }

  function wireRowActions() {
    view.querySelectorAll('button[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const act = btn.getAttribute('data-act');
        if (act === 'launch') {
          post({ action: 'launch', recipe: btn.getAttribute('data-recipe') || undefined });
        } else {
          post({ action: act, runId: btn.getAttribute('data-run') });
        }
      });
    });
  }

  vscode.postMessage({ type: 'ready' });
})();
