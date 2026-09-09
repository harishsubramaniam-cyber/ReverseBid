/* ReverseBid — small, dependency-free front-end helpers.
 *
 * One rule runs through this file: the live board is replaced wholesale every
 * few seconds by the poller, so nothing inside it may rely on a listener bound
 * at page load. Every handler here is delegated from `document`.
 */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  // ---------------------------------------------------------------- countdown
  function pad(n) { return String(n).padStart(2, "0"); }

  function tickClocks() {
    $$("[data-deadline]").forEach(function (el) {
      var end = parseInt(el.dataset.deadline, 10) * 1000;
      if (!end) return;
      var left = Math.max(0, Math.floor((end - Date.now()) / 1000));
      var d = Math.floor(left / 86400), h = Math.floor((left % 86400) / 3600),
          m = Math.floor((left % 3600) / 60), s = left % 60;
      el.textContent = d > 0 ? d + "d " + pad(h) + ":" + pad(m) + ":" + pad(s)
                             : pad(h) + ":" + pad(m) + ":" + pad(s);
      el.classList.toggle("urgent", left > 0 && left < 300);
      if (left === 0) {
        el.textContent = "Closing…";
        // Reload once per deadline. Without the guard an auction that is still
        // LIVE until the next scheduler tick would reload in a loop.
        var key = "ra-closed-" + location.pathname + "-" + el.dataset.deadline;
        try {
          if (!sessionStorage.getItem(key)) {
            sessionStorage.setItem(key, "1");
            setTimeout(function () { location.reload(); }, 2500);
          }
        } catch (e) { /* private window: just leave the clock at Closing… */ }
      }
    });
  }
  setInterval(tickClocks, 1000);
  tickClocks();

  // ---------------------------------------------------------------- live board
  var board = document.getElementById("live-board");
  if (board && board.dataset.src) {
    setInterval(function () {
      if (document.hidden) return;
      fetch(board.dataset.src, { headers: { "X-Partial": "1" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) { if (html) swapBoard(html); })
        .catch(function () { /* offline for a moment: try again next tick */ });
    }, 4000);
  }

  function swapBoard(html) {
    // Keep what the person is doing: the focused field, the caret, and every
    // price they have typed but not yet submitted.
    var active = document.activeElement;
    var focusedName = (active && board.contains(active)) ? active.id : null;
    var caret = focusedName && active.selectionStart;
    var typed = {};
    $$("input, textarea", board).forEach(function (el) {
      if (el.id && el.value) typed[el.id] = el.value;
    });

    board.innerHTML = html;

    $$("input, textarea", board).forEach(function (el) {
      if (el.id && typed[el.id] !== undefined) {
        el.value = typed[el.id];
        // Tell the page the value is back, so the "that is X for all Y" hint
        // under the price is redrawn instead of vanishing on every refresh.
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    if (focusedName) {
      var again = document.getElementById(focusedName);
      if (again) {
        again.focus();
        try { again.setSelectionRange(caret, caret); } catch (e) { /* not a text input */ }
      }
    }

    // The header clock and the status pill live outside the board, so the
    // fragment carries the current values for them.
    var state = document.getElementById("board-state");
    var header = $(".countdown [data-deadline]");
    if (state && header && state.dataset.deadline &&
        header.dataset.deadline !== state.dataset.deadline) {
      header.dataset.deadline = state.dataset.deadline;   // auto-extension
      delete header.dataset.done;
    }
    if (state && state.dataset.status && state.dataset.status !== "live") {
      location.reload();                                   // it has closed
    }
    tickClocks();
  }

  // ---------------------------------------------------------------- drawers
  window.openDrawer = function (id) {
    var drawer = document.getElementById(id);
    if (drawer) drawer.classList.add("open");
    var scrim = document.getElementById("scrim");
    if (scrim) scrim.classList.add("on");
  };
  window.closeDrawers = function () {
    $$(".drawer").forEach(function (d) { d.classList.remove("open"); });
    var scrim = document.getElementById("scrim");
    if (scrim) scrim.classList.remove("on");
  };
  window.openModal = function (id) {
    var m = document.getElementById(id);
    if (m) m.classList.add("on");
  };
  window.closeModal = function () {
    $$(".modal").forEach(function (m) { m.classList.remove("on"); });
  };
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { window.closeDrawers(); window.closeModal(); }
  });
  document.addEventListener("click", function (e) {
    if (e.target.classList && e.target.classList.contains("modal")) window.closeModal();
  });

  // ---------------------------------------------------------------- assistant
  var askForm = document.getElementById("ask-form");
  if (askForm) {
    askForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var input = askForm.querySelector("input[name=question]");
      var log = document.getElementById("ask-log");
      var q = input.value.trim();
      if (!q) return;
      var mine = document.createElement("div");
      mine.className = "chat-msg you";
      mine.textContent = q;
      log.appendChild(mine);
      input.value = "";
      log.scrollTop = log.scrollHeight;
      fetch("/assistant/ask", {
        method: "POST",
        headers: { "X-CSRF-Token": askForm.dataset.csrf || "" },
        body: new URLSearchParams({
          question: q,
          context: askForm.dataset.context || "",
          csrf_token: askForm.dataset.csrf || "",
        }),
      })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var wrap = document.createElement("div");
          wrap.innerHTML = html;
          log.appendChild(wrap);
          log.scrollTop = log.scrollHeight;
        })
        .catch(function () { toast("The assistant is not answering right now.", true); });
    });
  }
  window.askThis = function (text) {
    var input = $("#ask-form input[name=question]");
    if (!input) return;
    input.value = text;
    askForm.dispatchEvent(new Event("submit"));
  };

  // ------------------------------------------- inline (Odoo-style) create
  // Whatever is created here must end up ON the auction, not merely in the
  // master list: a buyer who creates three items expects three rows.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-quick]")) return;
    e.preventDefault();
    fetch(form.action, { method: "POST", body: new FormData(form) })
      .then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error(data.error || "That could not be saved.");
          return data;
        });
      })
      .then(function (data) {
        var mode = form.dataset.mode;                  // item | unit | vendor
        if (mode === "vendor") return addVendor(data);
        if (mode === "unit") return addUnit(data);
        return addItem(data);
      })
      .catch(function (err) { toast(err.message || "Could not save that.", true); });
  });

  // Anything created during this visit has to be replayed into rows added
  // later: those rows are cloned from a <template> rendered when the page
  // loaded, so they know nothing about it.
  var createdItems = [], createdUnits = [];

  function optionsFor(select, made) {
    var have = {};
    Array.prototype.forEach.call(select.options, function (o) { have[o.value] = true; });
    made.forEach(function (m) {
      if (!have[m.id]) select.add(new Option(m.label, m.id));
    });
  }
  window.replayCreated = function (row) {
    $$(".sel-item", row).forEach(function (sel) { optionsFor(sel, createdItems); });
    $$(".sel-unit", row).forEach(function (sel) { optionsFor(sel, createdUnits); });
  };

  function addItem(data) {
    createdItems.push({ id: String(data.id), label: data.label });
    $$(".sel-item").forEach(function (sel) { optionsFor(sel, createdItems); });

    var target = $$(".sel-item").filter(function (sel) { return !sel.value; })[0];
    if (!target) {
      // Every row is already spoken for, so give the new item a row of its own.
      window.addLineRow();
      var selects = $$(".sel-item");
      target = selects[selects.length - 1];
    }
    target.value = String(data.id);
    if (data.unit_id) {
      var unit = target.closest(".item-row").querySelector(".sel-unit");
      if (unit && !unit.value) unit.value = String(data.unit_id);
    }
    window.closeModal();
    var row = target.closest(".item-row");
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    var qty = row.querySelector("[name=line_qty]");
    if (qty) qty.focus();
    toast("“" + data.label + "” added to this auction. Now set the quantity.");
  }

  function addUnit(data) {
    createdUnits.push({ id: String(data.id), label: data.label });
    $$(".sel-unit").forEach(function (sel) { optionsFor(sel, createdUnits); });
    var target = $$(".sel-unit").filter(function (sel) { return !sel.value; })[0];
    if (target) target.value = String(data.id);
    window.closeModal();
    toast(target ? "Unit “" + data.label + "” created and selected."
                 : "Unit “" + data.label + "” created — pick it on any row.");
  }

  function addVendor(data) {
    var list = document.getElementById("vendor-list");
    if (!list) return;
    var row = document.createElement("div");
    row.className = "vendor-row";
    row.innerHTML =
      '<label class="check" style="margin-bottom:0">' +
      '<input type="checkbox" name="vendor_ids" value="' + data.id + '" checked>' +
      '<span><span class="t"></span><span class="d"></span></span></label>' +
      '<div class="vendor-override"><input type="text" name="notify_emails_' + data.id +
      '" placeholder="Send this auction to a different address (optional)"></div>';
    row.querySelector(".t").textContent = data.label;
    row.querySelector(".d").textContent = "Emails go to " + (data.emails || "");
    list.prepend(row);
    window.closeModal();
    toast("“" + data.label + "” added and invited.");
  }

  // ---------------------------------------------------------------- toast
  function toast(message, bad) {
    var el = document.createElement("div");
    el.className = "alert " + (bad ? "error" : "ok");
    el.style.cssText = "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);" +
      "z-index:90;box-shadow:0 12px 34px rgba(14,26,28,.22);max-width:90vw";
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3400);
  }
  window.toast = toast;

  // ---------------------------------------------------------------- line rows
  window.addLineRow = function () {
    var body = document.getElementById("line-rows");
    var template = document.getElementById("line-template");
    if (!body || !template) return;
    body.appendChild(template.content.cloneNode(true));
    var rows = $$("#line-rows .item-row");
    if (window.replayCreated) window.replayCreated(rows[rows.length - 1]);
    renumberLines();
  };
  window.removeLineRow = function (btn) {
    var rows = $$("#line-rows .item-row");
    if (rows.length <= 1) {
      toast("An auction needs at least one item.", true);
      return;
    }
    btn.closest(".item-row").remove();
    renumberLines();
  };
  function renumberLines() {
    $$("#line-rows .item-row").forEach(function (row, i) {
      var badge = row.querySelector(".li-num");
      var label = row.querySelector(".top b");
      if (badge) badge.textContent = i + 1;
      if (label) label.textContent = "Item " + (i + 1);
    });
  }
  window.renumberLines = renumberLines;

  // ---------------------------------------------- delegated click handlers
  document.addEventListener("click", function (e) {
    var fill = e.target.closest ? e.target.closest("[data-fill]") : null;
    if (fill) {
      var input = $(fill.dataset.fillTarget);
      if (input) {
        input.value = fill.dataset.fill;
        input.focus();
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  });

  // ---------------------------------------------- confirm before the big ones
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.matches && form.matches("form[data-confirm]")) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    }
  }, true);

  // ---------------------------------------------- live line total as you type
  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!input.dataset || !input.dataset.total) return;
    var out = document.getElementById(input.dataset.total);
    if (!out) return;
    var price = parseFloat(input.value), qty = parseFloat(input.dataset.qty || "0");
    out.textContent = (isFinite(price) && price > 0 && qty > 0)
      ? "That is " + (price * qty).toLocaleString(undefined, { maximumFractionDigits: 2 }) +
        " for all " + qty.toLocaleString() + "."
      : "";
  });
})();
