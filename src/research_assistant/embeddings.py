"""Embedding adapters used by the research retrieval pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text):
        value = match.group(0).lower()
        if "\u4e00" <= value[0] <= "\u9fff":
            tokens.extend(value)
            tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return tokens


class HashingEmbedder:
    """Deterministic dependency-light embedder for tests and offline fallback."""

    def __init__(self, dimensions: int = 384):
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"hashing-v1-{self.dimensions}"

    def embed(self, text: str) -> list[float]:
        counts = Counter(tokenize(text))
        vector = [0.0] * self.dimensions
        for token, count in counts.items():
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1 if digest[4] % 2 == 0 else -1
            vector[index] += sign * (1.0 + math.log(count))
        return normalize(vector)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


class SentenceTransformerEmbedder:
    """Async adapter around SentenceTransformer asymmetric retrieval methods.

    The model is loaded lazily and all synchronous inference is isolated in a
    dedicated executor so research calls do not block the application's event loop.
    """

    def __init__(
        self,
        model: str,
        *,
        revision: str | None = None,
        device: str = "auto",
        batch_size: int = 32,
        normalize_embeddings: bool = True,
        cache_folder: str | None = None,
        local_files_only: bool = False,
        executor_workers: int = 1,
    ) -> None:
        if not model.strip():
            raise ValueError("embedding model must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.model_name = model
        self.revision = revision
        self.device = device
        self.batch_size = batch_size
        self.normalize_embeddings = normalize_embeddings
        self.cache_folder = cache_folder
        self.local_files_only = local_files_only
        self._model: Any | None = None
        self._dimensions: int | None = None
        self._load_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, executor_workers),
            thread_name_prefix="research-embedding",
        )

    @property
    def model_id(self) -> str:
        revision = self.revision or "default"
        normalized = "normalized" if self.normalize_embeddings else "raw"
        return f"sentence-transformers:{self.model_name}@{revision}:{normalized}"

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            model = self._load_model()
            dimension = model.get_embedding_dimension()
            if not dimension:
                raise RuntimeError("embedding model did not expose a vector dimension")
            self._dimensions = int(dimension)
        return self._dimensions

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "SentenceTransformers embedding requires the 'semantic-rag' "
                    "optional dependency group"
                ) from exc
            kwargs: dict[str, Any] = {
                "revision": self.revision,
                "cache_folder": self.cache_folder,
                "local_files_only": self.local_files_only,
            }
            if self.device != "auto":
                kwargs["device"] = self.device
            self._model = SentenceTransformer(self.model_name, **kwargs)
            return self._model

    def _encode_documents_sync(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._load_model().encode_document(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [vector.tolist() for vector in vectors]

    def _encode_query_sync(self, text: str) -> list[float]:
        vectors = self._load_model().encode_query(
            [text],
            batch_size=1,
            normalize_embeddings=self.normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors[0].tolist()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._encode_documents_sync, texts
        )

    async def embed_query(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._encode_query_sync, text)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


_normalize = normalize
