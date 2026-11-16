# 安全说明与威胁模型

本项目的唯一目标是：**让你在自己这台机器上，不必反复扫码就能在多个自有账号间切换与查询。**
它不提供、也不应该被用来提供任何形式的账号共享或风控绕过。

## 1. 数据流向

```text
WorkBuddy 客户端（本机）
        │  ① 读取其登录态文件 / 加密快照
        ▼
wb_accounts.py ──DPAPI(当前用户)──▶  ~/.workbuddy/wb-accounts/
        │                                  snapshots/  tokens/  atrest.bin
        │  ② 用 refresh token 向官方接口续期
        ▼
   *.workbuddy / *.tencent.com  （官方服务端，HTTPS）
```

- **没有任何第三方服务器**参与，本项目不含遥测、不回传任何数据。
- 唯一的外部网络请求是你本来就会用的官方接口（续期、签到、查余额）。

## 2. 凭据保护

| 数据 | 保护方式 |
|---|---|
| 登录态快照、refresh token | Windows DPAPI，作用域 = 当前 Windows 用户 |
| `atRestSecretKey`（解快照用） | DPAPI 密封为 `atrest.bin`，仅在需要时解密到内存 |
| 面板访问 | `WB_DASHBOARD_TOKEN` / 首次自动生成的 `dashboard-token.txt`，+ `Host` 校验 + HttpOnly cookie |
| 日志与页面 | JWT、token、envelope、password 等字段正则脱敏；UID/UIN 打码 |

**关键性质：** 换机器、换 Windows 用户、重装系统后，DPAPI 解不开任何旧凭据。
这是刻意设计——即使整个数据目录泄露，攻击者拿到的也是密文。

## 3. 面本机攻击面

| 面 | 措施 |
|---|---|
| 面板 HTTP 服务 | 只 `bind 127.0.0.1`；校验 `Host` 头防 DNS rebinding；token 比对用 `hmac.compare_digest` |
| 面板页面 | 无外部资源（CSP `default-src 'self'`），无内联第三方脚本，不写 localStorage 存令牌 |
| 命令执行 | 面板只调用固定的 CLI 子命令，参数经过 `SAFE_ID` 白名单校验，不接受任意命令 |
| 进程操作 | 切换只结束/重启本机 WorkBuddy 进程，不注入、不改客户端文件 |
| 内存读取 | `recover_atrest.py` 只在**用户主动触发且客户端在运行**时读取该进程自身内存，取到即加密，不落明文 |

## 4. 已知限制与风险

1. **`recover_atrest` 依赖客户端内部结构**：属于逆向得到的实现，客户端版本升级后可能失效。
   失效时降级为「面板 → 打开授权窗口 → 手动登录一次」，不影响已有令牌的续期。
2. **面板 token 等同本机访问权**：拿到 `dashboard-token.txt` 且能在本机连接 8765 的人可以操作面板。
   请保持该文件仅当前用户可读。
3. **切换会中断当前 WorkBuddy 会话**：这是客户端机制决定的，不是本项目的缺陷。
4. **多号并存有服务端风控前提**：账号必须是你自己的；频繁切换可能触发官方风控，请自行评估。

## 5. 报告安全问题

请不要开公开 issue。通过仓库 Security 页的私密报告，或邮件联系维护者，说明：
复现步骤、影响范围、你的环境（Windows 版本 / Python 版本 / 客户端版本）。
我们会在确认后尽快修复并在 CHANGELOG 中致谢（如你愿意署名）。
