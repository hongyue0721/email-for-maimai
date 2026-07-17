"""SMTP 发信，基于 aiosmtplib。

关键设计：
- 同时支持 ssl(隐式TLS/465)、starttls(587)、none 三种加密方式。
  Brevo/QQ 等主流服务商据此自动选择连接参数。
- 失败分类：认证失败 / 收件人拒绝 → 永久错误（不重试，计死信）；
  连接/超时等 → 临时错误（指数退避重试）。
- 单次发送内含即时重试（max_retries），跨天不再重试同一封（由 scheduler 控制）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING

import aiosmtplib

from config_model import SMTP_PRESETS

if TYPE_CHECKING:
    from config_model import SmtpSection


@dataclass
class SendResult:
    """单次发送结果。"""

    success: bool
    error: str = ""
    error_code: str = ""
    permanent: bool = False  # 永久错误：不重试


# ─── 失败分类 ──────────────────────────────────────────────
_PERMANENT_CODES = {"AUTH_FAILED", "RECIPIENT_REFUSED", "SENDER_REFUSED"}


def _classify_exception(exc: BaseException) -> SendResult:
    """把 aiosmtplib 异常映射为 SendResult。"""

    if isinstance(exc, aiosmtplib.SMTPAuthenticationError):
        return SendResult(False, f"SMTP 认证失败：{exc}", "AUTH_FAILED", permanent=True)
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        return SendResult(False, f"收件人被拒绝：{exc}", "RECIPIENT_REFUSED", permanent=True)
    if isinstance(exc, aiosmtplib.SMTPRecipientRefused):
        return SendResult(False, f"收件人被拒绝：{exc}", "RECIPIENT_REFUSED", permanent=True)
    if isinstance(exc, aiosmtplib.SMTPSenderRefused):
        return SendResult(False, f"发件人被拒绝：{exc}", "SENDER_REFUSED", permanent=True)
    if isinstance(
        exc,
        (
            aiosmtplib.SMTPConnectError,
            aiosmtplib.SMTPConnectTimeoutError,
            aiosmtplib.SMTPConnectResponseError,
            aiosmtplib.SMTPTimeoutError,
            aiosmtplib.SMTPReadTimeoutError,
            aiosmtplib.SMTPServerDisconnected,
            ConnectionError,
            asyncio.TimeoutError,
        ),
    ):
        return SendResult(False, f"网络/连接错误：{exc}", "NETWORK", permanent=False)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        # 5xx 永久，4xx 临时
        code = getattr(exc, "code", 0) or 0
        if 500 <= code < 600:
            return SendResult(False, f"SMTP {code}：{exc}", "SMTP_5XX", permanent=True)
        return SendResult(False, f"SMTP {code}：{exc}", "SMTP_4XX", permanent=False)
    return SendResult(False, f"未知错误：{exc}", "UNKNOWN", permanent=False)


class EmailMailer:
    """SMTP 发信器。"""

    def __init__(self, smtp_config: "SmtpSection"):
        self._cfg = smtp_config

    def _resolve_connection(self) -> tuple[str, int, str]:
        """解析出实际使用的 host/port/security。

        preset != custom 时，预设完整决定 host/port/security；
        如需手动覆盖，请在 WebUI 选择 custom。
        """

        preset = str(getattr(self._cfg, "preset", "custom") or "custom").strip().lower()
        if preset != "custom" and preset in SMTP_PRESETS:
            preset_cfg = SMTP_PRESETS[preset]
            return (
                str(preset_cfg.get("host", "")).strip(),
                int(preset_cfg.get("port", 587)),
                str(preset_cfg.get("security", "starttls")).strip(),
            )
        return str(self._cfg.host or "").strip(), int(self._cfg.port or 587), str(self._cfg.security or "starttls")

    def _build_message(self, to_email: str, subject: str, body: str, content_type: str) -> EmailMessage:
        cfg = self._cfg
        sender_addr = (cfg.sender_email or cfg.username or "").strip()
        msg = EmailMessage()
        msg["From"] = f"{cfg.sender_name} <{sender_addr}>" if cfg.sender_name else sender_addr
        msg["To"] = to_email
        msg["Subject"] = subject
        if content_type == "html":
            # 同时提供纯文本降级部分，兼容不支持 HTML 的客户端
            msg.set_content("本邮件需要支持 HTML 的客户端查看。")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)
        return msg

    async def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        content_type: str = "html",
        max_retries: int = 3,
        backoff_base: int = 60,
    ) -> SendResult:
        """发送一封邮件，含即时重试（仅对临时错误）。"""

        host, port, security = self._resolve_connection()
        if not host or not self._cfg.username:
            return SendResult(False, "SMTP host 或 username 未配置", "NOT_CONFIGURED", permanent=True)
        if not to_email:
            return SendResult(False, "收件人邮箱为空", "EMPTY_RECIPIENT", permanent=True)

        use_tls = security == "ssl"
        start_tls = security == "starttls"
        attempts = max(1, int(max_retries))

        for attempt in range(attempts):
            # 每次重试重建 message，避免上一次失败时被 aiosmtplib 修改（如 Date 头）
            message = self._build_message(to_email, subject, body, content_type)
            try:
                await aiosmtplib.send(
                    message,
                    hostname=host,
                    port=port,
                    username=self._cfg.username,
                    password=self._cfg.password,
                    use_tls=use_tls,
                    start_tls=start_tls if start_tls else None,
                    timeout=float(self._cfg.timeout or 30),
                )
                return SendResult(True)
            except Exception as exc:  # noqa: BLE001 — 统一分类
                result = _classify_exception(exc)
                if result.permanent or attempt >= attempts - 1:
                    return result
                # 临时错误：指数退避后重试
                delay = int(backoff_base) * (2 ** attempt)
                await asyncio.sleep(delay)
        return SendResult(False, "重试用尽", "EXHAUSTED", permanent=False)
