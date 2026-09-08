/* ReverseBid - small, dependency-free front-end helpers. */
(function () {
  "use strict";

  // ---------------------------------------------------------------- countdown
  function pad(n) { return String(n).padStart(2, "0"); }

  function tickClocks() {
    document.querySelectorAll("[data-deadline]").forEach(function (el) {
      var end = parseInt(el.dataset.deadline, 10) * 1000;
      var left = Math.max(0, Math.floor((end - Date.now()) / 1000));
      var d = Math.floor(left / 86400), h = Math.floor((left % 86400) / 3600),
          m = Math.floor((left % 3600) / 60), s = left % 60;
      el.textContent = d > 0 ? d + "d " + pad(h) + ":" + pad(m) + ":" + pad(s)
                             : pad(h) + ":" + pad(m) + ":" + pad(s);
      el.classList.toggle("urgent", left > 0 && left < 300);
      if (left === 0 && !el.dataset.done) {
        el.dataset.done = "1";
        el.textContent = "Closed";
        setTimeout(function () { location.reload(); }, 1500);
      }
    });
  }
  setInterval(tickClocks, 1000); tickClocks();

  // ---------------------------------------------------------------- live board
  var board = document.getElementById("live-board");
  if (board && board.dataset.src) {
    setInterval(function () {
      if (document.hidden) return;
      if (document.activeElement && ["INPUT", "TEXTAREA"].indexOf(document.activeElement.tagName) > -1) return;
      fetch(board.dataset.src, { headers: { "X-Partial": "1" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) { if (html) { board.innerHTML = html; tickClocks(); } })
        .catch(function () {});
    }, 4000);
  }

  // ---------------------------------------------------------------- drawers
  function openDrawer(id) {
    document.getElementById(id).classList.add("open");
    document.getElementById("scrim").classList.add("on");
  }
  function closeDrawers() {
    document.querySelectorAll(".drawer").forEach(function (d) { d.classList.remove("open"); });
    document.getElementById("scrim").classList.remove("on");
  }
  window.openDrawer = openDrawer;
  window.closeDrawers = closeDrawers;
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeDrawers(); closeModal(); } });

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
      mine.className = "chat-msg you"; mine.textContent = q;
      log.appendChild(mine); input.value = "";
      log.scrollTop = log.scrollHeight;
      fetch("/assistant/ask", { method: "POST", body: new URLSearchParams({ question: q, context: askForm.dataset.context || "" }) })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var wrap = document.createElement("div");
          wrap.innerHTML = html;
          log.appendChild(wrap);
          log.scrollTop = log.scrollHeight;
        });
    });
  }
  window.askThis = function (text) {
    var input = document.querySelector("#ask-form input[name=question]");
    if (!input) return;
    input.value = text;
    document.getElementById("ask-form").dispatchEvent(new Event("submit"));
  };

  // ---------------------------------------------------------------- modals
  function openModal(id) { document.getElementById(id).classList.add("on"); }
  function closeModal() { document.querySelectorAll(".modal").forEach(function (m) { m.classList.remove("on"); }); }
  window.openModal = openModal;
  window.closeModal = closeModal;
  document.querySelectorAll(".modal").forEach(function (m) {
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(); });
  });

  // ------------------------------------------------- inline (Odoo-style) create
  document.querySelectorAll("form[data-quick]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var target = form.dataset.target;      // css selector of selects to extend
      fetch(form.action, { method: "POST", body: new FormData(form) })
        .then(function (r) { if (!r.ok) throw new Error("failed"); return r.json(); })
        .then(function (data) {
          document.querySelectorAll(target).forEach(function (sel) {
            if (sel.tagName === "SELECT") {
              var opt = new Option(data.label, data.id, false, false);
              sel.add(opt);
              if (sel.dataset.autoselect !== "0") sel.value = data.id;
            } else if (sel.tagName === "DIV") {
              var id = "v" + data.id;
              var label = document.createElement("label");
              label.className = "check";
              label.innerHTML = '<input type="checkbox" name="vendor_ids" value="' + data.id +
                '" checked><span><span class="t">' + data.label + "</span></span>";
              sel.prepend(label);
            }
          });
          form.reset();
          closeModal();
          toast("Saved and selected.");
        })
        .catch(function () { toast("Could not save that - check the fields.", true); });
    });
  });

  // ---------------------------------------------------------------- toast
  function toast(message, bad) {
    var el = document.createElement("div");
    el.className = "alert " + (bad ? "error" : "ok");
    el.style.cssText = "position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:90;box-shadow:0 10px 30px rgba(15,23,42,.18)";
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  }
  window.toast = toast;

  // ---------------------------------------------------------------- line rows
  window.addLineRow = function () {
    var body = document.getElementById("line-rows");
    var template = document.getElementById("line-template");
    var node = template.content.cloneNode(true);
    body.appendChild(node);
    renumberLines();
  };
  window.removeLineRow = function (btn) {
    var rows = document.querySelectorAll("#line-rows .line-item");
    if (rows.length <= 1) { toast("An auction needs at least one item.", true); return; }
    btn.closest(".line-item").remove();
    renumberLines();
  };
  function renumberLines() {
    document.querySelectorAll("#line-rows .line-item").forEach(function (row, i) {
      var badge = row.querySelector(".li-num");
      if (badge) badge.textContent = i + 1;
    });
  }
  window.renumberLines = renumberLines;

  // ------------------------------------------------- award: add a split row
  window.addSplitRow = function (lineId) {
    var body = document.getElementById("split-" + lineId);
    var last = body.querySelector(".split-row");
    var clone = last.cloneNode(true);
    clone.querySelectorAll("input[type=number]").forEach(function (i) { i.value = ""; });
    body.appendChild(clone);
  };

  // ---------------------------------------------- confirm on dangerous actions
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // ------------------------------------------------- quick-fill the bid input
  document.querySelectorAll("[data-fill]").forEach(function (el) {
    el.addEventListener("click", function () {
      var input = document.querySelector(el.dataset.fillTarget);
      if (input) { input.value = el.dataset.fill; input.focus(); }
    });
  });
})();
