import torch
import torch.nn as nn
from functools import partial
from .layers import DropPath, to_2tuple, trunc_normal_
import torch.utils.checkpoint as checkpoint 

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x

class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)  # 3, B, num_head, N, c
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x):
        x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class PatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        B, C, H, W = x.shape
        # FIXME look at relaxing size constraints
        # assert H == self.img_size[0] and W == self.img_size[1], \
        #     f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)
        return x


class HybridEmbed(nn.Module):
    """ CNN Feature Map Embedding
    Extract feature map from CNN, flatten, project to embedding dim.
    """
    def __init__(self, backbone, img_size=224, feature_size=None, in_chans=3, embed_dim=768):
        super().__init__()
        assert isinstance(backbone, nn.Module)
        img_size = to_2tuple(img_size)
        self.img_size = img_size
        self.backbone = backbone
        if feature_size is None:
            with torch.no_grad():
                # FIXME this is hacky, but most reliable way of determining the exact dim of the output feature
                # map for all networks, the feature metadata has reliable channel and stride info, but using
                # stride to calc feature dim requires info about padding of each stage that isn't captured.
                training = backbone.training
                if training:
                    backbone.eval()
                o = self.backbone(torch.zeros(1, in_chans, img_size[0], img_size[1]))[-1]
                feature_size = o.shape[-2:]
                feature_dim = o.shape[1]
                backbone.train(training)
        else:
            feature_size = to_2tuple(feature_size)
            feature_dim = self.backbone.feature_info.channels()[-1]
        self.num_patches = feature_size[0] * feature_size[1]
        self.proj = nn.Linear(feature_dim, embed_dim)

    def forward(self, x):
        x = self.backbone(x)[-1]
        x = x.flatten(2).transpose(1, 2)
        x = self.proj(x)
        return x


class VisionTransformer(nn.Module):
    """ Vision Transformer with support for patch or hybrid CNN input stage
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., hybrid_backbone=None, norm_layer=nn.LayerNorm, is_distill=False):
        super().__init__()
        
        if isinstance(img_size,tuple):
            self.img_size = img_size
        else:
            self.img_size = to_2tuple(img_size)
        
        self.depth = depth
        self.patch_size = patch_size
        self.in_chans = in_chans
        self.embed_dim = embed_dim
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models

        if hybrid_backbone is not None:
            self.patch_embed = HybridEmbed(
                hybrid_backbone, img_size=img_size, in_chans=in_chans, embed_dim=embed_dim)
        else:
            self.patch_embed = PatchEmbed(
                img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        self.num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        if is_distill:
            self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches + 2, embed_dim))
        else:
            self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches + 1, embed_dim))
        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)

        # NOTE as per official impl, we could have a pre-logits representation dense layer + tanh here
        #self.repr = nn.Linear(embed_dim, representation_size)
        #self.repr_act = nn.Tanh()

        # Classifier head
        # self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()

        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.cls_token, std=.02)
        self.apply(self._init_weights)



        # set finetune flag
        self.finetune = False
        self.has_mid_pe = False

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def finetune_det(self, img_size=[800, 1344], det_token_num=100, mid_pe_size=None):
        # import pdb;pdb.set_trace()

        import math
        g = math.pow(self.pos_embed.size(1) - 1, 0.5)
        if int(g) - g != 0:
            self.pos_embed = torch.nn.Parameter(self.pos_embed[:, 1:, :])

        self.det_token_num = det_token_num
        self.det_token = nn.Parameter(torch.zeros(1, det_token_num, self.embed_dim))
        self.det_token = trunc_normal_(self.det_token, std=.02)
        cls_pos_embed = self.pos_embed[:, 0, :]
        cls_pos_embed = cls_pos_embed[:,None]
        det_pos_embed = torch.zeros(1, det_token_num, self.embed_dim)
        det_pos_embed = trunc_normal_(det_pos_embed, std=.02)
        patch_pos_embed = self.pos_embed[:, 1:, :]
        patch_pos_embed = patch_pos_embed.transpose(1,2)
        B, E, Q = patch_pos_embed.shape
        P_H, P_W = self.img_size[0] // self.patch_size, self.img_size[1] // self.patch_size
        patch_pos_embed = patch_pos_embed.view(B, E, P_H, P_W)
        H, W = img_size
        new_P_H, new_P_W = H//self.patch_size, W//self.patch_size
        patch_pos_embed = nn.functional.interpolate(patch_pos_embed, size=(new_P_H,new_P_W), mode='bicubic', align_corners=False)
        patch_pos_embed = patch_pos_embed.flatten(2).transpose(1, 2)
        self.pos_embed = torch.nn.Parameter(torch.cat((cls_pos_embed, patch_pos_embed, det_pos_embed), dim=1))
        self.img_size = img_size
        if mid_pe_size == None:
            self.has_mid_pe = False
            print('No mid pe')
        else:
            print('Has mid pe')
            self.mid_pos_embed = nn.Parameter(torch.zeros(self.depth - 1, 1, 1 + (H * W // self.patch_size ** 2) + 100, self.embed_dim))
            trunc_normal_(self.mid_pos_embed, std=.02)
            self.has_mid_pe = True
            self.mid_pe_size = mid_pe_size

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token', 'det_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def InterpolatePosEmbed(self, pos_embed, img_size=(800, 1344), mid=False):
        # import pdb;pdb.set_trace()
        cls_pos_embed = pos_embed[:, 0, :]
        cls_pos_embed = cls_pos_embed[:,None]
        det_pos_embed = pos_embed[:, -self.det_token_num:,:]
        patch_pos_embed = pos_embed[:, 1:-self.det_token_num, :]
        patch_pos_embed = patch_pos_embed.transpose(1,2)
        B, E, Q = patch_pos_embed.shape

        if mid:
            P_H, P_W = self.mid_pe_size[0] // self.patch_size, self.mid_pe_size[1] // self.patch_size
            patch_pos_embed = patch_pos_embed.view(B, E, P_H, P_W)
        else:
            P_H, P_W = self.img_size[0] // self.patch_size, self.img_size[1] // self.patch_size
            patch_pos_embed = patch_pos_embed.view(B, E, P_H, P_W)

        # P_H, P_W = self.img_size[0] // self.patch_size, self.img_size[1] // self.patch_size
        # patch_pos_embed = patch_pos_embed.view(B, E, P_H, P_W)

        H, W = img_size
        new_P_H, new_P_W = H//self.patch_size, W//self.patch_size
        patch_pos_embed = nn.functional.interpolate(patch_pos_embed, size=(new_P_H,new_P_W), mode='bicubic', align_corners=False)
        patch_pos_embed = patch_pos_embed.flatten(2).transpose(1, 2)
        scale_pos_embed = torch.cat((cls_pos_embed, patch_pos_embed, det_pos_embed), dim=1)
        return scale_pos_embed


    def forward_features(self, x):
        # import pdb;pdb.set_trace()
        B, H, W = x.shape[0], x.shape[2], x.shape[3]

        if (H,W) != self.img_size:
            self.finetune = True

        x = self.patch_embed(x)

        if (self.pos_embed.shape[1] - 1 - self.det_token_num) != x.shape[1]:
            temp_pos_embed = self.InterpolatePosEmbed(self.pos_embed, img_size=(H,W))
        else:
            temp_pos_embed = self.pos_embed


        cls_tokens = self.cls_token.expand(B, -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
        det_token = self.det_token.expand(B, -1, -1)
        x = torch.cat((cls_tokens, x, det_token), dim=1)
        x = x + temp_pos_embed
        x = self.pos_drop(x)

        for i in range(len((self.blocks))):

            # x = checkpoint.checkpoint(self.blocks[i], x)    # saves mem, takes time
            x = self.blocks[i](x)
            if self.has_mid_pe:
                if i < 11:
                    if (self.mid_pos_embed.shape[2] - 1 - self.det_token_num) != x.shape[1]:
                        x = x + self.InterpolatePosEmbed(self.mid_pos_embed[i], img_size=(H,W), mid=True)
                    else:
                        x = x + self.mid_pos_embed[i]

        x = self.norm(x)

        return x[:, -self.det_token_num:, :]




    def forward(self, x):
        x = self.forward_features(x)
        return x


def _conv_filter(state_dict, patch_size=16):
    """ convert patch embedding weight from manual patchify + linear proj to conv"""
    out_dict = {}
    for k, v in state_dict.items():
        if 'patch_embed.proj.weight' in k:
            v = v.reshape((v.shape[0], 3, patch_size, patch_size))
        out_dict[k] = v
    return out_dict

def deit_tiny_patch16_224(pretrained=False, **kwargs):
    model = VisionTransformer(
                patch_size=16, embed_dim=192, depth=12, num_heads=3, mlp_ratio=4, qkv_bias=True,
                norm_layer=partial(nn.LayerNorm, eps=1e-6))
    if pretrained: 
        # checkpoint = torch.load('deit_tiny_patch16_224-a1311bcf.pth', map_location="cpu")
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_tiny_patch16_224-a1311bcf.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 192

def deit_tiny_distilled_patch16_224(pretrained=False, **kwargs):
    model = VisionTransformer(
                patch_size=16, embed_dim=192, depth=12, num_heads=3, mlp_ratio=4, qkv_bias=True,
                norm_layer=partial(nn.LayerNorm, eps=1e-6), is_distill=True)
    if pretrained: 
        # checkpoint = torch.load('deit_tiny_patch16_224-a1311bcf.pth', map_location="cpu")
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_tiny_distilled_patch16_224-b40b3cf7.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 192
    
def deit_small_patch16_224(pretrained=False, **kwargs):
    model = VisionTransformer(
        patch_size=16, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        # checkpoint = torch.load('deit_small_patch16_224-cd65a155.pth', map_location="cpu")
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_small_patch16_224-cd65a155.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 384


def fa_deit_small(pretrained=False, **kwargs):
    model = VisionTransformer(
        img_size=240, 
        patch_size=16, embed_dim=330, depth=14, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('fa_deit_ldr_14_330_240.pth', map_location="cpu")
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 330


def uni_deit_small(pretrained=False, **kwargs):
    model = VisionTransformer(
        img_size=272,
        patch_size=16, embed_dim=240, depth=19, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('uni_deit_small_ldr_19_240_272.pth', map_location="cpu")
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 240


def deit_13_236_704(pretrained=False, **kwargs):
    model = VisionTransformer(
        img_size=256, 
        patch_size=16, embed_dim=236, depth=13, num_heads=4, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('deit_13_236_704.pth', map_location="cpu")
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 236


def deit_16_220_688(pretrained=False, **kwargs):
    model = VisionTransformer(
        img_size=240, 
        patch_size=16, embed_dim=220, depth=16, num_heads=4, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('deit_16_220_688.pth', map_location="cpu")
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 220

def deit_base_distilled_patch16_384(pretrained=False, **kwargs):
    model = VisionTransformer(
        img_size=384, patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),is_distill=True, **kwargs)
    if pretrained:
        # checkpoint = torch.load('deit_base_distilled_patch16_384-d0272ac0.pth', map_location="cpu")
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_base_distilled_patch16_384-d0272ac0.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 768


def deit_small_distilled_patch16_224(pretrained=False, **kwargs):
    model = VisionTransformer(
        patch_size=16, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), is_distill=True, **kwargs)
    if pretrained:
        # checkpoint = torch.load('deit_small_distilled_patch16_224-649709d9.pth', map_location="cpu")
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_small_distilled_patch16_224-649709d9.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"], strict=False)
    return model, 384


def dino_deits16(pretrained=False, **kwargs):
    model = VisionTransformer(
        patch_size=16, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('dino_deitsmall16_pretrain.pth', map_location="cpu")
        model.load_state_dict(checkpoint, strict=False)
        # print(checkpoint.keys())
        # exit()
    return model, 384


def dino_deits8(pretrained=False, **kwargs):
    model = VisionTransformer(
        patch_size=8, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('dino_deitsmall8_pretrain.pth', map_location="cpu")
        model.load_state_dict(checkpoint, strict=False)
        # print(checkpoint.keys())
        # exit()
    return model, 384


def base_IN_21k(pretrained=False, **kwargs):
    model = VisionTransformer(
        img_size=224, patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    if pretrained:
        checkpoint = torch.load('vit_base_patch16_224_in21k_miil.pth', map_location="cpu")
        print(checkpoint.keys())
        model.load_state_dict(checkpoint, strict=False)
    return model, 768