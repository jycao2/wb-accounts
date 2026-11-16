# wb-accounts · WorkBuddy 多账号管家

> 一台机器上管理多个 WorkBuddy 桌面账号：**一次登记，长期免扫码**。
> 附带本地可视化面板、一键签到、一键切换客户端账号——**换号时连任务一起带走，不用从零开始**。

<p align="center">
  <em>Windows 10/11 · Python 3.10+ · 纯标准库优先 · 只监听 127.0.0.1</em>
</p>

---

## 它解决什么问题

WorkBuddy 桌面客户端同一时刻只能登录一个账号，退出再登另一个就得**重新扫码/收验证码**。
如果你是「主号 + 小号 + 网页授权号」这种多号用户，每天来回切很痛苦。

wb-accounts 把每个号的**登录态快照**和**令牌通道**管理起来：

- 需要哪个号，一键切换，客户端自动退出 → 写入该号登录态 → 校验 → **自动重开**；
- 平时不用切也能**查状态、领积分**（走独立的令牌通道，免登录）；
- 全程不碰你的密码，凭据用 Windows DPAPI 按当前用户加密落盘。

## 功能一览

| 能力 | 说明 |
|---|---|
| **账号登记** | 采集客户端当前登录态为一个快照（不含密码），可登记任意多个号 |
| **令牌通道** | 用 `refresh token` 自动续期，长期免扫码；凭据 DPAPI 加密 |
| **可视化面板** | 纯本机网页（`127.0.0.1:8765`），看余额/签到/切换/续期，含使用手册 |
| **签到领积分** | 面板一键签到（依赖的 `workbuddy-daily-claim` 已内置在 `vendor/`，安装脚本一起装好），幂等、可批量 |
| **一键切换** | 关客户端 → 写登录态 → 校验 → 自动重开；可选把当前号的任务一起带过去 |
| **环境自检** | `doctor` 一条命令查清哪里没配好 |

## 切换账号时，任务跟着走

换号最烦的不是重新登录，是**之前干的活全留在旧号里**。切换确认弹窗里勾上「同步任务数据」
（**默认关**，你明确勾了才动），当前号的任务会整份复制到目标号名下——切过去侧边栏就在、能打开、
能接着聊：

- 会话条目、完整对话正文（含工具调用记录）、所在分组、会话工作目录一起搬；
- **复制不是转移**：原账号的数据一行不动，两个号各留一份，互不影响；
- 可只挑一部分：弹窗列出当前号全部存活任务（默认全选），支持搜索和逐条取消；
- 动手前把整个 `workbuddy.db` 备份到 `wb-accounts/backups/`，每条成功即提交，
  失败只记账、不阻断切换本身。

三条边界最好先知道：

| 不跟着走 | 原因 |
|---|---|
| 定时任务（automations） | 绑定运行环境与账号，复制过去也跑不起来，得在新号重建 |
| 进行中的生成 | 切换必须先完全退出客户端，正在跑的任务会中断 |
| 记忆 | 用户级 `MEMORY.md` 与云端画像都按账号隔离存放，不参与复制；只有项目内的 `.workbuddy/memory/` 属于目录、换号后仍然共享 |

还有一个容易踩的点：同步是**快照式**的。`sync-ledger.json` 记下「源会话 → 新会话」的映射，
同一个会话再切一次会被直接跳过，**不会**把旧号后来新增的消息补过去。想让两边保持一致，
需要先删掉账本里对应的条目再同步一次。

## 快速开始

先把代码拿到本机，任选一种：

```bat
git clone https://github.com/jycao2/wb-accounts.git
cd wb-accounts
```

或者打开仓库页 → 右上角 **Code → Download ZIP** → 解压。下面所有命令都在这个目录里执行。

### 0. 一键安装（推荐，新电脑照这个来）

双击仓库根目录的 **`install.cmd`**。它会：

1. 把 `wb-accounts` 装到 `%USERPROFILE%\.workbuddy\skills\wb-accounts\`；
2. 把内置的 `vendor\workbuddy-daily-claim`（签到/积分的依赖）装到
   `...\skills\workbuddy-daily-claim\`；
3. **保留已有的 `state\`**（快照、令牌、别名不会被覆盖）；
4. 自动找到 Python 并跑一次 `doctor`，缺什么直接打在屏幕上。

重复执行 = 升级，账号数据不受影响。想装到别处：

```bat
powershell -ExecutionPolicy Bypass -File install.ps1 -SkillsRoot "D:\my\skills"
```

### 1. 手动放置（不想跑脚本时）

```text
%USERPROFILE%\.workbuddy\skills\wb-accounts\
%USERPROFILE%\.workbuddy\skills\workbuddy-daily-claim\    <- 从 vendor\ 复制
```

也可以用环境变量 `WB_ACCOUNTS_HOME` 指定数据目录。

### 2. 环境自检

```bat
python scripts\wb_accounts.py doctor
```

### 3. 登记第一个账号

先在 WorkBuddy 客户端登录你要管理的号，然后：

```bat
python scripts\wb_accounts.py capture --label 主号
```

多号就重复「客户端登录 → capture」这一步。

### 4. 打开面板

双击 `bin\open-dashboard.vbs`（它会自己找 Python、拉起服务、打开浏览器）。
首次会生成一个本机 token 存在 `~/.workbuddy/wb-accounts/dashboard-token.txt`；
启动器每次都会读出该 token、拼成 `http://127.0.0.1:8765/?t=<token>` 再打开，
所以换浏览器、清缓存都不会看到 403。浏览器记住 cookie 后，也可以直接访问
<http://127.0.0.1:8765>。

## 常用命令

```bat
:: 看当前客户端登的是谁
python scripts\wb_accounts.py current

:: 列出所有已登记账号
python scripts\wb_accounts.py list

:: 一屏概览（给脚本/AI 用，JSON）
python scripts\wb_accounts.py --json brief

:: 续期全部账号的令牌
python scripts\wb_accounts.py keepalive --all

:: 切换账号（先完全退出客户端，含托盘）
python scripts\wb_accounts.py switch <账号id>

:: 切错了？回滚
python scripts\wb_accounts.py rollback

:: 以某个号的身份查状态 / 签到
python scripts\wb_accounts.py run-as <账号id> status
python scripts\wb_accounts.py run-as <账号id> claim

:: 起面板（前台，Ctrl+C 停止）
python scripts\wb_accounts.py dashboard
```

## 目录结构

```text
wb-accounts/
├─ install.cmd / install.ps1 # 一键安装：装两个技能 + 跑 doctor（可重复执行 = 升级）
├─ vendor/
│  └─ workbuddy-daily-claim/ # 签到/积分依赖技能（市场没有，随仓库分发）
├─ SKILL.md                 # 技能定义（供 AI 助手读取的操作手册）
├─ scripts/
│  ├─ wb_accounts.py        # CLI 主体：保险库、采集、切换、令牌通道
│  ├─ dashboard.py          # 本地面板服务（127.0.0.1 + token/cookie）
│  ├─ dashboard.html        # 面板前端（单文件，无外部依赖）
│  ├─ hot_switch.py         # 一键切换执行器（独立进程，避免被客户端退出连带杀掉）
│  ├─ wb_crypto.py          # DPAPI 密封 / 只读解密
│  ├─ recover_atrest.py     # atRestSecretKey 一次性引导（读客户端自身内存）
│  ├─ sync_tasks.py         # 切换时复制任务数据（可选）
│  ├─ grab_web_cred.js      # 网页授权窗口（Playwright，可选）
│  └─ atrest_hunt.py / atrest_cdp.py   # 备用密钥定位工具
├─ bin/                     # 双击即用的启动器（.vbs / .cmd）
├─ references/              # 协议与限制说明
└─ docs/                    # 架构与安全说明
```

## 数据放在哪

| 路径 | 内容 |
|---|---|
| `~/.workbuddy/wb-accounts/` | 保险库：`index.json`、`snapshots/`、`tokens/`、`trash/` |
| `~/.workbuddy/wb-accounts/atrest.bin` | DPAPI 加密的 `atRestSecretKey`（解密快照用） |
| `~/.workbuddy/wb-accounts/dashboard-token.txt` | 面板访问 token（首次自动生成） |
| `wb-accounts/state/` | 本技能的运行状态（日志、切换记录、浏览器 profile） |

**所有凭据都只在本机、只对当前 Windows 用户可解**。换机器或换用户，DPAPI 解不开，
需要重新登记（这是设计如此，不是 bug）。

## 安全边界

- 面板只绑 `127.0.0.1`，校验 `Host`，token + HttpOnly cookie 鉴权，CSP 禁止外部请求；
- 页面与日志均不含令牌明文（UID/UIN 打码、JWT 与密钥字段自动脱敏）；
- 快照与令牌使用 Windows DPAPI（当前用户作用域）加密；
- `recover_atrest.py` 只读**本机运行中客户端自身的内存**，取到即加密落盘，不联网；
- 本项目**不修改客户端安装目录**，不注入进程，不绕过任何服务端校验。

更多细节与威胁模型见 [docs/security.md](docs/security.md)。

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10 / 11 | DPAPI 与客户端均为 Windows |
| Python | 3.10+ | WorkBuddy 自带运行时也可（启动器会自动找） |
| WorkBuddy 客户端 | 已安装并登录过 | 采集登录态的前提 |
| Node.js | 18+（可选） | 仅「网页授权窗口」需要 |
| workbuddy-daily-claim | **随仓库内置** | 在 `vendor/` 下，安装脚本自动装好；仅签到/积分查询需要，缺失时其余功能正常 |

## 常见问题

**Q：面板打不开 / 拒绝连接？**
双击 `bin/open-dashboard.vbs`（会自动启动服务并等端口就绪）。若仍失败，
用 `python scripts\wb_accounts.py dashboard` 前台起，看报错。

**Q：令牌通道配不上？**
先跑 `doctor`。常见原因是 `atrest.bin` 未固化——需要客户端**正在运行且已登录**，
再让面板触发一次自动配置；仍失败就用 `run-as <id> claim` 走 cookie 通道兜底。

**Q：切换失败？**
多半是客户端没完全退出（含托盘）。关干净再试；`switch` 失败不会破坏原登录态。

**Q：能直接把别人机器上的 `~/.workbuddy/wb-accounts` 拷过来用吗？**
不能。DPAPI 绑定 Windows 用户，拷过去也解不开；每个账号都要在目标机器上重新登记一次。

## 致谢与声明

本项目通过读取**本机**客户端登录态实现免登录切换与查询，供个人提效使用。
请遵守 WorkBuddy 的服务条款，不要用于分享账号、绕过风控或任何侵犯他人账号的用途。
凭据安全由你本机的 Windows 账户隔离性保障——**不要把 `~/.workbuddy/wb-accounts` 分享给任何人**。

## License

[MIT](LICENSE)
