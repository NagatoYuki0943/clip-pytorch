from collections import OrderedDict

import torch
from torch import nn


class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        ret = super().forward(x.type(torch.float32))
        return ret.type(orig_type)


class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)


class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None):
        super().__init__()

        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)
        self.attn_mask = attn_mask

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, x: torch.Tensor):
        x = x + self.attention(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


class Transformer(nn.Module):
    def __init__(self, width: int, layers: int, heads: int, attn_mask: torch.Tensor = None):
        super().__init__()
        self.width = width
        self.layers = layers
        self.resblocks = nn.Sequential(*[ResidualAttentionBlock(width, heads, attn_mask) for _ in range(layers)])

    def forward(self, x: torch.Tensor):
        return self.resblocks(x)


class VisionTransformer(nn.Module):
    def __init__(self, input_resolution: int, patch_size: int, width: int, layers: int, heads: int, output_dim: int):
        super().__init__()
        self.input_resolution = input_resolution
        self.output_dim = output_dim
        #-----------------------------------------------#
        #   224, 224, 3 -> 196, 768
        #-----------------------------------------------#
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)

        scale = width ** -0.5
        #--------------------------------------------------------------------------------------------------------------------#
        #   class_embedding部分是transformer的分类特征。用于堆叠到序列化后的图片特征中，作为一个单位的序列特征进行特征提取。
        #
        #   在利用步长为16x16的卷积将输入图片划分成14x14的部分后，将14x14部分的特征平铺，一幅图片会存在序列长度为196的特征。
        #   此时生成一个class_embedding，将class_embedding堆叠到序列长度为196的特征上，获得一个序列长度为197的特征。
        #   在特征提取的过程中，class_embedding会与图片特征进行特征的交互。最终分类时，我们取出class_embedding的特征，利用全连接分类。
        #--------------------------------------------------------------------------------------------------------------------#
        #   196, 768 -> 197, 768
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        #--------------------------------------------------------------------------------------------------------------------#
        #   为网络提取到的特征添加上位置信息。
        #   以输入图片为224, 224, 3为例，我们获得的序列化后的图片特征为196, 768。加上class_embedding后就是197, 768
        #   此时生成的pos_Embedding的shape也为197, 768，代表每一个特征的位置信息。
        #--------------------------------------------------------------------------------------------------------------------#
        #   197, 768 -> 197, 768
        self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
        self.ln_pre = LayerNorm(width)

        self.transformer = Transformer(width, layers, heads)

        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

    def forward(self, x: torch.Tensor):
        x = self.conv1(x)                           # shape = [*, width, grid, grid] [B, 3, 224, 224] -> [B, 768, 7, 7]
        x = x.reshape(x.shape[0], x.shape[1], -1)   # shape = [*, width, grid ** 2]  [B, 768, 7, 7] -> [B, 768, 49]
        x = x.permute(0, 2, 1)                      # shape = [*, grid ** 2, width]  [B, 768, 49] -> [B, 49, 768]
        x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)  # shape = [*, grid ** 2 + 1, width] [B, 49, 768] -> [B, 50, 768]
        x = x + self.positional_embedding.to(x.dtype) # [B, 50, 768] + [1, 50, 768] = [B, 50, 768]
        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND    [B, 50, 768] -> [50, B, 768]
        x = self.transformer(x) # LND -> LND    [50, B, 768] -> [50, B, 768]
        x = x.permute(1, 0, 2)  # LND -> NLD    [50, B, 768] -> [B, 50, 768]

        # 使用分类层进行后续计算
        x = self.ln_post(x[:, 0, :])          # [B, 50, 768] get [B, 768]

        if self.proj is not None:
            x = x @ self.proj                 # [B, 768] @ [768, 512] = [B, 512]

        return x

if __name__ == "__main__":
    model = VisionTransformer(
        input_resolution=224,
        patch_size=32,
        width=768,
        layers=12,
        heads=12,
        output_dim=512,
    )
    x = torch.ones(1, 3, 224, 224)

    model.eval()
    with torch.inference_mode():
        y = model(x)
    print(y.size()) # [1, 512]
