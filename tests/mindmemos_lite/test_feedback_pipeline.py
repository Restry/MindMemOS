from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mindmemos.components.activity import RecentActivityCollector
from mindmemos.components.feedback import DefaultExplicitFeedbackPlanner, FeedbackMemorySearchDecision
from mindmemos.infra.tasking import InMemoryTaskBackend, TaskClient, TaskHandlerRegistry
from mindmemos.pipeline.feedback import MEMORY_FEEDBACK_TOPIC, DefaultFeedbackPipeline
from mindmemos.pipeline.feedback.executor import FeedbackActionExecutor
from mindmemos.pipeline.feedback.explicit import ExplicitFeedbackHandler
from mindmemos.pipeline.feedback.implicit import (
    ImplicitFeedbackHandler,
    ImplicitFeedbackRecordCollector,
    _PendingFeedbackActivityStore,
)
from mindmemos.service.memory import VanillaMemoryService
from mindmemos.service.schema import (
    DialogueMessage as ServiceDialogueMessage,
)
from mindmemos.service.schema import (
    FeedbackMemoryRequest,
    MemoryItem,
    RequestContext,
)
from mindmemos.typing import (
    ActivityRecordSnapshot,
    ChatResponse,
    DialogueMessage,
    EmbeddingResponse,
    FeedbackActionResult,
    FeedbackAddAction,
    FeedbackDeleteAction,
    FeedbackPipelineInput,
    FeedbackPipelineResult,
    FeedbackUpdateAction,
    ImplicitFeedbackRound,
    ImplicitFeedbackSessionMaterial,
    ImplicitFeedbackSignal,
    ImplicitFeedbackSignalResult,
    MemoryDbMutationResult,
    MemoryDbWriteResult,
    MemoryRequestContext,
    MemorySearchItem,
    MemoryView,
    SearchPipelineInput,
    SearchPipelineResult,
    SupplementalSearchQuery,
)


def _context() -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id="00000000-0000-0000-0000-000000000001",
        account_id="acct",
        project_id="proj",
        api_key_uuid="key",
        user_id="user",
        session_id="session",
        scopes=["memory:write"],
    )


class _FakeLLM:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.tasks: list[str] = []
        self.messages: list[list[dict]] = []

    async def chat(self, task, messages, format_parser=None, **_kwargs):
        content = self.contents.pop(0)
        self.tasks.append(task)
        self.messages.append(messages)
        return ChatResponse(
            finish_reason="stop",
            content=content,
            parsed=format_parser(content) if format_parser else None,
        )


class _FakeExecutor:
    def __init__(self) -> None:
        self.actions: list[FeedbackActionResult] = []

    async def execute(self, actions, _context):
        self.actions.extend(actions)
        return actions


class _FakeSearch:
    def __init__(self, memories=()) -> None:
        self.memories = list(memories)
        self.inputs: list[SearchPipelineInput] = []

    async def search(self, inp, _context):
        self.inputs.append(inp)
        return SearchPipelineResult(status="ok", memories=self.memories)


@pytest.mark.asyncio
async def test_explicit_feedback_keeps_source_validation_and_llm_planning() -> None:
    missing = await DefaultFeedbackPipeline().feedback(
        FeedbackPipelineInput(feedback="wrong memory"),
        _context(),
    )
    assert missing.status == "error"
    assert missing.message == "explicit feedback requires messages context"

    llm = _FakeLLM(
        '{"need_search": false, "query": null}',
        """
        {"actions":[{
          "action":"update",
          "target_memory_id":"mem-1",
          "before_content":"User uses conda.",
          "after_content":"User uses uv.",
          "reason":"user correction"
        }]}
        """,
    )
    pipeline = DefaultFeedbackPipeline(
        explicit_handler=ExplicitFeedbackHandler(
            planner=DefaultExplicitFeedbackPlanner(llm_client=llm),
            executor=_FakeExecutor(),
        )
    )
    result = await pipeline.feedback(
        FeedbackPipelineInput(
            feedback="not conda, uv",
            messages=[DialogueMessage(role="user", content="not conda, uv")],
            recalled_memories=[
                MemorySearchItem(
                    id="mem-1",
                    memory="User uses conda.",
                    last_update_at="2026-06-01 00:00:00",
                )
            ],
        ),
        _context(),
    )

    assert result.status == "ok"
    assert result.actions[0].after_content == "User uses uv."
    assert llm.tasks == ["feedback.explicit.search_decision", "feedback.explicit.plan"]


@pytest.mark.asyncio
async def test_explicit_feedback_searches_once_and_deduplicates_by_memory_id() -> None:
    class Planner:
        planned_input = None

        async def decide_memory_search(self, _inp):
            return FeedbackMemorySearchDecision(need_search=True, query="python package manager uv")

        async def plan(self, inp):
            self.planned_input = inp
            return []

    planner = Planner()
    search = _FakeSearch(
        [
            MemorySearchItem(id="mem-1", memory="duplicate", last_update_at=""),
            MemorySearchItem(id="mem-2", memory="User prefers uv.", last_update_at=""),
        ]
    )
    pipeline = DefaultFeedbackPipeline(
        explicit_handler=ExplicitFeedbackHandler(
            planner=planner,
            executor=_FakeExecutor(),
            search_pipeline=search,
        )
    )
    await pipeline.feedback_sync(
        FeedbackPipelineInput(
            feedback="use uv",
            messages=[DialogueMessage(role="user", content="use uv")],
            recalled_memories=[MemorySearchItem(id="mem-1", memory="User uses conda.", last_update_at="")],
        ),
        _context(),
    )

    assert search.inputs == [SearchPipelineInput(query="python package manager uv", search_pipeline="vanilla")]
    assert [item.id for item in planner.planned_input.recalled_memories] == ["mem-1", "mem-2"]


class _FakeFeedbackPersistence:
    def __init__(self) -> None:
        self.plans = []

    async def get_memory(self, _context, memory_id):
        if memory_id != "mem-1":
            return None
        return MemoryView(
            memory_id="mem-1",
            project_id="proj",
            content="User uses conda.",
            mem_type="fact",
            status="active",
        )

    async def apply_mutation_plan(self, _context, plan, *, consistency="fast"):
        self.plans.append((plan, consistency))
        return MemoryDbWriteResult(
            memory_ids=[command.memory.memory_id for command in plan.memory_writes],
            mutations=[
                MemoryDbMutationResult(memory_id=command.memory_id, changed=True)
                for command in [*plan.memory_updates, *plan.memory_deletes]
            ],
        )


class _FakeEmbed:
    async def embed(self, _task=None, _text=None, **_kwargs):
        return EmbeddingResponse(embeddings=[[0.1, 0.2, 0.3]])


class _FakePreprocessor:
    def preprocess_text(self, _text, include_entities=False):
        del include_entities
        return SimpleNamespace(tokens=["user", "uv"])


class _FakeSparse:
    def encode_document(self, _tokens):
        return SimpleNamespace(indices=[1, 2], values=[1.0, 0.5])


@pytest.mark.asyncio
async def test_feedback_executor_preserves_add_update_delete_mutation_shapes() -> None:
    persistence = _FakeFeedbackPersistence()
    executor = FeedbackActionExecutor(
        persistence=persistence,
        embed_client=_FakeEmbed(),
        text_preprocessor=_FakePreprocessor(),
        sparse_encoder=_FakeSparse(),
    )
    result = await executor.execute(
        [
            FeedbackAddAction(after_content="User prefers uv."),
            FeedbackUpdateAction(
                target_memory_id="mem-1",
                before_content="User uses conda.",
                after_content="User uses uv.",
            ),
            FeedbackDeleteAction(target_memory_id="mem-2", before_content="stale"),
        ],
        _context(),
    )

    assert [item.status for item in result] == ["ok", "ok", "ok"]
    assert all(consistency == "strong" for _, consistency in persistence.plans)
    update_plan = persistence.plans[1][0]
    assert update_plan.memory_updates[0].status == "archived"
    write_plan = update_plan.to_write_plan()
    assert write_plan.relationships[0].rel_type == "DERIVED_FROM"
    assert write_plan.relationships[0].target.node_id == "mem-1"
    assert persistence.plans[2][0].memory_deletes[0].reason == "feedback_delete"


@pytest.mark.parametrize("legacy_dependency", ["db_reader", "db_writer"])
def test_feedback_executor_rejects_legacy_persistence_dependencies(legacy_dependency: str) -> None:
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{legacy_dependency}'"):
        FeedbackActionExecutor(**{legacy_dependency: object()})


class _FakeActivityRecorder:
    def __init__(self) -> None:
        self.patches = []
        now = datetime.now(UTC)
        base = {
            "project_id": "proj",
            "user_id": "user",
            "session_id": "session",
            "status": "ok",
            "request_submitted_at": now,
            "messages": [
                {"role": "user", "content": "Please be detailed."},
                {"role": "assistant", "content": "Understood."},
            ],
            "memories": [{"content": "User prefers detailed answers."}],
        }
        self.add_records = [
            ActivityRecordSnapshot(record_id="done", payload={**base, "feedback_processed": True}),
            ActivityRecordSnapshot(record_id="pending", payload={**base, "feedback_processed": False}),
        ]

    async def list_activity_records(self, kind, _scope, **_kwargs):
        return self.add_records if kind == "add" else []

    async def patch_add_record(self, _context, add_record_id, payload):
        self.patches.append((add_record_id, payload))
        return True


class _FakeMemoryPersistence:
    service = object()

    async def list_memories(self, context, *, filters=None, limit=100, cursor=None):
        del context, filters, limit, cursor
        return [
            MemoryView(
                memory_id="mem-written",
                project_id="proj",
                content="User prefers detailed answers.",
                mem_type="fact",
                status="active",
                created_at=datetime.now(UTC),
            )
        ], None


class _FakeQueryRewriter:
    async def rewrite(self, original_query):
        return SupplementalSearchQuery(query=original_query)


@pytest.mark.asyncio
async def test_implicit_collector_filters_processed_records_at_recorder_boundary() -> None:
    recorder = _FakeActivityRecorder()
    collector = ImplicitFeedbackRecordCollector(
        persistence=_FakeMemoryPersistence(),
        operation_recorder=recorder,
        activity_collector=RecentActivityCollector(_PendingFeedbackActivityStore(recorder)),
        query_rewriter=_FakeQueryRewriter(),
        search_pipeline=_FakeSearch(),
    )
    sessions = await collector.collect(_context())

    assert len(sessions) == 1
    assert sessions[0].source_add_record_ids == ["pending"]
    assert [message["content"] for message in sessions[0].rounds[0].messages] == [
        "Please be detailed.",
        "Understood.",
    ]
    await collector.mark_feedback_processed(_context(), ["pending"])
    assert recorder.patches == [("pending", {"feedback_processed": True})]


@pytest.mark.asyncio
async def test_implicit_handler_groups_signals_by_round_and_marks_records() -> None:
    class Collector:
        marked = []

        async def collect(self, _context):
            return [
                ImplicitFeedbackSessionMaterial(
                    session_id="session",
                    rounds=[
                        ImplicitFeedbackRound(
                            messages=[
                                {"role": "user", "content": "Please be detailed."},
                                {"role": "assistant", "content": "Understood."},
                            ]
                        )
                    ],
                    source_add_record_ids=["add-1"],
                )
            ]

        async def mark_feedback_processed(self, _context, add_record_ids):
            self.marked.extend(add_record_ids)

    class Detector:
        async def detect(self, _material):
            return ImplicitFeedbackSignalResult(
                signals=[
                    ImplicitFeedbackSignal(round_index=0, category="long_term"),
                    ImplicitFeedbackSignal(round_index=0, category="task_temporary"),
                    ImplicitFeedbackSignal(round_index=99, category="long_term"),
                ]
            )

    class Planner:
        calls = []

        async def plan(self, *, round_, signals, memories):
            self.calls.append((round_, signals, memories))
            return [FeedbackAddAction(after_content="User prefers detailed answers.")]

    collector = Collector()
    planner = Planner()
    result = await ImplicitFeedbackHandler(
        collector=collector,
        signal_detector=Detector(),
        action_planner=planner,
        executor=_FakeExecutor(),
    ).run(FeedbackPipelineInput(), _context())

    assert result.message == "processed 3 implicit feedback signals in 1 sessions"
    assert [signal.category for signal in planner.calls[0][1]] == ["long_term", "task_temporary"]
    assert collector.marked == ["add-1"]


@pytest.mark.asyncio
async def test_memory_service_owns_sync_and_async_feedback_dispatch() -> None:
    class Add:
        async def add_sync(self, payload, context):
            raise AssertionError((payload, context))

    class Search:
        async def search(self, payload, context):
            raise AssertionError((payload, context))

    class Feedback:
        def __init__(self) -> None:
            self.calls = []

        async def feedback_sync(self, payload, context):
            self.calls.append((payload, context))
            return FeedbackPipelineResult(
                status="ok",
                actions=[FeedbackAddAction(after_content="User prefers uv.", result_memory_id="mem-new")],
            )

    handlers = TaskHandlerRegistry()
    backend = InMemoryTaskBackend(handlers, max_concurrency=1, max_buffered=4)
    task_client = TaskClient(backend, handlers)
    feedback = Feedback()
    service = VanillaMemoryService(
        SimpleNamespace(service=object()),
        config=SimpleNamespace(),
        task_client=task_client,
        add_pipeline=Add(),
        search_pipeline=Search(),
        feedback_pipeline=feedback,
        recorder=SimpleNamespace(),
    )
    context = RequestContext(
        request_id="req",
        account_id="acct",
        project_id="proj",
        api_key_uuid="key",
        user_id="user",
        scopes=("memory:write",),
    )
    request = FeedbackMemoryRequest(
        feedback="use uv",
        messages=(ServiceDialogueMessage(role="user", content="use uv"),),
        recalled_memories=(
            MemoryItem(
                memory_id="mem-1",
                content="User uses conda.",
                memory_type="fact",
                updated_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        ),
    )

    sync_result = await service.feedback(context, request)
    await backend.start()
    try:
        async_result = await service.feedback(
            context,
            FeedbackMemoryRequest(
                feedback=request.feedback,
                messages=request.messages,
                recalled_memories=request.recalled_memories,
                mode="async",
            ),
        )
        await backend.flush(timeout=1)
    finally:
        await backend.close(timeout=1)

    assert sync_result.actions[0].result_memory_id == "mem-new"
    assert async_result.status == "queued"
    assert MEMORY_FEEDBACK_TOPIC in handlers.names()
    assert len(feedback.calls) == 2
    assert feedback.calls[1][0].mode == "sync"
