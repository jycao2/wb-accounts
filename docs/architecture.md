# 架构说明

## 模块职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `wb_accounts.py` | CLI 主体：保险库（index/snapshots/tokens）、采集、切换编排、令牌通道、`run-as` 委托 | 纯标准库优先；所有输出脱敏 |
| `wb_crypto.py` | DPAPI 密封/解封、可选的只读信封解密 | 只在内存短暂持有明文 |
| `hot_switch.py` | 一键切换执行器，**独立进程**执行 | 不能跑在客户端进程树里，否则被连带杀掉 |
| `dashboard.py` | 本地面板 HTTP 服务 + API | 只绑 127.0.0.1；token + Host 校验 |
| `dashboard.html` | 单文件前端（无外部依赖） | CSP 禁止外部请求；不存令牌 |
| `recover_atrest.py` | 一次性引导：从运行中客户端内存取 `atRestSecretKey` 并固化 | 只读本机进程自身内存，取到即加密 |
| `sync_tasks.py` | 切换时复制任务数据（可选） | 复制不是转移；复制前整库备份；幂等续传 |
| `grab_web_cred.js` | 网页授权窗口（Playwright 独立 profile） | 只读该页面存储与该 profile 的 cookie |

## 一键切换时序

```text
面板/CLI
  │ ① 预检：账号存在、目标不是当前号
  ▼
hot_switch.py（独立进程，脱离客户端 job）
  │ ② 优雅关闭客户端 → 超时强杀 → 内核线程数判活（僵尸视为已退出）
  │ ③ 备份当前登录态（可回滚）
  │ ④ 写入目标账号快照
  │ ⑤ 校验写入结果（读回比对）
  │ ⑥ 重新拉起客户端（exe 路径由「遍历所有进程取第一个可读路径」得到）
  ▼
state/hot-switch.json 记录进度；面板轮询展示
```

失败点与行为：

- 客户端关不掉 → 中止在 ②，**不写登录态**，原状态完好；
- 写登录态失败 → 用 ③ 的备份回滚；
- 重开失败 → 登录态已切换，仅提示手动打开（`relaunch_note`）。

## 令牌通道

```text
capture ──▶ snapshots/<id>.bin（DPAPI）
                 │
token recover ───┤ 需要 atRestSecretKey
                 ▼
           atrest.bin（DPAPI 密封的密钥）
                 │
token auto ──▶ 有 RT：续期 access      ──▶ tokens/<id>.json
               无 RT：从快照恢复 + 验证
```

`atRestSecretKey` 只存在于**运行中的客户端内存**，因此首次固化需要客户端在跑；
固化一次后永久可用（同一台机器、同一 Windows 用户）。

## 面板启动顺序（重要）

```text
main()
 ├─ ① 端口占用检查 → 已占用则直接跳过退出（避免重复实例）
 ├─ ② make_server(127.0.0.1, token)   ← 先监听！
 ├─ ③ 打印启动 JSON（auto_background: true）
 ├─ ④ 后台线程：自动采集快照 → 自动令牌续期 → 补打结果 JSON
 └─ ⑤ serve_forever()
```

② 必须在 ④ 之前：自动采集要跑几十秒网络请求，若放在监听之前，启动器打开的浏览器
会直接 `ERR_CONNECTION_REFUSED`。启动器侧同样要**轮询探测端口**而不是固定 sleep。

## 数据分层

| 层 | 位置 | 是否可移植 |
|---|---|---|
| 代码 | 技能目录 | ✅ 完全可移植 |
| 保险库（凭据） | `~/.workbuddy/wb-accounts/` | ❌ DPAPI 绑定 Windows 用户 |
| 运行状态 | `wb-accounts/state/` | ⚠️ 可删，删后重建 |
| 面板 token | `~/.workbuddy/wb-accounts/dashboard-token.txt` | ❌ 每台机器各自生成 |
