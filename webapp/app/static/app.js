/* ============================================================
   idocker —— 爱快 Docker 管理台（前端）
   ------------------------------------------------------------
   零依赖、无构建：一个 IIFE，按页面分区。
   约定：所有写操作先确认、再执行、执行后回读刷新；
        危险按钮一律 danger 色，文案必须说清「会发生什么」。
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = {
    containers: [],
    checkMap: {},        // "repo:tag" → 上游状态
    pinMap: {},          // "repo:tag" → "ahead" 表示锁定在固定版本、但 latest 已更新
    checkAt: "",
    selected: {},
    job: null,
    source: null,
    flow: [],
    logs: [],
    mirrors: [],         // 爱快 Docker 服务设置里的镜像加速源
    inited: false,
    progress: { current: 0, total: 0 },
    target: null,        // 当前正在选版本的容器
    settings: {},        // 下载超时 + 定时任务 + 通知
    setInited: false,
    setNotify: "fail",   // 设置弹窗里选的通知时机（off / fail / always）
    scEdit: null,        // 正在编辑的定时任务（null = 新建）
    scMode: "check",
    scFreq: "every",
    // ---- 上游版本检测 ----
    checkTs: 0,
    detecting: false,
    routerReady: false,
    jobKind: "",
    jobTargets: [],
    autoCheckedAt: 0,
    // ---- 自动刷新 ----
    auto: true,
    busy: false,
    autoTimer: null,
    nextAt: 0,
    lastRefresh: 0,
    ctSig: "",
    histSig: "",
    // ---- 管理台 ----
    page: "overview",
    statMap: {},         // 容器名 → {cpu, mem}
    images: [],
    networks: [],
    compose: [],
    audit: [],
    drawer: null,        // {name, tab, timer}
    ctSearch: "",
    ctFilter: "all",
    histFilter: "all",
    imgSearch: { keyword: "", page: 1, total: 0, list: [] }
  };

  var STEP_ORDER = ["login", "inspect", "pull", "download", "update", "verify"];
  var PAGE_TITLES = {
    overview: "概览", containers: "容器", images: "镜像", networks: "网络",
    compose: "编排", schedules: "定时任务", history: "执行历史", audit: "操作审计"
  };

  // 自动刷新间隔：默认 60 秒；可用 ?autorefresh=秒数 覆盖（5~3600），方便调试
  var AUTO_MS = (function () {
    var m = /[?&]autorefresh=(\d+)/.exec(location.search || "");
    var sec = m ? parseInt(m[1], 10) : 60;
    if (!(sec >= 5)) sec = 60;
    if (sec > 3600) sec = 3600;
    return sec * 1000;
  })();
  // 自动版本检测：打开页面时若上次检测结果已过期，就自己跑一次。
  // ?autocheck=秒 可调（最小 60），?autocheck=0 彻底关闭。
  var AUTO_CHECK_MS = (function () {
    var m = /[?&]autocheck=(\d+)/.exec(location.search || "");
    if (!m) return 600 * 1000;
    var sec = parseInt(m[1], 10);
    if (!(sec > 0)) return 0;
    if (sec < 60) sec = 60;
    return sec * 1000;
  })();

  /* ---------------- 基础 ---------------- */
  function goLogin() {
    var back = (location.pathname || "/") + (location.search || "");
    location.href = "/login?next=" + encodeURIComponent(back);
  }

  function api(path, opts) {
    opts = opts || {};
    return fetch(path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      if (r.status === 401) {
        goLogin();
        throw new Error("登录已过期，正在跳转登录页…");
      }
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  // 爱快有些字段「空」的时候给的是 {} 而不是 []（实测：没被使用的镜像，
  // containers 是 {}），直接 .map 会炸「is not a function」。统一收口。
  function asList(v) {
    return Array.isArray(v) ? v : [];
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showBanner(id, msg) {
    var el = $(id);
    if (!el) return;
    if (!msg) { el.classList.add("hidden"); return; }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
           p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function fmtClock(ts) {
    if (!ts) return "--:--:--";
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n <= 0) return "—";
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }

  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return sec + " 秒";
    var m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return m + " 分钟" + (s ? " " + s + " 秒" : "");
    var h = Math.floor(m / 60), mm = m % 60;
    return h + " 小时" + (mm ? " " + mm + " 分" : "");
  }

  /* ---------------- 轻提示 ---------------- */
  function toast(msg, type, ms) {
    var wrap = $("toastWrap");
    if (!wrap || !msg) return;
    var el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    wrap.appendChild(el);
    while (wrap.childNodes && wrap.childNodes.length > 4) {
      try { wrap.removeChild(wrap.childNodes[0]); } catch (e) { break; }
    }
    setTimeout(function () {
      try { if (wrap.removeChild) wrap.removeChild(el); } catch (e) { /* 已移除 */ }
    }, ms || 4200);
  }

  /* ---------------- 确认 / 告知弹窗 ---------------- */
  var _boxResolve = null;

  function openBox(opts) {
    return new Promise(function (resolve) {
      _boxResolve = resolve;
      $("cfmTitle").textContent = opts.title || "确认操作";
      $("cfmBody").innerHTML = opts.html || "";
      $("cfmOk").textContent = opts.okText || "确定";
      $("cfmOk").className = "btn " + (opts.danger ? "danger" : "primary");
      $("cfmCancel").classList.toggle("hidden", !!opts.oneButton);
      $("cfmModal").classList.remove("hidden");
    });
  }

  function closeBox(value) {
    $("cfmModal").classList.add("hidden");
    var r = _boxResolve;
    _boxResolve = null;
    if (r) r(!!value);
  }

  function confirmBox(opts) { return openBox(opts); }
  function infoBox(title, html) {
    return openBox({ title: title, html: html, okText: "知道了", oneButton: true });
  }

  /* ============================================================
     一、总览
     ============================================================ */
  function loadOverview(opts) {
    var silent = !!(opts && opts.silent);
    var hasData = state.containers.length > 0;
    if (!silent && !hasData) {
      showBanner("errBanner", "");
      $("ctBody").innerHTML = '<tr><td colspan="8" class="empty">加载中…</td></tr>';
    }
    return api("/api/overview").then(function (j) {
      var d = j.data || {};
      state.containers = d.containers || [];
      var cfg = d.config || {};
      var srv = (d.server && d.server.overview) || {};

      $("connInfo").textContent =
        (cfg.router_url || "未配置") + " · 账号 " + (cfg.username || "-") +
        " · Docker " + (srv.docker_version || "?") +
        " · " + (srv.status === 1 ? "服务运行中" : "服务已停止");
      var ver = "v" + (cfg.version || "?");
      $("verInfo").textContent = ver;
      $("footVer").textContent = ver + " · 爱快 Docker 管理台";

      if (!state.inited) {
        $("dryRun").checked = !!cfg.dry_run;
        state.inited = true;
      }

      var btnLogout = $("btnLogout");
      if (btnLogout) {
        if (cfg.auth_enabled) btnLogout.classList.remove("hidden");
        else btnLogout.classList.add("hidden");
      }

      if (!cfg.router_url) {
        state.routerReady = false;
        showBanner("cfgBanner", "⚠ 尚未配置爱快地址。请给容器设置环境变量 IKUAI_URL / IKUAI_USER / IKUAI_PASS 后重启容器。");
      } else if (!cfg.password_set) {
        state.routerReady = false;
        showBanner("cfgBanner", "⚠ 未配置登录密码（IKUAI_PASS），无法读取容器。");
      } else {
        state.routerReady = true;
        showBanner("cfgBanner", "");
      }

      $("ctCount").textContent = state.containers.length;
      $("navCtCount").textContent = state.containers.length;
      var imgEl = $("imageCount");
      if (imgEl) imgEl.textContent = (d.images_count || 0) + " 个";
      $("navImgCount").textContent = d.images_count || 0;
      state.mirrors = d.mirrors || [];

      // 容器实时资源（来自 docker_server overview.running[]）
      state.statMap = {};
      var running = (srv.overview && srv.overview.running) || [];
      running.forEach(function (r) {
        if (!r || !r.name) return;
        state.statMap[r.name] = { cpu: r.cpu_used || "", mem: r.memused || 0 };
      });

      // 恢复上次检测结果，刷新页面后上游状态不丢
      var ck = d.check || {};
      state.checkMap = {};
      state.pinMap = {};
      var items = ck.items || {};
      Object.keys(items).forEach(function (k) {
        state.checkMap[k] = items[k].status;
        if (items[k].pinned) state.pinMap[k] = items[k].pinned;
      });
      state.checkTs = ck.checked_at || 0;
      state.checkAt = ck.at_str || "";
      $("checkTime").textContent = state.checkAt ? ("上次检测 " + state.checkAt.slice(5)) : "";

      renderContainers();
      renderHistory(d.history || []);
      applySettings(d.settings || {});
      renderStats(srv);

      state.lastRefresh = Date.now() / 1000;
      $("lastRefresh").textContent = "刷新于 " + fmtClock(state.lastRefresh);
      loadStats();
      maybeAutoCheck();
    }).catch(function (e) {
      showBanner("errBanner", "读取失败：" + e.message);
      if (!state.containers.length) {
        $("ctBody").innerHTML = '<tr><td colspan="8" class="empty">读取失败</td></tr>';
      }
    });
  }

  /* ---------------- 概览卡片 ---------------- */
  function renderStats(srv) {
    var total = state.containers.length;
    var running = state.containers.filter(function (c) { return c.state === "running"; }).length;
    var newer = updatableContainers().length;
    $("statContainers").textContent = total;
    $("statContainersSub").textContent = running + " 个运行中 · " + (total - running) + " 个已停止";
    $("statImages").textContent = $("navImgCount").textContent || "—";
    $("statImagesSub").textContent = state.mirrors.length + " 个加速源";
    $("statUpdates").textContent = state.checkTs ? newer : "—";
    $("statUpdatesSub").textContent = state.checkTs
      ? (newer ? "点「容器」页可一键更新" : "全部已是最新")
      : "尚未检测";
    $("statsAt").textContent = state.lastRefresh ? ("刷新于 " + fmtClock(state.lastRefresh)) : "";

    // 资源 TOP：按内存排序取前 6
    var rows = state.containers.map(function (c) {
      var s = state.statMap[c.name] || {};
      return { name: c.name, cpu: s.cpu || "—", mem: s.mem || 0 };
    }).sort(function (a, b) { return b.mem - a.mem; }).slice(0, 6);
    var max = rows.length ? Math.max.apply(null, rows.map(function (r) { return r.mem; })) : 0;
    $("topStats").innerHTML = rows.length ? rows.map(function (r) {
      var pct = max ? Math.max(2, Math.round(r.mem / max * 100)) : 0;
      return '<div class="bar-row">' +
        '<span class="bar-name">' + esc(r.name) + '</span>' +
        '<span class="bar-track"><i style="width:' + pct + '%"></i></span>' +
        '<span class="bar-val mono">' + fmtBytes(r.mem) + ' · ' + esc(r.cpu) + '</span>' +
        '</div>';
    }).join("") : '<div class="empty">没有运行中的容器</div>';
  }

  function loadStats() {
    if (state.busy || state.source) return Promise.resolve();
    return api("/api/stats").then(function (j) {
      var d = j.data || {};
      var next = {};
      Object.keys(d).forEach(function (name) {
        var v = d[name] || {};
        next[name] = { cpu: v.cpu || "", mem: v.mem || 0 };
      });
      // 数字没变就别碰 DOM：静默刷新不该让表格闪一下
      if (JSON.stringify(next) === JSON.stringify(state.statMap)) return;
      state.statMap = next;
      state.ctSig = "";
      renderContainers();
      renderStats();
    }).catch(function () { /* 拿不到就保留上次的数字，不打断界面 */ });
  }

  function loadDisk() {
    api("/api/server/disks").then(function (j) {
      var disks = j.data || [];
      var total = 0;
      disks.forEach(function (d) { total += Number(d.st_size) || 0; });
      $("statDisk").textContent = total ? fmtBytes(total) : "—";
      $("statDiskSub").textContent = disks.length
        ? (disks.map(function (d) { return d.f_name; }).join("、") || "docker 目录")
        : "未读取到磁盘信息";
    }).catch(function () {
      $("statDisk").textContent = "—";
      $("statDiskSub").textContent = "读取失败";
    });
  }

  function renderOverviewHistory(list) {
    var el = $("ovHistory");
    if (!el) return;
    var rows = (list || []).slice(0, 6);
    el.innerHTML = rows.length ? rows.map(function (h) {
      return '<div class="mini-row">' +
        '<span class="badge ' + (h.ok ? "latest" : "failed") + '">' + (h.ok ? "成功" : "失败") + '</span>' +
        '<span class="mini-main">' + esc((h.targets || []).join("、") || (h.kind === "check" ? "版本检测" : "更新")) + '</span>' +
        '<span class="mini-time">' + esc(fmtTime(h.finished_at || h.created).slice(6)) + '</span>' +
        '</div>';
    }).join("") : '<div class="empty">暂无记录</div>';
  }

  function renderOverviewAudit(list) {
    var el = $("ovAudit");
    if (!el) return;
    var rows = (list || []).slice(0, 6);
    el.innerHTML = rows.length ? rows.map(function (r) {
      return '<div class="mini-row">' +
        '<span class="badge ' + (r.ok ? "latest" : "failed") + '">' + (r.ok ? "成功" : "失败") + '</span>' +
        '<span class="mini-main">' + esc(r.action) + (r.target ? " · " + esc(r.target) : "") + '</span>' +
        '<span class="mini-time">' + esc(String(r.at || "").slice(11)) + '</span>' +
        '</div>';
    }).join("") : '<div class="empty">暂无操作</div>';
  }

  /* ============================================================
     二、容器
     ============================================================ */
  function checkKey(c) { return (c.repo || "") + ":" + (c.tag || ""); }
  function checkStatusOf(c) { return state.checkMap[checkKey(c)] || "unknown"; }
  function pinStatusOf(c) { return state.pinMap[checkKey(c)] || ""; }
  function pinnedContainers() {
    return sortedContainers().filter(function (c) { return pinStatusOf(c) === "ahead"; });
  }
  function updatableContainers() {
    return sortedContainers().filter(function (c) { return checkStatusOf(c) === "newer"; });
  }

  // 可更新的排最前，其次运行中的，最后是还没查到状态的；同档按名字排，顺序才稳定
  function sortedContainers() {
    var rank = { newer: 0, running: 1 };
    function r(c) { var v = rank[checkStatusOf(c)]; return v == null ? 2 : v; }
    return state.containers.slice().sort(function (a, b) {
      var d = r(a) - r(b);
      if (d) return d;
      return String(a.name).localeCompare(String(b.name));
    });
  }

  function renderUpdBar() {
    var total = state.containers.length;
    var newer = 0, latest = 0, failed = 0, pinned = 0;
    state.containers.forEach(function (c) {
      var k = checkStatusOf(c);
      if (k === "newer") newer++;
      else if (k === "latest") latest++;
      else if (k === "failed") failed++;
      if (pinStatusOf(c) === "ahead") pinned++;
    });
    var stat = $("updStat"), all = $("btnUpdateAll"), pin = $("btnPinToLatest");
    if (!stat) return;
    function showPin() {
      if (!pin) return;
      pin.textContent = "切到 latest（" + pinned + " 个）";
      pin.classList.remove("hidden");
    }
    function hidePin() { if (pin) pin.classList.add("hidden"); }

    if (state.detecting) {
      stat.className = "upd-stat busy";
      stat.textContent = "正在检测上游版本…";
      all.classList.add("hidden");
      hidePin();
      return;
    }
    if (!total) {
      stat.className = "upd-stat";
      stat.textContent = "没有容器";
      all.classList.add("hidden");
      hidePin();
      return;
    }
    if (newer) {
      stat.className = "upd-stat newer";
      stat.textContent = "⬆ 有 " + newer + " 个容器可以更新" +
        (failed ? "（另 " + failed + " 个查询失败）" : "") +
        (pinned ? "；另有 " + pinned + " 个锁定在固定版本" : "");
      all.textContent = "更新这 " + newer + " 个";
      all.classList.remove("hidden");
      if (pinned) showPin(); else hidePin();
      return;
    }
    all.classList.add("hidden");
    if (pinned) {
      stat.className = "upd-stat pinned";
      stat.textContent = "⚑ 另有 " + pinned + " 个容器锁定在固定版本 tag，" +
        "其 latest 已发布更新（锁定状态下不会自动算作「可更新」）";
      showPin();
      return;
    }
    hidePin();
    if (state.checkTs && latest === total) {
      stat.className = "upd-stat ok";
      stat.textContent = "✓ 全部 " + total + " 个容器都已是最新";
      return;
    }
    stat.className = "upd-stat";
    stat.textContent = state.checkTs
      ? ("未发现新版本" + (total - latest ? "（" + (total - latest) + " 个未确认）" : ""))
      : "尚未检测上游版本，点右侧「检测版本」看看有没有新版";
  }

  function setDetecting(on) {
    state.detecting = !!on;
    var b1 = $("btnCheck"), b2 = $("btnCheckInline");
    if (b1) { b1.disabled = !!on; b1.textContent = on ? "检测中…" : "检查更新"; }
    if (b2) { b2.disabled = !!on; b2.textContent = on ? "检测中…" : "检测版本"; }
    renderUpdBar();
  }

  function maybeAutoCheck() {
    if (!AUTO_CHECK_MS || !state.routerReady) return;
    if (state.busy || state.source || state.detecting) return;
    var now = Date.now() / 1000;
    if (state.checkTs && (now - state.checkTs) < AUTO_CHECK_MS / 1000) return;
    if (state.autoCheckedAt && (now - state.autoCheckedAt) < AUTO_CHECK_MS / 1000) return;
    state.autoCheckedAt = now;
    setTimeout(function () {
      if (state.busy || state.source || state.detecting) return;
      startCheck(true);
    }, 700);
  }

  function pruneSelection() {
    var alive = {};
    state.containers.forEach(function (c) { alive[c.name] = true; });
    Object.keys(state.selected).forEach(function (k) {
      if (!alive[k]) delete state.selected[k];
    });
  }

  function visibleContainers() {
    var kw = (state.ctSearch || "").trim().toLowerCase();
    var f = state.ctFilter || "all";
    return sortedContainers().filter(function (c) {
      if (kw) {
        var hay = (c.name + " " + (c.repo || "") + ":" + (c.tag || "")).toLowerCase();
        if (hay.indexOf(kw) < 0) return false;
      }
      if (f === "running") return c.state === "running";
      if (f === "stopped") return c.state !== "running";
      if (f === "newer") return checkStatusOf(c) === "newer";
      return true;
    });
  }

  function renderContainers() {
    pruneSelection();
    var tb = $("ctBody");
    var html;
    if (!state.containers.length) {
      html = '<tr><td colspan="8" class="empty">没有容器（或 Docker 服务未启动）</td></tr>';
    } else {
      var list = visibleContainers();
      if (!list.length) {
        html = '<tr><td colspan="8" class="empty">没有匹配的容器</td></tr>';
      } else {
        html = list.map(function (c) {
          var st = checkStatusOf(c);
          var badge = { newer: ["newer", "⬆ 有新版本"], latest: ["latest", "已是最新"],
                        failed: ["failed", "查询失败"], running: ["running", "更新中"] }[st]
                      || ["", "未检测"];
          var pin = pinStatusOf(c) === "ahead";
          var running = String(c.state) === "running";
          var led = running ? "up" : "down";
          var stats = state.statMap[c.name] || {};
          var mem = stats.mem ? fmtBytes(stats.mem) : "—";
          var cpu = stats.cpu || "—";
          return '' +
            '<tr class="' + (st === "newer" ? "need-upd" : (pin ? "row-pinned" : "")) +
              '" data-name="' + esc(c.name) + '">' +
              '<td class="cb" data-label=""><input type="checkbox" class="ct-pick" data-name="' + esc(c.name) + '"' +
                (state.selected[c.name] ? " checked" : "") + '></td>' +
              '<td data-label="容器"><div class="ct-name">' + esc(c.name) + '</div>' +
                '<div class="ct-sub">' + esc(c.ipaddr || "-") + '</div></td>' +
              '<td data-label="镜像"><div class="mono">' + esc(c.repo) + '</div>' +
                '<div class="ct-sub">' + esc(c.tag) + (c.local_pulled ? " · " + esc(c.local_pulled) : "") + '</div></td>' +
              '<td data-label="状态"><span class="dot-led"><i class="led ' + led + '"></i>' +
                esc(shortStatus(c.status)) + '</span></td>' +
              '<td class="mono" data-label="CPU">' + esc(cpu) + '</td>' +
              '<td class="mono" data-label="内存">' + esc(mem) + '</td>' +
              '<td data-label="上游"><span class="badge ' + badge[0] + '" title="' +
                esc(state.checkAt ? ("检测于 " + state.checkAt) : "尚未检测") + '">' +
                badge[1] + '</span>' +
                (pin ? ' <span class="badge pin" title="该容器锁定在固定版本 tag（' + esc(c.tag) +
                       '），不跟随 latest；上游 latest 已有更新。点「切到 latest」可改为跟随 latest">⚑ 锁定</span>'
                     : '') + '</td>' +
              '<td class="r ct-acts">' +
                (running
                  ? '<button class="btn tiny ghost ct-stop" data-name="' + esc(c.name) + '" title="停止容器">停止</button> '
                  : '<button class="btn tiny ghost ct-start" data-name="' + esc(c.name) + '" title="启动容器">启动</button> ') +
                '<button class="btn tiny ghost ct-restart" data-name="' + esc(c.name) + '" title="重启容器">重启</button> ' +
                '<button class="btn tiny ghost ct-logs" data-name="' + esc(c.name) + '" title="查看日志">日志</button> ' +
                '<button class="btn tiny ghost ct-info" data-name="' + esc(c.name) + '" title="详情与配置">详情</button> ' +
                '<button class="btn tiny ghost ct-tag" data-name="' + esc(c.name) + '" ' +
                  'data-repo="' + esc(c.repo) + '" data-tag="' + esc(c.tag) + '">版本</button> ' +
                (pin ? '<button class="btn tiny primary ct-pin" data-name="' + esc(c.name) + '">切到 latest</button> ' : '') +
                '<button class="btn tiny' + (st === "newer" ? " primary" : "") +
                  ' ct-upd" data-name="' + esc(c.name) + '">更新</button> ' +
                '<button class="btn tiny ghost danger-text ct-del" data-name="' + esc(c.name) + '" title="删除容器">删除</button>' +
              '</td>' +
            '</tr>';
        }).join("");
      }
    }
    if (html === state.ctSig) return;
    state.ctSig = html;
    tb.innerHTML = html;
    bindRows();
    updateSelCount();
    renderUpdBar();
  }

  function shortStatus(s) {
    s = String(s || "");
    if (/^Up /.test(s)) return s.replace(/\(healthy\)/, "· 健康");
    return s || "未知";
  }

  function bindRows() {
    Array.prototype.forEach.call(document.querySelectorAll(".ct-pick"), function (cb) {
      cb.addEventListener("change", function () {
        state.selected[cb.dataset.name] = cb.checked;
        updateSelCount();
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".ct-upd"), function (b) {
      b.addEventListener("click", function () { startUpdate([b.dataset.name]); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".ct-tag"), function (b) {
      b.addEventListener("click", function () { openTagModal(b.dataset.name, b.dataset.repo, b.dataset.tag); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".ct-pin"), function (b) {
      b.addEventListener("click", function () { startUpdate([b.dataset.name], "latest"); });
    });
    // ---- 管理动作 ----
    bindAction(".ct-start", "start", "启动");
    bindAction(".ct-stop", "stop", "停止");
    bindAction(".ct-restart", "restart", "重启");
    Array.prototype.forEach.call(document.querySelectorAll(".ct-logs"), function (b) {
      b.addEventListener("click", function () { openDrawer(b.dataset.name, "logs"); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".ct-info"), function (b) {
      b.addEventListener("click", function () { openDrawer(b.dataset.name, "info"); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".ct-del"), function (b) {
      b.addEventListener("click", function () { deleteContainer(b.dataset.name); });
    });
  }

  function bindAction(sel, action, label) {
    Array.prototype.forEach.call(document.querySelectorAll(sel), function (b) {
      b.addEventListener("click", function () { containerAction(b.dataset.name, action, label); });
    });
  }

  function updateSelCount() {
    var n = Object.keys(state.selected).filter(function (k) { return state.selected[k]; }).length;
    var btn = $("btnUpdateSelected");
    btn.disabled = n === 0;
    btn.textContent = n ? ("更新选中 (" + n + ")") : "更新选中";
  }

  /* ---------------- 容器管理动作 ---------------- */
  function containerAction(name, action, label) {
    if (state.busy || state.source) { toast("已有任务在执行，等它跑完再说", "warn"); return; }
    var verb = label || action;
    var hint = action === "restart" ? "重启会让该容器的服务中断几秒。" :
               action === "stop" ? "停止后该容器提供的服务会立刻不可用。" : "";
    confirmBox({
      title: verb + "容器",
      html: '<p class="cfm-lead">确定' + esc(verb) + ' <b>' + esc(name) + '</b>？</p>' +
            (hint ? '<p class="cfm-note">' + esc(hint) + '</p>' : ''),
      okText: verb, danger: action !== "start"
    }).then(function (yes) {
      if (!yes) return;
      toast("正在" + verb + " " + name + "…", "info", 2000);
      api("/api/containers/action", { method: "POST", body: { name: name, action: action } })
        .then(function (j) {
          var r = j.data || {};
          toast(r.detail || (verb + "完成"), r.ok ? "ok" : "warn", 5000);
          refreshAfterChange();
        })
        .catch(function (e) { toast(verb + "失败：" + e.message, "error", 6000); });
    });
  }

  function deleteContainer(name) {
    if (state.busy || state.source) { toast("已有任务在执行，等它跑完再说", "warn"); return; }
    var c = state.containers.filter(function (x) { return x.name === name; })[0] || {};
    confirmBox({
      title: "删除容器",
      html: '<p class="cfm-lead">确定删除容器 <b>' + esc(name) + '</b>？</p>' +
            '<ul class="cfm-list"><li><span class="mono">' +
            esc((c.repo || "") + ":" + (c.tag || "")) + '</span></li></ul>' +
            '<p class="cfm-note">容器会被<b>彻底删除</b>（挂载的数据目录保留）。' +
            '如果只是想停掉它，用「停止」即可。</p>',
      okText: "删除", danger: true
    }).then(function (yes) {
      if (!yes) return;
      api("/api/containers/action", { method: "POST", body: { name: name, action: "delete" } })
        .then(function (j) {
          var r = j.data || {};
          toast(r.detail || "已删除", r.ok ? "ok" : "warn", 5000);
          if (state.drawer && state.drawer.name === name) closeDrawer();
          refreshAfterChange();
        })
        .catch(function (e) { toast("删除失败：" + e.message, "error", 6000); });
    });
  }

  // 任何写操作之后：立刻重拉一次，别让界面停在旧状态
  function refreshAfterChange() {
    loadOverview({ silent: true });
    if (state.page === "images") loadImages();
    if (state.page === "networks") loadNetworks();
    if (state.page === "compose") loadCompose();
    if (state.page === "audit") loadAudit();
    if (state.page === "overview") loadAudit(true);
  }

  /* ---------------- 详情抽屉 ---------------- */
  function openDrawer(name, tab) {
    state.drawer = { name: name, tab: tab || "info", timer: null };
    $("drawer").classList.remove("hidden");
    $("drawerTitle").textContent = name;
    $("drawerSub").textContent = "容器详情";
    setDrawerTab(state.drawer.tab);
    loadDrawer();
    // 实时资源：打开时每 5 秒刷一次，关掉就停
    state.drawer.timer = setInterval(function () {
      if (!state.drawer) return;
      if (state.drawer.tab === "info") loadDrawer(true);
    }, 5000);
  }

  function closeDrawer() {
    if (state.drawer && state.drawer.timer) clearInterval(state.drawer.timer);
    state.drawer = null;
    $("drawer").classList.add("hidden");
  }

  function setDrawerTab(tab) {
    if (!state.drawer) return;
    state.drawer.tab = tab;
    Array.prototype.forEach.call(document.querySelectorAll("#drawerTabs .tab"), function (b) {
      b.classList.toggle("on", b.getAttribute("data-tab") === tab);
    });
    loadDrawer();
  }

  function loadDrawer(quiet) {
    var d = state.drawer;
    if (!d) return;
    if (!quiet) $("drawerBody").innerHTML = '<div class="empty">加载中…</div>';
    if (d.tab === "logs") {
      api("/api/containers/" + encodeURIComponent(d.name) + "/logs?minutes=30")
        .then(function (j) {
          var lines = j.data || [];
          $("drawerBody").innerHTML = lines.length
            ? '<pre class="logs">' + esc(lines.join("\n")) + '</pre>'
            : '<div class="empty">最近 30 分钟没有日志输出</div>';
        })
        .catch(function (e) {
          $("drawerBody").innerHTML = '<div class="empty">日志读取失败：' + esc(e.message) + '</div>';
        });
      return;
    }
    api("/api/containers/" + encodeURIComponent(d.name))
      .then(function (j) {
        var v = j.data || {};
        var c = v.container || {};
        var st = v.stats || {};
        var insp = v.inspect || {};
        $("drawerSub").textContent = imgLabel(c) + " · " + (c.status || "");
        if (d.tab === "config") {
          $("drawerBody").innerHTML =
            '<div class="kv">' +
            kv("镜像", imgLabel(c)) +
            kv("固定 IP", c.ipaddr || "自动") +
            kv("网络", c.interface || "-") +
            kv("挂载", c.mounts || "无") +
            kv("环境变量", c.env || "无") +
            kv("启动命令", c.cmd || "无") +
            kv("自启", String(c.auto_start) === "1" ? "是" : "否") +
            kv("容器 ID", c.id || "-") +
            '</div>' +
            (insp && Object.keys(insp).length
              ? '<div class="kv-sub">inspect 摘要</div><pre class="logs">' +
                esc(JSON.stringify(insp, null, 2)) + '</pre>'
              : "");
          return;
        }
        $("drawerBody").innerHTML =
          '<div class="stat-inline">' +
            '<div><span class="muted">状态</span><b>' + esc(shortStatus(c.status)) + '</b></div>' +
            '<div><span class="muted">CPU</span><b>' + esc(st.cpu_used || "—") + '</b></div>' +
            '<div><span class="muted">内存</span><b>' + fmtBytes(st.memused) + '</b></div>' +
            '<div><span class="muted">下行</span><b>' + esc(fmtBytes(st.down_speed) === "—" ? "0 B/s" : fmtBytes(st.down_speed)) + '</b></div>' +
            '<div><span class="muted">上行</span><b>' + esc(fmtBytes(st.up_speed) === "—" ? "0 B/s" : fmtBytes(st.up_speed)) + '</b></div>' +
          '</div>' +
          '<div class="kv">' +
          kv("镜像", imgLabel(c)) +
          kv("IP", c.ipaddr || "-") +
          kv("挂载", c.mounts || "无") +
          kv("上游", checkStatusOf(c) === "newer" ? "有新版本"
             : checkStatusOf(c) === "latest" ? "已是最新" : "未检测") +
          '</div>' +
          '<div class="drawer-acts">' +
            '<button class="btn small ghost" data-drawer="logs">看日志</button>' +
            '<button class="btn small ghost" data-drawer="config">看配置</button>' +
          '</div>';
        Array.prototype.forEach.call(document.querySelectorAll("[data-drawer]"), function (b) {
          b.addEventListener("click", function () { setDrawerTab(b.getAttribute("data-drawer")); });
        });
      })
      .catch(function (e) {
        $("drawerBody").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + '</div>';
      });
  }

  // 详情接口的字段名以 repo/tag 为主，老数据只有 image，这里统一兜一层
  function imgLabel(c) {
    if (!c) return "";
    if (c.repo) return c.repo + ":" + (c.tag || "");
    return String(c.image || "");
  }

  function kv(k, v) {
    return '<div class="kv-row"><span>' + esc(k) + '</span><b class="mono">' + esc(v) + '</b></div>';
  }

  /* ============================================================
     三、执行面板 / SSE
     ============================================================ */
  function openExec(title, flow) {
    autoYieldToJob();
    state.flow = flow || STEP_ORDER.map(function (k) { return { key: k, state: "pending", message: "" }; });
    state.progress = { current: 0, total: 1 };
    $("execPanel").classList.remove("hidden");
    $("execTitle").textContent = title;
    $("execCounter").textContent = "";
    $("execBar").style.width = "0%";
    $("execSpinner").className = "spinner";
    state.logs = [];
    $("log").innerHTML = "";
    renderFlow();
  }

  function renderFlow() {
    var ol = $("flow");
    ol.innerHTML = state.flow.map(function (s, i) {
      var st = s.state || "pending";
      var inner = st === "running"
        ? '<span class="mini-spin"></span>'
        : (st === "done" ? "✓" : (st === "failed" ? "✕" : (st === "skipped" ? "–" : (i + 1))));
      return '' +
        '<li class="step ' + st + '" data-key="' + esc(s.key) + '">' +
          '<div class="fdot">' + inner + '</div>' +
          '<div class="ftxt">' +
            '<div class="fname">' + esc(s.name || s.key) + '</div>' +
            '<div class="fdesc">' + esc(s.desc || "") + '</div>' +
            (s.message ? '<div class="fmsg">' + esc(s.message) + '</div>' : '') +
          '</div>' +
        '</li>';
    }).join("");
  }

  function setStep(key, st, message) {
    var found = null;
    state.flow.forEach(function (s) { if (s.key === key) found = s; });
    if (!found) return;
    found.state = st;
    if (message != null) found.message = message;
    renderFlow();
    var done = state.flow.filter(function (s) {
      return s.state === "done" || s.state === "skipped";
    }).length;
    var frac = done / Math.max(state.flow.length, 1);
    var p = state.progress;
    var pct = p.total > 1
      ? Math.round(((p.current - 1) + frac) / p.total * 100)
      : Math.round(frac * 100);
    $("execBar").style.width = Math.min(pct, 100) + "%";
  }

  function logVisible(ev) {
    return showDebugOn() || (ev.level || "info") !== "debug";
  }

  function showDebugOn() {
    var el = $("showDebug");
    return !!(el && el.checked);
  }

  function logLineNode(ev) {
    var lv = ev.level || "info";
    var cls = lv === "error" ? "lv-error" : lv === "warn" ? "lv-warn"
            : lv === "debug" ? "lv-debug" : lv === "plain" ? "lv-plain" : "lv-info";
    var line = document.createElement("span");
    line.className = "ln";
    var t = document.createElement("span");
    t.className = "t";
    t.textContent = fmtTime(ev.ts).slice(6);
    var m = document.createElement("span");
    m.className = cls;
    m.textContent = ev.message;
    line.appendChild(t);
    line.appendChild(m);
    return line;
  }

  function appendLog(ev) {
    state.logs.push(ev);
    var trimmed = false;
    while (state.logs.length > 800) { state.logs.shift(); trimmed = true; }
    var log = $("log");
    if (trimmed) { renderLog(); return; }
    if (!logVisible(ev)) return;
    log.appendChild(logLineNode(ev));
    if ($("autoScroll").checked) log.scrollTop = log.scrollHeight;
  }

  function renderLog() {
    var log = $("log");
    log.innerHTML = "";
    state.logs.filter(logVisible).forEach(function (ev) { log.appendChild(logLineNode(ev)); });
    if ($("autoScroll").checked) log.scrollTop = log.scrollHeight;
  }

  /* ---------------- 自动刷新 ---------------- */
  function autoLabelText() {
    if (!state.auto) return "已暂停";
    if (state.busy || state.source) return "执行中…";
    var left = state.nextAt ? Math.round((state.nextAt - Date.now()) / 1000) : AUTO_MS / 1000;
    return "自动刷新 " + (left > 0 ? left : 0) + "s";
  }

  function paintAuto() {
    var btn = $("btnAuto");
    if (!btn) return;
    btn.classList.toggle("off", !state.auto);
    $("autoLabel").textContent = autoLabelText();
    btn.title = state.auto
      ? "每 " + (AUTO_MS / 1000) + " 秒自动刷新一次（执行任务时会自动让路）；点一下暂停"
      : "自动刷新已暂停，点一下恢复";
  }

  function autoClear() {
    if (state.autoTimer) { clearTimeout(state.autoTimer); state.autoTimer = null; }
    state.nextAt = 0;
  }

  function scheduleAuto(delay) {
    autoClear();
    if (!state.auto || state.busy || state.source) { paintAuto(); return; }
    var wait = delay || AUTO_MS;
    state.nextAt = Date.now() + wait;
    paintAuto();
    state.autoTimer = setTimeout(function () {
      state.autoTimer = null;
      if (!state.auto || state.busy || state.source) { paintAuto(); return; }
      if (document.hidden) { scheduleAuto(); return; }
      loadOverview({ silent: true }).then(function () {
        loadStats();
        if (state.page !== "overview" && state.page !== "containers") refreshPage(state.page, true);
        scheduleAuto();
      });
    }, wait);
  }

  function setAuto(on) {
    state.auto = !!on;
    try { localStorage.setItem("ikuai_auto_refresh", state.auto ? "1" : "0"); } catch (e) {}
    scheduleAuto();
  }

  function autoYieldToJob() {
    state.busy = true;
    autoClear();
    paintAuto();
  }

  function connect(jobId, silent) {
    if (state.source) { state.source.close(); state.source = null; }
    var es = new EventSource("/api/jobs/" + jobId + "/stream");
    state.source = es;
    state.jobKind = "";

    es.addEventListener("snapshot", function (e) {
      var d = JSON.parse(e.data);
      state.jobKind = d.kind || "";
      if (d.targets) state.jobTargets = d.targets.slice();
      if (silent) return;
      if (d.steps && d.steps.length) { state.flow = d.steps; renderFlow(); }
      if (d.targets && d.targets.length && d.kind === "update") {
        state.progress.total = d.targets.length;
      }
      var who = (d.targets || []).join("、");
      $("execCounter").textContent = (d.kind === "update" ? "更新 " : "检测 ") +
        (who || "全部容器");
    });

    es.addEventListener("progress", function (e) {
      var d = JSON.parse(e.data);
      if (silent) return;
      state.progress.current = d.current;
      state.progress.total = d.total;
      $("execCounter").textContent = "[" + d.current + "/" + d.total + "] " + d.container;
      state.flow = state.flow.map(function (s) {
        return { key: s.key, name: s.name, desc: s.desc, state: "pending", message: "" };
      });
      renderFlow();
    });

    es.addEventListener("step", function (e) {
      var d = JSON.parse(e.data);
      if (!silent) setStep(d.key, d.state, d.message);
    });

    es.addEventListener("log", function (e) { if (!silent) appendLog(JSON.parse(e.data)); });

    es.addEventListener("check_result", function (e) {
      var d = JSON.parse(e.data);
      (d.results || []).forEach(function (r) {
        state.checkMap[r.image] = r.status;
        if (r.pinned) state.pinMap[r.image] = r.pinned;
        else delete state.pinMap[r.image];
      });
      renderContainers();
    });

    es.addEventListener("done", function (e) {
      var d = JSON.parse(e.data);
      var kind = state.jobKind || "check";
      if (!silent) {
        $("execSpinner").className = "spinner " + (d.ok ? "done" : "failed");
        $("execTitle").textContent = d.ok ? "执行完成" : "执行失败";
        if (d.ok) $("execBar").style.width = "100%";
        appendLog({ ts: Date.now() / 1000, level: d.ok ? "ok" : "error",
                    message: (d.ok ? "✅ " : "❌ ") + (d.message || "") });
      }
      es.close();
      state.source = null;
      state.busy = false;
      setDetecting(false);
      loadHistory();
      notifyJobResult(kind, d);
      setTimeout(function () {
        loadOverview({ silent: true }).then(function () { scheduleAuto(); });
      }, 1200);
    });

    es.onerror = function () {
      if (es.readyState === 2) {
        state.source = null;
        state.busy = false;
        setDetecting(false);
        scheduleAuto();
      }
    };
  }

  function notifyJobResult(kind, d) {
    if (kind === "update") {
      var who = (state.jobTargets || []).join("、");
      toast(d.ok ? ("更新完成：" + (d.message || who))
                 : ("更新失败：" + (d.message || "请看执行日志")),
            d.ok ? "ok" : "error", 7000);
      return;
    }
    var n = updatableContainers().length;
    if (!d.ok) { toast("版本检测失败：" + (d.message || "请看执行日志"), "error", 7000); return; }
    toast(n ? ("版本检测完成：有 " + n + " 个容器可以更新")
            : ("版本检测完成：全部 " + state.containers.length + " 个都已是最新"),
          n ? "warn" : "ok", 6000);
  }

  /* ---------------- 触发更新 / 检测 ---------------- */
  function startUpdate(names, tag) {
    if (!names || !names.length) return;
    if (state.busy || state.source) { toast("已有任务在执行，等它跑完再说", "warn"); return; }
    tag = tag || "";
    confirmUpdate(names, tag).then(function (yes) { if (yes) runUpdate(names, tag); });
  }

  function confirmUpdate(names, tag) {
    var map = {};
    state.containers.forEach(function (c) { map[c.name] = c; });
    var dry = !!$("dryRun").checked;
    var rows = names.map(function (n) {
      var c = map[n];
      var st = c ? checkStatusOf(c) : "unknown";
      var isPin = c ? pinStatusOf(c) === "ahead" : false;
      var tail = isPin ? '<span class="badge pin">锁定在 ' + esc(c.tag) + '</span>'
               : st === "newer" ? '<span class="badge newer">有新版本</span>'
               : st === "latest" ? '<span class="badge latest">已是最新</span>' : "";
      var img = c ? ((c.repo || "") + ":" + (c.tag || "")) : "";
      return '<li><span class="cfm-nm">' + esc(n) + '</span>' +
             '<span class="mono cfm-img">' + esc(img) + '</span>' + tail + '</li>';
    }).join("");
    var tagNote = !tag ? ""
      : tag === "latest"
        ? '，并<b>切换为跟随 latest</b>（此后不再锁定在固定版本，上游一更新就能检测到）'
        : '，版本固定为 <b>' + esc(tag) + '</b>（此后锁定在该版本，不跟随 latest）';
    return confirmBox({
      title: dry ? "演练一次更新" : (tag === "latest" ? "确认切换为跟随 latest" : "确认更新容器"),
      html: '<p class="cfm-lead">将' + (dry ? "演练" : "") + '更新以下 <b>' + names.length +
            '</b> 个容器' + tagNote + '：</p>' +
            '<ul class="cfm-list">' + rows + '</ul>' +
            '<p class="cfm-note">' + (dry
              ? "当前是<b>演练模式</b>：只走一遍流程并打印将要发出的请求，<b>不会真的重启任何容器</b>。"
              : "过程中这些容器会各<b>重启一次</b>，服务短暂中断（几秒到几十秒，视镜像大小而定）。") +
            '</p>',
      okText: dry ? "开始演练" : (tag === "latest" ? "切到 latest" : "开始更新"),
      danger: !dry
    });
  }

  function runUpdate(names, tag) {
    openExec("正在更新容器", null);
    state.jobTargets = names.slice();
    appendLog({ ts: Date.now() / 1000, level: "info",
                message: "提交更新请求：" + names.join("、") +
                         (tag ? (" → " + tag) : "") +
                         "（演练：" + ($("dryRun").checked ? "开" : "关") + "）" });
    var body = { containers: names, dry_run: $("dryRun").checked };
    if (tag) body.tag = tag;
    api("/api/update", { method: "POST", body: body }).then(function (j) {
      connect(j.job_id, false);
    }).catch(function (e) {
      appendLog({ ts: Date.now() / 1000, level: "error", message: "提交失败：" + e.message });
      $("execSpinner").className = "spinner failed";
      $("execTitle").textContent = "提交失败";
      state.busy = false;
      scheduleAuto();
    });
  }

  function startCheck(silent) {
    silent = !!silent;
    if (state.busy || state.source || state.detecting) {
      if (!silent) toast("已有任务在执行，等它跑完再说", "warn");
      return;
    }
    if (silent) {
      autoYieldToJob();
      setDetecting(true);
    } else {
      openExec("正在检测上游版本", null);
      appendLog({ ts: Date.now() / 1000, level: "info", message: "开始检测所有容器的上游镜像版本…" });
    }
    api("/api/check", { method: "POST", body: {} }).then(function (j) {
      connect(j.job_id, silent);
    }).catch(function (e) {
      state.busy = false;
      setDetecting(false);
      scheduleAuto();
      if (silent) { toast("版本检测失败：" + e.message, "error", 6000); return; }
      appendLog({ ts: Date.now() / 1000, level: "error", message: "提交失败：" + e.message });
      $("execSpinner").className = "spinner failed";
      $("execTitle").textContent = "提交失败";
    });
  }

  /* ---------------- 版本弹窗 ---------------- */
  function openTagModal(name, repo, tag) {
    state.target = { name: name, repo: repo, tag: tag, picked: "" };
    $("tagTitle").textContent = name + " · 选择版本";
    $("tagManual").value = "";
    $("tagHint").textContent = "";
    $("tagBody").innerHTML = '<div class="muted">正在查询 ' + esc(repo) + ' 的可用版本…</div>';
    $("tagModal").classList.remove("hidden");
    api("/api/tags?image=" + encodeURIComponent(repo)).then(function (j) {
      var tags = j.data || [];
      var src = j.source || "";
      if (!tags.length) {
        $("tagBody").innerHTML = '<div class="muted">没查到版本列表 —— ' +
          '可以在上面手填 tag，比如 latest 或具体的版本号。</div>';
        $("tagHint").textContent = "列表没拿到，手填的 tag 会原样使用";
        return;
      }
      $("tagHint").textContent = (src === "registry" ? "列表来自加速源（爱快自己没给出）"
                                    : src === "ikuai" ? "列表来自爱快" : "") +
        (src === "registry" || src === "ikuai" ? " · 共 " + tags.length + " 个" : "");
      $("tagBody").innerHTML = '<div class="muted" style="margin-bottom:10px">当前：<b>' +
        esc(tag) + '</b> · 点击选择</div><div class="tag-grid">' +
        tags.map(function (t) {
          return '<span class="tag-chip" data-tag="' + esc(t) + '">' + esc(t) + '</span>';
        }).join("") + '</div>';
      Array.prototype.forEach.call(document.querySelectorAll(".tag-chip"), function (c) {
        c.addEventListener("click", function () {
          var on = c.classList.contains("on");
          Array.prototype.forEach.call(document.querySelectorAll(".tag-chip"),
            function (x) { x.classList.remove("on"); });
          if (on) {
            state.target.picked = "";
            return;
          }
          c.classList.add("on");
          state.target.picked = c.dataset.tag;
          $("tagManual").value = "";
        });
      });
    }).catch(function (e) {
      $("tagBody").innerHTML = '<div class="muted">查询失败：' + esc(e.message) +
        ' —— 可以在上面手填 tag。</div>';
      $("tagHint").textContent = "手填的 tag 会原样使用";
    });
  }

  /* ============================================================
     四、镜像 / 网络 / 编排 / 审计
     ============================================================ */
  function loadImages() {
    api("/api/images").then(function (j) {
      state.images = j.data || [];
      renderImages();
    }).catch(function (e) { showBanner("errBanner", "镜像列表读取失败：" + e.message); });
  }

  function renderImages() {
    var list = state.images || [];
    var badge = $("imgCount");
    if (badge) badge.textContent = list.length;
    var tb = $("imgBody");
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="6" class="empty">本地没有镜像</td></tr>';
      return;
    }
    var rows = list.slice().sort(function (a, b) {
      return String(a.name).localeCompare(String(b.name));
    });
    tb.innerHTML = rows.map(function (img) {
      var users = asList(img.containers).map(function (c) {
        return (c && c.name) || c;
      });
      var tags = String(img.tag || "").split(",").map(function (t) { return t.trim(); })
        .filter(Boolean);
      return '<tr>' +
        '<td data-label="镜像"><div class="mono">' + esc(img.name) + '</div>' +
          '<div class="ct-sub">' + esc(String(img.id || "").slice(0, 19)) + '</div></td>' +
        '<td data-label="版本">' + tags.map(function (t) {
          return '<span class="chip">' + esc(t) + '</span>';
        }).join(" ") + '</td>' +
        '<td class="mono" data-label="大小">' + fmtBytes(img.size) + '</td>' +
        '<td class="mono" data-label="时间">' + esc(img.install ? fmtTime(img.install) : fmtTime(img.created)) + '</td>' +
        '<td data-label="使用">' + (users.length
          ? users.map(function (n) { return '<span class="chip">' + esc(n) + '</span>'; }).join(" ")
          : '<span class="muted">未使用</span>') + '</td>' +
        '<td class="r" data-label="">' +
          '<button class="btn tiny ghost img-pull" data-repo="' + esc(img.name) + '" data-tag="' +
            esc(pickTag(tags)) + '">拉取最新</button> ' +
          '<button class="btn tiny ghost danger-text img-del" data-repo="' + esc(img.name) + '" data-tag="' +
            esc(tags[0] || "latest") + '"' + (users.length ? ' disabled title="被容器占用，不能删"' : '') +
            '>删除</button>' +
        '</td>' +
      '</tr>';
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll(".img-pull"), function (b) {
      b.addEventListener("click", function () { pullImage(b.dataset.repo, b.dataset.tag); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".img-del"), function (b) {
      b.addEventListener("click", function () { deleteImage(b.dataset.repo, b.dataset.tag); });
    });
  }

  // 拉取时优先 latest：镜像记录可能同时挂着 1.37.3,latest 两个 tag，
  // 拿第一个去拉会变成「重复拉同一个旧版本」。
  function pickTag(tags) {
    for (var i = 0; i < tags.length; i++) {
      if (tags[i] === "latest") return "latest";
    }
    return tags[0] || "latest";
  }

  function searchImages() {
    var kw = ($("imgSearch").value || "").trim();
    if (!kw) return;
    $("imgSearchResult").innerHTML = '<div class="empty">正在搜索…</div>';
    api("/api/images/search?keyword=" + encodeURIComponent(kw) + "&page=1").then(function (j) {
      var d = j.data || {};
      state.imgSearch = { keyword: kw, page: d.page || 1, total: d.total || 0, list: d.list || [] };
      var list = state.imgSearch.list;
      $("imgSearchResult").innerHTML = list.length
        ? '<div class="muted search-meta">共 ' + state.imgSearch.total + ' 个结果</div>' +
          list.map(function (it) {
            var full = (it.namespace ? it.namespace + "/" : "") + it.name;
            return '<div class="search-item">' +
              '<div class="si-main">' +
                '<div class="si-name mono">' + esc(full) + '</div>' +
                '<div class="si-desc">' + esc(it.description || "无描述") + '</div>' +
                '<div class="si-meta muted">★ ' + (it.star_count || 0) +
                  ' · 拉取 ' + (it.pull_count || 0) +
                  (it.is_official ? ' · 官方' : '') + '</div>' +
              '</div>' +
              '<div class="si-act">' +
                '<input class="input sm si-tag" data-repo="' + esc(full) + '" value="latest" title="要拉取的 tag">' +
                '<button class="btn small primary si-pull" data-repo="' + esc(full) + '">拉取</button>' +
              '</div>' +
            '</div>';
          }).join("")
        : '<div class="empty">没搜到「' + esc(kw) + '」</div>';
      Array.prototype.forEach.call(document.querySelectorAll(".si-pull"), function (b) {
        b.addEventListener("click", function () {
          var input = document.querySelector('.si-tag[data-repo="' + b.dataset.repo + '"]');
          pullImage(b.dataset.repo, (input && input.value) || "latest");
        });
      });
    }).catch(function (e) {
      $("imgSearchResult").innerHTML = '<div class="empty">搜索失败：' + esc(e.message) + '</div>';
    });
  }

  function pullImage(repo, tag) {
    confirmBox({
      title: "拉取镜像",
      html: '<p class="cfm-lead">将从爱快的加速源拉取 <b class="mono">' +
            esc(repo + ":" + tag) + '</b>。</p>' +
            '<p class="cfm-note">下载由爱快后台执行，大镜像可能要几分钟。下载完成后可在镜像列表里看到。</p>',
      okText: "开始拉取", danger: false
    }).then(function (yes) {
      if (!yes) return;
      toast("已提交拉取任务…", "info", 2500);
      api("/api/images/pull", { method: "POST", body: { repo: repo, tag: tag } })
        .then(function (j) {
          var r = j.data || {};
          toast(r.message || "已提交", r.state === "exists" ? "warn" : "ok", 6000);
          setTimeout(loadImages, 1500);
        })
        .catch(function (e) { toast("拉取失败：" + e.message, "error", 6000); });
    });
  }

  function deleteImage(repo, tag) {
    confirmBox({
      title: "删除镜像",
      html: '<p class="cfm-lead">确定删除 <b class="mono">' + esc(repo + ":" + tag) + '</b>？</p>' +
            '<p class="cfm-note">被容器占用的镜像会被拒绝删除 —— 这是刻意的保护：' +
            '删掉正在使用的记录可能让容器起不来。</p>',
      okText: "删除", danger: true
    }).then(function (yes) {
      if (!yes) return;
      api("/api/images/delete", { method: "POST", body: { repo: repo, tag: tag } })
        .then(function (j) { toast((j.data && j.data.detail) || "已删除", "ok", 5000); loadImages(); })
        .catch(function (e) { toast("删除失败：" + e.message, "error", 7000); });
    });
  }

  function loadNetworks() {
    api("/api/networks").then(function (j) {
      state.networks = j.data || [];
      renderNetworks();
    }).catch(function (e) { showBanner("errBanner", "网络列表读取失败：" + e.message); });
  }

  function renderNetworks() {
    var list = state.networks || [];
    $("netCount").textContent = list.length;
    var tb = $("netBody");
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">没有网络</td></tr>';
      return;
    }
    tb.innerHTML = list.map(function (n) {
      var cs = asList(n.containers);
      return '<tr>' +
        '<td data-label="名称"><div class="mono">' + esc(n.name) + '</div></td>' +
        '<td class="mono" data-label="子网">' + esc(n.subnet || "—") + '</td>' +
        '<td class="mono" data-label="网关">' + esc(n.gateway || "—") + '</td>' +
        '<td data-label="容器">' + (cs.length
          ? cs.map(function (c) { return '<span class="chip">' + esc(c) + '</span>'; }).join(" ")
          : '<span class="muted">空</span>') + '</td>' +
        '<td class="r" data-label="">' +
          '<button class="btn tiny ghost danger-text net-del" data-id="' + esc(n.id) +
            '" data-name="' + esc(n.name) + '"' + (cs.length ? ' disabled title="还有容器在用"' : '') +
            '>删除</button>' +
        '</td>' +
      '</tr>';
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll(".net-del"), function (b) {
      b.addEventListener("click", function () { deleteNetwork(b.dataset.id, b.dataset.name); });
    });
  }

  function deleteNetwork(id, name) {
    confirmBox({
      title: "删除网络",
      html: '<p class="cfm-lead">确定删除网络 <b>' + esc(name) + '</b>？</p>' +
            '<p class="cfm-note">网络里还有容器时不能删。</p>',
      okText: "删除", danger: true
    }).then(function (yes) {
      if (!yes) return;
      api("/api/networks", { method: "POST", body: { action: "delete", id: id } })
        .then(function () { toast("已删除网络 " + name, "ok"); loadNetworks(); })
        .catch(function (e) { toast("删除失败：" + e.message, "error", 6000); });
    });
  }

  function loadCompose() {
    api("/api/compose").then(function (j) {
      state.compose = j.data || [];
      renderCompose();
    }).catch(function (e) { showBanner("errBanner", "编排列表读取失败：" + e.message); });
  }

  function renderCompose() {
    var list = state.compose || [];
    $("cmpCount").textContent = list.length;
    var tb = $("cmpBody");
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="4" class="empty">没有 Compose 项目</td></tr>';
      return;
    }
    tb.innerHTML = list.map(function (p) {
      var id = String(p.id);
      var name = p.tagname || p.name || id;
      var stateTxt = p.status || p.state || "—";
      return '<tr>' +
        '<td data-label="项目"><div class="mono">' + esc(name) + '</div>' +
          '<div class="ct-sub">' + esc(id) + '</div></td>' +
        '<td data-label="状态"><span class="chip">' + esc(stateTxt) + '</span></td>' +
        '<td data-label="操作">' +
          '<button class="btn tiny ghost cmp-act" data-id="' + esc(id) + '" data-act="up">启动</button> ' +
          '<button class="btn tiny ghost cmp-act" data-id="' + esc(id) + '" data-act="down">停止</button> ' +
          '<button class="btn tiny ghost cmp-act" data-id="' + esc(id) + '" data-act="restart">重启</button>' +
        '</td>' +
        '<td class="r" data-label="">' +
          '<button class="btn tiny ghost cmp-logs" data-id="' + esc(id) + '" data-name="' + esc(name) + '">日志</button> ' +
          '<button class="btn tiny ghost danger-text cmp-act" data-id="' + esc(id) + '" data-act="clean">清理</button>' +
        '</td>' +
      '</tr>';
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll(".cmp-act"), function (b) {
      b.addEventListener("click", function () {
        composeAction(b.dataset.id, b.dataset.act);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".cmp-logs"), function (b) {
      b.addEventListener("click", function () { showComposeLogs(b.dataset.id, b.dataset.name); });
    });
  }

  function composeAction(id, act) {
    var label = { up: "启动", down: "停止", restart: "重启", clean: "清理" }[act] || act;
    var warn = act === "clean" ? "清理会移除该项目的容器，属于破坏性操作。" :
               act === "down" ? "停止后该项目下的服务会全部不可用。" : "";
    confirmBox({
      title: label + "编排项目",
      html: '<p class="cfm-lead">确定对项目 <b>' + esc(id) + '</b> 执行「' + esc(label) + '」？</p>' +
            (warn ? '<p class="cfm-note">' + esc(warn) + '</p>' : ""),
      okText: label, danger: act !== "up"
    }).then(function (yes) {
      if (!yes) return;
      api("/api/compose/action", { method: "POST", body: { id: id, action: act } })
        .then(function (j) { toast((j.data && j.data.detail) || (label + "已下发"), "ok"); loadCompose(); })
        .catch(function (e) { toast(label + "失败：" + e.message, "error", 6000); });
    });
  }

  function showComposeLogs(id, name) {
    $("logTitle").textContent = "编排日志 · " + name;
    $("logSub").textContent = "最近 30 分钟";
    $("logBody").innerHTML = '<div class="empty">加载中…</div>';
    $("logModal").classList.remove("hidden");
    api("/api/compose/" + encodeURIComponent(id) + "/logs").then(function (j) {
      var lines = j.data || [];
      $("logBody").innerHTML = lines.length
        ? lines.map(function (l) { return '<div class="lg"><span class="lg-m">' + esc(l) + '</span></div>'; }).join("")
        : '<div class="empty">没有日志</div>';
    }).catch(function (e) {
      $("logBody").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + '</div>';
    });
  }

  function loadAudit(forOverview) {
    api("/api/audit?limit=100").then(function (j) {
      state.audit = j.data || [];
      if (forOverview) { renderOverviewAudit(state.audit); return; }
      renderAudit();
    }).catch(function () {});
  }

  function renderAudit() {
    var list = state.audit || [];
    var tb = $("auditBody");
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">还没有管理操作记录</td></tr>';
      return;
    }
    tb.innerHTML = list.map(function (r) {
      return '<tr>' +
        '<td class="mono" data-label="时间">' + esc(r.at || "") + '</td>' +
        '<td data-label="动作"><span class="chip">' + esc(r.action || "") + '</span></td>' +
        '<td class="mono" data-label="目标">' + esc(r.target || "—") + '</td>' +
        '<td data-label="结果"><span class="badge ' + (r.ok ? "latest" : "failed") + '">' +
          (r.ok ? "成功" : "失败") + '</span></td>' +
        '<td data-label="说明">' + esc(r.detail || "") + '</td>' +
      '</tr>';
    }).join("");
  }

  /* ============================================================
     五、设置 / 定时任务 / 历史
     ============================================================ */
  function applySettings(s) {
    if (s && (s.schedules || s.pull_timeout != null)) state.settings = s;
    var t = (state.settings || {}).pull_timeout;
    if (t) {
      $("scTimeout").textContent = "下载超时 " + fmtDuration(t) +
        ($("dryRun").checked ? " · 演练中，不会真改容器" : "");
      if (!state.setInited) { $("setTimeout").value = t; state.setInited = true; }
    }
    renderSchedules();
  }

  function renderSchedules() {
    var list = (state.settings || {}).schedules || [];
    $("scCount").textContent = list.length;
    var tb = $("scBody");
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="8" class="empty">暂无定时任务，点右上角「新建定时任务」</td></tr>';
      return;
    }
    tb.innerHTML = list.map(function (s) {
      var modeBadge = s.mode === "update"
        ? '<span class="badge auto">自动更新</span>'
        : '<span class="badge gray">仅检测</span>';
      var targets = (s.mode === "check" && !(s.targets || []).length)
        ? '<span class="muted">全部容器</span>'
        : esc((s.targets || []).join("、"));
      return '<tr>' +
        '<td data-label="任务"><div class="ct-name">' + esc(s.name) + '</div>' +
          '<div class="ct-sub">' + esc(s.id) + '</div></td>' +
        '<td data-label="模式">' + modeBadge + '</td>' +
        '<td data-label="目标容器">' + targets + '</td>' +
        '<td data-label="频率">' + esc(s.frequency || "") + '</td>' +
        '<td class="mono" data-label="下次运行">' + esc(nextRunText(s)) + '</td>' +
        '<td data-label="上次结果">' + lastResultCell(s) + '</td>' +
        '<td data-label="启用"><label class="mini-switch"><input type="checkbox" class="sc-toggle" data-id="' +
          esc(s.id) + '"' + (s.enabled ? " checked" : "") + '></label></td>' +
        '<td class="r sc-acts">' +
          '<button class="btn tiny sc-run" data-id="' + esc(s.id) + '">立即执行</button> ' +
          '<button class="btn tiny ghost sc-edit" data-id="' + esc(s.id) + '">编辑</button> ' +
          '<button class="btn tiny ghost sc-del" data-id="' + esc(s.id) + '">删除</button>' +
        '</td>' +
      '</tr>';
    }).join("");
    bindScheduleRows();
  }

  function nextRunText(s) {
    if (!s.enabled) return "已停用";
    if (!s.next_run) return "—";
    var diff = s.next_run - Date.now() / 1000;
    return fmtTime(s.next_run) + " · " + (diff <= 5 ? "即将执行" : ("约 " + fmtDuration(diff) + "后"));
  }

  function lastResultCell(s) {
    var map = { ok: ["latest", "已完成"], fail: ["failed", "失败"],
                skip: ["gray", "已跳过"], running: ["running", "执行中"] };
    var m = map[s.last_status];
    if (!m) return '<span class="muted">—</span>';
    var t = s.last_run ? fmtTime(s.last_run).slice(5) : "";
    return '<span class="badge ' + m[0] + '" title="' + esc(s.last_message || "") + '">' +
           m[1] + '</span> <span class="muted">' + esc(t) + '</span>';
  }

  function bindScheduleRows() {
    Array.prototype.forEach.call(document.querySelectorAll(".sc-toggle"), function (cb) {
      cb.addEventListener("change", function () {
        var on = cb.checked;
        api("/api/schedules", { method: "POST",
          body: { action: "toggle", id: cb.dataset.id, enabled: on } })
          .then(function (j) { applySettings(j.data); })
          .catch(function (e) { cb.checked = !on; toast("操作失败：" + e.message, "error"); });
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sc-run"), function (b) {
      b.addEventListener("click", function () { runScheduleNow(b.dataset.id, b); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sc-edit"), function (b) {
      b.addEventListener("click", function () {
        var s = findSchedule(b.dataset.id);
        if (s) openSchedule(s);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sc-del"), function (b) {
      b.addEventListener("click", function () {
        var s = findSchedule(b.dataset.id);
        if (!s) return;
        confirmBox({
          title: "删除定时任务",
          html: '<p class="cfm-lead">确定删除定时任务 <b>' + esc(s.name) + '</b>？</p>' +
                '<p class="cfm-note">删除后不再自动执行；已经产生的执行历史会保留。</p>',
          okText: "删除", danger: true
        }).then(function (yes) {
          if (!yes) return;
          api("/api/schedules", { method: "POST", body: { action: "delete", id: s.id } })
            .then(function (j) { applySettings(j.data); toast("已删除「" + s.name + "」", "ok"); })
            .catch(function (e) { toast("删除失败：" + e.message, "error"); });
        });
      });
    });
  }

  function findSchedule(id) {
    var list = (state.settings || {}).schedules || [];
    for (var i = 0; i < list.length; i++) { if (list[i].id === id) return list[i]; }
    return null;
  }

  function runScheduleNow(id, btn) {
    if (btn) { btn.disabled = true; setTimeout(function () { btn.disabled = false; }, 2500); }
    api("/api/schedules", { method: "POST", body: { action: "run", id: id } })
      .then(function (j) {
        applySettings(j.data);
        if (j.data.job_id) {
          var s = findSchedule(id);
          openExec("定时任务 · " + ((s && s.name) || "立即执行"), null);
          connect(j.data.job_id);
        }
      })
      .catch(function (e) { toast("执行失败：" + e.message, "error"); });
  }

  function openSchedule(item) {
    state.scEdit = item || null;
    state.scMode = (item && item.mode) || "check";
    state.scFreq = (!item || (item.every || 0) > 0) ? "every" : "daily";
    $("scTitle").textContent = item ? ("编辑定时任务 · " + item.name) : "新建定时任务";
    $("scName").value = item ? item.name : "";
    $("scEvery").value = (item && item.every) ? item.every : 360;
    $("scAt").value = (item && item.at) || "03:30";
    $("scTag").value = (item && item.tag) || "";
    setSeg("scMode", "data-mode", state.scMode);
    setSeg("scFreq", "data-freq", state.scFreq);
    renderTargetPicker(item ? item.targets : []);
    syncFreqVisibility();
    warnSc("");
    $("scFoot").textContent = item ? "保存后重新排期" : "保存后立即开始按频率运行";
    $("scModal").classList.remove("hidden");
  }

  function setSeg(boxId, attr, value) {
    var box = $(boxId);
    if (!box) return;
    Array.prototype.forEach.call(box.querySelectorAll(".seg-item"), function (x) {
      x.classList.toggle("on", x.getAttribute(attr) === value);
    });
  }

  function bindSeg(boxId, onChange) {
    var box = $(boxId);
    if (!box) return;
    box.addEventListener("click", function (e) {
      var btn = e.target;
      while (btn && btn !== box && !btn.classList.contains("seg-item")) btn = btn.parentNode;
      if (!btn || btn === box) return;
      Array.prototype.forEach.call(box.querySelectorAll(".seg-item"), function (x) {
        x.classList.toggle("on", x === btn);
      });
      onChange(btn);
    });
  }

  function renderTargetPicker(selected) {
    var sel = {};
    (selected || []).forEach(function (n) { sel[n] = true; });
    var list = state.containers || [];
    $("scTargets").innerHTML = list.length
      ? list.map(function (c) {
          return '<label class="pick-item"><input type="checkbox" class="sc-target" value="' +
            esc(c.name) + '"' + (sel[c.name] ? " checked" : "") + '><span title="' +
            esc(c.repo + ":" + c.tag) + '">' + esc(c.name) + '</span></label>';
        }).join("")
      : '<div class="muted">没读到容器（先确认爱快连接正常）</div>';
    Array.prototype.forEach.call(document.querySelectorAll(".sc-target"), function (cb) {
      cb.addEventListener("change", syncTargetHint);
    });
    syncTargetHint();
  }

  function syncTargetHint() {
    var n = pickedTargets().length;
    $("scTargetHint").textContent = n ? ("已选 " + n + " 个") : "未选 = 全部容器";
  }

  function pickedTargets() {
    return Array.prototype.filter.call(document.querySelectorAll(".sc-target"), function (x) {
      return x.checked;
    }).map(function (x) { return x.value; });
  }

  function syncFreqVisibility() {
    $("freqEvery").classList.toggle("hidden", state.scFreq !== "every");
    $("scEveryQuick").classList.toggle("hidden", state.scFreq !== "every");
    $("freqDaily").classList.toggle("hidden", state.scFreq !== "daily");
  }

  function warnSc(msg) {
    var el = $("scWarn");
    if (!msg) { el.classList.add("hidden"); el.textContent = ""; return; }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function saveSchedule() {
    var mode = state.scMode;
    var targets = pickedTargets();
    if (mode === "update" && !targets.length) {
      warnSc("「检测到新版就更新」必须至少选择一个目标容器，否则不知道该更新谁。");
      return;
    }
    var every = 0;
    if (state.scFreq === "every") {
      every = parseInt($("scEvery").value, 10) || 0;
      if (every < 5) { warnSc("间隔不能小于 5 分钟，避免把加速源打爆。"); return; }
    }
    var payload = {
      id: state.scEdit ? state.scEdit.id : "",
      name: ($("scName").value || "").trim() ||
            (mode === "update" ? "自动更新任务" : "版本检测任务"),
      mode: mode,
      targets: targets,
      every: every,
      at: $("scAt").value || "03:30",
      tag: ($("scTag").value || "").trim(),
      enabled: state.scEdit ? state.scEdit.enabled : true
    };
    api("/api/schedules", { method: "POST", body: { action: "save", item: payload } })
      .then(function (j) { applySettings(j.data); $("scModal").classList.add("hidden"); })
      .catch(function (e) { warnSc("保存失败：" + e.message); });
  }

  function openSettings() {
    var s = state.settings || {};
    var t = s.pull_timeout || 900;
    $("setTimeout").value = t;
    $("setFoot").textContent = "可填 " + (s.pull_timeout_min || 60) +
      " ~ " + (s.pull_timeout_max || 7200) + " 秒";
    renderQuick($("setTimeoutQuick"),
      (s.suggested_timeouts || [600, 1800, 3600]).map(function (v) {
        return { v: v, label: fmtDuration(v) };
      }),
      function (v) { $("setTimeout").value = v; });
    state.setNotify = s.notify_mode || "fail";
    setSeg("setNotify", "data-notify", state.setNotify);
    renderNotifyHint(s);
    $("setMirrors").value = (state.mirrors || []).join("\n");
    $("setModal").classList.remove("hidden");
  }

  function renderNotifyHint(s) {
    var el = $("setNotifyHint");
    if (!el) return;
    var ch = (s && s.notify_channels) || [];
    if (ch.length) {
      el.innerHTML = "已配置：" + ch.map(function (c) { return esc(c.label); }).join(" · ") +
        "<br>定时任务凌晨跑完或挂掉都会推一条，不用一直开着页面看。";
    } else {
      el.innerHTML = "尚未配置通知渠道：在 compose 的 .env 里填 " +
        "<span class=\"mono\">NOTIFY_WEBHOOK_URL</span>（通用 webhook）或 " +
        "<span class=\"mono\">NOTIFY_PUSHPLUS_TOKEN</span>（微信），重启容器后生效。";
    }
  }

  function renderQuick(box, items, onPick) {
    if (!box) return;
    box.innerHTML = items.map(function (it) {
      return '<span class="quick" data-v="' + it.v + '">' + esc(it.label) + '</span>';
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll(".quick"), function (q) {
      q.addEventListener("click", function () { onPick(parseInt(q.dataset.v, 10)); });
    });
  }

  function saveSettings() {
    var v = parseInt($("setTimeout").value, 10);
    var mirrors = ($("setMirrors").value || "").split("\n")
      .map(function (x) { return x.trim(); })
      .filter(function (x) { return !!x; });
    var changedMirrors = mirrors.join(",") !== (state.mirrors || []).join(",");
    var chain = api("/api/settings", { method: "POST",
                   body: { pull_timeout: v, notify_mode: state.setNotify } });
    if (changedMirrors) {
      chain = chain.then(function () {
        return api("/api/server/mirrors", { method: "POST", body: { mirrors: mirrors } })
          .then(function (j) { state.mirrors = (j.data && j.data.mirrors) || mirrors; });
      });
    }
    chain.then(function (j) {
      if (j && j.data && j.data.schedules) applySettings(j.data);
      else applySettings(null);
      state.setInited = false;
      $("setModal").classList.add("hidden");
      toast(changedMirrors ? "设置已保存（含加速源）" : "设置已保存", "ok");
      if (changedMirrors) loadOverview({ silent: true });
    }).catch(function (e) { toast("保存失败：" + e.message, "error"); });
  }

  /* ---------------- 历史 ---------------- */
  function renderHistory(list) {
    var el = $("history");
    var rows = (list || []).slice();
    if (state.histFilter && state.histFilter !== "all") {
      rows = rows.filter(function (h) { return h.kind === state.histFilter; });
    }
    var html;
    if (!rows.length) {
      html = '<div class="empty">暂无记录</div>';
    } else {
      html = rows.map(function (h) {
        var ok = h.ok;
        var failedStep = (h.steps || []).filter(function (s) { return s.state === "failed"; })[0];
        var n = h.log_count || 0;
        return '<div class="hrow">' +
          '<span class="badge ' + (ok ? "latest" : "failed") + '">' + (ok ? "成功" : "失败") + '</span>' +
          '<span class="htime">' + esc(fmtTime(h.finished_at || h.created)) + '</span>' +
          '<span class="htarget">' + esc((h.targets || []).join("、") || (h.kind === "check" ? "版本检测" : "-")) + '</span>' +
          '<span class="hmsg">' + esc(failedStep ? (failedStep.name + "：" + (failedStep.message || "")) : "") + '</span>' +
          '<button class="btn tiny ghost hlog" data-id="' + esc(h.id) + '"' +
            (n ? "" : " disabled") + '>日志' + (n ? " " + n : "") + '</button>' +
          '</div>';
      }).join("");
    }
    if (html === state.histSig) return;
    state.histSig = html;
    el.innerHTML = html;
    Array.prototype.forEach.call(el.querySelectorAll(".hlog"), function (b) {
      b.addEventListener("click", function () { openLog(b.getAttribute("data-id")); });
    });
  }

  function openLog(id) {
    $("logBody").innerHTML = '<div class="empty">加载中…</div>';
    $("logModal").classList.remove("hidden");
    api("/api/history/" + encodeURIComponent(id)).then(function (j) {
      var h = j.data || {};
      var logs = h.logs || [];
      $("logTitle").textContent = (h.kind === "check" ? "版本检测" : "容器更新") +
        " · " + ((h.targets || []).join("、") || "全部容器");
      $("logSub").textContent = fmtTime(h.finished_at || h.created) +
        " · " + (h.ok ? "成功" : "失败") + " · " + logs.length + " 行日志" +
        (h.message ? " · " + h.message : "");
      if (!logs.length) {
        $("logBody").innerHTML =
          '<div class="empty">这条记录没有日志明细（可能是本次改动之前产生的旧记录）</div>';
        return;
      }
      $("logBody").innerHTML = logs.map(function (l) {
        var lv = l.level || "info";
        var cls = lv === "plain" ? "plain" : (lv === "debug" ? "debug" : lv);
        return '<div class="lg ' + cls + '">' +
          '<span class="lg-t">' + esc(fmtClock(l.ts)) + '</span>' +
          '<span class="lg-m">' + esc(l.message) + '</span></div>';
      }).join("");
      $("logBody").scrollTop = 0;
    }).catch(function (e) {
      $("logBody").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
    });
  }

  function loadHistory() {
    api("/api/history").then(function (j) {
      var list = j.data || [];
      renderHistory(list);
      renderOverviewHistory(list);
    }).catch(function () {});
  }

  /* ============================================================
     六、页面切换
     ============================================================ */
  function refreshPage(page, silent) {
    if (page === "images") loadImages();
    else if (page === "networks") loadNetworks();
    else if (page === "compose") loadCompose();
    else if (page === "audit") loadAudit();
    else if (page === "history") loadHistory();
    else if (page === "overview") {
      loadAudit(true);
      loadDisk();
      loadStats();
    }
  }

  function goPage(page) {
    if (!PAGE_TITLES[page]) page = "overview";
    state.page = page;
    Array.prototype.forEach.call(document.querySelectorAll(".page"), function (s) {
      s.classList.toggle("on", s.getAttribute("data-page") === page);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (a) {
      a.classList.toggle("on", a.getAttribute("data-page") === page);
    });
    $("pageTitle").textContent = PAGE_TITLES[page];
    try { localStorage.setItem("ikuai_page", page); } catch (e) {}
    if (state.booted) refreshPage(page);
  }

  /* ============================================================
     七、事件绑定
     ============================================================ */
  Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      goPage(a.getAttribute("data-page"));
      if (location.hash !== "#" + a.getAttribute("data-page")) {
        try { location.hash = a.getAttribute("data-page"); } catch (err) {}
      }
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-goto]"), function (b) {
    b.addEventListener("click", function () { goPage(b.getAttribute("data-goto")); });
  });
  // 前进/后退、手改地址栏里的 #hash 也要切页（否则只有点导航才生效）
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("hashchange", function () {
      var hp = (location.hash || "").replace("#", "");
      if (PAGE_TITLES[hp] && hp !== state.page) goPage(hp);
    });
  }
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Escape" || e.keyCode === 27)) {
      if (!$("cfmModal").classList.contains("hidden")) return closeBox(false);
      if (!$("drawer").classList.contains("hidden")) return closeDrawer();
      ["tagModal", "setModal", "scModal", "ctModal", "netModal", "cmpModal", "logModal"]
        .forEach(function (id) { var el = $(id); if (el) el.classList.add("hidden"); });
    }
  });

  $("btnAuto").addEventListener("click", function () { setAuto(!state.auto); });
  $("btnRefresh").addEventListener("click", function () {
    loadOverview().then(function () {
      if (state.page !== "overview") refreshPage(state.page);
      else { loadDisk(); loadStats(); }
      if (state.auto) scheduleAuto();
    });
  });
  $("btnCheck").addEventListener("click", function () { startCheck(false); });
  $("btnCheckInline").addEventListener("click", function () { startCheck(false); });
  $("btnUpdateAll").addEventListener("click", function () {
    startUpdate(updatableContainers().map(function (c) { return c.name; }));
  });
  $("btnPinToLatest").addEventListener("click", function () {
    startUpdate(pinnedContainers().map(function (c) { return c.name; }), "latest");
  });
  $("btnUpdateSelected").addEventListener("click", function () {
    var names = Object.keys(state.selected).filter(function (k) { return state.selected[k]; });
    startUpdate(names);
  });
  $("checkAll").addEventListener("change", function () {
    var on = this.checked;
    visibleContainers().forEach(function (c) { state.selected[c.name] = on; });
    renderContainers();
  });
  $("ctSearch").addEventListener("input", function () {
    state.ctSearch = this.value || "";
    state.ctSig = "";
    renderContainers();
  });
  $("ctFilter").addEventListener("change", function () {
    state.ctFilter = this.value || "all";
    state.ctSig = "";
    renderContainers();
  });
  $("dryRun").addEventListener("change", function () {
    try { localStorage.setItem("ikuai_dry_run", this.checked ? "1" : "0"); } catch (e) {}
    applySettings(null);
  });

  // 确认框
  $("cfmOk").addEventListener("click", function () { closeBox(true); });
  $("cfmCancel").addEventListener("click", function () { closeBox(false); });
  $("cfmModal").addEventListener("click", function (e) {
    if (e.target === this) closeBox(false);
  });

  // 退出登录
  (function () {
    var el = $("btnLogout");
    if (!el) return;
    el.addEventListener("click", function () {
      el.disabled = true;
      fetch("/api/logout", { method: "POST" })
        .catch(function () {})
        .then(function () { location.href = "/login"; });
    });
  })();

  // 历史 / 日志弹窗
  $("btnReloadHist").addEventListener("click", loadHistory);
  $("histFilter").addEventListener("change", function () {
    state.histFilter = this.value || "all";
    state.histSig = "";
    loadHistory();
  });
  $("btnCloseLog").addEventListener("click", function () { $("logModal").classList.add("hidden"); });
  $("logModal").addEventListener("click", function (e) {
    if (e.target === this) this.classList.add("hidden");
  });
  $("btnLogCopy").addEventListener("click", function () {
    var btn = this;
    var lines = Array.prototype.map.call($("logBody").querySelectorAll(".lg"), function (r) {
      return r.textContent;
    });
    var txt = $("logSub").textContent + "\n\n" + lines.join("\n");
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(txt).then(function () {
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = "复制"; }, 1500);
    });
  });

  // 执行面板
  $("btnCloseExec").addEventListener("click", function () { $("execPanel").classList.add("hidden"); });
  $("btnClearLog").addEventListener("click", function () {
    state.logs = [];
    $("log").innerHTML = "";
  });
  $("showDebug").addEventListener("change", function () {
    try { localStorage.setItem("ikuai_show_debug", this.checked ? "1" : "0"); } catch (e) {}
    renderLog();
  });

  // 详情抽屉
  $("btnCloseDrawer").addEventListener("click", closeDrawer);
  $("drawer").addEventListener("click", function (e) {
    if (e.target === this) closeDrawer();
  });
  Array.prototype.forEach.call(document.querySelectorAll("#drawerTabs .tab"), function (b) {
    b.addEventListener("click", function () { setDrawerTab(b.getAttribute("data-tab")); });
  });

  // 版本弹窗
  $("btnCloseTag").addEventListener("click", function () { $("tagModal").classList.add("hidden"); });
  $("btnTagConfirm").addEventListener("click", function () {
    var t = state.target;
    if (!t) return;
    var manual = ($("tagManual").value || "").trim();
    var tag = manual || t.picked || t.tag;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(tag)) {
      toast("tag 只能是字母、数字、点、下划线、横线", "warn", 5000);
      return;
    }
    $("tagModal").classList.add("hidden");
    if (state.busy || state.source) { toast("已有任务在执行，等它跑完再说", "warn"); return; }
    confirmUpdate([t.name], tag).then(function (yes) {
      if (yes) runUpdate([t.name], tag);
    });
  });

  // 设置 / 定时任务
  Array.prototype.forEach.call(
    document.querySelectorAll("#btnSettingsTop, .btn-settings"),
    function (b) { b.addEventListener("click", openSettings); });
  $("btnCloseSet").addEventListener("click", function () { $("setModal").classList.add("hidden"); });
  $("btnSetSave").addEventListener("click", saveSettings);
  $("btnNewSchedule").addEventListener("click", function () { openSchedule(null); });
  $("btnCloseSc").addEventListener("click", function () { $("scModal").classList.add("hidden"); });
  $("btnScSave").addEventListener("click", saveSchedule);
  $("scPickAll").addEventListener("click", function () {
    Array.prototype.forEach.call(document.querySelectorAll(".sc-target"),
      function (x) { x.checked = true; });
    syncTargetHint();
  });
  $("scPickNone").addEventListener("click", function () {
    Array.prototype.forEach.call(document.querySelectorAll(".sc-target"),
      function (x) { x.checked = false; });
    syncTargetHint();
  });
  bindSeg("scMode", function (el) { state.scMode = el.getAttribute("data-mode"); });
  bindSeg("setNotify", function (el) { state.setNotify = el.getAttribute("data-notify"); });
  bindSeg("scFreq", function (el) {
    state.scFreq = el.getAttribute("data-freq");
    syncFreqVisibility();
  });
  renderQuick($("scEveryQuick"),
    [{ v: 60, label: "1 小时" }, { v: 360, label: "6 小时" },
     { v: 720, label: "12 小时" }, { v: 1440, label: "1 天" }],
    function (v) {
      $("scEvery").value = v;
      state.scFreq = "every";
      setSeg("scFreq", "data-freq", "every");
      syncFreqVisibility();
    });

  // 镜像页
  $("btnReloadImages").addEventListener("click", loadImages);
  $("btnImgSearch").addEventListener("click", searchImages);
  $("imgSearch").addEventListener("keydown", function (e) {
    if (e.key === "Enter") searchImages();
  });

  // 网络 / 编排
  $("btnNewNetwork").addEventListener("click", function () {
    ["netName", "netSubnet", "netGateway", "netComment"].forEach(function (id) { $(id).value = ""; });
    showBanner("netWarn", "");
    $("netModal").classList.remove("hidden");
  });
  $("btnCloseNet").addEventListener("click", function () { $("netModal").classList.add("hidden"); });
  $("btnNetSave").addEventListener("click", function () {
    var body = {
      action: "add",
      name: ($("netName").value || "").trim(),
      subnet: ($("netSubnet").value || "").trim(),
      gateway: ($("netGateway").value || "").trim(),
      comment: ($("netComment").value || "").trim()
    };
    if (!body.name) { showBanner("netWarn", "网络名不能为空"); return; }
    api("/api/networks", { method: "POST", body: body })
      .then(function () {
        $("netModal").classList.add("hidden");
        toast("网络 " + body.name + " 已创建", "ok");
        loadNetworks();
      })
      .catch(function (e) { showBanner("netWarn", "创建失败：" + e.message); });
  });
  $("btnNewCompose").addEventListener("click", function () {
    $("cmpName").value = "";
    $("cmpContent").value = "";
    showBanner("cmpWarn", "");
    $("cmpModal").classList.remove("hidden");
  });
  $("btnCloseCmp").addEventListener("click", function () { $("cmpModal").classList.add("hidden"); });
  $("btnReloadCompose").addEventListener("click", loadCompose);
  $("btnCmpSave").addEventListener("click", function () {
    var body = {
      tagname: ($("cmpName").value || "").trim(),
      content: $("cmpContent").value || ""
    };
    if (!body.tagname) { showBanner("cmpWarn", "项目名不能为空"); return; }
    if (!body.content.trim()) { showBanner("cmpWarn", "compose 内容不能为空"); return; }
    api("/api/compose/create", { method: "POST", body: body })
      .then(function () {
        $("cmpModal").classList.add("hidden");
        toast("项目 " + body.tagname + " 已创建", "ok");
        loadCompose();
      })
      .catch(function (e) { showBanner("cmpWarn", "创建失败：" + e.message); });
  });
  $("btnReloadAudit").addEventListener("click", function () { loadAudit(); });

  // 新建容器
  $("btnCreateContainer").addEventListener("click", function () {
    ["ctName", "ctImage", "ctIp", "ctPorts", "ctMounts", "ctEnv", "ctCmd"].forEach(function (id) {
      $(id).value = "";
    });
    $("ctTag").value = "latest";
    $("ctIface").value = "doc_docker";
    $("ctMem").value = "0";
    $("ctAutoStart").checked = true;
    $("ctEnabled").checked = true;
    showBanner("ctWarn", "");
    $("ctModal").classList.remove("hidden");
  });
  $("btnCloseCt").addEventListener("click", function () { $("ctModal").classList.add("hidden"); });
  $("btnCtSave").addEventListener("click", function () {
    var body = {
      name: ($("ctName").value || "").trim(),
      image: ($("ctImage").value || "").trim(),
      tag: ($("ctTag").value || "latest").trim() || "latest",
      interface: ($("ctIface").value || "doc_docker").trim(),
      ipaddr: ($("ctIp").value || "").trim(),
      memory: (parseInt($("ctMem").value, 10) || 0) * 1048576,
      ports: ($("ctPorts").value || "").trim(),
      mounts: ($("ctMounts").value || "").trim(),
      env: ($("ctEnv").value || "").trim(),
      cmd: ($("ctCmd").value || "").trim(),
      auto_start: $("ctAutoStart").checked ? 1 : 0,
      enabled: $("ctEnabled").checked ? "yes" : "no"
    };
    if (!body.name) { showBanner("ctWarn", "容器名不能为空"); return; }
    if (!body.image) { showBanner("ctWarn", "镜像不能为空"); return; }
    var btn = $("btnCtSave");
    btn.disabled = true;
    api("/api/containers/create", { method: "POST", body: body })
      .then(function () {
        $("ctModal").classList.add("hidden");
        toast("容器 " + body.name + " 已创建", "ok");
        refreshAfterChange();
      })
      .catch(function (e) { showBanner("ctWarn", "创建失败：" + e.message); })
      .then(function () { btn.disabled = false; });
  });

  /* ============================================================
     八、启动
     ============================================================ */
  try {
    if (localStorage.getItem("ikuai_dry_run") === "1") $("dryRun").checked = true;
    if (localStorage.getItem("ikuai_show_debug") === "1") $("showDebug").checked = true;
    if (localStorage.getItem("ikuai_auto_refresh") === "0") state.auto = false;
  } catch (e) {}

  function autorun() {
    var q = location.search;
    if (/[?&]autorun=check/.test(q)) { setTimeout(startCheck, 600); return; }
    if (/[?&]autorun=update/.test(q) && /[?&]dry=1/.test(q)) {
      var m = /[?&]container=([^&]+)/.exec(q);
      var names = m ? [decodeURIComponent(m[1])] : [];
      if (!names.length) return;
      $("dryRun").checked = true;
      setTimeout(function () { startUpdate(names); }, 600);
    }
  }

  // 初始页面：hash 优先，其次上次停留的页面
  var initialPage = (location.hash || "").replace("#", "");
  if (!PAGE_TITLES[initialPage]) {
    try { initialPage = localStorage.getItem("ikuai_page") || "overview"; } catch (e) { initialPage = "overview"; }
  }
  goPage(initialPage);

  setAuto(state.auto);
  setInterval(paintAuto, 1000);

  document.addEventListener("visibilitychange", function () {
    if (document.hidden || !state.auto || state.busy || state.source) return;
    var stale = state.lastRefresh &&
                (Date.now() / 1000 - state.lastRefresh) > AUTO_MS / 2000;
    if (stale) {
      loadOverview({ silent: true }).then(function () { scheduleAuto(); });
    } else {
      scheduleAuto();
    }
  });

  loadOverview().then(function () {
    state.booted = true;
    try { autorun(); } catch (e) {}
    loadAudit(true);
    loadDisk();
    // 直接落在 #images / #networks 这类页面时，首屏那次 goPage 因为还没拿到
    // 容器数据会跳过取数，这里补一次，否则页面永远停在「加载中…」。
    if (state.page !== "overview") refreshPage(state.page);
  });
})();
