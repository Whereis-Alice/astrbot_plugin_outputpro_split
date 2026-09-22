from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.config import PluginConfig
from .core.model import (
    OutContext,
    StateManager,
)
from .core.pipeline import Pipeline
from .core.traffic import GroupTrafficFilter


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

    @filter.custom_filter(GroupTrafficFilter, priority=10000)
    async def on_message(self, event: AstrMessageEvent):
        """群消息记录由过滤器完成，此处理器不会被激活。"""

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
            group=StateManager.get_event_group(event),
            timestamp=event.message_obj.timestamp,
        )

        await self.pipeline.run(ctx)
