from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.config import PluginConfig
from .core.model import (
    REPLY_EVENT_SEQUENCE_EXTRA,
    OutContext,
    StateManager,
    StepName,
)
from .core.pipeline import Pipeline


class OutputProSplitPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.pipeline = Pipeline(self.cfg)

    async def initialize(self):
        await self.pipeline.initialize()
        if bool(getattr(self.cfg.pipeline, "debug_logging", False)):
            logger.info("[OutputProSplit][Trace] 插件已初始化，详细诊断日志已开启")

    async def terminate(self):
        if bool(getattr(self.cfg.pipeline, "debug_logging", False)):
            logger.info("[OutputProSplit][Trace] 插件正在终止")
        await self.pipeline.terminate()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        """收到群消息时"""
        gid = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        g = StateManager.get_group(gid)

        debug_logging = bool(getattr(self.cfg.pipeline, "debug_logging", False))
        if (
            self.cfg.reply.threshold > 0
            and str(sender_id) != str(self_id)
        ):
            raw_msg_id = getattr(event.message_obj, "message_id", None)
            msg_id = "" if raw_msg_id is None else str(raw_msg_id)
            if msg_id:
                # 序号写入事件本身，避免并发 LLM 回复互相消费共享队列。
                sequence = event.get_extra(REPLY_EVENT_SEQUENCE_EXTRA)
                if not isinstance(sequence, int):
                    sequence = g.record_reply_message(msg_id)
                    event.set_extra(REPLY_EVENT_SEQUENCE_EXTRA, sequence)
                if debug_logging:
                    logger.info(
                        "[OutputProSplit][Reply] 记录用户消息: gid=%s, msg_id=%s, "
                        "seq=%s, latest=%s, queue=%s",
                        gid,
                        msg_id,
                        sequence,
                        g.reply_message_seq,
                        list(g.msg_queue),
                    )
            elif debug_logging:
                logger.info(
                    "[OutputProSplit][Reply] 未记录用户消息: message_id 为空, gid=%s",
                    gid,
                )
        elif debug_logging and self.cfg.reply.threshold > 0:
            logger.info(
                "[OutputProSplit][Reply] 跳过消息记录: gid=%s, sender=%s, self=%s",
                gid,
                sender_id,
                self_id,
            )

        if self.cfg.pipeline.is_enabled_step(StepName.AT) and not self.cfg.at.at_str:
            name = event.get_sender_name()
            if len(g.name_to_qq) >= 100:
                g.name_to_qq.popitem(last=False)
            g.name_to_qq[name] = sender_id

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前"""
        result = event.get_result()
        debug_logging = bool(getattr(self.cfg.pipeline, "debug_logging", False))
        if not result:
            if debug_logging:
                logger.info("[OutputProSplit][Trace] 跳过结果装饰: event 没有 result")
            return
        if not result.chain:
            if debug_logging:
                logger.info("[OutputProSplit][Trace] 跳过结果装饰: result.chain 为空")
            return

        ctx = OutContext(
            event=event,
            chain=result.chain,
            is_llm=result.is_llm_result(),
            plain=result.get_plain_text(),
            gid=event.get_group_id(),
            uid=event.get_sender_id(),
            bid=event.get_self_id(),
            group=StateManager.get_group(event.get_group_id()),
            timestamp=event.message_obj.timestamp,
        )

        await self.pipeline.run(ctx)
