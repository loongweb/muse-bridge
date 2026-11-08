# 运维、排障与验收

所有命令中的 `bridge.py` 均指 `scripts/bridge.py`。凡涉及删除、清理、对外发送的动作，
**先列出路径并取得用户明确确认**，本 skill 不自动执行。

## 1. 症状 → 排查

| 症状 | 先跑 | 常见根因 |
|---|---|---|
| 对方说发了信，我看不到 | `bridge.py ls`、`doctor` | 双方 `mail_root` 不一致；比对会合点 `BRIDGE.json` |
| 信被读过后消失，业务没处理 | `doctor --legacy-script <旧客户端>` | 旧 watcher 取件即 ack（读=消费）；换成 `watch --once` |
| 完全收不到任何信 | `doctor`、`ssh <host> echo OK` | SSH 主线路不通且没配 `ssh_host_fallback` |
| 回信石沉大海 | `bridge.py acked <id>` | 把 `delivered` 当成已读；只有 `done/` 里的文件算数 |
| 附件说发了但取不到 | `bridge.py attachments <id>`、`doctor` | 旧客户端只 glob `*.md`；附件目录成孤儿 |
| 附件校验失败 | 同上 | 传输损坏或 manifest 与文件不匹配；不要跳过校验强行使用 |
| 日志越滚越大 | `doctor` | launchd/systemd 直接把 stdout 写单文件；改用 `/dev/null` + 自动轮转的 `bridge.log` |
| 同一封信被处理两次 | `doctor` | 用 `mv -n` 领取（目标存在仍返回 0）；改成硬链接领取 |
| 轮询频繁超时 | `doctor`、`ssh -G <host>` | 单 tailscale 地址被 VPN 数据面劫持；配第二可达主机 |
| Muse 侧 hook 醒来但无事可做 | `mb.sh doctor` | 门铃到了、信还没落盘；设计上门铃只是优化，轮询兜底 |
| 总线显示在线但发信无响应 | `mb.sh ls` | 陈旧 ONLINE 状态；presence 不判活，以 `done/` 为准 |

**定位顺序**：先 `ls` 看计数 → 再 `doctor` 看 FAIL → 最后才怀疑网络。
八成问题是路径或 ack 语义，不是链路。

## 2. 实测踩坑清单（来自 2026-09 ~ 11 的线上事故，已脱敏）

这些不是理论风险，每一条都真实造成过丢信或误判，因此被写进了 doctor 与自检：

1. **双方各建一套目录。** 一端抢跑建了 `agent-mail/`，另一端建了 `agent-bus/mail/`，
   hook 只盯自己的路径 → 信在两边都永远不可见。现在由 `BRIDGE.json` 强制单一事实来源。
2. **抢占式「收完即 ack」。** 常驻 listener 收到门铃就取件并 mv 进 done，只把内容打印进日志；
   WorkBuddy 会话没有任何入站 hook → 信被消费但从没被处理。现在 watcher 绝不 ack。
3. **Unix socket 假设同机。** 一端以为是 `~/workspace/.../bus.sock`，实际对方在云 VM，
   文件系统不通。确认拓扑再选 transport。
4. **等回复的 `chat` 会吞回复。** `create_connection(timeout=15)` 遗留的 socket 超时让 reader
   线程静默退出，几分钟后到达的回复没人接。要收迟到回复，用持久信箱 + 显式 ack，不要用同步等待。
5. **VPN 接管 tailscale 数据面。** 控制面显示在线、数据面全丢；单 host 写死导致上千次轮询超时。
   必须配 `ssh_host_fallback`。
6. **门铃 `delivered` 被当成送达。** 它只代表写进 socket。判据只有一个：`done/` 里有没有该文件。
7. **附件无协议。** 交付方把 PNG 直接丢进信箱目录，取件方只 glob `*.md` → 2.6MB 文件卡住，
   而通知信已被 ack。现在附件先落盘、信件为提交点、ack 时一起归档。
8. **凭据三处泄露。** 硬编码在 `.py`（其中一个 644 全局可读）、以 `--token` 出现在 `ps aux`、
   明文 TCP 公网监听。现在凭据只在 0600 文件、门铃默认关闭。
9. **日志单文件膨胀 + 重连噪声。** 426KB / 6603 行里绝大多数是 `closed on register`，
   没有轮转配置。现在 `bridge.log` 自带大小轮转，job 的 stdout 丢弃。
10. **明文约定当闸门。** 协议写「需用户批准」，但代码里没有任何强制；现在 `X-Requires-Approval`
    由 `inbox`/`watch` 主动高亮，并写进红线。

## 3. 上线验收清单

- [ ] `bridge.py selftest` → **18 项全 PASS，FAIL=0**
- [ ] `bridge.py doctor` → **FAIL=0**；WARN 逐条读过并接受
- [ ] 双方 `mail_root` 与 `BRIDGE.json` 完全一致
- [ ] `ssh_host_fallback` 已配，且实测主 host 断开时能自动切换
- [ ] 所有凭据文件 `chmod 600`；源码 / 命令行 / 日志中无字面 token
- [ ] 唤醒任务指向 `watch --once`，**绝不指向 `fetch` 或任何自动 ack 的命令**
- [ ] 端到端手工跑一遍：发信 → `inbox` → 处理 → `ack` → 对面 `acked` 可见
- [ ] 带附件再跑一遍：sha256 校验通过、ack 后无孤儿目录
- [ ] 断网 / 门铃失败演练：信仍在盘上，恢复后轮询能发现

## 4. FAQ

**为什么不用邮件 / Webhook / 消息队列？**
Muse 的 Secure VM 零入站端口，且没有 Agent Mail（只能碰用户 Gmail）。Webhook、MQ 消费者、
SSH 直连都需要入站能力，实测都不成立。会合点只要求双方能**主动出网**，是唯一可行解。

**门铃要不要开？**
默认关闭。门铃能把延迟从分钟级降到秒级，但引入明文 TCP 端口、静态 token 和一套长连接状态机。
先把纯轮询跑稳（60s 对 Agent 协作通常够用），确有低延迟需求再开，且优先走私有接口。
**门铃丢了不丢信**，它永远不是可靠性的一部分。

**轮询 60s 会不会太费资源？**
SSH 后端启用了 ControlMaster 连接复用（`ControlPersist=600`），后续轮询不付完整握手成本。
纯 Python 的 local 后端零网络开销。不要为了「实时」把间隔压到 1s。

**能不能让 watcher 自动 ack，省得会话忘了？**
不能。这正是本 skill 要消灭的故障模式。未 ack 的信会一直留在箱里，下次会话重新可见；
自动 ack 则不可逆。宁可重复提醒，也不要静默丢失。

**`done/` 会无限增长吗？**
`doctor` 会按 `done_retention_days`（默认 30）报 WARN。清理前**必须先向用户列出全部路径并确认**，
本 skill 不自动删除。建议先打包归档到别处，再清理。

**Windows 能用吗？**
`backend=local` 是纯 Python，可直接用。`backend=ssh` 依赖系统 OpenSSH（Win10+ 自带），
远程是 Linux/bash，也可用。唤醒任务在 Windows 上需改用「任务计划程序」，
命令同样是 `python bridge.py --config <cfg> watch --once`。

**已有 v1 桥要不要整体替换？**
不必。v2 与 v1 双向兼容（见 `references/protocol.md` 兼容矩阵）。先换 WorkBuddy 侧，
Muse 侧保持 v1 也能正常收发；附件与结构化头属于渐进增强。

**如何确认对端真的读到了？**
`bridge.py acked <id>` 查 `<peer>/done/<id>.md`。总线 `delivered`、`list` 里的在线状态、
门铃回执都不算证据。

## 5. 运维建议

- **备份**：定期打包整个 `mail_root`（含 `done/`）到另一处；信箱是唯一状态，VPS 挂了就全靠它。
- **监控**：把 `doctor` 挂到日常巡检，只对 `FAIL` 告警；`WARN` 里的积压数可做趋势。
- **日志**：`~/.local/state/muse-bridge/bridge.log` 自动轮转，保留 2 份；不要再把 job 的
  stdout 指向一个永不轮转的文件。
- **凭据轮换**：更新 0600 token 文件即可，无需改代码；轮换后跑一次 `doctor` 确认权限仍是 600。
- **变更纪律**：改协议前先跑 `selftest`；改完再跑一次。任何偏离协议的实现都要回到
  `references/protocol.md` 补记，不要留口头约定。

## 6. 无 VPS / HTTP 后端排障

| 现象 | 原因 / 处理 |
|---|---|
| `unauthorized` / 401 | token 文件内容不一致、被清空、或带了多余空行。核对 0600 文件；**不要**把 token 贴进命令行或聊天 |
| `config error: refusing plaintext http to a remote host` | 远程必须走 https，Bearer token 不能裸奔。只有 `127.0.0.1` / `localhost` 允许明文 |
| `http rendezvous unreachable` | 先看 `curl <base_url>/v2/health`。不通就查 cloudflared 是否在跑、hostname 是否指向正确隧道、本机是否刚睡醒 |
| 对端说「发不出去」但本机能收 | **本机睡眠 / 关机期间隧道断开**。信没丢，服务恢复后自动可见；让对端用同一个 id 重试，**不要换新 id 重发** |
| 容器重启后信和附件全没了 | 免费层的临时文件系统被清空 —— 没挂持久卷。这违反「文件管存永不丢」，上线前必须做重启验收 |
| `serve.py` 打印 `WARNING: binding 0.0.0.0` | 容器里是必要的，但公网暴露必须叠 TLS 终结 + Bearer token；本机部署一律保持 `127.0.0.1` |
| CF Access 开着，curl 全部 403 | Access 对非浏览器客户端不友好：要么给对端配 service token 的两个 header，要么只用强 Bearer token，**不要**误以为 Access 已经替你挡住了 |
| `probe.sh` 全部 `BLOCKED` | 该 VM 没有可用出站 HTTPS；无 VPS 方案不成立，回到 VPS（见 `references/no-vps.md`） |
| `path outside mail_root` / 400 | 服务端路径沙箱生效，属正常防护。检查两端 `BRIDGE.json` 的 `mail_root` 是否完全一致 |

`doctor` 在 `backend=http` 时会额外检查 token 文件权限、`/v2/health` 可达性，并固定给出一条
`always-on rendezvous` WARN —— 提醒本机下线会关闭对端的写入窗口。详见 `references/no-vps.md`。
