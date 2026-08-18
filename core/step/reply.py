from astrbot.api import logger
from astrbot.core.message.components import (
    At,
    Face,
    Image,
    Plain,
    Reply,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..model import (
    REPLY_EVENT_SEQUENCE_EXTRA,
    OutContext,
    StepName,
    StepResult,
)
from .base import BaseStep


class ReplyStep(BaseStep):
    name = StepName.REPLY
    unsupported_platforms = {"dingtalk"}

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.reply

    def _debug_enabled(self) -> bool:
        pipeline = getattr(self.plugin_config, "pipeline", None)
        return bool(getattr(pipeline, "debug_logging", False))

    def _trace(self, message: str, *args) -> None:
        if self._debug_enabled():
            logger.info("[OutputProSplit][Reply] " + message, *args)

    @staticmethod
    def _message_id(ctx: OutContext) -> str:
        raw_msg_id = getattr(ctx.event.message_obj, "message_id", None)
        return "" if raw_msg_id is None else str(raw_msg_id)

    @staticmethod
    def _fallback_pushed_count(queue, msg_id: str) -> int | None:
        """兼容没有事件序号的旧事件，且忽略旧版本留下的 bot 标记。"""
        queue_ids = [
            str(item)
            for item in queue
            if not str(item).startswith("__bot_reply__")
        ]
        if msg_id not in queue_ids:
            return None
        idx = queue_ids.index(msg_id)
        return len(queue_ids) - idx - 1

    async def handle(self, ctx: OutContext) -> StepResult:
        platform_name = ctx.event.get_platform_name()
        threshold = int(self.cfg.threshold or 0)
        if threshold <= 0:
            self._trace("跳过: 引用阈值=%s", threshold)
            return StepResult()

        if platform_name in self.unsupported_platforms:
            self._trace("跳过: 平台不支持, platform=%s", platform_name)
            return StepResult(msg=f"平台不支持智能引用，已跳过: {platform_name}")

        if not ctx.gid:
            self._trace("跳过: 当前事件不是群消息, gid=%s", ctx.gid)
            return StepResult()

        if not all(isinstance(x, Plain | Image | Face | At) for x in ctx.chain):
            self._trace(
                "跳过: 消息链包含不支持智能引用的组件, types=%s",
                [type(item).__name__ for item in ctx.chain],
            )
            return StepResult()

        msg_id = self._message_id(ctx)
        if not msg_id:
            self._trace("跳过: 当前事件 message_id 为空")
            return StepResult()

        if msg_id in ctx.group.reply_handled_msg_ids:
            self._trace("跳过: 当前原消息已处理过, msg_id=%s", msg_id)
            return StepResult()

        anchor_seq = ctx.event.get_extra(REPLY_EVENT_SEQUENCE_EXTRA)
        pushed_source = "event_seq"
        if isinstance(anchor_seq, int):
            pushed = max(0, ctx.group.reply_message_seq - anchor_seq)
        else:
            pushed_source = "queue_fallback"
            pushed = self._fallback_pushed_count(ctx.group.msg_queue, msg_id)

        self._trace(
            "判断: gid=%s, msg_id=%s, anchor_seq=%s, latest_seq=%s, pushed=%s, "
            "threshold=%s, source=%s, queue=%s",
            ctx.gid,
            msg_id,
            anchor_seq,
            ctx.group.reply_message_seq,
            pushed,
            threshold,
            pushed_source,
            list(ctx.group.msg_queue),
        )

        if pushed is None or pushed < threshold:
            return StepResult()

        ctx.chain.insert(0, Reply(id=msg_id))
        if self.cfg.include_at and isinstance(ctx.event, AiocqhttpMessageEvent):
            ctx.chain.insert(1, At(qq=ctx.event.get_sender_id()))
            # 在 At 后添加带零宽空格包裹的空格，确保与后续内容有间距
            ctx.chain.insert(2, Plain(text="\u200b \u200b"))
        ctx.group.mark_reply_handled(msg_id)
        self._trace(
            "触发: gid=%s, msg_id=%s, pushed=%s, chain_types=%s",
            ctx.gid,
            msg_id,
            pushed,
            [type(item).__name__ for item in ctx.chain],
        )
        return StepResult(msg=f"已插入Reply组件, 引用消息{msg_id}")
