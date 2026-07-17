# AGENTS.md — email-for-maimai 交接文档

> 给下一个接手的 **Agent / 人类维护者**。  
> 更新时间：2026-07-17  
> 仓库定位：MaiBot 插件「麦麦邮件」独立代码库（从生产/本地插件目录抽出）。

---

## 0. 30 秒结论

| 项 | 状态 |
|----|------|
| 插件是否写完并能跑 | **能**。生产 MaiBot 1.0.12 已加载 `email-for-maimai v0.1.0` |
| 真实 SMTP | 生产 Brevo 已验证能发到 QQ 邮箱 |
| 自助绑定 / 命令 / Tool | 已实现 |
| 调度问好 / 想念 | 已实现；有已知调度与查重坑 |
| 内容增强（人设+记忆+intent prompt） | **代码已合入**；生产可用性依赖配置与 Host 能力 |
| 邮箱验证码 / 复杂地址簿 | **没做**（刻意延后） |
| SQLite 存储 | **没做**（JSON MVP + `store.py` 抽象） |
| 独立 CI / 插件商店上架 | **没做** |

**你最该先看的三个文件：** `plugin.py`、`pipeline.py`、`store.py`。  
**生产数据不在 git 里：** `data/plugins/email-for-maimai/store.json`。

---

## 1. 仓库与路径

### 本仓库（开发）

```text
/mnt/data/email-for-maimai/          # 独立 git 仓库（本目录）
```

历史开发副本也曾存在于：

```text
/mnt/data/MaiBot/plugins/email-for-maimai/
```

两者内容同源；**以本独立仓库为对外源码真相**，改完再 rsync 到生产。

### 生产（用户服务器）

| 用途 | 绝对路径 |
|------|----------|
| MaiBot 根 | `/root/MaiBot` |
| 插件代码 | `/root/MaiBot/plugins/email-for-maimai/` |
| 插件配置 | `/root/MaiBot/plugins/email-for-maimai/config.toml` |
| **持久数据** | `/root/MaiBot/data/plugins/email-for-maimai/store.json` |
| 临时目录 | `/root/MaiBot/temp/plugins/email-for-maimai/` |
| Python | `/root/venv-v1.0.0/bin/python` |
| systemd | `maibot.service` |
| 主机 | `root@106.52.186.122`（Tencent Cloud，约 3.6G RAM） |

### 路径是怎么定的

SDK 注入 `PluginContext.paths`：

- `data_dir` → `MaiBot/data/plugins/<plugin_id>/`（持久）
- `runtime_dir` → `MaiBot/temp/plugins/<plugin_id>/`（临时）

`plugin.py`：

```python
self._store = JsonStore(self.ctx.paths.data_dir)
# → data_dir/store.json
```

用户自定义模板可放：`data_dir/templates/`，覆盖插件内置 `templates/`。

---

## 2. 已经干了什么（Done）

### 2.1 产品与架构

- [x] 插件骨架：`MaiBotPlugin` + `create_plugin()` + `_manifest.json` v2  
- [x] 生命周期：`on_load` / `on_unload` / `on_config_update`  
- [x] 后台调度：Runner 内 `asyncio` 队列 + Worker 池（**不是** Host `async_task_manager`）  
- [x] JSON 存储 `JsonStore`：bindings / preferences / send_logs，原子写  
- [x] 邮件管线 `EmailPipeline`：绑定→频控→上下文→记忆→作文→查重→渲染→SMTP→日志→死信  
- [x] SMTP：`aiosmtplib`，Brevo STARTTLS(587)，失败分类（永久/瞬时）+ 瞬时退避  
- [x] Jinja2 模板：`default` / `greeting` / `miss`（html + txt）  
- [x] 10 个中文命令 + 2 个 Tool + 1 HomeCard  
- [x] 用户自助绑定邮箱（非管理员维护通讯录）  
- [x] 管理员 `/查邮箱`；非管理员静默不提示权限  
- [x] 命令 `intercept` 防 Maisaka 双回复  
- [x] 本地 pytest（逻辑 / store / pipeline 等，历史约 40+ 测例）  

### 2.2 内容增强（相对最初 MVP）

- [x] `memory_retriever.py`：`knowledge.search`（A-Memorix）  
- [x] `persona_loader.py`：`config.get("personality.personality")` / `reply_style`，可截断缓存  
- [x] `composer.py` intent 分型 prompt（greeting/miss/blessing/chat）  
- [x] 主题模板用 bot 昵称：`config.get("bot.nickname")` 等，避免写死「麦麦」  
- [x] `context_builder`：全局回退时保留相邻 bot 回复，避免只剩用户独白  
- [x] `_manifest` capabilities 含：`config.get`、`knowledge.search` 等  

### 2.3 生产落地与踩坑修复

- [x] 部署到生产 plugins 目录，装 `aiosmtplib` / `jinja2`  
- [x] Host 不自动加插件路径 → `plugin.py` 顶部 `sys.path.insert`  
- [x] 去掉/避免 Host 不认的 manifest `icon`  
- [x] **根因修复：生产 `llm.model` 从空串改为 `utils`**  
  - 空串 → `resolve_task_name` → 字典序首任务 `embedding` → bge-m3 被当 chat 调 → 失败 → 单薄 fallback  
- [x] Brevo 真实发送验证（发件人需用已验证域名邮箱，如 `xiaoyue@...`）  
- [x] miss 到期逻辑修过：`last_success` 作周期锚，`last_attempt` 作 24h 防抖（见 `utils.is_miss_due`）  

### 2.4 周边（同会话但非本插件代码）

这些**不属于本仓库 diff**，但影响「邮件是否像活的 / 机器人是否说话」：

- 生产 MaiBot 0→1.0.11→1.0.12 升级与 DB 迁移急救  
- NapCat QQ 掉线扫码重登  
- 表情包污染标签清洗、注册审核脚本、内存清理与 maibot 重启  
- 磁盘 `prompt_imgs` / 日志清理  

接手 agent **不要**把这些误当成邮件插件未提交代码。

---

## 3. 什么没干（Not Done / Won't for now）

| 项 | 状态 | 说明 |
|----|------|------|
| 邮箱验证（验证码确认归属） | 未做 | 用户明确先跳过，后续迭代 |
| 管理员代绑通讯录 | 不做 | 产品定为用户自助绑定 |
| SQLite / `ctx.db` 存储 | 未做 | 仅 JSON；`store.py` 留了替换面 |
| 插件市场 / plugin-repo 上架 CI | 未做 | 有社区 registry 规范可对，但未提交 |
| 完善英文 i18n | 未做 | manifest 仅 zh-CN |
| 单元测试覆盖记忆/人设新路径 | 偏弱 | 早期测试偏 utils/store/pipeline 主干 |
| 彻底解决 greeting 日级 attempt 挡重试 | **未闭环** | 见 §5 |
| 彻底解决模板内容查重误杀 | **未闭环** | 见 §5 |
| 自动从生产回写 config 到 git | 禁止 | 密钥与环境相关，只用 `config.example.toml` |
| 多发件人 / 附件 / 日历 | 未做 | 范围外 |
| Host 侧 embedding 空 model 兜底修复 | 未做 | 属 MaiBot 核心，插件侧用显式 task 规避 |

---

## 4. 现在要干什么（Recommended Next）

按优先级：

### P0 — 正确性 / 生产稳定

1. **确认生产 `config.toml`**  
   - `[llm].model` 必须是文本任务（如 `utils`），不能 `""`  
   - `sender_email` / `sender_name` / SMTP 密钥正确  
   - **rsync 插件时务必 `--exclude config.toml`**（曾发生本地示例覆盖生产发件人）  
2. **修 greeting 调度语义**（建议）  
   - `is_greeting_due` 当前偏 `last_attempt` 日去重：失败/查重跳过会吞掉整天  
   - 更合理：成功用 `last_success` 定「今天已问好」；跳过写入可区分 reason，允许瞬时失败重试或次日补偿  
3. **查重策略**  
   - greeting/miss 对模板化短文本过严会 `内容与近期邮件过于相似，跳过`  
   - 可：提高阈值阈值、排除模板壳只比 LLM 正文、或 greeting 仅对 success 日志去重  

### P1 — 体验

4. 把 `plan.md` 里仍未勾选的体验项扫尾（若代码已做，把 plan 勾选状态与实现同步，避免误导）  
5. 给记忆检索加更好的 query（结合最近话题，而不是固定模板）  
6. 死信 QQ 通知路径回归：`chat.open_session` 建私聊流是否仍稳定  

### P2 — 工程化

7. 补测试：persona 截断、memory 空结果、greeting due 边界、dedup  
8. 可选：`store` SQLite 实现，接口保持 `JsonStore` 同语义  
9. 可选：邮箱验证码绑定  
10. 提交到 Mai-with-u/plugin-repo 规范（manifest 校验）  

### 运维提醒（生产机）

- 机器 **3.6G RAM**，`bot.py` 跑久了常涨到 2G+，swap 易满；与邮件插件无关但会导致「整机像死了」  
- QQ 掉线是 NapCat/账号问题，不是邮件插件  
- 备份优先拷：`store.json` + 生产 `config.toml`（离库私存）  

---

## 5. 已知 Bug / 设计债（详）

### 5.1 `llm.model=""` → embedding

- Host：`resolve_task_name("")` → `next(iter(models.keys()))`  
- 生产任务字典序第一个常为 `embedding`  
- 症状：邮件长期单薄 fallback；`llm_request` 里看到 bge-m3 + chat messages  
- 处理：配置写死文本 task；文档与 example 已强调  

### 5.2 greeting due + attempt

- `utils.is_greeting_due`：用 attempt 做「当天已试过」  
- pipeline 查重 skip / 组合失败若仍记 attempt，则当天不再试  
- store 主要记 success，排障要看 MaiBot 主日志  

### 5.3 内容查重

- `content_is_similar` 用于 greeting/miss  
- 模板化开头/结尾会导致误杀  

### 5.4 `message.build_readable`

- 部分 Host 期望 SessionMessage 对象，SDK 却给 dict  
- `context_builder` 已 try/fallback 手动抽 `processed_plain_text` / raw  

### 5.5 部署覆盖配置

- 禁止把开发机 `config.toml` 同步到生产  
- 仓库只保留 `config.example.toml`  

### 5.6 sys.path

- 生产 Host 需要插件目录自插入 `sys.path`；保留 `plugin.py` 头部逻辑  

---

## 6. 模块职责速查

| 文件 | 职责 | 改动风险 |
|------|------|----------|
| `plugin.py` | 注册命令/Tool、生命周期、装配依赖 | 高：入口 |
| `pipeline.py` | 单封邮件状态机 | 高：行为核心 |
| `scheduler.py` | 扫描、队列、worker、停止 | 中高 |
| `store.py` | 持久化契约 | 中高：迁移要兼容 |
| `composer.py` | prompt / LLM 正文 | 中：影响文风 |
| `context_builder.py` | 聊天素材 | 中 |
| `memory_retriever.py` | 长期记忆 | 中 |
| `persona_loader.py` | 人设缓存 | 低中 |
| `mailer.py` | SMTP | 中：投递 |
| `renderer.py` | 模板 | 低 |
| `utils.py` | due/查重/校验 | 中高：调度语义 |
| `config_model.py` | WebUI 字段 | 中：兼容默认值 |
| `_manifest.json` | 权限白名单 | 高：漏 cap 会 E_CAPABILITY_DENIED |

### Manifest capabilities（当前）

```text
send.text
chat.get_stream_by_user_id
chat.open_session
message.get_recent
message.get_by_time
message.get_by_time_in_chat
message.build_readable
person.get_id
person.get_value
llm.generate
config.get
knowledge.search
```

新增任何 `ctx.xxx` 调用前，**必须**先加 capability，否则 Host 直接拒绝。

---

## 7. 配置字段分组（config_model）

- `plugin` / `smtp` / `schedule` / `format`  
- `llm`（model, temperature, max_tokens, inject_persona, persona_max_chars）  
- `context` / `memory` / `safety` / `retry` / `admin` / `prompt`  

热更新：插件自身配置走 `on_config_update`；`scheduler.update_config` 需存在（已补）。  
`config_reload_subscriptions`：若要监听 bot/model 全局变更需显式声明（当前以 self 配置为主）。

---

## 8. 常用运维命令

```bash
# 服务
systemctl status maibot.service
systemctl restart maibot.service
journalctl -u maibot.service -n 100 --no-pager

# 插件是否加载
grep -E 'email-for-maimai|邮件' /root/MaiBot/logs/maibot.stdout.log | tail

# 数据
python3 -m json.tool /root/MaiBot/data/plugins/email-for-maimai/store.json | less

# 同步代码（示例：排除配置与缓存）
rsync -av --delete \
  --exclude config.toml --exclude __pycache__ --exclude .pytest_cache --exclude .git \
  ./email-for-maimai/ root@SERVER:/root/MaiBot/plugins/email-for-maimai/
```

---

## 9. 测试与验收

```bash
pytest tests/ -q
```

生产验收：

1. 加载无 error，scheduler workers=配置值  
2. 绑定→设问候→手动发邮件成功  
3. `store.json` send_logs 有 `success: true`  
4. 主日志无 bge-m3 chat 误用  
5. 主题/页脚为当前 bot 昵称而非写死「麦麦/MaiBot」  

---

## 10. 安全

- **永远不要**把生产 `config.toml`、Brevo API Key、SMTP 密码提交进 git  
- 对话历史中曾暴露过 SMTP 密钥：若仓库或日志外泄，**轮换 Brevo 密钥**  
- `admin_qq_list` 控制查他人邮箱；默认应收敛  
- WebUI / 主机暴露面是运维问题，不在本插件范围，但生产曾被扫描，注意 IP 限制  

---

## 11. 给下一个 Agent 的工作纪律

1. **先读本文件 + README + `pipeline.py`/`utils.py` due 逻辑**，再改调度  
2. 改 capability 同步改 `_manifest.json`  
3. 不靠猜 Host API：查本机 SDK / MaiBot `src/plugin_runtime`  
4. 部署排除 `config.toml`  
5. 3.6G 机器上禁止高并发 VLM/重任务与邮件联调同时炸内存  
6. 业务缺字段时失败要暴露，不要用「假成功」兜底掩盖  
7. 用户（哥哥）交互语气若走小岳人设，是会话规范，不是插件代码要求  

---

## 12. 变更日志（人类可读摘要）

| 阶段 | 内容 |
|------|------|
| MVP | 命令/Tool/调度/JSON/SMTP/模板 |
| 兼容 | sys.path、manifest、Brevo sender |
| 排障 | llm.model 空串→embedding 根因 |
| 增强 | memory / persona / intent prompt / 上下文保留 bot 回复 |
| 文档化 | 独立仓库 README + 本交接文档 + config.example |

---

## 13. 联系与归属

- 作者字段：manifest `hongyue`  
- 运行主人：生产 MaiBot「鸿小岳」环境  
- 问题排查顺序：配置 → store.json → maibot.stdout → SMTP 服务商后台  

**一句话交给下一任：**  
插件主体已在生产跑通；优先盯 `llm.model`、部署别覆盖 config、以及 greeting 的 attempt/查重误杀；数据在 `data/plugins/email-for-maimai/store.json`，代码在本仓库。
