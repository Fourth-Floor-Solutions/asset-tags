// Tag a site: scan a blank tag with the camera, link it to a device, save, scan the next.
// QR decoding is jsQR (static/jsQR.js, Apache-2.0); the camera is only used after a tap.
(function () {
  "use strict";
  var app = document.getElementById("site-app");
  if (!app) return;

  var ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  var STORE = "tagdesk-site";
  var $ = function (id) { return document.getElementById(id); };
  var hudu = app.dataset.hudu === "1";
  var state = load();
  var stream = null, scanning = false, lastCode = "", lastAt = 0, current = null, devices = [], linked = null;

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; }
  }
  function save() {
    try { localStorage.setItem(STORE, JSON.stringify(state)); } catch (e) {}
  }

  // A code from a QR link (https://…/7KQ3M) or typed: O->0, I/L->1, five Crockford characters.
  function parseCode(raw) {
    var s = String(raw || "").trim();
    var m = s.match(/\/([0-9A-Za-z-]{5,7})\/?$/);
    if (m) s = m[1];
    s = s.toUpperCase().replace(/[\s-]/g, "").replace(/O/g, "0").replace(/[IL]/g, "1");
    if (s.length !== 5) return null;
    for (var i = 0; i < 5; i++) if (ALPHABET.indexOf(s[i]) < 0) return null;
    return s;
  }

  function api(method, url, body) {
    return fetch(url, {
      method: method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { d._status = r.status; return d; });
    });
  }

  // ---------- setup ----------
  function showSetup() {
    stopCamera();
    $("site-setup").hidden = false;
    $("site-scan").hidden = true;
    if (state.client) $("site-client").value = state.client;
    $("site-name").value = state.site || "";
  }
  function showScan() {
    $("site-setup").hidden = true;
    $("site-scan").hidden = false;
    var sel = $("site-client");
    var opt = sel.querySelector('option[value="' + state.client + '"]');
    $("site-title").textContent = (opt ? opt.textContent : "Client") + (state.site ? " · " + state.site : "");
    renderDone();
    loadDevices();
  }
  $("site-start").addEventListener("click", function () {
    var client = $("site-client").value;
    if (state.client !== client) state.done = [];
    state.client = client;
    state.site = $("site-name").value.trim();
    save();
    showScan();
  });
  $("site-change").addEventListener("click", showSetup);

  // ---------- Hudu devices (from the coverage cache) ----------
  function loadDevices() {
    devices = [];
    if (!hudu) return;
    api("GET", "/api/site/devices?client_id=" + encodeURIComponent(state.client)).then(function (d) {
      devices = d.devices || [];
      var hint = $("f-hudu-hint");
      if (hint) hint.textContent = d.fetched
        ? devices.length + " devices without a tag (from Hudu, " + d.fetched.slice(0, 10) + ")."
        : "Refresh this client on the Coverage page to pick from its Hudu devices.";
    });
  }
  function renderDevices() {
    var box = $("f-devices");
    if (!box) return;
    box.innerHTML = "";
    if (linked) return;
    var q = ($("f-hudu-q").value || "").toLowerCase();
    var serial = ($("f-serial").value || "").replace(/[\s-]/g, "").toUpperCase();
    var list = devices.filter(function (a) {
      if (serial && a.serial && a.serial.replace(/[\s-]/g, "").toUpperCase() === serial) return true;
      return q && (a.name + " " + a.model + " " + a.serial).toLowerCase().indexOf(q) >= 0;
    }).slice(0, 8);
    list.forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "quiet";
      var n = document.createElement("span"); n.textContent = a.name;
      var m = document.createElement("small"); m.textContent = [a.type, a.model, a.serial].filter(Boolean).join(" · ");
      b.appendChild(n); b.appendChild(m);
      b.addEventListener("click", function () { link(a); });
      box.appendChild(b);
    });
  }
  function link(a) {
    linked = a;
    var fill = function (id, v) { var el = $(id); if (el && !el.value && v) el.value = v; };
    fill("f-name", a.name); fill("f-model", a.model); fill("f-serial", a.serial);
    var l = $("f-linked");
    l.querySelector("[data-name]").textContent = a.name;
    l.hidden = false;
    $("f-hudu-q").value = "";
    renderDevices();
  }
  if (hudu) {
    $("f-hudu-q").addEventListener("input", renderDevices);
    $("f-serial").addEventListener("input", renderDevices);
    $("f-unlink").addEventListener("click", function () { linked = null; $("f-linked").hidden = true; renderDevices(); });
  }

  // ---------- camera ----------
  var video = $("site-video");
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d", { willReadFrequently: true });

  function camStatus(t) { $("site-cam-status").textContent = t || ""; }
  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      camStatus("This browser can't use the camera here. Type the code instead.");
      return;
    }
    camStatus("Starting the camera…");
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(function (s) {
        stream = s;
        video.srcObject = s;
        return video.play();
      })
      .then(function () {
        $("site-camera").classList.add("on");
        camStatus("Point the camera at a tag's QR code.");
        scanning = true;
        requestAnimationFrame(tick);
      })
      .catch(function () { camStatus("No camera access. Allow it in the browser's settings, or type the code."); });
  }
  function stopCamera() {
    scanning = false;
    $("site-camera").classList.remove("on");
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
  }
  function tick() {
    if (!scanning) return;
    if (video.readyState === video.HAVE_ENOUGH_DATA && window.jsQR) {
      var w = Math.min(640, video.videoWidth), h = Math.round(video.videoHeight * w / video.videoWidth);
      canvas.width = w; canvas.height = h;
      ctx.drawImage(video, 0, 0, w, h);
      var hit = window.jsQR(ctx.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: "dontInvert" });
      if (hit && hit.data) {
        var code = parseCode(hit.data);
        var now = Date.now();
        if (code && (code !== lastCode || now - lastAt > 4000)) {
          lastCode = code; lastAt = now;
          if (navigator.vibrate) navigator.vibrate(60);
          stopCamera();
          open(code);
          return;
        }
        if (!code) camStatus("That QR code isn't a TagDesk tag.");
      }
    }
    setTimeout(function () { requestAnimationFrame(tick); }, 150);
  }
  $("site-cam-on").addEventListener("click", startCamera);
  $("site-manual").addEventListener("submit", function (e) {
    e.preventDefault();
    var code = parseCode($("site-code").value);
    if (!code) { camStatus("Codes are five characters, like 7KQ3M."); return; }
    $("site-code").value = "";
    stopCamera();
    open(code);
  });

  // ---------- one tag ----------
  function open(code) {
    $("site-saved").hidden = true;
    api("GET", "/api/site/tag/" + encodeURIComponent(code)).then(function (t) {
      if (t._status !== 200) { camStatus(t.error || "Couldn't look up " + code + "."); startCamera(); return; }
      current = t;
      linked = null;
      $("f-code").textContent = t.code;
      var err = $("f-error");
      err.hidden = true;
      var stateEl = $("f-state");
      stateEl.className = "pill " + t.status;
      stateEl.textContent = t.status === "active" ? "On " + (t.device_name || "a device") : t.status === "retired" ? "Retired" : "Blank";
      ["f-name", "f-model", "f-serial"].forEach(function (id) { $(id).value = ""; });
      if (hudu) { $("f-linked").hidden = true; $("f-hudu-q").value = ""; renderDevices(); }
      if (String(t.client_id) !== String(state.client)) {
        err.textContent = t.code + " is printed for " + t.client_name + ", not this client. Use one of their tags.";
        err.hidden = false;
      } else if (t.status === "active") {
        err.textContent = t.code + " is already on " + (t.device_name || "a device") + ". Saving replaces those details.";
        err.hidden = false;
        $("f-name").value = t.device_name; $("f-model").value = t.model; $("f-serial").value = t.serial;
      } else if (t.status === "retired") {
        err.textContent = t.code + " is retired. Use a new tag.";
        err.hidden = false;
      }
      $("site-form").hidden = false;
      $("site-camera").hidden = true;
      $("site-manual").hidden = true;
      (hudu && devices.length ? $("f-hudu-q") : $("f-name")).focus();
    });
  }
  function closeForm(andScan) {
    current = null;
    $("site-form").hidden = true;
    $("site-camera").hidden = false;
    $("site-manual").hidden = false;
    if (andScan) startCamera();
  }
  $("f-skip").addEventListener("click", function () { closeForm(true); });
  $("site-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (!current) return;
    var btn = $("f-save");
    btn.disabled = true;
    api("POST", "/api/site/assign", {
      code: current.code,
      client_id: state.client,
      site: state.site,
      device_name: $("f-name").value,
      model: $("f-model").value,
      serial: $("f-serial").value,
      hudu_asset_id: linked ? linked.id : "",
      hudu_asset_url: linked ? linked.url : "",
      replace: current.status === "active",
    }).then(function (d) {
      btn.disabled = false;
      if (d._status !== 200) {
        var err = $("f-error");
        err.textContent = d.error || "Couldn't save.";
        err.hidden = false;
        return;
      }
      var t = d.tag;
      if (linked) devices = devices.filter(function (a) { return a.id !== linked.id; });
      state.done = (state.done || []).filter(function (x) { return x.code !== t.code; });
      state.done.unshift({ code: t.code, device: t.device_name || t.serial, at: new Date().toISOString().slice(11, 16) });
      save();
      renderDone();
      var ok = $("site-saved");
      ok.textContent = "Saved " + t.code + " → " + (t.device_name || t.serial) + "." + (d.hudu_error ? " Hudu wasn't updated: " + d.hudu_error : "");
      ok.className = "flash " + (d.hudu_error ? "warn" : "ok");
      ok.hidden = false;
      closeForm(true);
    }).catch(function () { btn.disabled = false; });
  });

  function renderDone() {
    var list = $("site-done");
    list.innerHTML = "";
    var done = state.done || [];
    done.forEach(function (x) {
      var li = document.createElement("li");
      var time = document.createElement("time"); time.textContent = x.at;
      var span = document.createElement("span");
      var a = document.createElement("a"); a.href = "/tags/" + x.code; a.className = "mono"; a.textContent = x.code;
      span.appendChild(a); span.appendChild(document.createTextNode(" → " + (x.device || "")));
      li.appendChild(time); li.appendChild(span);
      list.appendChild(li);
    });
    $("site-done-box").hidden = !done.length;
    $("site-count").textContent = done.length ? " · " + done.length + " tagged" : "";
  }
  $("site-clear").addEventListener("click", function () { state.done = []; save(); renderDone(); });

  document.addEventListener("visibilitychange", function () { if (document.hidden) stopCamera(); });

  if (state.client && $("site-client").querySelector('option[value="' + state.client + '"]')) showScan();
  else showSetup();
})();
