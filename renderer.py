"""邮件模板渲染。

设计：LLM 只生成「纯文本正文」，由模板负责排版/样式。
- HTML 模板用 Jinja2 渲染，开启 autoescape，杜绝正文里的尖括号破坏布局。
- 渲染失败时的兜底链：指定模板 → default → 纯文本，逐级降级，确保发得出去。
- 模板目录优先插件自带的 templates/；用户可在 data_dir/templates 覆盖或新增。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jinja2


@dataclass
class RenderedEmail:
    """渲染结果。"""

    body: str
    content_type: str  # "html" | "plain"


class EmailRenderer:
    """Jinja2 模板渲染器。"""

    def __init__(self, builtin_templates_dir: Path, user_templates_dir: Path | None = None):
        loaders: list[jinja2.BaseLoader] = []
        # 用户模板优先，覆盖内置
        if user_templates_dir is not None:
            loaders.append(jinja2.FileSystemLoader(str(user_templates_dir)))
        loaders.append(jinja2.FileSystemLoader(str(builtin_templates_dir)))
        self._env = jinja2.Environment(
            loader=jinja2.ChoiceLoader(loaders),
            autoescape=True,  # 关键：自动转义，防正文破坏 HTML
            undefined=jinja2.ChainableUndefined,
            keep_trailing_newline=True,
        )

    def render(
        self,
        template_name: str,
        content_type: str,
        context: dict[str, object],
    ) -> RenderedEmail:
        """渲染邮件。content_type 决定读 .html 还是 .txt 模板。"""

        ext = "html" if content_type == "html" else "txt"
        # 兜底链：指定模板 → default → 纯文本
        for candidate in (template_name, "default"):
            try:
                template = self._env.get_template(f"{candidate}.{ext}")
                body = template.render(**context)
                return RenderedEmail(body=body.strip(), content_type=content_type)
            except jinja2.TemplateNotFound:
                continue
            except Exception:
                # 模板语法错误等，继续尝试下一级
                continue
        # 全部失败：降级为纯文本，直接用 content 占位符
        content = str(context.get("content", "") or "")
        return RenderedEmail(body=content, content_type="plain")
