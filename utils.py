"""纯工具函数：时间/时区、邮箱校验、正文清洗等。

本模块不依赖 SDK，便于独立单元测试。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# HH:MM 本地时间格式
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# 简易邮箱格式校验（务实即可，不做 RFC 全量校验）
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# 疑似泄露 prompt / 指令特征的关键词（正文清洗用）
_LEAK_PATTERNS = [
    re.compile(r"(?im)^\s*/\w+"),  # 以 / 开头的命令行
    re.compile(r"(?i)你是.{0,8}(助手|模型|AI|机器人)"),
    re.compile(r"(?i)系统提示|system prompt|instructions?"),
]


def is_valid_email(email: str) -> bool:
    """校验邮箱格式。"""

    return bool(_EMAIL_RE.match((email or "").strip()))


def is_valid_local_time(text: str) -> bool:
    """校验 HH:MM 本地时间格式。"""

    return bool(_TIME_RE.match((text or "").strip()))


def load_timezone(name: str) -> ZoneInfo:
    """加载时区，非法时回退 UTC（不抛异常）。"""

    cleaned = (name or "").strip()
    if not cleaned:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
    except Exception:  # 某些系统缺 tzdata 时 ZoneInfo 也会抛 KeyError
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    """当前 UTC 时间（带 tzinfo）。"""

    return datetime.now(timezone.utc)


def utc_timestamp(dt: datetime) -> float:
    """datetime → UTC 时间戳。"""

    return dt.timestamp()


def local_now(tz: ZoneInfo) -> datetime:
    """当前本地时间。"""

    return datetime.now(tz)


def to_local(dt_utc: float, tz: ZoneInfo) -> datetime:
    """UTC 时间戳 → 本地 datetime。"""

    return datetime.fromtimestamp(dt_utc, tz=tz)


def greeting_word(intent: str, local_hour: int | None = None) -> str:
    """根据意图与时段给出问候词。"""

    if intent == "miss":
        return "想你啦"
    if intent == "blessing":
        return "祝福你"
    # greeting / chat 按时段
    hour = local_hour if local_hour is not None else datetime.now().hour
    if 5 <= hour < 11:
        return "早安"
    if 11 <= hour < 14:
        return "中午好"
    if 14 <= hour < 18:
        return "下午好"
    if 18 <= hour < 23:
        return "晚安前"
    return "夜深了"


def is_greeting_due(
    greeting_enabled: bool,
    greeting_time_local: str,
    last_attempt_utc: float | None,
    now_local: datetime,
) -> bool:
    """判断每日问好是否到期。

    到期条件：开关开 + 今日目标时间已过 + 今日尚未尝试过。
    「今日」以本地日期衡量，避免时区/跨天重复触发。
    """

    if not greeting_enabled:
        return False
    m = _TIME_RE.match((greeting_time_local or "").strip())
    if not m:
        return False
    target_h, target_m = int(m.group(1)), int(m.group(2))
    today_target = datetime.combine(
        now_local.date(), time(target_h, target_m), tzinfo=now_local.tzinfo
    )
    if now_local < today_target:
        return False  # 还没到今天的目标时间
    # 今日是否已尝试：把上次尝试时间转到本地比较日期
    if last_attempt_utc is not None:
        last_local_date = to_local(last_attempt_utc, now_local.tzinfo).date()
        if last_local_date >= now_local.date():
            return False
    return True


def is_miss_due(
    miss_enabled: bool,
    miss_interval_days: int,
    last_success_utc: float | None,
    last_attempt_utc: float | None,
    now_utc_ts: float,
) -> bool:
    """判断周期想念是否到期。

    到期条件：开关开 + 距上次成功已满间隔（从未成功视为到期）
    + 距上次尝试不足 24 小时不重复（去抖，避免扫描周期内/失败后反复重试）。
    用「尝试」而非「成功」做去抖，单次失败不会立刻被下一轮扫描重发刷屏。
    """

    if not miss_enabled:
        return False
    interval = max(1, int(miss_interval_days or 1))
    if last_success_utc is not None:
        elapsed_days = (now_utc_ts - last_success_utc) / 86400.0
        if elapsed_days < interval:
            return False
    # 去抖：距上次尝试不足 1 天则跳过（成功后 last_success 已推进，这里主要拦失败重试）
    if last_attempt_utc is not None and (now_utc_ts - last_attempt_utc) < 86400.0:
        return False
    return True


def sanitize_content(text: str, max_tokens: int) -> str:
    """清洗 LLM 生成的正文：去指令特征行、截断超长内容。"""

    cleaned_lines = []
    for line in (text or "").splitlines():
        if any(pat.search(line) for pat in _LEAK_PATTERNS):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).strip()
    # 粗略截断：按 max_tokens*4 估算字符上限（中文约 1 字≈1-2 token）
    char_limit = max(50, int(max_tokens) * 4)
    if len(cleaned) > char_limit:
        cleaned = cleaned[:char_limit].rstrip() + "…"
    return cleaned


def normalize_intent(raw: str) -> str:
    """把意图输入归一化到枚举。"""

    value = (raw or "").strip().lower()
    mapping = {
        "greeting": "greeting",
        "问候": "greeting",
        "问好": "greeting",
        "miss": "miss",
        "想念": "miss",
        "想你": "miss",
        "blessing": "blessing",
        "祝福": "blessing",
        "chat": "chat",
        "闲聊": "chat",
    }
    return mapping.get(value, "chat")


def content_is_similar(new_text: str, recent_texts: list[str], threshold: float = 0.8) -> bool:
    """简易相似度查重：基于 token 集合的 Jaccard 系数。

    足够防「几乎一样」的重复，不做语义级查重。
    """

    new_tokens = set(_tokenize(new_text))
    if not new_tokens:
        return False
    for old in recent_texts:
        old_tokens = set(_tokenize(old))
        if not old_tokens:
            continue
        inter = len(new_tokens & old_tokens)
        union = len(new_tokens | old_tokens)
        if union and (inter / union) >= threshold:
            return True
    return False


def _tokenize(text: str) -> list[str]:
    """简易分词：中文按字符，英文按单词。"""

    if not text:
        return []
    # 提取英文单词
    words = re.findall(r"[A-Za-z]+", text.lower())
    # 中文按单字
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    return words + chars


def clamp(value: int, low: int, high: int) -> int:
    """把整数钳到区间。"""

    return max(low, min(high, int(value)))


def format_local_time(ts: float, tz: ZoneInfo) -> str:
    """UTC 时间戳 → 本地可读字符串。"""

    return to_local(ts, tz).strftime("%Y-%m-%d %H:%M")
