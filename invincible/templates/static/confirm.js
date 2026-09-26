// invincible/templates/static/confirm.js
// Claude-style destructive-action confirmations + toasts for the dashboard.
//
// Any button carrying data-confirm-title is intercepted at the
// htmx:confirm event: instead of the native hx-confirm() dialog we show
// a centered modal (action label from data-confirm-ok, "Deny" cancels).
// On success the page updates in place and a bottom-right toast confirms
// what happened - no reload needed.
//
// Why the manual DOM updates: the vendored htmx never swaps 204
// responses (responseHandling 204 -> swap:false), so hx-swap="delete"
// alone silently no-ops and the row only disappears after a reload.
// applyDeleteSuccess() below is what makes the change visible.
//
// Covered surfaces (all share this file, no per-page JS):
//   /account API keys Revoke, /dashboard/mcp token Revoke (Option A: the
//   row stays, the token count flips to 0), /dashboard/providers Remove,
//   /dashboard/memory Delete.
(function () {
  'use strict';

  var modal, titleEl, bodyEl, okBtn, denyBtn, stack;
  var pendingOk = null;
  var lastTrigger = null;

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function toast(message, kind) {
    if (!stack || !message) return;
    var el = document.createElement('div');
    el.className = 'toast' + (kind === 'error' ? ' toast-error' : '');
    el.textContent = message;
    el.setAttribute('role', 'status');
    stack.appendChild(el);
    // Keep the stack short; toasts auto-dismiss and dismiss on click.
    while (stack.children.length > 3) {
      stack.removeChild(stack.firstChild);
    }
    var done = false;
    function dismiss() {
      if (done) return;
      done = true;
      if (el.isConnected) el.remove();
    }
    el.addEventListener('click', dismiss);
    setTimeout(dismiss, 4200);
  }

  function openModal(opts) {
    titleEl.textContent = opts.title;
    bodyEl.textContent = opts.body;
    okBtn.textContent = opts.ok;
    lastTrigger = opts.trigger || null;
    pendingOk = opts.onOk || null;
    modal.hidden = false;
    okBtn.focus();
  }

  function closeModal(confirmed) {
    modal.hidden = true;
    var issue = pendingOk;
    pendingOk = null;
    if (confirmed && issue) {
      issue();
    } else if (lastTrigger && lastTrigger.focus) {
      // Deny/Esc/backdrop: hand focus back where it was.
      lastTrigger.focus();
    }
    lastTrigger = null;
  }

  function tableColspan(tbody) {
    var table = tbody.closest('table');
    var ths = table ? table.querySelectorAll('thead th').length : 0;
    return ths || 6;
  }

  function emptyMessageFor(tbody) {
    // Search results get their own empty text (same wording as the
    // server-rendered search empty state).
    if (tbody.id === 'memtable-body') {
      var q = document.querySelector('.memory-filters input[name="q"]');
      if (q && q.value.trim()) return 'No memories matching your search.';
    }
    return tbody.getAttribute('data-empty-message') || 'Nothing here yet.';
  }

  function ensureEmptyRow(tbody) {
    if (tbody.querySelector('tr')) return;
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = tableColspan(tbody);
    td.className = 'empty';
    td.textContent = emptyMessageFor(tbody);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function decrementMemoryTotals() {
    var next = null;
    document.querySelectorAll('[data-memory-total]').forEach(function (el) {
      var n = parseInt(el.textContent, 10);
      if (isNaN(n)) return;
      next = Math.max(0, n - 1);
      el.textContent = String(next);
    });
    var plural = document.querySelector('[data-memory-plural]');
    if (plural && next !== null) {
      plural.textContent = next === 1 ? 'y' : 'ies';
    }
  }

  function afterProviderDelete(tbody) {
    // Last provider gone: the routing form references providers that no
    // longer exist, so hide it until the next connect (a fresh GET
    // renders the empty state + routing correctly again).
    if (!tbody.querySelector('tr.provider-row')) {
      var routing = document.getElementById('routing-block');
      if (routing) routing.style.display = 'none';
    }
  }

  function applyDeleteSuccess(btn) {
    var mode = btn.getAttribute('data-delete-mode') || 'row';
    if (mode === 'mcp-tokens') {
      // Option A: the client stays registered - only its tokens are
      // gone, so the row stays and the count flips to 0.
      var mcpRow = btn.closest('tr');
      var cell = mcpRow ? mcpRow.querySelector('[data-token-count]') : null;
      if (cell) cell.textContent = '0';
      btn.disabled = true;
      btn.textContent = 'Revoked';
      btn.removeAttribute('hx-delete');
      btn.removeAttribute('data-confirm-title');
    } else {
      var liveRow = btn.closest('tr');
      var tbody = btn.closest('tbody');
      if (liveRow && liveRow.isConnected) liveRow.remove();
      if (tbody && tbody.isConnected) {
        ensureEmptyRow(tbody);
        if (tbody.id === 'provider-rows') afterProviderDelete(tbody);
        if (tbody.id === 'memtable-body') decrementMemoryTotals();
      }
    }
    toast(btn.getAttribute('data-toast') || 'Done.');
  }

  function fetchDelete(elt) {
    // No-htmx fallback (vendored htmx failed to load): the hx-delete
    // buttons would otherwise do nothing. DELETE by fetch, then run the
    // same in-place update.
    var url = elt.getAttribute('hx-delete');
    if (!url) return;
    fetch(url, {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'HX-Request': 'true' },
    }).then(function (r) {
      if (r.ok) {
        applyDeleteSuccess(elt);
      } else {
        toast('Something went wrong. Please try again.', 'error');
      }
    }).catch(function () {
      toast('Something went wrong. Please try again.', 'error');
    });
  }

  function confirmTarget(evt) {
    var detail = (evt && evt.detail) || {};
    var elt = detail.elt || evt.target;
    if (elt && elt.closest &&
        !(elt.getAttribute && elt.getAttribute('data-confirm-title'))) {
      elt = elt.closest('[data-confirm-title]');
    }
    return { detail: detail, elt: elt };
  }

  function bind() {
    modal = document.getElementById('confirm-modal');
    stack = document.getElementById('toast-stack');
    if (!modal || !stack) return;
    titleEl = document.getElementById('confirm-title');
    bodyEl = document.getElementById('confirm-body');
    okBtn = document.getElementById('confirm-ok');
    denyBtn = document.getElementById('confirm-deny');
    if (!titleEl || !bodyEl || !okBtn || !denyBtn) return;

    okBtn.addEventListener('click', function () { closeModal(true); });
    denyBtn.addEventListener('click', function () { closeModal(false); });
    var backdrop = modal.querySelector('[data-confirm-close]');
    if (backdrop) {
      backdrop.addEventListener('click', function () { closeModal(false); });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modal.hidden) closeModal(false);
    });

    document.body.addEventListener('htmx:confirm', function (evt) {
      var found = confirmTarget(evt);
      var elt = found.elt;
      if (!elt || !elt.getAttribute ||
          !elt.getAttribute('data-confirm-title')) {
        return; // not ours: let htmx proceed normally.
      }
      evt.preventDefault();
      (function (detail, trigger) {
        openModal({
          title: trigger.getAttribute('data-confirm-title'),
          body: trigger.getAttribute('data-confirm-body') || 'Are you sure?',
          ok: trigger.getAttribute('data-confirm-ok') || 'Delete',
          trigger: trigger,
          // issueRequest(true) skips htmx's built-in confirm check and
          // fires the request the button declared.
          onOk: function () { detail.issueRequest(true); },
        });
      })(found.detail, elt);
    });

    document.body.addEventListener('htmx:afterRequest', function (evt) {
      var found = confirmTarget(evt);
      var elt = found.elt;
      if (!elt || !elt.getAttribute ||
          !elt.getAttribute('data-confirm-title')) {
        return;
      }
      if (found.detail && found.detail.successful) {
        applyDeleteSuccess(elt);
      } else {
        toast('Something went wrong. Please try again.', 'error');
      }
    });

    if (!window.htmx) {
      document.body.addEventListener('click', function (evt) {
        var elt = evt.target && evt.target.closest ?
          evt.target.closest('[data-confirm-title]') : null;
        if (!elt || elt.disabled) return;
        evt.preventDefault();
        openModal({
          title: elt.getAttribute('data-confirm-title'),
          body: elt.getAttribute('data-confirm-body') || 'Are you sure?',
          ok: elt.getAttribute('data-confirm-ok') || 'Delete',
          trigger: elt,
          onOk: function () { fetchDelete(elt); },
        });
      });
    }
  }

  ready(bind);
})();
