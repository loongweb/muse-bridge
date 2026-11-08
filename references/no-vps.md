# 没有 VPS 时怎么搭会合点

定版：2026-11-06。适用：想把 WorkBuddy 与 Muse 互通、但不想（或不能）买 VPS 的用户。

## 先讲机制：Docker 不是答案，出站隧道才是

两端都是「只能主动出网、不能被连入」：Muse 在零入站端口的 Secure VM，WorkBuddy 在 NAT 后面的电脑上。
互通的**唯一**硬约束是：存在一个双方都能主动连出去的同一个地址。

- **Docker 只决定信箱跑在哪个进程里，不生产这个地址。** 容器跟着宿主走；宿主在 NAT 后面，容器就没有公网地址。
- **Cloudflare Tunnel 生产这个地址，而且不需要入站端口**：`cloudflared` 从本机**主动出站**与 CF 边缘建长连接，
  公网 hostname 的请求沿这条已建好的连接回送本机。路由器无需端口映射、无需公网 IP、无需 DDNS。
  用户自己的 CF zone 让 hostname **稳定不变**（`trycloudflare.com` 的临时域名每次重启都变，Muse 侧配置一次就不该再动）。

所以「在跑 WorkBuddy 的电脑上解决」**技术上成立**。但下面这条限制决定它是不是适合你。

## 关键限制：本机下线 = 对端写不进来

会合点不是被动存储，**双方都要往里写**。信箱在 WorkBuddy 本机时：

| 本机状态 | 已落盘的信 | Muse 能否发新信 | WorkBuddy 能否收 |
|---|---|---|---|
| 在线 | 完好 | 能 | 能 |
| 睡眠 / 关机 / 断网 | **完好，不会丢** | **不能**（隧道断了） | 不能 |
| 唤醒后（KeepAlive 重启 + 隧道重连） | 完好，自动可见 | 恢复 | 恢复 |

文件不丢，这是协议第一不变量的功劳；但**异步留言的窗口被关掉了** —— 而互通桥最值钱的恰恰是
「你睡觉时 Muse 把结果放进信箱，你醒来读」。

结论：

- 要**真正的异步**（Muse 随时能交付）→ 会合点必须常驻在云端。用 VPS，或用下面的「方案 B：免费常驻容器」。
- 只是**临时联调、双方都在线时协作、或需要让 Muse 访问本机资源** → 本机 + CF Tunnel 很合适，且零成本。

## 前置 gate：先测对面 VM 的出网能力（别跳过）

`assets/probe.sh` 是任何无 VPS 方案的**第 0 步**。没有这份数据就选方案，等于把整个客户端建在假设上。
在**对面 agent 自己的 VM 内**跑（只读 GET，零成本），把完整输出回给 WorkBuddy：

```bash
MB_EXTRA_DOMAINS='bridge.your-zone.com' MB_BRIDGE_URL='https://bridge.your-zone.com' \
  bash assets/probe.sh
```

⚠️ **不要经 VPS 代理去测** —— 那测的是 VPS 的出网，不是对面 VM 的，结论会反过来骗你。

### 一个真实样本（2026-11-07，某零入站 Secure VM，curl 8.5.0）

12 个候选中 **9 通 / 3 拦**，说明「无 VPS 会合点」这条路总体成立，但不是全通：

| REACH（9） | BLOCKED（3） |
|---|---|
| `cloudflare.com` 301 · `github.com` 200 · `api.github.com` 200 · `raw.githubusercontent.com` 301 · `workers.dev` 301 · `pages.dev` 301 · `trycloudflare.com` 403 · `onrender.com` 301 · `railway.app` 301 | `r2.cloudflarestorage.com` · `fly.dev` · `koyeb.app` |

读法与后果：

- `trycloudflare.com` 的 **403 是服务端有响应**（根路径拒绝），按探测规则计 REACH，不是被拦。
- `r2.cloudflarestorage.com` 不通 ⇒ **Workers+R2 方案里对面不能直连 R2，必须走 Worker 前置**，不能把 R2 当会合点直写。
- `fly.dev` / `koyeb.app` 不通 ⇒ 免费常驻容器只剩 `onrender` / `railway` 两条路可试。
- `github.com` + `api.github.com` 通 ⇒ 私有仓库做会合点可行；`workers.dev` 通 ⇒ CF 托管会合点可行。

**这是单台 VM 的样本，不是保证**：白名单按 VM / 地域 / 时段都可能不同。每个用户都要自己跑一次 `probe.sh`，
把输出作为选型的唯一依据。全 `BLOCKED` 时不要硬上，老实回到 VPS。

⚠️ 2026-09-29 那三份 `browser-use` / `muse.ai` 报告讲的是**我们访问 muse.ai 被服务商网关拦**，
方向与「对面 VM 出网」相反，不能拿来推断白名单。

## 四种形态（按推荐度）

| # | 会合点在哪 | 异步可用 | 花钱 | 当前 skill 支持 |
|---|---|---|---|---|
| **B** | 免费常驻容器 / PaaS（跑 `serve.py` + 持久卷） | ✅ | 0 | ✅ `backend=http` + `assets/Dockerfile` |
| **A** | WorkBuddy 本机 + CF Tunnel / 自己的 zone | ❌ 受在线时段限制 | 0（域名另算） | ✅ `backend=http` + `assets/tunnel-init.sh` |
| **C** | Cloudflare Workers + R2 / GitHub 私有仓库 | ✅ | 0 | ⚠️ 需自实现 `/v2/*` 七个端点，协议层不用改 |
| **D** | 两端同机 | ✅（同睡同醒） | 0 | ✅ `backend=local`，一条流量不出网 |

**方案 B 才是 VPS 的等价替代**：信箱常驻云端，本机只当客户端，跟现在的 VPS 模式行为完全一致。
`serve.py` 就是为此准备的 —— 纯标准库、零依赖，任何能跑 Python 的免费容器都能起。
Docker 在这里只是部署方式之一，**不在本机跑**，也就不占 Mac 的内存。

方案 C 说明：`HttpBackend` 只依赖 7 个 HTTP 端点（`health/list/read/write/mkdir/claim/move-tree` +
`stat`）。Workers/R2 或 GitHub Contents API 只要实现同一套语义即可复用全部客户端与协议，
但**本 skill 没有附这些实现**，不要当成已交付能力。

## 方案 A：本机 + Cloudflare Tunnel（完整步骤）

1. **生成 token**（一次即可，只写 0600 文件，绝不打印、绝不进命令行）：
   ```bash
   python3 scripts/serve.py --root ~/.local/share/muse-bridge/mail \
                            --token-file ~/.config/muse-bridge/http-token --init-token
   ```
2. **起本地服务**（默认只绑 `127.0.0.1:8787`）：
   ```bash
   python3 scripts/serve.py --root ~/.local/share/muse-bridge/mail \
                            --token-file ~/.config/muse-bridge/http-token
   curl -sS http://127.0.0.1:8787/v2/health      # {"ok":true,...}
   ```
3. **挂隧道**（二选一）：
   - 联调用 quick tunnel，不需要账号：`cloudflared tunnel --url http://127.0.0.1:8787`
     → 得到一个临时 `*.trycloudflare.com` 域名，重启会变。
   - 正式用自己 zone 的命名隧道：`bash assets/tunnel-init.sh bridge.your-zone.com 8787`
     → 只生成配置和 launchd/systemd job，**不登录、不建隧道、不改 DNS、不启用服务**。
     按脚本打印的 runbook 手工完成 `login` / `create` / `route dns`（DNS 是对外动作，必须本人确认）。
4. **本机配置**（`~/.config/muse-bridge/config.json`，0600）：
   ```json
   {
     "backend": "http",
     "http": {
       "base_url": "https://bridge.your-zone.com",
       "token_file": "~/.config/muse-bridge/http-token",
       "timeout": 20
     },
     "mail_root": "/srv/muse-bridge/mail"
   }
   ```
   `Config` 会拒绝「远程主机用明文 http」——Bearer token 不能裸奔。
5. **给 Muse 接入说明**：`python3 scripts/bridge.py muse-prompt` 会自动输出 **HTTP/curl 版**说明。
   token **另走一条安全渠道**单独给，绝不写进粘贴的聊天内容。
6. **验证**：`selftest` 全绿 → `doctor` 零 FAIL → 让 Muse 跑 `mb-http.sh doctor` 确认同一 `mail_root`。

### 安全边界

- 服务端默认 `127.0.0.1`；要绑 `0.0.0.0` 必须显式给 `--host` 且会打印警告。
- 公网 hostname 暴露的是「能读写信箱的 API」，Bearer token 是唯一闸门：0600、随机、不进 argv / 配置 / 日志 / 聊天。
  服务端另有路径沙箱（拒绝越出 `mail_root`、拒绝 `..` 与 symlink 逃逸）和请求体上限。
- CF Access 可作第二层，但**对非浏览器客户端不友好**：curl 需额外带 service token 的两个 header。
  做不到就靠强 Bearer token，不要把 Access 当成已生效的保护来自我安慰。
- `serve.py` 与 `bridge.py` 都不自动 ack。隧道断开、重连、服务重启都不消费信件。

## 方案 B：免费常驻容器（异步场景首选）

```bash
docker build -f assets/Dockerfile -t muse-bridge .
# 平台侧：挂持久卷到 /data，把 token 作为 secret 挂到 /run/secrets/mb_token，
#         HTTPS 由平台终结，把 config.http.base_url 指向该 origin。
```

- **持久卷是硬要求**：免费层的临时文件系统在容器重启后清空，会直接违反「文件管存、永不丢」。
  上线前务必做一次「写封信 → 重启容器 → 信还在」的验收。
- 本机完全不跑 Docker，也不必 24h 在线；Muse 和 WorkBuddy 都只是出站客户端。
- 若平台只给 HTTP 内部端口 + 外部 HTTPS，别把 `base_url` 写成 `http://` 远程地址，Config 会拒绝。

## 验收清单

- [ ] Muse 侧 `probe.sh` 输出至少有一个 `REACH`，且 `MB_BRIDGE_URL/v2/health` 返回 200
- [ ] 无 token 的 `/v2/list` 返回 401；带正确 token 返回 200
- [ ] `BRIDGE.json` 的 `mail_root` 与两端配置完全一致
- [ ] `selftest` 全 PASS（含 5 项 HTTP 回归）
- [ ] bash/curl 客户端发信 → Python 客户端读 + 附件 sha256 + ack → 对面看到 `done/`
- [ ] 隧道断 30s 再恢复，信仍在箱内且未被自动 ack
- [ ] **方案 B**：重启容器后信与附件都还在
- [ ] **方案 A**：明确记录并接受「本机睡眠期间 Muse 无法发信」这一限制
