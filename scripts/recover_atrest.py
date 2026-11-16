#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recover_atrest.py —— 一次性引导脚本（显式调用、可选、仅本机）

用途
----
从「正在运行的 WorkBuddy / Quark 客户端」进程内存中，**只读**提取 atRestSecretKey
（用来只读解密客户端登录态信封），并加密落盘到保险库的 atrest.bin，使之后
`wb_accounts.py token recover <id>` 能自动从客户端登录态恢复 refresh token。

与 wb_accounts 的关系
-------------------
这是独立的引导工具，**不是** wb_accounts CLI 的行为。wb_accounts / wb_crypto
自身绝不扫描进程内存、绝不读其它应用文件，只读保险库内已固化的 atrest.bin。
密钥只接受「用户显式导入」（环境变量 / 密钥文件 / 本脚本引导）。

打开可视化面板时，若 atrest.bin 缺失且存在未配通道的账号，dashboard.py 会在
**后台线程**调用本脚本做一次自愈（一天最多一次、失败静默、不阻塞面板；
--no-auto-token 可关闭）。取到后立刻 DPAPI 加密落盘，之后面板的 `token auto`
就完全只读盘，不再需要任何内存扫描。本脚本把「从客户端内存取一次密钥」做成
可审计、可复用的一次性引导，而不是把无差别内存扫描塞进常驻流程。

安全边界（刻意）
--------------
  * 只读取「当前用户自己的客户端进程」内存，搜索 44 字符规范 base64 候选，并用
    目标信封 keyId 校验，绝不错拿、绝不打印密钥明文（只回显校验过的 keyId）。
  * 提取到的密钥只经 stdin 喂给 `token atrest-import`，立即 DPAPI 加密落盘；
    不在磁盘留明文、不写日志、不保留中间文件。
  * 用完即弃：脚本本身不持久化任何东西。

用法
----
  python recover_atrest.py                 # 自动定位客户端进程并提取
  python recover_atrest.py --pid 1234      # 指定 PID
  python recover_atrest.py --dry-run       # 只校验能否找到，不落盘
  python recover_atrest.py --key-id 9127dea1b44020a7   # 指定要匹配的信封 keyId
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys

# 默认要匹配的信封 keyId（来自本机快照信封；换机器/换账号时可能不同，用 --key-id 覆盖）
DEFAULT_KEY_ID = "9127dea1b44020a7"

# 候选客户端进程名（不区分大小写）；命中其一即扫描其内存
CANDIDATE_NAMES = ("workbuddy.exe", "quark.exe", "codebuddy.exe", "buddy.exe")

PROCESS_VM_READ = 0x10
PROCESS_QUERY_INFORMATION = 0x400
MEM_COMMIT = 0x1000
# PAGE_* 低字节才是保护属性；高位可能是 PAGE_GUARD/NOCACHE 等标志，必须按位取
READABLE = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)  # PAGE_READONLY/READWRITE/WRITECOPY/EXECUTE_READ/...
PAGE_GUARD = 0x100
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260

# 32 字节的规范 base64 一定是「43 个 base64 字符 + 一个 =」。
# 两种内存形态都要扫：ASCII（C/Rust 侧）与 UTF-16LE（JS/.NET 字符串，每字符后跟 \x00）。
# 之前只扫 ASCII 会漏掉 Electron/托管堆里的密钥。
B64_RE = re.compile(rb"[A-Za-z0-9+/]{43}=")
B64_RE16 = re.compile(rb"(?:[A-Za-z0-9+/]\x00){43}=\x00")


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    """Win32 PROCESSENTRY32W。用 Structure 让 ctypes 自动处理 x64 对齐，
    不要手算 szExeFile 偏移（x86 是 36，x64 是 44，手算必然错）。"""
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),  # ULONG_PTR：指针宽度
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


# 64 位 Windows 必须显式声明 restype/argtypes，否则 HANDLE 被截成 32 位 c_int
_k32 = ctypes.windll.kernel32
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
# 必须显式用 W 版：无后缀的 Process32First 在本机解析到 ANSI 版，
# 用 PROCESSENTRY32W 去读会得到乱码进程名，枚举结果恒为空。
_k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = ctypes.c_int
_k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = ctypes.c_int
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = ctypes.c_int
_k32.OpenProcess.argtypes = [wt.DWORD, ctypes.c_int, wt.DWORD]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype = ctypes.c_int


def list_client_pids() -> list[int]:
    """枚举进程，返回候选客户端名的 PID 列表（x64 安全的 Structure 版）。"""
    pids: list[int] = []
    h_snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not h_snap or h_snap == ctypes.c_void_p(-1).value:
        return pids
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        okay = _k32.Process32FirstW(ctypes.c_void_p(h_snap), ctypes.byref(entry))
        while okay:
            try:
                name = (entry.szExeFile or "").lower()
            except Exception:
                name = ""
            if name in CANDIDATE_NAMES:
                pids.append(int(entry.th32ProcessID))
            okay = _k32.Process32NextW(ctypes.c_void_p(h_snap), ctypes.byref(entry))
    finally:
        _k32.CloseHandle(ctypes.c_void_p(h_snap))
    return pids


def derive_kid(secret: str):
    """返回 (key, derived_key_id) 或抛错。与 wb_crypto.derive_at_rest_key 同义。

    注意：必须直接用 hashlib.sha256，不能经由 hashlib_sha256()（它返回 digest
    bytes，再取 .hexdigest() 会抛 AttributeError——此前所有扫描因此从未真正
    校验过任何候选，全部静默判为未命中）。
    """
    import hashlib
    if len(secret) != 44:
        raise ValueError("长度不是 44")
    raw = base64.b64decode(secret)
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != secret:
        raise ValueError("不是规范 32 字节 base64")
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    return key, hashlib.sha256(key).hexdigest()[:16]


def hashlib_sha256(b):
    import hashlib
    return hashlib.sha256(b).digest()


def scan_pid(pid: int, target_kid: str, verbose: bool = False) -> str | None:
    """扫描单个进程的可读内存，返回匹配 target_kid 的 atRestSecretKey（或 None）。"""
    h = _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        if verbose:
            print("[scan] PID %d：OpenProcess 失败（可能是权限或进程已退出）" % pid)
        return None
    try:
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        chunk = ctypes.create_string_buffer(1024 * 1024)
        found: str | None = None
        n_regions = 0
        n_bytes = 0
        n_cand = 0
        while _k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), ctypes.sizeof(mbi)) == ctypes.sizeof(mbi):
            prot = mbi.Protect & 0xFF  # 去掉 PAGE_GUARD / NOCACHE 等高位标志
            if (mbi.State == MEM_COMMIT and prot in READABLE
                    and not (mbi.Protect & PAGE_GUARD)):
                n_regions += 1
                off = 0
                rs = mbi.RegionSize
                carry = b""  # 保留上一块尾部，避免候选串跨块被截断
                while off < rs:
                    to_read = min(rs - off, len(chunk))
                    if to_read <= 0:
                        break
                    # 必须是 c_size_t（8 字节），用 DWORD(4 字节) 会被 API 写越界
                    read = ctypes.c_size_t(0)
                    ok = _k32.ReadProcessMemory(
                        ctypes.c_void_p(h), ctypes.c_void_p(addr + off),
                        chunk, to_read, ctypes.byref(read))
                    got = int(read.value or 0)
                    if not ok or got == 0:
                        off += to_read  # 该块不可读就跳过，继续后面的块
                        continue
                    n_bytes += got
                    off += got
                    data = carry + chunk.raw[:got]
                    carry = data[-43:]
                    for m in B64_RE.finditer(data):
                        n_cand += 1
                        cand = m.group(0).decode("ascii")
                        try:
                            _key, kid = derive_kid(cand)
                        except Exception:
                            continue
                        if kid == target_kid:
                            found = cand
                            break
                    if not found:
                        for m in B64_RE16.finditer(data):
                            n_cand += 1
                            try:
                                cand = m.group(0).decode("utf-16-le")
                            except Exception:
                                continue
                            try:
                                _key, kid = derive_kid(cand)
                            except Exception:
                                continue
                            if kid == target_kid:
                                found = cand
                                break
                    if found:
                        break
                if found:
                    break
            # 必须用 region 的 BaseAddress 推进：查询地址可能落在 region 中间，
            # 用 addr + RegionSize 会跳过下一个 region 的开头（之前漏扫的根因）。
            step = mbi.RegionSize or 0x1000
            base = int(mbi.BaseAddress or 0)
            addr = (base + step) & 0xFFFFFFFFFFFFFFFF
            if addr >= 0x7FFFFFFFFFFF or step == 0:
                break
        if verbose and not found:
            print("[scan] PID %d：可读区 %d 个 / %.1f MB / base64 候选 %d 条，未命中 keyId=%s"
                  % (pid, n_regions, n_bytes / 1048576, n_cand, target_kid))
        return found
    except Exception as e:
        if verbose:
            print("[scan] PID %d：异常 %s: %s" % (pid, type(e).__name__, e))
        return None
    finally:
        _k32.CloseHandle(ctypes.c_void_p(h))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一次性从客户端内存提取 atRestSecretKey（可选）")
    ap.add_argument("--pid", type=int, help="指定客户端 PID；不填则自动枚举候选进程名")
    ap.add_argument("--key-id", default=DEFAULT_KEY_ID, help="要匹配的信封 keyId（默认 %s）" % DEFAULT_KEY_ID)
    ap.add_argument("--dry-run", action="store_true", help="只校验能否找到，不落盘")
    ap.add_argument("--verbose", action="store_true", help="打印只读扫描统计（不含任何密钥明文）")
    a = ap.parse_args(argv)

    if sys.platform != "win32":
        print("本工具仅支持 Windows（需要从客户端进程内存读取 atRestSecretKey）", file=sys.stderr)
        return 2

    pids = [a.pid] if a.pid else list_client_pids()
    if not pids:
        print("未找到候选客户端进程（%s）。请先打开 WorkBuddy/Quark 客户端并保持登录，再运行本脚本。"
              % "/".join(CANDIDATE_NAMES), file=sys.stderr)
        return 2

    if a.verbose:
        print("[scan] 候选客户端进程：%s" % ", ".join(str(p) for p in pids))
    target = a.key_id
    for pid in pids:
        secret = scan_pid(pid, target, verbose=a.verbose)
        if secret:
            print("在 PID %d 找到匹配 keyId=%s 的 atRestSecretKey（已校验，不打印明文）" % (pid, target))
            if a.dry_run:
                print("dry-run：未落盘。去掉 --dry-run 重新运行即可写入保险库。")
                return 0
            # 经 stdin 喂给 token atrest-import，直接 DPAPI 加密落盘，不留明文
            here = __file__
            import os
            vault = os.path.join(os.path.dirname(os.path.abspath(here)), "wb_accounts.py")
            py = sys.executable
            r = subprocess.run([py, vault, "token", "atrest-import", "--stdin"],
                               input=secret + "\n", capture_output=True, text=True)
            out = (r.stdout or "").strip() or (r.stderr or "").strip()
            if r.returncode == 0:
                print("已写入保险库 atrest.bin（DPAPI 加密）。之后 `token recover <id>` 可自动恢复。")
                return 0
            print("atrest-import 失败：%s" % out, file=sys.stderr)
            return 1
    print("在所扫描的进程中未找到匹配 keyId=%s 的 atRestSecretKey。" % target, file=sys.stderr)
    print("可能原因：客户端还没真正登录（内存里没有密钥），或本机信封 keyId 不同（用 --key-id 指定）。",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
