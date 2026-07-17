"""单封邮件发送的全流程编排。

流程：限流检查 → 标记尝试(幂等) → 取上下文 → 生成正文 → 查重 → 渲染 → 发送 → 记录。
失败按「连续失败计数」推进，达阈值判死信：能 QQ 通知就通知，否则暂停绑定（不解绑）。

scheduler 把每个到点任务交给 pipeline.process()，pipeline 内部完成所有副作用与持久化。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from composer import EmailComposer
from context_builder import ContextBuilder
from mailer import EmailMailer
from renderer import EmailRenderer, RenderedEmail
from store import Binding, JsonStore, Preference, SendLog
from utils import content_is_similar, greeting_word, load_timezone, now_utc, to_local, utc_timestamp

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from config_model import EmailForMaiConfig


@dataclass
class SendTask:
    """一个待发送任务。"""

    person_id: str
    intent: str
    custom_content: str = ""
    source: str = "scheduled"  # scheduled | tool | command


@dataclass
class ProcessOutcome:
    """单次处理结果，供上层（scheduler/工具）回报。"""

    sent: bool
    skipped: bool
    reason: str
    email: str = ""


# 邮件标题模板：运行时用主程序 bot 显示名称填充，不再写死“麦麦”
_INTENT_SUBJECT_TPL = {
    "greeting": "来自{name}的问候",
    "miss": "{name}想你了",
    "blessing": "一份祝福，来自{name}",
    "chat": "{name}给你写信啦",
}


class EmailPipeline:
    def __init__(
        self,
        ctx: "PluginContext",
        config: "EmailForMaiConfig",
        store: JsonStore,
        context_builder: ContextBuilder,
        composer: EmailComposer,
        renderer: EmailRenderer,
        mailer: EmailMailer,
    ):
        self._ctx = ctx
        self._cfg = config
        self._store = store
        self._context_builder = context_builder
        self._composer = composer
        self._renderer = renderer
        self._mailer = mailer

    # ─── 对外入口 ──────────────────────────────────────────
    async def process(self, task: SendTask) -> ProcessOutcome:
        """处理一个发送任务，返回处理结果。"""

        binding = await self._store.get_binding(task.person_id)
        if binding is None:
            return ProcessOutcome(False, True, "未绑定邮箱")
        if binding.status != "active":
            status_cn = "暂停" if binding.status == "suspended" else "异常"
            return ProcessOutcome(False, True, f"绑定已{binding.status}（{status_cn}）")

        pref = await self._store.get_preference(task.person_id)

        # 1. 限流检查（命令/工具触发也走同一套防骚扰）
        blocked = await self._rate_limited_async(task.person_id)
        if blocked:
            return ProcessOutcome(False, True, blocked)

        # 2. 标记尝试（幂等：同周期内不会重复发）
        await self._mark_attempt(task.intent, pref)

        # 3. 取上下文
        ctx_data = await self._context_builder.gather(binding)

        # 4. 生成正文
        content = await self._composer.compose(ctx_data, task.intent, task.custom_content)

        # 5. 查重（仅对自动生成内容，用户指定的 custom_content 不查重）
        if not task.custom_content:
            recent = await self._store.recent_contents(task.person_id, self._cfg.safety.content_dedup_window_days)
            if content_is_similar(content, recent):
                return ProcessOutcome(False, True, "内容与近期邮件过于相似，跳过")

        # 6. 解析主程序 bot 显示名称（用于标题与落款）
        bot_name = await self._resolve_bot_name()

        # 7. 渲染
        tz = load_timezone(self._cfg.schedule.timezone)
        rendered = self._render(task.intent, content, ctx_data.nickname, tz, bot_name)

        # 8. 发送
        subject = _INTENT_SUBJECT_TPL.get(task.intent, _INTENT_SUBJECT_TPL["chat"]).format(name=bot_name)
        result = await self._mailer.send(
            to_email=binding.email,
            subject=subject,
            body=rendered.body,
            content_type=rendered.content_type,
            max_retries=self._cfg.retry.max_retries,
            backoff_base=self._cfg.retry.backoff_base,
        )

        # 9. 记录 + 更新偏好
        await self._store.append_log(
            SendLog(
                person_id=task.person_id,
                email=binding.email,
                intent=task.intent,
                success=result.success,
                send_time_utc=utc_timestamp(now_utc()),
                error=result.error,
                error_code=result.error_code,
                subject=subject,
                content_preview=content[:120],
            )
        )

        if result.success:
            await self._store.update_binding(task.person_id, consecutive_failures=0)
            await self._mark_success(task.intent, pref)
            return ProcessOutcome(True, False, "发送成功", email=binding.email)

        # 10. 失败处理
        await self._handle_failure(binding, task, result)
        return ProcessOutcome(False, False, f"发送失败：{result.error}", email=binding.email)

    # ─── 限流 ──────────────────────────────────────────────
    async def _rate_limited_async(self, person_id: str) -> str:
        """异步限流检查：每日上限 + 最小间隔。返回阻止原因或空串。"""

        success_logs = await self._store.recent_success_logs(person_id, window_seconds=86400.0)
        if len(success_logs) >= int(self._cfg.safety.per_user_daily_limit or 3):
            return "今日发送已达上限"
        if success_logs:
            latest = max(log.send_time_utc for log in success_logs)
            min_interval = float(self._cfg.safety.min_interval_minutes or 60) * 60.0
            if (time.time() - latest) < min_interval:
                return "距上次发送间隔过短"
        return ""

    # ─── 渲染 ──────────────────────────────────────────────
    async def _resolve_bot_name(self) -> str:
        """获取主程序 bot 显示名称，用于邮件标题与落款。

        优先级：bot_config 的 bot.nickname → 插件配置 sender_name → 兜底“麦麦”。
        config.get 走 RPC，任何异常都不影响发信，降级到配置值。
        """
        try:
            name = await self._ctx.config.get("bot.nickname")
            if name and str(name).strip():
                return str(name).strip()
        except Exception:
            pass
        return self._cfg.smtp.sender_name or "麦麦"

    def _render(self, intent: str, content: str, nickname: str, tz, bot_name: str = "") -> RenderedEmail:
        send_time = to_local(utc_timestamp(now_utc()), tz).strftime("%Y-%m-%d %H:%M")
        hour = to_local(utc_timestamp(now_utc()), tz).hour
        template_name = intent if intent in ("greeting", "miss") else (self._cfg.format.template or "default")
        context = {
            "nickname": nickname,
            "content": content,
            "sender_name": bot_name or self._cfg.smtp.sender_name or "麦麦",
            "send_time": send_time,
            "greeting_word": greeting_word(intent, hour),
        }
        return self._renderer.render(template_name, self._cfg.format.type, context)

    # ─── 幂等标记 ──────────────────────────────────────────
    async def _mark_attempt(self, intent: str, pref: Preference) -> None:
        now = utc_timestamp(now_utc())
        if intent == "greeting":
            await self._store.update_preference(pref.person_id, last_greeting_attempt_utc=now)
        elif intent == "miss":
            await self._store.update_preference(pref.person_id, last_miss_attempt_utc=now)

    async def _mark_success(self, intent: str, pref: Preference) -> None:
        now = utc_timestamp(now_utc())
        if intent == "greeting":
            await self._store.update_preference(pref.person_id, last_greeting_utc=now)
        elif intent == "miss":
            await self._store.update_preference(pref.person_id, last_miss_utc=now)

    # ─── 失败与死信 ────────────────────────────────────────
    async def _handle_failure(self, binding: Binding, task: SendTask, result) -> None:
        failures = binding.consecutive_failures + 1
        await self._store.update_binding(task.person_id, consecutive_failures=failures)
        # 仅永久错误或累计达阈值才判死信；临时网络错误靠下次扫描自然重试
        threshold = max(1, int(self._cfg.retry.dead_letter_threshold or 3))
        if result.permanent or failures >= threshold:
            await self._dead_letter(binding, result.error or result.error_code or "连续失败")

    async def _dead_letter(self, binding: Binding, reason: str) -> None:
        """死信处理：能 QQ 通知就通知并保持 active；否则暂停绑定。"""

        tz = load_timezone(self._cfg.schedule.timezone)
        local_time = to_local(utc_timestamp(now_utc()), tz).strftime("%Y-%m-%d %H:%M")
        notified = await self._notify_user(binding, reason, local_time)
        if not notified:
            # 没有私聊流通知渠道 → 暂停（保留数据，不解绑），等用户重新确认
            await self._store.suspend_binding(binding.person_id, reason=f"死信：{reason}")
            self._ctx.logger.warning(
                f"死信且无私聊流，已暂停绑定 person={binding.person_id} email={binding.email} reason={reason}"
            )
        else:
            self._ctx.logger.info(f"死信已通过 QQ 通知用户 person={binding.person_id} reason={reason}")

    async def _notify_user(self, binding: Binding, reason: str, local_time: str) -> bool:
        """尝试通过 QQ 私聊通知用户。返回是否通知成功。"""

        try:
            stream = await self._ctx.chat.get_stream_by_user_id(binding.platform_uid, binding.platform or "qq")
        except Exception:
            stream = None
        if not stream:
            return False
        stream_id = stream.get("stream_id") or stream.get("session_id") or ""
        if not stream_id:
            return False
        msg = (
            f"给你的邮箱 {binding.email} 发送邮件失败了，可能是邮箱地址或发件配置有问题。\n"
            f"失败时间：{local_time}\n原因：{reason}\n"
            f"可检查邮箱地址后重新「/绑定邮箱」恢复。"
        )
        try:
            await self._ctx.send.text(msg, stream_id)
            return True
        except Exception as exc:
            self._ctx.logger.warning(f"死信 QQ 通知失败 person={binding.person_id}: {exc}")
            return False
