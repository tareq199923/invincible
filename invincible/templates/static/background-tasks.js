// Background-task dashboard controls: the start form on the Workflows
// page and the delegated Stop button on the task detail page. Kept in a
// static file (no inline script) so a future CSP only needs 'self'.
(function () {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function setError(message) {
    var box = byId("background-task-error");
    if (!box) return;
    box.textContent = message || "";
    box.hidden = !message;
  }

  function refreshTaskFragment() {
    var live = byId("task-live");
    if (live && window.htmx && typeof window.htmx.trigger === "function") {
      window.htmx.trigger(live, "refresh");
      return;
    }
    window.location.reload();
  }

  // Tokens this page has already settled. The server may still render the
  // card for one more poll until the runner claims the row; these keep its
  // buttons disabled across HTMX swaps.
  var resolvedTokens = {};

  function applyResolvedState() {
    var buttons = document.querySelectorAll("[data-approval-token]");
    for (var i = 0; i < buttons.length; i += 1) {
      var button = buttons[i];
      var token = button.getAttribute("data-approval-token");
      if (!token || !resolvedTokens[token]) continue;
      var card = button.closest(".approval-card");
      var cardButtons = card
        ? card.querySelectorAll("[data-approval-token]")
        : [button];
      for (var j = 0; j < cardButtons.length; j += 1) {
        cardButtons[j].disabled = true;
      }
      var status = card
        ? card.querySelector(".approval-decision-status")
        : null;
      if (status) {
        if (!status.textContent) {
          status.textContent = "Recorded \u2014 refreshing\u2026";
        }
        status.hidden = false;
      }
    }
  }

  document.addEventListener("htmx:afterSwap", applyResolvedState);

  document.addEventListener("DOMContentLoaded", function () {
    var form = byId("background-task-form");
    var input = byId("background-task-input");
    var counter = byId("background-task-counter");
    var button = byId("background-task-submit");
    var mode = byId("background-task-mode");
    applyResolvedState();
    if (!form || !input || !counter || !button) return;

    var max = parseInt(input.getAttribute("maxlength"), 10) || 8000;
    function updateCounter() {
      counter.textContent = input.value.length + " / " + max;
    }
    input.addEventListener("input", updateCounter);
    updateCounter();

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var task = input.value.trim();
      if (!task) {
        setError("Enter a task first.");
        input.focus();
        return;
      }
      if (task.length > max) {
        setError("Task must be at most " + max + " characters.");
        return;
      }
      setError("");
      button.disabled = true;
      button.textContent = "Starting...";

      fetch(form.getAttribute("data-endpoint"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({
          task: task,
          mode: mode ? mode.value : "plan"
        })
      }).then(function (response) {
        return response.json().catch(function () {
          return {};
        }).then(function (data) {
          if (response.status === 202 && data.workflow_id) {
            window.location.assign(
              "/dashboard/workflows/" + encodeURIComponent(data.workflow_id));
            return;
          }
          var message = data && data.error && data.error.message;
          setError(message || "Could not start the task. Try again.");
          button.disabled = false;
          button.textContent = "Start task";
        });
      }).catch(function () {
        setError("Network error. Try again.");
        button.disabled = false;
        button.textContent = "Start task";
      });
    });
  });

  // Delegated: the Stop button lives inside the HTMX-swapped fragment,
  // so a listener bound directly to it would be lost on every refresh.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-cancel-workflow]");
    if (!button || button.disabled) return;
    event.preventDefault();
    var workflowId = button.getAttribute("data-cancel-workflow");
    if (!workflowId) return;

    button.disabled = true;
    button.textContent = "Stopping...";
    fetch(
      "/dashboard/background-tasks/" + encodeURIComponent(workflowId) +
        "/cancel",
      {
        method: "POST",
        credentials: "same-origin",
        headers: {"Accept": "application/json"}
      }
    ).then(function (response) {
      // 202: cancellation is now flowing through the runner; 404: the
      // task already finished (or is not ours). Both cases refresh the
      // fragment so the badge reflects what actually happened.
      if (response.ok || response.status === 404) {
        refreshTaskFragment();
        return;
      }
      button.disabled = false;
      button.textContent = "Stop task";
    }).catch(function () {
      button.disabled = false;
      button.textContent = "Stop task";
    });
  });
  // Delegated: approval cards live inside the HTMX-swapped fragment, so
  // the listener is on the document and works after every refresh.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-approval-decision]");
    if (!button || button.disabled) return;
    event.preventDefault();
    var token = button.getAttribute("data-approval-token");
    var workflowId = button.getAttribute("data-approval-workflow");
    var decision = button.getAttribute("data-approval-decision");
    if (!token || !workflowId || !decision) return;

    var card = button.closest(".approval-card");
    var buttons = card
      ? card.querySelectorAll("[data-approval-token]")
      : [button];
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].disabled = true;
    }
    var status = card
      ? card.querySelector(".approval-decision-status")
      : null;
    if (status) {
      status.textContent = decision === "approve"
        ? "Approved, running\u2026"
        : "Denied, stopping\u2026";
      status.hidden = false;
    }

    fetch(
      "/dashboard/background-tasks/" + encodeURIComponent(workflowId) +
        "/approvals/" + encodeURIComponent(token),
      {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({approve: decision === "approve"})
      }
    ).then(function (response) {
      // 202 settled the waiter; 404 means it was already settled,
      // expired, or not ours. Both refresh the fragment. Anything else
      // is a real failure the user can retry.
      if (response.status === 202 || response.status === 404) {
        resolvedTokens[token] = true;
        applyResolvedState();
        refreshTaskFragment();
        return;
      }
      if (!resolvedTokens[token]) {
        for (var j = 0; j < buttons.length; j += 1) {
          buttons[j].disabled = false;
        }
        if (status) {
          status.textContent = "Could not record that decision. Try again.";
          status.hidden = false;
        }
      }
    }).catch(function () {
      if (resolvedTokens[token]) return;
      for (var j = 0; j < buttons.length; j += 1) {
        buttons[j].disabled = false;
      }
      if (status) {
        status.textContent = "Network error. Try again.";
        status.hidden = false;
      }
    });
  });
})();
