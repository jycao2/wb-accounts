#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板「切换到此号」的执行器：在 WorkBuddy 客户端之外完成整条切换链路。

为什么要独立进程：
  * 客户端退出时会把内存里的登录态写回文件，必须先退出、后写入，否则会被覆盖。
  * 关闭 WorkBuddy 会连带结束面板（dashboard.py）进程，切换动作必须活在这个
    脱离进程组的独立进程里，否则第一步就把自己杀了。

流程：记下客户端 exe 路径 → 优雅关闭（必要时强杀）→ 等进程消失 →
      `wb_accounts.py switch <id>`（WB_SKIP_CLIENT_CHECK=1）→ best-effort 重开客户端 → 写结果。

结果写在 state/hot-switch.json，面板下次打开会读取并展示。任何情况下都不写令牌内容。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
DETACHED = 0x00000008 | 0x00000200 if IS_WIN else 0
LOG_PATH: Path | None = None


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    """步骤日志：排查「切换没成功」时唯一可靠的时间线（不含任何令牌）。"""
    if LOG_PATH is None:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def write_state(path: Path, **kw) -> None:
    """原子写状态文件（面板据此展示进度/结果）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"at": now(), **kw}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def client_pids() -> list:
    try:
        if IS_WIN:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15)
            pids = []
            for line in (r.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "workbuddy.exe":
                    pids.append(parts[1])
            return pids
        r = subprocess.run(["pgrep", "-f", "WorkBuddy"], capture_output=True, text=True, timeout=10)
        return [x for x in (r.stdout or "").split() if x.strip()]
    except Exception:
        return []


def live_client_pids() -> list:
    """内核态判定 WorkBuddy 是否真的活着：ImageName 匹配且线程数 > 0。

    背景（2026-11-13 实测坑）：客户端被强杀后可能出现「线程/句柄已清零、
    但进程表条目仍在」的僵尸（终止收尾卡在内核），tasklist 仍会列出它，
    taskkill /F 对它返回拒绝访问——导致切换在「等待进程退出」阶段被死进程
    卡住而中止。线程数读的是内核数据，不受进程 ACL 影响：0 线程 = 已死。
    枚举失败时回退 tasklist（宁可保守，不误判活进程为死）。
    """
    if not IS_WIN:
        return client_pids()
    try:
        import ctypes

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                        ("Buffer", ctypes.c_void_p)]

        class SPI(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", ctypes.c_ulong),
                ("NumberOfThreads", ctypes.c_ulong),
                ("WorkingSetPrivateSize", ctypes.c_longlong),
                ("HardFaultCount", ctypes.c_ulong),
                ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
                ("CycleTime", ctypes.c_ulonglong),
                ("CreateTime", ctypes.c_longlong),
                ("UserTime", ctypes.c_longlong),
                ("KernelTime", ctypes.c_longlong),
                ("ImageName", UNICODE_STRING),
                ("BasePriority", ctypes.c_long),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p),
                ("HandleCount", ctypes.c_ulong),
                ("SessionId", ctypes.c_ulong),
            ]

        ntdll = ctypes.WinDLL("ntdll")
        for buf_len in (16 * 1024 * 1024, 64 * 1024 * 1024):
            buf = ctypes.create_string_buffer(buf_len)
            ret = ctypes.c_ulong(0)
            st = ntdll.NtQuerySystemInformation(5, buf, buf_len, ctypes.byref(ret))
            if st == 0:
                break
            if st & 0xFFFFFFFF != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH 之外的失败
                raise OSError("NtQuerySystemInformation st=0x%08x" % (st & 0xFFFFFFFF))
        else:
            raise OSError("句柄缓冲区不足")
        out = []
        offset = 0
        addr = ctypes.addressof(buf)
        while True:
            p = SPI.from_address(addr + offset)
            name = ""
            if p.ImageName.Buffer and p.ImageName.Length:
                name = (ctypes.c_wchar_p(p.ImageName.Buffer).value or "").lower()
            if name == "workbuddy.exe" and p.NumberOfThreads > 0:
                out.append(int(p.UniqueProcessId or 0))
            if not p.NextEntryOffset:
                break
            offset += p.NextEntryOffset
        return out
    except Exception as e:  # noqa: BLE001
        log("内核态进程枚举失败，回退 tasklist：%s: %s" % (type(e).__name__, e))
        return client_pids()


def client_exe_path() -> str | None:
    """在关闭前记下客户端路径，切完可以帮用户重新打开。"""
    if not IS_WIN:
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process WorkBuddy -ErrorAction SilentlyContinue | "
             "ForEach-Object { try { $_.Path } catch { } } | "
             "Where-Object { $_ -and $_.ToLower().EndsWith('.exe') } | "
             "Select-Object -First 1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        for line in (r.stdout or "").splitlines():
            p = line.strip()
            if p.lower().endswith(".exe") and os.path.isfile(p):
                return p
    except Exception:
        pass
    return None


def close_client(state: Path, acc_id: str, grace: int = 12, hard_wait: int = 45) -> tuple[bool, str]:
    """优雅关闭客户端；超时后强杀。返回 (是否已关闭, 说明)。

    进程死活的判定用 live_client_pids()（线程数>0），僵尸条目视为已退出。
    """
    pids = client_pids()
    if not live_client_pids():
        return True, "客户端本来就没在运行（或只剩僵尸条目）"
    log("探测到客户端进程 %d 个：%s" % (len(pids), ",".join(pids)))
    write_state(state, state="closing", id=acc_id,
                message="正在关闭 WorkBuddy（%d 个进程），请稍候…" % len(pids))
    # 1) 优雅关闭：taskkill 不带 /F 会向窗口发关闭请求，客户端有机会正常落盘/退订
    try:
        if IS_WIN:
            r = subprocess.run(["taskkill", "/IM", "WorkBuddy.exe"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=25)
            log("taskkill 优雅关闭 rc=%s out=%s" % (r.returncode, (r.stdout or "").strip()[:200]))
        else:
            subprocess.run(["pkill", "-TERM", "-f", "WorkBuddy"], capture_output=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        log("优雅关闭异常：%s: %s" % (type(e).__name__, e))
    deadline = time.time() + grace
    while time.time() < deadline:
        if not live_client_pids():
            log("客户端已正常退出（优雅关闭生效）")
            return True, "客户端已正常退出"
        time.sleep(1)
    # 2) 强杀兜底（此时若客户端还活着，说明它在等交互/最小化到托盘，长痛不如短痛）
    log("优雅关闭 %ds 未完成，转强制结束" % grace)
    write_state(state, state="closing", id=acc_id,
                message="优雅关闭未完成，正在强制结束客户端进程…")
    for pid in client_pids():
        try:
            if IS_WIN:
                r = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=15)
                log("强杀 pid=%s rc=%s %s" % (pid, r.returncode, (r.stdout or "").strip()[:120]))
            else:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=10)
        except Exception as e:  # noqa: BLE001
            log("强杀 pid=%s 异常：%s" % (pid, e))
    deadline = time.time() + hard_wait
    while time.time() < deadline:
        if not live_client_pids():
            log("客户端已强制退出（僵尸条目不阻塞切换）")
            return True, "客户端已强制退出"
        time.sleep(1)
    log("客户端 %ds 后仍未退出（活进程：%s），中止切换" % (hard_wait, live_client_pids()))
    return False, "客户端进程仍未退出（可能被系统或安全软件拦住），切换已中止"


def run_switch(vault: Path, acc_id: str) -> dict:
    env = dict(os.environ)
    env["WB_SKIP_CLIENT_CHECK"] = "1"   # 已确认进程消失，避免边角残留造成误判
    try:
        r = subprocess.run([sys.executable, str(vault), "switch", acc_id, "--json"],
                           env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, cwd=str(vault.parent))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "调用 switch 失败: %s: %s" % (type(e).__name__, e)}
    raw = (r.stdout or "").strip() or (r.stderr or "").strip()
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"ok": r.returncode == 0, "raw": raw[:800]}


def _vault_uids(vault: Path, target_id: str) -> dict:
    """读当前登录态与保险库索引，拿完整的 from/to uid（不打印、不落日志）。"""
    sys.path.insert(0, str(vault.parent))
    import wb_accounts as W  # noqa: PLC0415
    out = {"from_uid": "", "to_uid": ""}
    try:
        _raw, _cd, meta = W.read_login_state()
        out["from_uid"] = (meta or {}).get("uid") or ""
    except Exception as e:  # noqa: BLE001
        log("读当前登录态 uid 失败：%s: %s" % (type(e).__name__, e))
    try:
        idx = W.load_index()
        acc = next((x for x in (idx.get("accounts") or []) if x.get("id") == target_id), None)
        out["to_uid"] = (acc or {}).get("uid") or ""
    except Exception as e:  # noqa: BLE001
        log("读保险库索引失败：%s: %s" % (type(e).__name__, e))
    return out


def sync_tasks_copy(vault: Path, from_uid: str, to_uid: str, selection, progress=None) -> dict:
    """把当前号的任务数据「同步（复制）」一份给目标号。必须在客户端完全退出后调用。

    同步是复制不是转移：源账号的会话/分组一行不动，目标号新建副本（新会话 id、
    新正文文件）。selection=None 同步全部存活会话，否则只同步列表里的 id。
    幂等：sync-ledger.json 记录已复制项，重复执行/中断重跑自动跳过。
    automations（定时任务）刻意不复制：与运行环境绑定，复制过去跑不了。
    """
    try:
        sys.path.insert(0, str(vault.parent))
        import wb_accounts as W  # noqa: PLC0415
        import sync_tasks as S  # noqa: PLC0415
        return S.sync_copy(W.HOME, from_uid, to_uid, selection,
                           ledger_path=W.HOME / "sync-ledger.json", progress=progress)
    except Exception as e:  # noqa: BLE001
        log("任务同步失败：%s: %s" % (type(e).__name__, e))
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def verify_current(vault: Path, acc_id: str) -> tuple[bool, str]:
    """写完后核对客户端登录态是否真的换成了目标账号（只比脱敏 uid）。"""
    try:
        r = subprocess.run([sys.executable, str(vault), "brief", "--json"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=90)
        d = json.loads((r.stdout or "").strip())
    except Exception as e:  # noqa: BLE001
        return False, "写入完成，但无法校验（%s: %s）" % (type(e).__name__, e)
    cur = d.get("current_uid_masked") or ""
    acc = next((x for x in (d.get("accounts") or []) if x.get("id") == acc_id), {})
    want = acc.get("uid_masked") or ""
    log("校验登录态：当前=%s 目标=%s" % (cur or "未知", want or "未知"))
    if cur and want and cur == want:
        return True, "已校验：客户端登录态确实已切到目标账号"
    return False, "写入完成，但客户端登录态仍是 %s（目标 %s）" % (cur or "未知", want or "未知")


def relaunch(exe: str | None) -> str:
    if not exe or not os.path.isfile(exe):
        return ""
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=DETACHED)
        log("已重新打开客户端：%s" % exe)
        return "已自动重新打开 WorkBuddy"
    except Exception as e:  # noqa: BLE001
        log("重开客户端失败：%s" % e)
        return ""


def main() -> int:
    global LOG_PATH
    ap = argparse.ArgumentParser(description="退出客户端并切换 wb-accounts 登录态")
    ap.add_argument("--id", required=True)
    ap.add_argument("--vault", required=True, help="wb_accounts.py 路径")
    ap.add_argument("--state", required=True, help="结果状态文件路径")
    ap.add_argument("--no-relaunch", action="store_true")
    ap.add_argument("--sync-tasks", action="store_true",
                    help="切换时把当前登录号的任务数据同步（复制）一份给目标号（需用户在面板确认）")
    ap.add_argument("--sync-selection", default="",
                    help='选择文件 JSON：{"mode":"all"} 或 {"ids":[...]}；缺省=全部')
    ap.add_argument("--dry-run", action="store_true", help="只探测客户端与路径，不关闭不切换")
    a = ap.parse_args()

    state = Path(a.state)
    vault = Path(a.vault)
    LOG_PATH = state.parent / "hot-switch.log"
    log("=== 切换任务启动 pid=%s 目标=%s python=%s" % (os.getpid(), a.id, sys.executable))
    write_state(state, state="pending", id=a.id, message="已排队：准备关闭客户端并切换…")

    exe = client_exe_path()
    log("客户端 exe=%s" % (exe or "未找到"))
    if not exe and client_pids():
        log("警告：检测到客户端进程但读不到 exe 路径（可能存在权限异常的残留进程），"
            "切换完成后将无法自动重开，需用户手动启动 WorkBuddy")
    if a.dry_run:
        write_state(state, state="dry-run", id=a.id,
                    message="探测：客户端进程 %d 个，exe=%s" % (len(client_pids()), exe or "未找到"),
                    pids=client_pids(), exe=exe)
        return 0
    time.sleep(1.5)          # 让面板先把 HTTP 响应回给浏览器，页面能看到「正在切换」
    # 同步任务数据：必须在关客户端前读当前 uid（switch 会用目标快照覆盖登录态文件）
    sync_res: dict | None = None
    sel = None  # None = 全部
    if a.sync_tasks:
        if a.sync_selection:
            try:
                sd = json.loads(Path(a.sync_selection).read_text(encoding="utf-8"))
                sel = None if sd.get("mode") == "all" else \
                    [x for x in (sd.get("ids") or []) if isinstance(x, str)]
            except Exception as e:  # noqa: BLE001
                log("读取同步选择失败（按全部处理）：%s" % e)
        uids = _vault_uids(vault, a.id)
        log("待同步 uid：from=%s to=%s selection=%s" %
            (uids.get("from_uid")[:8] + "…" if uids.get("from_uid") else "缺失",
             uids.get("to_uid")[:8] + "…" if uids.get("to_uid") else "缺失",
             "全部" if sel is None else "%d 条" % len(sel)))
        if not uids.get("from_uid") or not uids.get("to_uid"):
            sync_res = {"ok": False, "error": "无法确定同步双方 uid（登录态或索引缺失）"}
    ok, note = close_client(state, a.id)
    if not ok:
        log("中止：%s" % note)
        write_state(state, state="error", id=a.id, message=note)
        return 1

    if a.sync_tasks and sync_res is None:
        def _prog(*parts):
            msg = " ".join(str(x) for x in parts)
            log(msg)
            try:
                write_state(state, state="switching", id=a.id,
                            message="正在同步任务数据：%s" % msg[:110])
            except Exception:  # noqa: BLE001
                pass
        write_state(state, state="switching", id=a.id,
                    message="客户端已退出，正在同步（复制）任务数据…")
        sync_res = sync_tasks_copy(vault, uids["from_uid"], uids["to_uid"], sel,
                                   progress=_prog)

    log("开始写入登录态：switch %s" % a.id)
    write_state(state, state="switching", id=a.id, message="客户端已退出，正在写入目标账号登录态…")
    res = run_switch(vault, a.id)
    if not res.get("ok"):
        msg = res.get("error") or (res.get("raw") or "写入失败")
        log("switch 失败：%s" % str(msg)[:300])
        write_state(state, state="error", id=a.id, message="切换失败：%s" % msg,
                    detail={k: res.get(k) for k in ("code", "error") if res.get(k)})
        return 1

    label = res.get("label") or a.id
    uid_masked = res.get("uid_masked") or ""
    log("switch 成功：%s %s" % (label, uid_masked))
    okv, vnote = verify_current(vault, a.id)
    reopened = "" if a.no_relaunch else relaunch(exe)
    if sync_res:
        n_s, n_sp = sync_res.get("sessions") or 0, sync_res.get("spaces") or 0
        if sync_res.get("ok"):
            sync_note = ("已同步任务数据（复制）：%d 条会话、%d 个分组到新账号；原账号任务保留。"
                         % (n_s, n_sp))
        elif n_s:
            nf = len(sync_res.get("failed") or [])
            sync_note = ("任务数据部分同步：%d 条成功、%d 条失败；切换本身未受影响，"
                         "重跑可续传。" % (n_s, nf))
        else:
            sync_note = ("任务数据同步失败（%s），切换本身未受影响。"
                         % (sync_res.get("error") or "未知原因"))
    else:
        sync_note = ""
    write_state(state, state="done" if okv else "error", id=a.id, label=label,
                uid_masked=uid_masked, verified=okv,
                message="切换完成：%s（%s）。%s。%s%s" % (label, a.id, note, vnote, sync_note),
                relaunched=bool(reopened), relaunch_note=reopened,
                sync=sync_res,
                backup=res.get("backup"), next_step=res.get("next_step") or "")
    log("=== 任务结束 verified=%s" % okv)
    return 0 if okv else 1


if __name__ == "__main__":
    raise SystemExit(main())
