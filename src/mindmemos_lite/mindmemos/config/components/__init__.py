"""Configuration owned by globally reusable components."""

from .message_chunker import MessageChunkerConfig
from .text_processing import TextProcessingConfig

__all__ = ["MessageChunkerConfig", "TextProcessingConfig"]
