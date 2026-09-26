/* ============================================================
   前端「自动刷新」行为回归测试
   ------------------------------------------------------------
   零依赖：手写 DOM 桩 + vm 直接跑 webapp/app/static/app.js，
   验证开关暂停/恢复、倒计时、静默刷新不重绘、后台标签页顺延、
   以及执行任务期间自动让路。

   跑法：  node _test/test_frontend.js
   用 ?autorefresh=5 把间隔压到 5 秒，全跑完约 25 秒。
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP = path.join(__dirname, "..", "webapp", "app", "static", "app.js");
const SRC = fs.readFileSync(APP, "utf8");
const AUTO_SEC = 5;                    // 与注入的 location.search 保持一致

let PASS = 0, FAIL = 0;
function ok(name, cond, extra) {
  if (cond) { PASS++; console.log("  \u2713 " + name); }
  else {
    FAIL++;
    console.log("  \u2717 " + name + (extra !== undefined ? "  \u2192 " + JSON.stringify(extra) : ""));
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- DOM 桩 ---------------- */
function makeEl(id) {
  const el = {
    id, textContent: "", value: "", checked: false, disabled: false,
    title: "", scrollTop: 0, className: "", style: {}, dataset: {},
    _listeners: {}, _htmlWrites: [],
    classList: {
      _s: {},
      add(c) { this._s[c] = 1; },
      remove(c) { delete this._s[c]; },
      contains(c) { return !!this._s[c]; },
      toggle(c, on) {
        if (on === undefined) { this._s[c] ? delete this._s[c] : (this._s[c] = 1); }
        else if (on) { this._s[c] = 1; } else { delete this._s[c]; }
      }
    },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    getAttribute() { return null; },
    setAttribute() {},
    querySelectorAll() { return []; },
    appendChild() {},
    focus() {},
    click() { (this._listeners.click || []).forEach((fn) => fn.call(this, { target: this })); }
  };
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get() { return html; },
    set(v) { html = String(v); el._htmlWrites.push(html); }
  });
  return el;
}

const els = {};
const docHandlers = {};
const documentStub = {
  getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
  createElement(tag) { return makeEl("<" + tag + ">"); },
  querySelectorAll() { return []; },
  addEventListener(t, fn) { (docHandlers[t] = docHandlers[t] || []).push(fn); },
  hidden: false
};

/* ---------------- 网络 / 存储 / 定时器桩 ---------------- */
const fetchLog = [];
function overviewPayload() {
  return {
    ok: true,
    data: {
      containers: [{
        name: "clash", repo: "metacubex/mihomo", tag: "latest",
        state: "running", status: "Up 3 days (healthy)",
        id: "9f2a1c0b7d33", ipaddr: "192.168.3.4",
        local_pulled: "09-20 10:00"
      }],
      images_count: 8,
      config: {
        router_url: "192.168.3.1", username: "admin", password_set: true,
        version: "1.3.0", registries: ["https://docker.1ms.run"], dry_run: false
      },
      server: { overview: { docker_version: "28.3.3", status: 1 } },
      check: {}, history: [],
      settings: { pull_timeout: 1800, schedules: [] },
      mirrors: []
    }
  };
}
function fetchStub(url) {
  fetchLog.push(url);
  let payload;
  if (url.indexOf("/api/check") === 0) payload = { ok: true, job_id: "job-1" };
  else if (url.indexOf("/api/overview") === 0) payload = overviewPayload();
  else payload = { ok: true, data: [] };
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(payload)
  });
}
const ls = {
  _d: {},
  getItem(k) { return k in this._d ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); }
};

const ES = [];
function FakeEventSource(url) {
  this.url = url; this.readyState = 0; this._h = {};
  this.addEventListener = (t, f) => { (this._h[t] = this._h[t] || []).push(f); };
  this.close = () => { this.readyState = 2; };
  ES.push(this);
}
FakeEventSource.prototype.emit = function (t, obj) {
  (this._h[t] || []).forEach((f) => f({ data: JSON.stringify(obj) }));
};

/* ---------------- 跑起来 ---------------- */
const ctx = {
  document: documentStub,
  location: { search: "?autorefresh=" + AUTO_SEC },
  localStorage: ls,
  fetch: fetchStub,
  EventSource: FakeEventSource,
  navigator: {},
  console,
  alert() {},
  setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, JSON, Date, Math, Set, Object, Array, String, Number,
  parseInt, parseFloat, isNaN
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(SRC, ctx, { filename: "app.js" });

const btnAuto = () => els["btnAuto"];
const label = () => els["autoLabel"].textContent;
const ctWrites = () => els["ctBody"]._htmlWrites.length;
const overviewFetches = () => fetchLog.filter((u) => u.indexOf("/api/overview") === 0).length;

(async function main() {
  console.log("\n=== A. 初始状态与开关 ===");
  await sleep(120);
  ok("首次加载拉了 /api/overview", overviewFetches() === 1, fetchLog);
  ok("首次渲染后表格有内容", els["ctBody"]._htmlWrites.some((h) => h.indexOf("clash") >= 0));
  ok("按钮显示自动刷新倒计时", /^自动刷新 [1-5]s$/.test(label()), label());
  ok("按开启状态，圆点未置灰", !btnAuto().classList.contains("off"), label());

  btnAuto().click();
  ok("点一下暂停：文案变「已暂停」", label() === "已暂停", label());
  ok("点一下暂停：圆点置灰", btnAuto().classList.contains("off"));
  ok("暂停状态写入 localStorage", ls.getItem("ikuai_auto_refresh") === "0",
     ls.getItem("ikuai_auto_refresh"));

  const n0 = overviewFetches();
  await sleep(AUTO_SEC * 1000 + 700);
  ok("暂停期间不再自动刷新", overviewFetches() === n0,
     { before: n0, after: overviewFetches() });

  btnAuto().click();
  ok("再点一下恢复", label() !== "已暂停" && !btnAuto().classList.contains("off"), label());
  ok("恢复后 localStorage 记为 1", ls.getItem("ikuai_auto_refresh") === "1");

  console.log("\n=== B. 到点自动刷新 ===");
  const n1 = overviewFetches();
  const w1 = ctWrites();
  await sleep(AUTO_SEC * 1000 + 900);
  ok("到点自动刷新了一次", overviewFetches() === n1 + 1,
     { before: n1, after: overviewFetches() });
  ok("静默刷新不重绘表格（数据没变就不碰 DOM）", ctWrites() === w1,
     { before: w1, after: ctWrites() });
  ok("「刷新于」时间有更新", /^刷新于 \d\d:\d\d:\d\d$/.test(els["lastRefresh"].textContent),
     els["lastRefresh"].textContent);
  ok("刷新后倒计时重新起算", /^自动刷新 [1-5]s$/.test(label()), label());

  console.log("\n=== C. 暂停/恢复状态可跨会话保留 ===");
  ok("存的是字符串 1/0（下次打开据此初始化）",
     ls.getItem("ikuai_auto_refresh") === "1");

  console.log("\n=== D. 后台标签页顺延，切回来补刷 ===");
  documentStub.hidden = true;
  const n2 = overviewFetches();
  await sleep(AUTO_SEC * 1000 + 700);
  ok("页面在后台时不刷新", overviewFetches() === n2,
     { before: n2, after: overviewFetches() });

  documentStub.hidden = false;
  (docHandlers["visibilitychange"] || []).forEach((fn) => fn());
  await sleep(300);
  ok("切回前台且数据已过期 → 立即补刷一次", overviewFetches() === n2 + 1,
     { before: n2, after: overviewFetches() });

  console.log("\n=== E. 执行任务期间让路 ===");
  els["btnCheck"].click();
  await sleep(400);
  ok("提交后进入执行态，按钮显示「执行中…」", label() === "执行中…", label());
  const n3 = overviewFetches();
  await sleep(AUTO_SEC * 1000 + 700);
  ok("任务执行中不自动刷新（不打扰执行面板）", overviewFetches() === n3,
     { before: n3, after: overviewFetches() });

  const es = ES[ES.length - 1];
  ok("前端连上了任务事件流", !!es && es.url.indexOf("/stream") > 0,
     es && es.url);
  es.emit("done", { ok: true, message: "完成" });
  await sleep(1800);
  ok("任务结束后自动刷新恢复计时", /^自动刷新 [1-5]s$/.test(label()), label());
  const n4 = overviewFetches();
  await sleep(AUTO_SEC * 1000 + 900);
  ok("任务结束后照常自动刷新", overviewFetches() === n4 + 1,
     { before: n4, after: overviewFetches() });

  console.log("\n=== F. 手动刷新会重置计时 ===");
  const n5 = overviewFetches();
  els["btnRefresh"].click();
  await sleep(250);
  ok("点「刷新」立即拉一次", overviewFetches() === n5 + 1,
     { before: n5, after: overviewFetches() });
  ok("手动刷新后倒计时重新起算", /^自动刷新 [1-5]s$/.test(label()), label());

  console.log("\n----------------------------------------");
  console.log("通过 " + PASS + " 项，失败 " + FAIL + " 项");
  process.exit(FAIL ? 1 : 0);
})().catch((e) => {
  console.error("测试脚本异常：", e);
  process.exit(1);
});
