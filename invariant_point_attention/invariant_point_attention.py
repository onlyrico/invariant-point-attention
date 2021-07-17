import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops.layers.torch import Rearrange
from einops import rearrange, repeat

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def max_neg_value(t):
    return -torch.finfo(t.dtype).max

def apply_transform(x, rot, trans):
    return x

def apply_inverse_transform(x, rot, trans):
    return x

# classes

class InvariantPointAttention(nn.Module):
    def __init__(
        self,
        *,
        dim,
        heads = 8,
        scalar_key_dim = 16,
        scalar_value_dim = 16,
        point_key_dim = 4,
        point_value_dim = 4,
        pairwise_repr_dim = None,
        eps = 1e-8
    ):
        super().__init__()
        self.eps = eps
        self.heads = heads

        # qkv projection for scalar attention (normal)

        self.scalar_attn_logits_scale = (3 * scalar_key_dim) ** -0.5

        self.to_scalar_q = nn.Linear(dim, scalar_key_dim * heads, bias = False)
        self.to_scalar_k = nn.Linear(dim, scalar_key_dim * heads, bias = False)
        self.to_scalar_v = nn.Linear(dim, scalar_value_dim * heads, bias = False)

        # qkv projection for point attention (coordinate and orientation aware)

        point_weight_init_value = torch.log(torch.exp(torch.full((heads,), 1.)) - 1.)
        self.point_weights = nn.Parameter(point_weight_init_value)

        self.point_attn_logits_scale = ((3 * point_key_dim) * (9 / 2)) ** -0.5

        self.to_point_q = nn.Linear(dim, point_key_dim * heads * 3, bias = False)
        self.to_point_k = nn.Linear(dim, point_key_dim * heads * 3, bias = False)
        self.to_point_v = nn.Linear(dim, point_value_dim * heads * 3, bias = False)

        # pairwise representation projection to attention bias

        self.pairwise_attn_logits_scale = 3 ** -0.5

        pairwise_repr_dim = default(pairwise_repr_dim, dim)

        self.to_pairwise_attn_bias = nn.Sequential(
            nn.Linear(pairwise_repr_dim, heads),
            Rearrange('b ... h -> (b h) ...')
        )

        # combine out

        self.to_out = nn.Linear((scalar_value_dim * heads) + (pairwise_repr_dim * heads) + (point_value_dim * heads * 3), dim)

    def forward(
        self,
        single_repr,
        pairwise_repr,
        *,
        rotations,
        translations,
        mask = None
    ):
        x, b, h, eps = single_repr, single_repr.shape[0], self.heads, self.eps

        # get queries, keys, values for scalar and point (coordinate-aware) attention pathways

        q_scalar, k_scalar, v_scalar = self.to_scalar_q(x), self.to_scalar_k(x), self.to_scalar_v(x)

        q_point, k_point, v_point = self.to_point_q(x), self.to_point_k(x), self.to_point_v(x)

        # split out heads

        q_scalar, k_scalar, v_scalar = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h = h), (q_scalar, k_scalar, v_scalar))
        q_point, k_point, v_point = map(lambda t: rearrange(t, 'b n (h d c) -> (b h) n d c', h = h, c = 3), (q_point, k_point, v_point))

        # derive attn logits for scalar and pairwise

        attn_logits_scalar = einsum('b i d, b j d -> b i j', q_scalar, k_scalar) * self.scalar_attn_logits_scale
        attn_logits_pairwise = self.to_pairwise_attn_bias(pairwise_repr) * self.pairwise_attn_logits_scale

        # derive attn logits for point attention

        point_qk_diff = rearrange(q_point, 'b i d c -> b i () d c') - rearrange(k_point, 'b j d c -> b () j d c')
        point_dist = (point_qk_diff ** 2).sum(dim = -2)

        point_weights = F.softplus(self.point_weights)
        point_weights = repeat(point_weights, 'h -> (b h) () () ()', b = b)

        attn_logits_points = -0.5 * (point_dist * point_weights).sum(dim = -1)

        # combine attn logits

        attn_logits = attn_logits_scalar + attn_logits_pairwise + attn_logits_points

        # mask

        if exists(mask):
            mask = rearrange(mask, 'b i -> b i ()') * rearrange(mask, 'b j -> b () j')
            mask_value = max_neg_value(attn_logits)
            attn_logits = attn_logits.masked_fill(~mask, mask_value)

        # attention

        attn = attn_logits.softmax(dim = - 1)

        # aggregate values

        results_scalar = einsum('b i j, b j d -> b i d', attn, v_scalar)

        attn_with_heads = rearrange(attn, '(b h) i j -> b h i j', h = h)
        results_pairwise = einsum('b h i j, b i j d -> b h i d', attn_with_heads, pairwise_repr)

        # aggregate point values

        results_points = einsum('b i j, b j d c -> b i d c', attn, v_point)

        # merge back heads

        results_scalar = rearrange(results_scalar, '(b h) n d -> b n (h d)', h = h)
        results_pairwise = rearrange(results_pairwise, 'b h n d -> b n (h d)', h = h)
        results_points = rearrange(results_points, '(b h) n d c -> b n (h d c)', h = h)

        results = torch.cat((results_scalar, results_pairwise, results_points), dim = -1)
        return self.to_out(results)
