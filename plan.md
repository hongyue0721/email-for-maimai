# 邮件插件增强规划：让邮件「有血有肉」

> 目标：解决问好邮件「单薄、空泛、不像麦麦本人写的」问题。  
> 状态：核心代码路径**已落地**；生产效果仍依赖 `llm.model` / 记忆质量 / 查重策略。  
> 完整交接见 [AGENTS.md](./AGENTS.md)。

---

## 问题根因（历史）

1. **素材层**：全局搜时过滤掉麦麦回复 → 只剩用户独白  
2. **Prompt 层**：硬凑字数 → 空话  
3. **人设层**：composer 不知道 bot 是谁  
4. **场景层**：greeting 无时间/记忆钩子  
5. **运维层（后发现）**：`llm.model=""` 打到 embedding，整段 LLM 失败走 fallback  

---

## Todolist

### 任务一：记忆系统接入

- [x] 1.1 新建 `memory_retriever.py` 封装 `knowledge.search`
- [x] 1.2 `_manifest.json` 增加 `knowledge.search`
- [x] 1.3 pipeline 取上下文后取记忆

### 任务二：注入人设

- [x] 2.1 `config.get` 读 personality / reply_style / nickname
- [x] 2.2 缓存与截断（`persona_loader.py`）
- [x] 2.3 system prompt 注入真实人设

### 任务三：结构化 prompting

- [x] 3.x intent 分型（greeting/miss/blessing/chat）与时间问候语
- [ ] 3.y 进一步减少套话的自动评测 / 回归样本（未做）

### 任务四：上下文

- [x] 4.1 全局回退保留相邻 bot 回复
- [x] 4.2 build_readable dict 兼容兜底

### 任务五：运维正确性

- [x] 5.1 生产 `llm.model=utils`
- [ ] 5.2 greeting attempt 日去重误杀（**未闭环**）
- [ ] 5.3 内容查重误杀模板信（**未闭环**）

---

## 非目标（明确不做或延后）

- 邮箱验证码  
- SQLite 存储（可替换 store 接口）  
- 附件 / 多发件人  
