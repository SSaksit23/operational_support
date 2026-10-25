"""Text embeddings behind one small interface.

HashingEmbedder runs locally with no API key: character n-grams hashed into
a fixed vector. It matches shared words and names (Thai, Chinese, English)
but not meaning, so "elderly" will not find "อายุ 82". Production should
use a real model; OpenAIEmbedder matches the text-embedding-3-large model
already cached in the AI Marketing Team project.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    def __init__(self, dim: int = 512, ngram: tuple[int, int] = (2, 4)):
        self.dim = dim
        self.ngram = ngram

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for token in re.findall(r"\w+", text.lower()):
            padded = f" {token} "
            for n in range(self.ngram[0], self.ngram[1] + 1):
                for i in range(max(1, len(padded) - n + 1)):
                    h = int.from_bytes(hashlib.blake2b(padded[i:i + n].encode(), digest_size=8).digest(), "little")
                    v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t).tolist() for t in texts]


class OpenAIEmbedder:
    """Needs OPENAI_API_KEY and the openai package. Not exercised by the tests."""

    def __init__(self, model: str = "text-embedding-3-large", dim: int = 1024):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.model, self.dim = model, dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self.client.embeddings.create(model=self.model, input=texts, dimensions=self.dim)
        return [d.embedding for d in resp.data]
