import asyncio
from types import SimpleNamespace

from astrbot.core.message.components import At, Image, Plain, Reply

from core import step as step_pkg
from core.model import GroupState, OutContext
from core.step import summary as summary_module
from core.step.summary import SummaryStep


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw_message, obmsg):
        self.sent.append((raw_message, obmsg))


class FakeEvent:
    """伪装成 aiocqhttp 事件，供 isinstance 检查通过（测试内替换类型）。"""

    def __init__(self):
        self.message_obj = SimpleNamespace(message_id="m1", raw_message={"raw": True})
        self.bot = FakeBot()
        self.llm_called = None

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def should_call_llm(self, value: bool) -> None:
        self.llm_called = value

    async def _parse_onebot_json(self, chain):
        segments = []
        for comp in chain.chain:
            if isinstance(comp, Image):
                segments.append({"type": "image", "data": {"file": comp.file}})
            elif isinstance(comp, Plain):
                segments.append({"type": "text", "data": {"text": comp.text}})
            else:
                segments.append({"type": type(comp).__name__.lower(), "data": {}})
        return segments


def make_step(quotes=("金句A",)) -> SummaryStep:
    config = SimpleNamespace(
        summary=SimpleNamespace(quotes=list(quotes), quotes_files=[]),
        pipeline=SimpleNamespace(debug_logging=True),
    )
    return SummaryStep(config)


def make_context(event: FakeEvent, chain: list) -> OutContext:
    return OutContext(
        event=event,
        chain=chain,
        is_llm=True,
        plain="",
        gid="group-1",
        uid="42",
        bid="99",
        group=GroupState(gid="group-1"),
        timestamp=0,
    )


def run(chain, quotes=("金句A",)):
    async def scenario():
        original = summary_module.AiocqhttpMessageEvent
        summary_module.AiocqhttpMessageEvent = FakeEvent  # type: ignore[assignment]
        try:
            event = FakeEvent()
            step = make_step(quotes)
            context = make_context(event, chain)
            result = await step.handle(context)
            return event, context, result
        finally:
            summary_module.AiocqhttpMessageEvent = original  # type: ignore[assignment]

    return asyncio.run(scenario())


def test_single_image_gets_summary():
    event, context, result = run([Image(file="a.jpg")])

    assert result.abort is True
    assert result.msg == "已给图片附加外显金句：金句A"
    assert context.chain == []
    assert event.llm_called is True
    assert event.bot.sent[0][1][0]["data"]["summary"] == "金句A"


def test_at_and_reply_do_not_block_summary():
    event, _, result = run([Reply(id="m0"), At(qq="1"), Image(file="a.jpg")])

    assert result.abort is True
    image_segments = [
        seg for seg in event.bot.sent[0][1] if seg["type"] == "image"
    ]
    assert image_segments[0]["data"]["summary"] == "金句A"


def test_multiple_images_use_first_image():
    event, _, result = run([Image(file="a.jpg"), Image(file="b.jpg")])

    assert result.abort is True
    segments = event.bot.sent[0][1]
    assert segments[0]["data"].get("summary") == "金句A"
    assert "summary" not in segments[1]["data"]


def test_visible_text_blocks_summary():
    event, context, result = run([Plain(text="你好"), Image(file="a.jpg")])

    assert result.abort is False
    assert event.bot.sent == []
    assert len(context.chain) == 2


def test_blank_text_does_not_block_summary():
    event, _, result = run([Plain(text="   "), Image(file="a.jpg")])

    assert result.abort is True
    assert event.bot.sent


def test_chain_without_image_is_skipped():
    event, _, result = run([Plain(text="纯文本")])

    assert result.abort is False
    assert event.bot.sent == []


def test_empty_quotes_is_skipped():
    event, _, result = run([Image(file="a.jpg")], quotes=())

    assert result.abort is False
    assert event.bot.sent == []


def test_non_aiocqhttp_event_is_skipped():
    async def scenario():
        event = FakeEvent()
        step = make_step()
        context = make_context(event, [Image(file="a.jpg")])
        return event, await step.handle(context)

    event, result = asyncio.run(scenario())

    assert result.abort is False
    assert event.bot.sent == []


assert step_pkg.SummaryStep is SummaryStep
