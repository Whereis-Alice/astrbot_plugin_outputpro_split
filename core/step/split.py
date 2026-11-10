import asyncio
import math
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    Image,
    Plain,
    Reply,
)

from ..config import PluginConfig
from ..content import MentionText, has_payload, materialize_mentions, visible_text
from ..model import OutContext, StepName, StepResult
from ..official import OFFICIAL_PLATFORMS, is_official, official_support
from .base import BaseStep
from .reply import ReplyStep

STRATEGY_ALIASES = {
    "单独": "standalone",
    "standalone": "standalone",
    "solo": "standalone",
    "跟随上段": "follow_prev",
    "follow_prev": "follow_prev",
    "previous": "follow_prev",
    "跟随下段": "follow_next",
    "接下文": "follow_next",
    "follow_next": "follow_next",
    "next": "follow_next",
    "嵌入": "inline",
    "inline": "inline",
}

STEALER_IMAGE_MARKERS = (
    "astrbot_plugin_stealer",
    "plugin_stealer",
)


@dataclass
class Segment:
    components: list[BaseMessageComponent] = field(default_factory=list)

    def append(self, comp: BaseMessageComponent) -> None:
        self.components.append(comp)

    def extend(self, comps: list[BaseMessageComponent]) -> None:
        self.components.extend(comps)

    @property
    def text(self) -> str:
        return "".join(c.text for c in self.components if isinstance(c, Plain) and not isinstance(c, MentionText))

    @property
    def visible_text(self) -> str:
        return visible_text(self.text)

    @property
    def has_media(self) -> bool:
        return any(not isinstance(c, Plain) for c in self.components)

    @property
    def is_empty(self) -> bool:
        return not has_payload(self.components)

    def lstrip_plain(self) -> None:
        prev_is_at = False
        for comp in self.components:
            if isinstance(comp, At):
                prev_is_at = True
                continue
            if isinstance(comp, Plain):
                original = comp.text
                stripped = original.lstrip()
                comp.text = " " + stripped if prev_is_at and original[:1].isspace() and stripped else stripped
                return
            return

    def rstrip_plain(self) -> None:
        for comp in reversed(self.components):
            if isinstance(comp, Plain):
                comp.text = comp.text.rstrip()
                return

    def trim_edge_blank_lines(self) -> None:
        first = next((c for c in self.components if isinstance(c, Plain)), None)
        last = next((c for c in reversed(self.components) if isinstance(c, Plain)), None)
        if first and first.text:
            first.text = re.sub(r"^(?:[ \t]*\r?\n)+", "", first.text)
        if last and last.text:
            last.text = re.sub(r"(?:\r?\n[ \t]*)+$", "", last.text)

    def strip_tail_punc(self, pattern: re.Pattern[str] | None) -> None:
        if not pattern:
            return
        for comp in reversed(self.components):
            if isinstance(comp, Plain) and comp.text.strip():
                comp.text = pattern.sub("", comp.text)
                return

    def apply_text_cleaners(
        self,
        *,
        items: list[str],
        pattern: re.Pattern[str] | None,
    ) -> None:
        for comp in self.components:
            if not isinstance(comp, Plain) or not comp.text:
                continue
            text = comp.text
            for item in items:
                if item:
                    text = text.replace(item, "")
            if pattern:
                text = pattern.sub("", text)
            comp.text = text


@dataclass
class Token:
    text: str
    is_split: bool
    priority: int = 10**9

    @property
    def weight(self) -> int:
        return sum(1 for ch in self.text if not ch.isspace())


class TextTokenizer:
    KAOMOJI_PATTERN = re.compile(
        r"("
        r"[(\[\uFF08\u3010<]"
        r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
        r"[^\u4e00-\u9fffA-Za-z0-9\s]"
        r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
        r"[)\]\uFF09\u3011>]"
        r")"
        r"|"
        r"([\u25b3\u25a6\u30fb\u30ef\^><\u2267\u2665\uff5e\uff40\u7c32\u2764]{2,15})"
    )

    def __init__(
        self,
        pattern: re.Pattern[str],
        split_priority: dict[str, int],
        *,
        smart_split: bool,
    ) -> None:
        self.pattern = pattern
        self.split_priority = split_priority
        self.smart_split = smart_split
        self.quote_chars = {'"', "'", "`"}
        self.pair_map = {
            "“": "”",
            "《": "》",
            "（": "）",
            "(": ")",
            "[": "]",
            "{": "}",
            "‘": "’",
            "【": "】",
            "<": ">",
            "「": "」",
        }

    def _protect_kaomoji(self, text: str) -> tuple[str, dict[str, str]]:
        protected = text
        mapping: dict[str, str] = {}

        for idx, match in enumerate(self.KAOMOJI_PATTERN.findall(text)):
            kaomoji = match[0] or match[1]
            if not kaomoji:
                continue
            placeholder = f"\u200bKAOMOJI_{idx}\u200b"
            protected = protected.replace(kaomoji, placeholder, 1)
            mapping[placeholder] = kaomoji

        return protected, mapping

    @staticmethod
    def _restore_kaomoji(text: str, mapping: dict[str, str]) -> str:
        for placeholder, kaomoji in mapping.items():
            text = text.replace(placeholder, kaomoji)
        return text

    def _get_split_priority(self, text: str) -> int:
        priority = 10**9
        for ch in text:
            priority = min(priority, self.split_priority.get(ch, 10**9))
        return priority

    def _has_closing_pair(self, text: str, start: int, opener: str) -> bool:
        closer = self.pair_map.get(opener)
        return bool(closer and text.find(closer, start + 1) != -1)

    @staticmethod
    def _ascii_inline_delimiter(text: str, start: int, delim: str) -> bool:
        if "\n" in delim or "\r" in delim:
            return False
        if not re.fullmatch(r"[ \t.?!,;:\-']+", delim):
            return False
        prev_ch = text[start - 1] if start > 0 else ""
        next_pos = start + len(delim)
        next_ch = text[next_pos] if next_pos < len(text) else ""
        return bool(
            re.fullmatch(r"[A-Za-z0-9 \t.?!,;:\-']", prev_ch)
            and re.fullmatch(r"[A-Za-z0-9 \t.?!,;:\-']", next_ch)
        )

    def _consume_protected_block(self, text: str, start: int) -> tuple[str, int] | None:
        if text.startswith("```", start):
            end = text.find("```", start + 3)
            if end == -1:
                return text[start:], len(text)
            end += 3
            return text[start:end], end

        if text.startswith("<think>", start):
            end = text.find("</think>", start + 7)
            if end == -1:
                return text[start:], len(text)
            end += len("</think>")
            return text[start:end], end

        return None

    def tokenize(self, text: str) -> Iterator[Token]:
        text, mapping = self._protect_kaomoji(text)
        stack: list[str] = []
        buf = ""
        i = 0

        while i < len(text):
            if self.smart_split:
                protected = self._consume_protected_block(text, i)
                if protected:
                    block, next_i = protected
                    buf += block
                    i = next_i
                    continue

            ch = text[i]
            is_opener = ch in self.pair_map

            if ch == " ":
                prev = text[i - 1] if i > 0 else ""
                next_ch = text[i + 1] if i + 1 < len(text) else ""
                if prev.isalnum() and next_ch.isalnum():
                    buf += ch
                    i += 1
                    continue

            if self.smart_split and ch in self.quote_chars:
                if stack and stack[-1] == ch:
                    stack.pop()
                elif text.find(ch, i + 1) != -1:
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            if self.smart_split and stack:
                expected = self.pair_map.get(stack[-1])
                if ch == expected:
                    stack.pop()
                elif is_opener and self._has_closing_pair(text, i, ch):
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            if self.smart_split and is_opener:
                # An unmatched LLM opener must not swallow all following text.
                if self._has_closing_pair(text, i, ch):
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            match = self.pattern.match(text, i)
            if match:
                delim = match.group()
                if self._ascii_inline_delimiter(text, i, delim):
                    buf += delim
                    i += len(delim)
                    continue

                buf += delim
                yield Token(
                    self._restore_kaomoji(buf, mapping),
                    True,
                    self._get_split_priority(delim),
                )
                buf = ""
                i += len(delim)
                continue

            buf += ch
            i += 1

        if buf:
            yield Token(self._restore_kaomoji(buf, mapping), False)


class SegmentBuilder:
    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self.current = Segment()
        self.pending_prefix: list[BaseMessageComponent] = []

    def add_prefix(self, comp: BaseMessageComponent) -> None:
        self.pending_prefix.append(comp)

    def append(self, comps: list[BaseMessageComponent]) -> None:
        if self.pending_prefix:
            self.current.components = self.pending_prefix + self.current.components
            self.pending_prefix.clear()
        self.current.extend(comps)

    def flush(self, *, force_prefix: bool = False) -> None:
        if self.pending_prefix:
            if not self.current.components and not force_prefix:
                return
            self.current.components = self.pending_prefix + self.current.components
            self.pending_prefix.clear()

        if self.current.components:
            self.segments.append(self.current)
        self.current = Segment()

    def append_to_previous(self, comp: BaseMessageComponent) -> bool:
        if self.segments:
            self.segments[-1].append(comp)
            return True
        return False

    def finalize(self) -> list[Segment]:
        self.flush(force_prefix=True)
        return self.segments


class TypingController:
    supported_platforms = {"telegram", "weixin_oc", "aiocqhttp"}

    async def _show_once(self, ctx: OutContext) -> None:
        platform = ctx.event.get_platform_name()
        try:
            if platform in {"telegram", "weixin_oc"}:
                await ctx.event.send_typing()
                return

            if platform == "aiocqhttp" and not ctx.gid:
                bot = getattr(ctx.event, "bot", None)
                api = getattr(bot, "api", None)
                if api:
                    await api.call_action("set_input_status", user_id=ctx.uid, event_type=1)
        except Exception:
            logger.debug("[Typing] 发送 typing 失败", exc_info=True)

    async def sleep(self, ctx: OutContext, delay: float) -> None:
        if delay <= 0:
            return

        platform_name = ctx.event.get_platform_name()
        if platform_name not in self.supported_platforms:
            await asyncio.sleep(delay)
            return

        if delay <= 1.0:
            await self._show_once(ctx)
            await asyncio.sleep(delay)
            return

        interval = min(2.5, max(1.0, delay / 3))
        remaining = delay
        while remaining > 0:
            await self._show_once(ctx)
            sleep_time = min(interval, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time


class SplitStep(BaseStep):
    name = StepName.SPLIT
    support_platforms = {"aiocqhttp", "telegram", "lark", "weixin_oc", *OFFICIAL_PLATFORMS}

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.split
        self.tokenizer = TextTokenizer(
            self.cfg.split_re,
            self.cfg.split_priority,
            smart_split=self.cfg.enable_smart_split,
        )
        self.typing = TypingController()

    async def handle(self, ctx: OutContext) -> StepResult:
        platform = ctx.event.get_platform_name()
        if platform not in self.support_platforms:
            return StepResult()

        total_len = self._get_chain_text_length(ctx.chain)
        if self.cfg.min_length > 0 and total_len < self.cfg.min_length:
            return StepResult()
        if self.cfg.max_length > 0 and total_len > self.cfg.max_length:
            logger.debug(
                "[OutputProSplit] 超过分段字数上限，跳过分段：len=%s, limit=%s",
                total_len,
                self.cfg.max_length,
            )
            return StepResult()

        self._clean_chain_before(ctx.chain)
        segments = self._split_chain(ctx.chain, self.cfg.max_count)
        self._postprocess_segments(segments)
        segments = [segment for segment in segments if not segment.is_empty]

        if is_official(ctx.event):
            support = official_support(self.plugin_config)
            chains = support.limit_segments([segment.components for segment in segments])
            ok = await support.deliver(ctx, chains, self._calc_delay)
            return StepResult(ok=ok, abort=not ok, msg="QQ 官方分段完成" if ok else "QQ 官方分段发送失败，已停止后续发送")

        if len(segments) <= 1:
            ctx.chain.clear()
            if segments:
                ctx.chain.extend(segments[0].components)
                await self._refresh_reply(ctx)
            return StepResult()

        logger.debug("[OutputProSplit] 消息被分为 %s 段", len(segments))

        for idx, segment in enumerate(segments[:-1], start=1):
            try:
                segment_ctx = replace(ctx, chain=segment.components)
                await self._refresh_reply(segment_ctx)
                ctx.reply_include_at = segment_ctx.reply_include_at
                materialize_mentions(segment.components)
                await self.plugin_config.context.send_message(
                    ctx.event.unified_msg_origin,
                    MessageChain(segment.components),
                )
                ReplyStep.commit_reply(segment_ctx)
                delay = self._calc_delay(segment.text)
                if self.cfg.show_typing:
                    await self.typing.sleep(ctx, delay)
                else:
                    await asyncio.sleep(delay)
            except Exception as exc:
                logger.error("[OutputProSplit] 第%s段发送失败: %s", idx, exc)

        ctx.chain.clear()
        ctx.chain.extend(segments[-1].components)
        await self._refresh_reply(ctx)
        return StepResult(msg="分段完成")

    async def _refresh_reply(self, ctx: OutContext) -> None:
        await ReplyStep(self.plugin_config).refresh(ctx)

    @staticmethod
    def _get_chain_text_length(chain: list[BaseMessageComponent]) -> int:
        return sum(len(comp.text) for comp in chain if isinstance(comp, Plain))

    def _clean_chain_before(self, chain: list[BaseMessageComponent]) -> None:
        for comp in chain:
            if not isinstance(comp, Plain) or not comp.text:
                continue
            text = comp.text
            for item in self.cfg.clean_before_items:
                if item:
                    text = text.replace(item, "")
            if self.cfg.clean_before_re:
                text = self.cfg.clean_before_re.sub("", text)
            comp.text = text

    def _postprocess_segments(self, segments: list[Segment]) -> None:
        for segment in segments:
            if self.cfg.trim_segment_edge_blank_lines:
                segment.trim_edge_blank_lines()
            segment.lstrip_plain()
            segment.rstrip_plain()
            segment.strip_tail_punc(self.cfg.tail_punc_re)
            segment.apply_text_cleaners(
                items=self.cfg.clean_after_items,
                pattern=self.cfg.clean_after_re,
            )

        self._bind_prefixes(segments)

        if self.cfg.min_segment_length <= 0:
            return

        idx = 1
        while idx < len(segments):
            current = segments[idx]
            if (
                0 < len(current.visible_text) < self.cfg.min_segment_length
                and not current.has_media
            ):
                segments[idx - 1].extend(current.components)
                segments.pop(idx)
                continue
            idx += 1

    @staticmethod
    def _bind_prefixes(segments: list[Segment]) -> None:
        for index, segment in enumerate(segments):
            if segment.visible_text:
                continue
            prefixes = [
                component for component in segment.components
                if isinstance(component, (At, AtAll, MentionText))
                or (segment.is_empty and isinstance(component, Reply))
            ]
            if not prefixes:
                continue
            text_targets = [pos for pos, item in enumerate(segments) if item.visible_text]
            targets = text_targets or [
                pos for pos, item in enumerate(segments) if not item.is_empty
            ]
            if not targets:
                segment.components.clear()
                continue
            target = next((pos for pos in targets if pos > index), targets[-1])
            if target == index:
                continue
            segment.components[:] = [
                component for component in segment.components
                if not any(component is prefix for prefix in prefixes)
            ]
            destination = segments[target].components
            reply_count = 1 if destination and isinstance(destination[0], Reply) else 0
            destination[reply_count:reply_count] = prefixes
        segments[:] = [segment for segment in segments if not segment.is_empty]

    def _calc_delay(self, text: str) -> float:
        strategy = self.cfg.delay_strategy
        if strategy == "random":
            delay = random.uniform(self.cfg.random_min, self.cfg.random_max)
        elif strategy == "log":
            delay = self.cfg.log_base + self.cfg.log_factor * math.log(len(text) + 1)
        elif strategy == "linear":
            delay = self.cfg.linear_base + len(text) * self.cfg.linear_factor
        elif strategy == "fixed":
            delay = self.cfg.fixed_delay
        else:
            cn = self.cfg.per_char_delay
            en = cn / 2
            delay = sum(cn if "\u4e00" <= ch <= "\u9fff" else en for ch in text)

        return max(self.cfg.min_delay, min(self.cfg.max_delay, delay))

    def _select_split_points(self, tokens: list[Token], max_count: int) -> set[int]:
        split_idx = [i for i, token in enumerate(tokens) if token.is_split and token.text.strip()]
        if not split_idx:
            return set()

        if max_count <= 0 or len(split_idx) <= max_count - 1:
            return set(split_idx)

        count = max_count - 1
        if count <= 0:
            return set()

        if not self.cfg.balanced_split_mode:
            return set(split_idx[:count])

        lengths = [token.weight or len(token.text) for token in tokens]
        total = sum(lengths)
        targets = [total * i / max_count for i in range(1, max_count)]

        split_points: list[tuple[int, int, int]] = []
        acc = 0
        for idx, length in enumerate(lengths):
            acc += length
            if tokens[idx].is_split:
                split_points.append((idx, acc, tokens[idx].priority))

        selected: set[int] = set()
        cursor = 0
        window: list[tuple[int, int, int]] = []
        last_acc = 0

        for target in targets:
            while cursor < len(split_points) and split_points[cursor][1] < target:
                window.append(split_points[cursor])
                cursor += 1
            if cursor < len(split_points):
                window.append(split_points[cursor])
                cursor += 1
            if not window:
                break

            candidates = [
                item
                for item in window
                if item[1] - last_acc >= self.cfg.min_segment_length
            ]
            if not candidates:
                candidates = window

            best = min(candidates, key=lambda item: (item[2], abs(item[1] - target), item[0]))
            selected.add(best[0])
            last_acc = best[1]
            window = [item for item in window if item[0] > best[0]]

        return selected

    def _normalize_strategy(self, value: str | None, default: str) -> str:
        return STRATEGY_ALIASES.get(str(value or "").strip(), default)

    @staticmethod
    def _component_text_sources(comp: BaseMessageComponent) -> list[str]:
        sources: list[str] = []
        for attr in ("file", "url", "path"):
            value = getattr(comp, attr, None)
            if value:
                sources.append(str(value))
        return sources

    def _is_stealer_emoji_image(self, comp: Image) -> bool:
        for source in self._component_text_sources(comp):
            normalized = source.replace("\\", "/").lower()
            if any(marker in normalized for marker in STEALER_IMAGE_MARKERS):
                return True
        return False

    def _component_strategy(self, comp: BaseMessageComponent) -> str:
        if isinstance(comp, Reply):
            return "follow_next"
        if isinstance(comp, (At, AtAll, MentionText)):
            return "follow_next"
        if isinstance(comp, Image) and self._is_stealer_emoji_image(comp):
            return self._normalize_strategy(self.cfg.face_strategy, "inline")
        if isinstance(comp, Image):
            return self._normalize_strategy(self.cfg.image_strategy, "standalone")
        if isinstance(comp, Face):
            return self._normalize_strategy(self.cfg.face_strategy, "inline")
        return self._normalize_strategy(self.cfg.other_media_strategy, "follow_next")

    def _append_component(self, builder: SegmentBuilder, comp: BaseMessageComponent) -> None:
        strategy = self._component_strategy(comp)

        if strategy == "standalone":
            builder.flush()
            builder.append([comp])
            builder.flush()
            return

        if strategy == "follow_prev":
            if builder.current.components:
                builder.append([comp])
                builder.flush()
            elif not builder.append_to_previous(comp):
                builder.append([comp])
            return

        if strategy == "follow_next":
            builder.flush()
            builder.add_prefix(comp)
            return

        builder.append([comp])

    def _split_chain(
        self,
        chain: list[BaseMessageComponent],
        max_count: int | None = None,
    ) -> list[Segment]:
        builder = SegmentBuilder()
        count = self.cfg.max_count if max_count is None else max_count

        for comp in chain:
            if isinstance(comp, MentionText):
                self._append_component(builder, comp)
                continue
            if isinstance(comp, Plain):
                text = comp.text or ""
                if not text:
                    continue

                tokens = list(self.tokenizer.tokenize(text))
                selected = self._select_split_points(tokens, count)

                for idx, token in enumerate(tokens):
                    builder.append([Plain(token.text)])
                    if idx in selected:
                        builder.flush()
                continue

            self._append_component(builder, comp)

        return builder.finalize()
