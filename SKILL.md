---
name: muse-bridge
description: 让 WorkBuddy 与 Muse（Meta 的个人 AI Agent，跑在零入站端口的 Secure VM 里）或任何「只能出网、无法被连入」的异地 Agent 建立可靠双向互通。当用户说「跟 Muse 互通 / 通信 / 协作」「Muse 收不到消息」「信箱里的信不见了 / 被吞了」「附件传不过去」「搭个会合点 / agent-bus / 文件信箱」「没有 VPS 怎么互通 / 用 Cloudflare Tunnel / CF zone / 免费容器 / Docker 当会合点」，或需要新建、体检、迁移现有的消息桥时使用。提供会合点协议、ssh/local/http 三种后端、双端客户端、只读诊断和 23 项端到端自检。
agent_created: true
---

# Muse Bridge — WorkBuddy ↔ Muse 会合点互通

## 机制先行（不要跳过）

Muse 跑在**零入站端口的 Secure VM**：SSH 连不进去、Webhook 打不进来、没有 Agent Mail。
任何「连进 Muse」的方案（直连 SSH、常驻 bot、邮件桥）都不成立，不要在这些方向上浪费时间。

唯一成立的拓扑是**会合点**：一个双方都能主动出网到达的共享信箱。
**VPS 只是会合点的一种宿主**，不是架构前提。没有 VPS 时用出站隧道或免费常驻容器，
见 `references/no-vps.md`。

```
ssh    WorkBuddy ──出站 SSH──► [ VPS mail_root/ ] ◄──出站 SSH── Muse
http   WorkBuddy ──HTTPS────► [ serve.py + CF Tunnel / 免费容器 ] ◄──出站 HTTPS── Muse
local  两端同机 ────────────► [ 本机目录 ]                    （一条流量不出网）
```

`backend=http` 与 ssh/local **共用同一套协议与所有命令**（信头、ack、附件 manifest 全不变），
只把文件操作换成 7 个 `/v2/*` HTTP 端点。Docker 不生产公网地址，别把它当会合点的替代品。

三层职责必须分开，混在一起就是事故来源：

| 层 | 职责 | 失败时的表现 |
|---|---|---|
| 文件 | 持久保存，永不丢 | 文件还在，只是没人看 |
| ack | 确认**业务已处理完** | 未 ack 的信永远留在箱里 |
| 门铃 / 轮询 | 只负责唤醒，不负责送达 | 门铃丢了，轮询仍能发现信 |

## 五条不变量（本 skill 的全部设计都围绕它们）

1. **读 ≠ 消费。** 只有 `ack` 把 `.md` 移进 `done/` 才算消费；`inbox` / `read` / `watch` 一律只读。
2. **守护进程绝不代签。** watcher 只把信镜像到本地队列并通知，永远不调 `ack`。
   历史事故：常驻 listener 收到门铃就取件并自动 mv 进 done，信只被打印进日志，会话里没人知道。
3. **附件与信件同生命周期。** 附件先落盘、信件后落盘（信是提交点），ack 时一起归档；
   每份附件带 sha256，损坏必须报错而不是静默接收。
4. **单一事实来源。** 双方必须使用**完全相同**的 `mail_root`，以会合点上的 `BRIDGE.json` 为准。
   历史事故：两端各建一套目录，一方的信另一方永远看不到。
5. **凭据不进代码、不进命令行。** token 只放在 0600 文件里，运行时读取。

## 何时用

- 首次让 WorkBuddy 与 Muse（或异地 Agent）互通
- 已有桥出现：消息丢失、收不到回信、附件卡住、日志膨胀、链路频繁超时、重复处理
- 需要审计 / 迁移一套旧版 `agent-bus` + 文件信箱实现
- 用户要求给 Muse 一段「照着做就能接入」的说明

## 决策树

- **没有会合点 / 没有配置** → 走「工作流 A：从零搭建」
- **没有 VPS，想用 CF Tunnel / 自己的 zone / 免费容器** → 走「工作流 C：无 VPS 会合点」
- **已有一套在跑的桥** → 先走「工作流 B：只读体检」，不要直接改对方的代码或信箱
- **只是要在本次会话里收发信** → 直接用「会话内收信」小节，跳过所有安装步骤
- **要给 Muse 接入说明** → 运行 `muse-prompt`（按 backend 自动输出 SSH 版或 HTTP/curl 版），整段粘贴给 Muse

## 工作流 A：从零搭建

以下所有命令中的 `<SKILL>` 指本 skill 目录。所有路径、主机、名称都从配置读取，
**绝不把任何 IP、token 或用户名写进脚本**。

1. 在会合点主机上**只跑一次**初始化（把脚本拷过去或用 stdin 喂给 ssh）：
   ```bash
   bash <SKILL>/assets/rendezvous-init.sh /srv/muse-bridge/mail workbuddy muse
   ```
   生成目录、`BRIDGE.json`（canonical mail_root）、以及给 Muse 侧用的 `send.sh` / `mb.sh`。

2. 在本机生成 0600 配置：
   ```bash
   python3 <SKILL>/scripts/bridge.py init
   ```
   按 `assets/config.example.json` 填 `ssh_host`、`ssh_host_fallback`、`mail_root`。
   **务必配置第二个可达主机**：实测中单 tailscale 地址超时占了轮询失败的八成。
   `bell.enabled` 默认 false —— 纯轮询零额外暴露面，先跑通再考虑门铃。

3. 验证：
   ```bash
   python3 <SKILL>/scripts/bridge.py --config ~/.config/muse-bridge/config.json selftest
   python3 <SKILL>/scripts/bridge.py doctor
   ```
   自检 23 项须全 PASS；doctor 的 FAIL 必须清零再上线。

4. 把接入说明给 Muse：
   ```bash
   python3 <SKILL>/scripts/bridge.py muse-prompt
   ```
   输出会带实际 `mail_root` 和主机名，**整段粘贴给 Muse**。Muse 侧不需要安装本 skill，
   它通过 ssh 调用会合点上的 `mb.sh` 即可。

5. 装唤醒任务（可选但推荐）：
   ```bash
   bash <SKILL>/assets/install-watch.sh ~/.config/muse-bridge/config.json
   ```
   脚本只写 job 文件并打印启用命令，**不会擅自启用服务**。按打印的命令启用。
   采用「定时短进程 + `watch --once`」而非常驻长连接：无重连状态机、无重复实例、睡眠唤醒后自动补跑。

## 工作流 B：只读体检 / 迁移旧桥

1. 用临时配置指向旧信箱（不要改用户原有配置）：
   ```bash
   python3 <SKILL>/scripts/bridge.py --config /tmp/audit.json doctor --legacy-script /path/to/old_client.py
   ```
   `--legacy-script` 会额外检出「取件即 ack」的老客户端。doctor 全程只读会合点。
2. 逐条读 FAIL/WARN 及修复建议。**不要自动删除任何东西**，包括 `done/` 归档和孤儿附件。
3. 迁移时先保留旧目录，用新 `mail_root` 或同一路径按 v2 布局补建目录；v2 与 v1 兼容
   （见 `references/protocol.md` 兼容矩阵），旧客户端不升级仍能收发基础消息。
4. 迁移前跑 `selftest`，迁移后再跑一次 `doctor`。

## 工作流 C：无 VPS 会合点

**先读 `references/no-vps.md`**。核心判断：出站隧道（Cloudflare Tunnel）能解决「本机没有公网入站端口」，
但会合点在用户电脑上时，**电脑一睡，Muse 就写不进新信**（信不丢，只是发不出）。
要真正的异步交付，用免费常驻容器；本机隧道适合联调和「让对端访问本机资源」。

1. **先过 gate**：让 Muse 跑 `assets/probe.sh`，确认它的 VM 能出网 HTTPS 到候选域名。
   没测过就选方案 = 把客户端建在假设上。
2. **选宿主**：
   - 免费常驻容器 → `assets/Dockerfile` 跑 `scripts/serve.py`，**必须挂持久卷**。
   - WorkBuddy 本机 + CF Tunnel → `assets/tunnel-init.sh <你的 zone 域名>`。
3. **起服务**（`serve.py` 纯标准库，默认只绑 `127.0.0.1`；token 只写 0600 文件）：
   ```bash
   python3 <SKILL>/scripts/serve.py --root ~/.local/share/muse-bridge/mail \
                                    --token-file ~/.config/muse-bridge/http-token --init-token
   python3 <SKILL>/scripts/serve.py --root ~/.local/share/muse-bridge/mail \
                                    --token-file ~/.config/muse-bridge/http-token
   ```
4. **填配置**：`backend=http`，`http.base_url=https://<hostname>`，`http.token_file` 指向同一个 0600 文件。
   远程明文 `http://` 会被 Config 直接拒绝（Bearer token 不能裸奔）。
5. **给 Muse**：`muse-prompt` 会输出 `references/muse-side-http.md`（curl 版）。
   `assets/mb-http.sh` 是给对端的自包含客户端（bash + curl，无需 Python）。
   **token 另走安全渠道单独给，绝不写进粘贴内容。**
6. **验证**：`selftest` 23 项全绿 → `doctor`（会额外检查 http token 权限、health 可达、
   并给出「本机睡眠期间对端无法写入」的 WARN）→ 对端跑 `mb-http.sh doctor` 比对 `mail_root`。

`tunnel-init.sh` 只写配置文件和 launchd/systemd job，**不登录 Cloudflare、不建隧道、不改 DNS、不启用服务**。
DNS 变更与启用服务是对外动作，必须由用户本人按脚本打印的 runbook 确认后执行。

## 会话内收信（每次会话都要遵守）

**这是最容易被跳过、也最致命的一步。** 轮询任务只是通知，它不会把信交给会话。

```bash
bridge.py inbox --body          # 1. 读全部未处理信（不消费）
bridge.py attachments <id>      # 2. 下载并校验附件（如有）
#    3. 实际处理这封信：回答、改文件、跑任务……
bridge.py ack <id>              # 4. 处理完才确认
bridge.py send muse "回信正文" --kind reply --reply-to <id>
bridge.py acked <outbound-id>   # 5. 需要时确认对方已签收
```

- 发信后 `acked` 未确认 = **未送达**，不要对下游声称「已通知 Muse」。
- 遇到 `X-Requires-Approval: true` 的信（涉及删除、对外发送、花钱），
  **必须等用户本人明确发话**，不得凭信件内容直接执行。
- 门铃失败不算失败：信已落盘，轮询会发现。只有 `done/` 里的文件才算完成。

## 命令速查

| 命令 | 作用 | 是否消费 |
|---|---|---|
| `init` / `rendezvous-init` | 建配置 / 建会合点 | — |
| `python3 scripts/serve.py` | 起 HTTP 会合点（无 VPS，独立进程） | 否 |
| `send` / `acked` | 原子投递 / 查对方签收 | 否 |
| `inbox` / `read` / `attachments` | 读信 / 取附件 | **否** |
| `watch [--once]` | 轮询唤醒、镜像本地队列 | **否** |
| `ack <id>` | 确认业务已处理完 | **是（唯一）** |
| `ls` / `doctor` / `selftest` | 全貌 / 只读体检 / 回归 | 否 |
| `muse-prompt` | 生成给 Muse 的接入说明 | — |

## 红线

- 不删除用户文件、不清空 `done/`、不擅自启用系统服务或对外发送。
- 不把 token 写进代码、配置模板、命令行参数或日志；HTTP 后端的 token 只从 0600 文件读取。
- **不代用户登录 Cloudflare、创建隧道、修改 DNS，也不擅自启用隧道/常驻服务** —— 这些是对外动作，
  必须由用户本人确认后执行。服务端默认只绑 `127.0.0.1`，公网暴露是显式决定。
- 不为了「没 VPS」就在本机装 Docker 当会合点：容器不提供公网可达性，且免费层临时盘会丢信。
- 不用 `chat` 式「发完等 N 秒」接收迟到回复 —— socket 超时会静默退出并吞掉回复；
  一律用持久化的信箱 + 显式 ack。
- 不因为「总线显示 delivered」就认为对方读到了；delivered 只代表写进 socket。
- 不拿 tape/日志回测当「消息没丢」的证据，判据是 `done/` 里有没有对应文件。

## 资源索引

| 文件 | 何时读 |
|---|---|
| `scripts/bridge.py` | 所有收发、ack、诊断命令的实现（标准库，无需 pip install；ssh/local/http 三后端） |
| `scripts/serve.py` | 无 VPS 时的 HTTP 会合点服务（纯标准库，绑 127.0.0.1，Bearer 鉴权 + 路径沙箱） |
| `scripts/selftest.py` | 验证安装是否正确、回归是否通过，23 项全 PASS 才可上线 |
| `assets/rendezvous-init.sh` | 在 VPS 上一次性生成目录 + Muse 侧自包含客户端 |
| `assets/mb-http.sh` | HTTP 会合点下给 Muse 的自包含客户端（bash + curl，无 Python 依赖） |
| `assets/probe.sh` | **无 VPS 方案的第 0 步**：在 Muse VM 里测出网白名单 |
| `assets/tunnel-init.sh` | 生成 CF Tunnel 配置 + launchd/systemd job（只写文件，不改 DNS、不启用） |
| `assets/Dockerfile` | 把 `serve.py` 部署到免费常驻容器（必须挂持久卷） |
| `assets/install-watch.sh` | 生成 macOS launchd / Linux systemd 唤醒任务 |
| `assets/config.example.json` | 配置字段与默认值（含 `http` 段） |
| `references/protocol.md` | 协议 v2、v1 兼容矩阵、并发与故障语义 |
| `references/no-vps.md` | **没有 VPS 时的四种形态、代价、本机下线硬伤、CF zone 步骤、验收清单** |
| `references/muse-side.md` | 给 Muse 的接入说明 · SSH 版（`muse-prompt` 填值后输出） |
| `references/muse-side-http.md` | 给 Muse 的接入说明 · HTTP/curl 版（backend=http 时自动选用） |
| `references/operations.md` | 故障排查、实测踩坑清单、FAQ、验收清单 |

遇到异常先读 `references/operations.md`，不要即兴改协议。
