/* ============================================================
   前端「使用者体验」回归测试
   ------------------------------------------------------------
   零依赖：手写 DOM 桩 + vm 直接跑 webapp/app/static/app.js。
   覆盖本轮为「打开就知道该不该更新」做的改动：

     · 概览条四种状态（未检测 / 有可更新 / 全部最新 / 检测中）
     · 可更新的容器置顶 + 行高亮 + 按钮变主色
     · 一键更新 / 单个更新前必须先确认，取消则不发请求
     · 打开页面自动检测（过期才跑、不弹执行面板、不重复触发）
     · ?autocheck=0 能彻底关掉自动检测
     · 轻提示 toast 收到结果文案

   与 test_frontend.js 分开是为了各管一摊：那边盯自动刷新的时序，
   这边盯交互与状态呈现，改坏哪块一眼能看出是哪边。

   跑法：  node _test/test_frontend_ux.js
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP = path.join(__dirname, "..", "webapp", "app", "static", "app.js");
const SRC = fs.readFileSync(APP, "utf8");

let PASS = 0, FAIL = 0;
function ok(name, cond, extra) {
  if (cond) { PASS++; console.log("  \u2713 " + name); }
  else {
    FAIL++;
    console.log("  \u2717 " + name + (extra !== undefined ? "  \u2192 " + JSON.stringify(extra) : ""));
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- DOM 桩（比基础版多记 childNodes，才能断言 toast） ---------------- */
function makeEl(id) {
  const el = {
    id, textContent: "", value: "", checked: false, disabled: false,
    title: "", scrollTop: 0, className: "", style: {}, dataset: {},
    childNodes: [], _listeners: {}, _htmlWrites: [],
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
    appendChild(c) { this.childNodes.push(c); return c; },
    removeChild(c) {
      const i = this.childNodes.indexOf(c);
      if (i >= 0) this.childNodes.splice(i, 1);
      return c;
    },
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

const CONTAINERS = [
  { name: "clash", repo: "metacubex/mihomo", tag: "latest",
    state: "running", status: "Up 3 days (healthy)", id: "9f2a1c0b7d33",
    ipaddr: "192.168.3.4", local_pulled: "09-20 10:00" },
  { name: "lucky", repo: "gdy666/lucky", tag: "2.27.2",
    state: "running", status: "Up 2 days (healthy)", id: "1a2b3c4d5e6f",
    ipaddr: "192.168.3.5", local_pulled: "09-25 08:11" },
  { name: "adguard", repo: "adguard/adguardhome", tag: "latest",
    state: "running", status: "Up 6 hours", id: "77aa88bb99cc",
    ipaddr: "192.168.3.6", local_pulled: "09-19 22:40" }
];

// 两个有新版本（clash / adguard）、一个已最新（lucky）
const CHECKED = {
  checked_at: Math.floor(Date.now() / 1000) - 60,
  at_str: "2026-09-26 19:50:00",
  items: {
    "metacubex/mihomo:latest": { status: "newer" },
    "gdy666/lucky:2.27.2": { status: "latest" },
    "adguard/adguardhome:latest": { status: "newer" }
  }
};

/* ---------------- 建一个独立的运行环境 ---------------- */
function boot(opts) {
  opts = opts || {};
  const els = {}, docHandlers = {}, fetches = [], sources = [];
  const documentStub = {
    getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement(tag) { return makeEl("<" + tag + ">"); },
    querySelectorAll() { return []; },
    addEventListener(t, fn) { (docHandlers[t] = docHandlers[t] || []).push(fn); },
    hidden: false
  };
  function overviewPayload() {
    return {
      ok: true,
      data: {
        containers: CONTAINERS,
        images_count: 9,
        config: {
          router_url: "192.168.3.1", username: "admin", password_set: true,
          version: "1.4.0", registries: ["https://docker.1ms.run"], dry_run: false,
          auth_enabled: true
        },
        server: { overview: { docker_version: "28.3.3", status: 1 } },
        check: opts.check === undefined ? CHECKED : opts.check,
        history: [],
        settings: { pull_timeout: 900, schedules: [] },
        mirrors: ["https://docker.1ms.run"]
      }
    };
  }
  function fetchStub(url, init) {
    fetches.push({ url, method: (init && init.method) || "GET",
                   body: init && init.body ? JSON.parse(init.body) : null });
    let payload;
    if (url.indexOf("/api/overview") === 0) payload = overviewPayload();
    else if (url.indexOf("/api/check") === 0) payload = { ok: true, job_id: "job-check" };
    else if (url.indexOf("/api/update") === 0) payload = { ok: true, job_id: "job-update" };
    else if (url.indexOf("/api/history") === 0) payload = { ok: true, data: [] };
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
  function FakeEventSource(url) {
    this.url = url; this.readyState = 0; this._h = {};
    this.addEventListener = (t, f) => { (this._h[t] = this._h[t] || []).push(f); };
    this.close = () => { this.readyState = 2; };
    sources.push(this);
  }
  FakeEventSource.prototype.emit = function (t, obj) {
    (this._h[t] || []).forEach((f) => f({ data: JSON.stringify(obj) }));
  };
  FakeEventSource.prototype.emitRaw = function (t, raw) {
    (this._h[t] || []).forEach((f) => f({ data: raw }));
  };

  const ctx = {
    document: documentStub,
    location: { search: opts.search || "?autorefresh=5" },
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
  return {
    els, fetches, sources, docHandlers, ls,
    el: (id) => els[id] || (els[id] = makeEl(id)),
    calls: (prefix) => fetches.filter((f) => f.url.indexOf(prefix) === 0),
    lastHtml: (id) => {
      const w = (els[id] || {})._htmlWrites || [];
      return w.length ? w[w.length - 1] : "";
    }
  };
}

(async function main() {
  /* ============================================================ */
  console.log("\n=== A. 还没检测过：不谎报「最新」，给出检测入口 ===");
  {
    const app = boot({ check: {} });
    await sleep(150);
    ok("概览条提示尚未检测",
       /尚未检测上游版本/.test(app.el("updStat").textContent), app.el("updStat").textContent);
    ok("没有可更新项时不显示「一键更新」按钮",
       app.el("btnUpdateAll").classList.contains("hidden"));
    ok("概览条有「检测版本」按钮且可用", app.el("btnCheckInline").disabled === false);
  }

  /* ============================================================ */
  console.log("\n=== B. 检测过：一眼看到有几个要更新 ===");
  let B;
  {
    B = boot({});
    await sleep(150);
    const stat = B.el("updStat").textContent;
    ok("概览条报出可更新数量", /有 2 个容器可以更新/.test(stat), stat);
    ok("按钮文案带上数量", B.el("btnUpdateAll").textContent === "更新这 2 个",
       B.el("btnUpdateAll").textContent);
    ok("有可更新项时按钮不再隐藏",
       !B.el("btnUpdateAll").classList.contains("hidden"));
    ok("概览条状态用醒目的 newer 样式",
       B.el("updStat").className === "upd-stat newer", B.el("updStat").className);

    const html = B.lastHtml("ctBody");
    ok("可更新的行加了 need-upd 高亮",
       (html.match(/class="need-upd"/g) || []).length === 2,
       (html.match(/class="need-upd"/g) || []).length);
    ok("可更新的行排在已最新的前面",
       html.indexOf("adguard") < html.indexOf("lucky") &&
       html.indexOf("clash") < html.indexOf("lucky"));
    ok("可更新那行的「更新」按钮变主色",
       /btn tiny primary ct-upd" data-name="adguard"/.test(html), html.slice(0, 400));
    ok("已最新的行按钮保持普通样式",
       /btn tiny ct-upd" data-name="lucky"/.test(html));
  }

  /* ============================================================ */
  console.log("\n=== C. 一键更新：先确认，再执行 ===");
  {
    const before = B.calls("/api/update").length;
    B.el("btnUpdateAll").click();
    await sleep(60);
    ok("点「一键更新」先弹确认框，没有直接开跑",
       !B.el("cfmModal").classList.contains("hidden") &&
       B.calls("/api/update").length === before);
    const body = B.el("cfmBody").innerHTML;
    ok("确认框列出了要更新的容器", body.indexOf("adguard") >= 0 && body.indexOf("clash") >= 0);
    ok("确认框说明了会重启、服务会短暂中断",
       /重启一次/.test(body) && /中断/.test(body), body);
    ok("确认按钮是危险色（提醒这不是只读操作）",
       B.el("cfmOk").className === "btn danger", B.el("cfmOk").className);

    B.el("cfmCancel").click();
    await sleep(60);
    ok("点「取消」不发更新请求", B.calls("/api/update").length === before);
    ok("点「取消」关掉确认框", B.el("cfmModal").classList.contains("hidden"));

    B.el("btnUpdateAll").click();
    await sleep(60);
    B.el("cfmOk").click();
    await sleep(120);
    const calls = B.calls("/api/update");
    ok("点「开始更新」才真的发请求", calls.length === before + 1);
    ok("请求里带的正是那两个可更新容器",
       calls.length && calls[calls.length - 1].body.containers.join(",") === "adguard,clash",
       calls.length && calls[calls.length - 1].body);
    ok("更新任务连上了事件流",
       B.sources.some((s) => s.url.indexOf("job-update") > 0));
  }

  /* ============================================================ */
  console.log("\n=== D. 打开页面自动检测（结果过期时） ===");
  {
    const app = boot({ check: {}, search: "?autorefresh=5&autocheck=60" });
    await sleep(200);
    ok("刚打开还没急着检测", app.calls("/api/check").length === 0);
    await sleep(1000);
    ok("过一会儿自动发起了版本检测", app.calls("/api/check").length === 1,
       app.calls("/api/check").length);
    ok("自动检测走的是静默模式：完全不碰执行面板",
       app.el("execTitle").textContent === "" && app.el("log").innerHTML === "",
       { title: app.el("execTitle").textContent, log: app.el("log").innerHTML });
    ok("但概览条会显示「正在检测」",
       /正在检测上游版本/.test(app.el("updStat").textContent), app.el("updStat").textContent);
    ok("检测期间按钮变「检测中…」且禁用",
       app.el("btnCheck").textContent === "检测中…" && app.el("btnCheck").disabled === true);
    ok("检测期间不会重复发起",
       app.calls("/api/check").length === 1);

    // 模拟检测结果回来
    const es = app.sources[app.sources.length - 1];
    es.emitRaw("snapshot", JSON.stringify({ id: "job-check", kind: "check", targets: [] }));
    es.emit("check_result", { results: [
      { image: "metacubex/mihomo:latest", container: "clash", status: "newer" },
      { image: "gdy666/lucky:2.27.2", container: "lucky", status: "latest" },
      { image: "adguard/adguardhome:latest", container: "adguard", status: "latest" }
    ]});
    es.emit("done", { ok: true, message: "已检查 3 个镜像" });
    await sleep(200);

    ok("检测完成后按钮恢复", app.el("btnCheck").textContent === "检查更新" &&
       app.el("btnCheck").disabled === false, app.el("btnCheck").textContent);
    ok("概览条按最新结果更新为「有 1 个」",
       /有 1 个容器可以更新/.test(app.el("updStat").textContent), app.el("updStat").textContent);
    ok("轻提示告诉用户检测结果",
       app.el("toastWrap").childNodes.length > 0 &&
       /版本检测完成/.test(app.el("toastWrap").childNodes[0].textContent),
       app.el("toastWrap").childNodes.map((n) => n.textContent));
  }

  /* ============================================================ */
  console.log("\n=== E. ?autocheck=0 能彻底关掉自动检测 ===");
  {
    const app = boot({ check: {}, search: "?autorefresh=5&autocheck=0" });
    await sleep(1400);
    ok("关闭后不会自动检测", app.calls("/api/check").length === 0,
       app.calls("/api/check").length);
    app.el("btnCheckInline").click();
    await sleep(200);
    ok("手动点「检测版本」仍然可用", app.calls("/api/check").length === 1);
    ok("手动检测会打开执行面板（对照：自动检测不会）",
       app.el("execTitle").textContent === "正在检测上游版本",
       app.el("execTitle").textContent);
  }

  /* ============================================================ */
  console.log("\n=== F. 全部最新时的结论 ===");
  {
    const allLatest = {
      checked_at: Math.floor(Date.now() / 1000) - 30,
      at_str: "2026-09-26 19:55:00",
      items: {
        "metacubex/mihomo:latest": { status: "latest" },
        "gdy666/lucky:2.27.2": { status: "latest" },
        "adguard/adguardhome:latest": { status: "latest" }
      }
    };
    const app = boot({ check: allLatest, search: "?autorefresh=5&autocheck=0" });
    await sleep(150);
    ok("全部最新时给出肯定结论",
       /全部 3 个容器都已是最新/.test(app.el("updStat").textContent),
       app.el("updStat").textContent);
    ok("全部最新时用 ok 样式", app.el("updStat").className === "upd-stat ok",
       app.el("updStat").className);
    ok("全部最新时不显示一键更新",
       app.el("btnUpdateAll").classList.contains("hidden"));
  }

  /* ============================================================ */
  console.log("\n=== G. 查询失败时如实说明 ===");
  {
    const someFailed = {
      checked_at: Math.floor(Date.now() / 1000) - 30,
      at_str: "2026-09-26 19:55:00",
      items: {
        "metacubex/mihomo:latest": { status: "newer" },
        "gdy666/lucky:2.27.2": { status: "failed" },
        "adguard/adguardhome:latest": { status: "latest" }
      }
    };
    const app = boot({ check: someFailed, search: "?autorefresh=5&autocheck=0" });
    await sleep(150);
    ok("部分查询失败时同时报出两个数字",
       /有 1 个容器可以更新/.test(app.el("updStat").textContent) &&
       /1 个查询失败/.test(app.el("updStat").textContent),
       app.el("updStat").textContent);
  }

  /* ============================================================ */
  console.log("\n=== H. 容器消失后选中态不残留（不对幽灵容器发请求）===");
  {
    const app = boot({ search: "?autorefresh=5&autocheck=0" });
    await sleep(150);
    app.el("checkAll").checked = true;
    app.el("checkAll")._listeners.change.forEach((fn) => fn.call(app.el("checkAll")));
    await sleep(50);
    ok("全选后显示 3 个选中",
       app.el("btnUpdateSelected").textContent === "更新选中 (3)",
       app.el("btnUpdateSelected").textContent);

    // 模拟 clash 被删掉，然后手动刷新总览
    const idx = CONTAINERS.findIndex((c) => c.name === "clash");
    const removed = CONTAINERS.splice(idx, 1)[0];
    app.el("btnRefresh").click();
    await sleep(200);
    ok("容器消失后选中数自动收缩为 2（残留键被清掉）",
       app.el("btnUpdateSelected").textContent === "更新选中 (2)",
       app.el("btnUpdateSelected").textContent);

    const before = app.calls("/api/update").length;
    app.el("btnUpdateSelected").click();
    await sleep(50);
    app.el("cfmOk").click();
    await sleep(120);
    const calls = app.calls("/api/update");
    ok("更新请求只含仍存在的容器，不含已删除的 clash",
       calls.length === before + 1 &&
       calls[calls.length - 1].body.containers.indexOf("clash") < 0,
       calls.length && calls[calls.length - 1].body);
    CONTAINERS.splice(idx, 0, removed);      // 还原，避免影响其他用例
  }

  console.log("\n----------------------------------------");
  console.log("通过 " + PASS + " 项，失败 " + FAIL + " 项");
  process.exit(FAIL ? 1 : 0);
})().catch((e) => {
  console.error("测试脚本异常：", e);
  process.exit(1);
});
