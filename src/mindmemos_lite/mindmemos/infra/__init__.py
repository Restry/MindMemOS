from .retry import AsyncRetryProxy, retry_delay, run_sync_with_retry


def setup_tracer_provider(*args, **kwargs):
    """Load the optional telemetry implementation only when it is requested."""

    from .telemetry import setup_tracer_provider as _setup_tracer_provider

    return _setup_tracer_provider(*args, **kwargs)


def shutdown_tracer_provider() -> None:
    """Shut down optional telemetry without importing it at package startup."""

    from .telemetry import shutdown_tracer_provider as _shutdown_tracer_provider

    _shutdown_tracer_provider()


__all__ = [
    "AsyncRetryProxy",
    "retry_delay",
    "run_sync_with_retry",
    "setup_tracer_provider",
    "shutdown_tracer_provider",
]
