"""Reverse-complement equivariant modules.

"""
from collections import OrderedDict

import torch
from torch import nn
from torch.nn import functional as F


class RCPSEmbedding(nn.Module):
    """Embedding layer that supports reverse-complement equivariance."""
    def __init__(self, vocab_size: int, d_model: int, complement_map: dict, **factory_kwargs):
        """
        Args:
            vocab_size: Size of vocabulary.
            d_model: Dimensionality of embedding (actual embedding matrix will have 1/2 the output dim).
            complement_map: Dictionary mapping each token id to its complement.
        """
        super().__init__()
        self.register_buffer(
            "complement_map",
            torch.tensor(list(OrderedDict(complement_map).values()), dtype=torch.long)
        )
        self.vmap_complement = torch.vmap(lambda t: self.complement_map[t])
        self.embedding = nn.Embedding(vocab_size, d_model, **factory_kwargs)
        # Project down so that final output has correct expected shape: (batch_size, seq_len, d_model)
        assert d_model % 2 == 0, "d_model must be even!"
        self.proj = nn.Linear(d_model, d_model // 2, **factory_kwargs)

    @property
    def weight(self):
        """Embedding weights."""
        return self.embedding.weight

    def rc(self, x):
        """Reverse-complement a tensor of input_ids by flipping along length dimension and complementing the ids."""
        return self.vmap_complement(torch.flip(x, dims=[-1]))

    def forward(self, input_ids):
        """Reverse-complement equivariant forward pass.

        Args:
            input_ids: Input tensor of shape (batch_size, seq_len)
        """
        fwd_out = self.proj(self.embedding(input_ids))
        rc_out = torch.flip(self.proj(self.embedding(self.rc(input_ids))), dims=[-2, -1])
        return torch.cat([fwd_out, rc_out], dim=-1)


class RCPSWrapper(nn.Module):
    """Wrapper to convert arbitrary nn.Module into a reverse-complement equivariant module.

    See ref. "Towards a Better Understanding of Reverse-Complement Equivariance for Deep Learning Models in Regulatory
    Genomics", Zhou et al. (2022), https://proceedings.mlr.press/v165/zhou22a.html for more details.
    """
    def __init__(self, submodule: nn.Module):
        super().__init__()
        self.submodule = submodule

    @staticmethod
    def rc(x):
        """Reverse-complement a tensor by flipping the length (dim=-2) and channel (dim=-1) dimensions."""
        return torch.flip(x, dims=[-2, -1])

    def forward(self, x, **kwargs):
        """Reverse-complement equivariant forward pass.

        x: Input tensor of shape (batch_size, seq_len, channels)
        """
        # Run submodule along sequence
        fwd_out = self.submodule(x, **kwargs)
        # Run submodule along rc-sequence
        rc_out = self.submodule(self.rc(x), **kwargs)
        # Concatenate along channel dimension (dim=-1)
        return torch.cat([fwd_out, self.rc(rc_out)], dim=-1)


class RCPSWrapperKeepDim(RCPSWrapper):
    """Wrapper to convert arbitrary nn.Module into a reverse-complement equivariant module while maintaining
    the same output dimensionality.
    """
    def __init__(self, submodule: nn.Module, dim: int, **factory_kwargs):
        super().__init__(submodule)
        self.submodule = submodule
        assert dim % 2 == 0, "dim must be even!"
        self.proj = nn.Linear(dim, dim // 2, **factory_kwargs)

    def forward(self, x, **kwargs):
        """Reverse-complement equivariant forward pass that maintains output dimensionality by projecting down
        forward and reverse strands.

        Args:
            x: Input tensor of shape (batch_size, seq_len, channels)
        """
        # Run submodule along sequence
        fwd_out = self.proj(self.submodule(x, **kwargs))

        # Run submodule along rc-sequence
        rc_out = self.proj(self.submodule(self.rc(x), **kwargs))

        # Concatenate along channel dimension (dim=-1)
        return torch.cat([fwd_out, self.rc(rc_out)], dim=-1)


class RCPSAddNormWrapper(nn.Module):
    """RC equivariant AddNorm layer."""
    def __init__(self, norm_f: nn.Module):
        super().__init__()
        self.norm_f = norm_f

    @staticmethod
    def rc(x):
        """Reverse-complement a tensor by flipping the length (dim=-2) and channel (dim=-1) dimensions."""
        return torch.flip(x, dims=[-2, -1])

    def forward(self, x, residual=None):
        """
        Args:
            x: Input tensor of shape (batch_size, seq_len, channels)
            residual: Residual tensor of shape (batch_size, seq_len, channels) or None.
        """
        residual_fwd = x + residual if residual is not None else x
        x_fwd = self.norm_f(residual_fwd.to(dtype=self.norm_f.weight.dtype))

        residual_rc = self.rc(x) + self.rc(residual) if residual is not None else self.rc(x)
        x_rc = self.norm_f(residual_rc.to(dtype=self.norm_f.weight.dtype))

        residual = torch.cat([residual_fwd, self.rc(residual_rc)], dim=2)
        x = torch.cat([x_fwd, self.rc(x_rc)], dim=2)
        return x, residual


class RCPSAddNormWrapperKeepDim(RCPSAddNormWrapper):
    """RC equivariant AddNorm layer that maintains the dimensionality by projecting down prior to concatenation."""
    def __init__(self, norm_f, dim: int, **factory_kwargs):
        super().__init__(norm_f)
        self.res_proj = nn.Linear(dim, dim // 2, bias=False, **factory_kwargs)

    def forward(self, x, residual=None):
        """
        Args:
            x: Input tensor of shape (batch_size, seq_len, channels)
            residual: Residual tensor of shape (batch_size, seq_len, channels) or None.
        """
        residual_fwd = self.res_proj(x + residual if residual is not None else x)
        x_fwd = self.norm_f(residual_fwd.to(dtype=self.norm_f.weight.dtype))

        residual_rc = self.res_proj(self.rc(x) + self.rc(residual) if residual is not None else self.rc(x))
        x_rc = self.norm_f(residual_rc.to(dtype=self.norm_f.weight.dtype))

        residual = torch.cat([residual_fwd, self.rc(residual_rc)], dim=2)
        x = torch.cat([x_fwd, self.rc(x_rc)], dim=2)
        return x, residual


class RCPSLMHead(nn.Module):
    """LM Head for reverse-complement equivariant inputs, which have dim * 2 relative to standard inputs."""
    def __init__(self, true_dim: int, vocab_size: int, **factory_kwargs):
        """
        `true_dim` corresponds to the actual dimensionality of the input were it not reverse-complement
        equivariant, i.e. 0.5 times the actual input dim.
        """
        super().__init__()
        self.lm_head = nn.Linear(true_dim, vocab_size, bias=False, **factory_kwargs)

    @property
    def weight(self):
        """LM head weights."""
        return self.lm_head.weight

    def tie_weights(self, value):
        """Set LM head weights."""
        self.lm_head.weight = value

    def forward(self, x):
        """
        Args:
            x: Input tensor of shape (batch_size, seq_len, dim), where dim = 2 * true_dim.
        """
        n_channels = x.shape[-1]
        fwd_logits = F.linear(x[..., :n_channels // 2], self.weight)
        rc_logits = F.linear(torch.flip(x[..., n_channels // 2:], dims=[-1]), self.weight)
        return fwd_logits + torch.flip(rc_logits, dims=[-1])


class RCPSCollapse(nn.Module):
    """Collapse reverse-complement equivariant output by splitting channels and averaging."""
    def __init__(self):
        super().__init__()

    def forward(self, x):
        """Collapse reverse-complement equivariant output by splitting channels and averaging."""
        num_channels = x.shape[-1]
        return (x[..., :num_channels // 2] + torch.flip(x[..., num_channels // 2:], dims=[-2, -1])) / 2
