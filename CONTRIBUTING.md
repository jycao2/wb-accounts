# 参与贡献

欢迎 issue 与 PR，尤其是：新版客户端的兼容性、异常场景的复现、文档纠错。

## 开发环境

1. Windows 10/11 + Python 3.10+（WorkBuddy 自带运行时也能用）；
2. 把仓库放到 `%USERPROFILE%\.workbuddy\skills\wb-accounts\`（或任意目录 +
   `WB_ACCOUNTS_HOME` 指向你的保险库）；
3. `python scripts\wb_accounts.py doctor` 应全绿，再开始改。

调试面板：`python scripts\wb_accounts.py dashboard` 前台运行，Ctrl+C 停止；
改 `dashboard.html` 后**刷新浏览器即可**（服务每次请求都重读文件），改 `dashboard.py` 需重启。

## 硬性约定（会 review 检查）

1. **绝不提交凭据或本机状态**：`state/`、`snapshots/`、`tokens/`、`atrest.bin`、
   `dashboard-token.txt`、任何 `*.log`、`*.code` 都在 `.gitignore` 里，请勿强行加入。
2. **不允许硬编码绝对路径**：用 `Path.home()`、`__file__` 相对定位、`%USERPROFILE%`；
   需要外部工具时用「环境变量可覆盖 + 自动探测」的组合。
3. **不允许在代码/日志/页面中出现令牌明文**：输出前走 `redact()`；UID/UIN 打码。
4. **面板只绑 127.0.0.1**，不得改成 `0.0.0.0`，不得移除 `Host` 校验。
5. **破坏性操作必须有幂等或回滚**：归档不真删、切换失败不破坏原登录态、复制前备份。

## 提交前自检

```bat
python -c "import ast,glob;[ast.parse(open(f,encoding='utf-8').read()) for f in glob.glob('scripts/*.py')]"
node --check scripts\grab_web_cred.js
python scripts\wb_accounts.py doctor
```

## 提交信息

用简短的祈使句概括行为，必要时在正文写「为什么」：

```text
dashboard: 先监听端口再做自动采集

采集要几十秒，阻塞在 make_server 前会让双击启动器打开的
浏览器直接 ERR_CONNECTION_REFUSED。
```

修 bug 请顺带在 `CHANGELOG.md` 的「关键修复」区补一行，注明触发条件。

## 报告问题

请附：Windows 版本、Python 版本、WorkBuddy 客户端版本、复现步骤、
`doctor` 的脱敏输出。**不要贴任何令牌、快照内容或完整日志**——需要时请自行打码。
