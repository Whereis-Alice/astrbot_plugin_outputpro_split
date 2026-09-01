import json
import random
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    At,
    AtAll,
    BaseMessageComponent,
    Image,
    Plain,
    Reply,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep

# 这些组件不携带可见正文，出现在图片消息里也不影响外显判定
TRANSPARENT_TYPES = (At, AtAll, Reply)


class SummaryStep(BaseStep):
    name = StepName.SUMMARY

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.summary
        self.quotes = self._load_all_quotes()

    def _load_all_quotes(self) -> list[str]:
        """
        把 summary.quotes 与 summary.quotes_files 里的金句全部合并成一个 list
        """
        quotes: list[str] = list(self.cfg.quotes)

        for file_path in self.cfg.quotes_files:
            path = Path(file_path)
            if not path.exists():
                logger.warning(f"金句文件不存在，已跳过：{path}")
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        quotes.extend(data)
                    else:
                        logger.warning(f"金句文件内容不是 list，已跳过：{path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取金句文件失败 %s: %s", path, e)
        return quotes

    def _debug_enabled(self) -> bool:
        return bool(getattr(self.plugin_config.pipeline, "debug_logging", False))

    @staticmethod
    def _preview(text: str, limit: int = 80) -> str:
        escaped = (text or "").replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= limit:
            return escaped
        return f"{escaped[:limit]}...<共{len(text)}字符>"

    @classmethod
    def _describe_chain(cls, chain: list[BaseMessageComponent]) -> str:
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(f"Plain({cls._preview(comp.text)!r})")
            elif isinstance(comp, Image):
                source = comp.file or comp.url or ""
                parts.append(f"Image({cls._preview(str(source), 60)})")
            else:
                parts.append(type(comp).__name__)
        return f"[{', '.join(parts)}]"

    def _skip(self, reason: str, chain: list[BaseMessageComponent]) -> StepResult:
        """记录跳过原因并放行给下游步骤"""
        if self._debug_enabled():
            logger.info(
                "[OutputProSplit][Summary] 跳过图片外显: %s, chain=%s",
                reason,
                self._describe_chain(chain),
            )
        return StepResult()

    async def handle(self, ctx: OutContext) -> StepResult:
        """图片外显（直接发送并中断流水线）

        注意：只有进入 AstrBot 发送前结果装饰流程的消息链才会到达这里。
        其他插件通过 event.send() / context.send_message() 主动发送的图片
        不经过本步骤，无法附加外显。
        """
        if not isinstance(ctx.event, AiocqhttpMessageEvent):
            return self._skip(
                f"仅支持 aiocqhttp 平台，当前为 {ctx.event.get_platform_name()}",
                ctx.chain,
            )

        if not ctx.chain:
            return self._skip("消息链为空", ctx.chain)

        images: list[Image] = []
        blockers: list[str] = []
        for comp in ctx.chain:
            if isinstance(comp, Image):
                images.append(comp)
                continue
            if isinstance(comp, TRANSPARENT_TYPES):
                continue
            if isinstance(comp, Plain):
                if comp.text and comp.text.strip():
                    blockers.append(f"Plain({self._preview(comp.text)!r})")
                continue
            blockers.append(type(comp).__name__)

        if not images:
            return self._skip("消息链中没有图片组件", ctx.chain)

        if blockers:
            return self._skip(
                f"消息链含有正文组件，不视为纯图片消息: {', '.join(blockers)}",
                ctx.chain,
            )

        if not self.quotes:
            return self._skip("可用金句为空，请检查 summary.quotes 配置", ctx.chain)

        try:
            obmsg = await ctx.event._parse_onebot_json(MessageChain(ctx.chain))
        except Exception as exc:
            logger.warning("[OutputProSplit][Summary] 构造 OneBot 消息失败: %s", exc)
            return StepResult(ok=False, msg=f"图片外显构造消息失败，已回退默认发送: {exc}")

        target = next(
            (
                seg
                for seg in obmsg
                if isinstance(seg, dict) and str(seg.get("type", "")) == "image"
            ),
            None,
        )
        if target is None:
            return self._skip("OneBot 消息中没有 image 段", ctx.chain)

        quote = random.choice(self.quotes)
        target.setdefault("data", {})["summary"] = quote

        if self._debug_enabled():
            logger.info(
                "[OutputProSplit][Summary] 准备发送带外显的图片: 图片数=%s, 金句=%r, chain=%s",
                len(images),
                quote,
                self._describe_chain(ctx.chain),
            )

        try:
            await ctx.event.bot.send(ctx.event.message_obj.raw_message, obmsg)  # type: ignore
        except Exception as exc:
            logger.warning("[OutputProSplit][Summary] 外显图片发送失败: %s", exc)
            return StepResult(ok=False, msg=f"图片外显发送失败，已回退默认发送: {exc}")

        ctx.event.should_call_llm(True)
        ctx.chain.clear()

        return StepResult(abort=True, msg=f"已给图片附加外显金句：{quote}")
