"""邮件上下文构建器：读取用户与麦麦的真实互动 + 长期记忆，作为邮件素材。

分层读取策略（每层失败都有兜底）：
1. 优先私聊流：get_stream_by_user_id → get_recent / get_by_time_in_chat
2. 私聊为空 → 跨群全局搜：get_by_time 按 user_id 过滤并保留麦麦的回复，形成完整对话
3. 完全无互动 → 返回 has_interaction=False，由 composer 走「初次问候」分支
4. 长期记忆：knowledge.search 取 A-Memorix 中与该用户相关的记忆

读取到的原始消息经 build_readable 转成 LLM 易读的对话文本。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from memory_retriever import MemoryRetriever
from store import Binding

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from config_model import ContextSection


@dataclass
class ContextData:
    """发给 LLM / 模板渲染用的上下文。"""

    nickname: str
    platform_uid: str
    recent_text: str
    has_interaction: bool
    memories: str = ""             # A-Memorix 长期记忆文本（可选）
    memory_available: bool = False  # 是否成功取到记忆


class ContextBuilder:
    def __init__(
        self,
        ctx: "PluginContext",
        context_config: "ContextSection",
        memory_retriever: "MemoryRetriever | None" = None,
    ):
        self._ctx = ctx
        self._cfg = context_config
        self._memory = memory_retriever

    async def _get_nickname(self, binding: Binding) -> str:
        """获取昵称，失败时回退到 QQ 号末段。"""

        if binding.nickname:
            return binding.nickname
        try:
            value = await self._ctx.person.get_value(binding.person_id, "nickname")
            if value:
                return str(value)
        except Exception:
            pass
        # 回退：QQ 号后 4 位
        uid = binding.platform_uid or binding.person_id
        return f"QQ{uid[-4:]}" if uid else "你"

    async def _messages_to_readable(self, messages: list) -> str:
        """把消息列表转成可读文本；空列表返回空串。

        兼容 Host 1.0.12：当 message.build_readable 无法处理 dict 列表时，
        直接提取 processed_plain_text 或 raw_message 文本做兜底。
        """

        if not messages:
            return ""

        # 首先尝试 Host 原生的 build_readable（对象格式时可用）
        try:
            text = await self._ctx.message.build_readable(messages)
            if text and str(text).strip():
                return str(text).strip()
        except Exception:
            pass

        # 兜底：自己从 dict 中提取文本
        lines = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            # 优先用 processed_plain_text
            content = str(m.get("processed_plain_text") or "").strip()
            if not content:
                # 否则从 raw_message 里取文本段
                raw = m.get("raw_message") or []
                if isinstance(raw, list):
                    parts = []
                    for seg in raw:
                        if isinstance(seg, dict):
                            seg_type = seg.get("type", "")
                            if seg_type == "text":
                                parts.append(str(seg.get("data", {}).get("text", "")))
                            elif seg_type in ("image", "emoji"):
                                parts.append(f"[{seg_type}]")
                    content = "".join(parts).strip()
            if not content:
                continue
            # 构造昵称前缀
            user_id = str((m.get("message_info") or {}).get("user_info", {}).get("user_id", ""))
            nickname = "麦麦" if user_id == "2679914384" else "你"
            # 时间戳
            ts = m.get("timestamp", "")
            try:
                ts_str = f"({datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime('%H:%M')}) " if ts else ""
            except Exception:
                ts_str = ""
            lines.append(f"{ts_str}{nickname}: {content}")
        return "\n".join(lines).strip()

    async def gather(self, binding: Binding) -> ContextData:
        """收集邮件所需上下文：聊天互动 + 长期记忆。"""

        nickname = await self._get_nickname(binding)
        limit = max(5, int(self._cfg.message_limit or 30))
        window_days = max(1, int(self._cfg.recent_window_days or 5))
        now = time.time()
        since = now - window_days * 86400.0

        # 第 1 层：私聊流
        recent_text = await self._gather_private(binding, since, now, limit)

        # 第 2 层：跨群全局搜（私聊为空时）
        if not recent_text:
            recent_text = await self._gather_global(binding, since, now, limit)

        # 第 3 层：长期记忆
        memories, memory_available = await self._gather_memories(binding)

        return ContextData(
            nickname=nickname,
            platform_uid=binding.platform_uid,
            recent_text=recent_text,
            has_interaction=bool(recent_text),
            memories=memories,
            memory_available=memory_available,
        )

    async def _gather_private(self, binding: Binding, since: float, now: float, limit: int) -> str:
        """读私聊流消息。"""

        try:
            stream = await self._ctx.chat.get_stream_by_user_id(binding.platform_uid, binding.platform or "qq")
        except Exception:
            stream = None
        if not stream:
            return ""
        stream_id = stream.get("stream_id") or stream.get("session_id") or ""
        if not stream_id:
            return ""
        # 先取最近（24h），不足再按窗口扩展
        try:
            messages = await self._ctx.message.get_recent(stream_id, limit=limit)
        except Exception:
            messages = []
        if not messages:
            try:
                messages = await self._ctx.message.get_by_time_in_chat(
                    stream_id, start_time=since, end_time=now, limit=limit
                )
            except Exception:
                messages = []
        return await self._messages_to_readable(messages)

    async def _gather_global(self, binding: Binding, since: float, now: float, limit: int) -> str:
        """全局按时间搜消息，保留该用户与麦麦的完整对话片段。"""

        global_limit = max(limit, int(self._cfg.global_search_limit or 200))
        try:
            all_messages = await self._ctx.message.get_by_time(
                start_time=since, end_time=now, limit=global_limit
            )
        except Exception:
            return ""
        if not all_messages:
            return ""

        uid = str(binding.platform_uid)
        # 保留该用户说的消息，以及麦麦的回复（下一步按相邻关系提取）
        relevant_indices = []
        for i, m in enumerate(all_messages):
            msg_user_id = str((m.get("message_info") or {}).get("user_info", {}).get("user_id", ""))
            if msg_user_id == uid:
                relevant_indices.append(i)
                # 同时保留该用户消息后紧跟的麦麦回复（通常1条）
                if i + 1 < len(all_messages):
                    relevant_indices.append(i + 1)
        # 去重并保持顺序
        seen = set()
        ordered_indices = []
        for i in relevant_indices:
            if i not in seen:
                seen.add(i)
                ordered_indices.append(i)
        user_messages = [all_messages[i] for i in ordered_indices]
        # 取最近若干条
        user_messages = user_messages[-limit:]
        return await self._messages_to_readable(user_messages)

    async def _gather_memories(self, binding: Binding) -> tuple[str, bool]:
        """取 A-Memorix 长期记忆，返回（记忆文本, 是否取到）。"""

        if self._memory is None:
            return "", False
        try:
            text = await self._memory.retrieve(binding)
            return text, bool(text)
        except Exception as exc:
            self._ctx.logger.warning(f"取记忆失败 person={binding.person_id}: {exc}")
            return "", False
