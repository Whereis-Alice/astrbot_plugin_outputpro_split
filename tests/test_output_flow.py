import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.message_components import At, Image, Plain, Reply

from core.config import PluginConfig
from core.content import MentionText, has_payload
from core.model import REPLY_EVENT_SEQUENCE_EXTRA, OutContext, StateManager
from core.pipeline import Pipeline
from core.step.at import AtStep
from core.step.reply import ReplyStep
from core.traffic import GroupTrafficFilter


class Event:
    def __init__(
        self,
        message_id,
        *,
        platform="qq",
        bot="99",
        group="123",
        sender="42",
        chain=None,
    ):
        self.message_obj = SimpleNamespace(
            message_id=message_id, message=chain or [], timestamp=0
        )
        self.platform = platform
        self.bot_id = bot
        self.group_id = group
        self.sender = sender
        self.extras = {}
        self.unified_msg_origin = f"{platform}:GroupMessage:{group}"

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return self.bot_id

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "Alice Smith"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


@pytest.fixture(autouse=True)
def isolated_state():
    StateManager._groups.clear()
    yield
    StateManager._groups.clear()


def observe(event):
    assert GroupTrafficFilter().filter(event, {}) is False
    return event


def make_context(event, chain):
    return OutContext(
        event=event,
        chain=chain,
        is_llm=True,
        plain="",
        gid=event.get_group_id(),
        uid=event.get_sender_id(),
        bid=event.get_self_id(),
        group=StateManager.get_event_group(event),
        timestamp=0,
    )


def make_config(*steps, at_str=False, at_prob=1):
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    data = {
        section: {key: value["default"] for key, value in node["items"].items()}
        for section, node in schema.items()
    }
    data["pipeline"].update(steps=list(steps), llm_steps=[], debug_logging=False)
    data["at"].update(at_str=at_str, at_prob=at_prob)
    data["split"].update(
        min_segment_length=0,
        max_length=0,
        max_count=0,
        balanced_split_mode=False,
        delay_strategy="fixed",
        fixed_delay=0,
        delay_scope_str="0~0",
        tail_punc=[],
        show_typing=False,
    )
    context = SimpleNamespace(get_config=dict, send_message=AsyncMock())
    return PluginConfig(data, context)


@pytest.mark.parametrize("at_str", [False, True])
def test_tool_empty_results_do_not_send_or_consume_quote(monkeypatch, at_str):
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("photo", chain=[Image(file="photo.jpg")]))
        config = make_config("at", "reply", "split", at_str=at_str)
        pipeline = Pipeline(config)
        draw = Mock(return_value=0)
        monkeypatch.setattr("core.step.at.random.random", draw)
        for text in ("", " ", "\n", "\u200b \ufeff"):
            ctx = make_context(event, [Plain(text=text)])
            await pipeline.run(ctx)
            assert ctx.chain == []
        assert not draw.called
        assert not config.context.send_message.called
        assert not StateManager.get_event_group(event).reply_last_sequences
        final = make_context(event, [Plain(text="问题已经查明。")])
        await pipeline.run(final)
        assert isinstance(final.chain[0], Reply)
        assert has_payload(final.chain)
        assert draw.call_count == 1
        assert any(isinstance(item, At) for item in final.chain) is (not at_str)
        if at_str:
            assert any(
                isinstance(item, Plain)
                and "@Alice Smith" in item.text
                and "问题" in item.text
                for item in final.chain
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_strategy", ["单独", "跟随下段", "跟随上段", "嵌入"])
@pytest.mark.parametrize("at_str", [False, True])
def test_mentions_stay_with_text_across_image_and_blank_segments(
    legacy_strategy, at_str
):
    async def scenario():
        event = observe(Event("anchor"))
        config = make_config("at", "split", at_str=at_str)
        config.split._data["at_strategy"] = legacy_strategy
        config.split.split_re = re.compile(r"[。\n\s]+")
        ctx = make_context(
            event,
            [
                Plain(text="\n\u200b"),
                Image(file="photo.jpg"),
                Plain(text="查到原因。已经解决。"),
            ],
        )
        await Pipeline(config).run(ctx)
        sent = [
            call.args[1].chain for call in config.context.send_message.call_args_list
        ] + [ctx.chain]
        assert any(any(isinstance(item, Image) for item in chain) for chain in sent)
        mention_chains = [
            chain
            for chain in sent
            if any(
                isinstance(item, At)
                or (isinstance(item, Plain) and "@Alice Smith" in item.text)
                for item in chain
            )
        ]
        assert len(mention_chains) == 1
        assert any(
            isinstance(item, Plain) and "查到原因" in item.text
            for item in mention_chains[0]
        )
        assert all(has_payload(chain) for chain in sent)
        assert not any(
            isinstance(item, MentionText) for chain in sent for item in chain
        )

    asyncio.run(scenario())


def test_legacy_zero_width_at_prefix_does_not_become_a_bubble():
    async def scenario():
        event = observe(Event("anchor"))
        config = make_config("split")
        config.split.split_re = re.compile(r"[\n。]+")
        ctx = make_context(
            event,
            [
                Reply(id="original"),
                At(qq="42"),
                Plain(text="\u200b\n"),
                Image(file="photo.jpg"),
                Plain(text="正文。下一句。"),
            ],
        )
        await Pipeline(config).run(ctx)
        sent = [
            call.args[1].chain for call in config.context.send_message.call_args_list
        ] + [ctx.chain]
        assert all(has_payload(chain) for chain in sent)
        assert any(
            any(isinstance(item, At) for item in chain)
            and any(isinstance(item, Plain) and "正文" in item.text for item in chain)
            for chain in sent
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("steps", [("at", "reply"), ("reply", "at")])
def test_reply_include_at_respects_probability_miss(monkeypatch, steps):
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        config = make_config(*steps, at_prob=0.5)
        config.pipeline.lock_order = False
        config.reply.include_at = True
        monkeypatch.setattr("core.step.reply.AiocqhttpMessageEvent", Event)
        monkeypatch.setattr("core.step.at.random.random", lambda: 0.5)
        ctx = make_context(event, [At(qq="42"), Plain(text="正文")])
        await Pipeline(config).run(ctx)
        assert isinstance(ctx.chain[0], Reply)
        assert not any(isinstance(item, At) for item in ctx.chain)

    asyncio.run(scenario())


def test_zero_probability_preserves_legacy_reply_include_at(monkeypatch):
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        config = make_config("at", "reply", at_prob=0)
        config.reply.include_at = True
        monkeypatch.setattr("core.step.reply.AiocqhttpMessageEvent", Event)
        ctx = make_context(event, [At(qq="42"), Plain(text="正文")])
        await Pipeline(config).run(ctx)
        assert len([item for item in ctx.chain if isinstance(item, At)]) == 1

    asyncio.run(scenario())


def test_unknown_nickname_is_not_deleted_or_turned_into_invalid_at():
    async def scenario():
        ctx = make_context(observe(Event("anchor")), [Plain(text="[at:unknown] 正文")])
        await AtStep(make_config("at", at_prob=0)).handle(ctx)
        assert ctx.chain[0].text == "[at:unknown] 正文"
        assert not any(isinstance(item, At) for item in ctx.chain)

    asyncio.run(scenario())


def test_cleaning_away_body_does_not_leave_mention_or_consume_quote():
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        config = make_config("at", "clean", "reply", "split")
        ctx = make_context(event, [Plain(text="[hidden]")])
        await Pipeline(config).run(ctx)
        assert ctx.chain == []
        assert not ctx.group.reply_last_sequences
        assert not config.context.send_message.called

    asyncio.run(scenario())


def test_count_images_before_processing_without_waking_and_deduplicate():
    anchor = observe(Event("anchor"))
    photo = observe(Event("photo", chain=[Image(file="photo.jpg")]))
    observe(photo)
    duplicate = observe(Event("photo"))
    group = StateManager.get_event_group(anchor)
    assert group.reply_message_seq == 2
    assert duplicate.get_extra(REPLY_EVENT_SEQUENCE_EXTRA) == 2
    ctx = make_context(anchor, [Plain(text="回复")])
    asyncio.run(ReplyStep(make_config("reply")).handle(ctx))
    assert isinstance(ctx.chain[0], Reply)


def test_traffic_is_isolated_by_platform_bot_and_group():
    anchor = observe(Event("anchor"))
    observe(Event("other", platform="other"))
    observe(Event("other", bot="88"))
    observe(Event("other", group="456"))
    observe(Event("self", sender=99))
    observe(Event("private", group=""))
    assert StateManager.get_event_group(anchor).reply_message_seq == 1


def test_quote_anchor_survives_more_than_recent_queue_length():
    event = observe(Event("anchor"))
    for index in range(250):
        observe(Event(str(index)))
    ctx = make_context(event, [Plain(text="回复")])
    asyncio.run(ReplyStep(make_config("reply")).handle(ctx))
    assert isinstance(ctx.chain[0], Reply)
    assert len(ctx.group.message_sequences) <= 200


def test_interruption_during_segment_delay_triggers_next_quote(monkeypatch):
    async def scenario():
        event = observe(Event("anchor"))
        config = make_config("reply", "split")

        async def interrupted(delay):
            observe(Event("photo", chain=[Image(file="photo.jpg")]))

        monkeypatch.setattr("core.step.split.asyncio.sleep", interrupted)
        ctx = make_context(event, [Plain(text="第一句。第二句。")])
        await Pipeline(config).run(ctx)
        assert not any(
            isinstance(item, Reply)
            for item in config.context.send_message.call_args_list[0].args[1].chain
        )
        assert isinstance(ctx.chain[0], Reply)
        assert ctx.chain[0].id == "anchor"

    asyncio.run(scenario())


def test_new_interruption_can_requote_same_long_running_event():
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("first"))
        step = ReplyStep(make_config("reply"))
        first = make_context(event, [Plain(text="阶段回复")])
        await step.handle(first)
        duplicate = make_context(event, [Plain(text="继续回复")])
        await step.handle(duplicate)
        assert not any(isinstance(item, Reply) for item in duplicate.chain)
        observe(Event("photo", chain=[Image(file="photo.jpg")]))
        final = make_context(event, [Plain(text="最终回复")])
        await step.handle(final)
        assert isinstance(final.chain[0], Reply)

    asyncio.run(scenario())


def test_image_summary_does_not_bypass_smart_quote(monkeypatch):
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        event.message_obj.raw_message = {}
        event.bot = SimpleNamespace(send=AsyncMock())
        event.should_call_llm = Mock()
        converted = []

        async def parse(chain):
            converted.extend(chain.chain)
            return [
                {"type": "reply", "data": {"id": "anchor"}},
                {"type": "image", "data": {"file": "photo.jpg"}},
            ]

        event._parse_onebot_json = parse
        monkeypatch.setattr("core.step.summary.AiocqhttpMessageEvent", Event)
        config = make_config("summary", "reply")
        config.summary.quotes_files = []
        ctx = make_context(event, [Image(file="photo.jpg")])
        await Pipeline(config).run(ctx)
        assert isinstance(converted[0], Reply)
        assert converted[0].id == "anchor"
        assert event.bot.send.call_count == 1
        assert ctx.chain == []

    asyncio.run(scenario())


@pytest.mark.parametrize("at_str", [False, True])
def test_split_cleaning_entire_body_drops_orphan_prefix(at_str):
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        config = make_config("at", "reply", "split", at_str=at_str)
        config.split.clean_after_items = ["remove"]
        ctx = make_context(event, [Plain(text="remove")])
        await Pipeline(config).run(ctx)
        assert not ctx.chain
        assert not ctx.group.reply_last_sequences
        assert not config.context.send_message.called

    asyncio.run(scenario())


def test_string_mention_is_merged_when_splitting_disabled():
    async def scenario():
        ctx = make_context(observe(Event("anchor")), [Plain(text="正文")])
        await Pipeline(make_config("at", at_str=True)).run(ctx)
        assert len(ctx.chain) == 1
        assert type(ctx.chain[0]) is Plain
        assert ctx.chain[0].text == "@Alice Smith 正文"

    asyncio.run(scenario())


def test_quote_is_not_repeated_when_prefix_moves_past_image():
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        config = make_config("at", "reply", "split")
        ctx = make_context(event, [Image(file="photo.jpg"), Plain(text="正文。下一句。")])
        await Pipeline(config).run(ctx)
        sent = [call.args[1].chain for call in config.context.send_message.call_args_list] + [ctx.chain]
        assert sum(isinstance(item, Reply) for chain in sent for item in chain) == 1

    asyncio.run(scenario())


def test_failed_first_segment_does_not_consume_final_quote():
    async def scenario():
        event = observe(Event("anchor"))
        observe(Event("inserted"))
        config = make_config("reply", "split")
        config.context.send_message.side_effect = RuntimeError("network failed")
        ctx = make_context(event, [Plain(text="第一句。最后一句。")])
        await Pipeline(config).run(ctx)
        assert isinstance(ctx.chain[0], Reply)

    asyncio.run(scenario())
