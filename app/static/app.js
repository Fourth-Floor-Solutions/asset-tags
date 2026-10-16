// Phones: the Menu button opens and closes the top navigation.
function initMenu() {
  var btn = document.querySelector(".menu-btn");
  if (!btn) return;
  var top = btn.closest(".top");
  btn.addEventListener("click", function () {
    var open = top.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
}
document.addEventListener("DOMContentLoaded", initMenu);

// Squeeze label text that's too long for its tape (client names).
function fitLabels(root) {
  (root || document).querySelectorAll("svg text[data-maxw]").forEach(function (t) {
    t.removeAttribute("textLength");
    t.removeAttribute("lengthAdjust");
    var max = parseFloat(t.getAttribute("data-maxw"));
    var w = t.getComputedTextLength();
    if (w > max) {
      t.setAttribute("textLength", max);
      t.setAttribute("lengthAdjust", "spacingAndGlyphs");
      var warn = document.getElementById("fit-warn");
      if (warn && max / w < 0.8) warn.hidden = false;
    }
  });
}

function runFit() {
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { fitLabels(); });
  else fitLabels();
}
document.addEventListener("DOMContentLoaded", runFit);

// Hudu asset picker: search the chosen client's Hudu assets and fill the form.
function initPicker() {
  var box = document.getElementById("hudu-picker");
  if (!box) return;
  var clientSel = document.getElementById(box.dataset.clientField);
  var q = document.getElementById("hudu-q");
  var results = document.getElementById("hudu-results");
  var status = document.getElementById("hudu-status");
  var idField = document.getElementById("hudu_asset_id");
  var urlField = document.getElementById("hudu_asset_url");
  var linked = document.getElementById("hudu-linked");
  var timer;

  function showLinked() {
    if (idField.value) {
      linked.hidden = false;
      linked.querySelector("[data-name]").textContent = linked.dataset.name || ("Hudu asset " + idField.value);
      var a = linked.querySelector("a");
      a.href = urlField.value || "#";
      a.hidden = !urlField.value;
    } else {
      linked.hidden = true;
    }
  }

  // Link the form to a Hudu asset; fill in only the fields that are still empty.
  function link(a) {
    idField.value = a.id;
    urlField.value = a.url || "";
    linked.dataset.name = a.name;
    var set = function (id, v) { var el = document.getElementById(id); if (el && !el.value && v) el.value = v; };
    set("device_name", a.name); set("model", a.model); set("serial", a.serial);
    results.innerHTML = "";
    if (suggest) suggest.hidden = true;
    status.textContent = "Linked. Name, model and serial were filled in where empty.";
    showLinked();
  }

  // Typing a serial number suggests the client's Hudu asset with that serial (never links on its own).
  var serialField = document.getElementById("serial");
  var suggest = document.getElementById("hudu-suggest");
  var serialTimer, lastSerial = "";
  function norm(s) { return (s || "").replace(/[\s-]/g, "").toUpperCase(); }
  function suggestBySerial() {
    var serial = serialField.value.trim();
    if (!suggest || idField.value || !clientSel.value || norm(serial).length < 4 || serial === lastSerial) return;
    lastSerial = serial;
    fetch("/api/hudu/assets?client_id=" + encodeURIComponent(clientSel.value) + "&q=" + encodeURIComponent(serial), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error || idField.value || serialField.value.trim() !== serial) return;
        var matches = d.assets.filter(function (a) { return a.serial && norm(a.serial) === norm(serial); });
        if (!matches.length) { suggest.hidden = true; return; }
        var a = matches[0];
        suggest.querySelector("[data-name]").textContent = a.name + (a.type ? " · " + a.type : "") + (matches.length > 1 ? " (and " + (matches.length - 1) + " more)" : "");
        suggest.hidden = false;
        suggest.querySelector("button").onclick = function () {
          if (matches.length === 1) link(a);
          else { q.value = serial; search(); suggest.hidden = true; }
        };
      })
      .catch(function () {});
  }
  if (serialField && suggest) {
    serialField.addEventListener("input", function () { clearTimeout(serialTimer); serialTimer = setTimeout(suggestBySerial, 600); });
    serialField.addEventListener("change", suggestBySerial);
  }

  function search() {
    var cid = clientSel.value;
    if (!cid) { status.textContent = "Choose a client first."; return; }
    status.textContent = "Searching Hudu…";
    results.innerHTML = "";
    fetch("/api/hudu/assets?client_id=" + encodeURIComponent(cid) + "&q=" + encodeURIComponent(q.value), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) { status.textContent = d.error; return; }
        status.textContent = d.assets.length ? d.assets.length + " found" : "No matching assets in Hudu for this client.";
        d.assets.forEach(function (a) {
          var b = document.createElement("button");
          b.type = "button";
          var name = document.createElement("span");
          name.textContent = a.name + (a.type ? " · " + a.type : "");
          var meta = document.createElement("small");
          meta.textContent = [a.model, a.serial].filter(Boolean).join(" · ");
          b.appendChild(name); b.appendChild(meta);
          b.addEventListener("click", function () { link(a); });
          results.appendChild(b);
        });
      })
      .catch(function () { status.textContent = "Couldn't reach the server."; });
  }

  q.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(search, 350); });
  document.getElementById("hudu-go").addEventListener("click", search);
  q.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); search(); } });
  var unlink = document.getElementById("hudu-unlink");
  if (unlink) unlink.addEventListener("click", function () { idField.value = ""; urlField.value = ""; linked.dataset.name = ""; showLinked(); });
  showLinked();
}
document.addEventListener("DOMContentLoaded", initPicker);

// Loan format only makes sense for our own kit: keep the client choice in step.
function initFormatGuard() {
  var form = document.querySelector("form[data-format-guard]");
  if (!form) return;
  var sel = form.querySelector("select[name=client_id]");
  var note = form.querySelector("[data-loan-note]");
  function check() {
    var fmt = form.querySelector("input[name=format]:checked");
    var opt = sel.options[sel.selectedIndex];
    var bad = fmt && fmt.value === "loan" && opt && opt.dataset.internal !== "1";
    if (note) note.hidden = !bad;
  }
  form.addEventListener("change", check);
  check();
}
document.addEventListener("DOMContentLoaded", initFormatGuard);

// Send labels straight to the label printer and report the result inline.
function printDirect(codes, button, out, onPrinted) {
  var label = button.textContent;
  var sel = document.getElementById(button.dataset.strip || "strip-mode");
  var mode = sel && sel.value ? sel.value : "cut";
  button.disabled = true;
  button.textContent = "Printing…";
  fetch("/print/send", { method: "POST", body: new URLSearchParams({ codes: codes, mode: mode }), credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      out.hidden = false;
      out.className = "flash " + (d.ok ? (d.waiting && d.waiting.length ? "warn" : "ok") : "bad");
      out.textContent = d.message;
      if (onPrinted && d.printed && d.printed.length) onPrinted(d.printed);
    })
    .catch(function () {
      out.hidden = false;
      out.className = "flash bad";
      out.textContent = "Couldn't reach the server. Check the Printer page.";
    })
    .finally(function () { button.disabled = false; button.textContent = label; });
}

function initPrintButtons() {
  document.querySelectorAll("[data-print-direct]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var out = document.getElementById(btn.dataset.out);
      printDirect(btn.dataset.printDirect, btn, out, function () {
        if (btn.dataset.reload) setTimeout(function () { location.reload(); }, 1800);
      });
    });
  });

  // Tags list: tick boxes + "Print selected"
  var bar = document.getElementById("select-bar");
  if (!bar) return;
  var boxes = Array.prototype.slice.call(document.querySelectorAll("input[name=pick]"));
  var all = document.getElementById("pick-all");
  var count = document.getElementById("pick-count");
  var direct = document.getElementById("print-selected");
  var preview = document.getElementById("preview-selected");
  function picked() { return boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; }); }
  function update() {
    var p = picked();
    bar.hidden = p.length === 0;
    count.textContent = p.length + " selected";
    if (preview) preview.href = "/print?codes=" + p.join(",");
    if (all) all.checked = p.length === boxes.length && boxes.length > 0;
  }
  boxes.forEach(function (b) { b.addEventListener("change", update); });
  if (all) all.addEventListener("change", function () { boxes.forEach(function (b) { b.checked = all.checked; }); update(); });
  if (direct) direct.addEventListener("click", function () {
    printDirect(picked().join(","), direct, document.getElementById("select-result"), function () {
      setTimeout(function () { location.reload(); }, 2500);
    });
  });
  update();
}
document.addEventListener("DOMContentLoaded", initPrintButtons);

// Batch cutting: remember the last choice in this browser (default: one pre-cut strip).
function initStripMode() {
  var sel = document.getElementById("strip-mode");
  if (!sel) return;
  // A choice passed in from Print a batch wins; otherwise use this browser's last choice.
  var initial = sel.dataset.initial;
  try {
    var saved = localStorage.getItem("tagdesk-strip-mode");
    var MODES = ["precut", "strip", "cut"];
    if (MODES.indexOf(initial) >= 0) {
      sel.value = initial;
      localStorage.setItem("tagdesk-strip-mode", initial);
    } else if (MODES.indexOf(saved) >= 0) {
      sel.value = saved;
    }
  } catch (e) {
    if (["precut", "strip", "cut"].indexOf(initial) >= 0) sel.value = initial;
  }
  sel.addEventListener("change", function () {
    try { localStorage.setItem("tagdesk-strip-mode", sel.value); } catch (e) {}
  });
}
document.addEventListener("DOMContentLoaded", initStripMode);
