"""JSON 文件存储：绑定、偏好、发送记录。

设计要点：
- 所有持久化数据放 ctx.paths.data_dir，跨重启保留。
- 写入用「临时文件 + os.replace」原子替换，防进程崩溃损坏文件。
- 读写用 asyncio.Lock 串行化，避免 scheduler 与 command 并发写冲突。
- 数据结构用 dataclass 表达，字段语义清晰、便于演进。

这是 MVP 实现；store 接口已抽象，未来可换成 SQLite 或 ctx.db 而不动上层。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Binding:
    """邮箱绑定记录。person_id 为主键。"""

    person_id: str
    platform: str = "qq"
    platform_uid: str = ""  # QQ 号
    email: str = ""
    nickname: str = ""
    bound_at: float = 0.0
    updated_at: float = 0.0
    status: str = "active"  # active | suspended
    suspend_reason: str | None = None
    suspended_at: float | None = None
    consecutive_failures: int = 0  # 连续失败计数（成功后清零）


@dataclass
class Preference:
    """单用户的邮件偏好。person_id 关联 Binding。"""

    person_id: str
    greeting_enabled: bool = False  # 绑定后默认关，主动开启
    greeting_time_local: str = "09:00"
    miss_enabled: bool = False
    miss_interval_days: int = 7
    last_greeting_utc: float | None = None  # 上次成功
    last_miss_utc: float | None = None
    last_greeting_attempt_utc: float | None = None  # 上次尝试（含失败）
    last_miss_attempt_utc: float | None = None
    updated_at: float = 0.0


@dataclass
class SendLog:
    """单次发送记录，用于限流统计与排查。"""

    person_id: str
    email: str
    intent: str
    success: bool
    send_time_utc: float
    error: str = ""
    error_code: str = ""
    subject: str = ""
    content_preview: str = ""


@dataclass
class StoreData:
    """整体存储结构。"""

    bindings: dict[str, Binding] = field(default_factory=dict)
    preferences: dict[str, Preference] = field(default_factory=dict)
    send_logs: list[SendLog] = field(default_factory=list)


class JsonStore:
    """带原子写入与并发锁的 JSON 存储。"""

    # 发送记录保留上限，避免无限膨胀
    MAX_LOGS = 5000

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "store.json"
        self._lock = asyncio.Lock()
        self._data: StoreData = StoreData()
        self._loaded = False

    async def load(self) -> None:
        """加载磁盘数据。找不到或损坏时从空状态开始。"""

        async with self._lock:
            self._data = self._read_from_disk()
            self._loaded = True

    def _read_from_disk(self) -> StoreData:
        if not self._path.exists():
            return StoreData()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return self._deserialize(raw)
        except Exception:
            # 损坏文件留个备份，便于排查；用空状态继续
            backup = self._path.with_suffix(".corrupt.json")
            try:
                self._path.replace(backup)
            except Exception:
                pass
            return StoreData()

    def _deserialize(self, raw: dict[str, Any]) -> StoreData:
        data = StoreData()
        for pid, b in (raw.get("bindings") or {}).items():
            try:
                data.bindings[pid] = Binding(**b)
            except Exception:
                continue
        for pid, p in (raw.get("preferences") or {}).items():
            try:
                data.preferences[pid] = Preference(**p)
            except Exception:
                continue
        for entry in raw.get("send_logs") or []:
            try:
                data.send_logs.append(SendLog(**entry))
            except Exception:
                continue
        return data

    async def _save(self) -> None:
        """原子写盘。调用方需已持有 self._lock。"""

        payload = {
            "bindings": {k: asdict(v) for k, v in self._data.bindings.items()},
            "preferences": {k: asdict(v) for k, v in self._data.preferences.items()},
            "send_logs": [asdict(v) for v in self._data.send_logs],
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # 原子替换

    # ─── 绑定 ──────────────────────────────────────────────
    async def save_binding(self, binding: Binding) -> None:
        async with self._lock:
            self._data.bindings[binding.person_id] = binding
            await self._save()

    async def get_binding(self, person_id: str) -> Binding | None:
        async with self._lock:
            return self._data.bindings.get(person_id)

    async def get_binding_by_qq(self, platform_uid: str) -> Binding | None:
        async with self._lock:
            for b in self._data.bindings.values():
                if b.platform_uid == str(platform_uid):
                    return b
            return None

    async def delete_binding(self, person_id: str) -> bool:
        async with self._lock:
            existed = self._data.bindings.pop(person_id, None) is not None
            self._data.preferences.pop(person_id, None)  # 关联偏好一并清理
            if existed:
                await self._save()
            return existed

    async def list_active_bindings(self) -> list[Binding]:
        async with self._lock:
            return [b for b in self._data.bindings.values() if b.status == "active"]

    async def update_binding(self, person_id: str, **changes: Any) -> Binding | None:
        async with self._lock:
            b = self._data.bindings.get(person_id)
            if b is None:
                return None
            for k, v in changes.items():
                if hasattr(b, k):
                    setattr(b, k, v)
            b.updated_at = time.time()
            await self._save()
            return b

    # ─── 偏好 ──────────────────────────────────────────────
    async def get_preference(self, person_id: str) -> Preference:
        async with self._lock:
            pref = self._data.preferences.get(person_id)
            if pref is None:
                pref = Preference(person_id=person_id)
                self._data.preferences[person_id] = pref
            return pref

    async def get_all_preferences(self) -> dict[str, Preference]:
        async with self._lock:
            return dict(self._data.preferences)

    async def update_preference(self, person_id: str, **changes: Any) -> Preference:
        async with self._lock:
            pref = self._data.preferences.get(person_id)
            if pref is None:
                pref = Preference(person_id=person_id)
                self._data.preferences[person_id] = pref
            for k, v in changes.items():
                if hasattr(pref, k):
                    setattr(pref, k, v)
            pref.updated_at = time.time()
            await self._save()
            return pref

    async def suspend_binding(self, person_id: str, reason: str) -> None:
        async with self._lock:
            b = self._data.bindings.get(person_id)
            if b is None:
                return
            b.status = "suspended"
            b.suspend_reason = reason
            b.suspended_at = time.time()
            await self._save()

    # ─── 发送记录 ──────────────────────────────────────────
    async def append_log(self, log: SendLog) -> None:
        async with self._lock:
            self._data.send_logs.append(log)
            # 超限时丢弃最旧的一批
            if len(self._data.send_logs) > self.MAX_LOGS:
                self._data.send_logs = self._data.send_logs[-self.MAX_LOGS :]
            await self._save()

    async def recent_success_logs(self, person_id: str, window_seconds: float) -> list[SendLog]:
        """返回窗口内成功的发送记录。"""

        async with self._lock:
            cutoff = time.time() - window_seconds
            return [
                log
                for log in self._data.send_logs
                if log.person_id == person_id and log.success and log.send_time_utc >= cutoff
            ]

    async def recent_contents(self, person_id: str, window_days: float) -> list[str]:
        """返回窗口内成功发送的正文预览，用于查重。"""

        async with self._lock:
            cutoff = time.time() - window_days * 86400.0
            return [
                log.content_preview
                for log in self._data.send_logs
                if log.person_id == person_id and log.success and log.content_preview and log.send_time_utc >= cutoff
            ]

    async def shutdown(self) -> None:
        """关闭时刷盘（load 后所有写入已即时落盘，这里做兜底）。"""

        async with self._lock:
            await self._save()
