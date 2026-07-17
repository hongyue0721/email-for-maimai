"""调度器：队列 + Worker 池。

生产者：扫描循环（每 scan_interval 秒）遍历活跃绑定 + 偏好，把到点任务塞队列。
消费者：N 个 Worker（max_workers，钳到 1-3）从队列取任务，逐个走 pipeline 全流程。

设计要点：
- 不要求准点，排队即可；发送精度 = scan_interval。
- Worker 处理一个任务到完成才取下一个，天然串行削峰；多 Worker 提供有限并发。
- 扫描和单任务处理都用 try/except 兜底，单个异常不影响整体调度。
- on_unload 调 stop()：停扫描 → Worker 完成当前任务后退出（不中途打断发送）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pipeline import SendTask
from utils import clamp, is_greeting_due, is_miss_due, load_timezone, local_now, now_utc, utc_timestamp

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from config_model import ScheduleSection
    from pipeline import EmailPipeline
    from store import JsonStore


class EmailScheduler:
    def __init__(
        self,
        ctx: "PluginContext",
        schedule_config: "ScheduleSection",
        store: "JsonStore",
        pipeline: "EmailPipeline",
    ):
        self._ctx = ctx
        self._cfg = schedule_config
        self._store = store
        self._pipeline = pipeline

        self._queue: asyncio.Queue[SendTask] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._scan_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._started = False

    # ─── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        n = clamp(int(self._cfg.max_workers or 2), 1, 3)
        self._workers = [asyncio.create_task(self._worker_loop(i)) for i in range(n)]
        self._scan_task = asyncio.create_task(self._scan_loop())
        self._ctx.logger.info(f"邮件调度器已启动，Worker 数={n}")

    async def stop(self) -> None:
        """优雅停止：停扫描，等 Worker 完成当前任务后退出。"""

        if not self._started:
            return
        self._stop.set()
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except (asyncio.CancelledError, Exception):
                pass
        # 给每个 Worker 投递哨兵，让它完成当前任务后退出
        for _ in self._workers:
            try:
                self._queue.put_nowait(_SENTINEL)
            except asyncio.QueueFull:
                pass
        for worker in self._workers:
            try:
                await asyncio.wait_for(worker, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                worker.cancel()
        self._workers = []
        self._scan_task = None
        self._started = False
        self._ctx.logger.info("邮件调度器已停止")

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ─── 立即入队（供 Tool/Command 使用）─────────────────
    async def enqueue(self, task: SendTask) -> None:
        await self._queue.put(task)

    def update_config(self, schedule_config: "ScheduleSection", pipeline: "EmailPipeline") -> None:
        """热重载时更新调度配置与 pipeline 引用（不改运行中的 worker 数，需 restart 生效）。"""

        self._cfg = schedule_config
        self._pipeline = pipeline

    # ─── 扫描循环（生产者）──────────────────────────────
    async def _scan_loop(self) -> None:
        # 启动等待：给 Host 数据库就绪留余地
        try:
            await asyncio.sleep(max(0, int(self._cfg.startup_grace_seconds or 0)))
        except asyncio.CancelledError:
            return
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ctx.logger.error(f"邮件扫描异常：{exc}", exc_info=True)
            try:
                await asyncio.sleep(max(30, int(self._cfg.scan_interval or 3600)))
            except asyncio.CancelledError:
                raise

    async def _scan_once(self) -> None:
        bindings = await self._store.list_active_bindings()
        if not bindings:
            return
        prefs = await self._store.get_all_preferences()
        tz = load_timezone(self._cfg.timezone)
        now_local = local_now(tz)
        now_ts = utc_timestamp(now_utc())
        enqueued = 0
        for binding in bindings:
            pref = prefs.get(binding.person_id)
            if pref is None:
                continue
            # 每日问好
            if is_greeting_due(
                pref.greeting_enabled,
                pref.greeting_time_local,
                pref.last_greeting_attempt_utc,
                now_local,
            ):
                await self._queue.put(SendTask(person_id=binding.person_id, intent="greeting"))
                enqueued += 1
            # 周期想念
            if is_miss_due(
                pref.miss_enabled,
                pref.miss_interval_days,
                pref.last_miss_utc,
                pref.last_miss_attempt_utc,
                now_ts,
            ):
                await self._queue.put(SendTask(person_id=binding.person_id, intent="miss"))
                enqueued += 1
        if enqueued:
            self._ctx.logger.info(f"本轮扫描入队 {enqueued} 个发送任务")

    # ─── Worker 循环（消费者）────────────────────────────
    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            try:
                task = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                if task is _SENTINEL:
                    return
                await self._process_task(worker_id, task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ctx.logger.error(f"[Worker{worker_id}] 任务异常：{exc}", exc_info=True)
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    async def _process_task(self, worker_id: int, task: SendTask) -> None:
        self._ctx.logger.debug(
            f"[Worker{worker_id}] 处理 person={task.person_id} intent={task.intent} source={task.source}"
        )
        outcome = await self._pipeline.process(task)
        if outcome.sent:
            self._ctx.logger.info(
                f"[Worker{worker_id}] 发送成功 person={task.person_id} email={outcome.email}"
            )
        elif outcome.skipped:
            self._ctx.logger.debug(
                f"[Worker{worker_id}] 跳过 person={task.person_id}：{outcome.reason}"
            )
        else:
            self._ctx.logger.warning(
                f"[Worker{worker_id}] 发送失败 person={task.person_id}：{outcome.reason}"
            )


# 哨兵对象：投递到队列让 Worker 退出
_SENTINEL: SendTask = SendTask(person_id="", intent="", source="sentinel")
