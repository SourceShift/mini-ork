// mini-ork IDE — a generic renderer for the backend's page spec.
//
// The extension host owns every byte of state (open tabs, the live page, the
// CLI log). This file only renders what it is handed and posts intents back.
// That is what keeps a strict CSP possible: no network access, no eval, no
// innerHTML — every string from the backend reaches the DOM through
// `textContent`.
//
// The page shape is `mini_ork/ide_pages/spec.py` (spec level 2): a page is
// `{title, sub, chips, actions, tabs, tab, sections, errors}`, a section is
// `{type, title, sub, note, actions, full, ...}`. Colours are names, never hex.
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();

  // ── state ────────────────────────────────────────────────────────────
  const ui = {
    tabs: [],
    active: 0,
    page: null,
    header: null,
    counts: {},
    backend: null,
    loading: false,
    railOpen: true,
    palette: null, // {q, results, index}
    confirm: null, // {title, body, cmd, actions[]}
    toasts: [],
    logs: [],
    logOpen: false,
    runs: [],
    storyOpen: {},
    // Which page revision is on screen. The host bumps `pageRev` only when the
    // page actually changed, so a shell poll (header + counts, every few
    // seconds) updates the top bar without tearing down the body — otherwise a
    // sentence half-typed into a composer would vanish mid-keystroke.
    pageRev: -1,
    renderedRev: -1,
    renderedLoading: false,
  };

  /**
   * Composer drafts, keyed by page + section. A genuine re-render (a page that
   * asked to refresh, an action that changed state) rebuilds the textarea, and
   * the user's unfinished sentence has to survive that.
   */
  const drafts = {};
  let focusedDraft = null;

  const root = document.getElementById('root');
  const viewport = document.getElementById('viewport');
  const drawerLog = document.getElementById('logbody');
  const drawer = document.getElementById('drawer');

  // ── tiny DOM helpers ─────────────────────────────────────────────────
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function frag() {
    return document.createDocumentFragment();
  }

  /** A colour name from the spec → a class. `fam:<lane>` paints a model lane.
   *
   * Lane aliases are not families: the spec says `fam:deepseek_flash`, while the
   * palette only defines `--fam-deepseek`. Nesting the fallbacks means the alias
   * paints its family and an unknown lane lands on `--sub` instead of nowhere.
   */
  function paint(node, c) {
    if (!c) return node;
    if (c.indexOf('fam:') === 0) {
      const parts = String(c.slice(4)).split('_');
      let expr = 'var(--sub)';
      for (let i = parts.length; i > 0; i--) {
        expr = 'var(--fam-' + parts.slice(0, i).join('_') + ', ' + expr + ')';
      }
      node.style.setProperty('--fam', expr);
      node.classList.add('c-fam');
      return node;
    }
    node.classList.add('c-' + c);
    return node;
  }

  const NUMERIC = /^\s*[$€£]?[\d][\d.,:×x\/ %+−–-]*\s*$|^\(?\s*\d[\d.,]*\s*(ms|s|m|h|d|kb|mb|gb)?\s*\)?$/i;

  function post(type, payload) {
    vscode.postMessage({ type: type, payload: payload || {} });
  }

  function act(doSpec) {
    if (!doSpec) return;
    const spec = Object.assign({}, doSpec);
    post('act', { do: spec });
  }

  function btn(label, doSpec, kind) {
    const b = el('button', 'btn ' + (kind || 'default'), label);
    if (!doSpec) {
      b.disabled = true;
      return b;
    }
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      act(doSpec);
    });
    return b;
  }

  function buttons(list, cls) {
    const box = el('div', cls || 'a');
    (list || []).forEach(function (b) {
      if (!b || !b.label) return;
      box.appendChild(btn(b.label, b.do, b.kind));
    });
    return box;
  }

  /** `menu` from the page header: secondary actions behind a ▾ popover. */
  function menuButton(items) {
    const wrap = el('div', 'menu');
    const trigger = el('button', 'btn default', '▾');
    trigger.title = 'More actions';
    const pop = el('div', 'menupop');
    pop.style.display = 'none';
    items.forEach(function (m) {
      if (!m || !m.label) return;
      const b = el('button', 'menuitem', m.label);
      if (m.kind) b.classList.add(m.kind);
      if (m.note) b.title = m.note;
      b.addEventListener('click', function () {
        pop.style.display = 'none';
        act(m.do);
      });
      pop.appendChild(b);
    });
    trigger.addEventListener('click', function (ev) {
      ev.stopPropagation();
      pop.style.display = pop.style.display === 'none' ? 'block' : 'none';
    });
    wrap.appendChild(trigger);
    wrap.appendChild(pop);
    return wrap;
  }

  /** "12s" / "4m" / "2h" — coarse on purpose, matching the tree views. */
  function relativeTime(epochSeconds) {
    const delta = Math.max(0, Math.floor(Date.now() / 1000 - Number(epochSeconds)));
    if (!Number.isFinite(delta)) return '';
    if (delta < 60) return delta + 's';
    if (delta < 3600) return Math.floor(delta / 60) + 'm';
    if (delta < 86400) return Math.floor(delta / 3600) + 'h';
    return Math.floor(delta / 86400) + 'd';
  }

  // ── markdown (DOM-only: never innerHTML) ─────────────────────────────
  function inline(text, parent) {
    // Handles `code`, **bold**, *italic* — the only markup the pages use.
    const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      if (tok[0] === '`') parent.appendChild(el('code', null, tok.slice(1, -1)));
      else if (tok.slice(0, 2) === '**') parent.appendChild(el('strong', null, tok.slice(2, -2)));
      else parent.appendChild(el('em', null, tok.slice(1, -1)));
      last = m.index + tok.length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function markdown(text) {
    const box = el('div', 'md');
    const lines = String(text || '').split('\n');
    let list = null;
    let para = null;
    let fence = null;

    function flushPara() {
      para = null;
    }
    function flushList() {
      list = null;
    }

    lines.forEach(function (raw) {
      const line = raw.replace(/\s+$/, '');
      if (fence !== null) {
        if (line.indexOf('```') === 0) {
          fence = null;
        } else {
          const pre = box.lastChild;
          pre.appendChild(el('div', null, line));
        }
        return;
      }
      if (line.indexOf('```') === 0) {
        flushPara();
        flushList();
        const pre = el('pre');
        pre.appendChild(el('code'));
        box.appendChild(pre);
        fence = '';
        return;
      }
      if (!line.trim()) {
        flushPara();
        return;
      }
      const h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) {
        flushPara();
        flushList();
        const hd = el('h' + Math.min(h[1].length + 1, 4));
        inline(h[2], hd);
        box.appendChild(hd);
        return;
      }
      if (/^[-*]\s+/.test(line) || /^\d+\.\s+/.test(line)) {
        flushPara();
        if (!list) {
          list = el('ul');
          box.appendChild(list);
        }
        const li = el('li');
        inline(line.replace(/^([-*]|\d+\.)\s+/, ''), li);
        list.appendChild(li);
        return;
      }
      if (/^-{3,}$/.test(line)) {
        flushPara();
        flushList();
        box.appendChild(el('hr'));
        return;
      }
      flushList();
      if (!para) {
        para = el('p');
        box.appendChild(para);
      }
      if (para.childNodes.length) para.appendChild(document.createTextNode(' '));
      inline(line, para);
    });
    return box;
  }

  // ── card scaffolding ─────────────────────────────────────────────────
  function card(sec, opts) {
    opts = opts || {};
    const c = el('div', 'card' + (sec.full ? ' full' : '') + (opts.hl ? ' hl-' + opts.hl : ''));
    if (sec.title || sec.sub || (sec.actions && sec.actions.length) || opts.badge) {
      const head = el('div', 'head');
      if (opts.icon) {
        const g = el('span', 'secicon');
        paint(g, opts.iconColor || 'muted');
        g.textContent = opts.icon;
        head.appendChild(g);
      }
      if (sec.title) head.appendChild(el('span', 't', sec.title));
      if (sec.sub) head.appendChild(el('span', 's', sec.sub));
      if (opts.badge) {
        const b = el('span', 'chip ' + (opts.badgeTone || ''), opts.badge);
        head.appendChild(b);
      }
      if (sec.actions && sec.actions.length) head.appendChild(buttons(sec.actions, 'a'));
      c.appendChild(head);
    }
    const body = el('div', 'body' + (opts.flush ? ' flush' : ''));
    c.appendChild(body);
    if (sec.note) c.appendChild(el('div', 'note', sec.note));
    return { card: c, body: body };
  }

  // ── section renderers ────────────────────────────────────────────────
  const R = {};

  R.table = function (sec) {
    const widths = (sec.cols || []).map(function (col) {
      if (col && col.w != null) return col.w + 'px';
      const min = (col && col.min) || 0;
      return 'minmax(' + min + 'px, ' + ((col && col.fr) || 1) + 'fr)';
    });
    const f = card(sec, { flush: true });
    const tbl = el('div', 'tbl');

    // The spec carries no per-cell alignment, so it is inferred per column: a
    // column whose cells are mostly numbers right-aligns, the way the design
    // draws cost/%, and a text column never does.
    const right = (sec.cols || []).map(function (_, i) {
      const vals = (sec.rows || [])
        .map(function (r) { return ((r.cells || [])[i] || {}).t; })
        .filter(function (v) { return v != null && String(v).trim() !== ''; });
      if (!vals.length) return i > 0;
      const numeric = vals.filter(function (v) { return NUMERIC.test(String(v)); }).length;
      return numeric * 2 > vals.length;
    });

    if (sec.head && sec.head.length) {
      const hr = el('div', 'trow header');
      hr.style.gridTemplateColumns = widths.join(' ');
      sec.head.forEach(function (h, i) {
        const cell = el('div', 'cell' + (right[i] ? ' right' : ''), h);
        hr.appendChild(cell);
      });
      if ((sec.rows || []).some(function (r) { return r.acts && r.acts.length; })) {
        hr.appendChild(el('div'));
      }
      tbl.appendChild(hr);
    }

    (sec.rows || []).forEach(function (row) {
      const r = el('div', 'trow' + (row.do ? ' go' : '') + (row.sel ? ' sel' : ''));
      r.style.gridTemplateColumns = widths.join(' ') +
        (row.acts && row.acts.length ? ' auto' : '');
      (row.cells || []).forEach(function (cell, i) {
        const c = el('div', 'cell');
        if (cell.mono) c.classList.add('mono');
        if (cell.b) c.classList.add('bold');
        if (right[i]) c.classList.add('right');
        if (cell.wrap) c.classList.add('wrap');
        paint(c, cell.c);
        c.textContent = cell.t == null ? '' : cell.t;
        c.title = c.textContent;
        r.appendChild(c);
      });
      if (row.acts && row.acts.length) r.appendChild(buttons(row.acts, 'rowacts'));
      if (row.do) {
        r.addEventListener('click', function () { act(row.do); });
      }
      tbl.appendChild(r);
    });

    if (!(sec.rows || []).length) tbl.appendChild(el('div', 'empty', 'Nothing here.'));
    f.body.appendChild(tbl);
    return f.card;
  };

  R.list = function (sec) {
    const f = card(sec, {
      flush: true,
      icon: sec.icon,
      iconColor: sec.iconColor,
      badge: sec.badge,
      badgeTone: sec.badgeTone,
    });
    const box = el('div', 'litems');
    (sec.items || []).forEach(function (it) {
      const row = el('div', 'litem' + (it.do ? ' go' : ''));
      const mk = el('span', 'mark');
      paint(mk, it.mc || 'sub');
      mk.textContent = it.m == null ? '•' : it.m;
      row.appendChild(mk);
      const txt = el('div', 'txt');
      const t = el('div', 'ttl' + (it.mono ? ' mono' : ''));
      paint(t, it.tc || 'text');
      t.textContent = it.t == null ? '' : it.t;
      txt.appendChild(t);
      if (it.sub) txt.appendChild(el('div', 'sub', it.sub));
      row.appendChild(txt);
      if (it.acts && it.acts.length) row.appendChild(buttons(it.acts, 'acts'));
      if (it.do) row.addEventListener('click', function () { act(it.do); });
      box.appendChild(row);
    });
    if (!(sec.items || []).length) {
      box.appendChild(el('div', 'empty', sec.empty || 'Nothing here.'));
    }
    f.body.appendChild(box);
    return f.card;
  };

  R.kv = function (sec) {
    const f = card(sec, { flush: false });
    const grid = el('div', 'kvlist' + (sec.cols ? ' tiles' : ''));
    if (sec.cols) grid.style.setProperty('--kvcols', String(sec.cols));
    (sec.items || []).forEach(function (it) {
      const row = el('div', 'kv');
      row.appendChild(el('span', 'k', it.k));
      const v = el('span', 'v' + (sec.big ? ' big' : ''));
      paint(v, it.c);
      v.textContent = it.v == null ? '' : it.v;
      if (it.sub) {
        const s = el('span', 'sub');
        paint(s, it.subc || 'sub');
        s.textContent = it.sub;
        v.appendChild(s);
      }
      row.appendChild(v);
      grid.appendChild(row);
    });
    f.body.appendChild(grid);
    return f.card;
  };

  R.bars = function (sec) {
    const f = card(sec);
    const box = el('div', 'bars');
    (sec.items || []).forEach(function (it) {
      const row = el('div', 'bar');
      row.appendChild(el('div', 'lbl', it.label));
      const track = el('div', 'track');
      const fill = el('div', 'fill');
      fill.style.width = Math.max(0, Math.min(100, it.pct || 0)) + '%';
      paint(fill, it.c || 'blue');
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('div', 'val', it.val));
      box.appendChild(row);
    });
    f.body.appendChild(box);
    return f.card;
  };

  R.flow = function (sec) {
    const f = card(sec);
    const box = el('div', 'flow');
    (sec.items || []).forEach(function (it, i) {
      if (i) box.appendChild(el('div', 'farrow', '→'));
      const s = el('div', 'fstep' + (it.do ? ' go' : ''));
      paint(s, null);
      const l = el('div', 'lbl');
      paint(l, it.c || 'text');
      l.textContent = it.label || '';
      s.appendChild(l);
      if (it.sub) s.appendChild(el('div', 'sub', it.sub));
      if (it.do) {
        s.style.cursor = 'pointer';
        s.addEventListener('click', function () { act(it.do); });
      }
      box.appendChild(s);
    });
    f.body.appendChild(box);
    return f.card;
  };

  R.code = function (sec) {
    const f = card(sec);
    const pre = el('div', 'code');
    if (sec.max_h) pre.style.setProperty('--codeh', sec.max_h);
    (sec.lines || []).forEach(function (ln) {
      const d = el('span', 'ln');
      paint(d, ln.c || 'body');
      d.textContent = ln.t == null ? '' : ln.t;
      pre.appendChild(d);
    });
    f.body.appendChild(pre);
    return f.card;
  };

  R.markdown = function (sec) {
    const acts = (sec.actions || []).slice();
    if (sec.path) {
      acts.push({ label: 'Open file', kind: 'ghost', do: { path: sec.path } });
    }
    const f = card(Object.assign({}, sec, { actions: acts }));
    f.body.appendChild(markdown(sec.text));
    return f.card;
  };

  R.pills = function (sec) {
    const f = card(sec);
    const box = el('div', 'pills');
    (sec.items || []).forEach(function (it) {
      const p = el('span', 'pill');
      paint(p, it.c);
      p.textContent = it.t;
      box.appendChild(p);
    });
    f.body.appendChild(box);
    return f.card;
  };

  R.chips = function (sec) {
    const f = card(sec);
    const box = el('div', 'chips');
    (sec.items || []).forEach(function (it) {
      const c = el('button', 'chip' + (it.on ? ' on' : '') + (it.do ? ' do' : ''), it.t);
      if (it.do) c.addEventListener('click', function () { act(it.do); });
      box.appendChild(c);
    });
    f.body.appendChild(box);
    return f.card;
  };

  R.columns = function (sec) {
    const f = card(sec, { flush: true });
    const box = el('div', 'cols');
    (sec.cols || []).forEach(function (col) {
      const k = el('div', 'kcol');
      const head = el('div', 'khead');
      paint(head.appendChild(el('span', null, col.title)), col.c);
      head.appendChild(el('span', 'n', String((col.cards || []).length)));
      k.appendChild(head);
      (col.cards || []).forEach(function (cd) {
        const kc = el('div', 'kcard' + (cd.do ? ' go' : ''));
        const t = el('div', 'kt');
        paint(t, cd.tc || 'text');
        t.textContent = cd.t || cd.title || '';
        kc.appendChild(t);
        if (cd.sub) kc.appendChild(el('div', 'ks', cd.sub));
        if (cd.acts && cd.acts.length) kc.appendChild(buttons(cd.acts, 'acts'));
        if (cd.do) kc.addEventListener('click', function () { act(cd.do); });
        k.appendChild(kc);
      });
      box.appendChild(k);
    });
    f.body.appendChild(box);
    return f.card;
  };

  R.callout = function (sec) {
    const tone = sec.tone || 'orange';
    const f = card(Object.assign({}, sec, { title: '' }), { hl: null });
    const box = el('div', 'callout ' + tone);
    const ico = el('span', 'ico');
    paint(ico, tone);
    ico.textContent = sec.icon || { green: '✓', red: '✗', yellow: '!', blue: '●', purple: '◆' }[tone] || '•';
    box.appendChild(ico);
    const txt = el('div', 'txt');
    const t = el('div', 'ttl');
    paint(t, tone);
    t.textContent = sec.title || '';
    txt.appendChild(t);
    if (sec.text_md) {
      const m = markdown(sec.text_md);
      m.classList.add('txt');
      txt.appendChild(m);
    }
    if (sec.meta && sec.meta.length) {
      const meta = el('div', 'meta');
      (sec.meta || []).forEach(function (m) {
        if (!m) return;
        const s = el('span', m.mono ? 'mono' : null);
        paint(s, m.c);
        s.textContent = m.t == null ? '' : m.t;
        meta.appendChild(s);
      });
      txt.appendChild(meta);
    }
    if (sec.actions && sec.actions.length) txt.appendChild(buttons(sec.actions, 'acts'));
    box.appendChild(txt);
    f.body.appendChild(box);
    return f.card;
  };

  R.triage = function (sec) {
    const f = card(Object.assign({}, sec, { title: '' }));
    const box = el('div', 'triage');
    box.style.borderLeftColor = 'var(--' + (sec.tone || 'muted') + ')';
    if (sec.icon) {
      const i = el('span', 'secicon');
      paint(i, sec.tone || 'muted');
      i.textContent = sec.icon;
      box.appendChild(i);
    }
    const txt = el('div');
    const t = el('div');
    paint(t, 'text');
    t.textContent = sec.text || '';
    txt.appendChild(t);
    if (sec.detail) txt.appendChild(el('div', 'sub', sec.detail));
    box.appendChild(txt);
    if (sec.actions && sec.actions.length) box.appendChild(buttons(sec.actions, 'a'));
    if (sec.counts && sec.counts.length) {
      const c = el('div', 'cnt');
      sec.counts.forEach(function (x) {
        if (!x) return;
        const s = el('span');
        paint(s, x.c);
        s.textContent = x.t;
        c.appendChild(s);
      });
      box.appendChild(c);
    }
    f.body.appendChild(box);
    return f.card;
  };

  R.hero = function (sec) {
    const f = card(sec);
    const box = el('div', 'hero');
    if (sec.goal) box.appendChild(el('div', 'goal', sec.goal));
    if (sec.pill) {
      const p = el('span', 'chip');
      paint(p, sec.pill.c);
      p.textContent = sec.pill.t;
      box.appendChild(p);
    }
    if (sec.criteria && sec.criteria.length) {
      const ul = el('ul');
      sec.criteria.forEach(function (c) { ul.appendChild(el('li', null, c)); });
      box.appendChild(ul);
    }
    if (sec.cells && sec.cells.length) {
      const cells = el('div', 'cells');
      sec.cells.forEach(function (c) {
        const cell = el('div', 'cell');
        const v = el('div', 'v');
        paint(v, c.c || 'text');
        v.textContent = c.v;
        cell.appendChild(v);
        cell.appendChild(el('div', 'l', c.label));
        if (c.sub) {
          const s = el('div', 's');
          paint(s, c.sc || 'sub');
          s.textContent = c.sub;
          cell.appendChild(s);
        }
        cells.appendChild(cell);
      });
      box.appendChild(cells);
    }
    if (sec.meta && sec.meta.length) {
      const meta = el('div', 'meta');
      sec.meta.forEach(function (m) {
        if (!m) return;
        const s = el('span', m.mono ? 'mono' : null);
        paint(s, m.c);
        s.textContent = m.t;
        meta.appendChild(s);
      });
      box.appendChild(meta);
    }
    f.body.appendChild(box);
    return f.card;
  };

  R.dag = function (sec) {
    const f = card(sec);
    const box = el('div', 'dagcols');
    (sec.cols || []).forEach(function (col) {
      const c = el('div', 'dagcol');
      (col || []).forEach(function (n) {
        const node = el('div', 'dagnode' + (n.sel ? ' sel' : ''));
        node.setAttribute('data-state', n.state || 'pending');
        const l = el('div', 'nl');
        l.textContent = n.label || n.id;
        node.appendChild(l);
        const lane = el('div', 'nlane mono');
        lane.style.setProperty('--fam', 'var(--fam-' + String(n.lane || '').split(/[_.]/)[0] + ', var(--sub))');
        lane.style.color = 'var(--fam)';
        lane.textContent = (n.lane || '') + (n.cost ? ' · ' + n.cost : '');
        node.appendChild(lane);
        node.title = n.id + ' · ' + n.state;
        const go = n.do || { page: 'run', args: { run: currentRun() }, tab: 'nodes' };
        node.addEventListener('click', function () { act(go); });
        c.appendChild(node);
      });
      box.appendChild(c);
    });
    f.body.appendChild(box);
    if (sec.legend) f.card.appendChild(el('div', 'note', sec.legend));
    return f.card;
  };

  R.inspector = function (sec) {
    const f = card(sec);
    const head = el('div', 'row');
    head.style.display = 'flex';
    head.style.gap = '10px';
    head.style.alignItems = 'baseline';
    head.appendChild(el('span', 'mono bold', sec.label));
    const role = el('span', 'sub');
    role.textContent = sec.role + ' · ' + sec.state;
    head.appendChild(role);
    f.body.appendChild(head);
    const pre = el('div', 'code');
    pre.style.marginTop = '6px';
    (sec.lines || []).forEach(function (ln) {
      const d = el('span', 'ln');
      paint(d, ln.c || 'body');
      d.textContent = ln.t;
      pre.appendChild(d);
    });
    f.body.appendChild(pre);
    if (sec.items && sec.items.length) {
      const grid = el('div', 'kvlist');
      sec.items.forEach(function (it) {
        const row = el('div', 'kv');
        row.appendChild(el('span', 'k', it.k));
        const v = el('span', 'v');
        paint(v, it.c);
        v.textContent = it.v;
        row.appendChild(v);
        grid.appendChild(row);
      });
      f.body.appendChild(grid);
    }
    return f.card;
  };

  R.story = function (sec) {
    const f = card(sec, { flush: true });
    (sec.steps || []).forEach(function (st, i) {
      const key = (sec.title || '') + '#' + (st.id || i);
      const row = el('div', 'storystep');
      const head = el('div', 'storyhead');
      const mk = el('span', 'mark');
      mk.textContent = stateMark(st.state);
      paint(mk, stateColor(st.state));
      head.appendChild(mk);
      const txt = el('div');
      txt.style.minWidth = '0';
      txt.style.flex = '1';
      txt.appendChild(el('div', 'ttl', st.title));
      const meta = el('div', 'meta');
      [st.kind, st.lane, st.model, st.dur, st.cost].filter(Boolean).forEach(function (x) {
        meta.appendChild(el('span', null, x));
      });
      txt.appendChild(meta);
      if (st.headline) {
        const h = el('div', 'meta');
        paint(h.appendChild(el('span', null, st.headline.t)), st.headline.c);
        txt.appendChild(h);
      }
      head.appendChild(txt);
      if (st.do) {
        const b = btn('Open', st.do, 'ghost');
        head.appendChild(b);
      }
      const caret = el('span', 'rt', ui.storyOpen[key] ? '▾' : '▸');
      head.appendChild(caret);
      row.appendChild(head);

      const hasBody = st.body && st.body.length;
      if (hasBody && ui.storyOpen[key]) {
        row.appendChild(renderBlocks(st.body));
      }
      if (hasBody) {
        head.addEventListener('click', function () {
          ui.storyOpen[key] = !ui.storyOpen[key];
          render();
        });
      }
      f.body.appendChild(row);
    });
    return f.card;
  };

  function renderBlocks(blocks) {
    const box = el('div', 'storybody');
    (blocks || []).forEach(function (b) {
      if (!b) return;
      if (b.kind === 'md') box.appendChild(markdown(b.text));
      else if (b.kind === 'lines') {
        const pre = el('div', 'code');
        (b.lines || []).forEach(function (ln) {
          const d = el('span', 'ln');
          paint(d, ln.c || 'body');
          d.textContent = ln.t;
          pre.appendChild(d);
        });
        box.appendChild(pre);
      } else if (b.kind === 'findings') {
        (b.items || []).forEach(function (it) {
          const row = el('div', 'litem');
          const mk = el('span', 'mark');
          mk.textContent = it.severity === 'high' ? '✗' : it.severity === 'medium' ? '!' : '•';
          paint(mk, it.severity === 'high' ? 'red' : it.severity === 'medium' ? 'yellow' : 'sub');
          row.appendChild(mk);
          const t = el('div', 'txt');
          t.appendChild(el('div', 'ttl', it.issue));
          const where = [it.file, it.line].filter(Boolean).join(':');
          if (where) t.appendChild(el('div', 'sub mono', where));
          row.appendChild(t);
          if (it.abs || it.file) {
            row.appendChild(btn('Open', { path: it.abs || it.file }, 'ghost'));
          }
          box.appendChild(row);
        });
      } else if (b.kind === 'files') {
        (b.files || []).forEach(function (f2) {
          const row = el('div', 'litem go');
          row.appendChild(el('span', 'mark mono', f2.status || 'M'));
          const t = el('div', 'txt');
          t.appendChild(el('div', 'ttl mono', f2.path));
          t.appendChild(el('div', 'sub mono', '+' + f2.added + ' −' + f2.removed));
          row.appendChild(t);
          if (f2.abs) row.addEventListener('click', function () { act({ path: f2.abs }); });
          box.appendChild(row);
        });
        if (b.diff) box.appendChild(markdown('```\n' + b.diff + '\n```'));
      } else if (b.kind === 'checks') {
        box.appendChild(checksList(b.rows));
      }
    });
    return box;
  }

  function checksList(rows) {
    const box = el('div', 'checklist');
    (rows || []).forEach(function (r) {
      const row = el('div', 'checkrow');
      const mk = el('span', 'mark');
      mk.textContent = stateMark(r.state);
      paint(mk, stateColor(r.state));
      row.appendChild(mk);
      const txt = el('div');
      txt.style.minWidth = '0';
      txt.style.flex = '1';
      txt.appendChild(el('div', 'nm', r.name));
      if (r.detail) txt.appendChild(el('div', 'dt', r.detail));
      if (r.log && r.log.length) {
        const pre = el('div', 'code');
        pre.style.maxHeight = '160px';
        pre.style.marginTop = '4px';
        r.log.forEach(function (ln) {
          const d = el('span', 'ln');
          paint(d, ln.c || 'body');
          d.textContent = ln.t;
          pre.appendChild(d);
        });
        txt.appendChild(pre);
      }
      row.appendChild(txt);
      if (r.do) row.appendChild(btn('Open', r.do, 'ghost'));
      box.appendChild(row);
    });
    return box;
  }

  R.checks = function (sec) {
    const f = card(sec, { flush: true });
    f.body.appendChild(checksList(sec.rows));
    if (sec.summary) {
      const s = el('div', 'note');
      const sum = el('div', 'summary');
      ['passing', 'failing', 'pending', 'na'].forEach(function (k) {
        if (!sec.summary[k]) return;
        const n = el('span');
        paint(n, k === 'passing' ? 'green' : k === 'failing' ? 'red' : k === 'pending' ? 'yellow' : 'sub');
        n.textContent = sec.summary[k] + ' ' + k;
        sum.appendChild(n);
      });
      s.appendChild(sum);
      f.card.appendChild(s);
    }
    return f.card;
  };

  R.findings = function (sec) {
    const f = card(sec, { flush: !!(sec.items || []).length });
    if (sec.verdict) {
      const v = el('div', 'note');
      const c = el('span', 'chip');
      paint(c, sec.verdict.c);
      c.textContent = sec.verdict.t;
      v.appendChild(c);
      if (sec.reasons && sec.reasons.length) {
        v.appendChild(document.createTextNode(' ' + sec.reasons.join(' · ')));
      }
      f.card.insertBefore(v, f.body);
    }
    (sec.items || []).forEach(function (it) {
      const row = el('div', 'litem');
      const mk = el('span', 'mark');
      mk.textContent = it.severity === 'high' ? '✗' : it.severity === 'medium' ? '!' : '•';
      paint(mk, it.severity === 'high' ? 'red' : it.severity === 'medium' ? 'yellow' : 'sub');
      row.appendChild(mk);
      const txt = el('div', 'txt');
      txt.appendChild(el('div', 'ttl', it.issue));
      const where = [it.file, it.line].filter(Boolean).join(':');
      if (where) txt.appendChild(el('div', 'sub mono', where));
      if (it.snippet) {
        const pre = el('div', 'code');
        pre.style.color = 'var(--sub)';
        pre.textContent = it.snippet;
        txt.appendChild(pre);
      }
      row.appendChild(txt);
      if (it.abs || it.file) row.appendChild(btn('Open', { path: it.abs || it.file }, 'ghost'));
      f.body.appendChild(row);
    });
    if (!(sec.items || []).length) f.body.appendChild(el('div', 'empty', 'No findings.'));
    return f.card;
  };

  R.files = function (sec) {
    const f = card(sec, { flush: true });
    (sec.files || []).forEach(function (it) {
      const row = el('div', 'litem' + (it.abs ? ' go' : ''));
      const mk = el('span', 'mark mono');
      paint(mk, it.status === 'D' ? 'red' : it.status === 'A' ? 'green' : 'yellow');
      mk.textContent = it.status || 'M';
      row.appendChild(mk);
      const t = el('div', 'txt');
      t.appendChild(el('div', 'ttl mono', it.path));
      t.appendChild(el('div', 'sub mono', '+' + (it.added || 0) + ' −' + (it.removed || 0)));
      row.appendChild(t);
      if (it.abs) row.addEventListener('click', function () { act({ path: it.abs }); });
      f.body.appendChild(row);
    });
    (sec.commits || []).forEach(function (cm) {
      const row = el('div', 'litem');
      row.appendChild(el('span', 'mark mono', '◆'));
      const t = el('div', 'txt');
      t.appendChild(el('div', 'ttl mono', typeof cm === 'string' ? cm : cm.title || cm.sha || ''));
      row.appendChild(t);
      f.body.appendChild(row);
    });
    if (sec.diff) {
      const pre = el('div', 'code');
      pre.style.maxHeight = '420px';
      pre.style.padding = '8px 11px';
      pre.style.borderTop = '1px solid var(--line)';
      pre.textContent = sec.diff;
      f.body.appendChild(pre);
    }
    if (sec.diff_note) f.card.appendChild(el('div', 'note', sec.diff_note));
    if (!(sec.files || []).length && !sec.diff) {
      f.body.appendChild(el('div', 'empty', 'No changed files.'));
    }
    return f.card;
  };

  R.levels = function (sec) {
    const rows = (sec.rows || []).map(function (r) {
      const mark = r.required === true ? '●' : r.required === false ? '○' : '?';
      const c = r.required === true ? 'green' : r.required === false ? 'dim' : 'yellow';
      return {
        cells: [
          { t: r.value || '', mono: true, c: 'text' },
          { t: r.name || '', c: 'body' },
          { t: mark, c: c },
          { t: r.reason || '', c: 'sub' },
        ],
      };
    });
    return R.table({
      type: 'table',
      title: sec.title,
      sub: sec.task_class ? sec.task_class + (sec.decision ? ' · ' + sec.decision : '') : sec.decision,
      cols: [{ w: 80 }, { fr: 1, min: 120 }, { w: 44 }, { fr: 1.4, min: 140 }],
      head: ['value', 'level', 'req', 'reason'],
      rows: rows,
      note: sec.note,
      full: sec.full,
    });
  };

  R.agents = function (sec) {
    const rows = (sec.rows || []).map(function (r) {
      return {
        cells: [
          { t: stateMark(r.state), c: stateColor(r.state) },
          { t: r.id, mono: true, c: 'text' },
          { t: r.lane || '', mono: true, c: 'muted' },
          { t: r.model || '', c: 'muted' },
          { t: r.step || '', c: 'body', wrap: true },
          { t: r.cost || '', mono: true },
          { t: r.dur || '', mono: true },
        ],
        do: r.do,
      };
    });
    return R.table({
      type: 'table',
      title: sec.title,
      cols: [{ w: 20 }, { fr: 1, min: 100 }, { w: 96 }, { w: 110 }, { fr: 1.6, min: 140 }, { w: 60 }, { w: 54 }],
      head: ['', 'node', 'lane', 'model', 'step', 'cost', 'dur'],
      rows: rows,
      note: sec.note,
      full: sec.full,
    });
  };

  R.composer = function (sec) {
    const f = card(sec);
    const box = el('div', 'composer');
    const ta = el('textarea');
    ta.placeholder = sec.placeholder || '';
    ta.rows = 3;
    // A draft outlives the DOM node it was typed into.
    const draftKey = (ui.page && ui.page.key) + '::' + (sec.title || '') + '::' + (sec.cli || []).join(' ');
    if (drafts[draftKey]) ta.value = drafts[draftKey];
    ta.dataset.draftKey = draftKey;
    ta.addEventListener('input', function () {
      if (ta.value) drafts[draftKey] = ta.value;
      else delete drafts[draftKey];
    });
    // A re-render replaces this node; remembering which field had the caret
    // lets the rebuild put the cursor back where the user left it.
    ta.addEventListener('focus', function () { focusedDraft = draftKey; });
    ta.addEventListener('blur', function () { if (focusedDraft === draftKey) focusedDraft = null; });
    box.appendChild(ta);
    const row = el('div', 'row');
    const send = btn(sec.action_label || 'Run', null, 'primary');
    const preview = el('div', 'preview');
    send.disabled = true;
    row.appendChild(send);
    if (sec.actions && sec.actions.length) row.appendChild(buttons(sec.actions, 'a'));
    const hint = el('span', 'hint', sec.hint || '⌘↵ to run');
    row.appendChild(hint);
    box.appendChild(row);
    box.appendChild(preview);

    function argv() {
      const text = ta.value.trim();
      return (sec.cli || []).concat(text ? [text] : []);
    }
    function sync() {
      const a = argv();
      send.disabled = !ta.value.trim();
      preview.textContent = 'mini-ork ' + a.join(' ');
    }
    ta.addEventListener('input', sync);
    ta.addEventListener('keydown', function (ev) {
      if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
        ev.preventDefault();
        if (ta.value.trim()) submit();
      }
    });
    function submit() {
      const a = argv();
      if (!a.length) return;
      // The text became a command; keeping it would make the next run repeat it.
      delete drafts[draftKey];
      focusedDraft = null;
      act({ cli: a, home: sec.home !== false });
    }
    send.addEventListener('click', submit);
    sync();
    f.body.appendChild(box);
    return f.card;
  };

  R.document = function (sec) {
    const f = card(sec);
    if (sec.kind === 'text') {
      f.body.appendChild(markdown(sec.text || ''));
    } else if (sec.kind === 'grid') {
      const tbl = R.table({
        type: 'table',
        title: '',
        cols: (sec.columns || []).map(function () { return { fr: 1, min: 80 }; }),
        head: sec.columns || [],
        rows: (sec.rows || []).map(function (r) {
          return { cells: r.map(function (v) { return { t: v == null ? '' : String(v), mono: true }; }) };
        }),
        note: sec.truncated ? 'showing ' + (sec.rows || []).length + ' of ' + sec.total_rows + ' rows' : '',
      });
      f.body.appendChild(tbl.querySelector('.tbl') || el('div'));
    } else {
      const list = el('div', 'litems');
      (sec.pages || []).forEach(function (p) {
        const row = el('div', 'litem' + (p.png ? ' go' : ''));
        row.appendChild(el('span', 'mark mono', String(p.page)));
        const t = el('div', 'txt');
        t.appendChild(el('div', 'ttl', 'page ' + p.page));
        t.appendChild(el('div', 'sub mono', p.width + '×' + p.height));
        row.appendChild(t);
        if (p.png) row.addEventListener('click', function () { act({ path: p.png }); });
        list.appendChild(row);
      });
      f.body.appendChild(list);
    }
    return f.card;
  };

  function stateMark(state) {
    return {
      done: '✓',
      ok: '✓',
      pass: '✓',
      running: '●',
      working: '●',
      failed: '✗',
      fail: '✗',
      error: '✗',
      skipped: '–',
      pending: '○',
      queued: '○',
      needs_you: '✋',
      needs: '✋',
      warn: '!',
    }[state] || '•';
  }

  function stateColor(state) {
    return {
      done: 'green',
      ok: 'green',
      pass: 'green',
      running: 'blue',
      working: 'blue',
      failed: 'red',
      fail: 'red',
      error: 'red',
      needs_you: 'yellow',
      needs: 'yellow',
      warn: 'yellow',
      pending: 'dim',
      queued: 'dim',
      skipped: 'dim',
    }[state] || 'sub';
  }

  function currentRun() {
    if (!ui.page) return '';
    return (ui.page.args && ui.page.args.run) || '';
  }

  // ── page shell ───────────────────────────────────────────────────────
  const LABEL = {
    orch: 'Dispatch',
    inbox: 'Inbox',
    kanban: 'Board',
    runs: 'Runs',
    changes: 'Changes',
    verify: 'Verify & safety',
    recipes: 'Recipes & epics',
    autos: 'Automations',
    lanes: 'Lanes & cost',
    learn: 'Learning & memory',
    context: 'Context',
    nodes: 'Nodes',
    dags: 'DAGs',
    setup: 'Setup & health',
    features: 'Features',
    run: 'Run',
    task: 'Task',
    thread: 'Threads',
    cn_session: 'CN session',
    cn_file: 'CN file',
  };
  const GLYPH = {
    orch: '✦',
    inbox: '✉',
    kanban: '▦',
    runs: '≡',
    changes: '±',
    verify: '✓',
    recipes: '▥',
    autos: '◷',
    lanes: '$',
    learn: '❖',
    context: '◫',
    nodes: '⬡',
    dags: '⋔',
    setup: '⚙',
    features: '⌗',
    run: '◇',
    task: '❑',
  };

  function railPages() {
    return ['orch', 'inbox', 'kanban', 'runs', 'thread', 'changes', 'verify', 'recipes', 'autos', 'lanes', 'learn', 'context', 'nodes', 'dags', 'setup', 'features'];
  }

  function renderRail() {
    const rail = document.getElementById('rail');
    rail.textContent = '';
    const counts = ui.counts || {};
    const badge = {
      inbox: counts.needs_you || 0,
      runs: (counts.working || 0) + (counts.failed || 0),
    };
    railPages().forEach(function (k) {
      const b = el('button', ui.page && ui.page.key === k ? 'on' : '');
      b.appendChild(el('span', 'g', GLYPH[k] || '•'));
      b.appendChild(el('span', null, LABEL[k] || k));
      const n = badge[k];
      if (n) b.appendChild(el('span', 'n', String(n)));
      b.addEventListener('click', function () { post('openPage', { key: k }); });
      rail.appendChild(b);
    });
  }

  function renderTopbar() {
    const h = ui.header || {};
    document.getElementById('project').textContent = h.project || 'mini-ork';
    document.getElementById('branch').textContent = h.branch ? '⎇ ' + h.branch : '';
    document.getElementById('worktrees').textContent = h.worktrees ? '· ' + h.worktrees + ' worktrees' : '';
    const needs = document.getElementById('needs');
    const n = h.needs_you || 0;
    needs.textContent = '✋ ' + n + ' need you';
    needs.style.display = n ? '' : 'none';
    const cost = document.getElementById('cost');
    cost.textContent = h.today_usd != null
      ? '$' + Number(h.today_usd).toFixed(2) + (h.cap_usd ? ' of $' + Number(h.cap_usd).toFixed(2) + ' today' : ' today')
      : '';
    const sched = document.getElementById('sched');
    if (h.scheduler_on != null) {
      sched.textContent = (h.scheduler_on ? '◷ scheduler on' : '◷ scheduler off');
      sched.className = h.scheduler_on ? '' : 'c-dim';
    } else {
      sched.textContent = '';
    }
    const nodes = document.getElementById('nodecount');
    const ns = h.nodes || {};
    const online = ns.online != null ? ns.online : (ns.local ? 1 : 0);
    nodes.textContent = online ? '○ ' + (ns.kind || 'local') : '';
    const pill = document.getElementById('backend');
    const b = ui.backend || {};
    pill.textContent = b.state === 'ready'
      ? (b.mode === 'attached' ? 'attached' : 'local')
      : b.state || 'offline';
    pill.className = 'flag ' + (b.state === 'ready' ? 'c-green' : b.state === 'stopped' ? 'c-dim' : 'c-yellow');
    pill.title = b.detail || b.baseUrl || '';
    pill.addEventListener('click', function () { post('backend', {}); });
    if (h.contextnest) {
      const cn = document.getElementById('cn');
      cn.textContent = h.contextnest.reachable ? '◫ cn' : '◫ cn off';
      cn.className = 'flag ' + (h.contextnest.reachable ? 'c-green' : 'c-dim');
      cn.title = h.contextnest.detail || '';
    }
  }

  function renderTabs() {
    const strip = document.getElementById('tabstrip');
    strip.textContent = '';
    (ui.tabs || []).forEach(function (t, i) {
      const b = el('button', 'tab' + (i === ui.active ? ' on' : ''));
      b.appendChild(el('span', null, t.label || LABEL[t.key] || t.key));
      if (t.pinned !== true) {
        const x = el('span', 'x', '×');
        x.addEventListener('click', function (ev) {
          ev.stopPropagation();
          post('closeTab', { index: i });
        });
        b.appendChild(x);
      }
      b.addEventListener('click', function () { post('activateTab', { index: i }); });
      strip.appendChild(b);
    });
  }

  function renderPage() {
    ui.renderedRev = ui.pageRev;
    ui.renderedLoading = ui.loading;
    // Rebuilding the body must not move the reader: restore the scroll offset
    // and the caret that the teardown would otherwise discard.
    const scroll = viewport.scrollTop;
    viewport.textContent = '';
    const page = ui.page;

    if (!page) {
      viewport.appendChild(el('div', 'loading', ui.loading ? 'Loading…' : 'No page open.'));
      return;
    }
    if (page.ok === false) {
      viewport.appendChild(el('div', 'errbox', page.error || 'The page could not be built.'));
      return;
    }

    if (page.errors && Object.keys(page.errors).length) {
      const box = el('div', 'errbox');
      Object.keys(page.errors).forEach(function (k) {
        box.appendChild(el('div', null, k + ': ' + page.errors[k]));
      });
      viewport.appendChild(box);
    }

    const head = el('div', 'pagehead');
    const left = el('div');
    // `crumbs` is contract v2.1 and optional: page › tab › run, each a target.
    if (page.crumbs && page.crumbs.length) {
      const trail = el('div', 'crumbs');
      page.crumbs.forEach(function (c, i) {
        if (i) trail.appendChild(el('span', 'sep', '›'));
        const node = c.do ? el('button', 'crumb do', c.t) : el('span', 'crumb', c.t);
        if (c.do) node.addEventListener('click', function () { act(c.do); });
        trail.appendChild(node);
      });
      left.appendChild(trail);
    }
    const title = el('h1');
    title.appendChild(el('span', 'secicon', GLYPH[page.key] || '◇'));
    title.appendChild(document.createTextNode(' ' + (page.title || LABEL[page.key] || page.key)));
    left.appendChild(title);
    if (page.sub) left.appendChild(el('div', 'sub', page.sub));
    if (page.desc) left.appendChild(el('div', 'desc', page.desc));
    head.appendChild(left);
    const acts = el('div', 'actions');
    if (page.updated_at) {
      acts.appendChild(el('span', 'updated', 'updated ' + relativeTime(page.updated_at)));
    }
    if (page.actions && page.actions.length) {
      page.actions.forEach(function (a) { acts.appendChild(btn(a.label, a.do, a.kind)); });
    }
    // Secondary actions live behind a ▾ rather than crowding the header.
    if (page.menu && page.menu.length) {
      acts.appendChild(menuButton(page.menu));
    }
    if (acts.childNodes.length) head.appendChild(acts);
    viewport.appendChild(head);

    if (page.chips && page.chips.length) {
      const chips = el('div', 'chiprow');
      page.chips.forEach(function (c) {
        const s = el('span', 'chip ' + (c.c && c.c !== 'sub' ? c.c : ''));
        if (c.do) {
          s.classList.add('do');
          s.addEventListener('click', function () { act(c.do); });
        }
        s.textContent = c.t;
        chips.appendChild(s);
      });
      viewport.appendChild(chips);
    }

    if (page.tabs && page.tabs.length) {
      const bar = el('div', 'pagetabs');
      page.tabs.forEach(function (t) {
        const b = el('button', 'pagetab' + (t.key === page.tab ? ' on' : ''));
        b.appendChild(document.createTextNode(t.label || t.key));
        // The engine emits `count` + `tone`; a bare `n` is the older shape.
        const badge = t.count !== undefined ? t.count : t.n;
        if (badge !== undefined && badge !== null && badge !== '' && badge !== 0) {
          const s = el('span', 'n', String(badge));
          if (t.tone) s.classList.add('c-' + t.tone);
          b.appendChild(s);
        }
        b.addEventListener('click', function () {
          post('openPage', { key: page.key, tab: t.key, args: page.args, keepTab: true });
        });
        bar.appendChild(b);
      });
      viewport.appendChild(bar);
    }

    const grid = el('div', 'grid');
    let pending = []; // sections without `full` that are not `col`-pinned

    function flushPending() {
      pending.forEach(function (s) { grid.appendChild(s); });
      pending = [];
    }

    (page.sections || []).forEach(function (sec) {
      if (!sec || !sec.type) return;
      const render = R[sec.type];
      let node;
      if (render) {
        try {
          node = render(sec);
        } catch (err) {
          node = el('div', 'errbox', sec.type + ' could not render: ' + (err && err.message));
        }
      } else {
        node = unrendered(sec);
      }
      if (sec.full) {
        flushPending();
        grid.appendChild(node);
      } else if (sec.col === 2) {
        node.classList.add('col2');
        grid.appendChild(node);
      } else if (sec.col === 1) {
        flushPending();
        grid.appendChild(node);
      } else {
        pending.push(node);
        if (pending.length === 2) flushPending();
      }
    });
    flushPending();
    viewport.appendChild(grid);
    viewport.scrollTop = scroll;

    if (focusedDraft) {
      const again = viewport.querySelector('textarea[data-draft-key="' + cssEscape(focusedDraft) + '"]');
      if (again) {
        again.focus();
        again.setSelectionRange(again.value.length, again.value.length);
      }
    }
  }

  /** Escape a value for use inside a quoted attribute selector. */
  function cssEscape(value) {
    return String(value).replace(/["\\]/g, '\\$&');
  }

  /** A section type this build does not know: show its shape, never drop it. */
  function unrendered(sec) {
    const f = card(sec);
    const pre = el('div', 'code');
    pre.style.maxHeight = '240px';
    pre.textContent = JSON.stringify(sec, null, 2);
    f.body.appendChild(pre);
    const note = el('div', 'note', 'section type "' + sec.type + '" is not rendered by this build');
    f.card.appendChild(note);
    return f.card;
  }

  // ── toast ────────────────────────────────────────────────────────────
  let toastTimer = null;
  function toast(text, sub, tone) {
    const box = document.getElementById('toast');
    box.textContent = '';
    box.className = 'toast on ' + (tone || '');
    box.appendChild(el('div', 't', text));
    if (sub) box.appendChild(el('div', 's', sub));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.className = 'toast'; }, 4200);
  }

  // ── modal: confirm ───────────────────────────────────────────────────
  function showConfirm(req) {
    ui.confirm = req;
    renderConfirm();
  }
  function renderConfirm() {
    const host = document.getElementById('modals');
    host.textContent = '';
    if (!ui.confirm) return;
    const scrim = el('div', 'scrim');
    const m = el('div', 'modal');
    m.appendChild(el('div', 'mh', ui.confirm.title || 'Confirm'));
    const body = el('div', 'mb');
    if (ui.confirm.body) body.appendChild(el('div', null, ui.confirm.body));
    if (ui.confirm.cmd) body.appendChild(el('code', 'cmd', ui.confirm.cmd));
    m.appendChild(body);
    const foot = el('div', 'mf');
    const cancel = btn('Cancel', null, 'ghost');
    cancel.disabled = false;
    cancel.addEventListener('click', function () {
      ui.confirm = null;
      renderConfirm();
    });
    foot.appendChild(cancel);
    const go = btn(ui.confirm.confirmLabel || 'Run', null, ui.confirm.danger ? 'danger' : 'primary');
    go.disabled = false;
    go.addEventListener('click', function () {
      const req = ui.confirm;
      ui.confirm = null;
      renderConfirm();
      post('confirmAct', { id: req.id });
    });
    foot.appendChild(go);
    m.appendChild(foot);
    scrim.appendChild(m);
    scrim.addEventListener('click', function (ev) {
      if (ev.target === scrim) {
        ui.confirm = null;
        renderConfirm();
      }
    });
    host.appendChild(scrim);
  }

  // ── palette (⌘K) ────────────────────────────────────────────────────
  function openPalette() {
    ui.palette = { q: '', index: 0, results: [] };
    renderPalette();
  }
  function paletteItems(q) {
    const items = [];
    railPages().forEach(function (k) {
      items.push({ label: LABEL[k] || k, glyph: GLYPH[k] || '•', do: { page: k }, kind: 'page' });
    });
    (ui.runs || []).forEach(function (r) {
      items.push({
        label: r.title || r.id,
        glyph: '◇',
        hint: r.state + ' · ' + r.recipe,
        do: { run: r.id, title: r.title, tab: 'outcome' },
        kind: 'run',
      });
    });
    const needle = String(q || '').toLowerCase();
    if (!needle) return items.slice(0, 40);
    return items.filter(function (i) {
      return (i.label + ' ' + (i.hint || '')).toLowerCase().indexOf(needle) !== -1;
    }).slice(0, 40);
  }
  function renderPalette() {
    const host = document.getElementById('modals');
    // The palette owns the overlay while open; keep a pending confirm behind it.
    const existing = host.querySelector('.scrim.palette-scrim');
    if (existing) existing.remove();
    if (!ui.palette) return;
    const results = paletteItems(ui.palette.q);
    ui.palette.results = results;
    if (ui.palette.index >= results.length) ui.palette.index = Math.max(0, results.length - 1);

    const scrim = el('div', 'scrim palette-scrim');
    const box = el('div', 'palette');
    const input = el('input');
    input.value = ui.palette.q;
    input.placeholder = 'Search runs and pages…';
    input.addEventListener('input', function () {
      ui.palette.q = input.value;
      ui.palette.index = 0;
      const at = input.selectionStart;
      renderPalette();
      const again = document.querySelector('.palette input');
      if (again) {
        again.focus();
        again.setSelectionRange(at, at);
      }
    });
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { ui.palette = null; renderPalette(); return; }
      if (ev.key === 'ArrowDown' || (ev.key === 'n' && ev.ctrlKey)) {
        ev.preventDefault();
        ui.palette.index = Math.min(ui.palette.index + 1, results.length - 1);
        renderPalette();
        document.querySelector('.palette input').focus();
        return;
      }
      if (ev.key === 'ArrowUp' || (ev.key === 'p' && ev.ctrlKey)) {
        ev.preventDefault();
        ui.palette.index = Math.max(0, ui.palette.index - 1);
        renderPalette();
        document.querySelector('.palette input').focus();
        return;
      }
      if (ev.key === 'Enter') {
        const pick = results[ui.palette.index];
        ui.palette = null;
        renderPalette();
        if (pick) act(pick.do);
      }
    });
    box.appendChild(input);
    const list = el('div', 'results');
    results.forEach(function (r, i) {
      const row = el('div', 'r' + (i === ui.palette.index ? ' on' : ''));
      row.appendChild(el('span', 'g', r.glyph));
      row.appendChild(el('span', null, r.label));
      if (r.hint) row.appendChild(el('span', 'k', r.hint));
      row.addEventListener('click', function () {
        ui.palette = null;
        renderPalette();
        act(r.do);
      });
      list.appendChild(row);
    });
    box.appendChild(list);
    scrim.appendChild(box);
    scrim.addEventListener('click', function (ev) {
      if (ev.target === scrim) { ui.palette = null; renderPalette(); }
    });
    host.appendChild(scrim);
    setTimeout(function () { input.focus(); }, 0);
  }

  // ── log drawer ───────────────────────────────────────────────────────
  function appendLog(entry) {
    ui.logs.push(entry);
    if (ui.logs.length > 800) ui.logs.shift();
    const atBottom = drawerLog.scrollTop + drawerLog.clientHeight >= drawerLog.scrollHeight - 24;
    drawerLog.appendChild(el('div', entry.kind === 'err' ? 'e' : entry.kind === 'cmd' ? 'c' : null, entry.text));
    if (atBottom) drawerLog.scrollTop = drawerLog.scrollHeight;
  }
  function renderDrawer() {
    drawer.className = 'drawer' + (ui.logOpen ? ' on' : '');
  }

  // ── full render ──────────────────────────────────────────────────────
  function render() {
    renderTopbar();
    renderRail();
    renderTabs();
    renderPage();
    renderDrawer();
  }

  // ── host messages ────────────────────────────────────────────────────
  window.addEventListener('message', function (ev) {
    const msg = ev.data || {};
    const p = msg.payload || {};
    switch (msg.type) {
      case 'state':
        if (p.header !== undefined) ui.header = p.header;
        if (p.counts !== undefined) ui.counts = p.counts || {};
        if (p.backend !== undefined) ui.backend = p.backend;
        if (p.tabs !== undefined) ui.tabs = p.tabs || [];
        if (p.active !== undefined) ui.active = p.active || 0;
        if (p.page !== undefined) ui.page = p.page;
        if (p.pageRev !== undefined) ui.pageRev = p.pageRev;
        if (p.loading !== undefined) ui.loading = p.loading;
        // The shell polls every few seconds with the same page stamped the same
        // way. Rebuilding the body for that would throw away a half-typed
        // composer sentence and the scroll position of a long page.
        renderTopbar();
        renderRail();
        renderTabs();
        if (
          p.pageRev === undefined ||
          ui.pageRev !== ui.renderedRev ||
          ui.loading !== ui.renderedLoading
        ) {
          renderPage();
        }
        renderDrawer();
        break;
      case 'page':
        ui.page = p.page;
        ui.loading = false;
        render();
        break;
      case 'tabs':
        ui.tabs = p.tabs || [];
        ui.active = p.active || 0;
        renderTabs();
        break;
      case 'toast':
        toast(p.text, p.sub, p.tone);
        break;
      case 'confirm':
        showConfirm(p);
        break;
      case 'log':
        ui.logs = p.reset ? [] : ui.logs;
        if (p.reset) drawerLog.textContent = '';
        (p.entries || []).forEach(appendLog);
        if (p.open) ui.logOpen = true;
        renderDrawer();
        break;
      case 'runs':
        ui.runs = p.runs || [];
        if (ui.palette) renderPalette();
        break;
      case 'focusSearch':
        openPalette();
        break;
      default:
        break;
    }
  });

  // ── local handlers ───────────────────────────────────────────────────
  // One listener for every menu popover, rather than one per render.
  document.addEventListener('click', function () {
    document.querySelectorAll('.menupop').forEach(function (p) { p.style.display = 'none'; });
  });
  document.getElementById('search').addEventListener('click', function () {
    post('search', {});
    openPalette();
  });
  document.getElementById('toggleRail').addEventListener('click', function () {
    ui.railOpen = !ui.railOpen;
    document.getElementById('rail').className = 'rail' + (ui.railOpen ? '' : ' collapsed');
  });
  document.getElementById('drawerClose').addEventListener('click', function () {
    ui.logOpen = false;
    renderDrawer();
  });
  document.getElementById('drawerClear').addEventListener('click', function () {
    drawerLog.textContent = '';
    ui.logs = [];
  });
  document.getElementById('logbtn').addEventListener('click', function () {
    ui.logOpen = !ui.logOpen;
    renderDrawer();
  });
  document.getElementById('refresh').addEventListener('click', function () {
    post('refresh', {});
  });
  document.getElementById('needs').addEventListener('click', function () {
    post('openPage', { key: 'inbox' });
  });

  window.addEventListener('keydown', function (ev) {
    const mod = ev.metaKey || ev.ctrlKey;
    if (mod && ev.key === 'k') {
      ev.preventDefault();
      post('search', {});
      openPalette();
      return;
    }
    if (ev.key === 'Escape') {
      if (ui.palette) { ui.palette = null; renderPalette(); }
      else if (ui.confirm) { ui.confirm = null; renderConfirm(); }
      return;
    }
    if (mod && ev.key === 'r') {
      ev.preventDefault();
      post('refresh', {});
    }
  });

  render();
  post('ready', {});
})();
