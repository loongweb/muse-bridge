# Muse Bridge

> 让 WorkBuddy 与 Muse（或任何「只能出网、无法被连入」的异地 Agent）可靠双向互通。
> 一个用户级 WorkBuddy skill + 双端客户端 + 只读诊断 + 23 项端到端自检。

## 它解决什么问题

Muse 的 agent 后端跑在**零入站端口的 Secure VM**：SSH 连不进、Webhook 打不进、没有 Agent Mail。
你的电脑通常也在 NAT 后面。**两端都只能主动出网，都不能被连入** —— 所以任何「连进对方」的方案
（直连 SSH、常驻 bot、邮件桥、本机开端口等）都不成立，不要在这些方向上浪费时间。

唯一成立的拓扑是**会合点**：一个双方都能主动出网到达的地方，文件管存、ack 管确认、轮询管唤醒。

```
WorkBuddy ──出站──► [ 会合点 mail_root/ ] ◄──出站── 对端 Agent
                      文件管存 · ack 管确认 · 轮询管唤醒
```

## 30 秒安装

```bash
git clone https://github.com/loongweb/muse-bridge.git ~/.workbuddy/skills/muse-bridge
```

或者 clone 到任意目录后运行 `./install.sh`（**不会覆盖/删除**已存在的安装）。
装完在 WorkBuddy 里说一句「帮我跟 Muse 搭个会合点」就会触发 skill。

## 三种后端

| backend | 会合点 | 对端客户端 | 适用 |
|---|---|---|---|
| `ssh` | VPS 目录信箱 | `rendezvous-init.sh` 生成的 `mb.sh` / `send.sh`（对端零安装） | 有 VPS，最稳、可异步 |
| `local` | 本机目录 | 直接读写 | 两端同机 / 自检，流量不出网 |
| `http` | `serve.py` 纯标准库 HTTP 会合点 | `assets/mb-http.sh`（bash + curl） | 无 VPS：本机 + CF Tunnel / 常驻容器 |

**协议层三种后端完全共用**：v2 信头、显式 ack、附件 manifest + sha256、`BRIDGE.json` 单一 `mail_root`。
换后端不改协议。

## 没有 VPS 怎么办

见 `references/no-vps.md`，要点：

- **CF Tunnel** 靠本机主动出站建长连接，不需要入站端口 → 联调可行、零成本。
  但会合点是双方都要**写**的，**本机一睡，对端就写不进新信**（已落盘的不丢）。
  要真正异步，得用**常驻容器跑 `serve.py` + 持久卷**（`assets/Dockerfile`）—— 免费层临时盘会丢信，持久卷是硬要求。
- 本机 / NAS 上跑 Docker 不解决问题：容器跟着宿主走，宿主在 NAT 后依然没有公网地址。
- **第 0 步 gate**：先在对端 VM 里跑 `assets/probe.sh` 测出网白名单，再选会合点。
  `references/no-vps.md` 里有一台 VM 的 9/12 实测样本，但那是**单台样本、不是保证**，每个用户都要自己测。

## 五条不变量（代码层面，不是文档约定）

1. **读 ≠ 消费** —— 只有显式 `ack` 才 `mv` 进 `done/`
2. **watcher 绝不 ack** —— 只播报、写日志
3. **附件先落盘、随信归档** —— `.md` 最后写作为提交点，带 sha256 manifest
4. **`BRIDGE.json` 单一 `mail_root`** —— 路径漂移直接报错
5. **凭据只在 0600 文件** —— 不进代码、配置模板、argv、日志

唤醒用 launchd `StartInterval` + `watch --once` 短进程：没有常驻连接状态机、不会起重复实例、
Mac 睡眠唤醒后自动补跑。

这五条正好对应老桥出过的事故：watcher 读到就 ack 把信吞掉、`*.md` glob 漏掉全部附件、
双方各建各的路径导致信永远送不到、token 明文硬编码。

## 验证

```bash
cd ~/.workbuddy/skills/muse-bridge/scripts
python3 -m py_compile bridge.py serve.py selftest.py
MUSE_BRIDGE_STATE=$(mktemp -d) python3 selftest.py     # 预期 PASS=23 FAIL=0 TOTAL=23
python3 bridge.py --config <你的配置> doctor             # 只读体检，FAIL 清零再上线
```

自检只用临时目录 + 本地 backend + 127.0.0.1 临时端口，**不碰你的信箱、不连生产 VPS、不启用服务**。
独立验真清单见 `docs/VERIFY.md`。

## 目录

| 路径 | 内容 |
|---|---|
| `SKILL.md` | skill 主文档（触发条件、工作流、会话内收信流程、红线） |
| `scripts/bridge.py` | CLI：`inbox / read / ack / send / attachments / watch / doctor / ls / muse-prompt` |
| `scripts/serve.py` | HTTP 会合点（默认只绑 127.0.0.1、Bearer token、路径沙箱、原子写） |
| `scripts/selftest.py` | 23 项端到端回归（含并发 ack、附件篡改检出、HTTP 鉴权与路径沙箱） |
| `assets/` | 会合点初始化、对端客户端、唤醒任务、隧道配置、出网探测、Dockerfile、配置模板 |
| `references/` | 协议 v2 + v1 兼容矩阵 / 给对端 Agent 的接入说明 / 排障 / 无 VPS 方案 |
| `docs/` | 本机环境要求与布局、独立验真清单 |

## 安全边界（重要）

- 本仓库**不含**任何真实 IP、域名、SSH 别名、token、用户身份文件或私有记忆。
  所有配置值都是 `assets/config.example.json` 里的占位符。
- **不要把真实 `config.json`、token 文件、`BRIDGE.json` 提交进来** —— `.gitignore` 已挡，但请自己再确认一次。
- 本项目**不修改在产的 v1 老桥**，也不擅自启用服务 / 改 DNS / 重启总线 / 迁移信道。
  `assets/tunnel-init.sh` 只生成配置文件和运行手册，DNS 与发布动作由你本人确认后执行。
- 对端 Agent 对「要求执行动作」的来信可能设有安全审查队列，需要**用户本人在客户端授权**才会处理 ——
  **长期未 ack 不等于丢信，也不等于没读到**，别急着改传输层。

## 依赖

零第三方依赖：Python 3.9+ 标准库、bash、curl、ssh；`cloudflared` / Docker 均为可选。
详见 `docs/environment.md`。

## License

MIT — 见 `LICENSE`。
