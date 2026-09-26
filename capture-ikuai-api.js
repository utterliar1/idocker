/* =====================================================================
 * 爱快 API 抓包脚本  (capture-ikuai-api.js)
 * ---------------------------------------------------------------------
 * 用途：把爱快后台发往 /Action/ 的所有请求完整记录下来，导出成 JSON，
 *       供 ikuai_docker_updater.py 当作接口模板使用。
 *
 * 用法：
 *   1. 浏览器登录爱快后台（4.0 / 3.7 都适用）
 *   2. 按 F12 打开开发者工具，切到「Console / 控制台」
 *   3. 把整个文件的内容粘贴进去，回车
 *   4. 看到 "[iKuai] 抓包已启动" 后，在页面里手动操作一遍：
 *        镜像管理 → 下载/拉取一个新版本镜像
 *        容器列表 → 停止、删除旧容器
 *        容器列表 → 新建容器（配置和原来一样），保存并启用
 *   5. 回到 Console 执行：  __IKUAI_CAP__.dump()
 *      → 自动下载 ikuai-api-capture.json
 *   6. 把下载到的 JSON 放到本工具目录，执行：
 *        python ikuai_docker_updater.py learn ikuai-api-capture.json
 *      → 自动生成 config.json 的接口模板
 * ===================================================================== */
(function () {
  if (window.__IKUAI_CAP__) {
    console.log("%c[iKuai] 抓包已经在运行中，用 __IKUAI_CAP__.dump() 导出即可", "color:#c60");
    return;
  }

  var records = [];
  var PREFIX = "/Action/";

  function pickHeaders(h) {
    var out = {};
    try {
      if (!h) return out;
      if (typeof Headers !== "undefined" && h instanceof Headers) {
        h.forEach(function (v, k) { out[k] = v; });
      } else if (Array.isArray(h)) {
        h.forEach(function (p) { out[p[0]] = p[1]; });
      } else {
        Object.keys(h).forEach(function (k) { out[k] = h[k]; });
      }
    } catch (e) {}
    return out;
  }

  function record(url, method, headers, body) {
    var u = String(url || "");
    if (u.indexOf(PREFIX) === -1) return;
    records.push({
      url: u,
      method: String(method || "POST").toUpperCase(),
      headers: headers || {},
      body: body === undefined || body === null ? null : String(body),
      time: Date.now()
    });
    console.log(
      "%c[iKuai #" + records.length + "]%c " + method + " " + u,
      "color:#0a0;font-weight:bold", "color:inherit",
      body ? "\n   " + String(body).slice(0, 500) : ""
    );
  }

  /* ---- hook fetch ---- */
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        var url = typeof input === "string" ? input : (input && input.url);
        var method = (init && init.method) || (input && input.method) || "GET";
        record(url, method, pickHeaders(init && init.headers), init && init.body ? String(init.body) : null);
      } catch (e) {}
      return origFetch.apply(this, arguments);
    };
  }

  /* ---- hook XMLHttpRequest ---- */
  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;
  var origSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function (m, u) {
    this.__ik = { method: m, url: u, headers: {} };
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (this.__ik) this.__ik.headers[k] = v; } catch (e) {}
    return origSetHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (b) {
    try {
      if (this.__ik) {
        record(this.__ik.url, this.__ik.method, this.__ik.headers, b === undefined || b === null ? null : String(b));
      }
    } catch (e) {}
    return origSend.apply(this, arguments);
  };

  window.__IKUAI_CAP__ = {
    records: records,
    /* 导出并下载 JSON */
    dump: function (filename) {
      var payload = {
        exported_at: new Date().toISOString(),
        page: location.href,
        count: records.length,
        records: records
      };
      var txt = JSON.stringify(payload, null, 2);
      try {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([txt], { type: "application/json" }));
        a.download = (filename || "ikuai-api-capture") + ".json";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } catch (e) {
        console.warn("[iKuai] 自动下载失败，请手动复制下面返回的文本");
      }
      console.log("%c[iKuai] 已导出 " + records.length + " 条记录", "color:#0a0;font-weight:bold");
      return txt;
    },
    /* 只打印，不下载 */
    show: function () {
      records.forEach(function (r, i) {
        console.log("#" + (i + 1) + " " + r.method + " " + r.url + "\n" + r.body);
      });
      return records;
    },
    clear: function () {
      records.length = 0;
      console.log("[iKuai] 记录已清空");
    }
  };

  console.log(
    "%c[iKuai] 抓包已启动%c\n" +
    "  1) 现在去页面里完整操作一遍：下载镜像 → 停止/删除容器 → 新建容器 → 启用\n" +
    "  2) 完成后执行：__IKUAI_CAP__.dump()   （自动下载 JSON）\n" +
    "  3) 调试用：__IKUAI_CAP__.show() / __IKUAI_CAP__.clear()",
    "color:#0a0;font-weight:bold;font-size:14px", "color:inherit;font-size:12px"
  );
})();
