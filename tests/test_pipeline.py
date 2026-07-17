"""端到端编排集成测试（mock 网络）。

用 FakeCtx + FakeMailer 替换真实 RPC 与 SMTP，验证 pipeline 的完整链路：
取上下文 → 生成正文 → 查重 → 渲染 → 发送 → 记录 → 失败/死信处理。

真实 SMTP 与 SDK RPC 无法在沙箱验证（出站被拦截），这里覆盖纯编排逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from config_model import EmailForMaiConfig
from context_builder import ContextBuilder
from composer import EmailComposer
from mailer import SendResult
from pipeline import EmailPipeline, SendTask
from renderer import EmailRenderer
from scheduler import EmailScheduler
from store import Binding, JsonStore, SendLog


class FakeCtx:
    """模拟 PluginContext 的能力子集。"""

    def __init__(self, data_dir: Path, *, llm_text: str = "你好呀，最近怎么样？", stream: dict | None = None):
        self.paths = type("P", (), {"data_dir": data_dir, "runtime_dir": data_dir})()
        self.logger = logging.getLogger("test")
        self.logger.addHandler(logging.NullHandler())
        self._llm_text = llm_text
        self._stream = stream
        self.sent_texts: list[tuple[str, str]] = []
        self.chat = self._Chat(self)
        self.message = self._Message()
        self.person = self._Person()
        self.llm = self._LLM(self)
        self.send = self._Send(self)

    class _Chat:
        def __init__(self, outer):
            self.outer = outer

        async def get_stream_by_user_id(self, user_id, platform="qq"):
            return self.outer._stream

    class _Message:
        async def get_recent(self, chat_id, limit=10):
            return [{"message_info": {"user_info": {"user_id": "10001"}}}, {"message_info": {"user_info": {"user_id": "0"}}}]

        async def build_readable(self, messages, **kw):
            return "麦麦: 在吗\n用户: 在的"

        async def get_by_time_in_chat(self, chat_id, start_time, end_time, **kw):
            return [{"message_info": {"user_info": {"user_id": "10001"}}}]

        async def get_by_time(self, start_time, end_time, **kw):
            return [{"message_info": {"user_info": {"user_id": "10001"}}}]

    class _Person:
        async def get_value(self, person_id, field_name):
            return "小明"

    class _LLM:
        def __init__(self, outer):
            self.outer = outer

        async def generate(self, prompt, model="", temperature=None, max_tokens=None, **kw):
            return {"success": True, "response": self.outer._llm_text}

    class _Send:
        def __init__(self, outer):
            self.outer = outer

        async def text(self, text, stream_id, **kw):
            self.outer.sent_texts.append((stream_id, text))
            return True


class FakeMailer:
    """可控结果的假发信器。"""

    def __init__(self, result: SendResult):
        self._result = result
        self.calls: list[dict] = []

    async def send(self, to_email, subject, body, content_type="html", max_retries=3, backoff_base=60):
        self.calls.append({"to": to_email, "subject": subject, "body": body, "type": content_type})
        return self._result


@pytest.mark.asyncio
async def test_successful_send(tmp_path):
    ctx = FakeCtx(tmp_path, stream={"stream_id": "s1", "session_id": "s1"})
    mailer = FakeMailer(SendResult(True))
    cfg = EmailForMaiConfig()
    store = JsonStore(tmp_path)
    await store.load()
    await store.save_binding(Binding(person_id="p1", platform_uid="10001", email="a@b.com", nickname="小明"))

    pipeline = EmailPipeline(
        ctx, cfg, store,
        ContextBuilder(ctx, cfg.context),
        EmailComposer(ctx, cfg.llm, "麦麦"),
        EmailRenderer(Path(__file__).parent.parent / "templates"),
        mailer,
    )
    outcome = await pipeline.process(SendTask(person_id="p1", intent="greeting"))
    assert outcome.sent, outcome.reason
    assert mailer.calls[0]["to"] == "a@b.com"
    assert mailer.calls[0]["type"] == "html"
    # 正文应被注入到 HTML 模板
    assert "你好呀" in mailer.calls[0]["body"]
    # 偏好已记录成功时间
    pref = await store.get_preference("p1")
    assert pref.last_greeting_utc is not None
    # 日志已写入
    logs = await store.recent_success_logs("p1", 86400)
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_permanent_failure_dead_letter_notifies(tmp_path):
    ctx = FakeCtx(tmp_path, stream={"stream_id": "s1", "session_id": "s1"})
    mailer = FakeMailer(SendResult(False, "认证失败", "AUTH_FAILED", permanent=True))
    cfg = EmailForMaiConfig()
    store = JsonStore(tmp_path)
    await store.load()
    await store.save_binding(Binding(person_id="p1", platform_uid="10001", email="a@b.com"))

    pipeline = EmailPipeline(
        ctx, cfg, store, ContextBuilder(ctx, cfg.context), EmailComposer(ctx, cfg.llm, "麦麦"),
        EmailRenderer(Path(__file__).parent.parent / "templates"), mailer,
    )
    outcome = await pipeline.process(SendTask(person_id="p1", intent="greeting"))
    assert not outcome.sent
    # 有私聊流 → 应通过 QQ 通知，绑定保持 active（不暂停）
    assert len(ctx.sent_texts) == 1
    assert "失败" in ctx.sent_texts[0][1]
    b = await store.get_binding("p1")
    assert b.status == "active"
    assert b.consecutive_failures == 1


@pytest.mark.asyncio
async def test_dead_letter_suspends_when_no_private_stream(tmp_path):
    ctx = FakeCtx(tmp_path, stream=None)  # 无私聊流
    mailer = FakeMailer(SendResult(False, "认证失败", "AUTH_FAILED", permanent=True))
    cfg = EmailForMaiConfig()
    store = JsonStore(tmp_path)
    await store.load()
    await store.save_binding(Binding(person_id="p1", platform_uid="10001", email="a@b.com"))

    pipeline = EmailPipeline(
        ctx, cfg, store, ContextBuilder(ctx, cfg.context), EmailComposer(ctx, cfg.llm, "麦麦"),
        EmailRenderer(Path(__file__).parent.parent / "templates"), mailer,
    )
    await pipeline.process(SendTask(person_id="p1", intent="greeting"))
    # 无私聊流 → 暂停（不解绑），数据保留
    b = await store.get_binding("p1")
    assert b.status == "suspended"
    assert b is not None  # 未被删除


@pytest.mark.asyncio
async def test_rate_limit_blocks(tmp_path):
    ctx = FakeCtx(tmp_path, stream={"stream_id": "s1"})
    mailer = FakeMailer(SendResult(True))
    cfg = EmailForMaiConfig()
    cfg.safety.per_user_daily_limit = 1
    cfg.safety.min_interval_minutes = 0
    store = JsonStore(tmp_path)
    await store.load()
    await store.save_binding(Binding(person_id="p1", platform_uid="10001", email="a@b.com"))
    # 预置一条今日成功记录
    import time as _t

    await store.append_log(SendLog(
        person_id="p1", email="a@b.com", intent="greeting", success=True, send_time_utc=_t.time()
    ))
    pipeline = EmailPipeline(
        ctx, cfg, store, ContextBuilder(ctx, cfg.context), EmailComposer(ctx, cfg.llm, "麦麦"),
        EmailRenderer(Path(__file__).parent.parent / "templates"), mailer,
    )
    outcome = await pipeline.process(SendTask(person_id="p1", intent="greeting"))
    assert outcome.skipped
    assert "上限" in outcome.reason
    assert mailer.calls == []  # 没有真的发


@pytest.mark.asyncio
async def test_custom_content_not_deduped(tmp_path):
    ctx = FakeCtx(tmp_path, stream={"stream_id": "s1"})
    mailer = FakeMailer(SendResult(True))
    cfg = EmailForMaiConfig()
    cfg.safety.min_interval_minutes = 0
    store = JsonStore(tmp_path)
    await store.load()
    await store.save_binding(Binding(person_id="p1", platform_uid="10001", email="a@b.com"))
    pipeline = EmailPipeline(
        ctx, cfg, store, ContextBuilder(ctx, cfg.context), EmailComposer(ctx, cfg.llm, "麦麦"),
        EmailRenderer(Path(__file__).parent.parent / "templates"), mailer,
    )
    outcome = await pipeline.process(
        SendTask(person_id="p1", intent="chat", custom_content="用户指定内容", source="command")
    )
    assert outcome.sent
    assert "用户指定内容" in mailer.calls[0]["body"]


@pytest.mark.asyncio
async def test_scheduler_enqueue_and_process(tmp_path):
    """验证 scheduler 队列 + worker 能取任务并调 pipeline。"""

    ctx = FakeCtx(tmp_path, stream={"stream_id": "s1"})
    mailer = FakeMailer(SendResult(True))
    cfg = EmailForMaiConfig()
    cfg.safety.min_interval_minutes = 0
    store = JsonStore(tmp_path)
    await store.load()
    await store.save_binding(Binding(person_id="p1", platform_uid="10001", email="a@b.com"))
    pipeline = EmailPipeline(
        ctx, cfg, store, ContextBuilder(ctx, cfg.context), EmailComposer(ctx, cfg.llm, "麦麦"),
        EmailRenderer(Path(__file__).parent.parent / "templates"), mailer,
    )
    sched = EmailScheduler(ctx, cfg.schedule, store, pipeline)
    await sched.start()
    await sched.enqueue(SendTask(person_id="p1", intent="greeting", source="command"))
    # 等待 worker 处理完
    await asyncio.sleep(0.3)
    await sched.stop()
    assert len(mailer.calls) == 1
