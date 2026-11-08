# 独立验真清单（给对端 Agent / 贡献者，勿空签）

> 原则：**不盲信自报**。维护者声称「23/23 全绿」属于自述层，
> 你自己跑出来的结果才进验真层。两者不一致时以你的实测为准，直接回报差异。

## 一、最小复跑（不碰生产、不联网、不改任何现有文件）

```bash
cd /tmp && rm -rf mb-verify && mkdir mb-verify && cd mb-verify
unzip -q <path-to>/muse-bridge.zip        # 或 git clone 本仓库
cd muse-bridge/scripts                     # 或 <clone-dir>/scripts
python3 -m py_compile bridge.py serve.py selftest.py
MUSE_BRIDGE_STATE=$(mktemp -d) python3 selftest.py
```

预期：`PASS=23  FAIL=0  TOTAL=23`。

自检只用临时目录 + 本地 backend + `127.0.0.1` 临时端口：
**不写你的信箱、不连生产 VPS、不启用任何服务、不安装依赖**。

## 二、五条不变量与对应实现（逐条可查证）

| # | 不变量 | 去哪查 | 对应自检用例 |
|---|---|---|---|
| 1 | 读 ≠ 消费 | `bridge.py` 的 `cmd_inbox` / `read` / `watch` 均不调 `ack`；`ack()` 是唯一执行 `claim` 的地方 | `read does not consume` |
| 2 | watcher 绝不 ack | `watch()` 只打印 + 写日志，`seen` 集合持久化去重 | `watch never acks` |
| 3 | 附件先落盘且随信归档 | `send()` 先 `write_atomic(attachments/<id>/…)`，`.md` 最后写作为提交点；`ack()` 连附件目录一起归档 | `attachment tamper rejected` (sha256) |
| 4 | `BRIDGE.json` 单一 `mail_root` | 启动时比对自身 `mail_root` 与标记文件，不一致立即报错 | 路径漂移用例 |
| 5 | 凭据只在 0600 文件 | `Config.token_path`；HTTP 客户端 token 走 `curl -K` 文件流、不进 argv；远程明文 http 在配置层拒绝 | `http: token enforcement` |

**建议重点打第 1、3 条** —— 这正是 v1 老桥出过事故的地方：
watcher 读到就 `mv` 进 `done/`（信被吞）、`*.md` glob 漏掉全部散落附件（PNG 永远取不走）。

其余覆盖：6 进程并发 ack 同一封信恰好 1 个赢家、sha256 篡改检出、v1 解析器兼容 v2 信、
HTTP 鉴权拒绝、路径穿越防护、4 线程并发、门铃挂掉信仍在、doctor 能检出孤儿附件。

## 三、回报模板

```
VERIFY_RUN:     PASS=.. FAIL=.. TOTAL=..
INVARIANTS:     1..5 逐条 pass/fail（哪条不通过直接说）
SELFTEST_GAPS:  你认为 23 项没覆盖到的场景
ENVIRONMENT:    OS / Python 版本 / backend
NOTES:          其他（含任何与自述不一致的地方）
```

## 四、验真时的边界（请一并核）

- **不要**改生产总线、重启服务、迁移信道、替对端 ack 或 `mv` 任何信件。
- **不要**创建 `BRIDGE.json` 到已在产的信箱目录 —— 那属于改对方结构，需用户本人确认。
- **不要**在回信里贴任何凭据；token 只在 0600 文件里、不进 argv / 日志 / 消息正文。
- `delivered ≠ read` 仍是协议层缺口：投递成功不代表对端读过，**唯一送达凭证是显式 ack**。

## 五、一个容易误判的现象

**信长期未 ack ≠ 丢信 ≠ 对端没读到。** 某些 Agent 对「要求执行动作」的来信设有安全审查队列，
会主动搁置，直到**用户本人在客户端说一句话**才处理；它的轮询 hook 可能早就唤醒过。
遇到未 ack：先确认是不是在等授权，**别急着改传输层**，也别顺手批准无关的待批准项。
