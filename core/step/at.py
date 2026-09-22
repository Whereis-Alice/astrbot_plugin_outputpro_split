import random
import re

from astrbot.api import logger
from astrbot.core.message.components import At, Face, Image, Plain, Reply

from ..config import PluginConfig
from ..content import MentionText, visible_text
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class AtStep(BaseStep):
    name = StepName.AT

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.at
        self.at_head_regex = re.compile(
            r"^\s*(?:"
            r"\[at[:：]\s*(\d+)\]"
            r"|\[at[:：]\s*([^\]]+)\]"
            r"|@(\d{5,12})"
            r"|@([\u4e00-\u9fa5\w-]{2,20})"
            r")\s*",
            re.IGNORECASE,
        )

    def _trace(self, message: str, *args) -> None:
        if getattr(self.plugin_config.pipeline, "debug_logging", False):
            logger.info("[OutputProSplit][At] " + message, *args)

    def _insert_at(self, chain: list, qq: str, nickname: str = "") -> None:
        for index, component in enumerate(chain):
            if (
                isinstance(component, Plain)
                and not isinstance(component, MentionText)
                and visible_text(component.text)
            ):
                prefix = (
                    MentionText(text=f"@{nickname} ")
                    if self.cfg.at_str and nickname
                    else At(qq=qq, name=nickname)
                )
                chain.insert(index, prefix)
                if isinstance(prefix, At) and not component.text[:1].isspace():
                    component.text = " " + component.text
                return

    def _parse_fake_at(self, ctx: OutContext) -> None:
        for index, component in enumerate(ctx.chain):
            if not isinstance(component, Plain) or isinstance(component, MentionText):
                continue
            if not visible_text(component.text):
                continue
            match = self.at_head_regex.match(component.text)
            if not match:
                return
            qq = match.group(1) or match.group(3)
            nickname = match.group(2) or match.group(4) or ""
            if not qq:
                qq = ctx.group.name_to_qq.get(nickname)
            if not qq and not (self.cfg.at_str and nickname):
                self._trace("跳过无法解析的昵称: %r", nickname)
                return
            if not nickname:
                nickname = next(
                    (
                        name
                        for name, user_id in ctx.group.name_to_qq.items()
                        if str(user_id) == str(qq)
                    ),
                    str(qq),
                )
            component.text = component.text[match.end() :]
            prefix = (
                MentionText(text=f"@{nickname} ")
                if self.cfg.at_str
                else At(qq=str(qq), name=nickname)
            )
            ctx.chain.insert(index, prefix)
            return

    async def handle(self, ctx: OutContext) -> StepResult:
        has_text = any(
            isinstance(component, Plain)
            and not isinstance(component, MentionText)
            and visible_text(self.at_head_regex.sub("", component.text or "", count=1))
            for component in ctx.chain
        )
        if not has_text or not ctx.gid:
            self._trace("跳过: 无可见正文或非群消息，不生成艾特")
            return StepResult()

        self._parse_fake_at(ctx)
        if self.cfg.at_prob > 0 and all(
            isinstance(component, (Plain, Image, Face, At, Reply))
            for component in ctx.chain
        ):
            hit = random.random() < self.cfg.at_prob
            self._trace("概率判断: probability=%s, hit=%s", self.cfg.at_prob, hit)
            if not hit:
                ctx.chain[:] = [
                    component
                    for component in ctx.chain
                    if not isinstance(component, (At, MentionText))
                ]
                for component in ctx.chain:
                    if isinstance(component, Plain):
                        component.text = self.at_head_regex.sub(
                            "", component.text, count=1
                        )
                return StepResult(msg="艾特概率未命中，已移除艾特")
            if not any(
                isinstance(component, (At, MentionText)) for component in ctx.chain
            ):
                self._insert_at(ctx.chain, str(ctx.uid), ctx.event.get_sender_name())

        if self.cfg.at_str:
            for index, component in enumerate(ctx.chain):
                if isinstance(component, At):
                    nickname = component.name or next(
                        (
                            name
                            for name, user_id in ctx.group.name_to_qq.items()
                            if str(user_id) == str(component.qq)
                        ),
                        str(component.qq),
                    )
                    ctx.chain[index] = MentionText(text=f"@{nickname} ")
        return StepResult()
