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

  document.addEventListener("DOMContentLoaded", function () {
    var form = byId("background-task-form");
    var input = byId("background-task-input");
    var counter = byId("background-task-counter");
    var button = byId("background-task-submit");
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
        body: JSON.stringify({task: task})
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
})();
