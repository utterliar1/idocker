/* ============================================================
   爱快 Docker 容器更新器 —— 前端逻辑
   通过 SSE 实时接收六步执行事件，驱动流程图与日志终端
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = {
    containers: [],
    checkMap: {},        // "repo:tag" → 上游状态
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
    settings: {},        // 下载超时 + 定时任务
    setInited: false,
    scEdit: null,        // 正在编辑的定时任务（null = 新建）
    scMode: "check",
    scFreq: "every",
    // ---- 上游版本检测 ----
    checkTs: 0,          // 上次检测的时刻（秒），来自服务端持久化的 last_check
    detecting: false,    // 正在后台静默检测（不弹执行面板）
    routerReady: false,  // 爱快地址与密码都配好了，才谈得上检测
    jobKind: "",         // 当前订阅的任务类型（check / update）
    jobTargets: [],      // 当前任务涉及的容器，收尾提示用
    autoCheckedAt: 0,    // 上次由「自动检测」发起的时刻（秒），用于节流
    // ---- 自动刷新 ----
    auto: true,          // 自动刷新开关（用户点按钮可暂停）
    busy: false,         // 有任务已提交/正在执行，自动刷新让路
    autoTimer: null,
    nextAt: 0,           // 下一次自动刷新的时刻（ms）
    lastRefresh: 0,      // 上次成功刷新的时刻（秒）
    ctSig: "",           // 容器表 HTML 指纹，用于跳过无变化的重绘
    histSig: ""          // 历史列表 HTML 指纹
  };

  var STEP_ORDER = ["login", "inspect", "pull", "download", "update", "verify"];
  // 自动刷新间隔：默认 60 秒；可用 ?autorefresh=秒数 覆盖（5~3600），方便调试
  var AUTO_MS = (function () {
    var m = /[?&]autorefresh=(\d+)/.exec(location.search || "");
    var sec = m ? parseInt(m[1], 10) : 60;
    if (!(sec >= 5)) sec = 60;
    if (sec > 3600) sec = 3600;
    return sec * 1000;
  })();
  // 自动版本检测：打开页面时若上次检测结果已过期，就自己跑一次，
  // 省掉「先点一下检查更新才知道有没有新版」这一步。
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
    // 会话过期 / 未登录：带上当前地址，登录后原路返回。
    // 服务端也会在页面类请求上直接 302 到登录页，这里主要兜住接口请求。
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
      // 401 = 没登录或登录过期。以前这里是浏览器原生弹框，现在跳登录页。
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

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showBanner(id, msg) {
    var el = $(id);
    if (!msg) { el.classList.add("hidden"); return; }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  /* ---------------- 轻提示 ----------------
     右下角堆叠、几秒后自动消失，几秒内重复出现会自动续期而不是刷屏。 */
  function toast(msg, type, ms) {
    var wrap = $("toastWrap");
    if (!wrap || !msg) return;
    var el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    wrap.appendChild(el);
    // 最多同时留 4 条，多了把最老的挤掉，免得糊住半个屏幕
    while (wrap.childNodes && wrap.childNodes.length > 4) {
      try { wrap.removeChild(wrap.childNodes[0]); } catch (e) { break; }
    }
    setTimeout(function () {
      try { if (wrap.removeChild) wrap.removeChild(el); } catch (e) { /* 已移除 */ }
    }, ms || 4200);
  }

  /* ---------------- 弹窗：确认 / 告知 ----------------
     不用原生 confirm/alert：一是长得难看，二是批量更新前要列出到底动了谁，
     原生框根本排不了版。 */
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

  /* ---------------- 总览 ----------------
     静默刷新（opts.silent）不清空表格、不显示「加载中」，
     配合 renderContainers 的指纹比对，没变化就一个像素都不动。
     这样 60 秒自动刷一次时页面不会「跳一下」。 */
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
      $("verInfo").textContent = "v" + (cfg.version || "?") +
        " · 加速源 " + (cfg.registries || []).join(" / ");
      // 干跑开关只在首次加载时从服务端同步，避免 60 秒轮询覆盖用户的选择
      if (!state.inited) {
        $("dryRun").checked = !!cfg.dry_run;
        state.inited = true;
      }

      // 只有开了访问认证才显示「退出登录」
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
      $("imageCount").textContent = "本地镜像 " + (d.images_count || 0) + " 个";
      state.mirrors = d.mirrors || [];

      // 恢复上次检测结果，刷新页面后上游状态不丢
      var ck = d.check || {};
      state.checkMap = {};
      var items = ck.items || {};
      Object.keys(items).forEach(function (k) { state.checkMap[k] = items[k].status; });
      state.checkTs = ck.checked_at || 0;
      state.checkAt = ck.at_str || "";
      $("checkTime").textContent = state.checkAt ? ("上次检测 " + state.checkAt.slice(5)) : "";

      renderContainers();
      renderHistory(d.history || []);
      applySettings(d.settings || {});

      state.lastRefresh = Date.now() / 1000;
      $("lastRefresh").textContent = "刷新于 " + fmtClock(state.lastRefresh);
      maybeAutoCheck();
    }).catch(function (e) {
      showBanner("errBanner", "读取失败：" + e.message);
      // 已有数据时保留旧表格，只挂红条提示，避免一次网络抖动就把列表清空
      if (!state.containers.length) {
        $("ctBody").innerHTML = '<tr><td colspan="8" class="empty">读取失败</td></tr>';
      }
    });
  }

  /* ---------------- 上游状态 ---------------- */
  function checkKey(c) { return (c.repo || "") + ":" + (c.tag || ""); }
  function checkStatusOf(c) { return state.checkMap[checkKey(c)] || "unknown"; }
  function updatableContainers() {
    // 用排好序的列表：一键更新的确认框里，顺序跟表格里看到的一致
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

  /* 概览条：一眼看到「有没有要更新的、有几个」，并给出一键更新入口。 */
  function renderUpdBar() {
    var total = state.containers.length;
    var newer = 0, latest = 0, failed = 0;
    state.containers.forEach(function (c) {
      var k = checkStatusOf(c);
      if (k === "newer") newer++;
      else if (k === "latest") latest++;
      else if (k === "failed") failed++;
    });
    var stat = $("updStat"), all = $("btnUpdateAll");
    if (!stat) return;

    if (state.detecting) {
      stat.className = "upd-stat busy";
      stat.textContent = "正在检测上游版本…";
      all.classList.add("hidden");
      return;
    }
    if (!total) {
      stat.className = "upd-stat";
      stat.textContent = "没有容器";
      all.classList.add("hidden");
      return;
    }
    if (newer) {
      stat.className = "upd-stat newer";
      stat.textContent = "⬆ 有 " + newer + " 个容器可以更新" +
        (failed ? "（另 " + failed + " 个查询失败）" : "");
      all.textContent = "更新这 " + newer + " 个";
      all.classList.remove("hidden");
      return;
    }
    all.classList.add("hidden");
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

  // 上次检测结果过期就自己检测一次。同一阈值同时充当节流：
  // 页面一直开着也不会漏检，刚失败也不会被反复重试打爆加速源。
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

  function renderContainers() {
    var tb = $("ctBody");
    var html;
    if (!state.containers.length) {
      html = '<tr><td colspan="8" class="empty">没有容器（或 Docker 服务未启动）</td></tr>';
    } else {
      html = sortedContainers().map(function (c) {
        var st = checkStatusOf(c);
        var badge = { newer: ["newer", "⬆ 有新版本"], latest: ["latest", "已是最新"],
                      failed: ["failed", "查询失败"], running: ["running", "更新中"] }[st]
                    || ["", "未检测"];
        var running = String(c.state) === "running";
        var led = running ? "up" : "down";
        return '' +
          '<tr class="' + (st === "newer" ? "need-upd" : "") + '" data-name="' + esc(c.name) + '">' +
            '<td class="cb" data-label=""><input type="checkbox" class="ct-pick" data-name="' + esc(c.name) + '"' +
              (state.selected[c.name] ? " checked" : "") + '></td>' +
            '<td data-label="容器"><div class="ct-name">' + esc(c.name) + '</div>' +
              '<div class="ct-sub">' + esc(c.id ? c.id.slice(0, 12) : "") + '</div></td>' +
            '<td data-label="镜像"><div class="mono">' + esc(c.repo) + '</div>' +
              '<div class="ct-sub">' + esc(c.tag) + '</div></td>' +
            '<td data-label="状态"><span class="dot-led"><i class="led ' + led + '"></i>' +
              esc(shortStatus(c.status)) + '</span></td>' +
            '<td class="mono" data-label="IP">' + esc(c.ipaddr || "-") + '</td>' +
            '<td class="mono" data-label="镜像拉取于">' + localPulledCell(c) + '</td>' +
            '<td data-label="上游"><span class="badge ' + badge[0] + '" title="' +
              esc(state.checkAt ? ("检测于 " + state.checkAt) : "尚未检测") + '">' +
              badge[1] + '</span></td>' +
            '<td class="r ct-acts">' +
              '<button class="btn tiny ghost ct-tag" data-name="' + esc(c.name) + '" ' +
                'data-repo="' + esc(c.repo) + '" data-tag="' + esc(c.tag) + '">指定版本</button> ' +
              '<button class="btn tiny' + (st === "newer" ? " primary" : "") +
                ' ct-upd" data-name="' + esc(c.name) + '">更新</button>' +
            '</td>' +
          '</tr>';
      }).join("");
    }
    // 内容一模一样就不碰 DOM：省掉整表重排造成的闪动，也保住鼠标悬停/焦点
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

  // 本地镜像时间。爱快对「导入/引用」而来的镜像没有拉取记录（install=0），
  // 后端会退回镜像构建时间并标记 src=build，这里如实标注，不要谎报成「未安装」。
  function localPulledCell(c) {
    if (!c.local_pulled) return '<span class="muted">—</span>';
    if (c.local_pulled_src === "build") {
      return '<span class="muted" title="爱快无拉取记录（镜像由导入或引用而来），此处为镜像构建时间">' +
             esc(c.local_pulled) + '</span>';
    }
    return esc(c.local_pulled);
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
  }

  function updateSelCount() {
    var n = Object.keys(state.selected).filter(function (k) { return state.selected[k]; }).length;
    var btn = $("btnUpdateSelected");
    btn.disabled = n === 0;
    btn.textContent = n ? ("更新选中 (" + n + ")") : "更新选中";
  }

  /* ---------------- 执行面板 ---------------- */
  function openExec(title, flow) {
    autoYieldToJob();          // 开面板即视为开始跑任务，自动刷新先让路
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
      // 运行中转圈；完成打勾；失败叉号；跳过用短横；其余显示序号
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
    // 跳过也算已走过，进度条才拉得满
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
    // 行数被裁剪过就整体重绘，保证 DOM 与数据一致
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

  /* ---------------- 自动刷新 ----------------
     60 秒一轮的定时刷新，随时可以暂停（按钮即可，偏好记在 localStorage）。
     执行任务期间自动让路、跑完再续上；切到后台标签页时顺延不刷。 */
  function autoLabelText() {
    if (!state.auto) return "已暂停";
    if (state.busy || state.source) return "执行中…";
    var left = state.nextAt ? Math.round((state.nextAt - Date.now()) / 1000) : AUTO_MS / 1000;
    return "自动刷新 " + (left > 0 ? left : 0) + "s";
  }

  function paintAuto() {
    var btn = $("btnAuto");
    btn.classList.toggle("off", !state.auto);
    $("autoLabel").textContent = autoLabelText();
    btn.title = state.auto
      ? "每 " + (AUTO_MS / 1000) + " 秒自动刷新一次列表（执行任务时会自动让路）；点一下暂停"
      : "自动刷新已暂停，点一下恢复";
  }

  function autoClear() {
    if (state.autoTimer) { clearTimeout(state.autoTimer); state.autoTimer = null; }
    state.nextAt = 0;
  }

  function scheduleAuto(delay) {
    autoClear();
    // 关闭 / 有任务在跑：不排下一轮。任务结束时会再调一次，不会漏刷新。
    if (!state.auto || state.busy || state.source) { paintAuto(); return; }
    var wait = delay || AUTO_MS;
    state.nextAt = Date.now() + wait;
    paintAuto();
    state.autoTimer = setTimeout(function () {
      state.autoTimer = null;
      if (!state.auto || state.busy || state.source) { paintAuto(); return; }
      if (document.hidden) { scheduleAuto(); return; }      // 后台标签页顺延，回来再刷
      loadOverview({ silent: true }).then(function () { scheduleAuto(); });
    }, wait);
  }

  function setAuto(on) {
    state.auto = !!on;
    try { localStorage.setItem("ikuai_auto_refresh", state.auto ? "1" : "0"); } catch (e) {}
    scheduleAuto();
  }

  // 开始跑任务时把自动刷新让出去
  function autoYieldToJob() {
    state.busy = true;
    autoClear();
    paintAuto();
  }

  /* ---------------- SSE ----------------
     silent = 后台静默任务（自动版本检测）：照常收事件、更新状态，
     但不碰执行面板，也不打扰正在看页面的人。 */
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
      $("execCounter").textContent = (d.kind === "update" ? "更新 " : "检测 ") +
        (d.targets || []).join("、");
    });

    es.addEventListener("progress", function (e) {
      var d = JSON.parse(e.data);
      if (silent) return;
      state.progress.current = d.current;
      state.progress.total = d.total;
      $("execCounter").textContent = "[" + d.current + "/" + d.total + "] " + d.container;
      // 进入新容器时重置流程图
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
      (d.results || []).forEach(function (r) { state.checkMap[r.image] = r.status; });
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
      // 服务端流正常结束或临时断线：已结束就关闭，否则让浏览器自动重连
      if (es.readyState === 2) {
        state.source = null;
        state.busy = false;         // 流断了也别把自动刷新一直卡住
        setDetecting(false);
        scheduleAuto();
      }
    };
  }

  // 任务收尾时给一句人话结论 —— 尤其是后台自动检测，用户没盯着看，
  // 必须主动告诉他「有几个可以更新」。
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

  /* ---------------- 触发任务 ---------------- */
  // 更新会重启容器（服务短暂中断），批量之前先让人确认一遍，并列出到底动谁
  function startUpdate(names) {
    if (!names || !names.length) return;
    if (state.busy || state.source) { toast("已有任务在执行，等它跑完再说", "warn"); return; }
    confirmUpdate(names, "").then(function (yes) { if (yes) runUpdate(names, ""); });
  }

  function confirmUpdate(names, tag) {
    var map = {};
    state.containers.forEach(function (c) { map[c.name] = c; });
    var dry = !!$("dryRun").checked;
    var rows = names.map(function (n) {
      var c = map[n];
      var st = c ? checkStatusOf(c) : "unknown";
      var tail = st === "newer" ? '<span class="badge newer">有新版本</span>'
               : st === "latest" ? '<span class="badge latest">已是最新</span>' : "";
      var img = c ? ((c.repo || "") + ":" + (c.tag || "")) : "";
      return '<li><span class="cfm-nm">' + esc(n) + '</span>' +
             '<span class="mono cfm-img">' + esc(img) + '</span>' + tail + '</li>';
    }).join("");
    return confirmBox({
      title: dry ? "确认演练更新" : "确认更新容器",
      html: '<p class="cfm-lead">将' + (dry ? "演练" : "") + '更新以下 <b>' + names.length +
            '</b> 个容器' + (tag ? '，版本固定为 <b>' + esc(tag) + '</b>' : '') + '：</p>' +
            '<ul class="cfm-list">' + rows + '</ul>' +
            '<p class="cfm-note">' + (dry
              ? "当前是<b>干跑模式</b>：只走一遍流程并打印将要发出的请求，<b>不会真的重启任何容器</b>。"
              : "过程中这些容器会各<b>重启一次</b>，服务短暂中断（几秒到几十秒，视镜像大小而定）。") +
            '</p>',
      okText: dry ? "开始演练" : "开始更新",
      danger: !dry
    });
  }

  function runUpdate(names, tag) {
    openExec("正在更新容器", null);
    state.jobTargets = names.slice();
    appendLog({ ts: Date.now() / 1000, level: "info",
                message: "提交更新请求：" + names.join("、") +
                         (tag ? (" → " + tag) : "") +
                         "（干跑：" + ($("dryRun").checked ? "开" : "关") + "）" });
    var body = { containers: names, dry_run: $("dryRun").checked };
    if (tag) body.tag = tag;
    api("/api/update", { method: "POST", body: body }).then(function (j) {
      connect(j.job_id, false);
    }).catch(function (e) {
      appendLog({ ts: Date.now() / 1000, level: "error", message: "提交失败：" + e.message });
      $("execSpinner").className = "spinner failed";
      $("execTitle").textContent = "提交失败";
      state.busy = false;          // 没跑起来，自动刷新照常继续
      scheduleAuto();
    });
  }

  // silent = 打开页面时的自动检测：不弹执行面板，只在概览条上转一圈
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
      state.busy = false;          // 没跑起来，自动刷新照常继续
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
    $("tagBody").innerHTML = '<div class="muted">正在查询 ' + esc(repo) + ' 的可用版本…</div>';
    $("tagModal").classList.remove("hidden");
    api("/api/tags?image=" + encodeURIComponent(repo)).then(function (j) {
      var tags = j.data || [];
      if (!tags.length) { $("tagBody").innerHTML = '<div class="muted">没查到版本列表</div>'; return; }
      $("tagBody").innerHTML = '<div class="muted" style="margin-bottom:10px">当前：<b>' +
        esc(tag) + '</b> · 共 ' + tags.length + ' 个版本，点击选择</div><div class="tag-grid">' +
        tags.map(function (t) {
          return '<span class="tag-chip" data-tag="' + esc(t) + '">' + esc(t) + '</span>';
        }).join("") + '</div>';
      Array.prototype.forEach.call(document.querySelectorAll(".tag-chip"), function (c) {
        c.addEventListener("click", function () {
          Array.prototype.forEach.call(document.querySelectorAll(".tag-chip"),
            function (x) { x.classList.remove("on"); });
          c.classList.add("on");
          state.target.picked = c.dataset.tag;
        });
      });
    }).catch(function (e) {
      $("tagBody").innerHTML = '<div class="muted">查询失败：' + esc(e.message) + '</div>';
    });
  }

  /* ---------------- 设置与定时任务 ---------------- */
  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return sec + " 秒";
    var m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return m + " 分钟" + (s ? " " + s + " 秒" : "");
    var h = Math.floor(m / 60), mm = m % 60;
    return h + " 小时" + (mm ? " " + mm + " 分" : "");
  }

  function applySettings(s) {
    if (s && (s.schedules || s.pull_timeout != null)) state.settings = s;
    var t = (state.settings || {}).pull_timeout;
    if (t) {
      $("scTimeout").textContent = "下载超时 " + fmtDuration(t) +
        ($("dryRun").checked ? " · 干跑中改不了真容器" : "");
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

  /* ---------------- 定时任务弹窗 ---------------- */
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
    Array.prototype.forEach.call($(boxId).querySelectorAll(".seg-item"), function (x) {
      x.classList.toggle("on", x.getAttribute(attr) === value);
    });
  }

  function bindSeg(boxId, onChange) {
    var box = $(boxId);
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

  /* ---------------- 设置弹窗 ---------------- */
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
    $("setModal").classList.remove("hidden");
  }

  function renderQuick(box, items, onPick) {
    box.innerHTML = items.map(function (it) {
      return '<span class="quick" data-v="' + it.v + '">' + esc(it.label) + '</span>';
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll(".quick"), function (q) {
      q.addEventListener("click", function () { onPick(parseInt(q.dataset.v, 10)); });
    });
  }

  function saveSettings() {
    var v = parseInt($("setTimeout").value, 10);
    api("/api/settings", { method: "POST", body: { pull_timeout: v } })
      .then(function (j) {
        applySettings(j.data);
        state.setInited = false;
        $("setModal").classList.add("hidden");
      })
      .catch(function (e) { toast("保存失败：" + e.message, "error"); });
  }

  /* ---------------- 历史 ---------------- */
  function renderHistory(list) {
    var el = $("history");
    var html;
    if (!list || !list.length) {
      html = '<div class="empty">暂无记录</div>';
    } else {
      html = list.map(function (h) {
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

  /* ---------------- 历史日志明细 ---------------- */
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
    api("/api/history").then(function (j) { renderHistory(j.data || []); }).catch(function () {});
  }

  /* ---------------- 事件绑定 ---------------- */
  $("btnAuto").addEventListener("click", function () { setAuto(!state.auto); });
  $("btnRefresh").addEventListener("click", function () {
    // 手动刷新只提前拉一次，并把自动刷新的倒计时重新起算（免得刚点完又被刷一遍）
    loadOverview().then(function () { if (state.auto) scheduleAuto(); });
  });
  $("btnCheck").addEventListener("click", function () { startCheck(false); });
  $("btnCheckInline").addEventListener("click", function () { startCheck(false); });
  $("btnUpdateAll").addEventListener("click", function () {
    startUpdate(updatableContainers().map(function (c) { return c.name; }));
  });

  // 通用确认弹窗
  $("cfmOk").addEventListener("click", function () { closeBox(true); });
  $("cfmCancel").addEventListener("click", function () { closeBox(false); });
  $("cfmModal").addEventListener("click", function (e) {
    if (e.target === this) closeBox(false);
  });
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Escape" || e.keyCode === 27) && !$("cfmModal").classList.contains("hidden")) {
      closeBox(false);
    }
  });

  // 退出登录：先让服务端把会话 cookie 清掉，再回登录页。
  // 清不掉（网络抖动）也照样跳，避免把人卡在页面上。
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
  $("btnReloadHist").addEventListener("click", loadHistory);
  $("btnCloseLog").addEventListener("click", function () {
    $("logModal").classList.add("hidden");
  });
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
  $("btnCloseExec").addEventListener("click", function () {
    $("execPanel").classList.add("hidden");
  });
  $("btnClearLog").addEventListener("click", function () {
    state.logs = [];
    $("log").innerHTML = "";
  });
  $("showDebug").addEventListener("change", function () {
    try { localStorage.setItem("ikuai_show_debug", this.checked ? "1" : "0"); } catch (e) {}
    renderLog();
  });
  $("btnUpdateSelected").addEventListener("click", function () {
    var names = Object.keys(state.selected).filter(function (k) { return state.selected[k]; });
    startUpdate(names);
  });
  $("checkAll").addEventListener("change", function () {
    var on = this.checked;
    state.containers.forEach(function (c) { state.selected[c.name] = on; });
    renderContainers();
  });
  $("dryRun").addEventListener("change", function () {
    try { localStorage.setItem("ikuai_dry_run", this.checked ? "1" : "0"); } catch (e) {}
    applySettings(null);          // 让「下载超时」那行同步提示当前处于干跑
  });
  $("btnCloseTag").addEventListener("click", function () { $("tagModal").classList.add("hidden"); });
  $("btnTagConfirm").addEventListener("click", function () {
    var t = state.target;
    if (!t) return;
    $("tagModal").classList.add("hidden");
    var tag = t.picked || t.tag;
    if (state.busy || state.source) { toast("已有任务在执行，等它跑完再说", "warn"); return; }
    confirmUpdate([t.name], tag).then(function (yes) {
      if (yes) runUpdate([t.name], tag);
    });
  });
  $("lnkRegistries").addEventListener("click", function (e) {
    e.preventDefault();
    function list(items) {
      return '<ol class="cfm-list cfm-ol">' + items.map(function (s) {
        return '<li class="mono">' + esc(s) + '</li>';
      }).join("") + '</ol>';
    }
    var src = (state.mirrors || []).slice();
    if (src.length) {
      infoBox("镜像加速源",
        '<p class="cfm-lead">读取自爱快 Docker 服务设置，按顺序回退：</p>' + list(src) +
        '<p class="cfm-note">镜像由<b>爱快自己下载</b>，改爱快后台这一处即可，本工具自动跟随。</p>');
      return;
    }
    api("/api/config").then(function (j) {
      infoBox("镜像加速源（兜底）",
        '<p class="cfm-lead">没能读到爱快的加速源，当前退回本工具内置源：</p>' +
        list(j.data.registries || []));
    }).catch(function (err) { toast("读取失败：" + err.message, "error"); });
  });

  /* ---------------- 设置 / 定时任务 事件 ---------------- */
  $("btnSettingsTop").addEventListener("click", openSettings);
  $("btnSettingsPanel").addEventListener("click", openSettings);
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

  // 恢复用户偏好
  try {
    if (localStorage.getItem("ikuai_dry_run") === "1") $("dryRun").checked = true;
    if (localStorage.getItem("ikuai_show_debug") === "1") $("showDebug").checked = true;
    if (localStorage.getItem("ikuai_auto_refresh") === "0") state.auto = false;
  } catch (e) {}

  // 便捷入口（可收藏为书签）：
  //   /?autorun=check                      打开即开始版本检测
  //   /?autorun=update&container=clash&dry=1  预览更新流程（强制干跑，URL 无法触发真实更新）
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

  // 自动刷新：先按偏好把开关摆好，再拉首次数据；按钮倒计时每秒走一格
  setAuto(state.auto);
  setInterval(paintAuto, 1000);

  // 从后台标签页切回来时，数据太旧就立刻补一次，否则照常等下一轮
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

  loadOverview().then(function () { try { autorun(); } catch (e) {} });
})();
