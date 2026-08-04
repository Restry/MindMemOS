"""Backward-compatible imports for service errors.

Error definitions live in :mod:`mindmemos_skill.errors`; this module remains as
an import shim for callers using the old service-local path.
"""

from ..errors import SkillCapabilityUnavailableError, SkillServiceClosedError

__all__ = ["SkillCapabilityUnavailableError", "SkillServiceClosedError"]
