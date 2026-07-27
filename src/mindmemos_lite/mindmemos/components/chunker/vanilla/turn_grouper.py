"""Turn grouping for vanilla add pipeline chunking."""
# ruff: noqa: E701, E731

from __future__ import annotations

from ....config import VanillaAddChunkerConfig
from ....typing import DialogueMessage, TextMessage, Turn, TurnBoundary, TurnMessageRef

_STANDARD_ROLES = {"user", "assistant", "system", "tool"}


def _estimate_tokens(t: str) -> int:
    """Estimate token count from text using whitespace and CJK heuristic."""
    is_cjk = lambda c: "一" <= c <= "鿿" or "㐀" <= c <= "䶿"
    return (
        int(sum(1 for c in t if is_cjk(c)) / 1.5) + len("".join(" " if is_cjk(c) else c for c in t).split()) if t else 0
    )


class TurnGrouper:
    def __init__(self, config: VanillaAddChunkerConfig | None = None) -> None:
        self._gap_sec = (config or VanillaAddChunkerConfig()).time_gap_threshold_seconds

    def group(self, messages: list[tuple[int, DialogueMessage | TextMessage]]) -> list[Turn]:
        refs = self._to_refs(messages)
        if not refs:
            return []
        if any(r.role == "speaker" for r in refs):
            return self._group_multi_speaker(refs)

        turns, cur = [], []
        for ref in refs:
            if (
                ref.role != "system"
                and cur
                and (
                    (ref.role == cur[-1].role and self._is_gap(ref, cur[-1]))
                    or (ref.role == "user" and any(r.role == "assistant" for r in cur))
                )
            ):
                turns.append(self._finalize_turn(cur))
                cur = []
            cur.append(ref)
        if cur:
            turns.append(self._finalize_turn(cur))

        if turns and turns[0].boundary == "orphan" and any(r.role == "user" for t in turns for r in t.messages):
            turns[0].boundary = "open_head"
        return turns

    def _group_multi_speaker(self, refs: list[TurnMessageRef]) -> list[Turn]:
        turns, cur, speakers = [], [], set()
        for ref in refs:
            if ref.role != "system" and cur:
                spkr, last = self._speaker_key(ref), next((m for m in reversed(cur) if m.is_extractable), None)
                repeat = ref.is_extractable and spkr in speakers and last and self._speaker_key(last) != spkr
                if self._is_gap(ref, cur[-1]) or repeat:
                    turns.append(self._finalize_turn(cur))
                    cur, speakers = [], set()
            cur.append(ref)
            if ref.is_extractable:
                speakers.add(self._speaker_key(ref))
        if cur:
            turns.append(self._finalize_turn(cur))
        return turns

    def _is_gap(self, r1: TurnMessageRef, r2: TurnMessageRef) -> bool:
        return (
            r1.timestamp is not None
            and r2.timestamp is not None
            and abs(r1.timestamp - r2.timestamp) / 1000.0 > self._gap_sec
        )

    def _speaker_key(self, ref: TurnMessageRef) -> str:
        return (ref.speaker or ref.raw_role or ref.role).strip().lower()

    def _normalize_role(self, role: str) -> tuple[str, str | None]:
        norm = (role or "").strip().lower().replace("-", "_").replace(" ", "_")
        return (norm, None) if norm in _STANDARD_ROLES else ("speaker", (role or "").strip() or None)

    def _to_refs(self, messages: list[tuple[int, DialogueMessage | TextMessage]]) -> list[TurnMessageRef]:
        refs = []
        for idx, msg in messages:
            is_txt = isinstance(msg, TextMessage)
            txt = msg.text if is_txt else msg.content
            if not txt.strip():
                continue

            role, spkr = ("user", None) if is_txt else self._normalize_role(msg.role)
            raw, ts = (None, None) if is_txt else (msg.role, msg.timestamp)
            refs.append(
                TurnMessageRef(
                    text=txt,
                    role=role,
                    raw_role=raw,
                    speaker=spkr,
                    timestamp=ts,
                    message_index=idx,
                    is_extractable=(role != "system"),
                )
            )
        return refs

    def _finalize_turn(self, refs: list[TurnMessageRef]) -> Turn:
        ns = [r for r in refs if r.role != "system"]
        roles = [r.role for r in ns]

        b: TurnBoundary = "complete"
        if not ns:
            b = "complete"
        elif "speaker" in roles:
            b = "complete" if len({self._speaker_key(r) for r in ns if r.role == "speaker"}) >= 2 else "open_tail"
        elif "user" not in roles:
            b = "orphan"
        elif "assistant" not in roles:
            b = "open_tail"
        else:
            b = "open_head" if roles[0] == "assistant" else "complete"

        return Turn(messages=refs, boundary=b, token_count=sum(_estimate_tokens(r.text) for r in refs))
