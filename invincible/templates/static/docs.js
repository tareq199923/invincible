// invincible/templates/static/docs.js
// Behavior for the public docs site (/docs). Vanilla, no dependencies -
// same "vendored, one file, no per-page JS" discipline as confirm.js and
// sidebar.js. Three jobs:
//
//   1. Copy buttons on every code block (clipboard API, with an
//      execCommand fallback for insecure contexts; announces the outcome
//      through #copy-status so screen readers hear it).
//   2. The sidebar page filter (moved out of docs.html so the template
//      carries no inline script).
//   3. The search palette, fed by the server-rendered
//      <script id="docs-search-index"> payload (every page plus its h2/h3
//      anchors, whose ids the server injects - this file never rewrites
//      anchors, so the palette and the "On this page" TOC cannot drift).
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.top = '-1000px';
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(area);
      if (ok) resolve(); else reject(new Error('copy rejected'));
    });
  }

  function setupCopyButtons() {
    var status = document.getElementById('copy-status');
    var blocks = document.querySelectorAll('.doc pre');
    Array.prototype.forEach.call(blocks, function (pre) {
      var parent = pre.parentNode;
      if (parent && parent.classList &&
          parent.classList.contains('codewrap')) {
        return;
      }
      var wrap = document.createElement('div');
      wrap.className = 'codewrap';
      parent.insertBefore(wrap, pre);
      wrap.appendChild(pre);

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn';
      btn.textContent = 'Copy';
      btn.setAttribute('aria-label', 'Copy code to clipboard');
      wrap.appendChild(btn);

      var timer = null;
      btn.addEventListener('click', function () {
        copyText(pre.textContent).then(function () {
          btn.classList.add('copied');
          btn.textContent = 'Copied';
          if (status) status.textContent = 'Code copied to clipboard.';
          clearTimeout(timer);
          timer = setTimeout(function () {
            btn.classList.remove('copied');
            btn.textContent = 'Copy';
          }, 1600);
        }, function () {
          if (status) {
            status.textContent = 'Copy failed. Select the code and copy it ' +
              'manually.';
          }
        });
      });
    });
  }

  function setupSidebarFilter() {
    var box = document.getElementById('docs-filter');
    var nav = document.getElementById('docs-nav');
    if (!box || !nav) return;
    box.addEventListener('input', function () {
      var q = box.value.trim().toLowerCase();
      nav.querySelectorAll('a').forEach(function (a) {
        var hit = !q || (a.getAttribute('data-title') || '').indexOf(q) !== -1;
        a.style.display = hit ? '' : 'none';
      });
      nav.querySelectorAll('h4').forEach(function (h) {
        var visible = false;
        var el = h.nextElementSibling;
        while (el && el.tagName !== 'H4') {
          if (el.tagName === 'A' && el.style.display !== 'none') {
            visible = true;
            break;
          }
          el = el.nextElementSibling;
        }
        h.style.display = visible || !q ? '' : 'none';
      });
    });
  }

  function setupPalette() {
    var overlay = document.getElementById('docs-palette');
    var openBtn = document.getElementById('palette-open');
    var input = document.getElementById('palette-input');
    var list = document.getElementById('palette-results');
    var empty = document.getElementById('palette-empty');
    var payload = document.getElementById('docs-search-index');
    if (!overlay || !input || !list || !payload) return;

    var index = [];
    try {
      index = JSON.parse(payload.textContent) || [];
    } catch (err) {
      index = [];
    }
    var entries = [];
    index.forEach(function (page) {
      var url = page.slug === 'introduction' ? '/docs' : '/docs/' + page.slug;
      entries.push({
        label: page.title,
        sub: page.desc || '',
        url: url,
        hay: (page.title + ' ' + (page.desc || '')).toLowerCase(),
      });
      (page.headings || []).forEach(function (heading) {
        entries.push({
          label: heading.title,
          sub: page.title,
          url: url + '#' + heading.anchor,
          hay: (heading.title + ' ' + page.title).toLowerCase(),
        });
      });
    });

    var results = [];
    var selected = 0;
    var opener = null;

    function search(query) {
      var q = query.trim().toLowerCase();
      if (!q) return entries.slice(0, 8);
      var scored = [];
      entries.forEach(function (entry, position) {
        var label = entry.label.toLowerCase();
        var score = 0;
        if (label.indexOf(q) === 0) score = 3;
        else if (label.indexOf(q) !== -1) score = 2;
        else if (entry.hay.indexOf(q) !== -1) score = 1;
        if (score) scored.push({ entry: entry, score: score, at: position });
      });
      scored.sort(function (a, b) {
        return b.score - a.score || a.at - b.at;
      });
      return scored.slice(0, 20).map(function (row) { return row.entry; });
    }

    function render() {
      list.innerHTML = '';
      var frag = document.createDocumentFragment();
      results.forEach(function (entry, i) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', i === selected ? 'true' : 'false');
        var link = document.createElement('a');
        link.href = entry.url;
        var title = document.createElement('strong');
        title.textContent = entry.label;
        var sub = document.createElement('small');
        sub.textContent = entry.sub;
        link.appendChild(title);
        link.appendChild(sub);
        li.appendChild(link);
        frag.appendChild(li);
      });
      list.appendChild(frag);
      if (empty) empty.hidden = results.length > 0;
      input.setAttribute('aria-expanded', results.length ? 'true' : 'false');
    }

    function refresh() {
      results = search(input.value);
      selected = 0;
      render();
    }

    function openPalette(trigger) {
      opener = trigger || null;
      overlay.hidden = false;
      if (openBtn) openBtn.setAttribute('aria-expanded', 'true');
      input.value = '';
      refresh();
      input.focus();
    }

    function closePalette() {
      overlay.hidden = true;
      if (openBtn) openBtn.setAttribute('aria-expanded', 'false');
      if (opener && opener.focus) opener.focus();
      opener = null;
    }

    function move(delta) {
      if (!results.length) return;
      selected = (selected + delta + results.length) % results.length;
      render();
      var active = list.children[selected];
      if (active && active.scrollIntoView) {
        active.scrollIntoView({ block: 'nearest' });
      }
    }

    if (openBtn) {
      openBtn.addEventListener('click', function () { openPalette(openBtn); });
    }
    input.addEventListener('input', refresh);
    input.addEventListener('keydown', function (evt) {
      if (evt.key === 'ArrowDown') {
        evt.preventDefault();
        move(1);
      } else if (evt.key === 'ArrowUp') {
        evt.preventDefault();
        move(-1);
      } else if (evt.key === 'Enter') {
        evt.preventDefault();
        if (results[selected]) location.href = results[selected].url;
      }
    });
    overlay.addEventListener('mousedown', function (evt) {
      if (evt.target === overlay) closePalette();
    });

    document.addEventListener('keydown', function (evt) {
      var isOpen = !overlay.hidden;
      if (evt.key === 'Escape' && isOpen) {
        evt.preventDefault();
        closePalette();
        return;
      }
      var chord = evt.metaKey || evt.ctrlKey;
      if (chord && (evt.key === 'k' || evt.key === 'K')) {
        evt.preventDefault();
        if (isOpen) closePalette(); else openPalette(openBtn);
        return;
      }
      if (isOpen) return;
      var target = evt.target;
      var typing = target && (target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' || target.isContentEditable);
      if (evt.key === '/' && !typing && !chord && !evt.altKey) {
        evt.preventDefault();
        openPalette(openBtn);
      }
    });
  }

  ready(function () {
    setupCopyButtons();
    setupSidebarFilter();
    setupPalette();
  });
})();
