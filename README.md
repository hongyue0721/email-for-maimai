# 麦麦邮件 (email-for-maimai)

MaiBot 插件：通过配置的 SMTP（Brevo / QQ / 163 / Gmail / Outlook / custom），按日程或自然语言触发，给**自助绑定邮箱**的用户发送问候 / 想念 / 祝福 / 闲聊邮件。

- 插件 ID：`email-for-maimai`
- 版本：`0.1.0`
- SDK：`maibot-plugin-sdk` / `maibot_sdk` **2.7.x**（manifest 兼容 2.x）
- 依赖：`aiosmtplib>=3.0.0`、`jinja2>=3.1.0`
- 许可证：MIT

> 接手开发请先读 **[AGENTS.md](./AGENTS.md)**（给下一个 agent / 人类维护者的完整交接）。

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 每日问好 | 用户设定本地时间（如 `09:00`），到点基于最近互动写问候邮件 |
| 周期想念 | 每 N 天一封，锚定上次**成功**发送时间 |
| 自然语言 | 对麦麦说「给我发封邮件」，Planner 可调 `send_email_to_user` Tool |
| 手动命令 | `/发邮件 <内容>` 立刻发给自己 |
| HTML 模板 | LLM 只产纯文本；Jinja2 填入 `.html/.txt` 模板 |
| 人设注入 | 可选读取 `bot_config.toml` 的 `personality.*` |
| 长期记忆 | 可选 `knowledge.search`（A-Memorix）补充素材 |
| 安全 | 每日上限、最小间隔、内容查重、死信暂停 |
| 管理 | `/查邮箱 <QQ>` 仅管理员；非管理员静默放行 |

---

## 安装

1. 将本目录放到 MaiBot 的 `plugins/email-for-maimai/`  
   （生产常见路径：`/root/MaiBot/plugins/email-for-maimai/`）
2. 安装依赖到 MaiBot 使用的 venv：
   ```bash
   pip install 'aiosmtplib>=3.0.0' 'jinja2>=3.1.0'
   ```
3. 复制配置：
   ```bash
   cp config.example.toml config.toml
   # 编辑 SMTP / llm.model / admin_qq_list 等
   ```
4. 重启 MaiBot 或热加载插件。

### 生产 Host 兼容注意

部分 Host（如 1.0.11/1.0.12）插件目录**不会**自动进 `sys.path`。本仓库 `plugin.py` 开头已做：

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
```

若你 fork 后删除了这段，扁平 import（`from store import ...`）会失败。

manifest **不要**写 Host 校验不认识的字段（旧 Host 曾拒 `icon`）。

---

## 配置

完整示例见 [`config.example.toml`](./config.example.toml)。关键项：

### SMTP

- `preset`：`brevo` / `qq` / `163` / `gmail` / `outlook` / `custom`
- Brevo：`port=587` + `security=starttls`
- **`sender_email` 必须是服务商已验证发件人**，不是 SMTP 登录名（中继类服务尤其如此）
- `password` 是 SMTP 密钥 / 授权码，不是网页登录密码

### LLM（极易踩坑）

```toml
[llm]
model = "utils"   # 必须是 model_task_config 里的文本任务名
```

**禁止长期留空。**  
在 MaiBot Host 上，`llm.generate(model="")` 会经 `resolve_task_name("")` 落到任务表**字典序第一个**任务；生产上常见是 `embedding`（bge-m3），导致聊天补全 400「Model does not exist」，邮件退回单薄兜底文案。

推荐：`utils` / `replyer` 等明确的文本生成任务。

### 调度

- `scan_interval`：扫描间隔（秒），默认 3600
- `max_workers`：1–3，默认 2
- `timezone`：默认 `Asia/Shanghai`（业务本地时间）
- 调度尝试时间戳按 UTC 持久化

### 记忆 / 人设

```toml
[memory]
enabled = true
limit = 5

[llm]
inject_persona = true
persona_max_chars = 1200
```

---

## 命令与 Tool

### 命令

| 命令 | 说明 |
|------|------|
| `/绑定邮箱 <邮箱>` | 自助绑定（默认不主动发，需再开问候/想念） |
| `/解绑邮箱` | 解绑 |
| `/我的邮箱` | 查看自己状态 |
| `/设问候 09:00` | 开每日问候（本地 HH:MM） |
| `/关问候` | 关问候 |
| `/设想念 7天` | 每 N 天想念 |
| `/关想念` | 关想念 |
| `/发邮件 <内容>` | 立刻发 |
| `/查邮箱 <QQ>` | 管理员查询 |
| `/邮件帮助` | 帮助 |

命令返回第三项 `intercept=True` 时拦截 Maisaka，避免双重回复。

### LLM Tools

| 名称 | 作用 |
|------|------|
| `send_email_to_user` | intent：`greeting` / `miss` / `blessing` / `chat` |
| `check_email_binding` | 查当前用户是否绑定 |

另有 1 个 `HomeCard` 仪表盘卡片。

---

## 数据落盘位置

| 用途 | 路径 |
|------|------|
| 插件代码 | `plugins/email-for-maimai/` |
| **持久化数据** | `data/plugins/email-for-maimai/store.json`（`ctx.paths.data_dir`） |
| 用户覆盖模板 | `data/plugins/email-for-maimai/templates/` |
| 运行时临时 | `temp/plugins/email-for-maimai/`（`ctx.paths.runtime_dir`，不保证持久） |
| 配置 | `plugins/email-for-maimai/config.toml`（勿提交密钥） |

`store.json` 结构：

```json
{
  "bindings": { "<person_id>": { "email", "platform_uid", "status", ... } },
  "preferences": { "<person_id>": { "greeting_enabled", "miss_enabled", ... } },
  "send_logs": [ { "intent", "success", "subject", "content_preview", ... } ]
}
```

- 绑定默认 `active`；死信可 `suspended`（不解绑、不删数据）
- 发送日志有上限裁剪（实现见 `store.py`）

---

## 目录结构

```text
email-for-maimai/
├── _manifest.json          # 元信息、capabilities、依赖
├── plugin.py               # 生命周期 + Command/Tool/HomeCard
├── config_model.py         # WebUI 配置模型
├── config.example.toml     # 示例配置（无密钥）
├── pipeline.py             # 单封邮件编排
├── scheduler.py            # 扫描 + 队列 + Worker
├── context_builder.py      # 聊天上下文
├── memory_retriever.py     # knowledge.search 封装
├── persona_loader.py       # bot 人设读取/缓存
├── composer.py             # intent 化 LLM 正文
├── renderer.py             # Jinja2
├── mailer.py               # aiosmtplib + 失败分类
├── store.py                # JSON 原子写
├── utils.py                # 校验/到期/查重等纯函数
├── templates/              # default/greeting/miss
├── tests/                  # pytest
├── README.md               # 本文件
├── AGENTS.md               # 交接文档（必读）
└── plan.md                 # 内容增强规划（部分已落地）
```

---

## 架构简述

```text
on_load
  → JsonStore(data_dir)
  → EmailPipeline / Composer / Mailer / Scheduler
  → asyncio 后台扫描（startup_grace 后）

扫描到期任务 → asyncio.Queue → N workers → pipeline.process()
命令/Tool 即时路径 → 直接 pipeline.process()（便于用户立刻拿到反馈）
```

发送主链路（`pipeline`）：

1. 查绑定与暂停状态  
2. 频控 / 最小间隔  
3. 拉聊天上下文（私聊流优先，失败再全局时间窗）  
4. 可选记忆检索  
5. LLM 写纯文本（注入人设 / intent prompt）  
6. 内容查重（greeting/miss 更敏感）  
7. Jinja2 渲染  
8. SMTP 发送（瞬时错误退避重试；永久错误立即失败）  
9. 写 `send_logs`；连续失败达阈值则死信处理  

---

## 本地测试

```bash
cd email-for-maimai
python -m pytest tests/ -q
# 或在 MaiBot 同环境中 import 插件，确认组件注册数
```

生产验证清单：

1. 插件加载日志无 traceback，scheduler Worker 数符合配置  
2. `/绑定邮箱` + `/设问候`  
3. `/发邮件 测试` 能收到  
4. 日志中 `llm.generate` 使用的是文本任务，不是 embedding  
5. `store.json` 出现 success 日志  

---

## 已知坑（摘要）

1. **`llm.model` 为空 → 可能打到 embedding**（见上）  
2. **同步部署时排除生产 `config.toml`**，避免覆盖发件人/密钥  
3. **`message.build_readable` 在部分 Host 上吃 dict 会炸**；`context_builder` 已有手动抽文本兜底  
4. **greeting 到期用 attempt 做日去重**：当天失败/跳过可能挡住同日重试（见 `utils.is_greeting_due`）  
5. **内容查重**可能把模板化问候判太相似而跳过  
6. 插件**不能**用 Host `async_task_manager`；后台任务必须在 Runner 内 `asyncio.create_task`，并在 `on_unload` 取消  

更完整的「已做 / 未做 / 下一步」见 [AGENTS.md](./AGENTS.md)。

---

## 许可证

MIT
