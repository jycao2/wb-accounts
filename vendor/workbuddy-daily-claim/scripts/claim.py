#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日积分领取（读本机登录态版，纯标准库）。

读取本机已登录 WorkBuddy 桌面的登录态文件，取 Bearer accessToken，
直接调后端接口完成：Buddy加油站每日签到 + 派猫猫旅行（先领后派）。

令牌获取（按优先级，成功后写入本地缓存、下次优先用缓存）：
  1. 环境变量 WORKBUDDY_ACCESS_TOKEN —— 用户显式提供的合法 Bearer 令牌（最高优先，覆盖缓存）。
  2. 本地缓存文件 ~/.workbuddy/skills/workbuddy-daily-claim/state/token.json（权限 600）。
     每日任务主路径：命中且令牌未过期即直接使用。access token 实测有效期约 55 天，
     故缓存通常跨数十日有效，真实负担仅约每 55 天补一次令牌。
  3. 本机登录态文件 workbuddy-desktop.info 中的明文 auth.accessToken
     （明文与否以文件格式为准：JWT 字符串=明文；dict 信封=加密，与操作系统/版本号无关）。

落盘令牌若已被客户端信封加密（auth.accessToken 为 dict 而非字符串），本脚本无法
读取，会提示用户运行 `python3 claim.py setup`，按屏幕提示粘贴一次 Bearer 令牌即可
（校验通过后写入缓存，后续约两个月免维护）。

用法：
  python3 claim.py                      # 每日播报（积分/连续天数/旅行状态）
  python3 claim.py checkin --travel-auto  # 每日签到 + 猫猫旅行全自动闭环
  python3 claim.py --json                # 输出 JSON
  python3 claim.py setup                # 首次配置：粘贴一次 Bearer 令牌（加密态用户必做）

登录态文件（明文 JSON，含 auth.accessToken / auth.domain）：
  Linux : $XDG_DATA_HOME/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
          （即 ~/.local/share/CodeBuddyExtension/...；XDG data home，非 ~/.config）
  macOS : ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
  Win   : %APPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
          （次选 %LOCALAPPDATA%\\...；两个都探测）
  兜底  : ~/.workbuddy/auth/workbuddy-desktop.info
  覆盖  : 环境变量 WORKBUDDY_DESKTOP_INFO 直接指定路径

跨平台：本脚本仅用标准库（os/platform/json/urllib），按当前系统优先探测，
再回退遍历三平台全部候选路径，Linux / macOS / Windows 均可直接运行。

接口（2026-09-22 实测全部 HTTP 200 / code 0；domain 取自登录态 auth.domain）：
  状态   POST /v2/billing/meter/checkin-activity-status
  签到   POST /v2/billing/meter/daily-checkin            （幂等：已签返回 code 10001）
  旅行状态 GET /activity/growth/buddy/travel/status
  旅行配置 GET /activity/growth/buddy/travel/config
  派发   POST /activity/growth/buddy/travel/depart  {"location_id": N}
  领取   POST /activity/growth/buddy/travel/claim    {}

安全：只读登录态文件，绝不打印/外传 accessToken；写操作仅 3 个已验证接口，
且旅行写操作仅在显式 --travel-auto 时执行、派出前必查 daily_limit_reached。
"""

import base64
import json
import os
import re
import sys
import time
import platform
import random
import urllib.error
import urllib.request

# 登录态信封解密模块（同目录）；缺失时信封态不可解密，但明文/缓存/刷新令牌通道仍可用
try:
    import login_crypto
except Exception:
    login_crypto = None

AUTH_FILENAME = "workbuddy-desktop.info"
EXTENSION_AUTH = os.path.join("CodeBuddyExtension", "Data", "Public", "auth")
USER_AUTH_DIR = os.path.join(".workbuddy", "auth")

CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"

# 账户资源（真实「积分余额」，即客户端账号页显示的那个带小数的数，如 530.19）接口。
# 该接口只在 copilot.tencent.com + Bearer 下可用；www.workbuddy.cn 或 cookie 通道会返回 401。
RESOURCE_HOST = "copilot.tencent.com"
USER_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"

TRAVEL_STATE_TEXT = {
    "idle": "空闲（可派遣）",
    "traveling": "旅行中",
    "arrived": "已到达（待领取）",
}


def candidate_dirs():
    """按当前系统优先返回候选目录，再回退遍历三平台全部可能位置。"""
    home = os.path.expanduser("~")
    system = platform.system()
    dirs = []
    if system == "Darwin":
        dirs.append(os.path.join(home, "Library", "Application Support", EXTENSION_AUTH))
    elif system == "Windows":
        for env_key in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if base:
                dirs.append(os.path.join(base, EXTENSION_AUTH))
    else:  # Linux 等 POSIX —— 实测为 XDG data home，非 ~/.config
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        dirs.append(os.path.join(xdg, EXTENSION_AUTH))
        dirs.append(os.path.join(home, ".config", EXTENSION_AUTH))
    # 三平台通用回退（用户可能装在非标准路径）
    dirs.append(os.path.join(home, ".local", "share", EXTENSION_AUTH))
    dirs.append(os.path.join(home, "Library", "Application Support", EXTENSION_AUTH))
    for env_key in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_key)
        if base:
            dirs.append(os.path.join(base, EXTENSION_AUTH))
    dirs.append(os.path.join(home, USER_AUTH_DIR))
    seen, ordered = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def find_login_state():
    candidates = candidate_dirs()
    for d in candidates:
        p = os.path.join(d, AUTH_FILENAME)
        if os.path.isfile(p):
            return p
    # 也支持带时间戳的备份文件（取修改时间最新的一个）
    import glob
    backups = []
    for d in candidates:
        backups += glob.glob(os.path.join(d, AUTH_FILENAME + ".*"))
    if backups:
        return max(backups, key=os.path.getmtime)
    return None


def _read_login_state(path):
    """读取登录态文件，返回 (domain, accessToken)。文件缺失/损坏返回 (None, None)。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None, None
    auth = data.get("auth", {}) if isinstance(data, dict) else {}
    return auth.get("domain"), auth.get("accessToken")


def _jwt_is_workbuddy(token):
    """判断 JWT 是否为本工作区（workbuddy.cn）的会话令牌。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return isinstance(claims, dict) and "workbuddy.cn" in str(claims.get("iss", ""))
    except Exception:
        return False


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _jwt_exp(token):
    claims = _jwt_claims(token)
    return claims.get("exp") if isinstance(claims, dict) else None


def _domain_from_token(token):
    """从 JWT iss 推断后端域名（env 提供令牌、登录态文件缺失 domain 时兜底）。"""
    claims = _jwt_claims(token)
    iss = claims.get("iss") if isinstance(claims, dict) else None
    if isinstance(iss, str):
        m = re.match(r"https?://([^/]+)", iss)
        if m:
            return m.group(1)
    return None


def _token_accepted(domain, token):
    """用只读状态接口验证令牌是否被后端接受。"""
    st, body = api_call(domain, token, CHECKIN_STATUS_PATH)
    return st == 200 and isinstance(body, dict) and body.get("code") == 0


def _normalize_bearer(raw):
    """从环境变量值中规整出裸令牌（容忍用户带上 'Bearer ' 前缀）。"""
    raw = (raw or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


# ---- 本地令牌缓存（每日任务主路径，避免每日重取登录态 / 不碰进程内存） ----
CACHE_PATH = os.path.expanduser(
    "~/.workbuddy/skills/workbuddy-daily-claim/state/token.json"
)
# 软过期阈值：距 exp 不足 1 小时即视作失效，强制重取（自愈被吊销/快过期令牌）
CACHE_SOFT_EXPIRE_LEEWAY = 3600


def load_cache():
    """读取本地令牌缓存，返回 (token, domain) 或 None。"""
    if not os.path.isfile(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as handle:
            d = json.load(handle)
    except Exception:
        return None
    tok = d.get("accessToken")
    dom = d.get("domain")
    if not tok or not dom:
        return None
    exp = _jwt_exp(tok)
    if exp is not None and exp < time.time() + CACHE_SOFT_EXPIRE_LEEWAY:
        return None  # 已软过期
    return tok, dom


def save_cache(domain, token):
    """原子写入本地令牌缓存（权限 600）。"""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(
            {"domain": domain, "accessToken": token, "cached_at": int(time.time())},
            handle,
            ensure_ascii=False,
        )
    os.chmod(tmp, 0o600)
    os.replace(tmp, CACHE_PATH)


# ---- 长效令牌自续期（主通道）：refresh_token 经官方插件网关换新接口令牌 ----
# 相比读登录态，本通道：客户端不开也能用、不依赖登录态是否加密、不扫任何进程/文件。
RT_PATH = os.path.expanduser(
    "~/.workbuddy/skills/workbuddy-daily-claim/state/rt.json"
)
REFRESH_URL = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"


def read_rt():
    if not os.path.isfile(RT_PATH):
        return None
    try:
        with open(RT_PATH, "r", encoding="utf-8") as h:
            d = json.load(h)
    except Exception:
        return None
    tok = d.get("rt")
    return tok.strip() if isinstance(tok, str) and tok.strip() else None


def save_rt(rt, source="manual"):
    os.makedirs(os.path.dirname(RT_PATH), exist_ok=True)
    tmp = RT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump({"v": 1, "rt": rt, "source": source, "saved_at": int(time.time())},
                  h, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, RT_PATH)


def delete_rt():
    if os.path.isfile(RT_PATH):
        with open(RT_PATH, "w", encoding="utf-8") as h:
            h.write("{}")
        return True
    return False


def rt_info():
    rt = read_rt()
    if not rt:
        return {"configured": False}
    return {"configured": True, "token_masked": rt[:8] + "..." + rt[-4:]}


def refresh_access_token(rt):
    """用长效 refresh token 经官方插件网关换取新接口令牌。返回 dict（原始应答）。"""
    req = urllib.request.Request(REFRESH_URL, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return {"code": -1, "msg": "HTTP " + str(exc.code)}
    except Exception as exc:  # noqa: BLE001
        return {"code": -1, "msg": str(exc)}


def token_via_refresh():
    """主通道：用已保存 refresh token 换新接口令牌并轮换回写。返回 (token, domain) 或 None。"""
    rt = read_rt()
    if not rt:
        return None
    r = refresh_access_token(rt)
    if r.get("code") != 0 or not isinstance(r.get("data"), dict):
        return None
    data = r["data"]
    at = data.get("accessToken") or ""
    new_rt = data.get("refreshToken") or ""
    if not at:
        return None
    if new_rt and new_rt != rt:
        try:
            save_rt(new_rt, source="refresh")  # 写失败不影响本次，旧 rt 仍可用兜底
        except Exception:
            pass
    domain = _domain_from_token(at) or "www.workbuddy.cn"
    return at, domain


def export_rt(save=False):
    """从本机登录态导出 refresh token（用户显式发起）。明文直取；信封需 login_crypto+密钥。"""
    path = os.environ.get("WORKBUDDY_DESKTOP_INFO")
    if not path or not os.path.isfile(path):
        path = find_login_state()
    if not path:
        sys.stderr.write("未找到本机登录态文件，请先登录 WorkBuddy 客户端。\n")
        return 1
    try:
        with open(path, "r", encoding="utf-8") as h:
            auth = json.load(h).get("auth", {})
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("读取登录态失败：%s\n" % exc)
        return 1
    raw = auth.get("refreshToken")
    if raw is None:
        sys.stderr.write("登录态缺少 refreshToken 字段。\n")
        return 1
    try:
        if login_crypto is not None:
            rt = login_crypto.resolve_field(raw)
        elif isinstance(raw, str):
            rt = raw
        else:
            sys.stderr.write("登录态 refreshToken 已信封加密，但缺少 login_crypto 解密模块。\n")
            return 1
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("解析 refreshToken 失败：%s\n" % exc)
        return 1
    if save:
        save_rt(rt, source="export")
        print(json.dumps({"status": "ok", "action": "rt_saved",
                          "token_masked": rt[:8] + "..." + rt[-4:], "file": RT_PATH,
                          "note": "长效令牌已保存本机(0600)，之后运行即可自动续期领积分。"},
                         ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(rt + "\n")
        sys.stderr.write("[提示] 以上为长效令牌明文（等同账号钥匙），请妥善保管、勿外传。\n")
    return 0


def setup_rt(value):
    value = (value or "").strip()
    if not (len(value) >= 200 and value.count(".") == 2):
        sys.stderr.write("录入失败：内容不像有效长效令牌（应为较长 base64、含两个点号）。\n")
        return 1
    save_rt(value, source="manual")
    print(json.dumps({"status": "ok", "action": "rt_saved",
                      "token_masked": value[:8] + "..." + value[-4:], "file": RT_PATH},
                     ensure_ascii=False, indent=2))
    return 0


# ---- 网页会话授权（备用通道）：本机无登录态时，用浏览器一次性授权的 bearer ----
# 思路（自写实现，仅借鉴"页面取令牌+UA"做法）：用户在本机没有登录态、只有浏览器登录时，
# 用 templates/web_cred_snippet.js 在已登录页面取自机 bearer + User-Agent + 域名，
# 打成 WBCRED1# 凭据码交给本脚本；保存前必须打过一次只读状态接口校验，通过才落盘(0600)。
WEB_CRED_PATH = os.path.expanduser(
    "~/.workbuddy/skills/workbuddy-daily-claim/state/web-cred.json"
)
WEB_CODE_PREFIX = "WBCRED1#"
_SESSION_UA = [None]  # 命中网页 bearer 时回放其 UA（官方网关会话与 UA 绑定）
_AUTH_COOKIE = [None]  # 网页会话为 HttpOnly-cookie 时，命中后在此记录 cookie 串
_COOKIE_ONLY = [False]  # WEB-ONLY 模式：仅 cookie 会话，绝不借用全局 Bearer 缓存（防多号串数据）


def _cookie_accepted(domain, cookie):
    """用 cookie 直接打一次只读状态接口，code==0 视为有效。"""
    cred = {"kind": "cookie", "secret": cookie, "agent": _SESSION_UA[0] or "", "host": domain}
    return _web_cred_valid(cred, domain)


def _b64url_decode_loose(text):
    s = re.sub(r"\s+", "", text or "")
    if s.startswith(WEB_CODE_PREFIX):
        s = s[len(WEB_CODE_PREFIX):]
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * ((-len(s)) % 4))


def _decode_web_code(code):
    """解析 WBCRED1# 凭据码 -> dict{kind, secret, agent, host}。"""
    code = (code or "").strip()
    if not code.startswith(WEB_CODE_PREFIX):
        raise ValueError("不是有效的网页凭据码（应以 %s 开头）" % WEB_CODE_PREFIX)
    obj = json.loads(_b64url_decode_loose(code).decode("utf-8"))
    kind, secret = obj.get("kind"), obj.get("secret")
    if kind not in ("jwt", "cookie") or not secret:
        raise ValueError("凭据码缺少 kind/secret")
    return {"kind": kind, "secret": secret,
            "agent": obj.get("agent") or "", "host": obj.get("host") or ""}


def _parse_curl(text):
    """从 Copy as cURL 文本提取 bearer/cookie + User-Agent + host。"""
    text = text or ""
    m = re.search(r"Authorization:\s*Bearer\s+([^\r\n'\"]+)", text, re.I)
    if m:
        kind, secret = "jwt", m.group(1).strip()
    else:
        mc = re.search(r"Cookie:\s*([^\r\n'\"]+)", text, re.I)
        if not mc:
            raise ValueError("cURL 中未找到 Authorization: Bearer 或 Cookie 头")
        kind, secret = "cookie", mc.group(1).strip()
    ua = re.search(r"User-Agent:\s*([^\r\n'\"]+)", text, re.I) or \
        re.search(r"(?:-A|--user-agent)\s+['\"]?([^'\"\r\n]+)", text, re.I)
    host = re.search(r"https?://([^/'\"\s]+)", text, re.I)
    return {"kind": kind, "secret": secret,
            "agent": (ua.group(1).strip() if ua else ""),
            "host": (host.group(1) if host else "")}


def _web_request_headers(cred):
    headers = {"Content-Type": "application/json"}
    if cred.get("agent"):
        headers["User-Agent"] = cred["agent"]
    if cred.get("kind") == "jwt":
        headers["Authorization"] = "Bearer " + cred["secret"]
    else:
        headers["Cookie"] = cred["secret"]
    return headers


def _web_cred_domain(cred):
    host = (cred.get("host") or "").replace("https://", "").replace("http://", "").split("/")[0]
    return host or "www.workbuddy.cn"


def _web_cred_valid(cred, domain):
    url = "https://" + domain + CHECKIN_STATUS_PATH
    req = urllib.request.Request(url, data=b"{}", method="POST")
    for k, v in _web_request_headers(cred).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            return isinstance(body, dict) and body.get("code") == 0
    except Exception:
        return False


def save_web_cred(cred, source):
    os.makedirs(os.path.dirname(WEB_CRED_PATH), exist_ok=True)
    tmp = WEB_CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump({"v": 1, "source": source, "saved_at": int(time.time()), "cred": cred},
                  h, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, WEB_CRED_PATH)


def read_web_cred():
    if not os.path.isfile(WEB_CRED_PATH):
        return None
    try:
        with open(WEB_CRED_PATH, "r", encoding="utf-8") as h:
            c = json.load(h).get("cred")
        if isinstance(c, dict) and c.get("secret"):
            return c
    except Exception:
        return None
    return None


def delete_web_cred():
    if os.path.isfile(WEB_CRED_PATH):
        with open(WEB_CRED_PATH, "w", encoding="utf-8") as h:
            h.write("{}")
        return True
    return False


def web_cred_info():
    c = read_web_cred()
    if not c:
        return {"configured": False}
    sec = c.get("secret", "")
    return {"configured": True, "kind": c.get("kind"),
            "secret_masked": (sec[:6] + "..." + sec[-4:]) if len(sec) > 12 else "****",
            "has_agent": bool(c.get("agent"))}


def setup_web(cred, source):
    """保存前必须校验：能在我们域名打过只读状态接口才落盘，否则明确告知、不保存。"""
    dom = _web_cred_domain(cred)
    if not (_web_cred_valid(cred, dom) or _web_cred_valid(cred, "www.workbuddy.cn")):
        sys.stderr.write(
            "校验未通过：该网页凭据无法在 %s / www.workbuddy.cn 打过状态接口。\n"
            "可能：会话过期、账号与客户端不一致、或网页仅 HttpOnly-cookie 无可用 bearer。\n"
            "请改用在接口请求上 Copy as cURL（含 Authorization: Bearer）的方式；未保存。\n"
            % dom)
        return 1
    save_web_cred(cred, source)
    print(json.dumps({"status": "ok", "action": "web_cred_saved",
                      "kind": cred.get("kind"), "file": WEB_CRED_PATH},
                     ensure_ascii=False, indent=2))
    return 0


def _print_setup_guide(encrypted, path):
    """首次/令牌不可用时，打印傻瓜式取令牌步骤。"""
    out = sys.stdout
    out.write("\n")
    out.write("=" * 60 + "\n")
    if encrypted:
        out.write("你的 WorkBuddy 登录态是【加密】的（新版客户端会加密，属正常）。\n")
        out.write("只需下面 3 步、约 1 分钟，配好一次，之后约两个月内每天自动领。\n")
    else:
        out.write("没有找到可用的登录令牌。跟着下面 3 步、约 1 分钟配好，之后约两个月内每天自动领。\n")
    out.write("=" * 60 + "\n\n")
    out.write("【第 1 步】在已登录 WorkBuddy 的浏览器里打开开发者工具：\n")
    out.write("  · Chrome/Edge：按 F12（或 Ctrl+Shift+I / Cmd+Option+I）\n")
    out.write("  · 切到顶部的 Network（网络）标签\n\n")
    out.write("【第 2 步】在过滤框输入 checkin，然后刷新页面，点开任意一个请求，\n")
    out.write("  在 Request Headers（请求标头）里找到 Authorization 这一行，\n")
    out.write("  把 Bearer 后面那串长令牌整段复制下来（可直接双击复制）。\n\n")
    out.write("【第 3 步】回到这里，运行下面这条命令并把令牌粘进去（末尾回车）：\n\n")
    out.write("    python3 claim.py setup\n\n")
    out.write("  粘贴时它不会回显，属正常，粘贴完直接回车即可。\n")
    out.write("  成功后它会自动校验、存好，并立刻帮你把今天的积分领了。\n")
    if encrypted:
        out.write("\n小提示：你不必非得找令牌。若你更愿意，也可以直接在本机用\n")
        out.write("scripts/local/claim_mem.py 一次性提取后写进缓存。\n")
    out.write("\n配好一次后，令牌有效期约 55 天，到期前再跑一次 setup 即可。\n")
    out.write("=" * 60 + "\n")


def _read_token_interactively():
    """从终端读取用户粘贴的 Bearer 令牌（回显关闭由调用方尽力处理）。"""
    try:
        import getpass

        return _normalize_bearer(getpass.getpass("请粘贴 Bearer 令牌（不回显，直接粘贴后回车）："))
    except Exception:
        try:
            return _normalize_bearer(input("请粘贴 Bearer 令牌："))
        except Exception:
            return ""


def cmd_setup():
    """交互式配置：粘贴一次 Bearer 令牌 -> 校验 -> 写缓存 -> 立即闭环领分。"""
    print("=" * 60)
    print("WorkBuddy 积分助手 · 一次性配置")
    print("=" * 60)
    print("只需粘贴一次登录令牌（约 1 分钟），之后约两个月内每天自动领积分。")
    print("如何拿令牌：浏览器打开已登录的 WorkBuddy → F12 开发者工具 → Network 标签")
    print("→ 过滤框输入 checkin → 刷新页面 → 点开请求 → Request Headers 里复制")
    print("Authorization 中 Bearer 后面那串长令牌。\n")
    token = os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    if token:
        token = _normalize_bearer(token)
        print("检测到环境变量 WORKBUDDY_ACCESS_TOKEN，直接使用它。\n")
    else:
        token = _read_token_interactively()
    if not token:
        print("\n未收到令牌，配置未完成。可按上面步骤重新运行：python3 claim.py setup\n")
        return 1
    domain = _domain_from_token(token) or "www.workbuddy.cn"
    if not (_jwt_is_workbuddy(token) or _token_accepted(domain, token)):
        print("\n❌ 这个令牌没通过校验（可能复制不全、过期或不是 WorkBuddy 的）。")
        print("请重新运行：python3 claim.py setup 再试一次。\n")
        return 1
    save_cache(domain, token)
    print("✅ 令牌校验通过，已安全保存（权限 600）。")
    exp = _jwt_exp(token)
    if isinstance(exp, int):
        days = max(0, (exp - int(time.time())) // 86400)
        print("   该令牌有效期约 %d 天，到期前再跑一次 setup 即可。" % days)
    print("   已写入：%s\n" % CACHE_PATH)
    print("现在帮你把今天的积分领了：")
    return run_once(travel_auto_on=True)


def run_once(travel_auto_on, location_id=None, as_json=False, as_checkin=False):
    """执行一次完整流程，返回进程退出码。"""
    override = os.environ.get("WORKBUDDY_DESKTOP_INFO")
    path = override if override and os.path.isfile(override) else find_login_state()
    try:
        token, domain = load_credentials(path)
    except Exception as exc:  # noqa: BLE001
        # 区分「加密态」与「其它失败」，给出对应傻瓜引导
        msg = str(exc)
        encrypted = ("信封加密" in msg) or ("加密" in msg and "accessToken" in msg)
        if encrypted:
            sys.stderr.write("读取登录态失败：%s\n" % msg)
            _print_setup_guide(True, path)
        else:
            sys.stderr.write("读取登录态失败：%s\n" % msg)
            _print_setup_guide(False, path)
        return 1

    try:
        data, travel_raw, auto_log, account_balance = collect(domain, token, travel_auto_on, location_id)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("%s\n" % exc)
        return 1

    if as_checkin:
        result = do_checkin(domain, token)
        if as_json:
            out = {"checkin_result": result, "total_credits": data.get("total_credits"),
                   "account_balance": account_balance,
                   "streak_days": data.get("streak_days"), "today_checked_in": data.get("today_checked_in"),
                   "travel": travel_raw}
            if auto_log:
                out["travel_auto_log"] = auto_log
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(render(data, travel_raw, auto_log, result, account_balance))
        return 0 if result.get("action") != "error" else 1

    if as_json:
        out = {"total_credits": data.get("total_credits"), "account_balance": account_balance,
               "streak_days": data.get("streak_days"),
               "today_checked_in": data.get("today_checked_in"), "travel": travel_raw}
        if auto_log:
            out["travel_auto_log"] = auto_log
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(data, travel_raw, auto_log, None, account_balance))
    return 0


def load_credentials(path):
    """解析出 (token, domain)。

    优先级：环境变量 WORKBUDDY_ACCESS_TOKEN > 本地缓存 > 本机登录态明文文件。
    任意来源成功取得后写入本地缓存（chmod 600），下次运行优先用缓存。
    落盘令牌若被客户端信封加密（accessToken 为 dict）且未提供任何令牌，则抛清晰错误（exit 1）。
    """
    # 0) WEB-ONLY：多账号场景（wb-accounts run-as）下，该账号只有自己的网页会话凭据、
    #    没有自己的 Bearer。此时必须跳过下面所有 Bearer 通道（env / 全局缓存 / refresh /
    #    本机登录态），因为 state/token.json 是全局唯一一份、属于另一个账号；若不跳过，
    #    就会把别的号的积分、签到、旅行状态张冠李戴地显示到这个号上。
    if os.environ.get("WORKBUDDY_CLAIM_WEB_ONLY") == "1":
        wc = read_web_cred()
        if not wc:
            raise ValueError("该账号只有网页会话通道，但未找到 state/web-cred.json；"
                             "请在面板对该账号执行一次「打开授权窗口」登录。")
        _SESSION_UA[0] = wc.get("agent") or None
        dom = (_domain_from_token(wc["secret"]) if wc.get("kind") == "jwt" else "") \
            or _web_cred_domain(wc)
        if wc.get("kind") == "jwt":
            sys.stderr.write("[auth] WEB-ONLY：使用网页会话 bearer，不使用全局 Bearer 缓存。\n")
            return wc["secret"], dom
        if not _cookie_accepted(dom, wc["secret"]):
            raise ValueError("该账号的网页会话 cookie 已失效，请重新登录授权。")
        _AUTH_COOKIE[0] = wc["secret"]
        _COOKIE_ONLY[0] = True
        sys.stderr.write("[auth] WEB-ONLY：使用网页会话 cookie（已校验），不使用全局 Bearer 缓存。\n")
        return wc["secret"], dom

    # 1) 环境变量显式令牌（显式覆盖，最高优先）
    env_raw = os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    if env_raw:
        tok = _normalize_bearer(env_raw)
        if tok:
            domain, _ = _read_login_state(path)
            if not domain:
                domain = _domain_from_token(tok)
            if domain and (_jwt_is_workbuddy(tok) or _token_accepted(domain, tok)):
                sys.stderr.write(
                    "[auth] 使用环境变量 WORKBUDDY_ACCESS_TOKEN 提供的令牌，已写入本地缓存。\n"
                )
                save_cache(domain, tok)
                return tok, domain
            sys.stderr.write(
                "[auth] 环境变量 WORKBUDDY_ACCESS_TOKEN 校验未通过，改走缓存/本机登录态。\n"
            )

    # 2) 本地缓存（命中即用）
    cached = load_cache()
    if cached:
        sys.stderr.write("[auth] 使用本地缓存令牌（%s）。\n" % CACHE_PATH)
        return cached

    # 3) 主通道 · 长效令牌自续期：缓存失效/缺失时，用 refresh token 经官方插件网关换新令牌
    rt_auth = token_via_refresh()
    if rt_auth:
        sys.stderr.write("[auth] 使用长效令牌刷新得到新接口令牌（主通道）。\n")
        save_cache(rt_auth[1], rt_auth[0])
        return rt_auth

    # 4) 备用通道 · 网页会话 bearer：本机无登录态时的浏览器授权兜底
    wc = read_web_cred()
    if wc and wc.get("kind") == "jwt":
        _SESSION_UA[0] = wc.get("agent") or None
        dom = _domain_from_token(wc["secret"]) or _web_cred_domain(wc)
        sys.stderr.write("[auth] 使用网页会话 bearer（备用通道）。\n")
        save_cache(dom, wc["secret"])
        return wc["secret"], dom
    if wc and wc.get("kind") == "cookie":
        # 官网新版改用 HttpOnly cookie 会话（无 Bearer）：cookie 直接通过状态接口校验后使用
        _SESSION_UA[0] = wc.get("agent") or None
        dom = _web_cred_domain(wc)
        if _cookie_accepted(dom, wc["secret"]):
            _AUTH_COOKIE[0] = wc["secret"]
            sys.stderr.write("[auth] 使用网页会话 cookie（备用通道，已通过只读校验）。\n")
            return wc["secret"], dom
        sys.stderr.write("[auth] 网页会话 cookie 已失效，跳过备用通道。\n")

    # 5) 本机登录态（明文直读 / 信封解密）
    if path is None:
        raise ValueError(
            "未找到本机登录态文件，且未提供缓存/环境变量令牌。"
            "请先在 WorkBuddy 客户端登录，或设置 WORKBUDDY_ACCESS_TOKEN，"
            "或把 WORKBUDDY_DESKTOP_INFO 指向登录态文件。"
        )
    domain, file_token = _read_login_state(path)
    if not domain:
        raise ValueError("登录态缺少 domain，请确认 WorkBuddy 客户端已正常登录")
    if not file_token:
        raise ValueError("登录态缺少 accessToken，请先在 WorkBuddy 客户端登录")
    if isinstance(file_token, dict):
        # 落盘为信封加密：以文件格式为准，与操作系统/版本号无关。
        raise RuntimeError(
            "本机登录态的 accessToken 落盘为信封加密（新版客户端会加密，属正常）。"
            "请运行 `python3 claim.py setup`，按屏幕提示粘贴一次 Bearer 令牌，"
            "配好之后约两个月内每天自动领。"
        )
    if not (_jwt_is_workbuddy(file_token) or _token_accepted(domain, file_token)):
        raise RuntimeError(
            "本机登录态明文令牌校验未通过，请重新登录客户端或改用环境变量令牌"
        )
    sys.stderr.write(
        "[auth] 使用本机登录态明文文件，已写入本地缓存（下次优先用缓存）。\n"
    )
    save_cache(domain, file_token)
    return file_token, domain


def api_call(domain, token, path, method="POST", payload=None):
    url = "https://" + domain + path
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        body = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, method=method, data=body)
    if _AUTH_COOKIE[0] and token == _AUTH_COOKIE[0]:
        req.add_header("Cookie", _AUTH_COOKIE[0])
    else:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    if _SESSION_UA[0]:
        req.add_header("User-Agent", _SESSION_UA[0])
    else:
        # 浏览器 UA 兜底：部分接口（如 copilot.tencent.com 的账户资源接口）会拒收 Python-urllib 默认 UA（403）
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return exc.code, {"code": -1, "msg": "HTTP " + str(exc.code)}
    except Exception as exc:  # noqa: BLE001
        return 0, {"code": -1, "msg": str(exc)}


def do_checkin(domain, token):
    """先查后签，幂等安全。"""
    st, body = api_call(domain, token, CHECKIN_STATUS_PATH)
    if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
        return {"action": "error", "message": "查询状态失败：HTTP %s, %s" % (st, body.get("msg"))}
    data = body.get("data", {}) or {}
    if data.get("today_checked_in"):
        return {"action": "skipped", "message": "今日已签到，无需重复打卡"}
    st2, chk = api_call(domain, token, CHECKIN_PATH)
    code = chk.get("code") if isinstance(chk, dict) else None
    msg = chk.get("msg") if isinstance(chk, dict) else None
    if st2 == 200 and code == 0:
        return {"action": "checked_in", "message": msg or "签到成功", "data": chk.get("data")}
    if code == 10001 and "已签到" in (msg or ""):
        return {"action": "skipped", "message": msg or "今日已签到"}
    return {"action": "error", "message": msg or ("HTTP %s" % st2), "code": code}


def travel_auto(domain, token, location_id=None):
    """派猫猫旅行全自动闭环（写操作）：先领后派。"""
    st, body = api_call(domain, token, TRAVEL_STATUS_PATH, method="GET")
    if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
        return {"success": False, "available": False, "log": ["查询旅行状态失败，已跳过全部写操作"]}
    status = body.get("data", {}) or {}
    log = []
    # 先领
    if status.get("state") == "arrived":
        st_c, c = api_call(domain, token, TRAVEL_CLAIM_PATH, method="POST", payload={})
        if st_c == 200 and isinstance(c, dict) and c.get("code") == 0:
            credit = (c.get("data") or {}).get("reward_credit")
            log.append("✅ 已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?"))
            st2, b2 = api_call(domain, token, TRAVEL_STATUS_PATH, method="GET")
            if st2 == 200 and isinstance(b2, dict) and b2.get("code") == 0:
                status = b2.get("data", {}) or {}
        else:
            log.append("领取失败：%s" % (c.get("msg") if isinstance(c, dict) else "HTTP %s" % st_c))
    # 后派
    if status.get("state") in (None, "", "idle"):
        if status.get("daily_limit_reached"):
            log.append("今日派遣次数已用完，跳过派遣")
        else:
            chosen = location_id
            if chosen is None:
                sc, cfg = api_call(domain, token, TRAVEL_CONFIG_PATH, method="GET")
                ids = []
                if sc == 200 and isinstance(cfg, dict) and cfg.get("code") == 0:
                    ids = [loc.get("id") for loc in (cfg.get("data", {}) or {}).get("locations", []) or []
                           if isinstance(loc, dict) and loc.get("id") is not None]
                if ids:
                    chosen = random.choice(ids)
            if chosen is None:
                log.append("未能获取可选地点，跳过派遣")
            else:
                st_d, dep = api_call(domain, token, TRAVEL_DEPART_PATH, method="POST", payload={"location_id": chosen})
                if st_d == 200 and isinstance(dep, dict) and dep.get("code") == 0:
                    loc = (dep.get("data") or {}).get("location") or {}
                    log.append("🚀 已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen)))
                else:
                    log.append("派出失败：%s" % (dep.get("msg") if isinstance(dep, dict) else "HTTP %s" % st_d))
    else:
        arrive = status.get("arrive_at")
        now = status.get("server_now")
        if isinstance(arrive, int) and isinstance(now, int) and arrive > now:
            sec = arrive - now
            h, m = divmod(sec, 3600)
            log.append("Buddy 正在旅行中，约 %d 小时 %d 分后到达，届时再运行一次即自动领取" % (h, m // 60))
        else:
            log.append("Buddy 正在旅行中，到达后可再运行一次自动领取")
    st3, b3 = api_call(domain, token, TRAVEL_STATUS_PATH, method="GET")
    final = b3.get("data", {}) if st3 == 200 and isinstance(b3, dict) and b3.get("code") == 0 else status
    return {"success": True, "available": True, "state": final.get("state"), "status": final, "log": log}


def fetch_account_balance(token):
    """用 Bearer 调账户资源接口，算客户端账号页的「积分余额」（带小数）。

    口径（两段式，2026-11-13 与客户端账号弹窗逐项实测核对）：
        平台奖励积分 = sum(CapacityRemainPrecise)  where CapacityType == 1
        套餐基础积分 = sum(CycleCapacityRemainPrecise)  where CapacityType != 1
        总剩余积分   = 平台奖励 + 套餐基础
    注意：非 1 类型的 CapacityRemainPrecise 是**总量池**（如 500），客户端不显示，
    绝不能把它当剩余量加进去；必须用 CycleCapacityRemainPrecise（本周期剩余）。
    旧口径（只加 CapacityType==1）会漏掉套餐部分，报 1600 而客户端显示 1700+。

    仅在 Bearer 通道有效：cookie / 网页会话通道调该接口会返回 401，此处容错返回 None，
    不影响签到活动积分等其它数据的正常展示。
    """
    try:
        st, body = api_call(RESOURCE_HOST, token, USER_RESOURCE_PATH)
    except Exception:
        return None
    if st != 200 or not isinstance(body, dict):
        return None
    inner = body.get("data") or {}
    resp = inner.get("Response") or {}
    d = resp.get("Data") or {}
    accs = d.get("Accounts") or []
    if not accs:
        return None
    total = 0.0
    for a in accs:
        if a.get("CapacityType") == 1:
            pv = a.get("CapacityRemainPrecise")
            if pv is None:
                pv = a.get("CapacityRemain") or 0
        else:
            pv = a.get("CycleCapacityRemainPrecise")
            if pv is None:
                continue  # 老条目无周期字段：宁缺勿错，不加总量池
        try:
            total += float(pv)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def collect(domain, token, travel_auto_on, location_id):
    """收集只读数据；travel_auto_on 为真时额外执行闭环。

    额外尝试账户资源接口（仅 Bearer 通道生效），拿真实「积分余额」。
    """
    st, body = api_call(domain, token, CHECKIN_STATUS_PATH)
    if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
        raise RuntimeError("查询 Buddy 加油站状态失败：HTTP %s, %s" % (st, body.get("msg")))
    data = body.get("data", {}) or {}
    travel_raw = None
    auto_log = None
    if travel_auto_on:
        res = travel_auto(domain, token, location_id)
        auto_log = res.get("log") or []
        travel_raw = res.get("status")
    else:
        stt, tt = api_call(domain, token, TRAVEL_STATUS_PATH, method="GET")
        travel_raw = tt.get("data", {}) if stt == 200 and isinstance(tt, dict) and tt.get("code") == 0 else None
    # cookie 会话拿不到账户资源接口（401），且 WEB-ONLY 下更不能借用别的号的 Bearer 去查
    account_balance = None if _COOKIE_ONLY[0] else fetch_account_balance(token)
    return data, travel_raw, auto_log, account_balance


def render(data, travel_raw, auto_log, checkin_result=None, account_balance=None):
    lines = []
    if checkin_result:
        a = checkin_result.get("action")
        lines.append("== WorkBuddy积分助手 · 自动签到 ==")
        if a == "checked_in":
            lines.append("签到结果：✅ 签到成功！%s" % (checkin_result.get("message") or ""))
        elif a == "skipped":
            lines.append("签到结果：⏭️ 今日已签到，无需重复打卡")
        else:
            lines.append("签到结果：❌ 失败 - %s" % (checkin_result.get("message") or "未知"))
        lines.append("")
    else:
        lines.append("== WorkBuddy积分助手 · 每日播报 ==")
    # 口径澄清：total_credits 是「Buddy 加油站每日签到活动积分」，并非客户端账号页的「积分余额」。
    # 客户端账号页那个（带小数、标体验版，如 530.19）来自 get-user-resource，见下方账户积分余额。
    lines.append("活动积分(累计)：%s" % data.get("total_credits"))
    if account_balance is not None:
        lines.append("账户积分余额：%s" % account_balance)
    lines.append("连续登录：%s 天" % data.get("streak_days"))
    if data.get("today_checked_in"):
        lines.append("今日记录：已记录（今日可得 %s 分）" % data.get("today_credit"))
    else:
        lines.append("今日记录：未记录（完成一次对话即可被记录登录 1 天）")
    if data.get("is_streak_day"):
        lines.append("连续奖励：今日为连续登录日")
    if data.get("next_streak_day"):
        lines.append("下一连续奖励节点：%s 天" % data.get("next_streak_day"))
    act = data.get("activity_name") or data.get("theme_name")
    if act:
        lines.append("本期活动：%s（%s ~ %s）" % (act, data.get("start_time"), data.get("end_time")))
    if data.get("claim_button_text"):
        lines.append("领取状态：%s" % data.get("claim_button_text"))

    if travel_raw:
        lines.append("")
        lines.append("派猫猫旅行：")
        state = travel_raw.get("state")
        lines.append("  旅行状态：%s" % TRAVEL_STATE_TEXT.get(state, state))
        loc = travel_raw.get("location") or {}
        if loc.get("name"):
            lines.append("  当前地点：%s" % loc["name"])
        arrive = travel_raw.get("arrive_at")
        now = travel_raw.get("server_now")
        if state == "traveling" and isinstance(arrive, int) and isinstance(now, int):
            sec = max(0, arrive - now)
            h, m = divmod(sec, 3600)
            lines.append("  到达倒计时：%d 小时 %d 分" % (h, m // 60))
        if travel_raw.get("reward_credit"):
            lines.append("  可领积分：%s 分" % travel_raw["reward_credit"])
        if travel_raw.get("daily_limit_reached"):
            lines.append("  今日派遣：已达上限，明日再来")
        if auto_log:
            lines.append("  自动执行：")
            for item in auto_log:
                lines.append("    · %s" % item)
    return "\n".join(lines)


def main():
    try:  # Windows 控制台默认 GBK，中文输出会乱码/报错
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:]
    as_json = "--json" in args
    as_checkin = ("checkin" in args) or ("--checkin" in args)
    travel_auto_on = "--travel-auto" in args
    location_id = None
    for idx, arg in enumerate(args):
        if arg == "--location" and idx + 1 < len(args):
            try:
                location_id = int(args[idx + 1])
            except ValueError:
                sys.stderr.write("[travel] --location 需为整数，已忽略\n")

    if "--rt-info" in args:
        print(json.dumps(rt_info(), ensure_ascii=False, indent=2))
        return 0
    if "--forget-rt" in args:
        print(json.dumps({"status": "ok" if delete_rt() else "noop", "action": "rt_forget"},
                         ensure_ascii=False, indent=2))
        return 0
    if "--export-rt" in args:
        return export_rt(save=("--save" in args))
    if "--setup-rt-file" in args:
        idx = args.index("--setup-rt-file")
        try:
            with open(args[idx + 1], "r", encoding="utf-8") as handle:
                return setup_rt(handle.read().strip())
        except (IndexError, OSError) as exc:
            sys.stderr.write("读取令牌文件失败：%s\n" % exc)
            return 1
    if "--setup-rt" in args:
        idx = args.index("--setup-rt")
        try:
            return setup_rt(args[idx + 1])
        except IndexError:
            sys.stderr.write("--setup-rt 后需跟令牌字符串。\n")
            return 1

    if "--web-info" in args:
        print(json.dumps(web_cred_info(), ensure_ascii=False, indent=2))
        return 0
    if "--forget-web" in args:
        print(json.dumps({"status": "ok" if delete_web_cred() else "noop", "action": "web_forget"},
                         ensure_ascii=False, indent=2))
        return 0
    if "--setup-web-curl" in args:
        idx = args.index("--setup-web-curl")
        try:
            text = args[idx + 1]
            if text == "-":
                text = sys.stdin.read()
            elif os.path.isfile(text):
                with open(text, "r", encoding="utf-8") as handle:
                    text = handle.read()
            return setup_web(_parse_curl(text), "curl")
        except (IndexError, ValueError) as exc:
            sys.stderr.write("解析 cURL 失败：%s\n" % exc)
            return 1
    if "--setup-web" in args:
        idx = args.index("--setup-web")
        try:
            code = args[idx + 1]
            if code == "-":
                code = sys.stdin.read().strip()
            return setup_web(_decode_web_code(code), "code")
        except (IndexError, ValueError) as exc:
            sys.stderr.write("解析凭据码失败：%s\n" % exc)
            return 1
    if "--snippet" in args:
        snip = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "templates", "web_cred_snippet.js"))
        print("网页会话凭据提取片段：%s" % snip)
        print("用法：浏览器登录 https://www.workbuddy.cn → F12 → Console → 粘贴运行该片段，"
              "把生成的 WBCRED1# 凭据码交给 `--setup-web '<码>'`。")
        return 0

    if "setup" in args or "--setup" in args:
        return cmd_setup()

    return run_once(travel_auto_on=travel_auto_on, location_id=location_id,
                    as_json=as_json, as_checkin=as_checkin)


if __name__ == "__main__":
    raise SystemExit(main())
