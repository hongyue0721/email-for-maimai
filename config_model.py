"""插件配置模型。

所有配置项都会被 WebUI 自动渲染为可视化表单：
- ``Literal[...]`` 类型字段渲染为下拉框
- 嵌套的 ``PluginConfigBase`` 子类渲染为折叠的配置分组（带标题/图标/排序）

运行时通过 ``self.config`` 访问强类型配置实例，热重载由 ``on_config_update`` 处理。
"""

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


# ─── 主流 SMTP 服务商预设 ───────────────────────────────────────────
# preset 非 "custom" 时，host/port/security 按预设自动使用；
# 如需完全手动覆盖，请选择 custom。
SMTP_PRESETS: dict[str, dict[str, object]] = {
    "brevo": {"host": "smtp-relay.brevo.com", "port": 587, "security": "starttls"},
    "qq": {"host": "smtp.qq.com", "port": 465, "security": "ssl"},
    "163": {"host": "smtp.163.com", "port": 465, "security": "ssl"},
    "gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls"},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "security": "starttls"},
    "custom": {},
}

# security 取值：ssl=隐式TLS(465) / starttls=STARTTLS升级(587) / none=不加密
SecurityType = Literal["ssl", "starttls", "none"]
EmailFormatType = Literal["html", "plain"]
IntentType = Literal["greeting", "miss", "blessing", "chat"]


class PluginSection(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件（关闭后定时调度与命令都会停用）")
    config_version: str = Field(default="0.1.0", description="配置版本号")


class SmtpSection(PluginConfigBase):
    """SMTP 发信服务配置。

    注意：password 填的是邮箱服务商的「授权码/SMTP 密钥」，不是登录密码。
    """

    __ui_label__ = "SMTP 邮件服务"
    __ui_icon__ = "mail"
    __ui_order__ = 1

    preset: str = Field(default="custom", description="服务商预设：brevo/qq/163/gmail/outlook/custom")
    host: str = Field(default="", description="SMTP 服务器地址，例如 smtp-relay.brevo.com")
    port: int = Field(default=587, description="SMTP 端口（ssl 通常 465，starttls 通常 587）")
    security: SecurityType = Field(default="starttls", description="加密方式：ssl/starttls/none")
    username: str = Field(default="", description="SMTP 登录账号（部分服务商是独立登录名）")
    password: str = Field(default="", description="SMTP 授权码/密钥（不是登录密码）")
    sender_name: str = Field(default="麦麦", description="发件人显示名称")
    sender_email: str = Field(
        default="",
        description="发件人邮箱地址；留空时使用 username。Brevo 等中继服务需填已验证的发件域名邮箱",
    )
    timeout: int = Field(default=30, description="单次 SMTP 连接超时（秒）")


class ScheduleSection(PluginConfigBase):
    """定时调度配置。

    采用「队列 + Worker 池」模型：扫描循环把到点任务塞进队列，
    Worker 按并发上限逐个发送，不要求准点，排队即可。
    """

    __ui_label__ = "定时调度"
    __ui_icon__ = "clock"
    __ui_order__ = 2

    scan_interval: int = Field(
        default=3600, description="扫描间隔（秒），即发送时间精度。越小越准时但扫描越频繁"
    )
    max_workers: int = Field(default=2, description="并发发送 Worker 数（1-3），超出会被钳到 3")
    timezone: str = Field(
        default="Asia/Shanghai",
        description="用户可见时间的时区（IANA 名称，如 Asia/Shanghai）。非法时回退 UTC",
    )
    startup_grace_seconds: int = Field(
        default=15, description="启动后首次扫描的等待时间（秒），给 Host 数据库就绪留余地"
    )


class FormatSection(PluginConfigBase):
    """邮件格式与模板。"""

    __ui_label__ = "邮件格式"
    __ui_icon__ = "file-text"
    __ui_order__ = 3

    type: EmailFormatType = Field(default="html", description="邮件格式：html=富文本 / plain=纯文本")
    template: str = Field(
        default="default",
        description="模板名（对应 templates/ 下文件名，不含扩展名）。不存在时回退 default",
    )


class LlmSection(PluginConfigBase):
    """邮件正文 LLM 生成配置。

    model 字段填的是 Host 模型配置里的「任务名」（task_name），如留空则用默认模型。
    """

    __ui_label__ = "LLM 生成"
    __ui_icon__ = "sparkles"
    __ui_order__ = 4

    model: str = Field(
        default="",
        description="生成正文用的模型任务名（留空=Host 默认模型）。例如 deepseek_chat",
    )
    temperature: float = Field(default=0.8, description="采样温度（0-2），越高越发散")
    max_tokens: int = Field(default=500, description="正文最大 token 数，超出会截断")
    inject_persona: bool = Field(
        default=True, description="是否将主程序 bot 的人设/风格注入邮件正文生成"
    )
    persona_max_chars: int = Field(
        default=800, description="注入的人设+风格文本最大长度，超限截断以控制 token"
    )


class ContextSection(PluginConfigBase):
    """聊天流上下文读取配置。"""

    __ui_label__ = "聊天上下文"
    __ui_icon__ = "messages-square"
    __ui_order__ = 5

    recent_window_days: int = Field(
        default=5, description="读取最近多少天的互动作为邮件素材（私聊流优先，无则跨群全局搜）"
    )
    message_limit: int = Field(default=30, description="最多读取多少条历史消息喂给 LLM")
    global_search_limit: int = Field(
        default=200, description="无私聊流时全局搜索的消息上限（再按用户过滤），控制开销"
    )


class MemorySection(PluginConfigBase):
    """长期记忆检索配置。"""

    __ui_label__ = "长期记忆"
    __ui_icon__ = "brain"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否检索 A-Memorix 长期记忆作为邮件素材")
    limit: int = Field(default=5, description="每次检索返回的记忆条数上限")
    query_template: str = Field(
        default="{nickname} 相关的事",
        description="记忆检索 query 模板；可用变量 {nickname}、{platform_uid}",
    )
    respect_filter: bool = Field(default=True, description="是否遵循麦麦的隐私/过滤设置")


class SafetySection(PluginConfigBase):
    """防骚扰与限流。"""

    __ui_label__ = "防骚扰"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    per_user_daily_limit: int = Field(default=3, description="单个用户每天最多成功发送邮件数")
    min_interval_minutes: int = Field(default=60, description="同一用户两次发送最小间隔（分钟）")
    content_dedup_window_days: int = Field(
        default=2, description="正文查重窗口（天），窗口内相似内容不再重发"
    )


class RetrySection(PluginConfigBase):
    """发送失败重试与死信。"""

    __ui_label__ = "重试与死信"
    __ui_icon__ = "alert-triangle"
    __ui_order__ = 7

    max_retries: int = Field(default=3, description="单次发送的即时重试次数（含首次）")
    backoff_base: int = Field(default=60, description="指数退避基数（秒）：base*2^attempt")
    dead_letter_threshold: int = Field(
        default=3, description="连续失败达此次数判为死信（跨多次发送累计，非同一天内）"
    )


class AdminSection(PluginConfigBase):
    """管理员与隐私。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "user-cog"
    __ui_order__ = 8

    admin_qq_list: list[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表，可使用「/查邮箱 <QQ>」查询任意用户绑定；普通用户只能查自己",
    )


class PromptSection(PluginConfigBase):
    """Prompt 自定义配置。

    允许在不修改代码的情况下，给各 intent 的 system/user prompt 追加风格要求。
    """

    __ui_label__ = "Prompt 自定义"
    __ui_icon__ = "pen-tool"
    __ui_order__ = 9

    greeting_system_extra: str = Field(
        default="",
        description="问候邮件 system prompt 追加内容（如：更调皮、更简短）",
    )
    miss_system_extra: str = Field(
        default="",
        description="想念邮件 system prompt 追加内容",
    )
    blessing_system_extra: str = Field(
        default="",
        description="祝福邮件 system prompt 追加内容",
    )
    chat_system_extra: str = Field(
        default="",
        description="闲聊邮件 system prompt 追加内容",
    )
    user_suffix: str = Field(
        default="",
        description="所有邮件 user prompt 末尾追加内容（如：不要表情包、控制在200字以内）",
    )


class EmailForMaiConfig(PluginConfigBase):
    """麦麦邮件插件总配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    smtp: SmtpSection = Field(default_factory=SmtpSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    format: FormatSection = Field(default_factory=FormatSection)
    llm: LlmSection = Field(default_factory=LlmSection)
    context: ContextSection = Field(default_factory=ContextSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    safety: SafetySection = Field(default_factory=SafetySection)
    retry: RetrySection = Field(default_factory=RetrySection)
    admin: AdminSection = Field(default_factory=AdminSection)
    prompt: PromptSection = Field(default_factory=PromptSection)
