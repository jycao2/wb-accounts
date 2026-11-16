#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atrest_cdp.py —— 带 DevTools 调试端口重启客户端，从主进程堆快照提取 atRestSecretKey

流程（脱离进程，客户端关闭不会连带杀死）：
  1. 优雅关闭客户端
  2. 带参数重启：--inspect=9229（主进程 Node 调试器）+ --remote-debugging-port=9222（渲染层）
  3. 连 9229 的 CDP，对主进程做 HeapProfiler.takeHeapSnapshot，在快照文本里
     搜索 44 字符规范 base64 候选并按 keyId 校验
  4. 未命中则对 9222 的各渲染层页面逐一快照搜索（上限 6 个）
  5. 命中 → `wb_accounts.py token atrest-import --stdin`（DPAPI 加密落盘）
  6. 若调试端口起不来 → 回落为「普通重启 + 内存轮询」10 分钟

结果写 state/atrest-cdp.json，过程写 state/atrest-cdp.log。全程不落密钥明文。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_DIR = ROOT / "state"
CDP_STATE = STATE_DIR / "atrest-cdp.json"
CDP_LOG = STATE_DIR / "atrest-cdp.log"
IS_WIN = os.name == "nt"
ENVELOPE_KEY_ID = "9127dea1b44020a7"
EXE = "D:\\workbuddy\\WorkBuddy.exe"
MAIN_PORT = 9229
RENDER_PORT = 9222

sys.path.insert(0, str(HERE))
import recover_atrest as ra  # noqa: E402

B64_RE = re.compile(rb"[A-Za-z0-9+/]{43}=")


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(CDP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_state(state: str, message: str, **extra) -> None:
    d = {"state": state, "message": message,
         "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d.update(extra)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CDP_STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except Exception:
        pass


def client_pids() -> list[int]:
    return ra.list_client_pids()


def close_client() -> tuple[bool, str]:
    if not client_pids():
        return True, "客户端未在运行"
    log("优雅关闭客户端…")
    try:
        subprocess.run(["taskkill", "/IM", "WorkBuddy.exe"],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    for _ in range(15):
        if not client_pids():
            return True, "客户端已退出"
        time.sleep(1)
    log("超时，强杀…")
    for pid in client_pids():
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    for _ in range(45):
        if not client_pids():
            return True, "客户端已强制退出"
        time.sleep(1)
    return False, "客户端未能退出"


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（纯标准库，够 CDP 用）
# ---------------------------------------------------------------------------
class WS:
    def __init__(self, url: str):
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=120)
        self.sock.settimeout(300)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("握手失败")
            buf += d
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("非 101 升级响应")

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            d = self.sock.recv(min(1 << 16, n - len(self.buf)))
            if not d:
                raise ConnectionError("连接关闭")
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_msg(self) -> tuple[int, bytes]:
        opcode0 = None
        data = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            masked = b2 & 0x80
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(ln) if ln else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping → pong
                self._send_raw(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0:
                data += payload
            else:
                opcode0 = opcode
                data = payload
            if fin:
                return opcode0, data

    def _send_raw(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        self._send_raw(0x1, text.encode())

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def http_json(url: str, timeout: int = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_port_json(port: int, timeout_s: int = 60):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            return http_json("http://127.0.0.1:%d/json/list" % port, timeout=3)
        except Exception:
            time.sleep(1)
    return None


def cdp_call(ws: WS, msg_id: int, method: str, params: dict | None = None,
             collect_method: str | None = None, done_method: str | None = None,
             timeout_s: int = 600) -> tuple[dict | None, list | None]:
    """发送 CDP 请求；若给了 collect/done，则收集 chunk 事件直到 done。"""
    ws.send_text(json.dumps({"id": msg_id, "method": method,
                             "params": params or {}}))
    deadline = time.time() + timeout_s
    result = None
    chunks: list[str] = []
    while time.time() < deadline:
        op, data = ws.recv_msg()
        if op != 1:
            continue
        try:
            m = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if m.get("id") == msg_id:
            result = m.get("result")
            if collect_method is None:
                return result, None
            if m.get("error"):
                return None, None
        if collect_method and m.get("method") == collect_method:
            chunks.append(m["params"].get("chunk", ""))
        if done_method and m.get("method") == done_method:
            return result, chunks
    return result, (chunks or None)


def scan_text_for_key(text: str) -> tuple[str | None, int, bool]:
    """在文本里搜 base64 候选并按 keyId 校验。返回 (密钥, 候选数, 是否出现 atRestSecretKey 字样)"""
    n_cand = 0
    has_marker = "atRestSecretKey" in text
    raw = text.encode("utf-8", errors="ignore")
    for m in B64_RE.finditer(raw):
        n_cand += 1
        cand = m.group(0).decode("ascii")
        try:
            _k, kid = ra.derive_kid(cand)
        except Exception:
            continue
        if kid == ENVELOPE_KEY_ID:
            return cand, n_cand, has_marker
    return None, n_cand, has_marker


def snapshot_target(ws: WS, label: str) -> tuple[str | None, int, bool]:
    log("对 %s 做堆快照（可能需要 1~2 分钟）…" % label)
    _res, chunks = cdp_call(ws, 2, "HeapProfiler.takeHeapSnapshot",
                            collect_method="HeapProfiler.addHeapSnapshotChunk",
                            done_method="HeapProfiler.heapSnapshotComplete",
                            timeout_s=900)
    if chunks is None:
        log("%s 堆快照失败" % label)
        return None, 0, False
    text = "".join(chunks)
    log("%s 快照大小 %.1f MB" % (label, len(text) / 1048576))
    key, n, marker = scan_text_for_key(text)
    log("%s 候选 %d 条，atRestSecretKey 字样=%s，命中=%s"
        % (label, n, marker, bool(key)))
    del text, chunks
    return key, n, marker


def import_secret(secret: str) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(HERE / "wb_accounts.py"),
                        "token", "atrest-import", "--stdin"],
                       input=secret + "\n", capture_output=True, text=True, timeout=60)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[:500]


def fallback_polling(minutes: int = 10) -> str | None:
    log("回落：普通重启 + 内存轮询 %d 分钟" % minutes)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        for pid in client_pids():
            secret = ra.scan_pid(pid, ENVELOPE_KEY_ID)
            if secret:
                return secret
        time.sleep(2)
    return None


def main() -> int:
    write_state("pending", "准备带调试端口重启客户端…")
    log("=== atrest cdp hunt 开始 ===")
    time.sleep(5)
    ok, note = close_client()
    if not ok:
        write_state("error", note)
        return 1
    log(note)
    write_state("relaunched", "正在带调试端口重启客户端…")
    args = [EXE, "--inspect=%d" % MAIN_PORT,
            "--remote-debugging-port=%d" % RENDER_PORT]
    subprocess.Popen(args, cwd=os.path.dirname(EXE), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200 if IS_WIN else 0)
    log("已启动（带 --inspect=%d）" % MAIN_PORT)

    targets = wait_port_json(MAIN_PORT, 60)
    key = None
    if targets:
        t = next((x for x in targets if x.get("webSocketDebuggerUrl")), targets[0])
        url = t.get("webSocketDebuggerUrl")
        log("主进程调试目标：%s" % (t.get("title") or t.get("id") or "?"))
        try:
            ws = WS(url)
            cdp_call(ws, 1, "HeapProfiler.enable")
            key, n, marker = snapshot_target(ws, "主进程")
            ws.close()
        except Exception as e:
            log("主进程快照异常: %s: %s" % (type(e).__name__, e))
    else:
        log("--inspect 端口 %d 未就绪（可能被生产版禁用）" % MAIN_PORT)

    if not key:
        rt = wait_port_json(RENDER_PORT, 30)
        if rt:
            pages = [x for x in rt if x.get("webSocketDebuggerUrl")][:6]
            log("渲染层目标 %d 个" % len(pages))
            for p in pages:
                try:
                    ws = WS(p["webSocketDebuggerUrl"])
                    cdp_call(ws, 1, "HeapProfiler.enable")
                    key, n, marker = snapshot_target(ws, "渲染层:" + (p.get("title") or "?")[:40])
                    ws.close()
                    if key:
                        break
                except Exception as e:
                    log("渲染层快照异常: %s" % e)

    if key:
        write_state("importing", "已从堆快照捕获密钥，导入保险库…")
        good, out = import_secret(key)
        if good:
            write_state("done", "✓ 密钥已从 DevTools 堆快照提取并固化（DPAPI 加密）。"
                                "面板将自动恢复 4371 的令牌通道与余额显示。",
                        key_id=ENVELOPE_KEY_ID)
            log("导入成功")
            return 0
        write_state("error", "捕获成功但导入失败：%s" % out)
        return 1

    log("堆快照未命中，尝试内存轮询回落…")
    write_state("hunting", "快照未命中，回落为内存轮询（10 分钟）…")
    key = fallback_polling(10)
    if key:
        good, out = import_secret(key)
        if good:
            write_state("done", "✓ 密钥已捕获并固化（轮询命中）。", key_id=ENVELOPE_KEY_ID)
            return 0
        write_state("error", "轮询命中但导入失败：%s" % out)
        return 1
    write_state("error", "DevTools 堆快照与内存轮询均未命中。密钥可能只在特定动作瞬间存在，"
                         "建议在客户端里触发一次登录/刷新后再试，或确认客户端版本行为。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
