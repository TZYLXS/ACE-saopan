/* renhua-web 面板前端：轮询 /api/status，动作走 /api/action */
(function () {
  "use strict";

  var state = { status: null, busy: false, consoleOk: false };

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  function num(v, suffix) {
    if (v === null || v === undefined || v === "") return "--";
    return String(v) + (suffix || "");
  }

  function setMsg(node, text, ok) {
    node.textContent = text || "";
    node.style.color = text && ok === false ? "#e2626b" : "";
  }

  function api(path, opt) {
    return fetch(path, opt).then(function (r) { return r.json(); }).catch(function () {
      return { ok: false, detail: "服务无响应" };
    });
  }

  function action(cmd, extra, msgNode) {
    if (state.busy) return;
    var body = Object.assign({ cmd: cmd }, extra || {});
    state.busy = true;
    if (msgNode) setMsg(msgNode, "执行中…");
    api("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      state.busy = false;
      if (msgNode) setMsg(msgNode, res.ok ? (res.detail || "完成") : (res.detail || "失败"), res.ok);
      refresh();
    });
  }

  function avail(d) {
    var pairs = [["温度", d.temp, "°C"], ["待映射", d.pend, ""], ["离线不可纠正", d.unc, ""],
                 ["CRC", d.crc, ""], ["通电", d.hours, "h"], ["重映射", d.realloc, ""]];
    return pairs;
  }

  // 数据没变就不重建 DOM——每 2 秒的轮询不该让页面闪
  function unchanged(key, value) {
    state.sig = state.sig || {};
    var v = JSON.stringify(value);
    if (state.sig[key] === v) return true;
    state.sig[key] = v;
    return false;
  }

  function drawDisks() {
    var s = state.status;
    var box = el("disks");
    if (unchanged("disks", [s.disks, s.links])) return;
    box.innerHTML = "";
    (s.disks || []).forEach(function (d) {
      var sys = d.can_scan === false;
      var card = document.createElement("div");
      card.className = "disk" + (sys ? " sys" : "");
      var tags = "";
      if (d.can_scan) tags += '<span class="tag ok">可扫描/测速</span>';
      if (d.verified) tags += '<span class="tag ok">已校验通过</span>';
      if (d.health && d.health.toUpperCase().indexOf("FAIL") >= 0) tags += '<span class="tag">SMART 异常</span>';
      var kv = avail(d).map(function (p) {
        var v = num(p[1], p[2]);
        var cls = "";
        if (p[0] === "待映射" && p[1] > 0) cls = "warn";
        if (p[0] === "离线不可纠正" && p[1] > 0) cls = "bad";
        if (p[0] === "CRC" && p[1] > 0) cls = "warn";
        return "<div><span>" + p[0] + "</span><b class=\"" + cls + "\">" + esc(v) + "</b></div>";
      }).join("");
      card.innerHTML =
        "<h3>" + esc(d.name) + " <small>ATA" + esc(d.ata || "?") + " · " + esc(d.size_gb) + " GB · " + esc(d.spd) + "</small> " + tags + "</h3>" +
        "<div class=\"model\">" + esc(d.model || "未知型号") + "　" + esc(d.sn || "") + "</div>" +
        "<div class=\"kvp\">" + kv + "</div>";
      box.appendChild(card);
    });
  }

  function drawAta(ata, s) {
    if (!ata) return;
    ata.innerHTML = "";
    var links = s.links || {};
    for (var i = 1; i <= 6; i++) {
      var has = (s.disks || []).some(function (d) { return d.ata === i; });
      var sp = document.createElement("span");
      sp.className = has ? "on" : "off";
      sp.innerHTML = "<i></i>ATA" + i + " " + (has ? esc(links[i] || "有盘") : "空口");
      ata.appendChild(sp);
    }
  }

  // F3 端口识别：哪个口有盘、盘是谁、跑什么速率
  function drawPorts() {
    var s = state.status;
    var box = el("portRows");
    if (unchanged("ports", [s.disks, s.links])) {
      drawPortSummary(s);
      return;
    }
    drawAta(el("ataPorts"), s);
    box.innerHTML = "";
    var byAta = {};
    (s.disks || []).forEach(function (d) { if (d.ata) byAta[d.ata] = d; });

    function metric(label, value, cls) {
      return '<span class="m"><i>' + esc(label) + "</i><b" +
        (cls ? ' class="' + cls + '"' : "") + ">" + esc(value) + "</b></span>";
    }

    for (var i = 1; i <= 6; i++) {
      var d = byAta[i];
      var row = document.createElement("div");
      if (!d) {
        row.className = "portcard empty";
        row.innerHTML = '<span class="pname">ATA' + i + "</span>" +
          '<span class="pdisk">空口 · 没接盘或没识别到</span>';
        box.appendChild(row);
        continue;
      }
      row.className = "portcard";
      var alerts = [];
      if (d.pend > 0) alerts.push("待映射 " + d.pend);
      if (d.unc > 0) alerts.push("离线不可纠正 " + d.unc);
      if (d.crc > 0) alerts.push("CRC " + d.crc);
      var smartBad = d.health && String(d.health).toUpperCase().indexOf("FAIL") >= 0;
      row.innerHTML =
        '<span class="pname">ATA' + i + "<em>有盘</em></span>" +
        '<span class="pdisk"><b>' + esc(d.model || "未知型号") + "</b>" +
        "<small>" + esc(d.name) + " · " + esc(d.sn || "无 SN") + "</small></span>" +
        '<span class="pmetrics">' +
        metric("容量", d.size_gb + " GB") +
        metric("链路", d.spd || "--") +
        metric("温度", num(d.temp, "°C")) +
        metric("SMART", d.health || "PASSED", smartBad ? "bad" : "good") +
        "</span>" +
        (alerts.length
          ? '<span class="palerts">' + alerts.map(function (a) {
              return '<span class="chip">' + esc(a) + "</span>";
            }).join("") + "</span>"
          : "");
      box.appendChild(row);
    }
    drawPortSummary(s);
  }

  // 概览页那行摘要（F1 帮助页也用它）
  function drawPortSummary(s) {
    var st = s.scan || {}, sp = s.speed || {}, hits = s.hits || [];
    var pageTxt = (s.page != null && s.page >= 0) ? "F" + (s.page + 1) : "--";
    var ov = el("ovState");
    if (!ov) return;
    var html = [
      "物理屏 <b>" + esc(pageTxt) + "</b>",
      "盘 <b>" + (s.disks || []).length + " 块</b>",
      "扫描 <b>" + (st.running ? ("进行中 " + (st.pct == null ? "--" : st.pct.toFixed(1) + "%")) : "空闲") + "</b>",
      "测速 <b>" + (sp.running ? ("进行中 " + (sp.pct == null ? "--" : sp.pct.toFixed(0) + "%")) : "空闲") + "</b>",
      "报错命中 <b>" + hits.length + " 类</b>"
    ].join("");
    if (unchanged("ovstate", html)) return;
    ov.innerHTML = html;
  }

  function fillSelect(sel, disks, keepValue) {
    var cur = keepValue || sel.value;
    sel.innerHTML = "";
    disks.forEach(function (d) {
      var o = document.createElement("option");
      o.value = "/dev/" + d.name;
      o.textContent = d.name + " · " + (d.model || "") + " (" + d.size_gb + " GB)";
      sel.appendChild(o);
    });
    if (cur) {
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === cur) { sel.selectedIndex = i; break; }
      }
    }
  }

  function drawScan() {
    var s = state.status;
    var st = s.scan || {};
    var pct = st.pct == null ? null : st.pct;
    var bar = el("scanBar");
    bar.style.width = (pct == null ? 0 : Math.max(0, Math.min(100, pct))) + "%";
    bar.parentNode.className = "bar" + (pct == null ? " idle" : "");
    el("scanStart").disabled = !!st.running;
    el("scanStop").disabled = !st.running;
    var dev = st.dev || "未选择";
    var kv = [
      "盘 <b>" + esc(dev) + "</b>",
      "进度 <b>" + (pct == null ? "--" : pct.toFixed(1) + "%") + "</b>",
      "已用 <b>" + (st.elapsed == null ? "--" : fmtHms(st.elapsed)) + "</b>",
      "速度 <b>" + (st.mbps ? st.mbps.toFixed(1) + " MB/s" : "--") + "</b>",
      "错误 读/写/校验 <b>" + esc(st.errs || "--") + "</b>",
      "状态 <b>" + (st.running ? "扫描中" : (st.interrupted ? "上次被中断" : "空闲")) + "</b>"
    ].join("");
    el("scanInfo").innerHTML = kv;
    var adv = el("scanAdvice");
    if (s.scan_advice) {
      adv.className = "advice";
      adv.textContent = s.scan_advice;
    } else {
      adv.className = "advice hide";
      adv.textContent = "";
    }
  }

  function drawSpeed() {
    var s = state.status;
    var st = s.speed || {};
    var bar = el("speedBar");
    var pct = st.pct == null ? 0 : st.pct;
    bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    bar.parentNode.className = "bar" + (pct ? "" : " idle");
    el("speedStart").disabled = !!st.running;
    el("speedStop").disabled = !st.running;

    var stages = s.stages || [];
    var idx = st.stage_i == null ? -1 : st.stage_i;
    var kv = [
      "当前 <b>" + esc(st.stage || "空闲") + (idx >= 0 ? "（" + (idx + 1) + "/" + (stages.length || 8) + "）" : "") + "</b>",
      "总进度 <b>" + (st.pct == null ? "--" : st.pct.toFixed(0) + "%") + "</b>",
      "实时 <b>" + (st.cur ? st.cur.toFixed(1) + " MB/s" : "--") + "</b>",
      "用时 <b>" + (st.elapsed ? fmtHms(st.elapsed) : "--") + "</b>",
      st.mount ? "写测试落在 <b>" + esc(st.mount) + "</b>" : ""
    ].join("");
    el("speedNow").innerHTML = kv;

    var rows = st.results || [];
    var body = el("speedRows");
    body.innerHTML = "";
    var names = stages.length ? stages : ["顺序读 1GB", "顺序写 1GB", "小文件写 32MB", "小文件读 32MB",
                                          "零碎写 32MB", "零碎读 32MB", "RND4K Q1T1 读", "RND4K Q1T1 写"];
    names.forEach(function (n, i) {
      var tr = document.createElement("tr");
      var val = rows[i] ? rows[i][1] : "未跑";
      tr.innerHTML = "<td>" + esc(n) + "</td><td>" + esc(val) + "</td>";
      body.appendChild(tr);
    });
    if (st.err) setMsg(el("speedMsg"), "错误：" + st.err, false);
  }

  function drawHits() {
    var s = state.status;
    var box = el("hits");
    box.innerHTML = "";
    var hits = s.hits || [];
    if (!hits.length) {
      box.innerHTML = "<p class=\"tip\">近 24 小时没有命中已知报错。盘状态干净。</p>";
    }
    hits.forEach(function (h) {
      var d = document.createElement("div");
      d.className = "hit sev-" + (h.sev || "提示") + (h.covered ? " covered" : "");
      var cover = "";
      if (h.covered && h.cover_ts) {
        var ct = new Date(h.cover_ts * 1000);
        cover = '<span class="cover">扫描前 · 已被 ' +
          ("0" + ct.getHours()).slice(-2) + ":" + ("0" + ct.getMinutes()).slice(-2) +
          " 的 0 坏块校验覆盖</span>";
      }
      d.innerHTML =
        "<div class=\"h\"><b>" + esc(h.title) + "</b>" +
        "<span class=\"sev\">" + esc(h.sev) + " ×" + esc(h.n) + "</span>" + cover + "</div>" +
        "<p>" + esc(h.human || "") + "</p>" +
        (h.advice ? "<div class=\"adv\">建议：" + esc(h.advice) + "</div>" : "") +
        (h.sample ? "<code>" + esc(h.sample) + "</code>" : "");
      box.appendChild(d);
    });
    if (s.hits_time) {
      el("hitsTime").textContent = "近 24 小时 · 命中 " + hits.length + " 类 · 更新于 " +
        new Date(s.hits_time * 1000).toLocaleTimeString();
    }
  }

  function fmtHms(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
    return (h ? h + ":" : "") + (h || m ? pad(m) + ":" : "") + pad(s2);
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function render() {
    var s = state.status;
    if (!s) return;
    el("host").textContent = s.host || "--";
    el("clock").textContent = s.time || "--";
    var badge = el("consoleState");
    if (s.console) {
      badge.className = "badge ok";
      badge.textContent = "控制台在线";
    } else {
      badge.className = "badge bad";
      badge.textContent = "控制台未运行";
    }
    // 物理屏被按键切走 → 网页跟随；但刚点过页签、服务器还没确认时不要抢回来
    var p = (typeof s.page === "number") ? s.page : -1;
    if (p >= 0 && p !== lastPage) {
      if (p === pendingPage) {
        pendingPage = -1;
        applyPage(p);
      } else if (pendingPage < 0 || Date.now() - pendingAt > 4000) {
        pendingPage = -1;
        applyPage(p);
      }
    }
    drawCurrent();
    el("msgLine").textContent = s.msg || "";
  }

  var lastCands = "";

  function refresh() {
    api("/api/status").then(function (res) {
      var s = (res && res.data) ? res.data : (res || {});
      state.status = s;
      var cands = (s.disks || []).filter(function (d) { return d.can_scan; }).map(function (d) { return d.name; }).join(",");
      if (cands !== lastCands) {
        lastCands = cands;
        fillSelect(el("scanDev"), (s.disks || []).filter(function (d) { return d.can_scan; }), el("scanDev").value);
        fillSelect(el("speedDev"), (s.disks || []).filter(function (d) { return d.can_scan; }), el("speedDev").value);
      }
      render();
    });
  }

  el("scanStart").onclick = function () { action("scan_start", { dev: el("scanDev").value }, el("scanMsg")); };
  el("scanStop").onclick = function () { action("scan_stop", {}, el("scanMsg")); };
  el("speedStart").onclick = function () { action("speed_start", { dev: el("speedDev").value }, el("speedMsg")); };
  el("speedStop").onclick = function () { action("speed_stop", {}, el("speedMsg")); };

  // ---------- 导航 = 物理屏 F1-F6：点一下切屏，屏幕被按键切走时这里跟着高亮 ----------
  var PAGE_VIEW = ["help", "scan", "ports", "smart", "hits", "speed"];
  var lastPage = -1;
  var pendingPage = -1;      // 刚点过、还没等到服务器确认的页
  var pendingAt = 0;

  // 只切视图与高亮，不碰数据（避免点一下就被旧状态弹回去）
  function applyPage(idx) {
    if (idx < 0 || idx >= PAGE_VIEW.length) return;
    PAGE_VIEW.forEach(function (name, i) {
      var node = el("view-" + name);
      if (node) node.className = "view" + (i === idx ? " on" : "");
    });
    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].className = "tab" + (i === idx ? " on" : "");
    }
    lastPage = idx;
    state.page = idx;
    try {
      if (location.hash !== "#f" + (idx + 1)) history.replaceState(null, "", "#f" + (idx + 1));
    } catch (e) {}
  }

  // 只重画看得见的那一页，其余页面不动
  function drawCurrent() {
    if (!state.status) return;
    switch (lastPage) {
      case 1: drawScan(); break;
      case 2: drawPorts(); break;
      case 3: drawDisks(); break;
      case 4: drawHits(); break;
      case 5: drawSpeed(); break;
      default: break;
    }
  }

  function showPage(idx, tellScreen) {
    if (idx < 0 || idx >= PAGE_VIEW.length) return;
    applyPage(idx);
    if (tellScreen) {
      pendingPage = idx;
      pendingAt = Date.now();
      action("page", { page: idx });
    }
    drawCurrent();
  }

  var tabs = document.querySelectorAll(".tab");
  for (var ti = 0; ti < tabs.length; ti++) {
    tabs[ti].addEventListener("click", function () {
      showPage(parseInt(this.getAttribute("data-page"), 10) || 0, true);
    });
  }

  var startPage = 0;
  var m = /^#f([1-6])$/.exec(location.hash || "");
  if (m) startPage = parseInt(m[1], 10) - 1;
  applyPage(startPage);

  window.addEventListener("hashchange", function () {
    var mm = /^#f([1-6])$/.exec(location.hash || "");
    if (mm) showPage(parseInt(mm[1], 10) - 1, false);
  });

  refresh();
  setInterval(refresh, 2000);
})();
