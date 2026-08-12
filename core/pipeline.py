from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.message_components import Plain

from .config import PluginConfig
from .model import OutContext
from .step import (
    AtStep,
    BaseStep,
    BlockStep,
    CleanStep,
    ErrorStep,
    ForwardStep,
    RecallStep,
    ReplaceStep,
    ReplyStep,
    SplitStep,
    SummaryStep,
    T2IStep,
    TypoStep,
    TTSStep,
)


class Pipeline:
    """
    生产级 Pipeline：

    - 构建 step 实例
    - 统一 initialize / terminate
    - 对外唯一接口：run
    """

    # 默认顺序
    STEP_REGISTRY: list[tuple[str, type[BaseStep]]] = [
        ("summary", SummaryStep),
        ("error", ErrorStep),
        ("block", BlockStep),
        ("at", AtStep),
        ("clean", CleanStep),
        ("replace", ReplaceStep),
        ("typo", TypoStep),
        ("tts", TTSStep),
        ("t2i", T2IStep),
        ("reply", ReplyStep),
        ("forward", ForwardStep),
        ("recall", RecallStep),
        ("split", SplitStep),
    ]

    def __init__(self, config: PluginConfig):
        self.plugin_config = config
        self.cfg = config.pipeline
        self._steps: list[BaseStep] = []

        self._build_steps()

    def _build_steps(self) -> None:
        """
        根据配置构建步骤实例（默认顺序或自定义顺序）
        """
        if self.cfg.lock_order:
            for name, cls in self.STEP_REGISTRY:
                if name in self.cfg._steps:
                    step = cls(self.plugin_config)
                    self._steps.append(step)
        else:
            step_map = dict(self.STEP_REGISTRY)
            for name in self.cfg._steps:
                cls = step_map.get(name)
                if not cls:
                    logger.warning(f"未知的步骤: {name}")
                    continue
                step = cls(self.plugin_config)
                self._steps.append(step)

    # =================== Lifecycle =======================

    async def initialize(self) -> None:
        """初始化所有步骤"""
        for step in self._steps:
            await step.initialize()

    async def terminate(self) -> None:
        """终止所有步骤"""
        for step in self._steps:
            await step.terminate()

    # ==================== run =====================

    def _llm_allow(self, step: BaseStep, is_llm: bool) -> bool:
        return not self.cfg.is_llm_step(step.name) or is_llm

    def _debug_enabled(self) -> bool:
        return bool(getattr(self.cfg, "debug_logging", False))

    @staticmethod
    def _step_name(step: BaseStep) -> str:
        return str(getattr(step.name, "value", step.name))

    @staticmethod
    def _preview(text: str, limit: int = 500) -> str:
        escaped = text.replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= limit:
            return escaped
        return f"{escaped[:limit]}...<共{len(text)}字符>"

    @classmethod
    def _describe_chain(cls, ctx: OutContext) -> str:
        components: list[str] = []
        plain_text: list[str] = []
        for comp in ctx.chain:
            if isinstance(comp, Plain):
                components.append(f"Plain(len={len(comp.text or '')})")
                plain_text.append(comp.text or "")
            else:
                components.append(type(comp).__name__)
        return (
            f"components=[{', '.join(components)}], "
            f"text={cls._preview(''.join(plain_text))!r}"
        )

    async def run(self, ctx: OutContext) -> bool:
        """
        运行 pipeline
        """
        debug_logging = self._debug_enabled()
        if debug_logging:
            logger.info(
                "[OutputProSplit][Pipeline] 开始: platform=%s, gid=%s, uid=%s, "
                "is_llm=%s, steps=%s, %s",
                ctx.event.get_platform_name(),
                ctx.gid,
                ctx.uid,
                ctx.is_llm,
                [self._step_name(step) for step in self._steps],
                self._describe_chain(ctx),
            )

        for step in self._steps:
            step_name = self._step_name(step)
            if not self._llm_allow(step, ctx.is_llm):
                if debug_logging:
                    logger.info(
                        "[OutputProSplit][Pipeline] 跳过步骤 %s: 仅处理 LLM 回复，当前 is_llm=%s",
                        step_name,
                        ctx.is_llm,
                    )
                continue

            before = self._describe_chain(ctx) if debug_logging else ""
            started_at = time.perf_counter()
            if debug_logging:
                logger.info("[OutputProSplit][Pipeline] 执行步骤 %s", step_name)

            try:
                result = await step.handle(ctx)
            except Exception:
                logger.error(
                    "[OutputProSplit][Pipeline] 步骤 %s 执行异常",
                    step_name,
                    exc_info=True,
                )
                raise

            if debug_logging:
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                after = self._describe_chain(ctx)
                logger.info(
                    "[OutputProSplit][Pipeline] 完成步骤 %s: %.2fms, ok=%s, "
                    "abort=%s, changed=%s, result=%r, %s",
                    step_name,
                    elapsed_ms,
                    result.ok,
                    result.abort,
                    before != after,
                    result.msg,
                    after,
                )

            if result.msg:
                if result.ok:
                    if not debug_logging:
                        logger.debug(result.msg)
                else:
                    logger.warning(result.msg)

            if result.abort:
                if debug_logging:
                    logger.info(
                        "[OutputProSplit][Pipeline] 被步骤 %s 中止",
                        step_name,
                    )
                return False

        if debug_logging:
            logger.info(
                "[OutputProSplit][Pipeline] 完成: %s",
                self._describe_chain(ctx),
            )
        return True
