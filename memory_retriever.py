"""长期记忆检索器：从 A-Memorix 取与当前用户相关的记忆，作为邮件素材。

设计：
- 封装 ctx.knowledge.search 调用，对插件上层提供简单 sync/retrieve 接口。
- query 支持模板化（{nickname}、{platform_uid}），方便哥哥自定义检索角度。
- 支持按 person_id / user_id 过滤，保证召回的记忆主要围绕该用户。
- 任何异常都降级为空串，不阻断发信。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from store import Binding

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from config_model import MemorySection


class MemoryRetriever:
    """检索与某个用户相关的 A-Memorix 长期记忆。"""

    def __init__(self, ctx: "PluginContext", config: "MemorySection") -> None:
        self._ctx = ctx
        self._cfg = config

    def _build_query(self, binding: Binding) -> str:
        """根据绑定信息构造记忆检索 query。"""

        template = self._cfg.query_template or "{nickname} 相关的事"
        nickname = binding.nickname or binding.platform_uid or "你"
        return template.format(nickname=nickname, platform_uid=binding.platform_uid or "")

    async def retrieve(self, binding: Binding) -> str:
        """检索与该用户相关的记忆文本。

        返回：A-Memorix 返回的格式化文本，无结果或失败返回空串。
        """

        if not self._cfg.enabled:
            return ""

        query = self._build_query(binding)
        try:
            # SDK 的 knowledge.search 返回 result['content'] 文本，已自动解包
            result = await self._ctx.knowledge.search(
                query=query,
                limit=self._cfg.limit,
                person_id=binding.person_id,
                user_id=binding.platform_uid,
                respect_filter=self._cfg.respect_filter,
            )
            text = str(result or "").strip()
            if not text or text.startswith("你不太了解"):
                return ""
            return text
        except Exception as exc:
            self._ctx.logger.warning(f"检索记忆失败 person={binding.person_id}: {exc}")
            return ""
