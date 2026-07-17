"""邮件正文生成器：用 LLM 基于聊天上下文、长期记忆、麦麦人设生成自然正文。

关键设计：
- 仅生成「纯文本正文」，HTML 排版交给 renderer 的模板，稳定可控。
- 按 intent 分别构建结构化 prompt，避免空泛套话。
- 支持用户自定义追加 prompt 内容（config [prompt] 段）。
- 有互动 → 基于互动 + 记忆写；无互动 → 走「初次问候」分支，不硬编不尬聊。
- LLM 调用失败 → 用模板化兜底文本，保证发得出去。
- 生成后做 sanitize：去命令特征行、截断超长。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from context_builder import ContextData
from utils import greeting_word, sanitize_content

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from config_model import LlmSection, PromptSection


_INTENT_FALLBACK = {
    "greeting": "最近过得怎么样呀？有什么开心的事记得跟我分享哦。",
    "miss": "好久没跟你说话了，有点想你呢。最近还好吗？",
    "blessing": "愿你一切顺利，每天都开开心心的。",
    "chat": "在忙吗？闲下来聊聊天呀。",
}


class EmailComposer:
    def __init__(
        self,
        ctx: "PluginContext",
        llm_config: "LlmSection",
        sender_name: str,
        persona: str = "",
        timezone: str = "Asia/Shanghai",
        prompt_config: "PromptSection | None" = None,
    ):
        self._ctx = ctx
        self._cfg = llm_config
        self._sender_name = sender_name or "麦麦"
        self._persona = persona
        self._timezone = timezone
        self._prompt_cfg = prompt_config

    def _persona_block(self) -> str:
        """人设块，如果已加载则注入 system prompt。"""

        return f"\n\n{self._persona}" if self._persona else ""

    def _memory_block(self, ctx_data: ContextData) -> str:
        """记忆块，只有取到记忆才展示。"""

        if not ctx_data.memory_available:
            return ""
        return f"关于 {ctx_data.nickname} 的长期记忆：\n{ctx_data.memories}"

    def _interaction_block(self, ctx_data: ContextData) -> str:
        """互动记录块。"""

        if not ctx_data.has_interaction:
            return ""
        return f"你们最近的互动记录：\n{ctx_data.recent_text}"

    def _system_extra(self, intent: str) -> str:
        """返回用户自定义的 system prompt 追加内容。"""

        if self._prompt_cfg is None:
            return ""
        extra = getattr(self._prompt_cfg, f"{intent}_system_extra", "") or ""
        return str(extra).strip()

    def _user_suffix(self) -> str:
        """返回用户自定义的 user prompt 追加内容。"""

        if self._prompt_cfg is None:
            return ""
        return str(self._prompt_cfg.user_suffix or "").strip()

    def _build_greeting_prompt(self, ctx_data: ContextData) -> list[dict[str, str]]:
        """早安/问候邮件：时间感 + 1-2 个具体细节 + 温暖收尾。"""

        from utils import greeting_word, local_now, load_timezone

        tz = load_timezone(self._timezone)
        local_now_dt = local_now(tz)
        hour = local_now_dt.hour
        greeting = greeting_word("greeting", hour)
        time_ctx = f"现在大约是 {local_now_dt.strftime('%H:%M')}，可以说「{greeting}」"

        extra = self._system_extra("greeting")
        extra_block = f"\n\n额外要求：\n{extra}" if extra else ""

        system = (
            f"你是「{self._sender_name}」。{self._persona_block()}"
            f"现在要给朋友 {ctx_data.nickname} 写一封日常问候邮件。"
            f"要求："
            f"1. 开场要有时间感（参考：{time_ctx}），不要突然蹦出一句'最近过得怎么样'；"
            f"2. 主体必须引用 1-2 个具体细节（来自下面的互动记录或长期记忆），禁止空话；"
            f"3. 结尾一句温暖的话；"
            f"4. 语气像真人朋友，自然、不官腔；"
            f"5. 只输出邮件正文，不要标题、不要解释、不要出现以 / 开头的内容。"
            f"{extra_block}"
        )
        user_parts = [f"收信人：{ctx_data.nickname}", time_ctx]
        interaction = self._interaction_block(ctx_data)
        if interaction:
            user_parts.append(interaction)
        memory = self._memory_block(ctx_data)
        if memory:
            user_parts.append(memory)
        user_parts.append("请写 150-300 字的邮件正文。")
        suffix = self._user_suffix()
        if suffix:
            user_parts.append(suffix)
        user = "\n\n".join(user_parts)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _build_miss_prompt(self, ctx_data: ContextData) -> list[dict[str, str]]:
        """想念邮件：基于共同记忆 + 表达想念。"""

        extra = self._system_extra("miss")
        extra_block = f"\n\n额外要求：\n{extra}" if extra else ""

        system = (
            f"你是「{self._sender_name}」。{self._persona_block()}"
            f"现在要给朋友 {ctx_data.nickname} 写一封想念邮件。"
            f"要求："
            f"1. 表达想念要自然，不要过度煽情；"
            f"2. 必须基于下面的长期记忆或互动记录，提及至少一个具体的共同经历/话题；"
            f"3. 禁止写'好久没联系了'这类无依据的套话；"
            f"4. 语气像真人朋友，自然、不官腔；"
            f"5. 只输出邮件正文，不要标题、不要解释、不要出现以 / 开头的内容。"
            f"{extra_block}"
        )
        user_parts = [f"收信人：{ctx_data.nickname}"]
        memory = self._memory_block(ctx_data)
        if memory:
            user_parts.append(memory)
        interaction = self._interaction_block(ctx_data)
        if interaction:
            user_parts.append(interaction)
        if not memory and not interaction:
            user_parts.append("你们还没有太多互动记录，请写一封轻松的、期待下次聊天的想念邮件。")
        user_parts.append("请写 150-300 字的邮件正文。")
        suffix = self._user_suffix()
        if suffix:
            user_parts.append(suffix)
        user = "\n\n".join(user_parts)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _build_blessing_prompt(self, ctx_data: ContextData) -> list[dict[str, str]]:
        """祝福邮件：温暖、简洁。"""

        extra = self._system_extra("blessing")
        extra_block = f"\n\n额外要求：\n{extra}" if extra else ""

        system = (
            f"你是「{self._sender_name}」。{self._persona_block()}"
            f"现在要给朋友 {ctx_data.nickname} 写一封祝福邮件。"
            f"要求："
            f"1. 真诚、温暖、简洁；"
            f"2. 可以结合下面的记忆或互动让祝福更具体；"
            f"3. 只输出邮件正文，不要标题、不要解释、不要出现以 / 开头的内容。"
            f"{extra_block}"
        )
        user_parts = [f"收信人：{ctx_data.nickname}"]
        interaction = self._interaction_block(ctx_data)
        if interaction:
            user_parts.append(interaction)
        memory = self._memory_block(ctx_data)
        if memory:
            user_parts.append(memory)
        user_parts.append("请写 100-200 字的祝福邮件正文。")
        suffix = self._user_suffix()
        if suffix:
            user_parts.append(suffix)
        user = "\n\n".join(user_parts)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _build_chat_prompt(self, ctx_data: ContextData) -> list[dict[str, str]]:
        """人触发的闲聊邮件：自由、像老朋友。"""

        extra = self._system_extra("chat")
        extra_block = f"\n\n额外要求：\n{extra}" if extra else ""

        system = (
            f"你是「{self._sender_name}」。{self._persona_block()}"
            f"现在要给朋友 {ctx_data.nickname} 写一封邮件，像老朋友一样闲聊几句。"
            f"要求："
            f"1. 基于下面的互动记录和长期记忆，内容具体、不空泛；"
            f"2. 语气自然、像真人朋友，不要官腔；"
            f"3. 只输出邮件正文，不要标题、不要解释、不要出现以 / 开头的内容。"
            f"{extra_block}"
        )
        user_parts = [f"收信人：{ctx_data.nickname}"]
        interaction = self._interaction_block(ctx_data)
        if interaction:
            user_parts.append(interaction)
        memory = self._memory_block(ctx_data)
        if memory:
            user_parts.append(memory)
        user_parts.append("请写 150-300 字的邮件正文。")
        suffix = self._user_suffix()
        if suffix:
            user_parts.append(suffix)
        user = "\n\n".join(user_parts)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _build_prompt(self, ctx_data: ContextData, intent: str) -> list[dict[str, str]]:
        """根据 intent 分发到对应的 prompt 构建器。"""

        if intent == "greeting":
            return self._build_greeting_prompt(ctx_data)
        if intent == "miss":
            return self._build_miss_prompt(ctx_data)
        if intent == "blessing":
            return self._build_blessing_prompt(ctx_data)
        return self._build_chat_prompt(ctx_data)

    async def compose(self, ctx_data: ContextData, intent: str, custom_content: str = "") -> str:
        """生成正文。custom_content 非空则直接用（仅做清洗）。"""

        custom = (custom_content or "").strip()
        if custom:
            return sanitize_content(custom, self._cfg.max_tokens)

        try:
            result = await self._ctx.llm.generate(
                prompt=self._build_prompt(ctx_data, intent),
                model=str(self._cfg.model or ""),
                temperature=float(self._cfg.temperature),
                max_tokens=int(self._cfg.max_tokens),
            )
        except Exception:
            result = {"success": False}

        if isinstance(result, dict) and result.get("success"):
            text = str(result.get("response") or result.get("content") or "").strip()
            if text:
                return sanitize_content(text, self._cfg.max_tokens)
        # 兜底：模板化文本 + 称呼，保证有内容可发
        return f"{ctx_data.nickname}，{_INTENT_FALLBACK.get(intent, _INTENT_FALLBACK['chat'])}"
