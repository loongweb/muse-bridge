# 本机环境要求与布局（可复现，不含任何用户数据）

> 这份文档回答「要跑起来，本机需要什么」。**它描述的是环境规格，不是某一台机器的真实配置**。
> 真实 IP、域名、SSH 别名、token、身份文件和记忆都不在本仓库里，也不应该被提交进来。

## 1. 依赖（零第三方包）

| 依赖 | 最低版本 | 用途 | 检查 |
|---|---|---|---|
| Python | 3.9+ | `bridge.py` / `serve.py` / `selftest.py`，全部标准库 | `python3 --version` |
| bash | 3.2+（macOS 自带即可） | `assets/*.sh`、对端自包含客户端 | `bash --version` |
| curl | 任意现代版 | `http` backend 的出站请求、出网探测 | `curl --version` |
| ssh / scp | OpenSSH | `ssh` backend 的会合点读写 | `ssh -V` |
| sha256sum / shasum | — | 附件校验（脚本已做跨平台兼容） | `command -v shasum sha256sum` |

**可选**：`cloudflared`（命名隧道）、Docker（把 `serve.py` 部署到常驻容器）、
launchd（macOS）/ systemd（Linux）用于定时唤醒。

不需要 `pip install`，不需要 Node，不需要数据库，不需要常驻 daemon（除非你主动跑 `serve.py`）。

## 2. 目录布局

| 路径 | 谁创建 | 权限 | 内容 |
|---|---|---|---|
| `~/.workbuddy/skills/muse-bridge/` | 你 / `install.sh` | 755 | skill 本体（本仓库根目录即其内容） |
| `~/.config/muse-bridge/config.json` | `bridge.py init` 或手工 | **0600** | 本机真实配置（**不入库**） |
| `~/.config/muse-bridge/*-token` | 你手工生成 | **0600** | 总线 / HTTP token（**不入库**） |
| `~/.local/state/muse-bridge/` | 运行时自动 | 700 | watcher 的 `seen` 集合、下载暂存、轮转日志 |
| `<mail_root>/` | `rendezvous-init.sh` | 双方可写 | 会合点：`BRIDGE.json`、`<me>/`、`<peer>/`、各自的 `done/` 与 `attachments/` |

配置模板见 `assets/config.example.json` —— 里面全是占位值（`my-rendezvous`、`/srv/muse-bridge/mail`、
`bridge.example.com`），**照抄前必须改成你自己的**。

`BRIDGE.json` 是协议里单一 `mail_root` 的锚点：两端启动时都会拿自己的配置与它比对，
不一致直接报错。这条不变量专门治「双方各建各的路径、信永远送不到」这类事故。

## 3. 三种后端各自需要的本机条件

### `ssh`（推荐，可异步）
- 一台双方都能主动出网连到的 VPS，配好 SSH 密钥登录（建议 `~/.ssh/config` 里定义别名 + 一个备用别名）。
- 在 VPS 上跑一次 `assets/rendezvous-init.sh <mail_root> <me> <peer>`，
  它会生成目录、`BRIDGE.json`，以及**给对端的自包含客户端** `mb.sh` / `send.sh`。
  对端 Agent 的沙箱里没有本 skill 的文件，所以客户端必须放在会合点上、用 bash 就能调用。

### `local`（两端同机 / 自检）
- 只需一个本机目录，`local_root` 与 `mail_root` 指向它。自检全程走这个后端，流量不出网。

### `http`（无 VPS）
- 本机或常驻容器跑 `python3 scripts/serve.py`，默认只绑 `127.0.0.1`，
  需要公网可达时前置 Cloudflare Tunnel 或反向代理。
- token 放在 0600 文件，客户端用 `curl -K` 文件流读取，**不进命令行参数**（`ps` 可见）。
- 远程明文 `http://` 在配置层直接被拒绝，必须 HTTPS。
- ⚠️ 本机跑服务的硬伤：睡眠 → 隧道断 → 对端写不进新信。要真异步请用常驻容器 + **持久卷**。
- 第 0 步永远是在对端 Agent 的 VM 里跑 `assets/probe.sh`，确认它到底能出网访问哪些域名。

## 4. 唤醒（入站通知）

不要让 watcher 常驻长连接。`assets/install-watch.sh` 生成
launchd plist（macOS）或 systemd user unit（Linux），按 `StartInterval` 定时跑一次 `watch --once` 短进程：

- 没有连接状态机、不会起重复实例；
- Mac 睡眠唤醒后 launchd 自动补跑；
- `seen` 集合持久化，避免每次重复播报；
- **watcher 绝不 ack**，它只播报，处理完由你显式 `ack <id>`。

脚本只**生成**配置文件和运行手册，**不会**擅自 `launchctl load` / `systemctl enable` / 登录 CF / 改 DNS。

## 5. 从零验收清单

```bash
# 1 装好之后先跑自检（只用临时目录与本地端口）
cd ~/.workbuddy/skills/muse-bridge/scripts
python3 -m py_compile bridge.py serve.py selftest.py
MUSE_BRIDGE_STATE=$(mktemp -d) python3 selftest.py        # PASS=23 FAIL=0 TOTAL=23

# 2 生成配置、体检
python3 bridge.py init                                     # 或手工复制 config.example.json
python3 bridge.py --config ~/.config/muse-bridge/config.json doctor

# 3 生成给对端 Agent 的接入说明（自动按 backend 输出，且不含 token 值）
python3 bridge.py --config ~/.config/muse-bridge/config.json muse-prompt

# 4 端到端：发一封、对端读、对端 ack、本机查签收
python3 bridge.py --config <cfg> send "ping"
python3 bridge.py --config <cfg> acked <msg-id>
```

`doctor` 的 `FAIL` 必须清零再上线。它检查：凭据是否硬编码 / 配置文件权限 / 日志轮转 /
SSH 是否有备用 host / 信箱积压与最老信件 / 散落的孤儿附件 / 旧版「读即 ack」脚本 /
唤醒任务是否缺失 / `http` 模式下的 token 权限与健康检查可达性。

## 6. 边界：什么不属于「本机环境」

本仓库**不发布**下列内容，它们属于用户私有环境，发布即事故：

- 真实 IP、域名、SSH 别名、VPS 拓扑、token / 密钥；
- `SOUL.md` / `USER.md` / `IDENTITY.md` / 任何记忆与对话记录；
- 在产的 v1 老桥代码（其中含硬编码凭据，本项目只做只读体检、不修改）；
- 真实 `config.json`、`BRIDGE.json`、`*.token`、信箱里的实际信件。

`.gitignore` 已挡住常见文件名，但**推送前请自己再扫一遍**：
```bash
git grep -nE '(token|secret|password|passwd)\s*[:=]\s*["'\''][A-Za-z0-9_-]{16,}' || true
```
