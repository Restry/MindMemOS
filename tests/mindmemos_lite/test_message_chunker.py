"""Behavior-contract tests for the independent message chunking module."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mindmemos.components.chunker import MessageChunker
from mindmemos.components.extractor.vanilla import MemoryExtractionResult
from mindmemos.components.extractor.vanilla.add_builder import AddCoreBuilder
from mindmemos.config import MessageChunkerConfig, VanillaAddConfig
from mindmemos.typing import (
    AddPipelineInput,
    DialogueMessage,
    FileMessage,
    MemoryRequestContext,
    PreprocessedText,
    TextMessage,
    TurnCompactionSummary,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "messages",
        "chunk_boundaries",
        "turn_boundaries",
        "extractable_roles",
        "extractable_indices",
        "context_roles",
    ),
    [
        ([], [], [], [], [], []),
        (
            [DialogueMessage(role="system", content="Context without evidence")],
            ["complete"],
            ["complete"],
            [],
            [],
            ["system"],
        ),
        (
            [
                DialogueMessage(role="user", content="First question"),
                DialogueMessage(role="system", content="Context only"),
                DialogueMessage(role="assistant", content="First answer"),
                TextMessage(text="A trailing note"),
            ],
            ["open_tail"],
            ["complete", "open_tail"],
            ["user", "assistant", "user"],
            [0, 2, 3],
            ["system"],
        ),
        (
            [
                DialogueMessage(role="Caroline", content="I moved to Boston."),
                DialogueMessage(role="Melanie", content="That is exciting."),
                DialogueMessage(role="Caroline", content="I like the parks."),
                DialogueMessage(role="Melanie", content="Great."),
            ],
            ["complete"],
            ["complete", "complete"],
            ["speaker", "speaker", "speaker", "speaker"],
            [0, 1, 2, 3],
            [],
        ),
        (
            [
                DialogueMessage(role="assistant", content="Earlier answer"),
                DialogueMessage(role="user", content="Current question"),
                DialogueMessage(role="assistant", content="Current answer"),
            ],
            ["open_head"],
            ["open_head", "complete"],
            ["assistant", "user", "assistant"],
            [0, 1, 2],
            [],
        ),
        (
            [
                FileMessage(file_name="notes.pdf", file_path="oss://bucket/notes.pdf"),
                DialogueMessage(role="user", content="Indexed after a file"),
                UrlMessage(url="https://example.com/design"),
                DialogueMessage(role="assistant", content="The original indices must survive"),
                TextMessage(text="   "),
            ],
            ["complete"],
            ["complete"],
            ["user", "assistant"],
            [1, 3],
            [],
        ),
        (
            [
                DialogueMessage(role="user", content="First session", timestamp=0),
                DialogueMessage(role="user", content="Later session", timestamp=3_600_000),
            ],
            ["open_tail"],
            ["open_tail", "open_tail"],
            ["user", "user"],
            [0, 1],
            [],
        ),
        (
            [
                DialogueMessage(role="user", content="Run the lookup"),
                DialogueMessage(role="tool", content="Lookup result"),
                DialogueMessage(role="assistant", content="Result explained"),
            ],
            ["complete"],
            ["complete"],
            ["user", "tool", "assistant"],
            [0, 1, 2],
            [],
        ),
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
async def test_message_chunker_preserves_message_grouping_contract(
    messages: list[InputMessage],
    chunk_boundaries: list[str],
    turn_boundaries: list[str],
    extractable_roles: list[str],
    extractable_indices: list[int],
    context_roles: list[str],
) -> None:
    result = await MessageChunker(MessageChunkerConfig()).split(messages)

    assert [prepared.chunk.boundary for prepared in result.chunks] == chunk_boundaries
    assert [turn.boundary for prepared in result.chunks for turn in prepared.chunk.turns] == turn_boundaries
    assert [
        message.role for prepared in result.chunks for message in prepared.extractable_messages
    ] == extractable_roles
    assert [
        message.message_index for prepared in result.chunks for message in prepared.extractable_messages
    ] == extractable_indices
    assert [message.role for prepared in result.chunks for message in prepared.context_messages] == context_roles


@pytest.mark.asyncio
async def test_message_chunker_retains_message_file_and_url_source_refs() -> None:
    messages: list[InputMessage] = [
        FileMessage(file_name="notes.pdf", file_path="oss://bucket/notes.pdf"),
        DialogueMessage(role="Customer", content="Remember the attachment"),
        UrlMessage(url="https://example.com/design"),
        DialogueMessage(role="Agent", content="I will remember it"),
    ]

    result = await MessageChunker(MessageChunkerConfig()).split(messages)

    assert [(ref.source_type, ref.metadata["message_index"]) for ref in result.external_source_refs] == [
        ("file", 0),
        ("url", 2),
    ]
    assert result.external_source_refs[0].file_path == "oss://bucket/notes.pdf"
    assert result.external_source_refs[1].uri == "https://example.com/design"
    assert all(ref.source_id is None for ref in result.external_source_refs)

    prepared = result.chunks[0]
    assert len(prepared.source_refs) == len(prepared.extractable_messages) == 2
    assert [ref.message_id for ref in prepared.source_refs] == [
        "chunk0-evidence-0-message-1",
        "chunk0-evidence-1-message-3",
    ]
    assert [ref.metadata["source_raw_role"] for ref in prepared.source_refs] == ["Customer", "Agent"]
    assert [ref.metadata["evidence_index"] for ref in prepared.source_refs] == [0, 1]
    assert all(ref.source_id is None for ref in prepared.source_refs)


@pytest.mark.asyncio
async def test_message_chunker_preserves_multi_chunk_history_flow() -> None:
    config = MessageChunkerConfig(
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

    result = await MessageChunker(config).split(messages)

    assert len(result.chunks) == 4
    assert result.chunks[0].history.in_request_history == []
    assert result.chunks[1].history.in_request_history == result.chunks[0].chunk.turns
    assert result.chunks[2].history.in_request_history == result.chunks[1].chunk.turns
    assert all(prepared.chunk.token_count == 16 for prepared in result.chunks)


@pytest.mark.asyncio
async def test_message_chunker_compacts_long_turn_and_retains_diagnostics() -> None:
    config = MessageChunkerConfig(
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
    llm_client = _SummaryLlm()

    result = await MessageChunker(config, llm_client=llm_client).split(messages)

    assert len(llm_client.calls) == 1
    assert len(result.chunks) == 1
    prepared = result.chunks[0]
    assert prepared.chunk.boundary == "compacted"
    assert prepared.compactions[0].result.is_lossy is True
    assert prepared.extractable_messages[0].text == "keep this complete user request"
    assert prepared.extractable_messages[-1].text.endswith("answer39")
    assert prepared.context_messages[0].role == "system"
    assert '"general_summary": "preserved middle context"' in prepared.context_messages[0].text
    assert len(prepared.source_refs) == len(prepared.extractable_messages)
    assert [ref.metadata["message_index"] for ref in prepared.source_refs] == [0, 1]
    assert [ref.metadata["evidence_index"] for ref in prepared.source_refs] == [0, 1]


@pytest.mark.asyncio
async def test_message_chunker_uses_summary_failure_fallback() -> None:
    config = MessageChunkerConfig(
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
    llm_client = _SummaryLlm(fail=True)

    result = await MessageChunker(config, llm_client=llm_client).split(messages)

    assert len(llm_client.calls) == 1
    summary = result.chunks[0].compactions[0].result.middle_summary
    assert "omitted" in summary.general_summary.lower()


@pytest.mark.asyncio
async def test_add_core_builder_consumes_prepared_multi_chunk_history_without_changing_envelopes() -> None:
    chunker_config = MessageChunkerConfig(
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
async def test_add_core_builder_consumes_chunker_owned_source_refs() -> None:
    messages: list[InputMessage] = [
        FileMessage(file_name="notes.pdf", file_path="oss://bucket/notes.pdf"),
        DialogueMessage(role="user", content="Remember the attachment"),
        UrlMessage(url="https://example.com/design"),
        DialogueMessage(role="assistant", content="I will remember it"),
    ]
    extractor = _EnvelopeCaptureExtractor()

    plan, _, _ = await _builder(extractor).build(
        AddPipelineInput(messages=messages),
        _context(),
    )

    assert [source.source_type for source in plan.sources] == ["file", "url", "message", "message"]
    assert [source.persist_payload for source in plan.sources] == [True, True, False, False]
    message_sources = [source for source in plan.sources if source.source_type == "message"]
    assert [source.metadata["message_index"] for source in message_sources] == [1, 3]
    assert [source.metadata["evidence_index"] for source in message_sources] == [0, 1]
    assert all(source.source_id for source in plan.sources)


@pytest.mark.asyncio
async def test_add_core_builder_preserves_compacted_envelope_contract() -> None:
    chunker_config = MessageChunkerConfig(
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
