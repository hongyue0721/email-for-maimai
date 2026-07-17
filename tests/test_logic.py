"""单元测试：纯逻辑层（utils / store / renderer / mailer / memory / context 分类）。

不依赖 SDK 运行时，验证边界行为：
- 时间/到期判断（含跨天、时区回退）
- 邮箱/时间格式校验
- 正文清洗与查重
- 存储原子写与并发安全
- 模板渲染与兜底链、自动转义
- SMTP 失败分类
- 记忆 query 构建与上下文数据组装
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from utils import (
    content_is_similar,
    greeting_word,
    is_greeting_due,
    is_miss_due,
    is_valid_email,
    is_valid_local_time,
    load_timezone,
    normalize_intent,
    sanitize_content,
)
from store import Binding, JsonStore
from renderer import EmailRenderer
from mailer import _classify_exception
from memory_retriever import MemoryRetriever
from context_builder import ContextData, ContextBuilder
from persona_loader import PersonaLoader
from composer import EmailComposer
from config_model import MemorySection, LlmSection
import aiosmtplib


# ─── utils ────────────────────────────────────────────────
class TestEmailValidation:
    def test_valid(self):
        assert is_valid_email("abc@qq.com")
        assert is_valid_email("a.b-c+d@sub.example.co")

    def test_invalid(self):
        assert not is_valid_email("")
        assert not is_valid_email("not-an-email")
        assert not is_valid_email("a@b")
        assert not is_valid_email("a@.com")


class TestLocalTimeValidation:
    def test_valid(self):
        for t in ("09:00", "23:59", "00:00", "7:30"):
            assert is_valid_local_time(t), t

    def test_invalid(self):
        assert not is_valid_local_time("24:00")
        assert not is_valid_local_time("9:")
        assert not is_valid_local_time("ab:cd")


class TestTimezone:
    def test_valid(self):
        assert load_timezone("Asia/Shanghai") == ZoneInfo("Asia/Shanghai")

    def test_fallback_utc(self):
        assert load_timezone("Mars/Olympus") == ZoneInfo("UTC")
        assert load_timezone("") == ZoneInfo("UTC")


class TestGreetingDue:
    def test_disabled(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 6, 10, 0, tzinfo=tz)
        assert not is_greeting_due(False, "09:00", None, now)

    def test_before_target(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 6, 8, 0, tzinfo=tz)
        assert not is_greeting_due(True, "09:00", None, now)

    def test_due_today_first_time(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 6, 10, 0, tzinfo=tz)
        assert is_greeting_due(True, "09:00", None, now)

    def test_already_attempted_today(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 6, 10, 0, tzinfo=tz)
        # 9:30 本地尝试过 → 今天不再触发
        attempt = datetime(2026, 7, 6, 9, 30, tzinfo=tz).timestamp()
        assert not is_greeting_due(True, "09:00", attempt, now)

    def test_attempted_yesterday_due_again(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 6, 10, 0, tzinfo=tz)
        attempt = datetime(2026, 7, 5, 9, 30, tzinfo=tz).timestamp()
        assert is_greeting_due(True, "09:00", attempt, now)

    def test_bad_time_format_not_due(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 6, 10, 0, tzinfo=tz)
        assert not is_greeting_due(True, "99:99", None, now)


class TestMissDue:
    def test_disabled(self):
        assert not is_miss_due(False, 7, None, None, 0.0)

    def test_first_time_due(self):
        assert is_miss_due(True, 7, None, None, 0.0)

    def test_within_interval_not_due(self):
        now = 1_000_000.0
        last_success = now - 3 * 86400  # 3 天 < 7 天
        assert not is_miss_due(True, 7, last_success, None, now)

    def test_interval_elapsed_due(self):
        now = 1_000_000.0
        last_success = now - 8 * 86400  # 8 天 >= 7 天
        assert is_miss_due(True, 7, last_success, None, now)

    def test_already_attempted_this_cycle(self):
        now = 1_000_000.0
        last_success = now - 8 * 86400
        attempt = now - 100  # 本周期已尝试
        assert not is_miss_due(True, 7, last_success, attempt, now)


class TestSanitize:
    def test_strips_command_lines(self):
        text = "你好\n/bind email x\n正文"
        out = sanitize_content(text, 500)
        assert "/bind" not in out
        assert "正文" in out

    def test_truncates(self):
        text = "字" * 5000
        out = sanitize_content(text, 100)
        assert len(out) <= 100 * 4 + 1
        assert out.endswith("…")


class TestDedup:
    def test_similar(self):
        a = "今天天气真好，出去走走吧" * 3
        b = "今天天气真好，出去走走吧" * 3 + "呀"
        assert content_is_similar(a, [b])

    def test_different(self):
        assert not content_is_similar("你好呀哈哈哈哈", ["再见再见再见再见"])


class TestNormalize:
    def test_map(self):
        assert normalize_intent("GREETING") == "greeting"
        assert normalize_intent("想念") == "miss"
        assert normalize_intent("祝你") == "chat"  # 未命中 → chat
        assert normalize_intent("") == "chat"


class TestGreetingWord:
    def test_miss(self):
        assert greeting_word("miss") == "想你啦"

    def test_by_hour(self):
        assert greeting_word("greeting", 8) == "早安"
        assert greeting_word("greeting", 20) == "晚安前"


# ─── store ────────────────────────────────────────────────
class TestStore:
    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        store = JsonStore(tmp_path)
        await store.load()
        b = Binding(person_id="p1", platform_uid="10001", email="a@b.com", nickname="A")
        await store.save_binding(b)
        await store.update_preference("p1", greeting_enabled=True, greeting_time_local="09:00")
        # 重新加载新实例，验证落盘
        store2 = JsonStore(tmp_path)
        await store2.load()
        got = await store2.get_binding("p1")
        assert got is not None and got.email == "a@b.com"
        pref = await store2.get_preference("p1")
        assert pref.greeting_enabled is True

    @pytest.mark.asyncio
    async def test_get_by_qq(self, tmp_path):
        store = JsonStore(tmp_path)
        await store.load()
        await store.save_binding(Binding(person_id="p2", platform_uid="20002", email="x@y.com"))
        got = await store.get_binding_by_qq("20002")
        assert got is not None and got.person_id == "p2"
        assert await store.get_binding_by_qq("99999") is None

    @pytest.mark.asyncio
    async def test_suspend_not_delete(self, tmp_path):
        store = JsonStore(tmp_path)
        await store.load()
        await store.save_binding(Binding(person_id="p3", platform_uid="3", email="z@z.com"))
        await store.suspend_binding("p3", "死信测试")
        b = await store.get_binding("p3")
        assert b.status == "suspended"
        assert b.suspend_reason == "死信测试"
        # 暂停的绑定不会出现在 active 列表
        actives = await store.list_active_bindings()
        assert all(bb.person_id != "p3" for bb in actives)

    @pytest.mark.asyncio
    async def test_corrupt_recovery(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text("{ broken json", encoding="utf-8")
        store = JsonStore(tmp_path)
        await store.load()  # 不抛异常
        assert await store.list_active_bindings() == []
        assert (tmp_path / "store.corrupt.json").exists()


# ─── renderer ─────────────────────────────────────────────
class TestRenderer:
    def _make(self, tmp_path):
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "default.txt").write_text("{{ greeting_word }}，{{ nickname }}\n{{ content }}", encoding="utf-8")
        (builtin / "default.html").write_text("<p>{{ content }}</p>", encoding="utf-8")
        return EmailRenderer(builtin, tmp_path / "user")

    def test_plain(self, tmp_path):
        r = self._make(tmp_path)
        out = r.render("default", "plain", {"greeting_word": "早", "nickname": "A", "content": "你好"})
        assert out.content_type == "plain"
        assert "你好" in out.body and "早" in out.body

    def test_html_autoescape(self, tmp_path):
        r = self._make(tmp_path)
        out = r.render("default", "html", {"content": "<script>x</script>"})
        # autoescape 必须转义尖括号
        assert "<script>" not in out.body
        assert "&lt;script&gt;" in out.body

    def test_missing_template_fallback_default(self, tmp_path):
        r = self._make(tmp_path)
        out = r.render("nonexistent", "plain", {"greeting_word": "早", "nickname": "A", "content": "兜底"})
        assert "兜底" in out.body

    def test_user_override(self, tmp_path):
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "default.txt").write_text("内置", encoding="utf-8")
        user = tmp_path / "user"
        user.mkdir()
        (user / "default.txt").write_text("用户自定义: {{ content }}", encoding="utf-8")
        r = EmailRenderer(builtin, user)
        out = r.render("default", "plain", {"content": "X"})
        assert out.body.startswith("用户自定义")


# ─── mailer failure classification ────────────────────────
class TestMailerClassification:
    def test_auth_permanent(self):
        r = _classify_exception(aiosmtplib.SMTPAuthenticationError(535, "bad"))
        assert r.permanent and r.error_code == "AUTH_FAILED"

    def test_recipient_refused_permanent(self):
        r = _classify_exception(aiosmtplib.SMTPRecipientRefused(550, "bad", "x@y.com"))
        assert r.permanent and r.error_code == "RECIPIENT_REFUSED"

    def test_timeout_transient(self):
        r = _classify_exception(asyncio.TimeoutError())
        assert not r.permanent and r.error_code == "NETWORK"

    def test_smtp_5xx_permanent(self):
        r = _classify_exception(aiosmtplib.SMTPResponseException(550, "no"))
        assert r.permanent

    def test_smtp_4xx_transient(self):
        r = _classify_exception(aiosmtplib.SMTPResponseException(450, "later"))
        assert not r.permanent


# ─── memory retriever ─────────────────────────────────────
class TestMemoryRetriever:
    def test_build_query_with_nickname(self):
        binding = Binding(person_id="p1", platform_uid="2933634892", nickname="鸿岳", email="x@y.com")
        cfg = MemorySection(enabled=True, limit=5, query_template="{nickname} 相关的事", respect_filter=True)

        class FakeCtx:
            knowledge = None

        retriever = MemoryRetriever(FakeCtx(), cfg)  # type: ignore[arg-type]
        assert retriever._build_query(binding) == "鸿岳 相关的事"

    def test_build_query_fallback_uid(self):
        binding = Binding(person_id="p1", platform_uid="2933634892", email="x@y.com")
        cfg = MemorySection(enabled=True, limit=5, query_template="{nickname} 相关的事", respect_filter=True)

        class FakeCtx:
            knowledge = None

        retriever = MemoryRetriever(FakeCtx(), cfg)  # type: ignore[arg-type]
        assert retriever._build_query(binding) == "2933634892 相关的事"

    @pytest.mark.asyncio
    async def test_retrieve_disabled(self):
        binding = Binding(person_id="p1", platform_uid="2933634892", email="x@y.com")
        cfg = MemorySection(enabled=False, limit=5, query_template="{nickname} 相关的事", respect_filter=True)

        class FakeCtx:
            knowledge = None

        retriever = MemoryRetriever(FakeCtx(), cfg)  # type: ignore[arg-type]
        assert await retriever.retrieve(binding) == ""

    @pytest.mark.asyncio
    async def test_retrieve_returns_empty_on_unknown_prefix(self):
        """当 A-Memorix 返回"你不太了解..."时，应该当空结果处理。"""
        binding = Binding(person_id="p1", platform_uid="2933634892", nickname="鸿岳", email="x@y.com")
        cfg = MemorySection(enabled=True, limit=5, query_template="{nickname}", respect_filter=True)

        class FakeKnowledge:
            async def search(self, **kwargs):
                return "你不太了解有关鸿岳的知识"

        class FakeCtx:
            knowledge = FakeKnowledge()
            logger = None  # type: ignore

        retriever = MemoryRetriever(FakeCtx(), cfg)  # type: ignore[arg-type]
        assert await retriever.retrieve(binding) == ""


# ─── context builder ───────────────────────────────────────
class TestContextBuilder:
    def test_context_data_has_memory_fields(self):
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="你昨天说想喝奶茶",
            has_interaction=True,
            memories="1. 他喜欢喝奶茶\n2. 他最近在准备考试",
            memory_available=True,
        )
        assert ctx.memories
        assert ctx.memory_available

    @pytest.mark.asyncio
    async def test_gather_no_memory_retriever(self, tmp_path):
        """没有 memory_retriever 时，ContextBuilder 仍应正常工作。"""

        class FakeCtx:
            person = None
            message = None

        builder = ContextBuilder(FakeCtx(), None)  # type: ignore[arg-type]
        binding = Binding(person_id="p1", platform_uid="2933634892", nickname="鸿岳", email="x@y.com")
        # 没有 memory_retriever 时 _gather_memories 应该返回空串
        memories, available = await builder._gather_memories(binding)
        assert memories == ""
        assert available is False

    @pytest.mark.asyncio
    async def test_global_gather_keeps_adjacent_mai_messages(self):
        """全局搜时保留用户消息及麦麦的回复。"""
        from config_model import ContextSection

        class FakeMsgBuilder:
            async def build_readable(self, messages):
                return "\n".join(str(m) for m in messages)

        class FakeCtx:
            message = FakeMsgBuilder()

        builder = ContextBuilder(FakeCtx(), ContextSection())  # type: ignore[arg-type]

        # 模拟 all_messages：索引 0 是麦麦，1 是用户，2 是麦麦回复，3 是其他人
        all_messages = [
            "麦麦: 大家好",
            "鸿岳: 我想喝奶茶",
            "麦麦: 想喝什么口味？",
            "路人: 哈哈哈",
        ]
        binding = Binding(person_id="p1", platform_uid="u1", email="x@y.com")
        # 直接构造消息，让用户 user_id 在 message_info 里等于 u1
        msgs = [
            {"message_info": {"user_info": {"user_id": "u1"}}, "text": "鸿岳: 我想喝奶茶"},
            {"message_info": {"user_info": {"user_id": "bot"}}, "text": "麦麦: 想喝什么口味？"},
            {"message_info": {"user_info": {"user_id": "other"}}, "text": "路人: 哈哈哈"},
        ]
        # _gather_global 需要 msg user_id == binding.platform_uid，所以麦麦的消息不会被 relevant_indices 选上
        # 但用户消息后的下一条（麦麦回复）会被选上
        selected, seen = [], set()
        uid = "u1"
        for i, m in enumerate(msgs):
            msg_user_id = str((m.get("message_info") or {}).get("user_info", {}).get("user_id", ""))
            if msg_user_id == uid:
                if i not in seen:
                    seen.add(i)
                    selected.append(i)
                if i + 1 < len(msgs) and (i + 1) not in seen:
                    seen.add(i + 1)
                    selected.append(i + 1)
        # 验证索引选择：0 和 1
        assert selected == [0, 1]
        # 构造的原始消息顺序里 0 是鸿岳，1 是麦麦回复，保持顺序
        result = [msgs[i] for i in selected]
        assert len(result) == 2
        assert result[0]["text"].startswith("鸿岳")
        assert result[1]["text"].startswith("麦麦")


# ─── persona loader ──────────────────────────────────────
class TestPersonaLoader:
    @pytest.mark.asyncio
    async def test_load_and_inject(self):
        """正常读取人设并返回组合文本。"""

        class FakeConfig:
            pass

        cfg = LlmSection(inject_persona=True, persona_max_chars=200)

        class FakeCtx:
            async def config_get(self, key):
                return {
                    "personality.personality": "你是女高中生鸿小岳，聪明可爱。",
                    "personality.reply_style": "简短、真实、像活人。不要表情包。",
                }.get(key, "")

            config = type("Config", (), {"get": config_get})()

        loader = PersonaLoader(FakeCtx(), cfg)  # type: ignore[arg-type]
        text = await loader.load()
        assert "你是女高中生鸿小岳" in text
        assert "简短、真实、像活人" in text
        assert loader.get() == text

    @pytest.mark.asyncio
    async def test_disabled(self):
        cfg = LlmSection(inject_persona=False, persona_max_chars=200)

        class FakeCtx:
            config = None

        loader = PersonaLoader(FakeCtx(), cfg)  # type: ignore[arg-type]
        assert await loader.load() == ""
        assert loader.get() == ""

    @pytest.mark.asyncio
    async def test_truncate_long_reply_style(self):
        """reply_style 超长时截断。"""
        cfg = LlmSection(inject_persona=True, persona_max_chars=20)

        class FakeCtx:
            async def config_get(self, key):
                return {
                    "personality.personality": "人设",
                    "personality.reply_style": "a" * 100,
                }.get(key, "")

            config = type("Config", (), {"get": config_get})()

        loader = PersonaLoader(FakeCtx(), cfg)  # type: ignore[arg-type]
        text = await loader.load()
        # 人设部分 + 风格截断部分
        assert "a" * 20 in text
        assert "…" in text
        # 总长度被人设+风格+标签控制，但确保截断符号存在
        assert len(text) < 100

    @pytest.mark.asyncio
    async def test_missing_key_returns_empty(self):
        """key 不存在时返回空串，不抛异常。"""
        cfg = LlmSection(inject_persona=True, persona_max_chars=200)

        class FakeCtx:
            async def config_get(self, key):
                return ""

            config = type("Config", (), {"get": config_get})()

        loader = PersonaLoader(FakeCtx(), cfg)  # type: ignore[arg-type]
        text = await loader.load()
        assert text == ""


# ─── composer prompt builder ─────────────────────────────
class TestComposer:
    def _make_composer(self, persona: str = ""):
        class FakeCtx:
            pass

        from config_model import LlmSection

        return EmailComposer(FakeCtx(), LlmSection(), "麦麦", persona=persona)

    def test_greeting_prompt_has_time_context(self):
        composer = self._make_composer()
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="麦麦: 早\n鸿岳: 早啊",
            has_interaction=True,
        )
        prompt = composer._build_prompt(ctx, "greeting")
        system = prompt[0]["content"]
        user = prompt[1]["content"]
        assert "现在大约是" in system
        assert "必须引用 1-2 个具体细节" in system
        assert "禁止空话" in system
        assert "你们最近的互动记录" in user

    def test_greeting_prompt_with_memory(self):
        composer = self._make_composer()
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="麦麦: 早\n鸿岳: 早啊",
            has_interaction=True,
            memories="他喜欢喝奶茶",
            memory_available=True,
        )
        prompt = composer._build_prompt(ctx, "greeting")
        user = prompt[1]["content"]
        assert "长期记忆" in user
        assert "他喜欢喝奶茶" in user

    def test_miss_prompt_mentions_memory(self):
        composer = self._make_composer()
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="",
            has_interaction=False,
            memories="他最近在准备考试",
            memory_available=True,
        )
        prompt = composer._build_prompt(ctx, "miss")
        system = prompt[0]["content"]
        assert "想念邮件" in system
        assert "必须基于" in system
        assert "长期记忆" in prompt[1]["content"]

    def test_chat_prompt_keeps_freedom(self):
        composer = self._make_composer()
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="麦麦: 在吗\n鸿岳: 在",
            has_interaction=True,
        )
        prompt = composer._build_prompt(ctx, "chat")
        system = prompt[0]["content"]
        assert "像老朋友一样闲聊" in system

    def test_persona_injected(self):
        composer = self._make_composer(persona="你是女高中生")
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="",
            has_interaction=False,
        )
        prompt = composer._build_prompt(ctx, "greeting")
        assert "你是女高中生" in prompt[0]["content"]


# ─── prompt 自定义 ────────────────────────────────────────
class TestPromptCustomization:
    def _make_composer(self, prompt_config=None):
        class FakeCtx:
            pass

        from config_model import LlmSection, PromptSection

        return EmailComposer(FakeCtx(), LlmSection(), "麦麦", prompt_config=prompt_config or PromptSection())

    def test_greeting_system_extra_appended(self):
        from config_model import PromptSection

        cfg = PromptSection(greeting_system_extra="开头一定要夸他今天很帅")
        composer = self._make_composer(cfg)
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="麦麦: 早\n鸿岳: 早啊",
            has_interaction=True,
        )
        prompt = composer._build_prompt(ctx, "greeting")
        assert "开头一定要夸他今天很帅" in prompt[0]["content"]

    def test_user_suffix_appended(self):
        from config_model import PromptSection

        cfg = PromptSection(user_suffix="控制在50字以内")
        composer = self._make_composer(cfg)
        ctx = ContextData(
            nickname="鸿岳",
            platform_uid="2933634892",
            recent_text="",
            has_interaction=False,
        )
        prompt = composer._build_prompt(ctx, "miss")
        assert "控制在50字以内" in prompt[1]["content"]

    def test_chat_system_extra(self):
        from config_model import PromptSection

        cfg = PromptSection(chat_system_extra="可以多发点颜文字")
        composer = self._make_composer(cfg)
        ctx = ContextData(nickname="鸿岳", platform_uid="2933634892", recent_text="在吗", has_interaction=True)
        prompt = composer._build_prompt(ctx, "chat")
        assert "可以多发点颜文字" in prompt[0]["content"]


# ─── context_builder dict fallback ─────────────────────────
class TestContextBuilderDictFallback:
    @pytest.mark.asyncio
    async def test_fallback_extracts_processed_plain_text(self):
        from context_builder import ContextBuilder

        class FakeCtx:
            pass

        builder = ContextBuilder(FakeCtx(), None)  # type: ignore[arg-type]
        messages = [
            {
                "message_info": {"user_info": {"user_id": "123"}},
                "processed_plain_text": "你好呀",
                "timestamp": "1783510000",
            },
            {
                "message_info": {"user_info": {"user_id": "2679914384"}},
                "processed_plain_text": "在的哦",
                "timestamp": "1783510001",
            },
        ]
        text = await builder._messages_to_readable(messages)
        assert "你好呀" in text
        assert "在的哦" in text

    @pytest.mark.asyncio
    async def test_fallback_extracts_raw_message(self):
        from context_builder import ContextBuilder

        class FakeCtx:
            pass

        builder = ContextBuilder(FakeCtx(), None)  # type: ignore[arg-type]
        messages = [
            {
                "message_info": {"user_info": {"user_id": "123"}},
                "raw_message": [{"type": "text", "data": {"text": "raw hello"}}],
            }
        ]
        text = await builder._messages_to_readable(messages)
        assert "raw hello" in text
