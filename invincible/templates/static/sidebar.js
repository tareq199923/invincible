// invincible/templates/static/sidebar.js
// Per-chat sidebar actions: the ⋮ menu on each row (Rename / Pin / Delete).
//
// Claude-style: the button is revealed on hover/focus (always visible in
// the mobile drawer, where there is no hover), the menu closes on
// click-outside/Escape/selection, and Rename edits the label in place
// (Enter saves, Esc reverts, blank restores the derived first-message
// title).
//
// Delete is deliberately NOT handled here: the Delete item carries the
// data-confirm-* / data-delete-url / data-delete-mode attributes that
// static/confirm.js already understands, so it reuses the app-wide modal,
// the in-place row removal and the toast stack. This file only remembers
// which row was asked for so an 'inv:deleted' event can bounce the browser
// off a conversation that no longer exists.
//
// Wire protocol (endpoints/chat.py):
//   PATCH  /dashboard/chat/sessions/{pk}   {"title": str|null, "pinned": bool}
//   DELETE /dashboard/chat/sessions/{pk}   (confirm.js, fetch + 204)
// Rows are addressed by the surrogate pk in data-session-pk - never by the
// client session string, which users never see.
(function () {
  'use strict';

  var list = document.getElementById('side-history');
  if (!list) return;

  var pendingDelete = null;

  function toast(message, kind) {
    // confirm.js owns the single toast stack; it is optional here so the
    // menu still works if that file failed to load.
    if (typeof window.invToast === 'function') window.invToast(message, kind);
  }

  function request(url, method, body) {
    return fetch(url, {
      method: method,
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.ok ? resp.json().catch(function () { return {}; })
                     : Promise.reject(resp);
    });
  }

  function failure() {
    toast('Something went wrong. Please try again.', 'error');
  }

  function refilter() {
    // base.html's search filter is exposed for exactly this: a renamed or
    // moved row must be re-evaluated without waiting for a keystroke.
    if (typeof window.invSidebarFilter === 'function') {
      window.invSidebarFilter();
    }
  }

  function activeSessionId() {
    try {
      return new URLSearchParams(window.location.search).get('session');
    } catch (e) {
      return null;
    }
  }

  // ---------- menu open / close ----------

  function closeMenus(except) {
    list.querySelectorAll('li.chat-row.menu-open').forEach(function (row) {
      if (row === except) return;
      row.classList.remove('menu-open');
      var menu = row.querySelector('.chat-menu');
      var btn = row.querySelector('.chat-menu-btn');
      if (menu) menu.hidden = true;
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  }

  function openMenu(row) {
    closeMenus(row);
    row.classList.add('menu-open');
    var menu = row.querySelector('.chat-menu');
    var btn = row.querySelector('.chat-menu-btn');
    if (menu) menu.hidden = false;
    if (btn) btn.setAttribute('aria-expanded', 'true');
  }

  function deleteUrl(row) {
    var btn = row.querySelector('[data-delete-url]');
    return btn ? btn.getAttribute('data-delete-url') : null;
  }

  function sessionUrl(row) {
    // Same route as the delete button, but declared on the row so PATCH
    // and DELETE need no attribute parsing of each other's markup.
    return row.getAttribute('data-session-url') || deleteUrl(row);
  }

  function derivedTitle(row) {
    // The template computed the derived label next to the custom one, so
    // clearing a name needs no server round trip to restore it.
    return row.getAttribute('data-derived-title') ||
           row.getAttribute('data-title') || '';
  }

  function applyTitle(row, text) {
    var span = row.querySelector('.sess-title');
    if (span) span.textContent = text;
    var link = row.querySelector('a.sess');
    if (link) link.title = text;
    row.setAttribute('data-title', text);
    refilter();
  }

  // ---------- rename ----------

  function startRename(row) {
    closeMenus();
    var link = row.querySelector('a.sess');
    var title = link ? link.querySelector('.sess-title') : null;
    if (!link || !title || row.querySelector('.sess-rename')) return;
    var current = row.getAttribute('data-title') || title.textContent || '';
    var field = document.createElement('input');
    field.type = 'text';
    field.className = 'sess-rename';
    field.maxLength = 100;
    field.value = current;
    field.setAttribute('aria-label', 'Chat name');
    title.replaceWith(field);
    field.focus();
    field.select();

    var settled = false;
    function restore(text) {
      if (settled) return;
      settled = true;
      var span = document.createElement('span');
      span.className = 'sess-title';
      span.textContent = text;
      field.replaceWith(span);
    }
    function save() {
      if (settled) return;
      var next = field.value.trim();
      settled = true;
      // Optimistic: label first, request after; a failure restores it.
      var span = document.createElement('span');
      span.className = 'sess-title';
      span.textContent = next || derivedTitle(row);
      field.replaceWith(span);
      request(sessionUrl(row), 'PATCH', {title: next})
        .then(function (body) {
          applyTitle(row, body.title || derivedTitle(row));
          toast(next ? 'Chat renamed.' : 'Chat name reset.');
        })
        .catch(function () {
          applyTitle(row, current);
          failure();
        });
    }
    field.addEventListener('keydown', function (evt) {
      if (evt.key === 'Enter') { evt.preventDefault(); save(); }
      else if (evt.key === 'Escape') { evt.preventDefault(); restore(current); }
    });
    field.addEventListener('blur', save);
  }

  // ---------- pin ----------

  function moveRow(row, pinned) {
    var others = Array.prototype.slice.call(
      list.querySelectorAll('li.chat-row'));
    var pinnedBlock = others.filter(function (other) {
      return other !== row && other.getAttribute('data-pinned') === '1';
    });
    if (pinned || !pinnedBlock.length) {
      // Just pinned (top of the pinned block) - or unpinned with no pinned
      // rows left, where "just touched" means the top of the list.
      var first = list.querySelector('li.chat-row');
      if (first) list.insertBefore(row, first);
      else list.appendChild(row);
      return;
    }
    list.insertBefore(row, pinnedBlock[pinnedBlock.length - 1].nextSibling);
  }

  function applyPin(row, pinned) {
    row.setAttribute('data-pinned', pinned ? '1' : '0');
    row.classList.toggle('pinned', pinned);
    var link = row.querySelector('a.sess');
    var pin = link ? link.querySelector('.sess-pin') : null;
    if (pinned && link && !pin) {
      pin = document.createElement('span');
      pin.className = 'sess-pin';
      pin.title = 'Pinned';
      pin.setAttribute('aria-label', 'Pinned');
      pin.textContent = '\uD83D\uDCCC';
      link.appendChild(pin);
    } else if (!pinned && pin) {
      pin.remove();
    }
    var item = row.querySelector('[data-chat-action="pin"]');
    if (item) item.textContent = pinned ? 'Unpin' : 'Pin';
    moveRow(row, pinned);
    refilter();
  }

  function togglePin(row) {
    closeMenus();
    var next = row.getAttribute('data-pinned') !== '1';
    request(sessionUrl(row), 'PATCH', {pinned: next})
      .then(function (body) {
        applyPin(row, body.pinned === undefined ? next : !!body.pinned);
        toast(next ? 'Chat pinned.' : 'Chat unpinned.');
      })
      .catch(failure);
  }

  // ---------- wiring ----------

  list.addEventListener('click', function (evt) {
    var btn = evt.target.closest ? evt.target.closest('.chat-menu-btn') : null;
    if (btn) {
      evt.preventDefault();
      var row = btn.closest('li.chat-row');
      if (!row) return;
      var wasOpen = row.classList.contains('menu-open');
      closeMenus();
      if (!wasOpen) openMenu(row);
      return;
    }
    var action = evt.target.closest
      ? evt.target.closest('[data-chat-action]') : null;
    if (!action) return;
    var owner = action.closest('li.chat-row');
    if (!owner) return;
    var kind = action.getAttribute('data-chat-action');
    if (kind === 'rename') {
      evt.preventDefault();
      startRename(owner);
    } else if (kind === 'pin') {
      evt.preventDefault();
      togglePin(owner);
    } else if (kind === 'delete') {
      // confirm.js owns the modal + the row removal; remember which chat
      // was asked for so 'inv:deleted' can act if it was the open one.
      pendingDelete = owner.getAttribute('data-session-id');
    }
  });

  document.addEventListener('click', function (evt) {
    if (!evt.target.closest || !evt.target.closest('li.chat-row')) {
      closeMenus();
    }
  });

  document.addEventListener('keydown', function (evt) {
    if (evt.key === 'Escape') closeMenus();
  });

  document.addEventListener('inv:deleted', function () {
    // The open conversation just disappeared: start a fresh chat rather
    // than leave the pane pointing at a session that no longer exists.
    if (pendingDelete && pendingDelete === activeSessionId()) {
      pendingDelete = null;
      window.location.assign('/dashboard/chat');
      return;
    }
    pendingDelete = null;
  });
})();
