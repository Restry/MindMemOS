"""Source-aware text segment used by extraction provenance mapping."""

from typing import Any

from pydantic import BaseModel, Field

from ...typing import SourceRef


class SourceAwareSegment(BaseModel):
    """A text segment with enough source metadata to build graph edges."""

    segment_id: str
    text: str
    source_ref: SourceRef
    message_index: int
    role: str | None = None
    timestamp: int | None = None
    start_offset: int = 0
    end_offset: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
