import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.message_components import (
    At,
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
from test_output_flow import Event, make_config, make_context, observe

from core.content import has_payload
from core.model import StateManager
from core.official import official_support
from core.pipeline import Pipeline
from core.step.tts import TTSStep


class OfficialEvent(Event):
    def __init__(
        self, message_id="anchor", *, platform="qq_official", scene="group", **kwargs
    ):
        if scene in {"c2c", "guild_dm"}:
            kwargs["group"] = ""
        super().__init__(message_id, platform=platform, **kwargs)
        source = SimpleNamespace(
            raw_data={"message_scene": {"ext": ["msg_idx=REFIDX_anchor"]}}
        )
        if scene == "group":
            source.group_openid = "group_openid"
        elif scene == "c2c":
            source.user_openid = "user_openid"
        else:
            source.channel_id = "channel"
        self.message_obj.raw_message = source
        self.message_str = "帮我检查这张图片"
        self.result = SimpleNamespace(use_markdown_=False, use_t2i_=False)
        self.send = AsyncMock()

    def get_platform_name(self):
        return self.platform

    def get_result(self):
        return self.result

    @staticmethod
    async def _parse_to_qqofficial(message):
        return ("".join(c.text for c in message.chain if isinstance(c, Plain)),)


class ModernEvent(OfficialEvent):
    @staticmethod
    async def _parse_to_qqofficial(message):
        return (
            "".join(
                f'<qqbot-at-user id="{c.qq}" />' if isinstance(c, At) else c.text
                for c in message.chain
                if isinstance(c, (At, Plain))
            ),
        )

    @staticmethod
    def _extract_self_ref_idx(source):
        for item in source.raw_data.get("message_scene", {}).get("ext", []):
            if item.startswith("msg_idx="):
                return item.removeprefix("msg_idx=")
        return ""


@pytest.fixture(autouse=True)
def isolated_state():
    StateManager._groups.clear()
    yield
    StateManager._groups.clear()


def output_chains(event, ctx):
    return [call.args[0].chain for call in event.send.call_args_list] + [ctx.chain]


def text(chain):
    return "".join(c.text for c in chain if isinstance(c, Plain))


@pytest.mark.parametrize("platform", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("scene", ["group", "c2c", "guild", "guild_dm"])
def test_official_segments_keep_event_and_format(platform, scene):
    async def scenario():
        event = observe(OfficialEvent(platform=platform, scene=scene))
        config = make_config("split")
        config.qq_official.message_format = "plain"
        ctx = make_context(
            event, [Plain(text="第一句。第二句。第三句。第四句。第五句。")]
        )
        await Pipeline(config).run(ctx)
        chains = output_chains(event, ctx)
        assert len(chains) == 3
        assert (
            text([c for chain in chains for c in chain]).replace("\n", "")
            == "第一句。第二句。第三句。第四句。第五句。"
        )
        assert all(has_payload(chain) for chain in chains)
        config.context.send_message.assert_not_called()
        assert all(
            call.args[0].use_markdown_ is False for call in event.send.call_args_list
        )
        assert event.result.use_markdown_ is False

    asyncio.run(scenario())


@pytest.mark.parametrize("steps", [(), ("split",)])
def test_media_preserved_even_if_text_split_disabled_or_skipped(steps):
    async def scenario():
        event = OfficialEvent()
        config = make_config(*steps)
        config.split.min_length = 1000
        media = [
            Image(file="a.jpg"),
            Image(file="b.jpg"),
            Record(file="voice.wav"),
            Video(file="v.mp4"),
            File(file="f.txt", name="file"),
        ]
        ctx = make_context(event, [Plain(text="媒体说明"), *media])
        await Pipeline(config).run(ctx)
        chains = output_chains(event, ctx)
        assert len(chains) == 5
        assert [
            c
            for chain in chains
            for c in chain
            if isinstance(c, (Image, Record, Video, File))
        ] == media
        assert all(
            sum(isinstance(c, (Image, Record, Video, File)) for c in chain) <= 1
            for chain in chains
        )
        assert text(chains[0]) == "媒体说明"

    asyncio.run(scenario())


@pytest.mark.parametrize("event_class", [OfficialEvent, ModernEvent])
@pytest.mark.parametrize("include_at", [False, True])
@pytest.mark.parametrize("order", [("at", "reply", "split"), ("reply", "at", "split")])
def test_smart_reply_beats_at_probability(event_class, include_at, order, monkeypatch):
    async def scenario():
        event = observe(event_class())
        observe(event_class("interrupt"))
        config = make_config(*order, at_prob=0.5)
        config.pipeline.lock_order = False
        config.reply.include_at = include_at
        monkeypatch.setattr(
            "core.step.at.random.random", lambda: 0.99 if include_at else 0
        )
        ctx = make_context(event, [Plain(text="已经找到原因。")])
        await Pipeline(config).run(ctx)
        has_at = any(isinstance(c, At) for c in ctx.chain) or "@Alice Smith" in text(
            ctx.chain
        )
        assert has_at is include_at
        if event_class is ModernEvent:
            assert [c.id for c in ctx.chain if isinstance(c, Reply)] == [
                "REFIDX_anchor"
            ]
        else:
            assert not any(isinstance(c, Reply) for c in ctx.chain)
            assert "回复「帮我检查这张图片」" in text(ctx.chain)
        assert "anchor" in ctx.group.reply_last_sequences

    asyncio.run(scenario())


def test_native_reference_missing_or_wrong_anchor_never_uses_robot_id():
    async def scenario():
        config = make_config()
        event = ModernEvent()
        event.message_obj.raw_message.raw_data = {}
        ctx = make_context(event, [Reply(id="anchor"), Plain(text="答复")])
        await Pipeline(config).run(ctx)
        assert not any(isinstance(c, Reply) for c in ctx.chain)
        event.message_obj.raw_message.raw_data = {
            "message_scene": {"ext": ["msg_idx=REFIDX_anchor"]}
        }
        ctx = make_context(
            event, [Reply(id="unrelated", message_str="其他消息"), Plain(text="答复")]
        )
        await Pipeline(config).run(ctx)
        assert "回复「其他消息」" in text(ctx.chain)
        assert not any(isinstance(c, Reply) for c in ctx.chain)

    asyncio.run(scenario())


def test_skip_unavailable_quote_does_not_override_probability():
    async def scenario():
        event = observe(OfficialEvent())
        observe(OfficialEvent("interrupt"))
        config = make_config("at", "reply")
        config.qq_official.reply_fallback = "skip"
        config.reply.include_at = False
        ctx = make_context(event, [Plain(text="正文")])
        await Pipeline(config).run(ctx)
        assert "@Alice Smith" in text(ctx.chain)
        assert ctx.reply_include_at is None
        assert not ctx.group.reply_last_sequences

    asyncio.run(scenario())


def test_quote_refresh_during_official_send():
    async def scenario():
        event = observe(ModernEvent())
        config = make_config("reply", "split")
        config.reply.include_at = False
        config.qq_official.max_text_segments = 0
        event.send.side_effect = lambda message: observe(ModernEvent("interrupt"))
        ctx = make_context(event, [Plain(text="第一句。第二句。")])
        await Pipeline(config).run(ctx)
        assert not any(isinstance(c, Reply) for c in event.send.call_args.args[0].chain)
        assert [c.id for c in ctx.chain if isinstance(c, Reply)] == ["REFIDX_anchor"]

    asyncio.run(scenario())


def test_official_failure_stops_tail_without_duplicate_fallback():
    async def scenario():
        event = OfficialEvent()
        event.send.side_effect = RuntimeError("quota exceeded")
        config = make_config("split")
        ctx = make_context(event, [Plain(text="第一句。第二句。第三句。")])
        assert await Pipeline(config).run(ctx) is False
        assert ctx.chain == []
        assert event.send.call_count == 1
        config.context.send_message.assert_not_called()

    asyncio.run(scenario())


def test_guild_unsupported_media_falls_back_with_original_tts_text():
    async def scenario():
        event = OfficialEvent(scene="guild")
        config = make_config()
        ctx = make_context(
            event,
            [
                Record(file="voice.wav", text="这段语音的文字"),
                Video(file="v.mp4"),
                File(file="f.txt", name="file"),
                Face(id=1),
            ],
        )
        await Pipeline(config).run(ctx)
        assert all(isinstance(c, Plain) for c in ctx.chain)
        assert "这段语音的文字" in text(ctx.chain)
        assert (
            "视频" in text(ctx.chain)
            and "文件" in text(ctx.chain)
            and "表情" in text(ctx.chain)
        )

    asyncio.run(scenario())


def test_forward_nodes_flatten_without_losing_images():
    async def scenario():
        event = OfficialEvent()
        config = make_config()
        nodes = Nodes(
            [
                Node(
                    name="作者",
                    content=[
                        Plain(text="转发正文"),
                        Image(file="a.jpg"),
                        Image(file="b.jpg"),
                    ],
                )
            ]
        )
        ctx = make_context(event, [nodes])
        await Pipeline(config).run(ctx)
        chains = output_chains(event, ctx)
        assert len(chains) == 2
        assert "转发正文" in text(chains[0])
        assert sum(isinstance(c, Image) for chain in chains for c in chain) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,expected", [("auto", False), ("plain", False), ("markdown", True)]
)
def test_message_format_is_inherited_by_all_packets(mode, expected):
    async def scenario():
        event = OfficialEvent()
        config = make_config()
        config.qq_official.message_format = mode
        ctx = make_context(event, [Image(file="a.jpg"), Image(file="b.jpg")])
        await Pipeline(config).run(ctx)
        assert event.send.call_args.args[0].use_markdown_ is expected
        assert event.result.use_markdown_ is expected

    asyncio.run(scenario())


def test_private_state_isolation():
    first = OfficialEvent(scene="c2c", sender="user_a")
    second = OfficialEvent(scene="c2c", sender="user_b")
    same = OfficialEvent(scene="c2c", sender="user_a")
    assert StateManager.get_event_group(first) is not StateManager.get_event_group(
        second
    )
    assert StateManager.get_event_group(first) is StateManager.get_event_group(same)


def test_delay_is_capped(monkeypatch):
    async def scenario():
        event = OfficialEvent()
        config = make_config("split")
        config.split.fixed_delay = 20
        config.split.min_delay = 20
        config.split.max_delay = 20
        config.qq_official.max_delay = 0.5
        sleep = AsyncMock()
        monkeypatch.setattr("core.official.asyncio.sleep", sleep)
        ctx = make_context(event, [Plain(text="第一句。第二句。第三句。")])
        await Pipeline(config).run(ctx)
        assert sleep.await_count == 2
        assert all(call.args == (0.5,) for call in sleep.call_args_list)

    asyncio.run(scenario())


def test_error_replacement_is_the_only_content_seen_downstream():
    async def scenario():
        event = OfficialEvent()
        config = make_config("error", "split")
        config.error.custom_msg = "稍后再试。"
        config.error.forward_umo = ""
        ctx = make_context(event, [Plain(text="请求失败。保密的堆栈信息。")])
        ctx.plain = text(ctx.chain)
        await Pipeline(config).run(ctx)
        assert "请求失败" not in text(
            [c for chain in output_chains(event, ctx) for c in chain]
        )
        assert text(ctx.chain) == "稍后再试。"

    asyncio.run(scenario())


def test_official_tts_uses_session_provider_and_preserves_prefixes():
    async def scenario():
        event = ModernEvent(scene="c2c")
        config = make_config("tts")
        config.tts.tts_provider_id = ""
        config.tts.threshold = 100
        config.tts.prob = 1
        provider = SimpleNamespace(get_audio=AsyncMock(return_value="audio.wav"))
        config.context.get_using_tts_provider = Mock(return_value=provider)
        ctx = make_context(
            event,
            [Reply(id="REFIDX_existing"), At(qq="user_openid"), Plain(text="语音正文")],
        )
        await Pipeline(config).run(ctx)
        provider.get_audio.assert_awaited_once_with("语音正文")
        config.context.get_using_tts_provider.assert_called_once_with(
            umo=event.unified_msg_origin
        )
        assert any(isinstance(c, Record) for c in ctx.chain)
        assert any(isinstance(c, At) for c in ctx.chain)
        assert any(
            isinstance(c, Reply) and c.id == "REFIDX_existing" for c in ctx.chain
        )

    asyncio.run(scenario())


def test_guild_tts_keeps_text_and_does_not_invoke_provider():
    async def scenario():
        event = OfficialEvent(scene="guild")
        config = make_config("tts")
        config.context.get_using_tts_provider = Mock()
        ctx = make_context(event, [Plain(text="原文保留")])
        result = await TTSStep(config).handle(ctx)
        assert result.ok
        assert text(ctx.chain) == "原文保留"
        config.context.get_using_tts_provider.assert_not_called()

    asyncio.run(scenario())


def test_empty_tool_result_stays_empty_on_official():
    async def scenario():
        event = OfficialEvent()
        config = make_config("at", "reply", "split")
        ctx = make_context(event, [Plain(text="\u200b \n")])
        await Pipeline(config).run(ctx)
        assert ctx.chain == []
        event.send.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("scene", ["group", "c2c", "guild", "guild_dm"])
def test_installed_sdk_sends_real_payload_with_original_anchor(
    scene, tmp_path, monkeypatch
):
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message

    async def scenario():
        monkeypatch.setattr(
            "astrbot.core.message.components.get_astrbot_temp_path",
            lambda: str(tmp_path),
        )
        monkeypatch.setattr(
            "astrbot.core.platform.astr_message_event.Metric.upload", AsyncMock()
        )
        api = SimpleNamespace(
            post_group_message=AsyncMock(return_value={"id": "sent"}),
            post_message=AsyncMock(return_value={"id": "sent"}),
            post_dms=AsyncMock(return_value={"id": "sent"}),
        )
        data = {
            "id": "ROBOT1.0_original",
            "group_openid": "group_openid",
            "channel_id": "channel",
            "guild_id": "guild",
            "author": {"id": "uid", "user_openid": "uid", "member_openid": "uid"},
        }
        source_type = {
            "group": GroupMessage,
            "c2c": C2CMessage,
            "guild": Message,
            "guild_dm": DirectMessage,
        }[scene]
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE
            if scene in {"group", "guild"}
            else MessageType.FRIEND_MESSAGE
        )
        message.self_id = "bot"
        message.group_id = "group" if scene in {"group", "guild"} else ""
        message.message_id = data["id"]
        message.sender = MessageMember(user_id="uid", nickname="Alice")
        message.message = [Plain(text="request")]
        message.message_str = "request"
        message.raw_message = source_type(api, "event", data)
        event = QQOfficialMessageEvent(
            "request",
            message,
            PlatformMetadata(name="qq_official", description="test", id="official"),
            session_id="session",
            bot=SimpleNamespace(api=api),
        )
        event.upload_group_and_c2c_image = AsyncMock(
            return_value={"file_info": "image"}
        )
        event.post_c2c_message = AsyncMock(return_value={"id": "sent"})
        chain = [
            At(qq="uid", name="Alice"),
            Plain(text="答案"),
            Image.fromBase64("aW1hZ2Ux"),
            Image.fromBase64("aW1hZ2Uy"),
        ]
        event.set_result(event.chain_result(chain))
        config = make_config()
        config.qq_official.message_format = "plain"
        ctx = make_context(event, event.get_result().chain)
        await Pipeline(config).run(ctx)
        await event.send(official_support(config).message(ctx, ctx.chain))
        sender = {
            "group": api.post_group_message,
            "c2c": event.post_c2c_message,
            "guild": api.post_message,
            "guild_dm": api.post_dms,
        }[scene]
        assert sender.await_count == 2
        payloads = [call.kwargs for call in sender.call_args_list]
        assert all(payload["msg_id"] == "ROBOT1.0_original" for payload in payloads)
        assert "答案" in payloads[0]["content"]
        assert "Alice" in payloads[0]["content"]
        if scene in {"group", "c2c"}:
            assert all(
                payload["msg_type"] == 7 and payload["media"] for payload in payloads
            )
        else:
            assert all(payload["file_image"] for payload in payloads)
            assert all("msg_type" not in payload for payload in payloads)
        event.cleanup_temporary_local_files()

    asyncio.run(scenario())


def test_guild_image_quote_uses_text_instead_of_dropped_sdk_reference(monkeypatch):
    async def scenario():
        event = observe(ModernEvent(scene="guild"))
        observe(ModernEvent("interrupt", scene="guild"))
        config = make_config("reply")
        converter = AsyncMock(return_value="D:/CodexData/Temp/official-image.jpg")
        monkeypatch.setattr(Image, "convert_to_file_path", converter)
        ctx = make_context(event, [Image(file="image.jpg")])
        await Pipeline(config).run(ctx)
        assert not any(isinstance(c, Reply) for c in ctx.chain)
        assert "回复「" in text(ctx.chain)
        assert any(
            isinstance(c, Image) and c.file.startswith("file:///") for c in ctx.chain
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("at_str", [False, True])
def test_fake_openid_mention_resolves_without_numeric_conversion(at_str):
    async def scenario():
        sender = "AABBCC00112233445566778899ZZYYXX"
        event = observe(ModernEvent(sender=sender))
        config = make_config("at", at_str=at_str, at_prob=0)
        ctx = make_context(event, [Plain(text=f"[at:{sender}] 正文")])
        await Pipeline(config).run(ctx)
        if at_str:
            assert "@Alice Smith" in text(ctx.chain)
        else:
            assert [c.qq for c in ctx.chain if isinstance(c, At)] == [sender]
        assert "正文" in text(ctx.chain)

    asyncio.run(scenario())


def test_unknown_qq_number_is_not_guessed_as_an_openid():
    async def scenario():
        event = OfficialEvent()
        config = make_config("at", at_prob=0)
        ctx = make_context(event, [Plain(text="@2127074778 正文")])
        await Pipeline(config).run(ctx)
        assert text(ctx.chain) == "@2127074778 正文"

    asyncio.run(scenario())


def test_official_recall_does_not_claim_to_schedule_a_task():
    async def scenario():
        event = OfficialEvent()
        config = make_config("recall")
        config.recall.keywords = ["测试"]
        pipeline = Pipeline(config)
        ctx = make_context(event, [Plain(text="测试文字")])
        await pipeline.run(ctx)
        assert text(ctx.chain) == "测试文字"
        assert pipeline._steps[0].recall_tasks == []
        event.send.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("limit,expected", [(0, 5), (1, 1), (2, 2)])
def test_soft_segment_limit_preserves_all_text(limit, expected):
    async def scenario():
        event = OfficialEvent()
        config = make_config("split")
        config.qq_official.max_text_segments = limit
        original = "第一句。第二句。第三句。第四句。第五句。"
        ctx = make_context(event, [Plain(text=original)])
        await Pipeline(config).run(ctx)
        chains = output_chains(event, ctx)
        assert len(chains) == expected
        assert "".join(text(chain) for chain in chains).replace("\n", "") == original

    asyncio.run(scenario())


def test_generated_guild_images_are_cleaned_without_deleting_original(
    tmp_path, monkeypatch
):
    async def scenario():
        generated = tmp_path / "generated.jpg"
        original = tmp_path / "original.jpg"
        generated.write_bytes(b"image")
        original.write_bytes(b"image")
        converter = AsyncMock(side_effect=[str(generated), str(original)])
        monkeypatch.setattr(Image, "convert_to_file_path", converter)
        event = OfficialEvent(scene="guild")
        config = make_config()
        pipeline = Pipeline(config)
        ctx = make_context(event, [Image.fromBase64("aW1hZ2U=")])
        await pipeline.run(ctx)
        ctx = make_context(event, [Image(file=str(original))])
        await pipeline.run(ctx)
        await pipeline.terminate()
        assert not generated.exists()
        assert original.exists()

    asyncio.run(scenario())
