#!/usr/bin/env node
/*
 * grab_web_cred.js —— 网页一次授权，换可自动续期的签到凭据
 * ---------------------------------------------------------------------------
 * 做四件事：
 *   1. 打开独立浏览器（独立 profile，不碰用户日常浏览器），进入 workbuddy.cn
 *   2. 等用户完成登录（最多 WAIT_MIN 分钟），按可靠性从高到低取令牌：
 *        a. 页面真实业务请求里的 Authorization: Bearer 头（最准）
 *        b. localStorage / sessionStorage 里的 access / refresh 令牌
 *        c. cookie（含 HttpOnly）里名字像令牌、且 JWT 未过期的值
 *   3. 优先用 refreshToken（可自动续期）→ claim.py --setup-rt；否则回落 --setup-web
 *   4. 最后跑一次只读查询验证链路
 *
 * 安全边界：只读该页面自身存储与该 profile 的 cookie，不读本机文件、不读其它站点；
 *          令牌只写本地 state/（claim.py 负责 0600），不打印、不上传。
 */
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const os = require("os");

// playwright-core 定位：环境变量 PW_CORE 优先；否则在 WorkBuddy 自带 node 里逐个版本查找。
function findPlaywrightCore() {
  if (process.env.PW_CORE) return process.env.PW_CORE;
  const roots = [
    path.join(os.homedir(), ".workbuddy", "binaries", "node", "versions"),
    path.join(path.resolve(__dirname, "..", "..", ".."), "node_modules"),
  ];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    for (const name of fs.readdirSync(root)) {
      const cands = [
        path.join(root, name, "node_modules", "@playwright", "cli", "node_modules", "playwright-core"),
        path.join(root, name, "playwright-core"),
        path.join(root, name),
      ];
      for (const c of cands) {
        try {
          if (fs.existsSync(path.join(c, "package.json"))) return c;
        } catch (_) { /* 忽略不可读目录 */ }
      }
    }
  }
  return "playwright-core"; // 交给 node 按常规解析（全局或项目内安装）
}
const CORE = findPlaywrightCore();
const { chromium } = require(CORE);

const SKILL_DIR = path.resolve(__dirname, "..");
const PROFILE = path.join(SKILL_DIR, "state", "browser-profile");
const OUT = path.join(SKILL_DIR, "state", "web-cred.code");

// claim.py 定位：环境变量 CLAIM_PY 优先；否则依次找兄弟技能目录、同项目的 scripts/
function findClaim() {
  if (process.env.CLAIM_PY) return process.env.CLAIM_PY;
  const cands = [
    path.join(os.homedir(), ".workbuddy", "skills", "workbuddy-daily-claim", "scripts", "claim.py"),
    path.join(SKILL_DIR, "..", "..", "workbuddy-daily-claim", "scripts", "claim.py"),
    path.join(SKILL_DIR, "claim.py"),
  ];
  for (const c of cands) {
    try {
      if (fs.existsSync(c)) return path.resolve(c);
    } catch (_) { /* ignore */ }
  }
  return cands[0];
}
const CLAIM = findClaim();
const WAIT_MIN = Number(process.env.WAIT_MIN || 15);
// 直接用全页登录页：支持手机号验证码 / 微信扫码（iframe 弹窗版加载慢且常卡白屏）
const START_URL = process.env.START_URL ||
  "https://www.workbuddy.cn/login/?platform=website&redirect_uri=https%3A%2F%2Fwww.workbuddy.cn%2F&product=workbuddy";
const PY = process.env.PYTHON || "python";
const WB_HOME = process.env.WB_ACCOUNTS_HOME ||
  path.join(require("os").homedir(), ".workbuddy", "wb-accounts");

const JWT_RE = /^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}/;
const ACCESS_KEYS = ["accessToken", "access_token", "authToken", "id_token", "idToken",
  "token", "jwt", "bearer", "Authorization", "wb_token", "userToken", "wbAccessToken"];
const REFRESH_KEYS = ["refreshToken", "refresh_token", "wbRefreshToken", "rt"];
// OAuth 流程里这些 cookie 长得像 JWT，但不是令牌
const BAD_COOKIE = /(state|nonce|csrf|verifier|challenge|checker|session_?id|fingerprint)/i;
const GOOD_COOKIE = /(access|auth|token|bearer|jwt|ticket)/i;

function jwtPayload(t) {
  try {
    const p = t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(Buffer.from(p, "base64").toString("utf8"));
  } catch (e) { return null; }
}
function looksLikeAccess(t) {
  if (typeof t !== "string" || !JWT_RE.test(t)) return false;
  const c = jwtPayload(t);
  if (!c) return false;
  if (typeof c.exp === "number" && c.exp * 1000 < Date.now()) return false;
  return true;
}

const READ = `(() => {
  const A = ${JSON.stringify(ACCESS_KEYS)}, R = ${JSON.stringify(REFRESH_KEYS)};
  const stores = [window.localStorage, window.sessionStorage];
  const out = { access: null, refresh: null };
  for (const k of A) { for (const s of stores) {
    try { const v = (s.getItem(k) || "").trim().replace(/^Bearer\\s+/i, "");
      if (/^eyJ[A-Za-z0-9_-]{10,}\\.eyJ/.test(v)) { out.access = { key: k, token: v }; break; } } catch (e) {}
    if (out.access) break;
  } if (out.access) break; }
  for (const k of R) { for (const s of stores) {
    try { const v = (s.getItem(k) || "").trim();
      if (v && v.length > 16) { out.refresh = { key: k, token: v }; break; } } catch (e) {}
    if (out.refresh) break;
  } if (out.refresh) break; }
  if (!out.access) {
    for (const s of stores) {
      try {
        for (let i = 0; i < s.length; i++) {
          const k = s.key(i), v = (s.getItem(k) || "").trim();
          if (/^eyJ[A-Za-z0-9_-]{10,}\\.eyJ/.test(v)) { out.access = { key: k, token: v }; break; }
        }
      } catch (e) {}
      if (out.access) break;
    }
  }
  return out;
})()`;

function stamp(m) { console.log("[grab] " + m); }

// 记录这份网页凭据属于哪个账号：面板靠 uid 判断「哪个号已配通道」
async function recordOwner(page, kind) {
  try {
    if (!/workbuddy\.cn\/?$/.test(new URL(page.url()).pathname + "")) {
      await page.goto("https://www.workbuddy.cn/", { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {});
      await page.waitForTimeout(1500);
    }
    const text = await page.evaluate(() => document.body.innerText || "").catch(() => "");
    const m = text.match(/(?:ID|账号\s*ID)\s*[:：]\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
    if (!m) { stamp("未能从页面识别登录账号 uid（不影响凭据保存，面板按未归属处理）"); return; }
    fs.mkdirSync(WB_HOME, { recursive: true });
    fs.writeFileSync(path.join(WB_HOME, "web-owner.json"),
      JSON.stringify({ uid: m[1], kind, at: new Date().toISOString(), source: "web-login" }, null, 2));
    stamp("凭据归属：uid=" + m[1].slice(0, 8) + "…（已记录到 web-owner.json）");
  } catch (e) {
    stamp("归属识别失败：" + (e && e.message ? e.message : e));
  }
}

(async () => {
  fs.mkdirSync(path.join(SKILL_DIR, "state"), { recursive: true });

  // 单实例锁：防止两个授权窗口同时用一个浏览器 profile
  // （Chrome 会把第二个实例静默委派给第一个，导致 Playwright 报 Target closed）
  const LOCK = path.join(SKILL_DIR, "state", "auth.lock");
  const acquireLock = () => {
    try {
      const fd = fs.openSync(LOCK, "wx");
      fs.writeSync(fd, String(process.pid));
      fs.closeSync(fd);
      return true;
    } catch (e) { return false; }
  };
  if (!acquireLock()) {
    let age = -1;
    try { age = Date.now() - fs.statSync(LOCK).mtimeMs; } catch (e) {}
    if (age >= 0 && age < (WAIT_MIN + 5) * 60 * 1000) {
      stamp("已有一个授权窗口在运行（" + Math.round(age / 1000) + " 秒前启动）。请直接在已弹出的浏览器窗口完成登录（手机号验证码或扫码），不要重复打开。");
      process.exit(2);
    }
    // 锁过期（上次异常残留）→ 清掉重试
    try { fs.unlinkSync(LOCK); } catch (e) {}
    if (!acquireLock()) { stamp("FATAL 无法创建锁文件 " + LOCK); process.exit(1); }
  }
  process.on("exit", () => { try { fs.unlinkSync(LOCK); } catch (e) {} });

  // 换号登录模式（WB_FRESH_LOGIN=1 或 --fresh）：清除浏览器里的旧登录会话。
  // 否则脚本检测到上次的有效 cookie 会「校验通过立即收尾」，窗口几秒就自动关闭，
  // 用户根本来不及登录新账号。
  // 注意：不能删 profile 目录（本环境的批量文件删除会被安全钩子拦下，实测失败），
  // 改为启动后用 Playwright API 清 cookie + 站点存储，效果等同且不碰文件系统删除。
  const FRESH = process.env.WB_FRESH_LOGIN === "1" || process.argv.includes("--fresh");

  const launch = async () => chromium.launchPersistentContext(PROFILE, {
    headless: false,
    channel: process.env.PW_CHANNEL || "chrome",
    viewport: { width: 1280, height: 900 },
    args: ["--no-first-run", "--no-default-browser-check"],
  });
  let ctx;
  try {
    ctx = await launch();
  } catch (e) {
    // 常见原因：profile 被残留 Chrome 进程占用。提示而不是裸堆栈。
    if (/closed/i.test(String(e && e.message))) {
      stamp("浏览器启动失败（profile 可能被残留进程占用）。可重试一次；若反复出现，关闭所有使用该 profile 的 Chrome 窗口后再试。");
    }
    throw e;
  }

  let hit = null, refreshHit = null, sessionCk = null;
  // 手机号验证码 / 纯 HttpOnly 会话登录：页面从头到尾都不发 Bearer。
  // 旧逻辑只能死等 15 分钟超时才收尾，而且只记第一次（往往是匿名）会话 cookie。
  // 现在：每轮取最新会话 cookie，用状态接口实测，通过即认定登录完成并立刻收尾。
  let sessionReady = false, lastProbe = 0, probedSecret = "";
  const run = (args) => spawnSync(PY, [CLAIM, ...args], { encoding: "utf8", timeout: 180000 });
  const packCode = (secret, agent) => {
    const pack = { schema: 1, kind: "cookie", secret, agent, host: "www.workbuddy.cn" };
    return "WBCRED1#" + Buffer.from(JSON.stringify(pack), "utf8").toString("base64").replace(/=+$/, "");
  };
  // 用户手动关掉浏览器窗口时，及时感知并退出，别傻等占用锁
  let browserClosed = false;
  ctx.on("close", () => { browserClosed = true; });

  // (a) 页面真实请求头：最可靠的信号
  ctx.on("request", (req) => {
    try {
      const h = req.headers();
      const auth = h["authorization"] || h["Authorization"];
      if (!auth) return;
      const m = String(auth).match(/^Bearer\s+(.+)$/i);
      if (m && looksLikeAccess(m[1].trim())) {
        hit = hit || { key: "header:" + req.url().slice(0, 70), token: m[1].trim() };
      }
    } catch (e) {}
  });
  // 登录/刷新接口响应体里可能直接带 refreshToken
  ctx.on("response", async (res) => {
    try {
      const ct = (res.headers()["content-type"] || "").toLowerCase();
      if (!ct.includes("json")) return;
      const u = res.url();
      if (!/workbuddy|copilot|tencent|qq\.com/i.test(u)) return;
      const txt = await res.text();
      if (!txt || txt.length > 2_000_000) return;
      const rm = txt.match(/"[Rr]efresh_?[Tt]oken"\s*:\s*"([^"]{16,})"/);
      if (rm && !refreshHit) refreshHit = { key: "net:" + u.slice(0, 60), token: rm[1] };
    } catch (e) {}
  });

  if (FRESH) {
    stamp("换号模式：清除窗口内的旧登录会话…");
    try { await ctx.clearCookies(); stamp("已清除全部 cookie"); }
    catch (e) { stamp("clearCookies 失败：" + (e && e.message ? e.message : e)); }
    try {
      const p0 = ctx.pages()[0] || (await ctx.newPage());
      await p0.goto("https://www.workbuddy.cn/", { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {});
      await p0.evaluate(() => {
        try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}
      }).catch(() => {});
      stamp("已清除站点本地存储");
    } catch (e) { stamp("清存储失败：" + (e && e.message ? e.message : e)); }
    stamp("旧会话已清除：窗口将打开全新登录页，请用新账号登录");
  }

  const page = ctx.pages()[0] || (await ctx.newPage());
  await page.goto(START_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  stamp("浏览器已打开：" + START_URL + "（手机号验证码 / 扫码均可；登录完成自动收尾，最多等 " + WAIT_MIN + " 分钟）");

  const deadline = Date.now() + WAIT_MIN * 60 * 1000;
  while (Date.now() < deadline && !hit && !sessionReady) {
    if (browserClosed) {
      stamp("浏览器窗口已被手动关闭，本次授权中止。重新打开授权窗口即可再试。");
      process.exit(5);
    }
    for (const p of ctx.pages()) {
      try {
        const r = await p.evaluate(READ);
        if (!hit && r && r.access && looksLikeAccess(r.access.token)) hit = r.access;
        if (!refreshHit && r && r.refresh) refreshHit = r.refresh;
      } catch (e) {}
    }
    if (!hit) {
      try {
        const cks = await ctx.cookies();
        for (const c of cks) {
          const v = (c.value || "").trim();
          if (BAD_COOKIE.test(c.name)) continue;
          if (GOOD_COOKIE.test(c.name) && looksLikeAccess(v)) {
            hit = { key: "cookie:" + c.name, token: v }; break;
          }
        }
        // 每轮都刷新会话 cookie：登录页会先下发匿名 session，登录成功后服务端
        // 换发新值。旧代码 `!sessionCk` 只记第一次（匿名态），导致超时后存进去
        // 的永远是无效凭据，校验 401 —— 这正是「登录了却显示未配置」的根因。
        const sess = cks.filter((c) => /^(session|session_2)$/.test(c.name) && c.value);
        if (sess.length) sessionCk = sess.map((c) => c.name + "=" + c.value).join("; ");
      } catch (e) {}
    }
    // 无 Bearer 的登录（手机号验证码 / 纯 HttpOnly 会话）：会话 cookie 一旦通过
    // 服务端校验就说明登录完成，立即收尾，不必干等满 WAIT_MIN 分钟。
    if (!hit && sessionCk && sessionCk !== probedSecret && Date.now() - lastProbe > 6000) {
      probedSecret = sessionCk;
      lastProbe = Date.now();
      const agent = await page.evaluate(() => navigator.userAgent).catch(() => "");
      const code = packCode(sessionCk, agent);
      const pr = run(["--setup-web", code]);  // claim.py 先校验、通过才落盘
      if (pr.status === 0) {
        fs.writeFileSync(OUT, code, { encoding: "utf8" });
        sessionReady = true;
        stamp("网页会话已通过状态接口校验（手机号验证码等无 Bearer 登录）——立即收尾");
      } else {
        stamp("会话 cookie 尚未生效（多半是登录页的匿名态），继续等待登录完成…");
      }
    }
    if (!hit && !sessionReady) await new Promise((r) => setTimeout(r, 2000));
  }

  if (sessionReady) {
    await recordOwner(page, "cookie");
    await ctx.close().catch(() => {});
    process.exit(0);
  }

  if (!hit) {
    // 官网新版无 Bearer：退而求其次，用业务会话 cookie 走凭据码
    if (sessionCk) {
      stamp("超时兜底：未发现 Bearer，用最后一次会话 cookie 再试一次凭据通道");
      const agent = await page.evaluate(() => navigator.userAgent).catch(() => "");
      const code = packCode(sessionCk, agent);
      fs.writeFileSync(OUT, code, { encoding: "utf8" });
      const r = run(["--setup-web", code]);
      const o = ((r.stdout || "") + (r.stderr || "")).trim();
      stamp("setup-web(cookie) 退出码 " + r.status + "：" + o.split("\n").slice(-2).join(" | "));
      if (r.status === 0) await recordOwner(page, "cookie");
      await ctx.close().catch(() => {});
      process.exit(r.status === 0 ? 0 : 4);
    }
    stamp("TIMEOUT 未检测到有效令牌（可能登录未完成，或页面只用 HttpOnly 会话、不向前端暴露 Bearer）");
    await ctx.close().catch(() => {});
    process.exit(3);
  }
  const claims = jwtPayload(hit.token) || {};
  stamp("检测到 access：来源=" + hit.key + "，长度=" + hit.token.length +
        "，exp=" + (claims.exp ? new Date(claims.exp * 1000).toISOString().slice(0, 10) : "?") +
        "，aud=" + String(claims.aud || claims.iss || "-").slice(0, 40));
  stamp("refresh：" + (refreshHit ? "有（" + refreshHit.key + "）" : "未发现"));

  if (refreshHit) {
    const r = run(["--setup-rt", refreshHit.token]);
    const o = ((r.stdout || "") + (r.stderr || "")).trim();
    stamp("setup-rt 退出码 " + r.status + "：" + o.split("\n").slice(-2).join(" | "));
    if (r.status === 0) {
      stamp("OK 已保存 refreshToken，可自动续期");
      await recordOwner(page, "jwt");
      await ctx.close().catch(() => {});
      process.exit(0);
    }
    stamp("refreshToken 不被接受，回落网页会话凭据");
  }

  const agent = await page.evaluate(() => navigator.userAgent).catch(() => "");
  const host = await page.evaluate(() => location.origin).catch(() => START_URL);
  const pack = { schema: 1, kind: "jwt", secret: hit.token, agent, host };
  const code = "WBCRED1#" + Buffer.from(JSON.stringify(pack), "utf8").toString("base64").replace(/=+$/, "");
  fs.writeFileSync(OUT, code, { encoding: "utf8" });

  const r = run(["--setup-web", code]);
  const o = ((r.stdout || "") + (r.stderr || "")).trim();
  stamp("setup-web 退出码 " + r.status + "：" + o.split("\n").slice(-2).join(" | "));
  if (r.status === 0) await recordOwner(page, "jwt");
  await ctx.close().catch(() => {});
  process.exit(r.status === 0 ? 0 : 4);
})().catch((e) => {
  stamp("FATAL " + (e && e.message ? e.message : e));
  process.exit(1);
});
