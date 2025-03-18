# Copyright (C) 2021-2025, Mindee.

# This program is licensed under the Apache License 2.0.
# See LICENSE or go to <https://opensource.org/licenses/Apache-2.0> for full license details.

import numpy as np

from ..core import RecognitionPostProcessor

__all__ = ["_VIPTR", "_VIPTRPostProcessor"]


class _VIPTR:
    vocab: str
    max_length: int

    def build_target(
        self,
        gts: list[str],
    ) -> tuple[np.ndarray, list[int]]:
        """Encodes a list of gts sequences into a np.array and returns the corresponding*
        sequence lengths.

        Args:
            gts: list of ground-truth labels

        Returns:
            A tuple of 2 tensors: Encoded labels and sequence lengths (for each entry of the batch)
        """
        # pad short inputs to max_length
        seq_len = [len(word) for word in gts]
        encoded = np.zeros((len(gts), self.max_length))
        for i, seq in enumerate(gts):
            encoded[i][: len(seq)] = [self.vocab.index(x) + 1 for x in seq]  # 0 is reserved
        return encoded, seq_len


class _VIPTRPostProcessor(RecognitionPostProcessor):
    """Abstract class to postprocess the raw output of the model

    Args:
        vocab: string containing the ordered sequence of supported characters
    """

    def __init__(
        self,
        vocab: str,
    ) -> None:
        # https://github.com/cxfyxl/VIPTR/blob/main/utils.py

        super().__init__(vocab)
