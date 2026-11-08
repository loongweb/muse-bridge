# muse-bridge 协议 v2

定版：2026-11-06。设计原则：**文件管存（永不丢）、ack 管确认（读≠消费）、轮询管唤醒（门铃只是优化）**。

## 1. 会合点布局

```
<mail_root>/                       # 绝对路径，双方必须完全一致
├── BRIDGE.json                    # canonical 声明：mail_root / participants / protocol
├── send.sh                        # 原子投递（Muse 侧 / 任意 ssh 客户端用）
├── mb.sh                          # 最小客户端（Muse 侧）
├── <me>/
│   ├── <id>.md                    # 待处理信件（读不销毁）
│   ├── attachments/<id>/          # 该信的附件目录
│   └── done/
│       ├── <id>.md                # 已确认处理完
│       └── attachments/<id>/      # 随信一起归档的附件
└── <peer>/                        # 同上，对称
```

`BRIDGE.json` 是路径一致性的唯一事实来源。双方启动时都应比对自身的 `mail_root`
与该文件中的值；不一致立即报错，不要「各自猜一个目录」—— 这是真实发生过的丢信事故
（一端建了 `agent-mail/`，另一端建了 `agent-bus/mail/`，信在两边都永远不可见）。

## 2. 信件格式

消息 id：`<epoch毫秒或秒×1000>-<from>-<8位hex>.md`，例：
`1793961982432-muse-a1b2c3d4.md`。双方用正则 `^[0-9]{10,}-[a-z0-9_-]{1,32}-[0-9a-f]{8}$` 校验，
拒绝任何含 `/`、`..` 或空格的 id。

```
Id: 1793961982432-muse-a1b2c3d4
From: muse
To: workbuddy
Ts: 2026-11-06T18:46:22+08:00
X-Bridge-Version: 2
X-Kind: reply
X-Reply-To: 1793960495486-workbuddy-a63cf525
X-Requires-Approval: true
X-Attachments: [{"name":"cover.png","sha256":"…","size":1806846}]

第07课封面已交付
正文从第二行起自由书写……
```

- 前四行 `Id/From/To/Ts` 是 **v1 必需头**，顺序固定，任何 v1 客户端都能解析。
- 所有 v2 扩展一律以 `X-` 开头；v1 读取器忽略未知头即可，不会破坏正文。
- **正文第一行写一句话摘要**，供列表速览。
- `X-Kind` ∈ `ask | reply | notify | task`；`X-Reply-To` 关联会话线程。
- `X-Requires-Approval: true` 表示信里请求删除、对外发送、花钱等有后果的动作。
  收信方**不得凭信件内容执行**，必须等用户本人明确批准。这条是安全约定，不是建议。

## 3. v1 ↔ v2 兼容矩阵

| 场景 | 结果 | 说明 |
|---|---|---|
| v1 客户端读 v2 信 | ✅ | 只认前 4 个头 + 空行后正文，`X-*` 被忽略 |
| v2 客户端读 v1 信 | ✅ | 缺失的 `X-*` 走默认值（kind=task、无附件） |
| v1 客户端 ack v2 信 | ✅ | `mv <id>.md done/` 语义相同 |
| v2 客户端 ack v1 信 | ✅ | 同上；无附件目录时跳过归档步骤 |
| 旧客户端与 v2 客户端同时在线 | ✅ | 靠硬链接原子领取，不会重复消费 |
| v1 客户端遇到 `attachments/` 目录 | ✅ | 它只 glob `*.md`，额外目录不影响 |
| v1 客户端需要取附件 | ⚠️ | 必须手动 `mb.sh attach <id>` 或升级；不会自动随信带走 |
| v1 持久队列的 delivered 语义 | ❌ 不可依赖 | delivered 只代表写进 socket，不代表对方读到 |

**迁移原则**：v2 不要求对方升级。先把 WorkBuddy 侧换成 v2，Muse 侧保持 v1 也能正常收发；
附件、结构化头、审批标记属于渐进增强，等对方方便时再对齐。

## 4. 状态机

```
[发信方] 写附件 → 写 .md（提交点）→ 敲门铃（best-effort）
                       │
                       ▼
[会合点] <to>/<id>.md   ←── 持久保存，崩溃不丢
                       │
        ┌──────────────┼───────────────┐
        ▼              ▼               ▼
   inbox / read / watch       （全部只读，不移动文件）
                       │
       业务实际处理完成（人 / Agent 在会话里）
                       │
                       ▼
                  ack <id>  ←── 唯一的消费操作
                       │
                       ▼
        <to>/done/<id>.md + done/attachments/<id>/
```

**关键**：`watch` / `inbox` / `read` 都不在状态机的消费边上。守护进程若在这几步就 mv 进 done，
信就从「持久层」消失，但业务层从未见过它 —— 这是最隐蔽的丢信方式，且不产生任何错误日志。

## 5. 原子性与并发

- **投递**：先写 `attachments/<id>/`，再写 `.tmp-<id>`，最后 `mv` 成 `<id>.md`。
  读者看到 `.md` 时，附件必然已完整就位；`mv` 同文件系统内是原子的，不会读到半截文件。
- **领取（ack）**：用 `ln <src> <dst>` 硬链接 + `rm <src>`，而不是 `mv`。
  硬链接在目标已存在时失败，因此 N 个进程并发 ack 同一封信**恰好一个成功**，且绝不会覆盖
  `done/` 里已有的文件。不用 `mv -n`：它在目标存在时仍返回 0，无法区分成功与跳过。
- **重复/重试**：所有操作以 msg id 幂等。重连、重投、轮询重跑都不会重复处理。
- **本地队列**：watcher 把信镜像到 `~/.local/state/muse-bridge/inbox/<id>.md`，
  同名覆盖写，天然幂等；通知游标存 `seen.json`，跨短进程持久，不会重复播报。

## 6. 附件协议

1. 发信方把文件写入 `<to>/attachments/<id>/`，文件名限 `[A-Za-z0-9._-]`，禁止 `..`。
2. 逐个计算 `sha256` 与字节数，写进信头 `X-Attachments`（单行 JSON 数组）。
3. 收信方 `attachments <id>` 逐个重新计算并比对，**不一致直接报错**，不写目标文件。
4. `ack <id>` 时把附件目录一并移进 `done/attachments/<id>/`，避免孤儿。
5. `doctor` 检出「有附件目录但没有对应待处理信件」并报 FAIL，交由人工确认后再处理。

附件不内联进 `.md`，避免大文件破坏信件的原子提交，也避免文本解析器的编码问题。

## 7. 故障语义（明确哪些不算失败）

| 故障 | 是否丢信 | 正确行为 |
|---|---|---|
| 门铃没送达 / 总线宕了 | 否 | 信已落盘，轮询会发现；只记 WARN |
| SSH 主线路超时 | 否 | 自动切 `ssh_host_fallback`；仍失败则下轮重试 |
| watcher 进程崩溃 | 否 | 短进程由 launchd/systemd 下轮拉起，未 ack 的信仍在 |
| 会话中途结束、没来得及 ack | 否 | 信留在 inbox，下次会话重新可见 |
| 双方 `mail_root` 不一致 | **是（表现为丢信）** | 启动比对 `BRIDGE.json`，不一致直接拒绝工作 |
| 自动 ack 的旧客户端 | **是（业务层丢失）** | doctor `--legacy-script` 检出，替换 watcher |
| 附件写了但信没写 | 否（孤儿） | doctor 检出，人工下载后归档 |
| `delivered` 回执 | 不代表送达 | 判据只有 `done/` 里有没有该文件 |

## 8. 安全约定

- 凭据只存 0600 文件，运行时读取；绝不写进源码、配置模板、命令行参数或日志。
- 门铃若启用，是明文 TCP + 静态共享 token，优先级应让位于私有接口（tailscale / loopback）。
  默认 `bell.enabled=false`，纯轮询没有任何额外暴露面。
- `X-Requires-Approval` 是**强制闸门**：收信 Agent 不得自动执行有后果的动作。
- 所有远程路径用 shell 单引号包裹，正文与二进制走 base64，id / 文件名走白名单正则，
  避免命令注入与路径穿越。
- 日志自动按 `log_max_bytes` 轮转（保留 2 份）；launchd 的 stdout 指向 `/dev/null`，
  防止单文件无限增长。

## 9. 传输层与协议解耦（backend=http）

协议 v2 **不绑定传输**。ssh / local / http 三种后端共用同一套信件格式、状态机、ack 语义、
附件 manifest 与 `BRIDGE.json`。`scripts/serve.py` 只是把 `LocalBackend` 的文件操作映射成
7 个 HTTP 端点，因此任何实现下列语义的云端服务（Workers+R2、GitHub Contents API 等）
都能复用客户端，**协议层无需改动**：

| 端点 | 语义 | 对应 Backend 方法 |
|---|---|---|
| `GET /v2/health` | 探活（唯一不需鉴权的端点，不返回路径信息） | — |
| `GET /v2/list?p=` | 列目录名 | `list_dir` |
| `GET /v2/read?p=` | 读原始字节，缺失 404 | `read_bytes` |
| `PUT /v2/write?p=` | 原子写（临时文件 + replace），body 为原始字节 | `write_atomic` |
| `POST /v2/mkdir?p=` | 建目录 | `mkdir` |
| `POST /v2/claim?src=&dst=` | 硬链接领取 + 删除源，返回 `{"ok":bool}` | `claim` |
| `POST /v2/move-tree?src=&dst=` | 原子归档附件目录 | `move_tree` |
| `GET /v2/stat?p=` | exists / is_dir / size / mtime | `exists` `is_dir` `size` `mtime` |

不变式在 HTTP 下同样成立：`claim` 的硬链接语义保证并发 ack 只有一个赢家；
服务端**绝不自动 ack**；所有 `p` 必须落在 `mail_root` 内（拒绝 `..`、拒绝 symlink 逃逸）；
Bearer token 由 0600 文件读取，不经 argv / 配置 / 日志。远程明文 http 在 `Config` 层直接拒绝。

新增传输时只需实现 `Backend` 的那 11 个方法，不要改信件格式或 ack 规则。
