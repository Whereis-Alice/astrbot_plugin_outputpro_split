import re
from collections import defaultdict

import emoji

from astrbot.api import logger
from astrbot.core.message.components import Plain

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class CleanStep(BaseStep):
    name = StepName.CLEAN

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.clean

    def _debug_enabled(self) -> bool:
        return bool(getattr(self.plugin_config.pipeline, "debug_logging", False))

    @staticmethod
    def _preview(text: str, limit: int = 500) -> str:
        escaped = text.replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= limit:
            return escaped
        return f"{escaped[:limit]}...<共{len(text)}字符>"

    async def handle(self, ctx: OutContext) -> StepResult:
        removed: dict[str, list[str]] = defaultdict(list)
        debug_logging = self._debug_enabled()
        threshold = int(self.cfg.text_threshold or 0)
        punctuation_pattern = str(self.cfg.punctuation or "")
        punctuation_re: re.Pattern[str] | None = None

        if punctuation_pattern:
            try:
                punctuation_re = re.compile(punctuation_pattern)
            except re.error as exc:
                logger.warning(
                    "[OutputProSplit][Clean] 整体清洗正则无效，已跳过: pattern=%r, error=%s",
                    punctuation_pattern,
                    exc,
                )

        if debug_logging:
            logger.info(
                "[OutputProSplit][Clean] 配置: threshold=%s, bracket=%s, "
                "parenthesis=%s, emotion_tag=%s, emoji=%s, lead=%r, tail=%r, "
                "pattern=%r, regex_valid=%s",
                threshold,
                self.cfg.bracket,
                self.cfg.parenthesis,
                self.cfg.emotion_tag,
                self.cfg.emoji,
                self.cfg.lead,
                self.cfg.tail,
                punctuation_pattern,
                punctuation_re is not None or not punctuation_pattern,
            )

        if threshold <= 0:
            if debug_logging:
                logger.info(
                    "[OutputProSplit][Clean] 跳过全部文本清理: text_threshold=%s 表示禁用",
                    threshold,
                )
            return StepResult()

        for index, seg in enumerate(ctx.chain, start=1):
            if not isinstance(seg, Plain):
                if debug_logging:
                    logger.info(
                        "[OutputProSplit][Clean] 跳过组件 #%s: type=%s，不是 Plain 文本",
                        index,
                        type(seg).__name__,
                    )
                continue

            original_text = seg.text or ""
            if len(original_text) >= threshold:
                if debug_logging:
                    logger.info(
                        "[OutputProSplit][Clean] 跳过文本 #%s: len=%s >= threshold=%s, text=%r",
                        index,
                        len(original_text),
                        threshold,
                        self._preview(original_text),
                    )
                continue

            if debug_logging:
                logger.info(
                    "[OutputProSplit][Clean] 开始文本 #%s: len=%s, text=%r",
                    index,
                    len(original_text),
                    self._preview(original_text),
                )

            # 中括号
            if self.cfg.bracket:
                matches = re.findall(r"\[.*?\]", seg.text)
                if matches:
                    removed["中括号内容"].extend(matches)
                    seg.text = re.sub(r"\[.*?\]", "", seg.text)

            # 圆括号
            if self.cfg.parenthesis:
                matches = re.findall(r"[（(].*?[）)]", seg.text)
                if matches:
                    removed["圆括号内容"].extend(matches)
                    seg.text = re.sub(r"[（(].*?[）)]", "", seg.text)

            # 情绪标签
            if self.cfg.emotion_tag:
                matches = re.findall(r"&&.*?&&", seg.text)
                if matches:
                    removed["情绪标签"].extend(matches)
                    seg.text = re.sub(r"&&.*?&&", "", seg.text)

            # Emoji
            if self.cfg.emoji:
                emojis = [c for c in seg.text if c in emoji.EMOJI_DATA]
                if emojis:
                    removed["Emoji"].extend(emojis)
                    seg.text = emoji.replace_emoji(seg.text, replace="")

            # 前缀
            if self.cfg.lead:
                for s in self.cfg.lead:
                    if seg.text.startswith(s):
                        removed["前缀"].append(s)
                        seg.text = seg.text[len(s) :]
                        break

            # 后缀
            if self.cfg.tail:
                for s in self.cfg.tail:
                    if seg.text.endswith(s):
                        removed["后缀"].append(s)
                        seg.text = seg.text[: -len(s)]
                        break

            # 标点
            if punctuation_re:
                matches = [match.group(0) for match in punctuation_re.finditer(seg.text)]
                if matches:
                    removed["整体清洗匹配"].extend(matches)
                    seg.text = punctuation_re.sub("", seg.text)

                if debug_logging:
                    match_preview = matches[:20]
                    match_suffix = "" if len(matches) <= 20 else " ..."
                    logger.info(
                        "[OutputProSplit][Clean] 正则文本 #%s: pattern=%r, "
                        "match_count=%s, matches=%r%s",
                        index,
                        punctuation_pattern,
                        len(matches),
                        match_preview,
                        match_suffix,
                    )

            if debug_logging:
                logger.info(
                    "[OutputProSplit][Clean] 完成文本 #%s: changed=%s, before=%r, after=%r",
                    index,
                    original_text != seg.text,
                    self._preview(original_text),
                    self._preview(seg.text),
                )

        return StepResult(msg=self._build_msg(removed))

    def _build_msg(self, removed: dict[str, list[str]]) -> str:
        """消息构建（记录删了什么）"""
        if not removed:
            return ""

        parts: list[str] = []

        for k, items in removed.items():
            uniq = list(dict.fromkeys(items))  # 去重但保序
            if len(uniq) == 1:
                parts.append(f"{k}: {uniq[0]}")
            else:
                preview = "、".join(uniq[:3])
                more = f" 等{len(uniq)}项" if len(uniq) > 3 else ""
                parts.append(f"{k}: {preview}{more}")

        return "文本清理：" + "；".join(parts)
