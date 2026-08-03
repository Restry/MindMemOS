from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mindmemos_lite.components.text import SparseVectorEncoder, TextPreprocessor
from mindmemos_lite.components.text.vectorizer import MEMORY_EMBED_BATCH_SIZE, MemoryVectorizer
from mindmemos_lite.config import TextProcessingConfig
from mindmemos_lite.llm.embedding import EmbedClient
from mindmemos_lite.typing import EmbeddingResponse, EntityWrite


def _text_components():
    config = TextProcessingConfig(
        sparse_hash_dim=128,
        bm25_use_spacy_lemma=False,
        spacy_en_model="missing_en_model",
        spacy_zh_model="missing_zh_model",
    )
    return SparseVectorEncoder(config), TextPreprocessor(config)


@pytest.mark.asyncio
async def test_vectorize_many_uses_native_batches_and_preserves_item_alignment() -> None:
    class RecordingEmbedClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed(self, task: str, text: str | list[str], **kwargs):
            assert task == "memory.add.embed"
            texts = text if isinstance(text, list) else [text]
            self.calls.append(texts)
            return EmbeddingResponse(embeddings=[[float(item.rsplit(" ", 1)[-1])] for item in texts])

    sparse_encoder, text_preprocessor = _text_components()
    embed_client = RecordingEmbedClient()
    vectorizer = MemoryVectorizer(
        sparse_encoder=sparse_encoder,
        embed_client=embed_client,
        text_preprocessor=text_preprocessor,
    )
    items = [
        (
            f"mem-{index}",
            text_preprocessor.preprocess_text(f"memory {index}", include_entities=False),
            f"memory {index}",
        )
        for index in range(5)
    ]

    vectors, pending = await vectorizer.vectorize_many(items, batch_size=2)

    assert [len(call) for call in embed_client.calls] == [2, 2, 1]
    assert [vector.memory_id for vector in vectors] == [f"mem-{index}" for index in range(5)]
    assert [vector.semantic_vector for vector in vectors] == [[float(index)] for index in range(5)]
    assert pending == [False] * 5


@pytest.mark.asyncio
async def test_vectorize_entities_splits_large_embedding_requests() -> None:
    class RecordingEmbedClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed(self, task: str, text: str | list[str], **kwargs):
            assert task == "memory_vectorizer.add.entity"
            texts = text if isinstance(text, list) else [text]
            self.calls.append(texts)
            return EmbeddingResponse(embeddings=[[float(index)] for index, _ in enumerate(texts)])

    sparse_encoder, text_preprocessor = _text_components()
    embed_client = RecordingEmbedClient()
    vectorizer = MemoryVectorizer(
        sparse_encoder=sparse_encoder,
        embed_client=embed_client,
        text_preprocessor=text_preprocessor,
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    entities = [
        EntityWrite(
            entity_id=f"ent-{index}",
            account_id="acct",
            project_id="project",
            api_key_uuid="key",
            user_id="user",
            session_id="session",
            entity_name=f"Entity {index}",
            entity_type="concept",
            created_at=created_at,
        )
        for index in range(MEMORY_EMBED_BATCH_SIZE + 1)
    ]

    vectors, pending = await vectorizer.vectorize_entities(entities)

    assert pending is False
    assert [len(call) for call in embed_client.calls] == [MEMORY_EMBED_BATCH_SIZE, 1]
    assert [vector.entity_id for vector in vectors] == [f"ent-{index}" for index in range(MEMORY_EMBED_BATCH_SIZE + 1)]


@pytest.mark.asyncio
async def test_vectorize_entities_preserves_alignment_after_short_batch_response() -> None:
    class ShortFirstBatchEmbedClient:
        def __init__(self) -> None:
            self.call_count = 0

        async def embed(self, task: str, text: str | list[str], **kwargs):
            assert task == "memory_vectorizer.add.entity"
            texts = text if isinstance(text, list) else [text]
            self.call_count += 1
            if self.call_count == 1:
                return EmbeddingResponse(embeddings=[[1.0]])
            return EmbeddingResponse(embeddings=[[2.0] for _ in texts])

    sparse_encoder, text_preprocessor = _text_components()
    vectorizer = MemoryVectorizer(
        sparse_encoder=sparse_encoder,
        embed_client=ShortFirstBatchEmbedClient(),
        text_preprocessor=text_preprocessor,
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    entities = [
        EntityWrite(
            entity_id=f"ent-{index}",
            account_id="acct",
            project_id="project",
            api_key_uuid="key",
            user_id="user",
            session_id="session",
            entity_name=f"Entity {index}",
            entity_type="concept",
            created_at=created_at,
        )
        for index in range(MEMORY_EMBED_BATCH_SIZE + 1)
    ]

    vectors, pending = await vectorizer.vectorize_entities(entities)

    assert pending is True
    assert len(vectors) == MEMORY_EMBED_BATCH_SIZE + 1
    assert vectors[0].semantic_vector == [1.0]
    assert all(vector.semantic_vector is None for vector in vectors[1:MEMORY_EMBED_BATCH_SIZE])
    assert vectors[MEMORY_EMBED_BATCH_SIZE].semantic_vector == [2.0]


@pytest.mark.asyncio
async def test_embed_client_restores_provider_batch_order_by_index() -> None:
    class OutOfOrderRouter:
        model_list = []

        async def aembedding(self, *, model: str, input: str | list[str], **kwargs):
            assert model == "embedding"
            assert input == ["first", "second", "third"]
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=2, embedding=[2.0]),
                    SimpleNamespace(index=0, embedding=[0.0]),
                    SimpleNamespace(index=1, embedding=[1.0]),
                ],
                model="embedding-test",
                usage=None,
                _hidden_params={},
            )

    response = await EmbedClient(OutOfOrderRouter()).embed(
        task="memory.add.embed",
        text=["first", "second", "third"],
        expected_dim=1,
    )

    assert response.embeddings == [[0.0], [1.0], [2.0]]
