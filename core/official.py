"""QQ Official output compatibility, without replacing the core adapter."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)

from .content import MentionText, has_payload, materialize_mentions

if TYPE_CHECKING:
    from .config import PluginConfig
    from .model import OutContext

OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
MEDIA = (Image, Record, Video, File)


def is_official(event) -> bool:
    return event.get_platform_name() in OFFICIAL_PLATFORMS


def is_guild(event) -> bool:
    source = getattr(event.message_obj, "raw_message", None)
    names = {cls.__name__ for cls in type(source).__mro__}
    if names & {"GroupMessage", "C2CMessage"}:
        return False
    if getattr(source, "group_openid", None) or getattr(source, "user_openid", None):
        return False
    return bool(
        names & {"Message", "DirectMessage"} or getattr(source, "channel_id", None)
    )


def mention_name(event, user_id: str, nickname: str = "") -> str:
    name = str(nickname or "").strip()
    # Official events may only expose an opaque OpenID, not a public nickname.
    if not name or name == str(user_id):
        return "用户"
    return name


class OfficialSupport:
    def __init__(self, config: "PluginConfig"):
        self.config = config
        self.cfg = config.qq_official
        self._at_support: dict[type, bool] = {}
        self._warnings: set[str] = set()
        self._temp_images: set[str] = set()

    async def terminate(self) -> None:
        for path in self._temp_images:
            try:
                await asyncio.to_thread(Path(path).unlink, missing_ok=True)
            except OSError as exc:
                logger.warning("[OutputProSplit][QQOfficial] 清理临时图片失败: %s", exc)
        self._temp_images.clear()

    def trace(self, message: str, *args) -> None:
        if self.config.pipeline.debug_logging:
            logger.info("[OutputProSplit][QQOfficial] " + message, *args)

    def warn_once(self, key: str, message: str) -> None:
        if key not in self._warnings:
            self._warnings.add(key)
            logger.warning("[OutputProSplit][QQOfficial] " + message)

    async def _supports_at(self, event) -> bool:
        event_type = type(event)
        if event_type not in self._at_support:
            parser = getattr(event_type, "_parse_to_qqofficial", None)
            supported = False
            if callable(parser):
                try:
                    # An At-only parse cannot upload media or call the QQ send API.
                    parsed = await parser(MessageChain([At(qq="outputpro_probe")]))
                    supported = bool(parsed and "outputpro_probe" in str(parsed[0]))
                except (AttributeError, IndexError, TypeError, ValueError):
                    self.trace("无法确认原生艾特能力，使用可见文字降级")
            self._at_support[event_type] = supported
        return self._at_support[event_type]

    @staticmethod
    def _reference_id(event, reply: Reply) -> str:
        extractor = getattr(type(event), "_extract_self_ref_idx", None)
        if not callable(extractor):
            return ""
        if str(reply.id).startswith("REFIDX_"):
            return str(reply.id)
        if str(reply.id) != str(event.message_obj.message_id):
            return ""
        source = getattr(event.message_obj, "raw_message", None)
        ref_id = str(extractor(source) or "")
        return ref_id if ref_id.startswith("REFIDX_") else ""

    def can_reply(self, ctx: "OutContext") -> bool:
        if self.cfg.reply_fallback == "text":
            return True
        if is_guild(ctx.event) and any(isinstance(c, Image) for c in ctx.chain):
            return False
        return bool(
            self._reference_id(ctx.event, Reply(id=ctx.event.message_obj.message_id))
        )

    def expand(self, ctx: "OutContext", chain: list, depth: int = 0) -> list:
        expanded = []
        for component in chain:
            if isinstance(component, (Nodes, Node)) and depth < 8:
                nodes = component.nodes if isinstance(component, Nodes) else [component]
                self.trace("合并转发降级为展开内容")
                for node in nodes:
                    expanded.append(Plain(text=f"\n{node.name or '消息'}：\n"))
                    expanded.extend(self.expand(ctx, node.content, depth + 1))
            elif is_guild(ctx.event) and isinstance(component, (Record, Video, File)):
                label = (
                    "语音"
                    if isinstance(component, Record)
                    else "视频"
                    if isinstance(component, Video)
                    else "文件"
                )
                text = (
                    getattr(component, "text", "")
                    if isinstance(component, Record)
                    else ""
                )
                expanded.append(
                    Plain(text=text or f"[{label}：当前频道适配器不支持发送]")
                )
                self.warn_once(
                    "guild_media",
                    "频道/频道私信适配器不支持此语音、视频或文件，保留文字说明；图片仍正常发送。",
                )
            elif isinstance(component, Face):
                expanded.append(Plain(text=f"[表情:{component.id}]"))
                self.trace("QQ Face 表情降级为文字，图片表情包不受影响")
            elif isinstance(component, (Plain, At, Reply, *MEDIA)):
                expanded.append(component)
            else:
                expanded.append(
                    Plain(text=f"[不支持的消息组件：{type(component).__name__}]")
                )
                self.warn_once(
                    type(component).__name__,
                    f"官方适配器不支持 {type(component).__name__}，已替换为可见说明。",
                )
        return expanded

    @staticmethod
    def packets(chain: list[BaseMessageComponent]) -> list[list[BaseMessageComponent]]:
        """One media per call also protects AstrBot versions that overwrite media."""
        packets = []
        current = []
        has_media = False
        for component in chain:
            if isinstance(component, MEDIA):
                if has_media:
                    packets.append(current)
                    current = []
                has_media = True
            current.append(component)
        if current:
            packets.append(current)
        return [packet for packet in packets if has_payload(packet)]

    def limit_segments(self, segments: list[list]) -> list[list]:
        limit = max(0, int(self.cfg.max_text_segments))
        if not limit or len(segments) <= limit:
            return segments
        tail = []
        for segment in segments[limit - 1 :]:
            if tail:
                tail.append(Plain(text="\n"))
            tail.extend(segment)
        self.trace("限制文本分段：%s -> %s；媒体仍逐条保留", len(segments), limit)
        return segments[: limit - 1] + [tail]

    async def prepare(self, ctx: "OutContext") -> list:
        prepared = []
        for component in ctx.chain:
            if isinstance(component, Reply):
                ref_id = self._reference_id(ctx.event, component)
                # botpy drops message_reference when guild file_image is present.
                image_quote = is_guild(ctx.event) and any(
                    isinstance(c, Image) for c in ctx.chain
                )
                if ref_id and not image_quote:
                    prepared.append(Reply(id=ref_id))
                    self.trace("使用原生引用 REFIDX")
                elif self.cfg.reply_fallback == "text":
                    original = str(component.id) == str(
                        ctx.event.message_obj.message_id
                    )
                    text = (
                        getattr(ctx.event, "message_str", "")
                        if original
                        else component.message_str
                    )
                    text = " ".join((text or "").split())
                    excerpt = text[:79] + "…" if len(text) > 80 else text
                    prepared.append(Plain(text=f"回复「{excerpt or '原消息'}」：\n"))
                    self.trace("没有可用原生引用，使用文字摘要降级")
                else:
                    self.trace("没有可用原生引用，按配置跳过引用组件")
            elif isinstance(component, AtAll):
                prepared.append(Plain(text="@全体成员 "))
                self.trace("AtAll 降级为文字，不发送全体通知")
            elif isinstance(component, At):
                if await self._supports_at(ctx.event):
                    prepared.append(component)
                else:
                    name = component.name or (
                        ctx.event.get_sender_name()
                        if str(component.qq) == str(ctx.uid)
                        else ""
                    )
                    prepared.append(
                        MentionText(
                            text=f"@{mention_name(ctx.event, str(component.qq), name)} "
                        )
                    )
                    self.trace("旧适配器不支持原生艾特，使用文字降级（不通知用户）")
            elif isinstance(component, Image) and is_guild(ctx.event):
                # Older adapters only set guild file_image for explicit file URIs/URLs.
                source = component.url or component.file or ""
                path = await component.convert_to_file_path()
                prepared.append(Image.fromFileSystem(path))
                if source.startswith(("http://", "https://", "base64://")):
                    tracker = getattr(ctx.event, "track_temporary_local_file", None)
                    if callable(tracker):
                        tracker(path)
                    else:
                        self._temp_images.add(path)
            else:
                prepared.append(component)
        materialize_mentions(prepared)
        return prepared

    def message(self, ctx: "OutContext", chain: list) -> MessageChain:
        result = getattr(ctx.event, "get_result", lambda: None)()
        packet = MessageChain(chain=chain)
        packet.use_t2i_ = getattr(result, "use_t2i_", None)
        packet.use_markdown_ = getattr(result, "use_markdown_", None)
        mode = self.cfg.message_format
        if mode in {"plain", "markdown"}:
            packet.use_markdown_ = mode == "markdown"
        return packet

    async def deliver(
        self, ctx: "OutContext", segments: list[list], delay=None
    ) -> bool:
        from .step.reply import ReplyStep

        packets = [
            packet
            for segment in segments
            for packet in self.packets(self.expand(ctx, segment))
        ]
        if not packets:
            ctx.chain.clear()
            return True
        reply = ReplyStep(self.config)
        for index, packet in enumerate(packets[:-1]):
            segment_ctx = replace(ctx, chain=packet)
            await reply.refresh(segment_ctx)
            ctx.reply_include_at = segment_ctx.reply_include_at
            try:
                prepared = await self.prepare(segment_ctx)
                # Session sends can select a newer cached msg_id; keep the triggering event.
                await ctx.event.send(self.message(ctx, prepared))
            except Exception as exc:  # noqa: BLE001 - adapter exceptions vary by SDK version
                self.warn_once(
                    "send_failure",
                    "官方分段发送失败时会停止后续发送，不自动重试，避免未知送达状态导致重复消息；请查看下方异常。",
                )
                logger.error(
                    "[OutputProSplit][QQOfficial] 第%s/%s条发送失败，停止后续发送: %s",
                    index + 1,
                    len(packets),
                    exc,
                    exc_info=True,
                )
                ctx.chain.clear()
                return False
            reply.commit_reply(segment_ctx)
            self.trace("已发送第%s/%s条，保留触发事件 msg_id", index + 1, len(packets))
            if delay:
                text = "".join(c.text for c in packet if isinstance(c, Plain))
                seconds = min(
                    max(0.0, delay(text)), max(0.0, float(self.cfg.max_delay))
                )
                await asyncio.sleep(seconds)
        ctx.chain[:] = packets[-1]
        await reply.refresh(ctx)
        return True

    async def finalize(self, ctx: "OutContext") -> bool:
        if not await self.deliver(ctx, [ctx.chain.copy()]):
            return False
        from .step.reply import ReplyStep

        ReplyStep.commit_reply(ctx)
        ctx.chain[:] = await self.prepare(ctx)
        result = getattr(ctx.event, "get_result", lambda: None)()
        if result is not None and self.cfg.message_format in {"plain", "markdown"}:
            result.use_markdown_ = self.cfg.message_format == "markdown"
        return True


def official_support(config: "PluginConfig") -> OfficialSupport:
    support = getattr(config, "_official_support", None)
    if support is None:
        support = OfficialSupport(config)
        config._official_support = support
    return support
