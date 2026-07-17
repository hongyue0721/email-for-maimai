"""人设加载器：从主程序 config 读取麦麦的真实人设与说话风格，用于邮件生成。

设计：
- 在 plugin on_load / on_config_update 时读取一次并缓存，避免每次发信都走 RPC。
- 读取失败或禁用注入时返回空串，不阻断发信。
- 对超长的 reply_style 做截断，控制 token 开销。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from config_model import LlmSection


class PersonaLoader:
    """从主程序配置加载并缓存麦麦人设。"""

    _PERSONALITY_KEYS = ["personality.personality"]
    _STYLE_KEYS = ["personality.reply_style"]

    def __init__(self, ctx: "PluginContext", config: "LlmSection"):
        self._ctx = ctx
        self._cfg = config
        self._cache: str = ""
        self._loaded: bool = False

    @staticmethod
    def _first_valid(*values) -> str:
        for v in values:
            if v and str(v).strip():
                return str(v).strip()
        return ""

    async def _try_get(self, keys: list[str]) -> str:
        """按候选 key 列表尝试读取，返回第一个有效值。"""

        for key in keys:
            try:
                value = await self._ctx.config.get(key)
                if value and str(value).strip():
                    return str(value).strip()
            except Exception:
                continue
        return ""

    async def load(self) -> str:
        """读取并缓存人设文本。返回空串表示禁用或读取失败。"""

        if not self._cfg.inject_persona:
            self._cache = ""
            self._loaded = True
            return ""

        personality = await self._try_get(self._PERSONALITY_KEYS)
        style = await self._try_get(self._STYLE_KEYS)

        parts = []
        if personality:
            parts.append(f"【人设】\n{personality}")
        if style:
            # 截断控制 token，保留前半部分通常包含最核心指令
            max_chars = max(0, int(self._cfg.persona_max_chars or 0))
            if max_chars and len(style) > max_chars:
                style = style[:max_chars].rstrip() + "…"
            parts.append(f"【风格约束】\n{style}")

        self._cache = "\n\n".join(parts)
        self._loaded = True
        if hasattr(self._ctx, "logger") and self._ctx.logger is not None:
            self._ctx.logger.debug(f"邮件插件已加载人设，长度={len(self._cache)}")
        return self._cache

    def get(self) -> str:
        """返回缓存的人设文本。未加载时返回空串。"""

        return self._cache

    async def refresh(self) -> str:
        """重新加载并返回人设文本。"""

        self._loaded = False
        return await self.load()
