"""Behavior-equivalence tests for the unified message chunking module."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from mindmemos.components.chunker import MessageChunker
from mindmemos.components.chunker.vanilla import (
    ChunkPlanner,
    HistoryPacker,
    LongTurnCompactor,
    LongTurnSummarizer,
    TurnGrouper,
)
from mindmemos.components.extractor.vanilla import MemoryExtractionResult
from mindmemos.components.extractor.vanilla.add_builder import AddCoreBuilder
from mindmemos.config import VanillaAddChunkerConfig, VanillaAddConfig
from mindmemos.typing import (
    AddPipelineInput,
    Chunk,
    DialogueMessage,
    FileMessage,
    HistoryPack,
    MemoryRequestContext,
    PreprocessedText,
    TextMessage,
    TurnCompactionResult,
    TurnCompactionSummary,
    TurnMessageRef,
    UrlMessage,
)

InputMessage = DialogueMessage | TextMessage | FileMessage | UrlMessage


class _SummaryLlm:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("summary unavailable")
        return SimpleNamespace(
            parsed=TurnCompactionSummary(
                general_summary="preserved middle context",
                key_entities=["MindMemOS"],
            )
        )


class _Preprocessor:
    def preprocess_text(self, text: str, **kwargs) -> PreprocessedText:
        return PreprocessedText(
            text=text,
            normalized_text=text,
            content_hash=f"hash-{text[:16]}",
            bm25_text=text,
        )

    def preprocess_many(self, texts: list[str], **kwargs) -> list[PreprocessedText]:
        return [self.preprocess_text(text) for text in texts]


class _EnvelopeCaptureExtractor:
    def __init__(self) -> None:
        self.envelopes = []

    async def extract_from_envelope(self, envelope, preprocessed_texts, context):
        self.envelopes.append(envelope)
        return MemoryExtractionResult()


class _NoRecall:
    async def list_active_memories(self, context):
        return []

    async def recall(self, context, preprocessed, **kwargs):
        return None


class _IdentityDeduplicator:
    def dedup(self, candidates):
        return candidates


def _builder(extractor: _EnvelopeCaptureExtractor, *, llm_client=None) -> AddCoreBuilder:
    return AddCoreBuilder(
        text_preprocessor=_Preprocessor(),
        memory_extractor=extractor,
        candidate_deduplicator=_IdentityDeduplicator(),
        related_memory_recall=_NoRecall(),
        safety_gate=object(),
        vectorizer=object(),
        llm_client=llm_client,
    )


def _context() -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id="request",
        account_id="account",
        project_id="project",
        api_key_uuid="api-key",
        user_id="user",
        session_id="session",
    )


def _message_snapshot(message: TurnMessageRef) -> dict:
    return message.model_dump(mode="json")


def _chunk_snapshot(
    chunk: Chunk,
    history: HistoryPack,
    extractable: Sequence[TurnMessageRef],
    context: Sequence[TurnMessageRef],
    compactions: Sequence[tuple[int, TurnCompactionResult]],
) -> dict:
    return {
        "chunk": chunk.model_dump(mode="json"),
        "history": history.model_dump(mode="json"),
        "extractable": [_message_snapshot(message) for message in extractable],
        "context": [_message_snapshot(message) for message in context],
        "compactions": [
            {
                "turn_index": turn_index,
                "result": result.model_dump(mode="json"),
            }
            for turn_index, result in compactions
        ],
    }


async def _legacy_preprocessing_snapshot(
    messages: Sequence[InputMessage],
    config: VanillaAddChunkerConfig,
    *,
    llm_client=None,
) -> list[dict]:
    """Run the exact pre-refactor orchestration formerly in AddCoreBuilder."""

    indexed_dialogue = [
        (message_index, message)
        for message_index, message in enumerate(messages)
        if isinstance(message, (DialogueMessage, TextMessage))
    ]
    turns = TurnGrouper(config).group(indexed_dialogue)
    chunks = ChunkPlanner(config).plan(turns)

    compactor = LongTurnCompactor(config)
    summarizer = LongTurnSummarizer(config, llm_client)
    compactions_by_chunk: dict[int, list[tuple[int, TurnCompactionResult]]] = {}
    for chunk in chunks:
        if not chunk.needs_compaction:
            continue
        for turn_index in chunk.compacted_turn_indices:
            original_turn = chunk.turns[turn_index]
            parts = compactor.split(original_turn)
            summary = await summarizer.summarize(parts.middle_text)
            compacted_turn, result = compactor.compact(
                original_turn,
                summary=summary,
                parts=parts,
            )
            chunk.turns[turn_index] = compacted_turn
            compactions_by_chunk.setdefault(chunk.chunk_index, []).append((turn_index, result))
        chunk.token_count = sum(turn.token_count for turn in chunk.turns)
        if chunk.boundary == "complete":
            chunk.boundary = "compacted"

    history_packer = HistoryPacker(config)
    previous_history = None
    previous_chunk = None
    snapshots: list[dict] = []
    for chunk in chunks:
        if chunk.chunk_index == 0:
            history = history_packer.pack_for_first_chunk()
        else:
            history = history_packer.pack_for_chunk(
                chunk.chunk_index,
                previous_history,
                previous_chunk,
            )

        extractable: list[TurnMessageRef] = []
        context: list[TurnMessageRef] = []
        for turn in chunk.turns:
            for message in turn.messages:
                (extractable if message.is_extractable else context).append(message)

        snapshots.append(
            _chunk_snapshot(
                chunk,
                history,
                extractable,
                context,
                compactions_by_chunk.get(chunk.chunk_index, []),
            )
        )
        previous_history = history
        previous_chunk = chunk

    return snapshots


def _new_preprocessing_snapshot(result) -> list[dict]:
    return [
        _chunk_snapshot(
            prepared.chunk,
            prepared.history,
            prepared.extractable_messages,
            prepared.context_messages,
            [(compaction.turn_index, compaction.result) for compaction in prepared.compactions],
        )
        for prepared in result.chunks
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        [],
        [
            DialogueMessage(role="system", content="Context without evidence"),
        ],
        [
            DialogueMessage(role="user", content="First question"),
            DialogueMessage(role="system", content="Context only"),
            DialogueMessage(role="assistant", content="First answer"),
            TextMessage(text="A trailing note"),
        ],
        [
            DialogueMessage(role="Caroline", content="I moved to Boston."),
            DialogueMessage(role="Melanie", content="That is exciting."),
            DialogueMessage(role="Caroline", content="I like the parks."),
            DialogueMessage(role="Melanie", content="Great."),
        ],
        [
            DialogueMessage(role="assistant", content="Earlier answer"),
            DialogueMessage(role="user", content="Current question"),
            DialogueMessage(role="assistant", content="Current answer"),
        ],
        [
            FileMessage(file_name="notes.pdf", file_path="oss://bucket/notes.pdf"),
            DialogueMessage(role="user", content="Indexed after a file"),
            UrlMessage(url="https://example.com/design"),
            DialogueMessage(role="assistant", content="The original indices must survive"),
            TextMessage(text="   "),
        ],
        [
            DialogueMessage(role="user", content="First session", timestamp=0),
            DialogueMessage(role="user", content="Later session", timestamp=3_600_000),
        ],
        [
            DialogueMessage(role="user", content="Run the lookup"),
            DialogueMessage(role="tool", content="Lookup result"),
            DialogueMessage(role="assistant", content="Result explained"),
        ],
    ],
    ids=[
        "empty",
        "system-only",
        "standard-and-system",
        "named-speakers",
        "open-head",
        "mixed-input-types",
        "time-gap",
        "tool-message",
    ],
)
async def test_message_chunker_matches_existing_vanilla_preprocessing(messages: list[InputMessage]) -> None:
    config = VanillaAddChunkerConfig()

    legacy = await _legacy_preprocessing_snapshot(messages, config)
    result = await MessageChunker(config).split(messages)

    assert _new_preprocessing_snapshot(result) == legacy


@pytest.mark.asyncio
async def test_message_chunker_matches_multi_chunk_history_flow() -> None:
    config = VanillaAddChunkerConfig(
        chunk_soft_token_budget=20,
        chunk_hard_token_budget=40,
        turn_hard_token_budget=30,
        history_soft_token_budget=20,
        history_hard_token_budget=30,
        template_tokens=0,
        recall_budget=0,
        output_headroom=0,
    )
    messages: list[InputMessage] = []
    for turn_index in range(4):
        messages.extend(
            [
                DialogueMessage(
                    role="user",
                    content=" ".join(f"question{turn_index}_{word}" for word in range(8)),
                ),
                DialogueMessage(
                    role="assistant",
                    content=" ".join(f"answer{turn_index}_{word}" for word in range(8)),
                ),
            ]
        )

    legacy = await _legacy_preprocessing_snapshot(messages, config)
    result = await MessageChunker(config).split(messages)
    actual = _new_preprocessing_snapshot(result)

    assert actual == legacy
    assert len(actual) == 4
    assert actual[0]["history"]["in_request_history"] == []
    assert actual[1]["history"]["in_request_history"]


@pytest.mark.asyncio
async def test_message_chunker_matches_long_turn_compaction_and_retains_diagnostics() -> None:
    config = VanillaAddChunkerConfig(
        chunk_soft_token_budget=30,
        chunk_hard_token_budget=40,
        turn_hard_token_budget=10,
        history_soft_token_budget=1,
        history_hard_token_budget=2,
        compaction_head_tokens=4,
        compaction_tail_tokens=4,
        compaction_summary_context_token_budget=100,
        compaction_summary_output_token_budget=20,
        template_tokens=0,
        recall_budget=0,
        output_headroom=0,
    )
    messages = [
        DialogueMessage(role="user", content="keep this complete user request"),
        DialogueMessage(
            role="assistant",
            content=" ".join(f"answer{word}" for word in range(40)),
        ),
    ]
    legacy_llm = _SummaryLlm()
    new_llm = _SummaryLlm()

    legacy = await _legacy_preprocessing_snapshot(messages, config, llm_client=legacy_llm)
    result = await MessageChunker(config, llm_client=new_llm).split(messages)
    actual = _new_preprocessing_snapshot(result)

    assert actual == legacy
    assert len(legacy_llm.calls) == len(new_llm.calls) == 1
    assert actual[0]["chunk"]["boundary"] == "compacted"
    assert actual[0]["compactions"][0]["result"]["is_lossy"] is True
    assert actual[0]["extractable"][0]["text"] == "keep this complete user request"
    assert actual[0]["context"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_message_chunker_matches_summary_failure_fallback() -> None:
    config = VanillaAddChunkerConfig(
        chunk_soft_token_budget=30,
        chunk_hard_token_budget=40,
        turn_hard_token_budget=10,
        history_soft_token_budget=1,
        history_hard_token_budget=2,
        compaction_head_tokens=3,
        compaction_tail_tokens=3,
        compaction_summary_context_token_budget=100,
        compaction_summary_output_token_budget=20,
        template_tokens=0,
        recall_budget=0,
        output_headroom=0,
    )
    messages = [
        DialogueMessage(role="user", content="remember the request"),
        DialogueMessage(role="assistant", content=" ".join(f"token{word}" for word in range(40))),
    ]
    legacy_llm = _SummaryLlm(fail=True)
    new_llm = _SummaryLlm(fail=True)

    legacy = await _legacy_preprocessing_snapshot(messages, config, llm_client=legacy_llm)
    result = await MessageChunker(config, llm_client=new_llm).split(messages)
    actual = _new_preprocessing_snapshot(result)

    assert actual == legacy
    assert "omitted" in actual[0]["compactions"][0]["result"]["middle_summary"]["general_summary"].lower()


@pytest.mark.asyncio
async def test_add_core_builder_consumes_prepared_multi_chunk_history_without_changing_envelopes() -> None:
    chunker_config = VanillaAddChunkerConfig(
        chunk_soft_token_budget=20,
        chunk_hard_token_budget=40,
        turn_hard_token_budget=30,
        history_soft_token_budget=20,
        history_hard_token_budget=30,
        template_tokens=0,
        recall_budget=0,
        output_headroom=0,
    )
    messages: list[InputMessage] = []
    for turn_index in range(2):
        messages.extend(
            [
                DialogueMessage(
                    role="user",
                    content=" ".join(f"question{turn_index}_{word}" for word in range(8)),
                ),
                DialogueMessage(
                    role="assistant",
                    content=" ".join(f"answer{turn_index}_{word}" for word in range(8)),
                ),
            ]
        )
    expected = await MessageChunker(chunker_config).split(messages)
    extractor = _EnvelopeCaptureExtractor()

    await _builder(extractor).build(
        AddPipelineInput(messages=messages),
        _context(),
        config=VanillaAddConfig(chunker=chunker_config),
    )

    assert len(extractor.envelopes) == len(expected.chunks) == 2
    for envelope, prepared in zip(extractor.envelopes, expected.chunks, strict=True):
        assert envelope.extractable_messages == list(prepared.extractable_messages)
        assert envelope.current_context_messages == list(prepared.context_messages)
        assert envelope.history == prepared.history
        assert envelope.boundary == prepared.chunk.boundary
        assert envelope.chunk_index == prepared.chunk.chunk_index


@pytest.mark.asyncio
async def test_add_core_builder_preserves_compacted_envelope_contract() -> None:
    chunker_config = VanillaAddChunkerConfig(
        chunk_soft_token_budget=30,
        chunk_hard_token_budget=40,
        turn_hard_token_budget=10,
        history_soft_token_budget=1,
        history_hard_token_budget=2,
        compaction_head_tokens=4,
        compaction_tail_tokens=4,
        compaction_summary_context_token_budget=100,
        compaction_summary_output_token_budget=20,
        template_tokens=0,
        recall_budget=0,
        output_headroom=0,
    )
    messages = [
        DialogueMessage(role="user", content="keep this complete request"),
        DialogueMessage(role="assistant", content=" ".join(f"answer{word}" for word in range(40))),
    ]
    llm_client = _SummaryLlm()
    extractor = _EnvelopeCaptureExtractor()

    await _builder(extractor, llm_client=llm_client).build(
        AddPipelineInput(messages=messages),
        _context(),
        config=VanillaAddConfig(chunker=chunker_config),
    )

    assert len(llm_client.calls) == 1
    assert len(extractor.envelopes) == 1
    envelope = extractor.envelopes[0]
    assert envelope.boundary == "compacted"
    assert envelope.extractable_messages[0].text == "keep this complete request"
    assert envelope.extractable_messages[-1].text.endswith("answer39")
    assert len(envelope.current_context_messages) == 1
    assert envelope.current_context_messages[0].is_extractable is False
