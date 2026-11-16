#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-accounts 加密层（纯标准库 + 可选 AES-GCM 后端）

两类完全不同的加密，别混淆：

1) 保险库自身存的东西（登录态快照、导入的令牌）
   -> Windows DPAPI（绑定当前系统用户）/ 非 Windows AES-256-GCM + PBKDF2 主密码
   -> 前缀 AV1 / AV2，由本模块 seal()/unseal() 负责

2) WorkBuddy 客户端登录态里的 $wbEncrypted:1 字段信封
   -> AES-256-GCM，key = SHA256(atRestSecretKey 字符串)，keyId = SHA256(key)[:16]
   -> 只在「用户显式提供 atRestSecretKey」时做**只读解密**，绝不回写客户端信封
   -> decrypt_login_field() / load_at_rest_key()

安全：本模块不打印、不返回、不落盘任何令牌明文给调用方之外的地方；
      不扫描进程内存、不扫描其它应用文件。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
from ctypes import wintypes

ENTROPY = b"workbuddy.wb-accounts.v1"
_AAD_DOMAIN = b"WB-AAD\x00"


# ---------------------------------------------------------------------------
# 保险库自身加密：Windows DPAPI / 非 Windows AES-GCM
# ---------------------------------------------------------------------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    def blob(buf: bytes):
        b = ctypes.create_string_buffer(buf, len(buf))
        return _BLOB(len(buf), ctypes.cast(b, ctypes.POINTER(ctypes.c_char))), b

    src, _k1 = blob(data)
    ent, _k2 = blob(ENTROPY)
    out = _BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise RuntimeError("DPAPI 调用失败（GetLastError=%s）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _aes_backend():
    """返回 (backend_name, encrypt_fn, decrypt_fn) 或 None。

    系统 Python 常没有 AES-GCM 库；WorkBuddy 自带 decrypt 环境里有 PyCryptodome，
    这里温和地把它加入搜索路径，让「可选的信封只读解密」开箱可用。
    """
    import sys
    from pathlib import Path
    extra = Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "decrypt" / "Lib" / "site-packages"
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.append(str(extra))
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        def enc(key, nonce, plain, aad):
            return AESGCM(key).encrypt(nonce, plain, aad)

        def dec(key, nonce, ct, aad):
            return AESGCM(key).decrypt(nonce, ct, aad)

        return "cryptography", enc, dec
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # type: ignore

        def enc(key, nonce, plain, aad):
            c = AES.new(key, AES.MODE_GCM, nonce=nonce)
            c.update(aad)
            ct, tag = c.encrypt_and_digest(plain)
            return ct + tag

        def dec(key, nonce, ct, aad):
            c = AES.new(key, AES.MODE_GCM, nonce=nonce)
            c.update(aad)
            return c.decrypt_and_verify(ct[:-16], ct[-16:])

        return "PyCryptodome", enc, dec
    except ImportError:
        return None


def crypto_backend() -> str:
    b = _aes_backend()
    return b[0] if b else "none"


def seal(plain: bytes) -> bytes:
    if platform.system() == "Windows":
        return b"AV1" + _dpapi(plain, True)
    b = _aes_backend()
    if not b:
        raise RuntimeError("非 Windows 平台需要 cryptography 或 pycryptodome")
    pwd = os.environ.get("WB_ACCOUNTS_PASSWORD")
    if not pwd:
        raise RuntimeError("非 Windows 平台请设置环境变量 WB_ACCOUNTS_PASSWORD 作为保险库主密码")
    import secrets
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, 200_000, dklen=32)
    return b"AV2" + salt + nonce + b[1](key, nonce, plain, b"wb-accounts")


def unseal(blob: bytes) -> bytes:
    if blob.startswith(b"AV1"):
        if platform.system() != "Windows":
            raise RuntimeError("AV1 密文由 Windows DPAPI 加密，只能在原 Windows 用户下解开")
        return _dpapi(blob[3:], False)
    if blob.startswith(b"AV2"):
        b = _aes_backend()
        if not b:
            raise RuntimeError("解密需要 cryptography 或 pycryptodome")
        pwd = os.environ.get("WB_ACCOUNTS_PASSWORD")
        if not pwd:
            raise RuntimeError("请设置环境变量 WB_ACCOUNTS_PASSWORD")
        salt, nonce, ct = blob[3:19], blob[19:31], blob[31:]
        key = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, 200_000, dklen=32)
        return b[2](key, nonce, ct, b"wb-accounts")
    raise RuntimeError("未知的保险库密文格式")


# ---------------------------------------------------------------------------
# WorkBuddy 客户端登录态信封（只读解密；用户显式提供密钥才可用）
# ---------------------------------------------------------------------------
def _b64d(value: str) -> bytes:
    return base64.b64decode(value)


def _u32(v: int) -> bytes:
    return v.to_bytes(4, "big")


def _lp(s: str) -> bytes:
    b = s.encode("utf-8")
    return _u32(len(b)) + b


def build_field_aad(key_id: str) -> bytes:
    return b"".join([
        _AAD_DOMAIN,
        bytes([1]),
        _lp("WBEV1"),
        _lp("sym-v1"),
        _u32(1),
        _lp(key_id),
        bytes([2]),   # framing = field
        bytes([0]),   # sequence 缺失
        bytes([0]),   # final 缺失
    ])


def derive_at_rest_key(secret: str):
    if not isinstance(secret, str) or len(secret) != 44:
        raise ValueError("atRestSecretKey 必须是 44 字符规范 base64（32 字节）")
    raw = _b64d(secret)
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != secret:
        raise ValueError("atRestSecretKey 不是规范的 32 字节 base64")
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    return key, hashlib.sha256(key).hexdigest()[:16]


def envelope_key_id(value) -> str | None:
    """只读信封的 keyId（不是密钥本身，可安全显示）。"""
    if not isinstance(value, dict) or value.get("$wbEncrypted") != 1:
        return None
    try:
        env = json.loads(_b64d(value["envelope"]).decode("utf-8"))
        return env.get("keyId")
    except Exception:
        return None


def is_envelope(value) -> bool:
    return isinstance(value, dict) and value.get("$wbEncrypted") == 1 and isinstance(value.get("envelope"), str)


def load_at_rest_key() -> str | None:
    """密钥仅来自用户显式提供的环境变量 / 密钥文件。绝不扫描进程内存或其它文件。"""
    k = os.environ.get("WORKBUDDY_ATREST_KEY")
    if k:
        return k.strip()
    kf = os.environ.get("WORKBUDDY_ATREST_KEY_FILE")
    if kf and os.path.isfile(kf):
        try:
            with open(kf, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return None
    return None


def decrypt_login_field(value, secret: str | None = None) -> str:
    """把登录态字段（明文 str 或信封 dict）解析为明文字符串。"""
    if isinstance(value, str):
        return value
    if not is_envelope(value):
        raise ValueError("无法解析的登录态字段类型")
    b = _aes_backend()
    if not b:
        raise RuntimeError("解密客户端信封需要 cryptography 或 pycryptodome；"
                           "也可改用 `token import` 导入 refresh token，完全不需要解密")
    sk = secret or load_at_rest_key()
    if not sk:
        raise RuntimeError("客户端登录态已信封加密，但未提供 WORKBUDDY_ATREST_KEY；"
                           "更简单的做法是用 `token import` 导入 refresh token")
    env = json.loads(_b64d(value["envelope"]).decode("utf-8"))
    if env.get("suite") != 1:
        raise ValueError("不支持的 envelope suite=%s" % env.get("suite"))
    key, derived_kid = derive_at_rest_key(sk)
    if derived_kid != env.get("keyId"):
        raise ValueError("密钥与信封 keyId 不匹配（derived=%s envelope=%s）"
                         % (derived_kid, env.get("keyId")))
    nonce = _b64d(env["nonce"])
    ct = _b64d(env["ciphertext"]) + _b64d(env["authTag"])
    return b[2](key, nonce, ct, build_field_aad(env["keyId"])).decode("utf-8")


def mask(value: str, keep_head: int = 6, keep_tail: int = 4) -> str:
    """只用于非敏感标识（uid / uin）的展示脱敏。"""
    if not isinstance(value, str) or len(value) <= keep_head + keep_tail:
        return "***"
    return value[:keep_head] + "…" + value[-keep_tail:]
