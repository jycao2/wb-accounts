---
name: workbuddy-daily-claim
display_name: WorkBuddy 积分助手
display_name_en: WorkBuddy Daily Claim
version: 1.1.0
description: |
  WorkBuddy 积分助手：每日签到（幂等、免扫码）、活动积分（累计）、账户积分余额
  （两段式口径：平台奖励 CapacityType==1 的 CapacityRemainPrecise + 套餐本周期剩余
  CycleCapacityRemainPrecise）、派猫猫旅行状态。支持 Bearer 令牌通道与网页 cookie 会话
  双通道；通常由 wb-accounts 通过 `run-as <id> claim ...` 委托调用，实现一套令牌管多账号。
  触发词：签到、领积分、查积分、积分余额、活动积分、派猫猫、daily-claim。
---

# workbuddy-daily-claim

WorkBuddy 客户端的签到与积分查询工具。纯标准库实现（无第三方依赖），
既可作为 wb-accounts 的依赖被委托调用，也可单独在命令行使用。

本目录是 wb-accounts 仓库的 **vendor 副本**，由 `install.ps1` 复制到
`%USERPROFILE%\.workbuddy\skills\workbuddy-daily-claim\`。技能市场里没有它，
**请勿用市场版本覆盖**（市场版不含下面的积分口径修复，会导致余额对不上）。

## 常用命令

```bat
python claim.py                    :: 今日播报（签到状态 + 活动积分 + 账户余额）
python claim.py status             :: 只看状态，不签到
python claim.py checkin            :: 执行签到（幂等，重复调用不会重复加）
python claim.py setup              :: 首次配置：交互粘贴一次 Bearer 令牌
python claim.py --json             :: 机器可读输出（供 wb-accounts / 面板解析）
```

## 积分口径（关键，改代码前必读）

账户积分余额 = **平台奖励** + **套餐本周期剩余**，两段相加：

| 条目 | 字段 | 说明 |
|---|---|---|
| 平台奖励 | `CapacityType == 1` 的 `CapacityRemainPrecise` | 客户端账号页显示的固定奖励部分，**求和** |
| 套餐本周期剩余 | 其余 `CapacityType` 的 `CycleCapacityRemainPrecise` | 只取「本周期剩余」，会随使用实时下降 |
| ⚠️ 不要用 | 套餐条目的 `CapacityRemainPrecise` | 那是**总量池**（如 500），客户端并不显示它 |

另外两点不要改动：

- **`api_call` 里的浏览器 UA 兜底**：不带 UA 时接口返回 403；
- **cookie-only 账号**（`WORKBUDDY_CLAIM_WEB_ONLY=1`）不查账户余额，
  避免借用全局 Bearer 造成多账号串数据。

## 数据来源与状态文件

令牌来源优先级：`WORKBUDDY_ACCESS_TOKEN`（由 wb-accounts 注入）> 本机客户端登录态
> `state/token.json` 缓存 > 网页会话凭据 `state/web-cred.json`。

- `state/token.json` —— 本号 Bearer 缓存，可重建；
- `state/web-cred.json` —— 网页会话凭据（cookie / bearer + UA），可用网页授权生成；
- `state/rt.json` —— refresh token 缓存，可由 wb-accounts 保险库重建。

`state/` 下全部是**本机凭据**，不要提交到仓库、不要跨机器复制。
