"""Slim CLIP BPE tokenizer ported from OpenAI's CLIP repo.

Replaces a ~50MB transformers dependency with ~130 lines of MIT-licensed
Python plus the same bpe_simple_vocab_16e6.txt.gz vocab file (~1.3MB)
that the original CLIP uses. MobileCLIP shares this tokenizer.

Original source: github.com/openai/CLIP/blob/main/clip/simple_tokenizer.py
License: MIT (Copyright (c) 2021 OpenAI). Reproduced here under that license.

Drops these heavy deps that we were pulling just for tokenization:
- transformers (~50MB)
- tokenizers (~5MB Rust binding)
- safetensors, filelock, packaging, regex transitive load

Adds only:
- ftfy (~50KB pure Python, text fixing)
- regex (~700KB compiled, needed for \\p{L} / \\p{N} Unicode classes)

Net bundle savings: ~50MB compressed in the PyInstaller binary.
"""
from __future__ import annotations

import gzip
import html
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

import ftfy
import numpy as np
import regex as re


SOT_TOKEN = "<|startoftext|>"
EOT_TOKEN = "<|endoftext|>"
CONTEXT_LENGTH = 77  # CLIP's fixed text input length


def _vocab_path() -> Path:
    """Resolve where bpe_simple_vocab_16e6.txt.gz lives. Three lookups:
    1. SPOTTED_CLIP_VOCAB env var (developer override).
    2. PyInstaller bundle: <_MEIPASS>/clip_tokenizer/bpe_simple_vocab_16e6.txt.gz.
    3. Source-tree dev location: sidecar/vendor/clip_tokenizer/bpe_simple_vocab_16e6.txt.gz.
    """
    env = os.environ.get("SPOTTED_CLIP_VOCAB")
    if env and Path(env).is_file():
        return Path(env)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = Path(base) / "clip_tokenizer" / "bpe_simple_vocab_16e6.txt.gz"
        if cand.is_file():
            return cand
    here = Path(__file__).resolve().parent.parent
    cand = here / "sidecar" / "vendor" / "clip_tokenizer" / "bpe_simple_vocab_16e6.txt.gz"
    if cand.is_file():
        return cand
    raise FileNotFoundError(
        "CLIP BPE vocab not found. Set SPOTTED_CLIP_VOCAB or stage the file "
        "under sidecar/vendor/clip_tokenizer/."
    )


@lru_cache()
def _bytes_to_unicode() -> dict[int, str]:
    """Reversible byte-to-unicode map that keeps BPE from barfing on
    whitespace/control characters. Standard GPT-2/CLIP BPE preprocessing."""
    bs = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("¡"), ord("¬") + 1))
        + list(range(ord("®"), ord("ÿ") + 1))
    )
    cs = bs[:]
    n = 0
    for b in range(2 ** 8):
        if b not in bs:
            bs.append(b)
            cs.append(2 ** 8 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


def _get_pairs(word: tuple[str, ...]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    prev = word[0]
    for ch in word[1:]:
        pairs.add((prev, ch))
        prev = ch
    return pairs


def _basic_clean(text: str) -> str:
    text = ftfy.fix_text(text)
    text = html.unescape(html.unescape(text))
    return text.strip()


def _whitespace_clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class SimpleTokenizer:
    """Pure-Python CLIP BPE tokenizer. Loaded once; `tokenize(prompts)`
    returns a (N, 77) int32 numpy array ready for MobileCLIP's text model."""

    def __init__(self, bpe_path: Optional[str] = None) -> None:
        self.byte_encoder = _bytes_to_unicode()
        bpe_path = bpe_path or str(_vocab_path())
        merges_raw = gzip.open(bpe_path).read().decode("utf-8").split("\n")
        merges_raw = merges_raw[1 : 49152 - 256 - 2 + 1]
        merges = [tuple(m.split()) for m in merges_raw]

        vocab = list(_bytes_to_unicode().values())
        vocab = vocab + [v + "</w>" for v in vocab]
        for merge in merges:
            vocab.append("".join(merge))
        vocab.extend([SOT_TOKEN, EOT_TOKEN])

        self.encoder = {tok: i for i, tok in enumerate(vocab)}
        self.bpe_ranks = {pair: i for i, pair in enumerate(merges)}
        self._cache: dict[str, str] = {
            SOT_TOKEN: SOT_TOKEN,
            EOT_TOKEN: EOT_TOKEN,
        }
        self._sot_id = self.encoder[SOT_TOKEN]
        self._eot_id = self.encoder[EOT_TOKEN]

        # CLIP's tokenizer regex — uses Unicode property classes (\p{L},
        # \p{N}) which the stdlib `re` doesn't support. That's the whole
        # reason we depend on `regex`.
        self._pat = re.compile(
            r"""<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+""",
            re.IGNORECASE,
        )

    def _bpe(self, token: str) -> str:
        if token in self._cache:
            return self._cache[token]
        word: tuple[str, ...] = tuple(token[:-1]) + (token[-1] + "</w>",)
        pairs = _get_pairs(word)
        if not pairs:
            return token + "</w>"
        while True:
            bigram = min(pairs, key=lambda p: self.bpe_ranks.get(p, float("inf")))
            if bigram not in self.bpe_ranks:
                break
            first, second = bigram
            new_word: list[str] = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                except ValueError:
                    new_word.extend(word[i:])
                    break
                new_word.extend(word[i:j])
                i = j
                if word[i] == first and i < len(word) - 1 and word[i + 1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            word = tuple(new_word)
            if len(word) == 1:
                break
            pairs = _get_pairs(word)
        joined = " ".join(word)
        self._cache[token] = joined
        return joined

    def _encode_one(self, text: str) -> list[int]:
        ids: list[int] = []
        text = _whitespace_clean(_basic_clean(text)).lower()
        for token in re.findall(self._pat, text):
            token = "".join(self.byte_encoder[b] for b in token.encode("utf-8"))
            ids.extend(self.encoder[bpe_token] for bpe_token in self._bpe(token).split(" "))
        return ids

    def tokenize(self, prompts: list[str], context_length: int = CONTEXT_LENGTH) -> np.ndarray:
        """Tokenize a batch of prompts to a (len(prompts), context_length)
        int32 numpy array. Each row: [SOT, ...prompt-tokens, EOT, 0, 0, ...]
        truncating prompts that would otherwise exceed context_length-2."""
        out = np.zeros((len(prompts), context_length), dtype=np.int32)
        for row, prompt in enumerate(prompts):
            ids = [self._sot_id] + self._encode_one(prompt) + [self._eot_id]
            if len(ids) > context_length:
                # Truncate but keep the EOT so the model still sees a
                # well-formed end-of-sequence marker.
                ids = ids[: context_length - 1] + [self._eot_id]
            out[row, : len(ids)] = ids
        return out
