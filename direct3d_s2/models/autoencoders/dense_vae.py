from typing import *
import torch
import torch.nn as nn
import torch.nn.functional as F
import trimesh
from skimage import measure
from ...modules.norm import GroupNorm32, ChannelLayerNorm32
from ...modules.spatial import pixel_shuffle_3d
from ...modules.utils import zero_module, convert_module_to_f16, convert_module_to_f32
from .distributions import DiagonalGaussianDistribution


def norm_layer(norm_type: str, *args, **kwargs) -> nn.Module:
    """
    Return a normalization layer.
    """
    if norm_type == "group":
        return GroupNorm32(32, *args, **kwargs)
    elif norm_type == "layer":
        return ChannelLayerNorm32(*args, **kwargs)
    else:
        raise ValueError(f"Invalid norm type {norm_type}")


class ResBlock3d(nn.Module):
    def __init__(
        self,
        channels: int,
        out_channels: Optional[int] = None,
        norm_type: Literal["group", "layer"] = "layer",
    ):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels

        self.norm1 = norm_layer(norm_type, channels)
        self.norm2 = norm_layer(norm_type, self.out_channels)
        self.conv1 = nn.Conv3d(channels, self.out_channels, 3, padding=1)
        self.conv2 = zero_module(nn.Conv3d(self.out_channels, self.out_channels, 3, padding=1))
        self.skip_connection = nn.Conv3d(channels, self.out_channels, 1) if channels != self.out_channels else nn.Identity()
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.norm1(x)
        h = F.silu(h)
        h = self.conv1(h)
        h = self.norm2(h)
        h = F.silu(h)
        h = self.conv2(h)
        h = h + self.skip_connection(x)
        return h


class DownsampleBlock3d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        mode: Literal["conv", "avgpool"] = "conv",
    ):
        assert mode in ["conv", "avgpool"], f"Invalid mode {mode}"

        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels

        if mode == "conv":
            self.conv = nn.Conv3d(in_channels, out_channels, 2, stride=2)
        elif mode == "avgpool":
            assert in_channels == out_channels, "Pooling mode requires in_channels to be equal to out_channels"

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if hasattr(self, "conv"):
            return self.conv(x)
        else:
            return F.avg_pool3d(x, 2)


class UpsampleBlock3d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        mode: Literal["conv", "nearest"] = "conv",
    ):
        assert mode in ["conv", "nearest"], f"Invalid mode {mode}"

        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels

        if mode == "conv":
            self.conv = nn.Conv3d(in_channels, out_channels*8, 3, padding=1)
        elif mode == "nearest":
            assert in_channels == out_channels, "Nearest mode requires in_channels to be equal to out_channels"

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if hasattr(self, "conv"):
            x = self.conv(x)
            return pixel_shuffle_3d(x, 2)
        else:
            return F.interpolate(x, scale_factor=2, mode="nearest")
        

class SparseStructureEncoder(nn.Module):
    """
    Encoder for Sparse Structure (\mathcal{E}_S in the paper Sec. 3.3).
    
    Args:
        in_channels (int): Channels of the input.
        latent_channels (int): Channels of the latent representation.
        num_res_blocks (int): Number of residual blocks at each resolution.
        channels (List[int]): Channels of the encoder blocks.
        num_res_blocks_middle (int): Number of residual blocks in the middle.
        norm_type (Literal["group", "layer"]): Type of normalization layer.
        use_fp16 (bool): Whether to use FP16.
    """
    def __init__(
        self,
        in_channels: int,
        latent_channels: int,
        num_res_blocks: int,
        channels: List[int],
        num_res_blocks_middle: int = 2,
        norm_type: Literal["group", "layer"] = "layer",
        use_fp16: bool = False,
        use_checkpoint: bool = False,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.latent_channels = latent_channels
        self.num_res_blocks = num_res_blocks
        self.channels = channels
        self.num_res_blocks_middle = num_res_blocks_middle
        self.norm_type = norm_type
        self.use_fp16 = use_fp16
        self.dtype = torch.float16 if use_fp16 else torch.float32
        self.use_checkpoint = use_checkpoint

        self.input_layer = nn.Conv3d(in_channels, channels[0], 3, padding=1)

        self.blocks = nn.ModuleList([])
        for i, ch in enumerate(channels):
            self.blocks.extend([
                ResBlock3d(ch, ch)
                for _ in range(num_res_blocks)
            ])
            if i < len(channels) - 1:
                self.blocks.append(
                    DownsampleBlock3d(ch, channels[i+1])
                )
        
        self.middle_block = nn.Sequential(*[
            ResBlock3d(channels[-1], channels[-1])
            for _ in range(num_res_blocks_middle)
        ])

        self.out_layer = nn.Sequential(
            norm_layer(norm_type, channels[-1]),
            nn.SiLU(),
            nn.Conv3d(channels[-1], latent_channels*2, 3, padding=1)
        )

        if use_fp16:
            self.convert_to_fp16()

    @property
    def device(self) -> torch.device:
        """
        Return the device of the model.
        """
        return next(self.parameters()).device

    def convert_to_fp16(self) -> None:
        """
        Convert the torso of the model to float16.
        """
        self.use_fp16 = True
        self.dtype = torch.float16
        self.blocks.apply(convert_module_to_f16)
        self.middle_block.apply(convert_module_to_f16)

    def convert_to_fp32(self) -> None:
        """
        Convert the torso of the model to float32.
        """
        self.use_fp16 = False
        self.dtype = torch.float32
        self.blocks.apply(convert_module_to_f32)
        self.middle_block.apply(convert_module_to_f32)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.input_layer(x)

        for block in self.blocks:
            h = block(h)
        h = self.middle_block(h)

        h = self.out_layer(h)

        return h
        

class SparseStructureDecoder(nn.Module):
    """
    Decoder for Sparse Structure (\mathcal{D}_S in the paper Sec. 3.3).
    
    Args:
        out_channels (int): Channels of the output.
        latent_channels (int): Channels of the latent representation.
        num_res_blocks (int): Number of residual blocks at each resolution.
        channels (List[int]): Channels of the decoder blocks.
        num_res_blocks_middle (int): Number of residual blocks in the middle.
        norm_type (Literal["group", "layer"]): Type of normalization layer.
        use_fp16 (bool): Whether to use FP16.
    """ 
    def __init__(
        self,
        out_channels: int,
        latent_channels: int,
        num_res_blocks: int,
        channels: List[int],
        num_res_blocks_middle: int = 2,
        norm_type: Literal["group", "layer"] = "layer",
        use_fp16: bool = False,
        use_checkpoint: bool = False,
    ):
        super().__init__()
        self.out_channels = out_channels
        self.latent_channels = latent_channels
        self.num_res_blocks = num_res_blocks
        self.channels = channels
        self.num_res_blocks_middle = num_res_blocks_middle
        self.norm_type = norm_type
        self.use_fp16 = use_fp16
        self.dtype = torch.float16 if use_fp16 else torch.float32
        self.use_checkpoint = use_checkpoint

        self.input_layer = nn.Conv3d(latent_channels, channels[0], 3, padding=1)

        self.middle_block = nn.Sequential(*[
            ResBlock3d(channels[0], channels[0])
            for _ in range(num_res_blocks_middle)
        ])

        self.blocks = nn.ModuleList([])
        for i, ch in enumerate(channels):
            self.blocks.extend([
                ResBlock3d(ch, ch)
                for _ in range(num_res_blocks)
            ])
            if i < len(channels) - 1:
                self.blocks.append(
                    UpsampleBlock3d(ch, channels[i+1])
                )

        self.out_layer = nn.Sequential(
            norm_layer(norm_type, channels[-1]),
            nn.SiLU(),
            nn.Conv3d(channels[-1], out_channels, 3, padding=1)
        )

        if use_fp16:
            self.convert_to_fp16()

    @property
    def device(self) -> torch.device:
        """
        Return the device of the model.
        """
        return next(self.parameters()).device
    
    def convert_to_fp16(self) -> None:
        """
        Convert the torso of the model to float16.
        """
        self.use_fp16 = True
        self.dtype = torch.float16
        # self.blocks.apply(convert_module_to_f16)
        # self.middle_block.apply(convert_module_to_f16)
        self.apply(convert_module_to_f16)

    def convert_to_fp32(self) -> None:
        """
        Convert the torso of the model to float32.
        """
        self.use_fp16 = False
        self.dtype = torch.float32
        self.blocks.apply(convert_module_to_f32)
        self.middle_block.apply(convert_module_to_f32)
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.input_layer(x)
                
        h = self.middle_block(h)
        for block in self.blocks:
            h = block(h)

        h = self.out_layer(h)
        return h


class DenseShapeVAE(nn.Module):
    def __init__(self,
                 embed_dim: int = 0,
                 model_channels_encoder: list = [32, 128, 512],
                 model_channels_decoder: list = [512, 128, 32],
                 num_res_blocks_encoder: int = 2,
                 num_res_blocks_middle_encoder: int = 2,
                 num_res_blocks_decoder: int = 2,
                 num_res_blocks_middle_decoder: int=2,
                 in_channels: int = 1,
                 out_channels: int = 1,
                 use_fp16: bool = False,
                 use_checkpoint: bool = False,
                 latents_scale: float = 1.0,
                 latents_shift: float = 0.0):

        super().__init__()

        self.use_checkpoint = use_checkpoint
        self.latents_scale = latents_scale
        self.latents_shift = latents_shift

        self.encoder = SparseStructureEncoder(
            in_channels=in_channels,
            latent_channels=embed_dim,
            num_res_blocks=num_res_blocks_encoder,
            channels=model_channels_encoder,
            num_res_blocks_middle=num_res_blocks_middle_encoder,
            use_fp16=use_fp16,
            use_checkpoint=use_checkpoint,
        )

        self.decoder = SparseStructureDecoder(
            num_res_blocks=num_res_blocks_decoder,
            num_res_blocks_middle=num_res_blocks_middle_decoder,
            channels=model_channels_decoder,
            latent_channels=embed_dim,
            out_channels=out_channels,
            use_fp16=use_fp16,
            use_checkpoint=use_checkpoint,
        )

        self.embed_dim = embed_dim

    def encode(self, batch, sample_posterior: bool = True):

        x = batch['dense_index'] * 2.0 - 1.0
        h = self.encoder(x)
        posterior = DiagonalGaussianDistribution(h, feat_dim=1)
        if sample_posterior:
            z = posterior.sample()
        else:
            z = posterior.mode()

        return z, posterior 

    def forward(self, batch):
        
        z, posterior = self.encode(batch)
        reconst_x = self.decoder(z)
        outputs = {'reconst_x': reconst_x, 'posterior': posterior}

        return outputs

    def decode_mesh(self,
                    latents,
                    voxel_resolution: int = 64,
                    mc_threshold: float = 0.5,
                    return_index: bool = False):
        x = self.decoder(latents)
        if return_index:
            outputs = []
            for i in range(len(x)):
                occ = x[i].sigmoid()
                occ = (occ >= mc_threshold).float().squeeze(0)
                index = occ.unsqueeze(0).nonzero()
                outputs.append(index)
        else:
            outputs = self.dense2mesh(x, voxel_resolution=voxel_resolution, mc_threshold=mc_threshold)
        
        return outputs
    
    def dense2mesh(self,
                    x: torch.FloatTensor,
                    voxel_resolution: int = 64,
                    mc_threshold: float = 0.5):

        meshes = []
        for i in range(len(x)):
            occ = x[i].sigmoid()
            occ = (occ >= 0.1).float().squeeze(0).cpu().detach().numpy()
            vertices, faces, _, _ = measure.marching_cubes(
                occ,
                mc_threshold,
                method="lewiner",
            )
            vertices = vertices / voxel_resolution * 2 - 1
            meshes.append(trimesh.Trimesh(vertices, faces))

        return meshes
