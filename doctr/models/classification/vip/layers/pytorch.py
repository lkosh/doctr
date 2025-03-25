# Copyright (C) 2021-2025, Mindee.

# This program is licensed under the Apache License 2.0.
# See LICENSE or go to <https://opensource.org/licenses/Apache-2.0> for full license details.

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Tuple


from doctr.models.modules.layers import DropPath
from doctr.models.modules.transformer import PositionwiseFeedForward
from doctr.models.utils import conv_sequence_pt


__all__ = [
    "PatchEmbed",
    "Attention",
    "MultiHeadSelfAttention",
    "OverlappingShiftedRelativeAttention",
    "OSRABlock",
    "PatchMerging",
    "LePEAttention",
    "CrossShapedWindowAttention",
]





class PatchEmbed(nn.Module):
    """
    Patch embedding layer for Vision Permutable Extractor.

    This layer reduces the spatial resolution of the input tensor by a factor of 4 in total
    (two consecutive strides of 2). It then permutes the output into `(b, h, w, c)` form.

    Args:
        in_channels: Number of channels in the input images.
        embed_dim: Dimensionality of the embedding (i.e., output channels).
    """

    def __init__(self, in_channels: int = 3, embed_dim: int = 128) -> None:
        super().__init__()
        self.embed_dim = embed_dim
        self.proj = nn.Sequential(
            *conv_sequence_pt(
                in_channels, embed_dim // 2, kernel_size=3, stride=2, padding=1, bias=False, bn=True, relu=False
            ),
            nn.GELU(),
            *conv_sequence_pt(
                embed_dim // 2, embed_dim, kernel_size=3, stride=2, padding=1, bias=False, bn=True, relu=False
            ),
            nn.GELU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass for PatchEmbed.

        Args:
            x: A float tensor of shape (b, c, h, w).

        Returns:
            A float tensor of shape (b, h/4, w/4, embed_dim).
        """
        return self.proj(x).permute(0, 2, 3, 1)


class Attention(nn.Module):
    """
    Standard multi-head attention module.

    This module applies self-attention across the input sequence using 'num_heads' heads.

    Args:
        dim: Dimensionality of the input embeddings.
        num_heads: Number of attention heads.
        qkv_bias: If True, adds a learnable bias to the query, key, value projections.
        attn_drop: Dropout rate applied to the attention map.
        proj_drop: Dropout rate applied to the final output projection.
    """

    def __init__(
        self,
        dim: int,
        num_heads: int = 8,
        qkv_bias: bool = False,
        attn_drop: float = 0.0,
        proj_drop: float = 0.0,
    ) -> None:
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim**-0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass for Attention.

        Args:
            x: A float tensor of shape (b, n, c), where n is the sequence length and c is
                the embedding dimension.

        Returns:
            A float tensor of shape (b, n, c) with attended information.
        """
        _, n, c = x.shape
        qkv = self.qkv(x).reshape((-1, n, 3, self.num_heads, c // self.num_heads)).permute((2, 0, 3, 1, 4))
        q, k, v = qkv[0] * self.scale, qkv[1], qkv[2]

        attn = q.matmul(k.permute((0, 1, 3, 2)))
        attn = nn.functional.softmax(attn, dim=-1)
        attn = self.attn_drop(attn)

        x = attn.matmul(v).permute((0, 2, 1, 3)).contiguous().reshape((-1, n, c))
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class MultiHeadSelfAttention(nn.Module):
    """
    Multi-head Self Attention block with an MLP for feed-forward processing.

    This block normalizes the input, applies attention mixing, adds a residual connection,
    then applies an MLP with another residual connection.

    Args:
        dim: Dimensionality of input embeddings.
        num_heads: Number of attention heads.
        mlp_ratio: Expansion factor for the internal dimension of the MLP.
        qkv_bias: If True, adds a learnable bias to the query, key, value projections.
        drop_path_rate: Drop path rate. If > 0, applies stochastic depth.
    """

    def __init__(
        self,
        dim: int,
        num_heads: int,
        mlp_ratio: float = 4.0,
        qkv_bias: bool = False,
        drop_path_rate: float = 0.0,
    ) -> None:
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)

        self.mixer = Attention(
            dim,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
        )

        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0.0 else nn.Identity()
        self.norm2 = nn.LayerNorm(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = PositionwiseFeedForward(d_model=dim, ffd=mlp_hidden_dim, dropout=0.0, activation_fct=nn.GELU())

    def forward(self, x: torch.Tensor, size: Optional[Tuple[int, int]] = None) -> torch.Tensor:
        """
        Forward pass for MultiHeadSelfAttention.

        Args:
            x: A float tensor of shape (b, n, c).
            size: An optional tuple (h, w) if needed by some modules (unused here).

        Returns:
            A float tensor of shape (b, n, c) after self-attention and MLP.
        """
        x = x + self.drop_path(self.mixer(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class OverlappingShiftedRelativeAttention(nn.Module):
    """
    Overlapping Shifted Relative Attention (OSRA).

    This attention mechanism downsamples the input according to 'sr_ratio' (spatial reduction ratio),
    applies a local convolution for feature enhancement, and computes attention with an
    optional relative positional encoding. It captures dependencies in an overlapping manner.

    Args:
        dim: The embedding dimension of the tokens.
        num_heads: Number of attention heads.
        qk_scale: Optional override of q-k scaling factor. Defaults to head_dim^-0.5 if None.
        attn_drop: Dropout rate for attention weights.
        sr_ratio: Spatial reduction ratio. If > 1, a depthwise conv-based downsampling is applied.
    """

    def __init__(
        self,
        dim: int,
        num_heads: int = 1,
        qk_scale: Optional[float] = None,
        attn_drop: float = 0.0,
        sr_ratio: int = 1,
    ) -> None:
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divisible by num_heads {num_heads}."
        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5
        self.sr_ratio = sr_ratio
        self.q = nn.Conv2d(dim, dim, kernel_size=1)
        self.kv = nn.Conv2d(dim, dim * 2, kernel_size=1)
        self.attn_drop = nn.Dropout(attn_drop)

        if sr_ratio > 1:
            self.sr = nn.Sequential(
                *conv_sequence_pt(
                    dim,
                    dim,
                    kernel_size=sr_ratio + 3,
                    stride=sr_ratio,
                    padding=(sr_ratio + 3) // 2,
                    groups=dim,
                    bias=False,
                    bn=True,
                    relu=False,
                ),
                nn.GELU(),
                *conv_sequence_pt(dim, dim, kernel_size=1, groups=dim, bias=False, bn=True, relu=False),
            )
        else:
            self.sr = nn.Identity()

        self.local_conv = nn.Conv2d(dim, dim, kernel_size=3, padding=1, groups=dim)

    def forward(
        self,
        x: torch.Tensor,
        size: Tuple[int, int],
        relative_pos_enc: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass for OverlappingShiftedRelativeAttention.

        Args:
            x: A float tensor of shape (b, n, c) where n = h * w.
            size: A tuple (h, w) giving the height and width of the original feature map.
            relative_pos_enc: An optional relative positional encoding tensor to be
                added to the attention logits.

        Returns:
            A float tensor of shape (b, n, c) with updated representations.
        """
        b, n, c = x.shape
        h, w = size
        x = x.permute(0, 2, 1).contiguous().reshape(b, -1, h, w)

        q = self.q(x).reshape(b, self.num_heads, c // self.num_heads, -1).transpose(-1, -2)
        kv = self.sr(x)
        kv = self.local_conv(kv) + kv
        k, v = torch.chunk(self.kv(kv), chunks=2, dim=1)
        k = k.reshape(b, self.num_heads, c // self.num_heads, -1)
        v = v.reshape(b, self.num_heads, c // self.num_heads, -1).transpose(-1, -2)

        attn = (q @ k) * self.scale
        if relative_pos_enc is not None:
            if attn.shape[2:] != relative_pos_enc.shape[2:]:
                relative_pos_enc = F.interpolate(
                    relative_pos_enc, size=attn.shape[2:], mode="bicubic", align_corners=False
                )
            attn = attn + relative_pos_enc

        attn = torch.softmax(attn, dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(-1, -2).contiguous().reshape(b, c, -1)
        x = x.permute(0, 2, 1).contiguous()
        return x


class OSRABlock(nn.Module):
    """
    Global token mixing block using Overlapping Shifted Relative Attention (OSRA).

    Captures global dependencies by aggregating context from a wider spatial area,
    followed by a position-wise feed-forward layer.

    Args:
        dim: Embedding dimension of tokens.
        sr_ratio: Spatial reduction ratio for OSRA.
        num_heads: Number of attention heads.
        mlp_ratio: Expansion factor for the MLP hidden dimension.
        drop_path: Drop path rate. If > 0, applies stochastic depth.
    """

    def __init__(
        self,
        dim: int = 64,
        sr_ratio: int = 1,
        num_heads: int = 1,
        mlp_ratio: float = 4.0,
        drop_path: float = 0.0,
    ) -> None:
        super().__init__()
        mlp_hidden_dim = int(dim * mlp_ratio)

        self.norm1 = nn.LayerNorm(dim)
        self.token_mixer = OverlappingShiftedRelativeAttention(dim, num_heads=num_heads, sr_ratio=sr_ratio)
        self.norm2 = nn.LayerNorm(dim)

        self.mlp = PositionwiseFeedForward(d_model=dim, ffd=mlp_hidden_dim, dropout=0.0, activation_fct=nn.GELU())
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()

    def forward(self, x: torch.Tensor, relative_pos_enc: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Forward pass for OSRABlock.

        Args:
            x: A float tensor of shape (b, n, c).
            relative_pos_enc: Optional relative positional encoding to be used in attention.

        Returns:
            A float tensor of shape (b, n, c) with globally mixed features.
        """
        x = x + self.drop_path(self.token_mixer(self.norm1(x), relative_pos_enc=relative_pos_enc))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class PatchMerging(nn.Module):
    """
    Patch Merging Layer.

    Reduces the spatial dimension by half along the height. If the input has shape
    (b, h, w, c), the output shape becomes (b, h//2, w, out_dim).

    Args:
        dim: Number of input channels.
        out_dim: Number of output channels after merging.
    """

    def __init__(self, dim: int, out_dim: int) -> None:
        super().__init__()
        self.dim = dim
        self.reduction = nn.Conv2d(dim, out_dim, 3, (2, 1), 1)
        self.norm = nn.LayerNorm(out_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass for PatchMerging.

        Args:
            x: A float tensor of shape (b, h, w, c).

        Returns:
            A float tensor of shape (b, h//2, w, out_dim).
        """
        x = x.permute(0, 3, 1, 2)
        x = self.reduction(x).permute(0, 2, 3, 1)
        return self.norm(x)


class LePEAttention(nn.Module):
    """
    Local Enhancement Positional Encoding (LePE) Attention.

    This is used for computing attention in cross-shaped windows (part of CrossShapedWindowAttention),
    and includes a learnable position encoding via depthwise convolution.

    Args:
        dim: Embedding dimension.
        resolution: The overall resolution (h, w) of the feature map.
        idx: Index used to determine the direction/split dimension for cross-shaped windows:
            - idx == -1: no splitting (attend to all).
            - idx == 0: vertical split.
            - idx == 1: horizontal split.
        split_size: Size of the split window.
        dim_out: Output dimension; if None, defaults to `dim`.
        num_heads: Number of attention heads.
        attn_drop: Dropout rate for attention weights.
    """

    def __init__(
        self,
        dim: int,
        resolution: Tuple[int, int],
        idx: int,
        split_size: int = 7,
        dim_out: Optional[int] = None,
        num_heads: int = 8,
        attn_drop: float = 0.0,
    ) -> None:
        super().__init__()
        self.dim = dim
        self.dim_out = dim_out or dim
        self.resolution = resolution
        self.split_size = split_size
        self.num_heads = num_heads
        self.idx = idx
        head_dim = dim // num_heads
        self.scale = head_dim**-0.5

        self.get_v = nn.Conv2d(dim, dim, kernel_size=3, stride=1, padding=1, groups=dim)
        self.attn_drop = nn.Dropout(attn_drop)

    def img2windows(self, img: torch.Tensor, h_sp: int, w_sp: int) -> torch.Tensor:
        """
        Slice an image into windows of shape (h_sp, w_sp).

        Args:
            img: A float tensor of shape (b, c, h, w).
            h_sp: The window's height.
            w_sp: The window's width.

        Returns:
            A float tensor of shape (b', h_sp*w_sp, c), where b' = b * (h//h_sp) * (w//w_sp).
        """
        b, c, h, w = img.shape
        img_reshape = img.view(b, c, h // h_sp, h_sp, w // w_sp, w_sp)
        img_perm = img_reshape.permute(0, 2, 4, 3, 5, 1).contiguous().reshape(-1, h_sp * w_sp, c)
        return img_perm

    def windows2img(self, img_splits_hw: torch.Tensor, h_sp: int, w_sp: int, h: int, w: int) -> torch.Tensor:
        """
        Merge windowed images back to the original spatial shape.

        Args:
            img_splits_hw: A float tensor of shape (b', h_sp*w_sp, c).
            h_sp: Window height.
            w_sp: Window width.
            h: Original height.
            w: Original width.

        Returns:
            A float tensor of shape (b, h, w, c).
        """
        b_merged = int(img_splits_hw.shape[0] / (h * w / h_sp / w_sp))
        img = img_splits_hw.view(b_merged, h // h_sp, w // w_sp, h_sp, w_sp, -1)
        img = img.permute(0, 1, 3, 2, 4, 5).contiguous().view(b_merged, h, w, -1)
        return img

    def _get_split(self, size: Tuple[int, int]) -> Tuple[int, int]:
        """
        Determine how to split the height/width for the cross-shaped windows.

        Args:
            size: A tuple (h, w).

        Returns:
            A tuple (h_sp, w_sp) indicating split window dimensions.
        """
        h, w = size
        if self.idx == -1:
            return h, w
        elif self.idx == 0:
            return h, self.split_size
        elif self.idx == 1:
            return self.split_size, w
        else:
            raise ValueError("idx must be -1, 0, or 1")

    def im2cswin(self, x: torch.Tensor, size: Tuple[int, int]) -> torch.Tensor:
        """
        Re-arrange features into cross-shaped windows for Q/K.

        Args:
            x: A float tensor of shape (b, n, c).
            size: A tuple (h, w).

        Returns:
            A float tensor of shape (b', num_heads, h_sp*w_sp, c//num_heads).
        """
        b, n, c = x.shape
        h, w = size
        x = x.transpose(-2, -1).contiguous().view(b, c, h, w)
        h_sp, w_sp = self._get_split(size)

        x = self.img2windows(x, h_sp, w_sp)
        x = x.reshape(-1, h_sp * w_sp, self.num_heads, c // self.num_heads).permute(0, 2, 1, 3).contiguous()
        return x

    def get_lepe(self, x: torch.Tensor, size: Tuple[int, int]) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Compute the learnable position encoding via depthwise convolution.

        Args:
            x: A float tensor of shape (b, n, c).
            size: A tuple (h, w).

        Returns:
            x: A float tensor rearranged for V in shape (b', num_heads, n_window, c//num_heads).
            lepe: A position encoding tensor of the same shape as x.
        """
        b, n, c = x.shape
        h, w = size
        x = x.transpose(-2, -1).contiguous().view(b, c, h, w)
        h_sp, w_sp = self._get_split(size)

        x = x.view(b, c, h // h_sp, h_sp, w // w_sp, w_sp)
        x = x.permute(0, 2, 4, 1, 3, 5).contiguous().reshape(-1, c, h_sp, w_sp)  # b', c, h_sp, w_sp

        lepe = self.get_v(x)
        lepe = lepe.reshape(-1, self.num_heads, c // self.num_heads, h_sp * w_sp).permute(0, 1, 3, 2).contiguous()

        x = x.reshape(-1, self.num_heads, c // self.num_heads, h_sp * w_sp).permute(0, 1, 3, 2)
        return x, lepe

    def forward(self, qkv: torch.Tensor, size: Tuple[int, int]) -> torch.Tensor:
        """
        Forward pass for LePEAttention.

        Splits Q/K/V according to cross-shaped windows, computes attention,
        and returns the combined features.

        Args:
            qkv: A tensor of shape (3, b, n, c) containing Q, K, and V.
            size: A tuple (h, w) giving the height and width of the image/feature map.

        Returns:
            A float tensor of shape (b, n, c) after cross-shaped window attention with LePE.
        """
        q, k, v = qkv[0], qkv[1], qkv[2]

        h, w = size
        b, n, c = q.shape

        h_sp, w_sp = self._get_split(size)
        q = self.im2cswin(q, size)
        k = self.im2cswin(k, size)
        v, lepe = self.get_lepe(v, size)

        q = q * self.scale
        attn = q @ k.transpose(-2, -1)  # (b', head, n_window, n_window)
        attn = nn.functional.softmax(attn, dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v) + lepe
        x = x.transpose(1, 2).reshape(-1, h_sp * w_sp, c)
        # Window2Img
        x = self.windows2img(x, h_sp, w_sp, h, w).view(b, -1, c)
        return x


class CrossShapedWindowAttention(nn.Module):
    """
    Local mixing module, performing attention within cross-shaped windows.

    This captures local patterns by splitting the feature map into two cross-shaped windows:
    vertical and horizontal slices. Each slice is passed to a LePEAttention. Outputs are
    concatenated and projected, followed by an MLP for mixing.

    Args:
        dim: Embedding dimension.
        patches_resolution: A tuple (h, w) indicating height and width of the feature map.
        num_heads: Number of attention heads.
        split_size: Window size for splitting.
        mlp_ratio: Expansion factor for MLP hidden dimension.
        qkv_bias: If True, adds a bias term to Q/K/V projections.
        drop_path: Drop path rate. If > 0, applies stochastic depth.
    """

    def __init__(
        self,
        dim: int,
        patches_resolution: Tuple[int, int],
        num_heads: int,
        split_size: int = 7,
        mlp_ratio: float = 4.0,
        qkv_bias: bool = False,
        drop_path: float = 0.0,
    ) -> None:
        super().__init__()
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.norm1 = nn.LayerNorm(dim)
        self.proj = nn.Linear(dim, dim)

        self.attns = nn.ModuleList([
            LePEAttention(
                dim // 2,
                resolution=patches_resolution,
                idx=i,
                split_size=split_size,
                num_heads=num_heads // 2,
                dim_out=dim // 2,
            )
            for i in range(2)
        ])

        mlp_hidden_dim = int(dim * mlp_ratio)
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()
        self.mlp = PositionwiseFeedForward(d_model=dim, ffd=mlp_hidden_dim, dropout=0.0, activation_fct=nn.GELU())
        self.norm2 = nn.LayerNorm(dim)

    def forward(self, x: torch.Tensor, size: Tuple[int, int]) -> torch.Tensor:
        """
        Forward pass for CrossShapedWindowAttention.

        Args:
            x: A float tensor of shape (b, n, c), where n = h * w.
            size: A tuple (h, w) for the height and width of the feature map.

        Returns:
            A float tensor of shape (b, n, c) after cross-shaped window attention.
        """
        b, _, c = x.shape
        qkv = self.qkv(self.norm1(x)).reshape(b, -1, 3, c).permute(2, 0, 1, 3)

        # Split QKV for each half, then apply cross-shaped window attention
        x1 = self.attns[0](qkv[:, :, :, : c // 2], size)
        x2 = self.attns[1](qkv[:, :, :, c // 2 :], size)

        # Project and merge
        merged = self.proj(torch.cat([x1, x2], dim=2))
        x = x + self.drop_path(merged)

        # MLP
        return x + self.drop_path(self.mlp(self.norm2(x)))
