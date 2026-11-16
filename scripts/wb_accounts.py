#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-accounts / WorkBuddy 多账号管家

两条互相独立的通道
------------------
A. 切换客户端账号：每个账号保存一份 **登录态整文件快照**（workbuddy-desktop.info 的原始字节，
   含客户端自己的 $wbEncrypted 信封）。切换时先备份当前，再原子写回目标快照。
   插件全程不解密、不打印、不导出任何令牌明文；客户端信封密钥不变，写回后客户端自己能解开。
B. 按账号并行跑任务：每个账号可选保存一份独立的 refresh token（用户显式导入，
   或从快照在用户提供 atRestSecretKey 时只读解密）。经官方 refresh 网关换新 access token，
   用 Bearer 调业务接口 / 委托已装的 daily-claim 技能。客户端不用切换、不用重启、多号并存。

安全硬规则
----------
- 绝不打印 accessToken / refreshToken / envelope / 昵称 / 手机号明文。
- 快照与令牌都用 DPAPI(Windows) 或 AES-GCM(其它平台) 加密落盘，权限 600。
- switch 在客户端运行时默认拒绝（客户端退出会覆写登录态），绝不杀进程、绝不自动重启。
- 不扫描进程内存、不读其它应用文件；只读用户显式指定的登录态文件。
  （本机密钥的一次性自愈由独立脚本 recover_atrest.py 在面板启动时后台完成，
    本 CLI 始终只读盘；dashboard.py --no-auto-token 可整条关闭。）
- 所有写操作先备份再原子替换；remove 只归档不真删。

退出码: 0 成功 / 1 一般错误 / 2 需要先登记或客户端正在运行
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import wb_crypto as crypto

VERSION = "1.1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DASHBOARD = SCRIPT_DIR / "dashboard.py"
# 仅供本地 mock / 内网调试覆盖；生产默认走官方网关
REFRESH_URL = os.environ.get("WB_REFRESH_URL") or "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
API_BASE = os.environ.get("WB_API_BASE") or ""
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
DAILY_CLAIM = Path.home() / ".workbuddy" / "skills" / "workbuddy-daily-claim" / "scripts" / "claim.py"
DAILY_CLAIM_WEB_CRED = DAILY_CLAIM.parent.parent / "state" / "web-cred.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def env_home() -> Path:
    raw = os.environ.get("WB_ACCOUNTS_HOME")
    return (Path(raw).expanduser() if raw else Path.home() / ".workbuddy" / "wb-accounts").resolve()


HOME = env_home()
INDEX_FILE = HOME / "index.json"
SNAPSHOTS = HOME / "snapshots"
TOKENS = HOME / "tokens"
BACKUPS = HOME / "backups"
TRASH = HOME / "trash"
USAGE_LOG = HOME / "usage.jsonl"
# atRestSecretKey（仅用于只读解密客户端登录态信封）的保险库落盘位置：
# 与令牌同样 DPAPI/AES 加密；来源仍是「用户显式导入」，绝不主动去进程内存/其它文件找。
ATREST_FILE = HOME / "atrest.bin"


class VaultError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "acct"


def check_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise VaultError("账号 id 只允许字母/数字/下划线/连字符，长度 1-64")
    return value


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def fresh_index() -> dict:
    return {"version": 1, "created_at": now(), "vault_version": VERSION,
            "active": None, "accounts": [], "last_backup": None, "last_switch": None}


def load_index(create: bool = False) -> dict:
    if not INDEX_FILE.exists():
        if create:
            return fresh_index()
        raise VaultError("尚未初始化，先运行: wb_accounts.py init")
    try:
        d = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise VaultError("index.json 损坏: %s" % e)
    for k, v in (("accounts", []), ("active", None), ("last_backup", None), ("last_switch", None)):
        d.setdefault(k, v)
    return d


def save_index(idx: dict) -> None:
    atomic_write(INDEX_FILE, json.dumps(idx, ensure_ascii=False, indent=2).encode("utf-8"))


def log_event(event: str, **fields) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now(), "event": event, **fields}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_account(idx: dict, key: str) -> dict:
    for a in idx["accounts"]:
        if a["id"] == key or a["label"] == key:
            return a
    raise VaultError("找不到账号: %s" % key, "no_account")


def emit(payload: dict, as_json: bool, human: str = "") -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if as_json
          else (human or json.dumps(payload, ensure_ascii=False, indent=2)))


def ts_to_str(ms) -> str | None:
    if not ms or not isinstance(ms, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def expiry_verdict(ms) -> str:
    if not ms or not isinstance(ms, (int, float)):
        return "unknown"
    days = (ms / 1000 - time.time()) / 86400
    if days < 0:
        return "expired"
    if days < 7:
        return "expiring"
    return "ok"


# ---------------------------------------------------------------------------
# 客户端登录态：路径探测 / 只读元数据 / 进程检测
# ---------------------------------------------------------------------------
def login_state_candidates() -> list[Path]:
    home = Path.home()
    out = []
    for key in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(key)
        if base:
            out.append(Path(base) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info")
    out.append(home / ".workbuddy" / "auth" / "workbuddy-desktop.info")
    out.append(home / "Library" / "Application Support" / "CodeBuddyExtension" /
               "Data" / "Public" / "auth" / "workbuddy-desktop.info")
    out.append(Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))) /
               "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info")
    return out


def login_state_path() -> Path:
    override = os.environ.get("WB_LOGIN_STATE") or os.environ.get("WORKBUDDY_DESKTOP_INFO")
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise VaultError("指定的登录态文件不存在: %s" % p, "no_login_state")
        return p
    for p in login_state_candidates():
        if p.is_file():
            return p
    raise VaultError("找不到 WorkBuddy 登录态文件，请确认客户端已登录；"
                     "也可用 WB_LOGIN_STATE 显式指定路径", "no_login_state")


def read_login_state(path: Path | None = None):
    """返回 (原始字节, 解析后的 dict, 脱敏元数据)。绝不输出任何令牌内容。"""
    p = path or login_state_path()
    raw = p.read_bytes()
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise VaultError("登录态文件不是合法 JSON: %s" % e)
    auth, acc = d.get("auth", {}), d.get("account", {})
    uid = acc.get("uid", "")
    if not uid:
        raise VaultError("登录态缺少 account.uid（可能处于未登录状态）")
    meta = {
        "uid": uid,
        "uin": str(acc.get("uin", "")),
        "account_type": acc.get("type", ""),
        "domain": auth.get("domain", ""),
        "expires_at": auth.get("expiresAt"),
        "refresh_expires_at": auth.get("refreshExpiresAt"),
        "last_refresh_time": auth.get("lastRefreshTime"),
        "encrypted": crypto.is_envelope(auth.get("accessToken")),
        "key_id": crypto.envelope_key_id(auth.get("accessToken")),
        "fingerprint": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "path": str(p),
    }
    return raw, d, meta


def client_running() -> bool:
    if os.environ.get("WB_SKIP_CLIENT_CHECK") == "1":
        return False
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe"],
                               capture_output=True, text=True, timeout=8,
                               encoding="utf-8", errors="replace")
            return "WorkBuddy.exe" in (r.stdout or "")
        r = subprocess.run(["pgrep", "-f", "WorkBuddy"], capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 快照 / 令牌 存取（一律加密落盘）
# ---------------------------------------------------------------------------
def snapshot_path(acc_id: str) -> Path:
    return SNAPSHOTS / ("%s.bin" % check_id(acc_id))


def token_path(acc_id: str) -> Path:
    return TOKENS / ("%s.bin" % check_id(acc_id))


def write_sealed(path: Path, obj: bytes | dict) -> None:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    atomic_write(path, crypto.seal(raw))


def read_sealed(path: Path):
    if not path.is_file():
        return None
    return crypto.unseal(path.read_bytes())


def load_tokens(acc_id: str) -> dict:
    raw = read_sealed(token_path(acc_id))
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def save_tokens(acc_id: str, data: dict) -> None:
    write_sealed(token_path(acc_id), data)


def jwt_exp(token: str) -> int | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) * 1000 if exp else None
    except Exception:
        return None


def call_refresh(rt: str) -> dict:
    req = urllib.request.Request(REFRESH_URL, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"code": -1, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def refresh_tokens(acc: dict) -> dict:
    """用已保存的 RT 换新令牌并轮换回写。返回不含令牌值的状态 dict。"""
    store = load_tokens(acc["id"])
    rt = store.get("refresh_token")
    if not rt:
        raise VaultError("账号 %s 没有 refresh token；用 `token import %s --stdin` 导入一次，"
                         "或设置 WORKBUDDY_ATREST_KEY 让插件从快照只读解密" % (acc["id"], acc["id"]),
                         "no_token")
    r = call_refresh(rt)
    if r.get("code") != 0 or not isinstance(r.get("data"), dict):
        raise VaultError("刷新失败（code=%s msg=%s），该账号的 refresh token 可能已被重登吊销；"
                         "请在客户端重新登录该账号后 `refresh %s`，或重新导入 RT"
                         % (r.get("code"), r.get("msg"), acc["id"]), "refresh_failed")
    data = r["data"]
    at = data.get("accessToken") or ""
    new_rt = data.get("refreshToken") or rt
    if not at:
        raise VaultError("刷新应答里没有 accessToken", "refresh_failed")
    store.update({"access_token": at, "refresh_token": new_rt,
                  "at_expires_at": jwt_exp(at), "updated_at": now(), "source": "refresh"})
    save_tokens(acc["id"], store)
    log_event("keepalive", account=acc["id"], rotated=(new_rt != rt))
    return {"ok": True, "id": acc["id"], "rotated": new_rt != rt,
            "at_expires_at": ts_to_str(store.get("at_expires_at")),
            "access_token": at}


def resolve_at_rest_key() -> str | None:
    """atRestSecretKey 解析顺序：环境变量 / 密钥文件 / 保险库内已导入的 atrest.bin。

    注意：本函数不主动扫描进程内存、不读其它应用文件。保险库里的 atrest.bin
    必须由 `token atrest-import` 显式导入（通常来自一次性引导脚本，在客户端
    运行时从自身内存取出后落盘），与普通令牌一样 DPAPI/AES 加密。
    """
    k = crypto.load_at_rest_key()  # 环境变量 / 密钥文件
    if k:
        return k
    raw = read_sealed(ATREST_FILE)
    if not raw:
        return None
    try:
        return raw.decode("utf-8").strip()
    except Exception:
        return None


def token_from_snapshot(acc: dict) -> str | None:
    """可选通道：用户提供 atRestSecretKey 时，从快照只读解密令牌（不打印、不回写客户端）。"""
    raw = read_sealed(snapshot_path(acc["id"]))
    if not raw:
        return None
    sk = resolve_at_rest_key()
    try:
        d = json.loads(raw.decode("utf-8"))
        at_field = d.get("auth", {}).get("accessToken")
        rt_field = d.get("auth", {}).get("refreshToken")
        at = crypto.decrypt_login_field(at_field, secret=sk)
        store = load_tokens(acc["id"])
        if not store.get("refresh_token") and rt_field is not None:
            try:
                store["refresh_token"] = crypto.decrypt_login_field(rt_field, secret=sk)
                store["source"] = "snapshot-decrypt"
                save_tokens(acc["id"], store)
            except Exception:
                pass
        return at
    except Exception:
        return None


def get_access_token(acc: dict, force_refresh: bool = False) -> tuple[str, str]:
    """返回 (access_token, domain)。所有失败都给出可执行的下一步，绝不输出令牌明文。"""
    store = load_tokens(acc["id"])
    at = store.get("access_token")
    exp = store.get("at_expires_at")
    if at and not force_refresh and (not exp or exp / 1000 > time.time() + 60):
        return at, acc.get("domain") or "www.workbuddy.cn"
    if store.get("refresh_token"):
        r = refresh_tokens(acc)
        return r["access_token"], acc.get("domain") or "www.workbuddy.cn"
    at = token_from_snapshot(acc)
    if at:
        return at, acc.get("domain") or "www.workbuddy.cn"
    raise VaultError("账号 %s 没有可用通道。三种补法：\n"
                     "  1) 面板点「打开授权窗口」，用这个号扫码登录一次（网页会话通道，最快）\n"
                     "  2) `token import %s --stdin` 导入一次 refresh token（长期自动续期）\n"
                     "  3) 设置 WORKBUDDY_ATREST_KEY 让插件从快照只读解密"
                     % (acc["id"], acc["id"]), "no_token")


# ---------------------------------------------------------------------------
# 业务接口
# ---------------------------------------------------------------------------
def api_call(domain: str, token: str, path: str, method: str = "POST", payload=None) -> dict:
    if API_BASE:
        url = API_BASE.rstrip("/") + (path if path.startswith("/") else "/" + path)
    elif path.startswith("http"):
        url = path
    else:
        url = "https://" + domain + path
    data = None
    if method.upper() in ("POST", "PUT", "PATCH"):
        data = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Domain", domain)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return {"ok": False, "http_status": e.code, "response": json.loads(body)}
        except Exception:
            return {"ok": False, "http_status": e.code, "response": body[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        return {"ok": True, "response": json.loads(body)}
    except Exception:
        return {"ok": True, "response": body[:1000]}


# ---------------------------------------------------------------------------
# 账号视图
# ---------------------------------------------------------------------------
def web_session_info() -> dict:
    """daily-claim 的网页会话凭据（cookie / bearer）状态，以及它归属的账号 uid。

    官网已全面改用 HttpOnly cookie 会话，凭据落在 daily-claim 的 state/web-cred.json，
    不在本保险库的 tokens/ 里 —— 面板必须把这条通道一起认，否则会误报「未配令牌」。
    """
    out = {"configured": False, "kind": None, "saved_at": None, "uid": ""}
    try:
        if DAILY_CLAIM_WEB_CRED.is_file():
            d = json.loads(DAILY_CLAIM_WEB_CRED.read_text(encoding="utf-8"))
            cred = d.get("cred") or {}
            if cred.get("secret"):
                out.update(configured=True, kind=cred.get("kind"), saved_at=d.get("saved_at"))
    except Exception:
        pass
    try:
        o = json.loads((HOME / "web-owner.json").read_text(encoding="utf-8"))
        out["uid"] = o.get("uid") or ""
    except Exception:
        pass
    return out


def account_view(idx: dict, acc: dict, current_uid: str = "", web: dict | None = None) -> dict:
    store = load_tokens(acc["id"])
    if web is None:
        web = web_session_info()
    owned = bool(web.get("configured") and web.get("uid") and acc.get("uid")
                 and acc.get("uid") == web.get("uid"))
    has_token = bool(store.get("refresh_token") or store.get("access_token"))
    return {
        "id": acc["id"], "label": acc["label"],
        "uid_masked": crypto.mask(acc.get("uid", ""), 8, 4),
        "uin_masked": crypto.mask(acc.get("uin", ""), 3, 4),
        "account_type": acc.get("account_type", ""),
        "captured_at": acc.get("captured_at"), "refreshed_at": acc.get("refreshed_at"),
        "expires_at": ts_to_str(acc.get("expires_at")),
        "refresh_expires_at": ts_to_str(acc.get("refresh_expires_at")),
        "expiry": expiry_verdict(acc.get("expires_at")),
        "refresh_expiry": expiry_verdict(acc.get("refresh_expires_at")),
        "encrypted": acc.get("encrypted", False), "key_id": acc.get("key_id"),
        "snapshot_ok": snapshot_path(acc["id"]).is_file(),
        "token_channel": bool(store.get("refresh_token")),
        "web_session": owned, "web_kind": (web.get("kind") if owned else None),
        "channel": "token" if has_token else ("web" if owned else "none"),
        "token_updated_at": store.get("updated_at"),
        "is_current_client": bool(current_uid and current_uid == acc.get("uid")),
        "is_active": idx.get("active") == acc["id"],
        "note": acc.get("note", ""),
    }


def format_table(views: list, idx: dict) -> str:
    if not views:
        return "保险库为空。先在 WorkBuddy 客户端登录一个账号，再运行 `capture --label '主号'`。"
    lines = ["%-16s %-12s %-16s %-12s %-10s %-8s %s" %
             ("ID", "别名", "UID", "采集时间", "登录态", "令牌", "标记"), "-" * 92]
    for v in views:
        marks = []
        if v["is_current_client"]:
            marks.append("客户端当前")
        if v["is_active"]:
            marks.append("上次切换目标")
        lines.append("%-16s %-12s %-16s %-12s %-10s %-8s %s" %
                     (v["id"][:15], v["label"][:10], v["uid_masked"],
                      (v["captured_at"] or "")[:10], v["expiry"],
                      "已配置" if v["token_channel"] else "-", " ".join(marks)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def cmd_init(a) -> None:
    for d in (HOME, SNAPSHOTS, TOKENS, BACKUPS, TRASH):
        d.mkdir(parents=True, exist_ok=True)
    if INDEX_FILE.exists() and not a.force:
        idx = load_index()
        emit({"ok": True, "home": str(HOME), "accounts": len(idx["accounts"]),
              "message": "保险库已存在，未改动"}, a.json,
             "保险库已存在: %s（%d 个账号），未改动" % (HOME, len(idx["accounts"])))
        return
    if INDEX_FILE.exists() and a.force:
        bak = INDEX_FILE.with_name("index.json.bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        atomic_write(bak, INDEX_FILE.read_bytes())
    idx = load_index(create=True)
    save_index(idx)
    log_event("init")
    emit({"ok": True, "home": str(HOME), "accounts": 0,
          "next_step": "在 WorkBuddy 客户端登录一个账号，然后 `capture --label '主号'`"}, a.json,
         "初始化完成: %s\n下一步: capture --label '主号'" % HOME)


def cmd_capture(a) -> None:
    idx = load_index()
    raw, _d, meta = read_login_state()
    existing = next((x for x in idx["accounts"] if x["uid"] == meta["uid"]), None)
    if existing and not a.force:
        raise VaultError("该账号（uid %s）已登记为 %s；令牌轮换后请用 `refresh %s` 更新快照"
                         % (crypto.mask(meta["uid"], 8, 4), existing["id"], existing["id"]),
                         "already_exists")
    if a.force and existing:
        acc_id, created = existing["id"], False
    else:
        acc_id = check_id(a.id) if a.id else "%s-%s" % (slug(a.label or "acct"), uuid.uuid4().hex[:5])
        created = True
    write_sealed(snapshot_path(acc_id), raw)
    rec = {
        "id": acc_id, "label": a.label or acc_id, "uid": meta["uid"], "uin": meta["uin"],
        "account_type": meta["account_type"], "domain": meta["domain"],
        "expires_at": meta["expires_at"], "refresh_expires_at": meta["refresh_expires_at"],
        "encrypted": meta["encrypted"], "key_id": meta["key_id"],
        "fingerprint": meta["fingerprint"], "snapshot": "snapshots/%s.bin" % acc_id,
        "captured_at": now() if created else (existing or {}).get("captured_at"),
        "refreshed_at": now() if not created else None, "note": a.note or "",
    }
    if created:
        idx["accounts"].append(rec)
        if idx.get("active") is None:
            idx["active"] = acc_id
    else:
        idx["accounts"] = [rec if x["id"] == acc_id else x for x in idx["accounts"]]
    save_index(idx)
    log_event("capture", account=acc_id, uid_masked=crypto.mask(meta["uid"], 8, 4),
              encrypted=meta["encrypted"])
    payload = {"ok": True, "id": acc_id, "label": rec["label"],
               "uid_masked": crypto.mask(meta["uid"], 8, 4),
               "uin_masked": crypto.mask(meta["uin"], 3, 4),
               "expires_at": ts_to_str(meta["expires_at"]),
               "refresh_expires_at": ts_to_str(meta["refresh_expires_at"]),
               "encrypted": meta["encrypted"], "key_id": meta["key_id"],
               "bytes": meta["bytes"], "updated_existing": not created,
               "next_step": "再登录另一个账号后重复 capture；要长期免登录，"
                            "给每个账号 `token import <id> --stdin` 导入一次 refresh token"}
    emit(payload, a.json,
         "已%s账号 %s（%s / uid %s），登录态快照 %d 字节，access 到 %s\n%s"
         % ("更新" if not created else "采集", acc_id, rec["label"],
            payload["uid_masked"], meta["bytes"], payload["expires_at"], payload["next_step"]))


def cmd_capture_web(a) -> None:
    """把「网页授权登录」的账号登记进保险库（无客户端快照，走网页会话通道）。

    适用：用户刚在授权窗口登录了一个新号（web-owner.json 有归属 uid），但客户端
    从未登录过它。局限（汇报时要如实说明）：没有快照 → 不能把客户端切到它；
    没有 Bearer → 查不到账户积分余额；能做：签到 / 活动积分 / 派猫猫
    （run-as claim|status 走 cookie，归属校验按 uid 匹配）。
    """
    web = web_session_info()
    if not web.get("configured") or not web.get("uid"):
        raise VaultError("没有已归属的网页会话：先用面板「打开授权窗口」登录一次"
                         "（或终端跑 grab_web_cred.js），再来登记", "no_web_session")
    uid = web["uid"]
    idx = load_index()
    existing = next((x for x in idx["accounts"] if x.get("uid") == uid), None)
    if existing:
        raise VaultError("该账号（uid %s）已登记为 %s，无需重复登记"
                         % (crypto.mask(uid, 8, 4), existing["id"]), "already_exists")
    acc_id = check_id(a.id) if a.id else "web-" + uid.replace("-", "")[:8]
    rec = {
        "id": acc_id, "label": a.label or ("网页号 " + uid.replace("-", "")[:8]),
        "uid": uid, "uin": "", "account_type": "web", "domain": "www.workbuddy.cn",
        "encrypted": False, "key_id": None,
        "captured_at": now(), "refreshed_at": None,
        "note": a.note or "网页授权入库：无客户端快照，走网页会话通道",
    }
    idx["accounts"].append(rec)
    save_index(idx)
    log_event("capture-web", account=acc_id, uid_masked=crypto.mask(uid, 8, 4))
    payload = {"ok": True, "id": acc_id, "label": rec["label"],
               "uid_masked": crypto.mask(uid, 8, 4), "channel": "web",
               "snapshot_ok": False, "token_channel": False,
               "limits": "仅网页会话通道：可签到/查活动积分；查账户积分余额或切换客户端"
                         "需先在客户端登录该号再 capture",
               "next_step": "面板刷新即可看到该号；想补全通道，在客户端登录它后自动采集+token auto"}
    emit(payload, a.json,
         "已登记网页授权账号 %s（uid %s）——可签到/查活动积分。\n%s"
         % (acc_id, payload["uid_masked"], payload["limits"]))


def cmd_refresh(a) -> None:
    idx = load_index()
    acc = get_account(idx, a.key)
    raw, _d, meta = read_login_state()
    if meta["uid"] != acc["uid"]:
        raise VaultError("拒绝覆盖：当前客户端登录的是 uid %s，而账号 %s 的 uid 是 %s。"
                         "请先在客户端切换到该账号，或用 `capture` 登记当前账号"
                         % (crypto.mask(meta["uid"], 8, 4), acc["id"],
                            crypto.mask(acc["uid"], 8, 4)), "uid_mismatch")
    write_sealed(snapshot_path(acc["id"]), raw)
    acc.update({"expires_at": meta["expires_at"], "refresh_expires_at": meta["refresh_expires_at"],
                "encrypted": meta["encrypted"], "key_id": meta["key_id"],
                "fingerprint": meta["fingerprint"], "refreshed_at": now(), "domain": meta["domain"]})
    save_index(idx)
    log_event("refresh", account=acc["id"])
    emit({"ok": True, "id": acc["id"], "fingerprint": meta["fingerprint"],
          "expires_at": ts_to_str(meta["expires_at"]),
          "message": "已用当前客户端登录态刷新快照（捕获令牌轮换后的最新状态）"}, a.json,
         "已刷新 %s 的快照，access 到 %s" % (acc["id"], ts_to_str(meta["expires_at"])))


def cmd_current(a) -> None:
    _raw, _d, meta = read_login_state()
    idx = load_index()
    hit = next((x for x in idx["accounts"] if x["uid"] == meta["uid"]), None)
    payload = {"ok": True, "uid_masked": crypto.mask(meta["uid"], 8, 4),
               "uin_masked": crypto.mask(meta["uin"], 3, 4),
               "encrypted": meta["encrypted"], "key_id": meta["key_id"],
               "expires_at": ts_to_str(meta["expires_at"]),
               "expiry": expiry_verdict(meta["expires_at"]),
               "registered": bool(hit), "account": ({"id": hit["id"], "label": hit["label"]} if hit else None),
               "active": idx.get("active"),
               "next_step": ("该账号尚未登记，运行 `capture --label '别名'`" if not hit
                             else "该账号已登记，可用 `switch` / `run-as` / `keepalive`")}
    emit(payload, a.json,
         "客户端当前登录: uid %s / uin %s / %s / access 到 %s\n%s"
         % (payload["uid_masked"], payload["uin_masked"],
            ("信封加密" if meta["encrypted"] else "明文"), payload["expires_at"],
            payload["next_step"]))


def cmd_list(a) -> None:
    idx = load_index()
    try:
        _raw, _d, meta = read_login_state()
        cur = meta["uid"]
    except VaultError:
        cur = ""
    _web = web_session_info()
    views = [account_view(idx, x, cur, _web) for x in idx["accounts"]]
    emit({"ok": True, "home": str(HOME), "active": idx.get("active"), "accounts": views}, a.json,
         format_table(views, idx))


def cmd_brief(a) -> None:
    try:
        idx = load_index()
    except VaultError:
        emit({"ok": True, "initialized": False, "message": "保险库未初始化，运行 init"}, a.json,
             "WorkBuddy 多账号保险库未初始化（运行 init）")
        return
    try:
        _raw, _d, meta = read_login_state()
        cur_uid, cur_masked = meta["uid"], crypto.mask(meta["uid"], 8, 4)
    except VaultError:
        cur_uid, cur_masked = "", "未知"
    _web = web_session_info()
    views = [account_view(idx, x, cur_uid, _web) for x in idx["accounts"]]
    warn = [v["id"] for v in views if v["expiry"] in ("expired", "expiring")]
    no_token = [v["id"] for v in views if not v["token_channel"] and not v["web_session"]]
    payload = {"ok": True, "initialized": True, "count": len(views),
               "current_uid_masked": cur_masked, "active": idx.get("active"),
               "expiring": warn, "no_token_channel": no_token,
               "accounts": [{"id": v["id"], "label": v["label"], "uid_masked": v["uid_masked"],
                             "is_current": v["is_current_client"], "expiry": v["expiry"],
                             "token_channel": v["token_channel"],
                             "web_session": v["web_session"], "channel": v["channel"]}
                            for v in views],
               "hint": "切换客户端账号用 switch（需先退出客户端）；"
                       "不切换而按账号跑任务用 run-as / keepalive"}
    emit(payload, a.json,
         "WorkBuddy 多账号: %d 个，客户端当前 uid %s，上次切换目标 %s；"
         "临近过期: %s；未配令牌通道: %s"
         % (len(views), cur_masked, idx.get("active") or "无",
            ", ".join(warn) or "无", ", ".join(no_token) or "无"))


def cmd_switch(a) -> None:
    idx = load_index()
    acc = get_account(idx, a.key)
    snap = snapshot_path(acc["id"])
    if not snap.is_file():
        raise VaultError("账号 %s 的快照不存在" % acc["id"], "no_snapshot")
    raw = read_sealed(snap)
    try:
        d = json.loads(raw.decode("utf-8"))
        if d.get("account", {}).get("uid") != acc["uid"]:
            raise VaultError("快照 uid 与索引不一致，已拒绝写入（防止串号）", "uid_mismatch")
    except VaultError:
        raise
    except Exception as e:
        raise VaultError("快照损坏: %s" % e)
    running = client_running()
    if running and not a.allow_running and not a.dry_run:
        raise VaultError(
            "WorkBuddy 客户端正在运行。客户端退出时会把内存中的登录态写回文件，"
            "此时切换会被覆写。请【完全退出 WorkBuddy（含托盘）】后，在终端运行：\n"
            "  python wb_accounts.py switch %s\n"
            "或用 bin/switch.cmd 双击切换，然后重新打开 WorkBuddy。"
            "（--allow-running 可强制写入，但不保证生效；--dry-run 只预览）" % acc["id"],
            "client_running")
    if a.dry_run:
        emit({"ok": True, "dry_run": True, "id": acc["id"], "label": acc["label"],
              "uid_masked": crypto.mask(acc["uid"], 8, 4),
              "target": str(login_state_path()), "client_running": running,
              "would_backup": True, "would_write_bytes": len(raw),
              "warning": "客户端仍在运行，写入后必须完全退出再重开才可能生效" if running else ""},
             a.json, "预览：会把 %s（uid %s）的 %d 字节快照写回 %s；客户端运行中=%s"
             % (acc["id"], crypto.mask(acc["uid"], 8, 4), len(raw), login_state_path(), running))
        return
    # 1) 先备份当前登录态（即使当前文件损坏也不跳过切换，但记录警告）
    backup_note = None
    try:
        cur_raw, _cd, cur_meta = read_login_state()
        bak = BACKUPS / ("%s-%s.bin" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
                                        cur_meta["uid"][:8]))
        write_sealed(bak, cur_raw)
        idx["last_backup"] = {"file": str(bak.relative_to(HOME)),
                              "uid": cur_meta["uid"], "at": now(),
                              "uid_masked": crypto.mask(cur_meta["uid"], 8, 4)}
    except Exception as e:
        backup_note = "当前登录态备份失败（%s），已跳过备份" % e
    # 2) 原子写回目标快照
    target = login_state_path()
    atomic_write(target, raw, 0o600)
    idx["active"] = acc["id"]
    idx["last_switch"] = {"id": acc["id"], "at": now(), "uid_masked": crypto.mask(acc["uid"], 8, 4)}
    save_index(idx)
    log_event("switch", account=acc["id"], client_running=running, backup=not backup_note)
    payload = {"ok": True, "id": acc["id"], "label": acc["label"],
               "uid_masked": crypto.mask(acc["uid"], 8, 4),
               "target": str(target), "bytes": len(raw), "client_running": running,
               "backup": idx.get("last_backup"),
               "warning": backup_note or ("⚠ 客户端仍在运行，写入可能被退出时的回写覆盖；"
                                          "请完全退出并重开 WorkBuddy" if running else ""),
               "next_step": "重新打开 WorkBuddy 即为该账号；若不对，运行 `rollback` 回滚"}
    emit(payload, a.json,
         "已把 %s（%s）的登录态写回 %s。%s\n%s"
         % (acc["id"], acc["label"], target, payload["warning"] or "备份已完成。",
            payload["next_step"]))


def cmd_rollback(a) -> None:
    idx = load_index()
    b = idx.get("last_backup")
    if not b or not (HOME / b["file"]).is_file():
        raise VaultError("没有可回滚的备份", "no_backup")
    if client_running() and not a.allow_running:
        raise VaultError("WorkBuddy 客户端正在运行，请先完全退出后再回滚（或加 --allow-running）",
                         "client_running")
    raw = read_sealed(HOME / b["file"])
    try:
        json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise VaultError("备份损坏: %s" % e)
    atomic_write(login_state_path(), raw, 0o600)
    idx["active"] = None
    save_index(idx)
    log_event("rollback", uid_masked=b.get("uid_masked"))
    emit({"ok": True, "restored_uid_masked": b.get("uid_masked"), "from": b["file"],
          "next_step": "重新打开 WorkBuddy 生效"}, a.json,
         "已回滚到 %s 的登录态，重新打开 WorkBuddy 生效" % b.get("uid_masked"))


def cmd_label(a) -> None:
    idx = load_index()
    acc = get_account(idx, a.key)
    old = acc["label"]
    acc["label"] = a.name
    save_index(idx)
    emit({"ok": True, "id": acc["id"], "label": a.name, "old_label": old}, a.json,
         "已把 %s 的别名从「%s」改为「%s」" % (acc["id"], old, a.name))


def cmd_remove(a) -> None:
    idx = load_index()
    acc = get_account(idx, a.key)
    dst = TRASH / ("%s-%s" % (acc["id"], dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    dst.mkdir(parents=True, exist_ok=True)
    for src in (snapshot_path(acc["id"]), token_path(acc["id"])):
        if src.is_file():
            shutil.move(str(src), str(dst / src.name))
    idx["accounts"] = [x for x in idx["accounts"] if x["id"] != acc["id"]]
    if idx.get("active") == acc["id"]:
        idx["active"] = None
    save_index(idx)
    log_event("remove", account=acc["id"])
    emit({"ok": True, "id": acc["id"], "archived_to": str(dst),
          "message": "快照与令牌已移入回收站，未真正删除"}, a.json,
         "账号 %s 已归档到 %s（未真正删除）" % (acc["id"], dst))


# ---------------------------------------------------------------------------
# 令牌通道
# ---------------------------------------------------------------------------
def auto_configure_tokens() -> dict:
    """打开可视化面板时的自动令牌通道配置（纯读盘，不扫描任何进程内存）。

    对每个已登记账号：
      1) 保险库已有 refresh token -> refresh_tokens() 续期并立即验证；
      2) 没有 RT、但有该账号自己的登录态快照且 atRestSecretKey 已固化 ->
         从快照只读解密 refreshToken 写进保险库并立即验证。与 `token recover`
         不同，这里不要求「当前客户端正登录该账号」，多号并存时每个号都能各自配好；
      3) 都不满足 -> 列出明确原因，不影响其它账号。

    全程不打印、不返回任何令牌明文；密钥缺失时由调用方决定是否做一次性引导。
    """
    idx = load_index()
    sk = resolve_at_rest_key()
    results: list[dict] = []
    for acc in idx.get("accounts", []):
        aid = acc["id"]
        store = load_tokens(aid)
        if store.get("refresh_token"):
            try:
                r = refresh_tokens(acc)
                results.append({"id": aid, "action": "renewed", "ok": True,
                                "at_expires_at": r["at_expires_at"]})
            except VaultError as e:
                results.append({"id": aid, "action": "renew_failed", "ok": False,
                                "code": e.code, "error": str(e)[:220]})
            continue
        raw = read_sealed(snapshot_path(aid))
        if not raw:
            results.append({"id": aid, "action": "skip", "ok": False,
                            "reason": "无登录态快照，且未配令牌通道"})
            continue
        if not sk:
            results.append({"id": aid, "action": "skip", "ok": False,
                            "reason": "缺 atRestSecretKey，无法解密快照（需一次性引导）"})
            continue
        try:
            d = json.loads(raw.decode("utf-8"))
            rt_field = (d.get("auth") or {}).get("refreshToken")
            rt = crypto.decrypt_login_field(rt_field, secret=sk)
            if len(rt) < 20:
                raise ValueError("解密出的 refreshToken 长度异常")
        except Exception as e:
            results.append({"id": aid, "action": "skip", "ok": False,
                            "reason": "快照解密失败：%s" % type(e).__name__})
            continue
        store.update({"refresh_token": rt, "updated_at": now(), "source": "auto-recover"})
        save_tokens(aid, store)
        try:
            r = refresh_tokens(acc)
            results.append({"id": aid, "action": "recovered", "ok": True,
                            "at_expires_at": r["at_expires_at"]})
        except VaultError as e:
            # 恢复出的 RT 不可用就回滚，别在保险库里留坏凭据反复失败
            store.pop("refresh_token", None)
            save_tokens(aid, store)
            results.append({"id": aid, "action": "recover_failed", "ok": False,
                            "code": e.code, "error": str(e)[:220]})
    ready = sum(1 for r in results if r.get("ok"))
    log_event("token.auto", accounts=len(results), ready=ready, atrest_key=bool(sk))
    return {"ok": True, "atrest_key": bool(sk), "count": len(results), "ready": ready,
            "results": results, "note": "只输出状态，不输出任何令牌明文"}


def cmd_token(a) -> None:
    idx = load_index()
    sub = a.token_action
    if sub == "auto":
        res = auto_configure_tokens()
        label = {"renewed": "已自动续期", "recovered": "已从快照自动恢复并验证",
                 "renew_failed": "续期失败", "recover_failed": "恢复失败", "skip": "未完成"}
        lines = ["  %s: %s%s" % (r["id"], label.get(r["action"], r["action"]),
                                 ("（%s）" % (r.get("reason") or r.get("error") or ""))[:120]
                                 if not r.get("ok") else "")
                 for r in res["results"]]
        emit(res, a.json,
             "自动配置令牌通道：%d/%d 就绪，atRestSecretKey %s\n%s"
             % (res["ready"], res["count"], "已固化" if res["atrest_key"] else "未固化",
                "\n".join(lines)))
        return
    if sub == "atrest-import":
        # 仅导入 atRestSecretKey，不绑定具体账号（key 可选）
        if a.stdin:
            value = sys.stdin.read().strip()
        elif a.file:
            value = Path(a.file).read_text(encoding="utf-8").strip()
        elif a.value:
            value = a.value.strip()
        else:
            raise VaultError("用 --stdin（推荐）/ --file / --value 提供 atRestSecretKey")
        try:
            _key, kid = crypto.derive_at_rest_key(value)
        except Exception as e:
            raise VaultError("atRestSecretKey 非法：%s" % e, "bad_atrest")
        write_sealed(ATREST_FILE, value.encode("utf-8"))
        log_event("token.atrest-import", key_id=kid)
        emit({"ok": True, "stored": True, "key_id": kid,
              "encrypted_with": "Windows DPAPI" if platform.system() == "Windows" else "AES-256-GCM",
              "next_step": "之后 `token recover <id>` 可从客户端登录态自动恢复 refresh token"}, a.json,
             "已加密保存 atRestSecretKey（keyId %s）到保险库；之后 token recover 可自动恢复令牌通道"
             % kid)
        return
    acc = get_account(idx, a.key)
    if sub == "import":
        if a.stdin:
            value = sys.stdin.read().strip()
        elif a.file:
            value = Path(a.file).read_text(encoding="utf-8").strip()
        elif a.value:
            value = a.value.strip()
        else:
            raise VaultError("用 --stdin（推荐，不留命令行痕迹）/ --file / --value 提供 refresh token")
        if len(value) < 20:
            raise VaultError("refresh token 看起来太短，请确认粘贴完整")
        store = load_tokens(acc["id"])
        store.update({"refresh_token": value, "updated_at": now(), "source": "import"})
        save_tokens(acc["id"], store)
        log_event("token.import", account=acc["id"])
        emit({"ok": True, "id": acc["id"], "stored": True,
              "token_masked": value[:6] + "…" + value[-4:],
              "encrypted_with": "Windows DPAPI" if platform.system() == "Windows" else "AES-256-GCM",
              "next_step": "运行 `keepalive %s` 验证并换取新 access token" % acc["id"]}, a.json,
             "已加密保存 %s 的 refresh token（%s…%s），后续可自动续期"
             % (acc["id"], value[:6], value[-4:]))
    elif sub == "status":
        store = load_tokens(acc["id"])
        rt = store.get("refresh_token")
        emit({"ok": True, "id": acc["id"], "configured": bool(rt),
              "source": store.get("source"), "updated_at": store.get("updated_at"),
              "access_token_cached": bool(store.get("access_token")),
              "at_expires_at": ts_to_str(store.get("at_expires_at")),
              "hint": "不显示令牌明文；用 keepalive 续期，用 forget 清除"}, a.json,
             "%s 令牌通道: %s（来源 %s，更新 %s）"
             % (acc["id"], "已配置" if rt else "未配置",
                store.get("source") or "-", store.get("updated_at") or "-"))
    elif sub == "forget":
        p = token_path(acc["id"])
        if p.is_file():
            TRASH.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(TRASH / ("%s-token-%s.bin" %
                                             (acc["id"], dt.datetime.now().strftime("%Y%m%d-%H%M%S")))))
        emit({"ok": True, "id": acc["id"], "cleared": True}, a.json,
             "已清除 %s 的令牌通道（归档，未真删）" % acc["id"])
    elif sub == "recover":
        # 从客户端「在盘登录态」恢复该账号的 refresh token 进保险库令牌通道。
        # 与 capture/refresh 同源地只读 workbuddy-desktop.info，不写客户端、不扫内存。
        sk = resolve_at_rest_key()
        if not sk:
            raise VaultError("recover 需要先有 atRestSecretKey：运行 `token atrest-import --stdin` "
                             "一次（或在环境里设置 WORKBUDDY_ATREST_KEY / WORKBUDDY_ATREST_KEY_FILE）",
                             "no_atrest")
        try:
            raw_login, login_d, meta = read_login_state()
        except VaultError as e:
            raise VaultError("读不到客户端登录态：%s；请确认客户端登录过且文件未被清理" % e, "no_login_state")
        if meta["uid"] != acc["uid"]:
            raise VaultError("拒绝：当前客户端登录的是 uid %s，而账号 %s 的 uid 是 %s；"
                             "recover 只恢复与本账号同 uid 的令牌"
                             % (crypto.mask(meta["uid"], 8, 4), acc["id"], crypto.mask(acc["uid"], 8, 4)),
                             "uid_mismatch")
        # 顺手用当前登录态刷新快照（与 refresh 同效，方便下次 token_from_snapshot 也能用）
        write_sealed(snapshot_path(acc["id"]), raw_login)
        acc.update({"expires_at": meta["expires_at"], "refresh_expires_at": meta["refresh_expires_at"],
                    "encrypted": meta["encrypted"], "key_id": meta["key_id"],
                    "fingerprint": meta["fingerprint"], "refreshed_at": now(), "domain": meta["domain"]})
        idx = load_index(); save_index(idx)
        rt_field = (login_d or {}).get("auth", {}).get("refreshToken")
        if not crypto.is_envelope(rt_field):
            raise VaultError("refreshToken 不是信封（已是明文？），直接用 `token import` 导入即可",
                             "no_envelope")
        try:
            rt = crypto.decrypt_login_field(rt_field, secret=sk)
        except Exception as e:
            raise VaultError("解密 refreshToken 失败：%s（atRestSecretKey 与信封 keyId 不匹配？）"
                             % e, "decrypt_failed")
        if len(rt) < 20:
            raise VaultError("解密出的 refreshToken 长度异常，疑似密钥不匹配", "decrypt_failed")
        store = load_tokens(acc["id"])
        store.update({"refresh_token": rt, "updated_at": now(), "source": "recover"})
        save_tokens(acc["id"], store)
        r = refresh_tokens(acc)  # 立即用新 RT 换 AT 验证链路
        emit({"ok": True, "id": acc["id"], "recovered": True, "rotated": r["rotated"],
              "at_expires_at": r["at_expires_at"],
              "next_step": "令牌通道已恢复并验证；`keepalive --all` 会长期续期"}, a.json,
             "已从客户端登录态恢复 %s 的 refresh token 并验证成功（access 到 %s）"
             % (acc["id"], r["at_expires_at"]))
    else:
        raise VaultError("token 子命令只能是 auto/import/status/forget/recover/atrest-import")


def cmd_keepalive(a) -> None:
    idx = load_index()
    if not a.all and not a.key:
        raise VaultError("请指定账号 id，或用 `keepalive --all` 批量续期全部账号")
    keys = [x["id"] for x in idx["accounts"]] if a.all else [get_account(idx, a.key)["id"]]
    results = []
    for k in keys:
        acc = get_account(idx, k)
        try:
            r = refresh_tokens(acc)
            results.append({"id": k, "ok": True, "rotated": r["rotated"],
                            "at_expires_at": r["at_expires_at"]})
        except VaultError as e:
            results.append({"id": k, "ok": False, "error": str(e), "code": e.code})
    ok = sum(1 for r in results if r["ok"])
    emit({"ok": ok == len(results), "count": len(results), "succeeded": ok, "results": results,
          "note": "只输出状态，不输出任何令牌明文"}, a.json,
         "\n".join("  %s: %s" % (r["id"], "续期成功" if r["ok"] else r.get("error"))
                   for r in results) + ("\n成功 %d/%d" % (ok, len(results)) if results else "保险库为空"))


def _run_daily_claim(rest: list, bearer: str = "") -> dict:
    """委托 daily-claim 执行（status / claim 等）。

    bearer 为空 = 该账号没有自己的保险库令牌，只有网页会话凭据。此时必须强制
    WEB-ONLY：claim.py 的 state/token.json 是全局唯一一份（属于另一个账号），
    只清 WORKBUDDY_ACCESS_TOKEN 不够，它会命中那份缓存，导致积分/签到串号。
    """
    env = dict(os.environ)
    if bearer:
        env["WORKBUDDY_ACCESS_TOKEN"] = bearer
        env.pop("WORKBUDDY_CLAIM_WEB_ONLY", None)
    else:
        env.pop("WORKBUDDY_ACCESS_TOKEN", None)
        env["WORKBUDDY_CLAIM_WEB_ONLY"] = "1"  # 禁用全局 Bearer 缓存，只用该账号的网页会话
    try:
        r = subprocess.run([sys.executable, str(DAILY_CLAIM), *rest],
                           env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except Exception as e:
        raise VaultError("调用 daily-claim 失败: %s" % e)
    return {"ok": r.returncode == 0, "exit_code": r.returncode,
            "stdout": (r.stdout or "")[:4000], "stderr": (r.stderr or "")[:1000]}


def cmd_run_as(a) -> None:
    idx = load_index()
    acc = get_account(idx, a.key)
    action = a.action
    rest = [x for x in (a.rest or []) if x != "--"]
    force_refresh = "--force-refresh" in rest
    rest = [x for x in rest if x != "--force-refresh"]
    # claim / status 可以走「网页会话凭据」通道：此时不需要保险库令牌，交给 claim.py 自己用
    web = web_session_info()
    web_owned = bool(web.get("configured") and web.get("uid")
                     and acc.get("uid") and acc.get("uid") == web.get("uid"))
    at = ""
    domain = acc.get("domain") or "www.workbuddy.cn"
    no_vault_token = False
    try:
        at, domain = get_access_token(acc, force_refresh=force_refresh)
    except VaultError as e:
        if e.code != "no_token" or action not in ("claim", "status") or not web_owned:
            raise
        no_vault_token = True  # 没有保险库令牌但网页会话归属本账号 → 回落 cookie 通道
    if action == "status":
        if no_vault_token:
            # 只有网页会话（cookie）：经 daily-claim 查签到站状态，无需 Bearer
            res = _run_daily_claim(["status"], bearer="")
            if not res.get("ok"):
                res = {"ok": False, "error": (res.get("stderr") or res.get("stdout") or "查询失败").strip()[:600]}
        else:
            res = api_call(domain, at, STATUS_PATH, "POST", {})
    elif action == "raw":
        if len(rest) < 2:
            raise VaultError("raw 需要 <METHOD> <PATH>，例如 `run-as main raw GET /v2/billing/meter/...`")
        method, path = rest[0], rest[1]
        payload = None
        if "--data" in rest:
            i = rest.index("--data")
            if i + 1 >= len(rest):
                raise VaultError("--data 后缺少 JSON")
            try:
                payload = json.loads(rest[i + 1])
            except Exception as e:
                raise VaultError("--data 不是合法 JSON: %s" % e)
        res = api_call(domain, at, path, method, payload)
    elif action == "claim":
        if not DAILY_CLAIM.is_file():
            raise VaultError("未找到 daily-claim 技能: %s；可用 `run-as <id> raw ...` 直接调接口"
                             % DAILY_CLAIM, "no_claim_skill")
        res = _run_daily_claim(rest, bearer=at)
    else:
        raise VaultError("未知动作: %s（支持 status / raw / claim）" % action)
    log_event("run_as", account=acc["id"], action=action, ok=res.get("ok", False))
    emit({"ok": res.get("ok", False), "id": acc["id"], "label": acc["label"],
          "action": action, "domain": domain, "result": res}, a.json,
         "以 %s（%s）执行 %s: %s" % (acc["id"], acc["label"], action,
                                    json.dumps(res, ensure_ascii=False)[:1200]))


# ---------------------------------------------------------------------------
# 报告 / 导出 / 自检
# ---------------------------------------------------------------------------
def cmd_where(a) -> None:
    emit({"ok": True, "home": str(HOME), "index": str(INDEX_FILE),
          "snapshots": str(SNAPSHOTS), "tokens": str(TOKENS), "backups": str(BACKUPS),
          "login_state": (str(login_state_path()) if _state_exists() else "未找到"),
          "env_overrides": ["WB_ACCOUNTS_HOME", "WB_LOGIN_STATE", "WB_SKIP_CLIENT_CHECK"]},
         a.json, str(HOME))


def _state_exists() -> bool:
    try:
        login_state_path()
        return True
    except VaultError:
        return False


def cmd_export_state(a) -> None:
    idx = load_index()
    try:
        _raw, _d, meta = read_login_state()
        cur = meta["uid"]
    except VaultError:
        cur = ""
    _web = web_session_info()
    views = [account_view(idx, x, cur, _web) for x in idx["accounts"]]
    payload = {"ok": True, "version": VERSION, "exported_at": now(),
               "note": "不含任何令牌、信封、昵称或手机号明文，可安全放入上下文或备份",
               "active": idx.get("active"), "accounts": views}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if a.out:
        p = Path(a.out).expanduser()
        atomic_write(p, text.encode("utf-8"), 0o644)
        emit({"ok": True, "out": str(p), "accounts": len(views)}, a.json,
             "已导出 %d 个账号的状态到 %s（不含凭据明文）" % (len(views), p))
    else:
        print(text)


def cmd_report(a) -> None:
    idx = load_index()
    try:
        _raw, _d, meta = read_login_state()
        cur = meta["uid"]
    except VaultError:
        cur = ""
    _web = web_session_info()
    views = [account_view(idx, x, cur, _web) for x in idx["accounts"]]
    out = Path(a.out).expanduser() if a.out else HOME / "report.html"
    atomic_write(out, render_report(views, idx).encode("utf-8"), 0o644)
    emit({"ok": True, "out": str(out), "accounts": len(views)}, a.json, "状态面板: %s" % out)


def render_report(views: list, idx: dict) -> str:
    import html as H
    color = {"ok": "#3fb950", "expiring": "#d29922", "expired": "#f85149", "unknown": "#8b949e"}
    rows = []
    for v in views:
        c = color.get(v["expiry"], "#8b949e")
        tags = []
        if v["is_current_client"]:
            tags.append('<span class="tag cur">客户端当前</span>')
        if v["is_active"]:
            tags.append('<span class="tag">上次切换目标</span>')
        if v["token_channel"]:
            tags.append('<span class="tag ok">令牌通道</span>')
        rows.append("<tr><td><code>%s</code></td><td>%s</td><td><code>%s</code></td>"
                    "<td><span class='dot' style='background:%s'></span>%s</td>"
                    "<td class='mono'>%s</td><td class='mono'>%s</td><td>%s</td></tr>"
                    % (H.escape(v["id"]), H.escape(v["label"]), H.escape(v["uid_masked"]),
                       c, H.escape(v["expiry"]), H.escape((v["captured_at"] or "")[:19]),
                       H.escape((v["expires_at"] or "")[:19]), "".join(tags)))
    warn = [v for v in views if v["expiry"] in ("expired", "expiring")]
    alert = ("<div class='alert'>⚠ %d 个账号登录态临近/已过期：%s。可在客户端登录该账号后 "
             "<code>refresh &lt;id&gt;</code>，或用 <code>keepalive</code> 续期令牌通道。</div>"
             % (len(warn), ", ".join(H.escape(v["id"]) for v in warn))) if warn else \
            "<div class='ok'>✓ 全部账号登录态在有效期内</div>"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>WorkBuddy 多账号面板</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;padding:32px;background:#0d1117;color:#e6edf3;
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1120px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}
.sub{color:#8b949e;margin-bottom:22px;font-size:13px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px;margin-bottom:18px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#8b949e;font-weight:600;padding:9px 10px;border-bottom:1px solid #30363d}
td{padding:10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:last-child td{border-bottom:0}code{background:#21262d;padding:2px 6px;border-radius:4px;font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}
.mono{font-family:Consolas,monospace;font-size:12px;color:#8b949e}
.tag{display:inline-block;background:#21262d;border:1px solid #30363d;border-radius:12px;
padding:1px 8px;font-size:11px;margin-right:5px;color:#8b949e}
.tag.cur{background:#1f6feb33;border-color:#1f6feb;color:#79c0ff}
.tag.ok{background:#3fb95022;border-color:#3fb950;color:#3fb950}
.alert{background:#d2992222;border:1px solid #d29922;border-radius:8px;padding:12px 16px;margin-bottom:18px;color:#e3b341}
.ok{background:#3fb95022;border:1px solid #3fb950;border-radius:8px;padding:12px 16px;margin-bottom:18px;color:#3fb950}
pre{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:14px;overflow:auto;font-size:12px;margin:0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px}
.kpi{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}
.kpi b{display:block;font-size:26px;line-height:1.2}.kpi span{color:#8b949e;font-size:12px}
footer{color:#6e7681;font-size:12px;margin-top:20px;text-align:center}
</style></head><body><div class="wrap">
<h1>WorkBuddy 多账号面板</h1>
<div class="sub">wb-accounts v""" + VERSION + " · 生成于 " + H.escape(now()) + """</div>
<div class="grid">
<div class="kpi"><b>""" + str(len(views)) + """</b><span>已登记账号</span></div>
<div class="kpi"><b>""" + str(sum(1 for v in views if v["token_channel"])) + """</b><span>已配令牌通道</span></div>
<div class="kpi"><b>""" + str(len(warn)) + """</b><span>临近/已过期</span></div></div>
""" + alert + """
<div class="card"><table><thead><tr><th>ID</th><th>别名</th><th>UID</th><th>登录态</th>
<th>采集时间</th><th>access 到期</th><th>标记</th></tr></thead><tbody>""" + \
        ("".join(rows) or "<tr><td colspan='7'>暂无账号</td></tr>") + """</tbody></table></div>
<div class="card"><h2 style="font-size:15px;margin:0 0 10px">常用命令</h2><pre>
python scripts/wb_accounts.py brief --json          # 一屏概览
python scripts/wb_accounts.py capture --label "主号"  # 采集当前客户端账号
python scripts/wb_accounts.py switch &lt;id&gt;          # 切换客户端账号（需先退出客户端）
python scripts/wb_accounts.py token import &lt;id&gt; --stdin   # 导入 refresh token
python scripts/wb_accounts.py keepalive --all       # 批量续期，长期免登录
python scripts/wb_accounts.py run-as &lt;id&gt; status     # 以该账号身份调接口</pre></div>
<footer>本页不含任何令牌 / 信封 / 昵称 / 手机号明文 · 快照与令牌均加密落盘于本机</footer>
</div></body></html>"""


def cmd_doctor(a) -> None:
    checks = []
    checks.append({"item": "Python", "ok": sys.version_info >= (3, 8), "detail": sys.version.split()[0]})
    checks.append({"item": "平台", "ok": True, "detail": platform.system()})
    checks.append({"item": "保险库目录可写", "ok": os.access(str(HOME.parent if HOME.exists() else Path.home()), os.W_OK),
                   "detail": str(HOME)})
    try:
        p = login_state_path()
        _raw, _d, meta = read_login_state(p)
        checks.append({"item": "登录态文件", "ok": True,
                       "detail": "%s (%d B, %s)" % (p, meta["bytes"],
                                                    "信封加密" if meta["encrypted"] else "明文")})
        checks.append({"item": "信封 keyId", "ok": bool(meta["key_id"] or not meta["encrypted"]),
                       "detail": meta["key_id"] or "无（明文登录态）"})
    except VaultError as e:
        checks.append({"item": "登录态文件", "ok": False, "detail": str(e)})
    checks.append({"item": "客户端进程", "ok": True,
                   "detail": "运行中（switch 需先完全退出）" if client_running() else "未运行（可执行 switch）"})
    try:
        ok = crypto.unseal(crypto.seal(b"selftest")) == b"selftest"
        checks.append({"item": "保险库加密自检", "ok": ok,
                       "detail": "Windows DPAPI(当前用户)" if platform.system() == "Windows" else "AES-256-GCM"})
    except Exception as e:
        checks.append({"item": "保险库加密自检", "ok": False, "detail": str(e)})
    checks.append({"item": "AES-GCM 后端", "ok": crypto.crypto_backend() != "none",
                   "detail": crypto.crypto_backend() + "（仅解密客户端信封/非Windows时需要）"})
    checks.append({"item": "atRestSecretKey", "ok": bool(crypto.load_at_rest_key()),
                   "detail": "已显式提供，可从快照只读解密令牌" if crypto.load_at_rest_key()
                   else "未提供（不影响 token import 通道）"})
    checks.append({"item": "daily-claim 技能", "ok": DAILY_CLAIM.is_file(),
                   "detail": str(DAILY_CLAIM) if DAILY_CLAIM.is_file() else "未安装（run-as claim 不可用，raw 仍可用）"})
    try:
        idx = load_index()
        checks.append({"item": "账号索引", "ok": True, "detail": "%d 个账号" % len(idx["accounts"])})
        for x in idx["accounts"]:
            checks.append({"item": "快照 %s" % x["id"], "ok": snapshot_path(x["id"]).is_file(),
                           "detail": "已加密" if snapshot_path(x["id"]).is_file() else "缺失"})
    except VaultError as e:
        checks.append({"item": "账号索引", "ok": False, "detail": str(e)})
    emit({"ok": True, "checks": checks,
          "all_required_ok": all(c["ok"] for c in checks if c["item"] in
                                 ("Python", "保险库目录可写", "登录态文件", "保险库加密自检"))}, a.json,
         "\n".join("[%s] %s: %s" % ("OK  " if c["ok"] else "FAIL", c["item"], c["detail"])
                   for c in checks))


def cmd_dashboard(a) -> int:
    """启动本地可视化面板（常驻进程，浏览器里点操作；Ctrl+C 或面板按钮关闭）。"""
    if not DASHBOARD.is_file():
        raise VaultError("找不到 dashboard.py: %s" % DASHBOARD)
    argv = [sys.executable, str(DASHBOARD), "--port", str(a.port)]
    if a.no_browser:
        argv.append("--no-browser")
    if getattr(a, "no_auto_capture", False):
        argv.append("--no-auto-capture")
    return subprocess.call(argv)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wb_accounts.py", description="WorkBuddy 多账号管家（纯标准库优先）")
    p.add_argument("--json", action="store_true", dest="json", help="输出 JSON")
    p.add_argument("--version", action="version", version="wb-accounts " + VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--json", action="store_true", dest="json",
                        default=argparse.SUPPRESS, help="输出 JSON")

    sp = sub.add_parser("init", help="初始化保险库"); common(sp)
    sp.add_argument("--force", action="store_true"); sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("capture", help="采集当前客户端登录态为一个账号快照"); common(sp)
    sp.add_argument("--label", required=True); sp.add_argument("--id"); sp.add_argument("--note")
    sp.add_argument("--force", action="store_true", help="同 uid 已存在时改为更新快照")
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("capture-web",
                        help="把授权窗口登录的新号登记进保险库（网页会话通道，无快照）"); common(sp)
    sp.add_argument("--label"); sp.add_argument("--id"); sp.add_argument("--note")
    sp.set_defaults(func=cmd_capture_web)

    sp = sub.add_parser("refresh", help="用当前登录态刷新该账号快照（令牌轮换后）"); common(sp)
    sp.add_argument("key"); sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("current", help="显示客户端当前登录的是哪个账号"); common(sp)
    sp.set_defaults(func=cmd_current)

    sp = sub.add_parser("list", help="列出账号"); common(sp); sp.set_defaults(func=cmd_list)
    sp = sub.add_parser("brief", help="一屏概览（Agent / Hook 用）"); common(sp)
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("switch", help="切换客户端登录账号（需先完全退出 WorkBuddy）"); common(sp)
    sp.add_argument("key"); sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--allow-running", action="store_true", help="客户端仍运行时强制写入（不保证生效）")
    sp.set_defaults(func=cmd_switch)

    sp = sub.add_parser("rollback", help="回滚到上一次切换前的登录态"); common(sp)
    sp.add_argument("--allow-running", action="store_true"); sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("label", help="修改账号别名"); common(sp)
    sp.add_argument("key"); sp.add_argument("name"); sp.set_defaults(func=cmd_label)

    sp = sub.add_parser("remove", help="归档账号（不真删）"); common(sp)
    sp.add_argument("key"); sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("token", help="独立令牌通道：auto / import / status / forget / recover / atrest-import"); common(sp)
    sp.add_argument("token_action", choices=["auto", "import", "status", "forget",
                                             "recover", "atrest-import"])
    sp.add_argument("key", nargs="?", help="账号 id（auto / atrest-import 时可选）")
    sp.add_argument("--value"); sp.add_argument("--file"); sp.add_argument("--stdin", action="store_true")
    sp.set_defaults(func=cmd_token)

    sp = sub.add_parser("keepalive", help="用 refresh token 续期，长期免登录"); common(sp)
    sp.add_argument("key", nargs="?")
    sp.add_argument("--all", action="store_true"); sp.set_defaults(func=cmd_keepalive)

    sp = sub.add_parser("run-as", help="以指定账号身份调接口：status / raw / claim"); common(sp)
    sp.add_argument("key"); sp.add_argument("action", choices=["status", "raw", "claim"])
    # REMAINDER 统一吞掉后续参数，避免 --travel-auto / --data 之类被 argparse 误解析
    sp.add_argument("rest", nargs=argparse.REMAINDER,
                    help="raw: <METHOD> <PATH> [--data '<json>']；claim: 透传给 daily-claim")
    sp.set_defaults(func=cmd_run_as)

    sp = sub.add_parser("report", help="生成 HTML 状态面板"); common(sp)
    sp.add_argument("--out"); sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("export-state", help="导出不含凭据的状态快照"); common(sp)
    sp.add_argument("--out"); sp.set_defaults(func=cmd_export_state)

    sp = sub.add_parser("where", help="打印各数据路径"); common(sp); sp.set_defaults(func=cmd_where)
    sp = sub.add_parser("doctor", help="环境自检"); common(sp); sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("dashboard", help="启动本地可视化面板（127.0.0.1 + 一次性 token）")
    common(sp)
    sp.add_argument("--port", type=int, default=8765, help="端口，默认 8765；0 = 系统分配")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.add_argument("--no-auto-capture", action="store_true",
                    help="打开面板时不自动采集客户端当前登录的账号")
    sp.set_defaults(func=cmd_dashboard)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    # run-as 的 REMAINDER 会连 --json 一起吞掉，这里兜底还原，
    # 保证 `--json` 写在命令前或命令后效果一致，且不会透传进业务参数。
    rest = getattr(a, "rest", None)
    if rest and "--json" in rest:
        a.json = True
        a.rest = [x for x in rest if x != "--json"]
    try:
        rc = a.func(a)
        return int(rc) if isinstance(rc, int) else 0
    except VaultError as e:
        code = 2 if e.code in ("no_account", "already_exists", "client_running", "no_token") else 1
        payload = {"ok": False, "error": str(e), "code": e.code}
        if getattr(a, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("错误: %s" % e, file=sys.stderr)
        return code
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        if getattr(a, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("未预期错误: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
