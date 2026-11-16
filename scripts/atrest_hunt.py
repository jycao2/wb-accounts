#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atrest_hunt.py —— 一次性「重启客户端并抓住 atRestSecretKey 窗口」的脱离执行器

流程（独立进程，不随客户端/面板一起死）：
  1. 记录 WorkBuddy.exe 路径
  2. 优雅关闭客户端（超时强杀）
  3. 立即重新启动客户端 —— 启动时会重新拉取/解析凭据保护 bootstrap，
     atRestSecretKey 会短暂出现在某个进程内存里
  4. 高频轮询扫描（每 2 秒一轮，最多 8 分钟），复用 recover_atrest 的
     只读扫描逻辑（ASCII + UTF-16LE 双口径、keyId 校验）
  5. 命中后经 stdin 喂给 `wb_accounts.py token atrest-import --stdin`
     （DPAPI 加密落盘，不留明文），写结果状态文件

结果写 state/atrest-hunt.json（pending/closing/relaunched/hunting/done/error）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_DIR = ROOT / "state"
HUNT_STATE = STATE_DIR / "atrest-hunt.json"
HUNT_LOG = STATE_DIR / "atrest-hunt.log"

IS_WIN = os.name == "nt"

sys.path.insert(0, str(HERE))
import recover_atrest as ra  # noqa: E402

DEFAULT_KEY_ID = ra.DEFAULT_KEY_ID
ENVELOPE_KEY_ID = "9127dea1b44020a7"


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(HUNT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def write_state(state: str, message: str, **extra) -> None:
    d = {"state": state, "message": message,
         "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d.update(extra)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HUNT_STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def client_pids() -> list[int]:
    return ra.list_client_pids()


def find_exe() -> str | None:
    """从运行中的进程反查 WorkBuddy.exe 路径。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process WorkBuddy | Select-Object -First 1).Path"],
            capture_output=True, text=True, timeout=15)
        p = (r.stdout or "").strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    for cand in ("D:\\workbuddy\\WorkBuddy.exe",):
        if os.path.isfile(cand):
            return cand
    return None


def close_client(grace: int = 15, hard_wait: int = 45) -> tuple[bool, str]:
    pids = client_pids()
    if not pids:
        return True, "客户端本来就没在运行"
    log("客户端 %d 个进程，开始优雅关闭…" % len(pids))
    try:
        subprocess.run(["taskkill", "/IM", "WorkBuddy.exe"],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not client_pids():
            return True, "客户端已正常退出"
        time.sleep(1)
    log("优雅关闭超时，强杀…")
    for pid in client_pids():
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    deadline = time.time() + hard_wait
    while time.time() < deadline:
        if not client_pids():
            return True, "客户端已强制退出"
        time.sleep(1)
    return False, "客户端进程未能退出，中止（不改动任何状态）"


def hunt(timeout_s: int = 480, interval: float = 2.0) -> str | None:
    """轮询扫描所有候选进程，命中返回密钥字符串。"""
    deadline = time.time() + timeout_s
    rounds = 0
    seen: set[int] = set()
    while time.time() < deadline:
        rounds += 1
        for pid in client_pids():
            if pid in seen:
                continue
            secret = ra.scan_pid(pid, ENVELOPE_KEY_ID, verbose=False)
            if secret:
                log("命中！PID %d 找到匹配 keyId=%s 的密钥（已校验）" % (pid, ENVELOPE_KEY_ID))
                return secret
        if rounds % 15 == 0:  # 每 30 秒报一次进度
            log("扫描中… 第 %d 轮，候选进程 %s" % (rounds, client_pids()))
        time.sleep(interval)
    return None


def import_secret(secret: str) -> tuple[bool, str]:
    vault = HERE / "wb_accounts.py"
    r = subprocess.run([sys.executable, str(vault), "token", "atrest-import", "--stdin"],
                       input=secret + "\n", capture_output=True, text=True, timeout=60)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out[:500]


def main() -> int:
    write_state("pending", "已排队：准备重启客户端并捕获密钥窗口…")
    log("=== atrest hunt 开始 ===")
    exe = find_exe()
    if not exe:
        write_state("error", "未找到 WorkBuddy.exe，无法执行")
        return 1
    log("exe = %s" % exe)

    # 留 8 秒让发起方把消息发出去（关客户端会中断对话）
    time.sleep(8)
    ok, note = close_client()
    if not ok:
        write_state("error", note)
        return 1
    log(note)

    write_state("relaunched", "客户端已关闭，正在重新启动…")
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200 if IS_WIN else 0)
    except Exception as e:
        write_state("error", "重启客户端失败: %s" % e)
        log("重启失败: %s" % e)
        return 1
    log("客户端已重新启动，等待进程出现…")
    time.sleep(3)

    write_state("hunting", "客户端已重启，正在高频扫描内存中的密钥窗口（最多 8 分钟）…")
    secret = hunt()
    if not secret:
        write_state("error",
                    "8 分钟窗口内未捕获到 atRestSecretKey（客户端启动后未在内存中暴露该密钥）。"
                    "可再试一次，或改用客户端 DevTools 手动提取。")
        log("未命中，退出")
        return 1

    write_state("importing", "已捕获密钥（keyId 校验通过），正在导入保险库…")
    good, out = import_secret(secret)
    if good:
        write_state("done", "✓ atRestSecretKey 已捕获并固化进保险库（DPAPI 加密）。"
                            "面板将自动恢复 4371 的令牌通道与余额显示。", key_id=ENVELOPE_KEY_ID)
        log("导入成功：%s" % out)
        return 0
    write_state("error", "密钥捕获成功但导入失败：%s" % out)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
