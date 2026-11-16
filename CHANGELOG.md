# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 1.1.1 — 2026-11-16

### 文档

- **README 新增「切换账号时，任务跟着走」一节**（中英双版）：把原先只写在 `SKILL.md` 里的
  任务同步能力提到门面——复制语义、可只选部分任务、复制前整库备份。此前 README 只有功能表里
  一句「可选复制任务数据」，读者看不出这是本项目的核心卖点。
- **明确「记忆不复制」**：`SKILL.md` 与面板内置使用手册同步补充——用户级 `MEMORY.md` 与云端
  画像缓存都按 uid 分目录/分文件存放，切换后新号是空记忆；项目内的 `.workbuddy/memory/`
  属于工作区目录而非账号数据，换号后仍然共享。
- **写清快照式同步的限制**：`sync-ledger.json` 命中即跳过，源账号后来新增的消息不会补进副本，
  需要刷新得先删对应 key。此前只用「幂等」描述，容易被理解成两边自动保持同步。

## 1.1.0 — 2026-11-16

### 新增

- **一键安装**：`install.cmd`（双击）/ `install.ps1`（可传参）。自动把 `wb-accounts` 与
  内置的 `vendor/workbuddy-daily-claim` 安装到 `%USERPROFILE%\.workbuddy\skills\`，
  保留已有 `state\`，自动定位 Python（优先 WorkBuddy 自带运行时），安装完直接跑 `doctor`。
  重复执行即升级。支持 `-SkillsRoot` 指定安装目录、`-SkipDoctor` 跳过自检。
- **`vendor/workbuddy-daily-claim`**：签到/积分依赖技能随仓库分发（技能市场没有它）。
  内含两段式积分口径、UA 兜底、cookie 通道等修复，安装后签到与积分查询开箱可用。

### 说明

- 装完之后仍有一件"人的事"：先在客户端登录账号（每个号登一次），面板才能采集。
- 令牌与快照由 Windows DPAPI 按用户加密，**无法跨机器复制**；新机器需重新采集一次，
  之后 `atrest.bin` 由面板启动时后台引导，续期全自动。

## 1.0.1 — 2026-11-16

### 修复

- **启动器打开后 403**：`bin/open-dashboard.vbs` 之前打开的是裸地址
  `http://127.0.0.1:8765/`，依赖浏览器里早已种下的 cookie。换浏览器、清缓存或首次使用
  就会看到 `{"ok": false, "error": "未授权…"}` 的 JSON 页。现在启动器会读取保险库里的
  `dashboard-token.txt`（由 `dashboard.py` 启动时写入），拼成 `?t=<token>` 链接再打开，
  任何浏览器都能直接进入面板。
- **vbs 编码**：所有启动器改为**纯 ASCII 注释 + CRLF 换行**。此前用 UTF-8 无 BOM + LF
  保存，Windows 脚本宿主按 ANSI 解码会解析错乱，报出「无效的 'exit' 语句（800A040F）」
  这类假行号语法错误。

## 1.0.0 — 2026-11-15（开源首发）

首个公开版本，整合内部迭代 v1.9.0 ~ v1.11.0 的能力与修复。

### 功能

- **账号登记与快照**：`capture` 采集客户端当前登录态；`refresh` 令牌轮换后刷新；
  `list` / `current` / `brief` 查看；`label` / `remove`（归档，不真删）。
- **令牌通道**：`token auto|import|status|forget|recover|atrest-import`，`keepalive --all` 批量续期；
  `atrest.bin` 固化后全自动，无需再扫码。
- **本地面板**：`127.0.0.1:8765`，token + HttpOnly cookie 鉴权；含「📖 使用手册」内置说明；
  账号卡片可签到、切换、刷新快照、查状态、改别名、归档。
- **一键切换**：`hot_switch.py` 独立进程执行「关客户端 → 写登录态 → 校验 → 自动重开」；
  可选「同步任务数据」（**默认不勾选**，复制而非转移，复制前整库备份）。
- **签到领积分**：委托 `workbuddy-daily-claim`；面板与 CLI 均可，幂等、可批量。
- **环境自检**：`doctor` 报告各项依赖与权限状态。

### 相对内部版的改动（开源化）

- 面板 token 不再硬编码：改为「环境变量 > `~/.workbuddy/wb-accounts/dashboard-token.txt` > 首次生成」；
- `bin/*.vbs` 启动器改为**自动探测**：`%USERPROFILE%` 定位 + `binaries\python\versions\*\pythonw.exe` 扫描，
  不再写死用户名与 Python 版本号；
- `grab_web_cred.js` 的 `playwright-core` 与 `claim.py` 路径改为自动探测（可用
  `PW_CORE` / `CLAIM_PY` 覆盖）；
- 移除所有本机账号标识与个人状态记录，`state/` 与凭据目录加入 `.gitignore`。

### 关键修复（这几条改代码前务必先读）

- `dashboard.py` **先监听端口、再做自动采集**：采集/续期要几十秒，若阻塞在 `make_server` 之前，
  双击启动器打开的浏览器会「拒绝连接」。现改为端口立即可用，采集放后台守护线程，
  完成后补打 `{"background": true, "auto_capture": …, "auto_tokens": …}`。
- 启动器**轮询等待端口就绪**（0.7s × 最多 90 次），端口通了才开浏览器。
- `client_exe_path` 改为**遍历所有进程取第一个可读路径**：`Select -First 1` 会命中
  权限异常/僵尸进程的空 Path，被误记成「exe 未找到」，导致切换后不自动重开。
- 进程死活判定改为**读内核线程数**（`live_client_pids`）：线程/句柄均已清零的僵尸条目
  视为已退出，不再阻塞切换。

## 历史

内部版本 1.9.0 之前为私有迭代，主要解决：DPAPI 密封、refresh 网关续期、
`atRestSecretKey` 一次性引导、两段式积分口径（平台奖励 + 套餐本周期剩余）等问题。
