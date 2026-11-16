# 客户端协议、现场事实与限制

本文基于 2026-11-12 对本机 WorkBuddy 5.9.3 / Windows 的**只读勘查**，不是猜测；
2026-11-13 追加多账号凭据隔离、两个积分池与 `atRestSecretKey` 自愈的实测修订（第 4 / 7 / 8 / 9 节）。

## 1. 登录态文件在哪

优先级（插件按此顺序探测，`WB_LOGIN_STATE` / `WORKBUDDY_DESKTOP_INFO` 可覆盖）：

| 平台 | 路径 |
|---|---|
| Windows 主 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| Windows 次 | `%APPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| 兜底 | `~/.workbuddy/auth/workbuddy-desktop.info` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/...` |
| Linux | `$XDG_DATA_HOME/CodeBuddyExtension/Data/Public/auth/...` |

本机实测：`%LOCALAPPDATA%` 那份，9,102 B，客户端运行时持续更新。

## 2. 文件结构（值脱敏）

```
auth
  accessToken      {$wbEncrypted:1, envelope: base64}   ← AES-GCM 信封
  refreshToken     {$wbEncrypted:1, envelope: base64}   ← AES-GCM 信封
  tokenType        "Bearer"
  domain           "www.workbuddy.cn"
  expiresIn        2419200        (28 天)
  refreshExpiresIn 2591999        (30 天)
  expiresAt / refreshExpiresAt / lastRefreshTime   毫秒时间戳（明文）
  sessionState     UUID（明文）
account
  uid              36 字符 UUID（明文，账号唯一标识）
  uin              12 位数字（明文）
  type             "personal"
  nickname / phoneNumber          {$wbEncrypted:1, envelope}  ← 加密
  lastLogin / isCreator / ...     明文布尔
accounts: list[1]       ← 只有账号元数据，不含令牌
allAccounts: list[1]    ← 同上
```

**关键结论**：`accounts` / `allAccounts` 不含任何令牌，且实测只有当前账号 1 项。
客户端不会替你并存多个账号的令牌；切换账号本质上就是替换顶层 `auth`。

## 3. 信封格式

`envelope` 是规范 base64，解码后为：

```json
{"suite":1,"keyId":"<16位hex>","nonce":"<b64>","authTag":"<b64>","ciphertext":"<b64>"}
```

- `key = SHA256(atRestSecretKey 字符串 UTF-8)`，32 字节
- `keyId = SHA256(key).hexdigest()[:16]`
- AES-256-GCM，AAD = `WB-AAD\0` + `WBEV1` + `sym-v1` + suite + keyId + framing=field

**keyId 与账号无关**，是客户端级别的密钥标识。因此**整文件快照可以直接跨账号写回**：
信封本身不动，客户端用同一把密钥就能解开。这是通道 A 不需要任何密钥的原因。

插件只在用户显式提供 `WORKBUDDY_ATREST_KEY`（或 `WORKBUDDY_ATREST_KEY_FILE`）时做
**只读解密**，用于从快照提取令牌；绝不生成或回写客户端信封。解密需要
`cryptography` 或 `pycryptodome`，没有也可走 `token import` 通道。

## 4. refresh 网关（通道 B 的保活依据）

```
POST https://copilot.tencent.com/v2/plugin/auth/token/refresh
Content-Type: application/json
X-Refresh-Token: <refresh token>
X-Auth-Refresh-Source: plugin
X-Domain: copilot.tencent.com
body: {}
→ {"code":0,"data":{"accessToken":"...","refreshToken":"..."}}
```

新 RT 与旧 RT 不同时要轮换回写（插件已实现）。本机
`~/.workbuddy/skills/workbuddy-daily-claim/scripts/claim.py` 有经过实测的实现，
本插件的 `call_refresh` 与其一致。

业务接口形如 `https://<auth.domain><path>`，带 `Authorization: Bearer <AT>` +
`X-Domain: <domain>`。已验证的只读接口：`POST /v2/billing/meter/checkin-activity-status`。
`run-as raw` 可调用任意路径；`run-as claim` 委托 daily-claim 技能完成签到/旅行闭环。

**另有一个不同主机、只认 Bearer 的只读接口**（2026-11-13 实测）：

```
POST https://copilot.tencent.com/v2/billing/meter/get-user-resource
Authorization: Bearer <AT>
User-Agent: <浏览器 UA>       # 缺省 Python-urllib 会被 403
→ data.Response.Data.Accounts[]，筛 CapacityType==1，求和 CapacityRemainPrecise，round(2)
```

它返回的是**客户端账号页「积分余额」**（带小数，实测当时 530.19 → 520.21），
与 `checkin-activity-status` 的 `total_credits`（**活动积分(累计)**，整数 2500）
是**两个独立池**。cookie 通道调它返回 401，`claim.py` 容错为 `None`。
详见同级技能 `workbuddy-daily-claim/references/api.md` 第 4 节。

## 5. 为什么切换必须先完全退出客户端

客户端进程（本机实测 `D:\workbuddy\WorkBuddy.exe`，`--user-data-dir=~/.workbuddy/app`）
运行时把会话放在内存里，**退出时会把登录态写回文件**。若在运行时替换文件，
要么被覆写，要么被内存中的旧会话覆盖，切换不生效甚至写坏文件。

因此 `switch`：

1. 检测到 `WorkBuddy.exe` 在运行 → 默认拒绝（`code: client_running`），
   给出「完全退出 → 终端运行 switch → 重开」的精确指令。
2. 客户端已退出 → 先把**当前**登录态加密备份到 `backups/`，再原子写回目标快照。
3. 用户重开 WorkBuddy 生效；异常时 `rollback` 一键恢复。

插件**不杀进程、不自动重启**。`--allow-running` 只是强制写入，不保证生效。

## 6. 账号数据目录（切换时不用动）

`~/.workbuddy/` 下按 uid 隔离，实测本机有两套（说明历史上切过号）：

```
<uid>/                         sidebar-list-snapshot.json, startup-route.json
user-<uid>-personal/           SOUL.md / IDENTITY.md / USER.md / MEMORY.md
plugin-account-state/<uid>.json
```

客户端会依据登录态里的 `account.uid` 自动选用对应目录，所以 `switch` **只需替换一个文件**，
不要去搬这些目录。

## 7. 已知限制与应对

| 限制 | 说明 / 应对 |
|---|---|
| **重登吊销旧 RT** | 本机 daily-claim 实测：「重登客户端会吊销」该账号之前的 refresh token。依次登录多个账号采集快照时，早采集账号的 RT 可能失效，快照只剩 access token 有效期（约 28 天）。通道 B 用独立导入的 RT + `keepalive` 才可靠。 |
| 快照不含新令牌 | `switch` 写回的是采集时的原始登录态，不注入通道 B 刷新出的新 AT/RT。切回某账号后立刻 `refresh <id>` 可捕获客户端轮换后的最新状态。 |
| 每次切换要重启 | 客户端架构限制，无法规避。若不想重启，走通道 B。 |
| 信封解密需要密钥 | 仅影响「从快照直接提取令牌」；`token import` 完全不需要。 |
| 官方无多账号 API | 文档只提供「退出登录 → 用另一个手机号重新验证」。本插件是本地状态管理，不改动客户端程序、不调用未公开账号接口。 |
| 可视化面板不执行切换 | 面板只监听 `127.0.0.1`、带一次性 token，动作白名单开放采集 / 改名 / 预览 / 体检 / 续期 / 只读查询 / 令牌恢复；真实 `switch` / `rollback` / `token import` / 任意 `raw` 一律返回 `blocked`，必须走终端。面板关掉后链接即失效。 |
| **全局单份凭据会串号** | daily-claim 的 `state/token.json`（Bearer 缓存）与 `state/web-cred.json`（网页会话）**各只有一份**。多账号下，无令牌账号必须由 `run-as` 下发 `WORKBUDDY_CLAIM_WEB_ONLY=1` 跳过全局 Bearer；网页会话归属 uid 记在 `web-owner.json`，给 A 号授权后 B 号的 `web_session` 会变 False。 |
| **账户积分余额只有 B 通道能查** | `get-user-resource` 只认 Bearer；只有网页会话的号显示「—」，绝不能借别的号的令牌代查。 |
| **`atRestSecretKey` 不在磁盘** | 它只存在于运行中的客户端进程内存（2026-11-13 扫 7243 个文件 / 130 条 base64 候选未命中；客户端重启后内存里的也换掉）。未固化进 `atrest.bin` 时，`token auto` 只能给已有 RT 的号续期；需重新登录该账号，由面板后台自愈或 `scripts/recover_atrest.py` 取回并 DPAPI 固化。 |

## 8. 排错

| 现象 | 处理 |
|---|---|
| `switch` 返回 `client_running` | 完全退出 WorkBuddy（含托盘），在终端执行或双击 `bin/switch.cmd` |
| `switch` 返回 `uid_mismatch` | 快照与索引 uid 不一致，已拒绝写入；重新 `capture` 该账号 |
| `keepalive` 返回 `refresh_failed` | RT 被重登吊销；客户端重新登录该账号后 `refresh <id>` 或重新 `token import` |
| `run-as` 返回 `no_token` | 未配令牌通道；`token import <id> --stdin`，或设置 `WORKBUDDY_ATREST_KEY` |
| `current` 说 `registered=false` | 该账号还没登记，`capture --label '别名'` |
| 切换后客户端仍是旧号 | 客户端没完全退出或退出时回写覆盖了；重跑 `switch`，必要时 `rollback` |
| 换电脑 / 换 Windows 用户 | DPAPI 密文解不开；快照需重新采集，令牌需重新导入 |
| 面板打开显示 403 | 一次性 token 丢失 / 过期或 Host 被改写；重新运行 `dashboard` 用新链接打开，不要用端口转发访问 |
| 面板按钮点了完全没反应 | 先确认后端还活着（`8765` 端口是否 LISTENING，或直连 `/api/state`）。切换账号会关闭客户端并连带结束同一进程组里的面板，重开客户端后面板**不会**自动重启，旧标签页只剩静态 DOM。重新运行 `dashboard` 用新链接打开即可（v1.5.3 起页面会明确提示服务已停止）。 |
| 切换后另一个号的任务列表异常（幽灵条目 / 列表不全 / 某会话打不开） | 「同步任务数据」转移后没清旧缓存或客户端缓存未重建：完全退出客户端，删 `~/.workbuddy/<uid>/sidebar-list-snapshot.json` 与 `startup-route.json` 后重开。想整体撤销转移：退出客户端，用 `~/.workbuddy/wb-accounts/backups/` 里最近一份 `workbuddy-db-*.db` 覆盖回 `~/.workbuddy/workbuddy.db`。 |
| 切换横幅显示「任务数据同步失败」 | 看切换本身是否成功（verified）；同步失败常见于 uid 读取失败或 DB 被占用。重试一次切换（不勾同步），或按上一行手动回退/清理。 |
| 面板按钮提示 `blocked` | 该动作刻意只在终端开放；用面板「终端命令速查」里生成的命令 |
| 浏览器已登录、面板仍「未配置」 | ① 是否在**面板弹出的独立 profile** 里登录（普通浏览器抓不到）；② 抓到的是不是登录页**匿名** `session`（校验 401，必须取登录成功后换发的最新 cookie）；③ 旧授权脚本只等 Bearer，手机号验证码登录永不触发 → 用「每轮取最新 + 6 秒校验探针」版 `grab_web_cred.js`。 |
| 账户积分余额接口 403 | UA 缺省为 `Python-urllib`，被网关拒收；`claim.py` 的 `api_call` 已加浏览器 UA 兜底，别删。 |
| 两个号积分 / 签到 / 旅行状态一模一样 | 命中了全局单份 `state/token.json`；`run-as` 必须下发 `WORKBUDDY_CLAIM_WEB_ONLY=1`（见 SKILL.md 硬规则 8）。 |
| 面板 `auto_tokens` 显示 x/y 未配齐 | 多为 `atrest.bin` 未固化；客户端切到该号登录一次 → 重开面板触发后台自愈，或手动跑 `scripts/recover_atrest.py`。已有 RT 的号不受影响。 |
| 活动积分是 0 却显示「—」 | 前端必须用 `!= null` 判断，不能用 truthy；0 是有效值。 |

## 9. 数据安全边界

- 不打印 / 不导出 `accessToken`、`refreshToken`、`envelope`、昵称、手机号明文。
- 快照（含客户端信封）与导入的令牌都用 DPAPI（Windows，绑定当前用户）二次加密，权限 600。
- 主 CLI 与 `wb_crypto` 不扫描进程内存、不读其它应用文件、不改客户端程序、不上传任何数据。
  **唯一例外**：面板可关闭的后台自愈 `scripts/recover_atrest.py`，只读**客户端自身进程**
  内存提取本机 `atRestSecretKey` 并立即 DPAPI 加密落盘（不打印明文、一天最多一次；
  `dashboard --no-auto-token` 可整条关闭）。
- `export-state` / `report.html` 只含 uid/uin 掩码、keyId、时间、布尔状态。
- `usage.jsonl` 只记事件 + 账号 id，不记凭据。
- 不代填密码、不绕过短信 / 验证码 / 2FA。
