"""Independent one-step preparation of messages for chunk-based extraction.

The module owns the complete message-splitting lifecycle:

1. normalize text-bearing messages while preserving their input indices;
2. group messages into semantic conversation turns;
3. pack complete turns into token-budgeted chunks;
4. compact an oversized turn into raw head, summarized middle, and raw tail;
5. separate extractable evidence from context and attach sliding history.

Text normalization, related-memory recall, and memory extraction intentionally
remain outside this boundary because they do not decide how messages are split.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ...config import MessageChunkerConfig
from ...logging import get_logger
from ...typing import (
    Chunk,
    ChunkBoundary,
    DialogueMessage,
    FileMessage,
    HistoryPack,
    TextMessage,
    Turn,
    TurnBoundary,
    TurnCompactionResult,
    TurnCompactionSummary,
    TurnMessageRef,
    UrlMessage,
)

logger = get_logger(__name__)

InputMessage = DialogueMessage | TextMessage | FileMessage | UrlMessage
_STANDARD_ROLES = {"user", "assistant", "system", "tool"}

_LONG_TURN_SUMMARY_PROMPT = """You summarize the middle section of a long conversation turn for later memory extraction.

Preserve user intent, resolved references, important entities, confirmed facts, decisions, corrections,
open questions, and warnings. Do not invent unsupported facts and do not output memory candidates.

Return one JSON object with these fields:
{
  "general_summary": "concise factual summary",
  "key_entities": ["entity"],
  "user_intent": "intent",
  "confirmed_facts": ["fact"],
  "decisions": ["decision"],
  "open_questions": ["question"],
  "warnings": ["warning"]
}
"""


@dataclass(frozen=True, slots=True)
class PreparedTurnCompaction:
    """Diagnostic record for one oversized turn compacted inside a chunk."""

    turn_index: int
    result: TurnCompactionResult


@dataclass(frozen=True, slots=True)
class PreparedMessageChunk:
    """One extraction-ready chunk with an explicit evidence boundary."""

    chunk: Chunk
    extractable_messages: tuple[TurnMessageRef, ...]
    context_messages: tuple[TurnMessageRef, ...]
    history: HistoryPack
    compactions: tuple[PreparedTurnCompaction, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageChunkingResult:
    """Complete ordered result of one message-splitting operation."""

    chunks: tuple[PreparedMessageChunk, ...]


@dataclass(frozen=True, slots=True)
class _TurnCompactionParts:
    """Deterministic source ranges selected for long-turn compaction."""

    head_text: str
    middle_text: str
    tail_text: str
    head_messages: tuple[TurnMessageRef, ...] = ()
    tail_messages: tuple[TurnMessageRef, ...] = ()


def _estimate_tokens(text: str) -> int:
    """Estimate token count with the existing whitespace and CJK heuristic."""

    if not text:
        return 0
    cjk_count = sum(1 for character in text if _is_cjk(character))
    non_cjk_text = "".join(" " if _is_cjk(character) else character for character in text)
    return int(cjk_count / 1.5) + len(non_cjk_text.split())


def _is_cjk(character: str) -> bool:
    return "一" <= character <= "鿿" or "㐀" <= character <= "䶿"


def _parse_compaction_summary(content: str) -> TurnCompactionSummary:
    """Parse a structured summary while tolerating markdown JSON fences."""

    text = content.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
        text = text.removesuffix("```").strip()
    return TurnCompactionSummary.model_validate(json.loads(text))


class MessageChunker:
    """Convert a mixed message sequence into extraction-ready chunks.

    ``split`` is asynchronous only because oversized turns may require an LLM
    summary. Normal inputs are deterministic and make no LLM calls.
    """

    def __init__(
        self,
        config: MessageChunkerConfig | None = None,
        *,
        llm_client: Any = None,
    ) -> None:
        self._config = config or MessageChunkerConfig()
        self._llm_client = llm_client

    async def split(self, messages: Sequence[InputMessage]) -> MessageChunkingResult:
        """Run the complete message-splitting lifecycle."""

        message_refs = self._normalize_messages(messages)
        turns = self._group_turns(message_refs)
        chunks = self._plan_chunks(turns)
        compactions_by_chunk = await self._compact_oversized_turns(chunks)
        prepared_chunks = self._prepare_chunks(chunks, compactions_by_chunk)
        return MessageChunkingResult(chunks=tuple(prepared_chunks))

    # Phase 1: normalize input messages

    def _normalize_messages(self, messages: Sequence[InputMessage]) -> list[TurnMessageRef]:
        """Create internal message refs and retain positions in the mixed input."""

        refs: list[TurnMessageRef] = []
        for message_index, message in enumerate(messages):
            if isinstance(message, TextMessage):
                text = message.text
                role = "user"
                raw_role = None
                speaker = None
                timestamp = None
            elif isinstance(message, DialogueMessage):
                text = message.content
                raw_role = message.role
                role, speaker = self._normalize_role(message.role)
                timestamp = message.timestamp
            else:
                continue

            if not text.strip():
                continue
            refs.append(
                TurnMessageRef(
                    text=text,
                    role=role,
                    raw_role=raw_role,
                    speaker=speaker,
                    timestamp=timestamp,
                    message_index=message_index,
                    is_extractable=role != "system",
                )
            )
        return refs

    @staticmethod
    def _normalize_role(role: str) -> tuple[str, str | None]:
        raw_role = (role or "").strip()
        normalized = raw_role.lower().replace("-", "_").replace(" ", "_")
        if normalized in _STANDARD_ROLES:
            return normalized, None
        return "speaker", raw_role or None

    # Phase 2: group normalized messages into semantic turns

    def _group_turns(self, refs: list[TurnMessageRef]) -> list[Turn]:
        if not refs:
            return []
        if any(ref.role == "speaker" for ref in refs):
            return self._group_multi_speaker_turns(refs)

        turns: list[Turn] = []
        current: list[TurnMessageRef] = []
        for ref in refs:
            should_split = (
                ref.role != "system"
                and bool(current)
                and (
                    (ref.role == current[-1].role and self._has_time_gap(ref, current[-1]))
                    or (ref.role == "user" and any(message.role == "assistant" for message in current))
                )
            )
            if should_split:
                turns.append(self._build_turn(current))
                current = []
            current.append(ref)

        if current:
            turns.append(self._build_turn(current))

        request_has_user = any(message.role == "user" for turn in turns for message in turn.messages)
        if turns and turns[0].boundary == "orphan" and request_has_user:
            turns[0].boundary = "open_head"
        return turns

    def _group_multi_speaker_turns(self, refs: list[TurnMessageRef]) -> list[Turn]:
        turns: list[Turn] = []
        current: list[TurnMessageRef] = []
        speakers: set[str] = set()

        for ref in refs:
            if ref.role != "system" and current:
                speaker = self._speaker_key(ref)
                last_extractable = next((message for message in reversed(current) if message.is_extractable), None)
                speaker_repeated_after_reply = (
                    ref.is_extractable
                    and speaker in speakers
                    and last_extractable is not None
                    and self._speaker_key(last_extractable) != speaker
                )
                if self._has_time_gap(ref, current[-1]) or speaker_repeated_after_reply:
                    turns.append(self._build_turn(current))
                    current = []
                    speakers = set()

            current.append(ref)
            if ref.is_extractable:
                speakers.add(self._speaker_key(ref))

        if current:
            turns.append(self._build_turn(current))
        return turns

    def _has_time_gap(self, current: TurnMessageRef, previous: TurnMessageRef) -> bool:
        return (
            current.timestamp is not None
            and previous.timestamp is not None
            and abs(current.timestamp - previous.timestamp) / 1000.0 > self._config.time_gap_threshold_seconds
        )

    @staticmethod
    def _speaker_key(ref: TurnMessageRef) -> str:
        return (ref.speaker or ref.raw_role or ref.role).strip().lower()

    def _build_turn(self, messages: list[TurnMessageRef]) -> Turn:
        return Turn(
            messages=messages,
            boundary=self._derive_turn_boundary(messages),
            token_count=sum(_estimate_tokens(message.text) for message in messages),
        )

    def _derive_turn_boundary(self, messages: list[TurnMessageRef]) -> TurnBoundary:
        non_system = [message for message in messages if message.role != "system"]
        roles = [message.role for message in non_system]
        if not non_system:
            return "complete"
        if "speaker" in roles:
            speakers = {self._speaker_key(message) for message in non_system if message.role == "speaker"}
            return "complete" if len(speakers) >= 2 else "open_tail"
        if "user" not in roles:
            return "orphan"
        if "assistant" not in roles:
            return "open_tail"
        return "open_head" if roles[0] == "assistant" else "complete"

    # Phase 3: pack complete turns into token-budgeted chunks

    @property
    def _hard_extractable_budget(self) -> int:
        config = self._config
        return (
            config.chunk_hard_token_budget
            - config.template_tokens
            - config.history_hard_token_budget
            - config.recall_budget
            - config.output_headroom
        )

    @property
    def _soft_extractable_budget(self) -> int:
        config = self._config
        return (
            config.chunk_soft_token_budget
            - config.template_tokens
            - config.history_soft_token_budget
            - config.recall_budget
            - config.output_headroom
        )

    def _plan_chunks(self, turns: list[Turn]) -> list[Chunk]:
        if not turns:
            return []

        chunks: list[Chunk] = []
        current_turns: list[Turn] = []
        current_tokens = 0
        chunk_index = 0

        for turn in turns:
            turn_tokens = turn.token_count
            turn_is_oversized = (
                turn_tokens > self._config.turn_hard_token_budget or turn_tokens > self._hard_extractable_budget
            )
            if turn_is_oversized:
                if current_turns:
                    chunks.append(self._build_chunk(current_turns, current_tokens, chunk_index))
                    chunk_index += 1
                    current_turns = []
                    current_tokens = 0

                chunk = self._build_chunk([turn], turn_tokens, chunk_index)
                chunk.needs_compaction = True
                chunk.compacted_turn_indices = [0]
                chunks.append(chunk)
                chunk_index += 1
                continue

            next_token_count = current_tokens + turn_tokens
            if current_turns and (
                next_token_count > self._soft_extractable_budget or next_token_count > self._hard_extractable_budget
            ):
                chunks.append(self._build_chunk(current_turns, current_tokens, chunk_index))
                chunk_index += 1
                current_turns = []
                current_tokens = 0

            current_turns.append(turn)
            current_tokens += turn_tokens

        if current_turns:
            chunks.append(self._build_chunk(current_turns, current_tokens, chunk_index))
        return chunks

    def _build_chunk(self, turns: list[Turn], token_count: int, chunk_index: int) -> Chunk:
        return Chunk(
            turns=turns,
            boundary=self._derive_chunk_boundary(turns),
            token_count=token_count,
            chunk_index=chunk_index,
        )

    @staticmethod
    def _derive_chunk_boundary(turns: list[Turn]) -> ChunkBoundary:
        if not turns:
            return "complete"
        boundaries = [turn.boundary for turn in turns]
        if "open_head" in boundaries:
            return "open_head"
        if boundaries[-1] == "open_tail":
            return "open_tail"
        if "orphan" in boundaries:
            return "open_head" if len(turns) > 1 else "orphan"
        return "complete"

    # Phase 4: compact oversized turns

    async def _compact_oversized_turns(
        self,
        chunks: list[Chunk],
    ) -> dict[int, tuple[PreparedTurnCompaction, ...]]:
        compactions_by_chunk: dict[int, tuple[PreparedTurnCompaction, ...]] = {}
        for chunk in chunks:
            if not chunk.needs_compaction:
                continue

            chunk_compactions: list[PreparedTurnCompaction] = []
            for turn_index in chunk.compacted_turn_indices:
                original_turn = chunk.turns[turn_index]
                parts = self._split_oversized_turn(original_turn)
                summary = await self._summarize_middle(parts.middle_text)
                compacted_turn, result = self._compact_turn(original_turn, parts, summary)
                chunk.turns[turn_index] = compacted_turn
                chunk_compactions.append(PreparedTurnCompaction(turn_index=turn_index, result=result))

            chunk.token_count = sum(turn.token_count for turn in chunk.turns)
            if chunk.boundary == "complete":
                chunk.boundary = "compacted"
            compactions_by_chunk[chunk.chunk_index] = tuple(chunk_compactions)
        return compactions_by_chunk

    def _split_oversized_turn(self, turn: Turn) -> _TurnCompactionParts:
        text, message_ranges = self._flatten_extractable_messages(turn)
        if not text:
            return _TurnCompactionParts(head_text="", middle_text="", tail_text="")

        head_end = self._prefix_end_for_budget(text, self._config.compaction_head_tokens)
        first_user_end = next(
            (end for message, _start, end in message_ranges if message.role == "user"),
            None,
        )
        if first_user_end is not None:
            head_end = max(head_end, first_user_end)

        tail_start = self._suffix_start_for_budget(text, self._config.compaction_tail_tokens)
        tail_start = max(head_end, tail_start)
        return _TurnCompactionParts(
            head_text=text[:head_end],
            middle_text=text[head_end:tail_start],
            tail_text=text[tail_start:],
            head_messages=tuple(self._slice_message_refs(message_ranges, 0, head_end)),
            tail_messages=tuple(self._slice_message_refs(message_ranges, tail_start, len(text))),
        )

    @staticmethod
    def _flatten_extractable_messages(
        turn: Turn,
    ) -> tuple[str, list[tuple[TurnMessageRef, int, int]]]:
        text_parts: list[str] = []
        message_ranges: list[tuple[TurnMessageRef, int, int]] = []
        position = 0
        for message in turn.extractable_messages:
            if text_parts:
                text_parts.append("\n")
                position += 1
            start = position
            text_parts.append(message.text)
            position += len(message.text)
            message_ranges.append((message, start, position))
        return "".join(text_parts), message_ranges

    @staticmethod
    def _slice_message_refs(
        message_ranges: list[tuple[TurnMessageRef, int, int]],
        range_start: int,
        range_end: int,
    ) -> list[TurnMessageRef]:
        refs: list[TurnMessageRef] = []
        for message, message_start, message_end in message_ranges:
            start = max(range_start, message_start)
            end = min(range_end, message_end)
            if start >= end:
                continue
            text = message.text[start - message_start : end - message_start]
            if text:
                refs.append(message.model_copy(update={"text": text}))
        return refs

    @staticmethod
    def _prefix_end_for_budget(text: str, token_budget: int) -> int:
        if token_budget <= 0:
            return 0
        low = 0
        high = len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _estimate_tokens(text[:middle]) <= token_budget:
                low = middle
            else:
                high = middle - 1
        return low

    @staticmethod
    def _suffix_start_for_budget(text: str, token_budget: int) -> int:
        if token_budget <= 0:
            return len(text)
        low = 0
        high = len(text)
        while low < high:
            middle = (low + high) // 2
            if _estimate_tokens(text[middle:]) <= token_budget:
                high = middle
            else:
                low = middle + 1
        return low

    def _compact_turn(
        self,
        original: Turn,
        parts: _TurnCompactionParts,
        summary: TurnCompactionSummary,
    ) -> tuple[Turn, TurnCompactionResult]:
        full_text, _ = self._flatten_extractable_messages(original)
        head_tokens = _estimate_tokens(parts.head_text)
        tail_tokens = _estimate_tokens(parts.tail_text)
        result = TurnCompactionResult(
            head_text=parts.head_text,
            head_tokens=head_tokens,
            tail_text=parts.tail_text,
            tail_tokens=tail_tokens,
            middle_summary=summary,
            original_token_count=_estimate_tokens(full_text),
            is_lossy=True,
        )

        summary_text = self._format_summary(summary)
        compacted_turn = Turn(
            messages=self._build_compacted_messages(original, parts, summary_text),
            boundary="complete",
            token_count=head_tokens + tail_tokens + _estimate_tokens(summary_text),
        )
        return compacted_turn, result

    def _build_compacted_messages(
        self,
        original: Turn,
        parts: _TurnCompactionParts,
        summary_text: str,
    ) -> list[TurnMessageRef]:
        messages: list[TurnMessageRef] = list(parts.head_messages)
        if not messages and parts.head_text:
            first = original.messages[0] if original.messages else None
            messages.append(
                TurnMessageRef(
                    text=parts.head_text,
                    role=first.role if first else "user",
                    timestamp=first.timestamp if first else None,
                    message_index=first.message_index if first else 0,
                    is_extractable=True,
                )
            )

        if summary_text:
            messages.append(
                TurnMessageRef(
                    text=f"[Compacted context summary]\n{summary_text}",
                    role="system",
                    timestamp=None,
                    message_index=-1,
                    is_extractable=False,
                )
            )

        if parts.tail_messages:
            messages.extend(parts.tail_messages)
        elif parts.tail_text:
            last = original.messages[-1] if original.messages else None
            messages.append(
                TurnMessageRef(
                    text=parts.tail_text,
                    role=last.role if last else "assistant",
                    timestamp=last.timestamp if last else None,
                    message_index=last.message_index if last else 0,
                    is_extractable=True,
                )
            )
        return messages

    @staticmethod
    def _format_summary(summary: TurnCompactionSummary) -> str:
        data = summary.model_dump(exclude_defaults=True)
        return json.dumps(data, ensure_ascii=False) if data else ""

    async def _summarize_middle(self, middle_text: str) -> TurnCompactionSummary:
        if not middle_text:
            return TurnCompactionSummary()
        if self._llm_client is None:
            return self._fallback_summary(middle_text)
        try:
            segments = self._split_for_summary_context(middle_text)
            summaries = [await self._summarize_once(segment, mode="segment") for segment in segments]
            while len(summaries) > 1:
                summaries = await self._reduce_summaries(summaries)
            return summaries[0]
        except Exception:
            logger.warning("long_turn_summary_failed", exc_info=True)
            return self._fallback_summary(middle_text)

    async def _reduce_summaries(
        self,
        summaries: list[TurnCompactionSummary],
    ) -> list[TurnCompactionSummary]:
        text = "\n".join(summary.model_dump_json() for summary in summaries)
        return [await self._summarize_once(segment, mode="reduce") for segment in self._split_for_summary_context(text)]

    async def _summarize_once(self, middle_text: str, *, mode: str) -> TurnCompactionSummary:
        response = await self._llm_client.chat(
            task="memory.add.long_turn_summary",
            messages=[
                {"role": "system", "content": _LONG_TURN_SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({"mode": mode, "middle_text": middle_text}, ensure_ascii=False),
                },
            ],
            format_parser=_parse_compaction_summary,
            max_tokens=self._config.compaction_summary_output_token_budget,
        )
        if isinstance(response.parsed, TurnCompactionSummary):
            return response.parsed
        return TurnCompactionSummary.model_validate(response.parsed)

    def _split_for_summary_context(self, text: str) -> list[str]:
        budget = self._config.compaction_summary_context_token_budget
        if _estimate_tokens(text) <= budget:
            return [text]

        segments: list[str] = []
        remaining = text
        while remaining:
            end = self._prefix_end_for_budget(remaining, budget)
            if end <= 0:
                end = 1
            segments.append(remaining[:end])
            remaining = remaining[end:]
        return segments

    @staticmethod
    def _fallback_summary(middle_text: str) -> TurnCompactionSummary:
        return TurnCompactionSummary(
            general_summary=f"[Compacted middle section omitted: {_estimate_tokens(middle_text)} tokens]"
        )

    # Phase 5: separate evidence/context and attach sliding history

    def _prepare_chunks(
        self,
        chunks: list[Chunk],
        compactions_by_chunk: dict[int, tuple[PreparedTurnCompaction, ...]],
    ) -> list[PreparedMessageChunk]:
        prepared: list[PreparedMessageChunk] = []
        previous_history: HistoryPack | None = None
        previous_chunk: Chunk | None = None

        for chunk in chunks:
            if chunk.chunk_index == 0:
                history = self._pack_history([])
            else:
                if previous_history is None or previous_chunk is None:
                    raise RuntimeError("chunk plan must be ordered and start at chunk index 0")
                available_turns = list(previous_history.in_request_history) + list(previous_chunk.turns)
                history = HistoryPack(
                    external_history=[],
                    in_request_history=self._pack_history_turns(available_turns),
                )
                history.token_usage = sum(turn.token_count for turn in history.in_request_history)

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

    def _pack_history(self, external_turns: list[Turn]) -> HistoryPack:
        packed = self._pack_history_turns(external_turns)
        return HistoryPack(
            external_history=packed,
            in_request_history=[],
            token_usage=sum(turn.token_count for turn in packed),
        )

    def _pack_history_turns(self, turns: list[Turn]) -> list[Turn]:
        if not turns:
            return []

        packed: list[Turn] = []
        total_tokens = 0
        for turn in reversed(turns):
            next_total = total_tokens + turn.token_count
            if len(packed) < self._config.history_min_turn_count:
                if next_total > self._config.history_hard_token_budget:
                    break
                packed.append(turn)
                total_tokens = next_total
                continue
            if next_total > self._config.history_soft_token_budget:
                break
            if next_total > self._config.history_hard_token_budget:
                break
            packed.append(turn)
            total_tokens = next_total

        packed.reverse()
        return packed
