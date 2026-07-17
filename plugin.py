"""麦麦邮件插件 (Email for MaiMai) 主入口。

通过配置的 SMTP 服务，定时或对话触发，给绑定邮箱的用户发送问候/想念邮件。
邮件正文基于用户与麦麦的真实聊天记录由 LLM 生成，HTML 用模板填空渲染。

触发方式：
1. 定时：调度器扫描到点任务，入队由 Worker 发送（每日问好 / 周期想念）
2. 对话：LLM 自主调用 send_email_to_user 工具（自然语言触发）
3. 手动：用户执行 /发邮件 命令
"""

from __future__ import annotations

import os
import sys

# 兼容旧版插件加载器：将插件自身目录加入 sys.path，使同目录平铺模块可被顶层导入。
# 新版加载器通过 submodule_search_locations 处理包内导入，但顶层 from composer import ...
# 仍依赖 sys.path，这里显式注入保证双环境可用。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from pathlib import Path
from typing import Any

from maibot_sdk import (
    Command,
    HomeCard,
    MaiBotPlugin,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from composer import EmailComposer
from config_model import EmailForMaiConfig
from context_builder import ContextBuilder
from mailer import EmailMailer
from memory_retriever import MemoryRetriever
from persona_loader import PersonaLoader
from pipeline import EmailPipeline, SendTask
from renderer import EmailRenderer
from scheduler import EmailScheduler
from store import Binding, JsonStore
from utils import is_valid_email, is_valid_local_time


class EmailForMaiPlugin(MaiBotPlugin):
    """麦麦邮件插件。"""

    config_model = EmailForMaiConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: JsonStore | None = None
        self._pipeline: EmailPipeline | None = None
        self._scheduler: EmailScheduler | None = None
        self._builtin_templates: Path = Path(__file__).parent / "templates"
        self._persona_loader: PersonaLoader | None = None

    # ─── 生命周期 ──────────────────────────────────────────
    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("麦麦邮件插件已在配置中禁用，跳过启动")
            return

        # 持久化存储（跨重启保留）
        self._store = JsonStore(self.ctx.paths.data_dir)
        await self._store.load()

        # 加载主程序人设（缓存，避免每次发信都 RPC）
        self._persona_loader = PersonaLoader(self.ctx, self.config.llm)
        await self._persona_loader.load()

        # 构建 pipeline（从当前配置）
        self._pipeline = self._build_pipeline()

        # 启动调度
        self._scheduler = EmailScheduler(
            self.ctx, self.config.schedule, self._store, self._pipeline
        )
        await self._scheduler.start()
        self.ctx.logger.info("麦麦邮件插件加载完成")

    async def on_unload(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
        if self._store is not None:
            await self._store.shutdown()
        self.ctx.logger.info("麦麦邮件插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # 仅本插件配置变化时重建模块；bot/model 变化不直接影响发信
        if scope != "self":
            return
        if not self.config.plugin.enabled:
            # 被禁用：停掉调度
            if self._scheduler is not None:
                await self._scheduler.stop()
            self.ctx.logger.info("麦麦邮件插件已被禁用")
            return
        # 重建 pipeline 以应用新配置（SMTP/LLM/模板/上下文等）
        # 人设可能变化，也重新加载
        self._persona_loader = PersonaLoader(self.ctx, self.config.llm)
        await self._persona_loader.load()
        self._pipeline = self._build_pipeline()
        if self._store is None:
            self._store = JsonStore(self.ctx.paths.data_dir)
            await self._store.load()
        if self._scheduler is None:
            self._scheduler = EmailScheduler(self.ctx, self.config.schedule, self._store, self._pipeline)
            await self._scheduler.start()
        else:
            self._scheduler.update_config(self.config.schedule, self._pipeline)
            await self._scheduler.restart()
        self.ctx.logger.info("麦麦邮件插件配置已热重载")

    def _build_pipeline(self) -> EmailPipeline:
        """根据当前配置构建一次性的 pipeline。配置变更后重建即生效。"""

        cfg = self.config
        memory_retriever = MemoryRetriever(self.ctx, cfg.memory)
        context_builder = ContextBuilder(self.ctx, cfg.context, memory_retriever)
        persona = self._persona_loader.get() if self._persona_loader else ""
        composer = EmailComposer(
            self.ctx,
            cfg.llm,
            cfg.smtp.sender_name or "麦麦",
            persona=persona,
            timezone=cfg.schedule.timezone,
            prompt_config=cfg.prompt,
        )
        # 用户可在 data_dir/templates 放自定义模板，覆盖内置
        user_templates = Path(self.ctx.paths.data_dir) / "templates"
        renderer = EmailRenderer(self._builtin_templates, user_templates)
        mailer = EmailMailer(cfg.smtp)
        return EmailPipeline(
            self.ctx, cfg, self._store, context_builder, composer, renderer, mailer  # type: ignore[arg-type]
        )

    # ─── 辅助 ──────────────────────────────────────────────
    def _is_admin(self, kwargs: dict) -> bool:
        user_id = str(kwargs.get("user_id", "") or "")
        return user_id in {str(q) for q in self.config.admin.admin_qq_list}

    async def _person_id_from_kwargs(self, kwargs: dict) -> str:
        platform = str(kwargs.get("platform", "") or "qq") or "qq"
        user_id = str(kwargs.get("user_id", "") or "")
        if not user_id:
            return ""
        try:
            person_id = await self.ctx.person.get_id(platform, user_id)
            return str(person_id or "")
        except Exception:
            return ""

    async def _get_nickname(self, person_id: str, fallback_uid: str = "") -> str:
        try:
            value = await self.ctx.person.get_value(person_id, "nickname")
            if value:
                return str(value)
        except Exception:
            pass
        return f"QQ{fallback_uid[-4:]}" if fallback_uid else "你"

    # ─── WebUI 首页卡片 ───────────────────────────────────
    @HomeCard(
        "email_for_maimai_card",
        title="麦麦邮件",
        description="定时/对话触发，给绑定邮箱的用户发送有温度的邮件。",
        content=[
            {"type": "markdown", "content": "绑定邮箱后，麦麦会按设定给你写信（问好/想念）。"},
            {
                "type": "list",
                "items": [
                    "/绑定邮箱 <邮箱> — 绑定你的邮箱",
                    "/设问候 09:00 — 开启每日问候",
                    "/设想念 7天 — 每 7 天一封想念邮件",
                    "/发邮件 <内容> — 立刻给自己发一封",
                ],
            },
        ],
        link_url="/plugin-config?plugin=email-for-maimai",
        link_label="配置 SMTP 与调度",
        icon="mail",
        width="medium",
        order=200,
    )
    async def home_card(self) -> None:
        return None

    # ─── LLM 工具 ─────────────────────────────────────────
    @Tool(
        "send_email_to_user",
        brief_description="给用户发一封邮件。当用户主动要求发邮件、或你想表达问候/想念/祝福时调用。",
        detailed_description=(
            "给当前对话用户发送一封邮件。调用前建议先用 check_email_binding 确认对方已绑定邮箱。\n"
            "intent 决定邮件风格；content 留空时由你基于与该用户的记忆/互动自动撰写正文。"
        ),
        parameters=[
            ToolParameterInfo(
                name="intent",
                param_type=ToolParamType.STRING,
                description="邮件意图/风格",
                required=True,
                enum_values=["greeting", "miss", "blessing", "chat"],
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="指定的邮件正文；留空则由你基于记忆自动撰写",
                required=False,
            ),
        ],
    )
    async def tool_send_email(self, intent: str = "chat", content: str = "", **kwargs: Any):
        if self._pipeline is None:
            return {"success": False, "content": "邮件插件未就绪"}

        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            return {"success": False, "content": "无法识别当前用户"}

        binding = await self._store.get_binding(person_id)  # type: ignore[union-attr]
        if binding is None:
            return {"success": False, "content": "你还没有绑定邮箱，用 /绑定邮箱 <邮箱> 绑定后才能发邮件哦"}
        if binding.status != "active":
            return {"success": False, "content": "你的邮箱绑定已暂停，请重新 /绑定邮箱 恢复"}

        task = SendTask(person_id=person_id, intent=intent, custom_content=content or "", source="tool")
        outcome = await self._pipeline.process(task)
        if outcome.sent:
            return {"success": True, "content": f"已经给 {binding.email} 发了一封邮件~"}
        return {"success": False, "content": f"邮件没发出去：{outcome.reason}"}

    @Tool(
        "check_email_binding",
        brief_description="检查当前用户是否绑定了邮箱，发邮件前先调用确认。",
        parameters=[],
    )
    async def tool_check_binding(self, **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            return {"success": False, "content": "无法识别当前用户"}
        binding = await self._store.get_binding(person_id)  # type: ignore[union-attr]
        if binding is None:
            return {"success": True, "content": "未绑定邮箱"}
        if binding.status != "active":
            return {"success": True, "content": f"邮箱绑定状态：{binding.status}（需重新绑定）"}
        return {"success": True, "content": f"已绑定邮箱：{binding.email}"}

    # ─── 用户命令 ─────────────────────────────────────────
    @Command("绑定邮箱", description="绑定你的邮箱", pattern=r"^/绑定邮箱\s+(?P<email>\S+)$")
    async def cmd_bind(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件未启用", True
        matched = kwargs.get("matched_groups") or {}
        email = str(matched.get("email", "") or "").strip()
        if not is_valid_email(email):
            await self.ctx.send.text("邮箱格式好像不对哦，检查一下再试~", stream_id)
            return True, "邮箱格式错误", True

        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份，稍后再试~", stream_id)
            return True, "无法识别用户", True

        nickname = await self._get_nickname(person_id, str(kwargs.get("user_id", "")))
        import time as _time

        binding = Binding(
            person_id=person_id,
            platform=str(kwargs.get("platform", "") or "qq"),
            platform_uid=str(kwargs.get("user_id", "") or ""),
            email=email,
            nickname=nickname,
            bound_at=_time.time(),
            updated_at=_time.time(),
            status="active",
        )
        await self._store.save_binding(binding)  # type: ignore[union-attr]
        await self.ctx.send.text(
            f"绑定成功~ 邮箱：{email}\n默认不会主动发邮件，可用 /设问候 HH:MM 或 /设想念 N天 来开启",
            stream_id,
        )
        return True, "绑定成功", True

    @Command("解绑邮箱", description="解除邮箱绑定", pattern=r"^/解绑邮箱$")
    async def cmd_unbind(self, stream_id: str = "", **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        existed = await self._store.delete_binding(person_id)  # type: ignore[union-attr]
        if existed:
            await self.ctx.send.text("已解除邮箱绑定，相关偏好也一并清除了", stream_id)
            return True, "解绑成功", True
        await self.ctx.send.text("你还没有绑定邮箱哦", stream_id)
        return True, "未绑定", True

    @Command("我的邮箱", description="查看我的邮箱绑定", pattern=r"^/我的邮箱$")
    async def cmd_my_email(self, stream_id: str = "", **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        binding = await self._store.get_binding(person_id)  # type: ignore[union-attr]
        if binding is None:
            await self.ctx.send.text("你还没有绑定邮箱，用 /绑定邮箱 <邮箱> 绑定", stream_id)
            return True, "未绑定", True
        status_text = "正常" if binding.status == "active" else f"暂停（{binding.suspend_reason or ''}）"
        await self.ctx.send.text(f"你的邮箱：{binding.email}\n状态：{status_text}", stream_id)
        return True, "查询完成", True

    @Command("设问候", description="开启每日问候并设定时间", pattern=r"^/设问候\s+(?P<time>\S+)$")
    async def cmd_set_greeting(self, stream_id: str = "", **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        binding = await self._store.get_binding(person_id)  # type: ignore[union-attr]
        if binding is None:
            await self.ctx.send.text("先 /绑定邮箱 再设问候哦", stream_id)
            return True, "未绑定", True
        matched = kwargs.get("matched_groups") or {}
        t = str(matched.get("time", "") or "").strip()
        if not is_valid_local_time(t):
            await self.ctx.send.text("时间格式不对，要用 HH:MM，例如 /设问候 09:00", stream_id)
            return True, "时间格式错误", True
        await self._store.update_preference(person_id, greeting_enabled=True, greeting_time_local=t)  # type: ignore[union-attr]
        await self.ctx.send.text(f"每日问候已开启，时间 {t}（{self.config.schedule.timezone}）", stream_id)
        return True, "设问候成功", True

    @Command("关问候", description="关闭每日问候", pattern=r"^/关问候$")
    async def cmd_unset_greeting(self, stream_id: str = "", **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        await self._store.update_preference(person_id, greeting_enabled=False)  # type: ignore[union-attr]
        await self.ctx.send.text("每日问候已关闭", stream_id)
        return True, "关问候成功", True

    @Command("设想念", description="开启周期想念邮件", pattern=r"^/设想念\s+(?P<days>\d+)\s*天?$")
    async def cmd_set_miss(self, stream_id: str = "", **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        binding = await self._store.get_binding(person_id)  # type: ignore[union-attr]
        if binding is None:
            await self.ctx.send.text("先 /绑定邮箱 再设想念哦", stream_id)
            return True, "未绑定", True
        matched = kwargs.get("matched_groups") or {}
        try:
            days = max(1, int(matched.get("days", "7")))
        except (TypeError, ValueError):
            days = 7
        await self._store.update_preference(person_id, miss_enabled=True, miss_interval_days=days)  # type: ignore[union-attr]
        await self.ctx.send.text(f"周期想念已开启，每 {days} 天一封", stream_id)
        return True, "设想念成功", True

    @Command("关想念", description="关闭周期想念", pattern=r"^/关想念$")
    async def cmd_unset_miss(self, stream_id: str = "", **kwargs: Any):
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        await self._store.update_preference(person_id, miss_enabled=False)  # type: ignore[union-attr]
        await self.ctx.send.text("周期想念已关闭", stream_id)
        return True, "关想念成功", True

    @Command("发邮件", description="立刻给自己发一封邮件", pattern=r"^/发邮件\s+(?P<content>.+)$")
    async def cmd_send_now(self, stream_id: str = "", **kwargs: Any):
        if self._pipeline is None:
            await self.ctx.send.text("邮件插件未就绪", stream_id)
            return True, "插件未就绪", True
        person_id = await self._person_id_from_kwargs(kwargs)
        if not person_id:
            await self.ctx.send.text("没能识别到你的身份", stream_id)
            return True, "无法识别用户", True
        binding = await self._store.get_binding(person_id)  # type: ignore[union-attr]
        if binding is None:
            await self.ctx.send.text("先 /绑定邮箱 <邮箱> 再发邮件哦", stream_id)
            return True, "未绑定", True
        matched = kwargs.get("matched_groups") or {}
        content = str(matched.get("content", "") or "").strip()
        task = SendTask(person_id=person_id, intent="chat", custom_content=content, source="command")
        outcome = await self._pipeline.process(task)
        if outcome.sent:
            await self.ctx.send.text(f"发好啦~ 邮件已送到 {binding.email}", stream_id)
        else:
            await self.ctx.send.text(f"没发出去：{outcome.reason}", stream_id)
        return True, "发邮件完成", True

    # ─── 管理员命令 ───────────────────────────────────────
    @Command("查邮箱", description="管理员：查询指定QQ号的邮箱绑定", pattern=r"^/查邮箱\s+(?P<qq>\S+)$")
    async def cmd_admin_query(self, stream_id: str = "", **kwargs: Any):
        # 非管理员：weight=0 放行给 Maisaka，让麦麦自然回应
        if not self._is_admin(kwargs):
            return False, "", False
        matched = kwargs.get("matched_groups") or {}
        target_qq = str(matched.get("qq", "") or "").strip()
        if not target_qq:
            await self.ctx.send.text("用法：/查邮箱 <QQ号>", stream_id)
            return True, "用法提示", True
        binding = await self._store.get_binding_by_qq(target_qq)  # type: ignore[union-attr]
        if binding is None:
            await self.ctx.send.text(f"QQ {target_qq} 未绑定邮箱", stream_id)
            return True, "未绑定", True
        status_text = "正常" if binding.status == "active" else f"暂停（{binding.suspend_reason or ''}）"
        await self.ctx.send.text(
            f"QQ {target_qq}\n邮箱：{binding.email}\n状态：{status_text}",
            stream_id,
        )
        return True, "查询完成", True

    @Command("邮件帮助", description="查看邮件插件帮助", pattern=r"^/邮件帮助$")
    async def cmd_help(self, stream_id: str = "", **kwargs: Any):
        help_text = (
            "【麦麦邮件】命令列表：\n"
            "/绑定邮箱 <邮箱> — 绑定邮箱\n"
            "/解绑邮箱 — 解除绑定\n"
            "/我的邮箱 — 查看绑定状态\n"
            "/设问候 09:00 — 开启每日问候（本地时间）\n"
            "/关问候 — 关闭每日问候\n"
            "/设想念 7天 — 每 7 天一封想念邮件\n"
            "/关想念 — 关闭想念\n"
            "/发邮件 <内容> — 立刻给自己发一封\n"
            "也可以直接跟我说「给我发封邮件」，我会调用邮件工具~"
        )
        await self.ctx.send.text(help_text, stream_id)
        return True, "帮助", True


def create_plugin() -> EmailForMaiPlugin:
    """插件工厂函数，Runner 加载时调用。"""

    return EmailForMaiPlugin()
