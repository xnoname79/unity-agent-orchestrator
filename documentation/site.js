/* Theme, section links, phone drawer, search and copy buttons for the user guide. No dependencies. */
(() => {
  const root = document.documentElement;
  const $ = (sel, from = document) => from.querySelector(sel);
  const $$ = (sel, from = document) => [...from.querySelectorAll(sel)];
  const make = (tag, cls = '', text = '') => Object.assign(document.createElement(tag), { className: cls, textContent: text });
  const tidy = s => s.trim().replace(/\s+/g, ' ');

  // Runs in <head>, before first paint, so a stored dark theme never flashes light.
  root.classList.add('js');
  try {
    const saved = localStorage.getItem('docs-theme');
    if (saved === 'dark' || saved === 'light') root.dataset.theme = saved;
  } catch { /* storage blocked: follow the OS */ }

  function setupTheme() {
    $('.theme-btn').addEventListener('click', () => {
      const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      root.dataset.theme = dark ? 'light' : 'dark';
      try { localStorage.setItem('docs-theme', root.dataset.theme); } catch { /* lasts for this page only */ }
    });
  }

  function setupSections() {
    const heads = $$('main h2[id]'), sub = make('ul', 'nav-sub');
    if (!heads.length) return;
    const links = heads.map(h => {
      const a = make('a', '', tidy(h.textContent));
      a.href = '#' + h.id;
      sub.appendChild(make('li')).appendChild(a);
      return a;
    });
    $('.nav-list a[aria-current="page"]').parentNode.appendChild(sub);
    const update = () => {
      let current = heads.findLastIndex(h => h.getBoundingClientRect().top <= innerHeight * 0.3);
      if (innerHeight + scrollY >= root.scrollHeight - 2) current = heads.length - 1;
      links.forEach((a, i) => i === current ? a.setAttribute('aria-current', 'location') : a.removeAttribute('aria-current'));
    };
    // One observer watches headings crossing the highlight line. The other catches any jump, and the page bottom.
    const cross = new IntersectionObserver(update, { rootMargin: '0px 0px -70% 0px' });
    heads.forEach(h => cross.observe(h));
    const edges = new IntersectionObserver(update, { threshold: [0, 1] });
    [...$('main').children, $('.site-foot')].forEach(el => edges.observe(el));
    update();
  }

  function setupDrawer() {
    const btn = $('.menu-btn'), side = $('#sidebar'), back = $('#backdrop');
    const set = open => {
      root.classList.toggle('drawer-open', open);
      btn.setAttribute('aria-expanded', String(open));
      $('.page').inert = open;
      back.hidden = !open;
    };
    btn.addEventListener('click', () => { set(true); $('a', side).focus(); });
    back.addEventListener('click', () => { set(false); btn.focus(); });
    side.addEventListener('click', e => { if (e.target.closest('a')) set(false); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && btn.getAttribute('aria-expanded') === 'true') { set(false); btn.focus(); }
    });
    matchMedia('(min-width: 1024px)').addEventListener('change', () => set(false));
  }

  // One entry per h1/h2/h3, holding the text up to the next heading.
  async function readPage(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html'), out = [];
    // Blocks written with no whitespace between them would otherwise run their words together.
    $$('p,li,td,th,.callout b,.card > *', doc).forEach(el => el.append(' '));
    const walk = doc.createTreeWalker($('main', doc), NodeFilter.SHOW_TEXT);
    let cur = null, el = null;
    while (walk.nextNode()) {
      const n = walk.currentNode, h = n.parentElement.closest('h1,h2,h3');
      if (h && h !== el) {
        const target = h.id ? h : h.tagName === 'H1' ? null : h.closest('section[id]');
        out.push(cur = { page: $('h1', doc).textContent, url: url + (target ? '#' + target.id : ''), head: '', text: '' });
        el = h;
      }
      if (h) cur.head += n.data;
      else if (cur) cur.text += n.data;
    }
    return out.map(e => ({ ...e, head: tidy(e.head), text: tidy(e.text) }));
  }

  function setupSearch() {
    const dlg = make('dialog', 'search');
    dlg.setAttribute('aria-label', 'Search documentation');
    dlg.innerHTML = '<input type="text" role="combobox" aria-label="Search documentation" aria-expanded="true" aria-controls="search-results" aria-autocomplete="list" autocomplete="off" spellcheck="false" placeholder="Search documentation…"><p class="search-status" role="status"></p><ul id="search-results" role="listbox" aria-label="Results"></ul>';
    document.body.appendChild(dlg);
    const input = $('input', dlg), status = $('.search-status', dlg), list = $('#search-results', dlg);
    const FAILED = 'Search works on the published site or a local server. This page was opened from a file, so it cannot read the other pages.';
    let rows = [], sel = -1, index = null;

    const load = () => index ||= Promise.all($$('.nav-list > li > a').map(a => readPage(a.getAttribute('href'))))
      .then(all => all.flat())
      .catch(() => { index = null; status.textContent = FAILED; return null; });

    function row(e, word, i) {
      const a = make('a');
      a.href = e.url;
      a.id = 'result-' + i;
      a.setAttribute('role', 'option');
      a.setAttribute('aria-selected', 'false');
      // Cut at spaces so a snippet never shows half a word.
      const at = e.text.toLowerCase().indexOf(word), from = at > 40 ? e.text.lastIndexOf(' ', at - 40) + 1 : 0;
      const end = e.text.indexOf(' ', from + 120);
      const snippet = (from ? '…' : '') + (end < 0 ? e.text.slice(from) : e.text.slice(from, end) + '…');
      a.append(make('span', 'result-page', e.page), make('span', 'result-head', e.head));
      if (snippet) a.append(make('span', '', snippet));
      const li = make('li');
      li.setAttribute('role', 'presentation');
      li.append(a);
      return li;
    }
    function select(i) {
      if (rows[sel]) rows[sel].setAttribute('aria-selected', 'false');
      sel = i;
      rows[sel].setAttribute('aria-selected', 'true');
      rows[sel].scrollIntoView({ block: 'nearest' });
      input.setAttribute('aria-activedescendant', rows[sel].id);
    }
    async function render() {
      const q = input.value.trim().toLowerCase(), words = q.split(/\s+/);
      list.textContent = '';
      rows = [];
      sel = -1;
      input.removeAttribute('aria-activedescendant');
      if (!q) { status.textContent = ''; return; }
      const entries = await load();
      if (!entries || input.value.trim().toLowerCase() !== q) return;
      const hasAll = s => words.every(w => s.toLowerCase().includes(w));
      const hits = entries.filter(e => hasAll(e.head + ' ' + e.text))
        .sort((a, b) => hasAll(b.head) - hasAll(a.head)).slice(0, 20);
      status.textContent = hits.length ? '' : `No results for “${input.value.trim()}”.`;
      list.replaceChildren(...hits.map((e, i) => row(e, words[0], i)));
      rows = $$('a', list);
    }
    function open() {
      if (!dlg.open) dlg.showModal();
      input.select();
      load();
    }
    input.addEventListener('input', render);
    input.addEventListener('keydown', e => {
      if (!rows.length) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); select((sel + 1) % rows.length); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); select((Math.max(sel, 0) - 1 + rows.length) % rows.length); }
      else if (e.key === 'Enter') { e.preventDefault(); rows[Math.max(sel, 0)].click(); }
    });
    // Closing here also covers a result on the current page, where nothing reloads.
    list.addEventListener('click', e => { if (e.target.closest('a')) dlg.close(); });
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
    $('.search-btn').addEventListener('click', open);
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); open(); }
    });
    if (/Mac/.test(navigator.platform)) $('.search-hint').textContent = '⌘ K';
  }

  function setupCopy() {
    $$('main pre').forEach(pre => {
      const wrap = make('div', 'code'), btn = make('button', 'copy-btn', 'Copy');
      let timer;
      btn.type = 'button';
      pre.before(wrap);
      wrap.append(pre, btn);
      const flash = text => {
        btn.textContent = text;
        clearTimeout(timer);
        timer = setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
      };
      btn.addEventListener('click', () => {
        (navigator.clipboard ? navigator.clipboard.writeText(pre.textContent) : Promise.reject())
          .then(() => flash('Copied'), () => flash('Copy failed'));
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    setupTheme();
    setupSections();
    setupDrawer();
    setupSearch();
    setupCopy();
  });
})();
