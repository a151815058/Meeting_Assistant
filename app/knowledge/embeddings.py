"""Embedding providers for the knowledge base (REQ-58). The indexer depends only on
``EmbeddingProvider``, so the model can be swapped (EMBEDDING_PROVIDER) like the LLM.

Default: intfloat/multilingual-e5-small as an int8 ONNX model, run locally with onnxruntime
(already installed with faster-whisper). Meeting content never leaves the server for embedding.
The model is downloaded once from Hugging Face at a pinned revision and cached.
"""
import logging
from abc import ABC, abstractmethod

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding failure with a stable code: not_configured, model_unavailable, dimension_mismatch."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class EmbeddingProvider(ABC):
    name: str  # stored with every indexed meeting, so a model change can be detected
    dimensions: int

    @abstractmethod
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Unit-length vectors for documents to be stored."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Unit-length vector for a search query."""


class E5OnnxEmbedder(EmbeddingProvider):
    """E5 models expect "passage: " / "query: " prefixes, mean pooling and L2 normalisation."""

    dimensions = 384
    max_tokens = 512

    def __init__(self, *, repo: str, revision: str, onnx_file: str, tokenizer_file: str,
                 batch_size: int = 16, cpu_threads: int = 2):
        self.name = f"{repo}@{revision[:12]}"
        self._repo, self._revision = repo, revision
        self._onnx_file, self._tokenizer_file = onnx_file, tokenizer_file
        self._batch_size = batch_size
        self._cpu_threads = cpu_threads
        self._session = None
        self._tokenizer = None

    def _load(self):
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            model_path = hf_hub_download(self._repo, self._onnx_file, revision=self._revision)
            tokenizer_path = hf_hub_download(self._repo, self._tokenizer_file, revision=self._revision)
            options = ort.SessionOptions()
            options.intra_op_num_threads = self._cpu_threads
            session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
            tokenizer = Tokenizer.from_file(tokenizer_path)
        except Exception as exc:  # download / file / runtime problems all mean "model unavailable"
            raise EmbeddingError("model_unavailable", str(exc)) from exc
        tokenizer.enable_truncation(self.max_tokens)
        tokenizer.enable_padding()
        self._input_names = {i.name for i in session.get_inputs()}
        self._tokenizer, self._session = tokenizer, session
        logger.info("embedding model %s loaded", self.name)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        self._load()
        vectors = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start:start + self._batch_size]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        encodings = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]
        return mean_pool_normalize(hidden, mask).tolist()

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"passage: {t}" for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"query: {text}"])[0]


def mean_pool_normalize(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Average the token vectors of real (non-padding) tokens, then scale to unit length."""
    weights = mask[..., None].astype(hidden.dtype)
    pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
    return pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)


def get_embedder(app) -> EmbeddingProvider:
    """Provider selected by EMBEDDING_PROVIDER, cached per app (tests inject a fake here)."""
    if "embedding_provider" not in app.extensions:
        cfg = app.config
        if cfg["EMBEDDING_PROVIDER"] != "e5_onnx":
            raise EmbeddingError("not_configured", f"unknown EMBEDDING_PROVIDER {cfg['EMBEDDING_PROVIDER']!r}")
        app.extensions["embedding_provider"] = E5OnnxEmbedder(
            repo=cfg["EMBEDDING_MODEL_REPO"], revision=cfg["EMBEDDING_MODEL_REVISION"],
            onnx_file=cfg["EMBEDDING_ONNX_FILE"], tokenizer_file=cfg["EMBEDDING_TOKENIZER_FILE"],
            batch_size=cfg["EMBEDDING_BATCH_SIZE"], cpu_threads=cfg["EMBEDDING_CPU_THREADS"],
        )
    return app.extensions["embedding_provider"]
