"""One-step preparation of input messages for chunk-based extraction.

``MessageChunker`` is the public orchestration boundary for message splitting.
It keeps the existing vanilla behavior while hiding the implementation phases:

1. select text-bearing input messages and preserve their original indices;
2. group messages into semantic turns;
3. pack complete turns into token-budgeted chunks;
4. compact oversized turns while keeping raw head/tail evidence;
5. attach the same sliding in-request history used by vanilla extraction.

The text preprocessor, related-memory recall, and memory extractor deliberately
remain outside this module. They consume the prepared chunks but do not decide
how the original message sequence is split.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ...config import VanillaAddChunkerConfig
from ...typing import (
    Chunk,
    DialogueMessage,
    FileMessage,
    HistoryPack,
    TextMessage,
    TurnCompactionResult,
    TurnMessageRef,
    UrlMessage,
)
from .vanilla import ChunkPlanner, HistoryPacker, LongTurnCompactor, LongTurnSummarizer, TurnGrouper

InputMessage = DialogueMessage | TextMessage | FileMessage | UrlMessage


@dataclass(frozen=True, slots=True)
class PreparedTurnCompaction:
    """Diagnostic record for one oversized turn compacted inside a chunk."""

    turn_index: int
    result: TurnCompactionResult


@dataclass(frozen=True, slots=True)
class PreparedMessageChunk:
    """One extraction-ready chunk produced from the input message sequence.

    ``extractable_messages`` are raw evidence that may produce memories.
    ``context_messages`` and ``history`` are visible context only. Keeping
    these collections separate makes the evidence boundary explicit for every
    extraction algorithm that consumes this module.
    """

    chunk: Chunk
    extractable_messages: tuple[TurnMessageRef, ...]
    context_messages: tuple[TurnMessageRef, ...]
    history: HistoryPack
    compactions: tuple[PreparedTurnCompaction, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageChunkingResult:
    """Complete ordered result of one message-splitting operation."""

    chunks: tuple[PreparedMessageChunk, ...]


class MessageChunker:
    """Convert a mixed message sequence into extraction-ready message chunks.

    The public operation is asynchronous because oversized turns can require an
    LLM summary. Normal inputs remain deterministic and make no LLM calls.
    """

    def __init__(
        self,
        config: VanillaAddChunkerConfig | None = None,
        *,
        llm_client: Any = None,
    ) -> None:
        self._config = config or VanillaAddChunkerConfig()
        self._turn_grouper = TurnGrouper(self._config)
        self._chunk_planner = ChunkPlanner(self._config)
        self._turn_compactor = LongTurnCompactor(self._config)
        self._turn_summarizer = LongTurnSummarizer(self._config, llm_client)
        self._history_packer = HistoryPacker(self._config)

    async def split(self, messages: Sequence[InputMessage]) -> MessageChunkingResult:
        """Split messages while preserving the current vanilla preprocessing contract."""

        indexed_messages = self._select_text_messages(messages)
        turns = self._turn_grouper.group(indexed_messages)
        chunks = self._chunk_planner.plan(turns)
        compactions_by_chunk = await self._compact_oversized_turns(chunks)
        prepared_chunks = self._attach_context_and_history(chunks, compactions_by_chunk)
        return MessageChunkingResult(chunks=tuple(prepared_chunks))

    @staticmethod
    def _select_text_messages(
        messages: Sequence[InputMessage],
    ) -> list[tuple[int, DialogueMessage | TextMessage]]:
        """Select extractable message types without losing input-list indices."""

        return [
            (message_index, message)
            for message_index, message in enumerate(messages)
            if isinstance(message, (DialogueMessage, TextMessage))
        ]

    async def _compact_oversized_turns(
        self,
        chunks: list[Chunk],
    ) -> dict[int, tuple[PreparedTurnCompaction, ...]]:
        """Resolve planner compaction markers into final chunk contents."""

        compactions_by_chunk: dict[int, tuple[PreparedTurnCompaction, ...]] = {}
        for chunk in chunks:
            if not chunk.needs_compaction:
                continue

            chunk_compactions: list[PreparedTurnCompaction] = []
            for turn_index in chunk.compacted_turn_indices:
                original_turn = chunk.turns[turn_index]
                parts = self._turn_compactor.split(original_turn)
                summary = await self._turn_summarizer.summarize(parts.middle_text)
                compacted_turn, result = self._turn_compactor.compact(
                    original_turn,
                    summary=summary,
                    parts=parts,
                )
                chunk.turns[turn_index] = compacted_turn
                chunk_compactions.append(
                    PreparedTurnCompaction(
                        turn_index=turn_index,
                        result=result,
                    )
                )

            chunk.token_count = sum(turn.token_count for turn in chunk.turns)
            if chunk.boundary == "complete":
                chunk.boundary = "compacted"
            compactions_by_chunk[chunk.chunk_index] = tuple(chunk_compactions)

        return compactions_by_chunk

    def _attach_context_and_history(
        self,
        chunks: list[Chunk],
        compactions_by_chunk: dict[int, tuple[PreparedTurnCompaction, ...]],
    ) -> list[PreparedMessageChunk]:
        """Build the evidence/context split and sliding history for every chunk."""

        prepared: list[PreparedMessageChunk] = []
        previous_history: HistoryPack | None = None
        previous_chunk: Chunk | None = None

        for chunk in chunks:
            if chunk.chunk_index == 0:
                history = self._history_packer.pack_for_first_chunk()
            else:
                if previous_history is None or previous_chunk is None:
                    raise RuntimeError("chunk plan must be ordered and start at chunk index 0")
                history = self._history_packer.pack_for_chunk(
                    chunk.chunk_index,
                    previous_history,
                    previous_chunk,
                )

            extractable_messages: list[TurnMessageRef] = []
            context_messages: list[TurnMessageRef] = []
            for turn in chunk.turns:
                for message in turn.messages:
                    target = extractable_messages if message.is_extractable else context_messages
                    target.append(message)

            prepared.append(
                PreparedMessageChunk(
                    chunk=chunk,
                    extractable_messages=tuple(extractable_messages),
                    context_messages=tuple(context_messages),
                    history=history,
                    compactions=compactions_by_chunk.get(chunk.chunk_index, ()),
                )
            )
            previous_history = history
            previous_chunk = chunk

        return prepared
