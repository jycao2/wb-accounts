#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-accounts 本地可视化面板（纯标准库，零第三方依赖）。

    python scripts/dashboard.py                  # 起服务并打开默认浏览器
    python scripts/dashboard.py --port 8765 --no-browser
    python scripts/dashboard.py --port 0          # 由系统分配端口

安全边界（刻意的）：
  * 只绑定 127.0.0.1，校验 Host，拒绝任何非本机 Host / DNS rebinding。
  * 每次启动生成一次性 token，所有 /api/* 请求必须携带；不带即 403。
  * 前端 CSP 禁止一切外部请求；页面无 CDN、无外部字体、无遥测。
  * 动作白名单：只开放只读与低风险写操作。rollback / token import / token forget /
    任意 raw 调用一律不通过网页执行，只提供可复制的终端命令。
  * 切换客户端账号由「面板排队 + 独立进程执行」完成：网页只传账号 id，
    真正动作是 hot_switch.py 里的「关客户端 → wb_accounts.py switch → 重开客户端」，
    登录态内容从不经过浏览器；切换会中断当前对话，页面必须在确认框里明确告知。
  * 签到（run-as <id> claim checkin）是唯一开放的"业务写操作"：必须由用户
    在页面上显式点击并在确认框里二次确认，且只调用 daily-claim 的幂等签到，
    不存在自动批量、不存在定时触发。
  * 网页不接收、不显示、不落盘任何 refresh token / access token / 信封。
    令牌配置引导只做两件事：启动本地授权脚本、回显其进度日志（不含令牌）。
  * 后端复用 wb_accounts.py --json 的脱敏输出，并再做一次正则兜底脱敏。
  * 面板自身不写日志；仅授权脚本的步骤日志写入 state/grab.log 供页面回显。
  * shutdown 后端进程优雅退出。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
VAULT = ROOT / "wb_accounts.py"
PAGE = ROOT / "dashboard.html"
GRAB = ROOT / "grab_web_cred.js"
RECOVER_ATREST = ROOT / "recover_atrest.py"
HOT = ROOT / "hot_switch.py"
STATE_DIR = ROOT.parent / "state"
AUTH_LOG = STATE_DIR / "grab.log"
HOT_STATE = STATE_DIR / "hot-switch.json"
HOT_LOG = STATE_DIR / "hot-switch.log"
VERSION = "1.1.0"
MAX_BODY = 64 * 1024
CLI_TIMEOUT = 45
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
SECRET_KV = re.compile(
    r'(?i)("(?:accessToken|refreshToken|envelope|atRestSecretKey|password|secret)"\s*:\s*")[^"]*(")')

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 脱敏（兜底：CLI 本身已脱敏，这里防止将来误把凭据带进界面）
# ---------------------------------------------------------------------------
def redact(text: str) -> str:
    text = JWT_LIKE.sub("[已隐藏-令牌]", text)
    return SECRET_KV.sub(r"\1[已隐藏]\2", text)


def text_param(p: dict, key: str, limit: int = 80, required: bool = True) -> str:
    v = str(p.get(key, "") or "").strip()
    if required and not v:
        raise ValueError("缺少参数 %s" % key)
    if len(v) > limit:
        raise ValueError("参数 %s 过长（上限 %d 字符）" % (key, limit))
    if "\x00" in v or "\n" in v or "\r" in v:
        raise ValueError("参数 %s 含非法字符" % key)
    if v.startswith("-"):
        raise ValueError("参数 %s 不能以 - 开头" % key)
    return v


def id_param(p: dict) -> str:
    v = text_param(p, "id", 64)
    if not SAFE_ID.fullmatch(v):
        raise ValueError("账号 id 只允许字母/数字/下划线/连字符")
    return v


# ---------------------------------------------------------------------------
# 动作白名单：任何动作都必须在这里显式列出，参数逐个校验
# ---------------------------------------------------------------------------
def build_argv(action: str, p: dict) -> list[str]:
    if action == "init":
        return ["init"]
    if action == "capture":
        argv = ["capture", "--label=" + text_param(p, "label", 40)]
        if p.get("id"):
            argv.append("--id=" + id_param(p))
        if p.get("note"):
            argv.append("--note=" + text_param(p, "note", 120, required=False))
        if p.get("force"):
            argv.append("--force")
        return argv
    if action == "refresh_snapshot":
        return ["refresh", id_param(p)]
    if action == "switch_preview":
        return ["switch", id_param(p), "--dry-run"]
    if action == "label":
        return ["label", id_param(p), text_param(p, "name", 40)]
    if action == "remove":
        return ["remove", id_param(p)]
    if action == "token_status":
        return ["token", "status", id_param(p)]
    if action == "token_recover":
        # 从客户端在盘登录态恢复令牌通道（需 atRestSecretKey，已在保险库内）；
        # 与 capture/refresh 同源只读客户端文件，不写客户端、不触令牌明文。
        return ["token", "recover", id_param(p)]
    if action == "claim_checkin":
        # 签到 = 写操作：必须由用户在页面上显式点击 + 确认后才到这里
        argv = ["run-as", id_param(p), "claim", "checkin"]
        if p.get("travel"):
            argv.append("--travel-auto")
        return argv
    if action == "keepalive_all":
        return ["keepalive", "--all"]
    if action == "run_as_status":
        return ["run-as", id_param(p), "status"]
    simple = {"current": "current", "list": "list", "brief": "brief",
              "where": "where", "doctor": "doctor", "report": "report",
              "export_state": "export-state"}
    if action in simple:
        return [simple[action]]
    raise ValueError("该动作未在面板白名单中开放")


# 明确禁止、且要给用户精确终端命令的动作
BLOCKED = {
    "switch": "真实切换会写客户端登录态，必须完全退出 WorkBuddy 后在终端执行",
    "rollback": "回滚会写客户端登录态，必须完全退出 WorkBuddy 后在终端执行",
    "token_import": "面板不接收 refresh token，请在终端用 stdin 导入",
    "token_forget": "删除令牌属高风险操作，请在终端执行",
    "run_as_raw": "任意接口调用不在面板开放，请在终端执行",
}


def run_cli(argv: list[str], timeout: int | None = None) -> dict:
    """调用 wb_accounts.py，返回结构化、脱敏后的结果。"""
    limit = timeout or CLI_TIMEOUT
    cmd = [sys.executable, str(VAULT), *argv, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=limit)
        raw = redact((r.stdout or "").strip() or (r.stderr or "").strip())
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": " ".join(argv), "error": "命令超时（%ds）" % limit}
    except Exception as e:
        return {"ok": False, "command": " ".join(argv), "error": "%s: %s" % (type(e).__name__, e)}
    try:
        data = json.loads(raw)
    except Exception:
        data = {"ok": False, "raw": raw[:2000]}
    return {"ok": bool(r.returncode == 0 and data.get("ok")),
            "exit_code": r.returncode,
            "command": "wb_accounts.py " + " ".join(argv),
            "data": data}


def state_payload() -> dict:
    # 先「打开即采集」，再「打开即配置令牌通道」，最后取状态；
    # 顺序很重要：列表里的 token_channel 必须反映刚自动配好的结果。
    auto = ensure_auto_capture()
    auto_tokens = ensure_auto_tokens()
    keys = ("brief", "list", "where", "doctor")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {k: pool.submit(run_cli, [k]) for k in keys}
        out = {k: futs[k].result()["data"] for k in keys}
    return {"ok": True, "version": VERSION, "vault": str(VAULT),
            "auto_capture": auto, "auto_tokens": auto_tokens,
            "switch_state": read_hot_state(), **out}


# ---------------------------------------------------------------------------
# 签到状态（只读查询 + 当日缓存）：页面据此把已签到的按钮置灰成「已签到」
# daily-claim 的人类可读输出是稳定口径，这里做轻量解析，不引入新依赖。
# ---------------------------------------------------------------------------
_claim_lock = threading.Lock()
_claim_cache: dict = {}   # id -> {"id","date","at","ttl","claimed_today","balance","activity_credits","streak"}
CLAIM_TTL = 300           # 当日缓存 5 分钟，超时重新查询（面板长期挂着也能跟进状态）
CLAIM_ERR_TTL = 60        # 查询失败只短缓存，避免每次刷新都打一轮网络请求


def _parse_claim_text(text: str) -> dict | None:
    """解析 daily-claim 的人类可读输出，提取 今日已签 / 账户积分余额 / 活动积分 / 连续天数。

    口径澄清（与客户端对齐）：
      - balance  = 账户积分余额（客户端账号页那个，带小数，如 530.19），来自 get-user-resource，
                  仅 Bearer 通道能拿到；cookie 通道拿不到时为 None。这才是用户眼里的「积分」。
      - activity_credits = 活动积分(累计)（Buddy 加油站每日签到活动积分，整数，如 2500）。
    """
    if not text:
        return None
    claimed = ("今日记录：已记录" in text) or ("今日已签到" in text) or ("签到成功" in text)
    m = re.search(r"账户积分余额[:：]\s*([\d.]+)", text)
    act = re.search(r"活动积分\(累计\)[:：]\s*(\d+)", text)
    s = re.search(r"连续登录[:：]\s*(\d+)", text)
    return {"claimed_today": claimed,
            "balance": m.group(1) if m else None,            # 账户积分余额（真·积分，小数）
            "activity_credits": act.group(1) if act else None,
            "streak": s.group(1) if s else None}


def _claim_state_for(acc_id: str) -> dict:
    today = time.strftime("%Y-%m-%d")
    now = time.time()
    with _claim_lock:
        ent = _claim_cache.get(acc_id)
        if ent and ent.get("date") == today and now - ent.get("at", 0) < ent.get("ttl", CLAIM_TTL):
            return dict(ent)
    r = run_cli(["run-as", acc_id, "claim", "status"], timeout=90)
    stdout = (((r.get("data") or {}).get("result") or {}).get("stdout") or "")
    parsed = _parse_claim_text(stdout) if r.get("ok") else None
    if parsed:
        ent = {"id": acc_id, "date": today, "at": time.time(), "ttl": CLAIM_TTL, **parsed}
        with _claim_lock:
            _claim_cache[acc_id] = dict(ent)
        return ent
    ent = {"id": acc_id, "date": today, "at": time.time(), "ttl": CLAIM_ERR_TTL,
           "claimed_today": None, "balance": None, "streak": None,
           "error": (r.get("data") or {}).get("error") or "查询失败（该账号可能未配通道）"}
    with _claim_lock:
        _claim_cache[acc_id] = dict(ent)
    return ent


# ---------------------------------------------------------------------------
# 面板内切换账号：顺序是「关客户端 → 写登录态 → 重开客户端」，
# 关客户端会连带结束本面板进程，所以真正干活的是脱离进程组的 hot_switch.py。
# ---------------------------------------------------------------------------
def read_hot_state() -> dict:
    d: dict = {}
    try:
        raw = json.loads(HOT_STATE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d = raw
    except Exception:
        return {"state": "idle"}
    state = d.get("state")
    if state in ("pending", "closing", "switching"):
        d["running"] = True
        # 执行器本该在 1~2 分钟内走完；超时还停在运行态 = 它中途被连带结束了
        try:
            at = dt.datetime.strptime(str(d.get("at") or ""), "%Y-%m-%d %H:%M:%S")
            if (dt.datetime.now() - at).total_seconds() > 180:
                d.update(state="error", running=False,
                         message="上次切换中断：执行器在关闭客户端后没能继续，登录态未改动。"
                                 "请重试——若反复出现，说明切换进程仍被客户端连带结束。")
        except Exception:
            pass
    return d


def ack_hot_state() -> dict:
    """用户点「知道了」：把上次结果标为已读，避免每次打开都提醒。"""
    try:
        d = read_hot_state()
        if d.get("state") in ("done", "error"):
            d["state"] = "ack"
            d["acked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            tmp = HOT_STATE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, HOT_STATE)
    except Exception:
        pass
    return read_hot_state()


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串（内部单引号翻倍），避免命令行注入/转义问题。"""
    return "'" + s.replace("'", "''") + "'"


def _wmi_spawn(args: list[str]) -> int | None:
    """用 WMI 创建进程：父进程是 WmiPrvSE，不在 WorkBuddy 的 job 里。

    面板和执行器都在客户端的进程作业中，客户端一退出会连带清场；
    只有 WMI/Task Scheduler 这类"系统代创建"的进程才能活到切换完成。
    """
    if os.name != "nt":
        return None
    cmdline = subprocess.list2cmdline(args)
    script = ("$ErrorActionPreference='Stop';"
              "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine=" + _ps_quote(cmdline) + "};"
              "if ($r.ReturnValue -eq 0) { [Console]::Out.Write($r.ProcessId) } "
              "else { [Console]::Out.Write('ERR:' + $r.ReturnValue) }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45)
        out = (r.stdout or "").strip()
        if out.isdigit():
            return int(out)
        with open(HOT_LOG, "a", encoding="utf-8") as lf:
            lf.write("[%s] WMI 创建失败: %s %s\n"
                     % (time.strftime("%H:%M:%S"), out, (r.stderr or "").strip()[:300]))
    except Exception as e:  # noqa: BLE001
        with open(HOT_LOG, "a", encoding="utf-8") as lf:
            lf.write("[%s] WMI 创建异常: %s: %s\n"
                     % (time.strftime("%H:%M:%S"), type(e).__name__, e))
    return None


def spawn_hot_switch(acc_id: str, sync_tasks: bool = False,
                     sync_selection=None) -> dict:
    """把「退出并切换」交给独立进程执行；本请求立刻返回，页面先看到进度。"""
    if not HOT.is_file():
        return {"ok": False, "error": "缺少 hot_switch.py，无法执行面板内切换"}
    lst = run_cli(["list"], timeout=25)
    accounts = ((lst.get("data") or {}).get("accounts") or [])
    acc = next((x for x in accounts if x.get("id") == acc_id), None)
    if not acc:
        return {"ok": False, "error": "账号 %s 不在保险库里" % acc_id}
    if acc.get("is_current_client"):
        return {"ok": False, "error": "客户端当前已经是「%s」，无需切换" % (acc.get("label") or acc_id)}
    if not acc.get("snapshot_ok"):
        return {"ok": False, "error": "「%s」没有可用快照，无法切换；请先在客户端登录该号并采集" %
                                       (acc.get("label") or acc_id)}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"state": "pending", "id": acc_id,
               "label": acc.get("label") or acc_id,
               "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "message": "已排队：准备关闭客户端并切换…"}
    HOT_STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args = [sys.executable, str(HOT), "--id", acc_id, "--vault", str(VAULT),
            "--state", str(HOT_STATE)]
    if sync_tasks:
        args.append("--sync-tasks")
        sel_file = STATE_DIR / "sync-selection.json"
        if sync_selection == "all" or sync_selection is None:
            payload_sel = {"mode": "all"}
        elif isinstance(sync_selection, list) and \
                all(isinstance(x, str) for x in sync_selection) and sync_selection:
            payload_sel = {"mode": "ids", "ids": sync_selection[:500]}
        else:
            return {"ok": False, "error": "sync_selection 必须是 \"all\" 或会话 id 数组"}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sel_file.write_text(json.dumps(payload_sel, ensure_ascii=False), encoding="utf-8")
        args += ["--sync-selection", str(sel_file)]
    try:
        with open(HOT_LOG, "a", encoding="utf-8") as lf:
            lf.write("\n[%s] 启动切换：%s\n" % (time.strftime("%H:%M:%S"), " ".join(args[1:])))
    except Exception:
        pass
    pid = _wmi_spawn(args)          # 首选：WMI 代创建，脱离客户端 job，关客户端不会被连带杀死
    if pid is None:
        flags = 0
        if os.name == "nt":
            # 兜底（可能随客户端退出被结束）：DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            flags = 0x00000008 | 0x00000200
        try:
            with open(HOT_LOG, "ab") as lf:
                p = subprocess.Popen(args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                     stdout=lf, stderr=lf, creationflags=flags, close_fds=True)
            pid = p.pid
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "启动切换进程失败: %s: %s" % (type(e).__name__, e)}
    return {"ok": True, "id": acc_id, "label": acc.get("label") or acc_id, "pid": pid,
            "will_close_client": True,
            "message": "开始切换：约 3 秒后自动关闭 WorkBuddy，写入「%s」的登录态，随后自动重开。"
                       % (acc.get("label") or acc_id)}


def _note_checkin_done(acc_id: str, result: dict) -> None:
    """签到成功后立刻把「今日已签到」写进缓存：页面即时置灰，不等重新查询。"""
    if not acc_id or not SAFE_ID.fullmatch(acc_id):
        return
    out = (((result.get("data") or {}).get("result") or {}).get("stdout") or "")
    if not (("签到成功" in out) or ("今日已签到" in out)):
        return
    parsed = _parse_claim_text(out) or {}
    with _claim_lock:
        _claim_cache[acc_id] = {"id": acc_id, "date": time.strftime("%Y-%m-%d"),
                                "at": time.time(), "ttl": 60,  # 余额是签到前快照，1 分钟后允许重查校准
                                "claimed_today": True,
                                "balance": parsed.get("balance"),
                                "activity_credits": parsed.get("activity_credits"),
                                "streak": parsed.get("streak")}


# ---------------------------------------------------------------------------
# 打开即采集：面板每次启动 / 页面每次加载，都把客户端「当前登录的号」收进保险库。
#   * 未登记  -> capture（新建快照）
#   * 已登记  -> refresh（用当前登录态更新该号快照，令牌轮换后自动变新）
#   * 只写保险库里的加密快照，绝不写客户端登录态、绝不触碰进程。
# ---------------------------------------------------------------------------
AUTOCAPTURE_MIN_INTERVAL = 15.0  # 秒；页面反复刷新时不重复打 CLI
AUTOTOKEN_MIN_INTERVAL = 300.0   # 秒；自动配置会打刷新网关，别频繁触发
_auto_lock = threading.Lock()
_auto_cache: dict = {"at": 0.0, "payload": None}
_token_lock = threading.Lock()
_token_cache: dict = {"at": 0.0, "payload": None}


def _is_uninitialized(res: dict) -> bool:
    d = res.get("data") or {}
    return "尚未初始化" in str(d.get("error", "")) or "index.json" in str(d.get("error", ""))


def _autocapture_once() -> dict:
    cur = run_cli(["current"])
    if not cur["ok"] and _is_uninitialized(cur):
        ini = run_cli(["init"])
        if not ini["ok"]:
            return {"ok": False, "action": "error",
                    "message": "保险库初始化失败：" + str((ini.get("data") or {}).get("error"))}
        cur = run_cli(["current"])
    d = cur.get("data") or {}
    if not cur["ok"]:
        code = str(d.get("code") or "")
        msg = str(d.get("error") or "无法读取客户端登录态")
        if code == "no_login_state" or "登录态" in msg:
            msg = "客户端未登录 / 找不到登录态文件，跳过自动采集"
        return {"ok": False, "action": "skipped", "message": msg}

    masked = d.get("uid_masked") or "?"
    acc = d.get("account") or {}
    acc_id = str(acc.get("id") or "")

    if d.get("registered") and acc_id and SAFE_ID.fullmatch(acc_id):
        r = run_cli(["refresh", acc_id])
        rd = r.get("data") or {}
        if r["ok"]:
            return {"ok": True, "action": "refresh", "id": acc_id,
                    "label": str(acc.get("label") or acc_id), "uid_masked": masked,
                    "expires_at": rd.get("expires_at"),
                    "message": "已用客户端当前登录态更新快照 %s（uid %s）" % (acc_id, masked)}
        return {"ok": False, "action": "refresh_failed", "id": acc_id, "uid_masked": masked,
                "message": "更新快照失败：" + str(rd.get("error") or r.get("error") or "未知原因")}

    label = "自动采集 " + time.strftime("%m-%d %H:%M")
    new_id = "auto-" + time.strftime("%m%d%H%M%S")
    r = run_cli(["capture", "--label=" + label, "--id=" + new_id])
    rd = r.get("data") or {}
    if r["ok"] or rd.get("code") == "already_exists":
        return {"ok": True, "action": "capture", "id": rd.get("id") or new_id,
                "label": rd.get("label") or label, "uid_masked": rd.get("uid_masked") or masked,
                "expires_at": rd.get("expires_at"),
                "message": "已把客户端当前登录的账号采集进保险库（uid %s）" % masked}
    return {"ok": False, "action": "capture_failed", "uid_masked": masked,
            "message": "采集失败：" + str(rd.get("error") or r.get("error") or "未知原因")}


def ensure_auto_capture(force: bool = False) -> dict:
    """幂等 + 节流的自动采集；返回最近一次结果（页面直接展示，不含任何凭据）。"""
    with _auto_lock:
        now_ts = time.time()
        cached = _auto_cache.get("payload")
        if not force and cached and now_ts - _auto_cache["at"] < AUTOCAPTURE_MIN_INTERVAL:
            return dict(cached, cached=True)
        res = _autocapture_once()
        res["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        res["cached"] = False
        _auto_cache.update({"at": now_ts, "payload": res})
        return res


_bootstrap_lock = threading.Lock()
_bootstrap_state: dict = {"at": 0.0, "running": False, "result": None}


def maybe_bootstrap_atrest(detail: dict) -> None:
    """本机 atRestSecretKey 未固化、且有账号没配上时，异步做一次性自愈引导。

    只在面板启动时尝试、一天最多一次、后台线程执行，绝不阻塞面板打开。
    引导脚本只读客户端自身进程内存，取到后立刻 DPAPI 加密落盘；成功后
    会再跑一次 token auto，把刚能解开的账号自动配上。
    """
    if not RECOVER_ATREST.is_file() or detail.get("atrest_key"):
        return
    if not [r for r in (detail.get("results") or []) if not r.get("ok")]:
        return
    with _bootstrap_lock:
        if _bootstrap_state["running"] or time.time() - _bootstrap_state["at"] < 86400:
            return
        _bootstrap_state.update({"at": time.time(), "running": True, "result": None})

    def _worker() -> None:
        try:
            r = subprocess.run([sys.executable, str(RECOVER_ATREST)], cwd=str(ROOT),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=240)
            msg = redact(((r.stdout or "") + (r.stderr or "")).strip())[-400:]
            ok = r.returncode == 0
        except Exception as e:
            ok, msg = False, "%s: %s" % (type(e).__name__, e)
        with _bootstrap_lock:
            # 成功就不再重试；失败（多半是客户端当时没把密钥载入内存）1 小时后可再试，
            # 这样用户重新登录 / 切号后无需等满一天。
            _bootstrap_state.update({
                "running": False,
                "at": time.time() if ok else time.time() - 86400 + 3600,
                "result": {"ok": ok, "message": msg}})
        if ok:
            ensure_auto_tokens(force=True)  # 密钥刚固化，立刻把能配的号配上
    threading.Thread(target=_worker, daemon=True, name="atrest-bootstrap").start()


def ensure_auto_tokens(force: bool = False) -> dict:
    """打开面板 / 加载页面时自动配置令牌通道（幂等 + 节流）。

    只调 `wb_accounts.py token auto`（纯读盘：已有 RT 续期、无 RT 从各自的
    加密快照恢复并验证）。面板本身不扫描任何进程内存；本机 atRestSecretKey
    由一次性引导 `recover_atrest.py` 固化后，这里就能全自动。
    失败绝不影响面板启动，结果脱敏后回显。
    """
    with _token_lock:
        now_ts = time.time()
        cached = _token_cache.get("payload")
        if not force and cached and now_ts - _token_cache["at"] < AUTOTOKEN_MIN_INTERVAL:
            return dict(cached, cached=True, bootstrap=dict(_bootstrap_state))
        try:
            r = run_cli(["token", "auto"], timeout=90)
            payload = {"ok": bool(r.get("ok")), "detail": r.get("data") or {},
                       "error": r.get("error")}
        except Exception as e:  # 配置失败绝不影响面板
            payload = {"ok": False, "detail": {},
                       "error": "%s: %s" % (type(e).__name__, e)}
        payload["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload["cached"] = False
        payload["bootstrap"] = dict(_bootstrap_state)
        _token_cache.update({"at": now_ts, "payload": payload})
        return payload


# ---------------------------------------------------------------------------
# 令牌配置引导：面板只负责「打开授权窗口」与「回显日志」。
#   真正的登录与令牌抓取由 grab_web_cred.js 在独立浏览器里完成，
#   令牌直接写进本机 state/（0600），既不经过网页、也不进页面 DOM。
# ---------------------------------------------------------------------------
_auth_lock = threading.Lock()
_auth: dict = {"proc": None, "started": 0.0}


def _node_bin() -> str | None:
    """找一个可用的 node：环境变量 > PATH > 客户端自带版本目录。"""
    cands = [os.environ.get("CODEBUDDY_NODE_BIN"), os.environ.get("NODE_BIN"),
             shutil.which("node"), shutil.which("node.exe")]
    for c in cands:
        try:
            if c and Path(c).is_file() and Path(c).suffix.lower() != ".cmd":
                return str(c)
        except OSError:
            continue
    try:
        found = sorted(Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"))
        if found:
            return str(found[-1])
    except OSError:
        pass
    return None


def start_auth_browser(fresh: bool = False) -> dict:
    with _auth_lock:
        p = _auth.get("proc")
        if p is not None and p.poll() is None:
            return {"ok": True, "running": True, "pid": p.pid,
                    "message": "授权窗口已在运行，请在弹出来的浏览器里完成登录"}
        node = _node_bin()
        if not node:
            return {"ok": False, "code": "no_node",
                    "error": "没找到 node 运行环境；可在终端直接跑 scripts/grab_web_cred.js"}
        if not GRAB.is_file():
            return {"ok": False, "code": "no_script", "error": "缺少 grab_web_cred.js"}
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            fh = open(AUTH_LOG, "w", encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": "无法创建授权日志：%s: %s" % (type(e).__name__, e)}
        fh.write("[%s] 启动授权窗口（独立 profile，不影响日常浏览器）%s\n" % (
            time.strftime("%H:%M:%S"), "——换号模式：先清除上次登录会话" if fresh else ""))
        fh.flush()
        env = dict(os.environ)
        env.setdefault("WAIT_MIN", "15")
        if fresh:
            env["WB_FRESH_LOGIN"] = "1"
        try:
            proc = subprocess.Popen(
                [node, str(GRAB)], cwd=str(ROOT), env=env,
                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            try:
                fh.close()
            except Exception:
                pass
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        _auth.update({"proc": proc, "started": time.time()})
        # 观察一小段时间：脚本可能因单实例锁 / 浏览器启动失败当场退出
        try:
            rc = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            rc = None
        if rc is not None:
            lines: list[str] = []
            try:
                lines = [ln for ln in AUTH_LOG.read_text(encoding="utf-8",
                         errors="replace").splitlines() if ln.strip()][-3:]
            except Exception:
                pass
            return {"ok": False, "running": False, "exit_code": rc,
                    "error": "授权窗口启动后立即退出（退出码 %d）：%s"
                             % (rc, " / ".join(lines) or "无日志")}
        return {"ok": True, "running": True, "pid": proc.pid,
                "message": "授权窗口已打开：在弹出的浏览器里用手机号验证码或扫码登录，登录完成自动收尾"}


def read_auth_log(max_lines: int = 40) -> dict:
    with _auth_lock:
        p = _auth.get("proc")
        running = bool(p is not None and p.poll() is None)
        code = None if (p is None or running) else p.returncode
    # 面板进程之外拉起的实例（终端 / 会话里后台跑的）用锁文件判断是否仍在运行
    if not running:
        try:
            lock = STATE_DIR / "auth.lock"
            if lock.is_file() and (time.time() - lock.stat().st_mtime) < 20 * 60:
                running = True
                code = None
        except OSError:
            pass
    lines: list[str] = []
    if AUTH_LOG.is_file():
        try:
            raw = AUTH_LOG.read_text(encoding="utf-8", errors="replace")
            lines = [ln for ln in raw.splitlines() if ln.strip()][-max_lines:]
        except Exception:
            pass
    return {"ok": True, "running": running, "exit_code": code,
            "lines": [redact(ln) for ln in lines]}



class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, token: str):
        super().__init__(addr, handler)
        self.token = token


class Handler(BaseHTTPRequestHandler):
    server_version = "wb-accounts-dashboard/" + VERSION
    sys_version = ""

    def log_message(self, *_args) -> None:
        return  # 不写日志，避免 token / 路径外泄

    # -- 基础响应 ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        # 浏览器随时可能中断请求（刷新、关标签、切页），这种情况绝不能影响面板进程本身
        try:
            self.send_response(code)
            # 去掉占位 Content-Length，写真实长度
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'; "
                             "img-src 'self' data:; form-action 'none'; base-uri 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        given = self.headers.get("X-Dashboard-Token") or ""
        if not given:
            given = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        if not given:
            # 首次用 ?t= 打开后服务端会种一个 HttpOnly cookie，之后直接访问
            # http://127.0.0.1:8765 即可（不必把 token 留在书签里）。
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "wbdash":
                    given = v
                    break
        return bool(given) and hmac.compare_digest(given, self.server.token)

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._json(403, {"ok": False, "error": "未授权：缺少或错误的一次性 token / 非法 Host",
                         "code": "forbidden"})
        return False

    # -- 路由 --------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not self._guard():
                return
            if not PAGE.is_file():
                self._json(500, {"ok": False, "error": "dashboard.html 缺失"})
                return
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8",
                       extra={"Set-Cookie": "wbdash=%s; Path=/; HttpOnly; "
                                            "SameSite=Strict; Max-Age=31536000"
                                            % self.server.token})
            return
        if path == "/api/state":
            if not self._guard():
                return
            try:
                self._json(200, state_payload())
            except Exception as e:
                self._json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._guard():
            return
        if path not in ("/api/action", "/api/shutdown"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            size = 0
        if size > MAX_BODY:
            self._json(413, {"ok": False, "error": "请求体过大"})
            return
        raw = self.rfile.read(size) if size else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return
        if path == "/api/shutdown":
            self._json(200, {"ok": True, "message": "面板已关闭"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        action = str(body.get("action", ""))
        params = body.get("params") or {}
        if not isinstance(params, dict):
            self._json(400, {"ok": False, "error": "params 必须是对象"})
            return
        # 令牌配置引导：只启动/查询本地授权窗口，网页不接触任何令牌
        if action == "auth_browser":
            self._json(200, start_auth_browser(fresh=bool(params.get("fresh"))))
            return
        if action == "auth_log":
            self._json(200, read_auth_log())
            return
        # 签到状态查询（只读、当日缓存）：前端用它把已签到的按钮置灰成「已签到」
        # 面板内切换账号：只启动/查询独立的切换进程，网页不接触登录态内容
        if action == "switch_status":
            self._json(200, {"ok": True, **read_hot_state()})
            return
        if action == "switch_ack":
            self._json(200, {"ok": True, **ack_hot_state()})
            return
        if action == "switch_client":
            try:
                aid = id_param(params)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            self._json(200, spawn_hot_switch(
                aid, sync_tasks=bool((params or {}).get("sync_tasks")),
                sync_selection=(params or {}).get("sync_selection")))
            return
        if action == "sync_plan":
            # 任务同步选择列表：当前登录号的存活会话（只读，供勾选）
            try:
                sys.path.insert(0, str(ROOT))
                import wb_accounts as W  # noqa: PLC0415
                import sync_tasks as S  # noqa: PLC0415
                _raw, _cd, meta = W.read_login_state()
                uid = (meta or {}).get("uid") or ""
                if not uid:
                    self._json(200, {"ok": False, "error": "读不到当前登录账号 uid"})
                    return
                cands = S.list_candidates(W.HOME.parent / "workbuddy.db", uid)
                self._json(200, {"ok": True, "uid": uid, "count": len(cands),
                                 "total_bytes": sum(c["size"] for c in cands),
                                 "items": cands})
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)})
            return
        if action == "claim_state":
            ids = params.get("ids") or []
            if not isinstance(ids, list) or not ids or len(ids) > 50:
                self._json(400, {"ok": False, "error": "ids 必须是非空账号 id 数组（≤50 个）"})
                return
            clean = []
            for x in ids:
                v = str(x or "").strip()
                if not SAFE_ID.fullmatch(v):
                    self._json(400, {"ok": False, "error": "账号 id 不合法"})
                    return
                clean.append(v)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(clean))) as pool:
                states = list(pool.map(_claim_state_for, clean))
            self._json(200, {"ok": True, "states": states})
            return
        if action in BLOCKED:
            self._json(403, {"ok": False, "code": "blocked",
                             "error": BLOCKED[action],
                             "hint": "在终端执行 wb_accounts.py 对应命令；面板只做预览与管理"})
            return
        try:
            argv = build_argv(action, params)
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e)})
            return
        # 签到要串行调 daily-claim（含网络往返），单独放宽超时
        result = run_cli(argv, timeout=180 if action == "claim_checkin" else None)
        if action == "claim_checkin":
            # 签到成功 → 立即记入当日缓存，前端把按钮置灰成「已签到」
            _note_checkin_done(str(params.get("id") or "").strip(), result)
        self._json(200, result)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def make_server(port: int, token: str) -> DashboardServer:
    last = None
    candidates = [port] if port == 0 else list(range(port, port + 20))
    for p in candidates:
        try:
            return DashboardServer(("127.0.0.1", p), Handler, token)
        except OSError as e:
            last = e
    raise SystemExit("无法绑定 127.0.0.1 端口（%d 起）：%s" % (port, last))


def vault_home() -> Path:
    """保险库目录（与 wb_accounts.py 的 env_home 保持一致）。"""
    raw = (os.environ.get("WB_ACCOUNTS_HOME") or "").strip()
    return (Path(raw).expanduser() if raw else Path.home() / ".workbuddy" / "wb-accounts").resolve()


def resolve_dashboard_token() -> str:
    """解析面板访问 token，优先级：环境变量 > 持久化文件 > 新生成并落盘。

    持久化后浏览器书签与已种下的 cookie 在重启/开机自启后依然有效；
    也避免把 token 写死在任何启动脚本里（开源仓库里不该出现固定 token，
    每个用户首次运行会自动生成本机专属的一份）。
    """
    env_tok = (os.environ.get("WB_DASHBOARD_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    path = vault_home() / "dashboard-token.txt"
    try:
        if path.is_file():
            tok = path.read_text(encoding="utf-8").strip()
            if len(tok) >= 16:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tok + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # Windows 上是宽松映射，非 0 即尽力而为
        except OSError:
            pass
    except OSError:
        pass
    return tok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dashboard.py", description="wb-accounts 本地可视化面板")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765；0 = 系统分配")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-auto-capture", action="store_true",
                    help="打开时不自动采集客户端当前登录的账号（默认会采集/刷新）")
    ap.add_argument("--no-auto-token", action="store_true",
                    help="打开时不自动配置/续期令牌通道（默认会）")
    ap.add_argument("--json", action="store_true", help="启动信息以 JSON 打印（默认即是）")
    a = ap.parse_args(argv)

    if not VAULT.is_file():
        print("找不到 wb_accounts.py: %s" % VAULT, file=sys.stderr)
        return 1
    if not PAGE.is_file():
        print("找不到 dashboard.html: %s" % PAGE, file=sys.stderr)
        return 1

    # 端口已被占用 = 面板多半已在运行（常驻 / 开机自启 / 上一实例），不重复启动。
    if a.port:
        try:
            import socket
            s = socket.socket()
            s.settimeout(0.4)
            busy = s.connect_ex(("127.0.0.1", a.port)) == 0
            s.close()
        except OSError:
            busy = False
        if busy:
            print("[wb-accounts] 端口 %d 已被占用（面板可能已在运行），本次启动跳过。"
                  "需要另开实例请用 --port 指定其他端口。" % a.port,
                  file=sys.stderr, flush=True)
            return 0

    # 先监听、后采集：快照刷新 + 令牌续期要跑几十秒，如果阻塞在 serve 之前，
    # 双击 vbs 打开的浏览器会碰到"拒绝连接"。改为端口立即就绪，采集放后台线程，
    # 完成后把结果补打到 stdout（auto_capture / auto_tokens 字段在补充 JSON 里）。
    # 默认从持久化文件取 token（首次自动生成并落盘）；设置 WB_DASHBOARD_TOKEN 可覆盖，
    # 让浏览器书签/已种下的 cookie 在重启后继续有效。仍只绑 127.0.0.1 + 校验 Host。
    token = resolve_dashboard_token()
    httpd = make_server(a.port, token)
    port = httpd.server_address[1]

    def _auto_bg() -> None:
        auto = None
        if not a.no_auto_capture:
            try:
                auto = ensure_auto_capture(force=True)
            except Exception as e:  # 采集失败绝不影响面板启动
                auto = {"ok": False, "action": "error",
                        "message": "%s: %s" % (type(e).__name__, e)}
            if auto.get("message"):
                print("[wb-accounts] " + auto["message"], file=sys.stderr, flush=True)
        # 采集之后立刻配置令牌通道：新收进来的号也能在同一轮里被配上
        auto_tokens = None
        if not a.no_auto_token:
            try:
                auto_tokens = ensure_auto_tokens(force=True)
            except Exception as e:  # 配置失败绝不影响面板启动
                auto_tokens = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            d = auto_tokens.get("detail") or {}
            print("[wb-accounts] 自动令牌通道：%s/%s 就绪%s" %
                  (d.get("ready", "?"), d.get("count", "?"),
                   "" if d.get("atrest_key") else "（本机密钥未固化，已在后台尝试自愈）"),
                  file=sys.stderr, flush=True)
            maybe_bootstrap_atrest(d)
        print(json.dumps({"ok": True, "background": True,
                          "auto_capture": auto, "auto_tokens": auto_tokens},
                         ensure_ascii=False), flush=True)

    if not (a.no_auto_capture and a.no_auto_token):
        threading.Thread(target=_auto_bg, daemon=True, name="wb-auto-capture").start()

    url = "http://127.0.0.1:%d/?t=%s" % (port, token)
    print(json.dumps({"ok": True, "url": url, "port": port, "pid": os.getpid(),
                      "version": VERSION, "bound": "127.0.0.1",
                      "auto_background": True, "auto_capture": None, "auto_tokens": None},
                     ensure_ascii=False), flush=True)
    if not a.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    print(json.dumps({"ok": True, "message": "面板已停止"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
