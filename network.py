import torch
import numpy as np
from torch import nn
import torch.nn.functional as F
from torch.nn.utils import spectral_norm

from cpc.cpc_network import SincEncoder
from utils import pixel_norm, resample_signal
from layers import GeneralConv, SelfAttention, MinibatchStddev, ScaledTanh, PassChannelResidual, ConcatResidual


class UnetGBlock(nn.Module):
    def __init__(self, ch_in, ch_out, ch_rgb_in, ch_rgb_out, signal_freq, inner_freq, dec_layer_settings,
                 enc_layer_settings, inner_layer=None, k_size=3, initial_kernel_size=None, is_residual=False,
                 no_tanh=False, deep=False, per_channel_noise=False, to_rgb_mode='pggan'):
        super().__init__()
        self.ch_out = ch_out
        self.ch_in = ch_in
        self.enc_ch_out = inner_layer.ch_in if inner_layer is not None else (ch_in + ch_out)
        self.encoder = DBlock(ch_in, self.enc_ch_out, ch_rgb_in, k_size, initial_kernel_size, is_residual, deep,
                              group_size=-1, temporal_groups_per_window=1, conv_disc=False, sinc_ks=False,
                              **enc_layer_settings)
        self.signal_freq = signal_freq
        self.inner_freq = inner_freq
        self.middle = inner_layer
        self.dec_ch_in = self.enc_ch_out + (inner_layer.ch_out if inner_layer is not None else 0)
        self.decoder = GBlock(self.dec_ch_in, ch_out, ch_rgb_out, k_size, initial_kernel_size, is_residual,
                              no_tanh, deep, per_channel_noise, to_rgb_mode, **dec_layer_settings)

    def forward(self, x, alpha, is_first=False, y=None, z=None):
        if is_first:
            encoded = self.encoder.fromRGB(x)
        else:
            encoded = x
        encoded = self.encoder(encoded)
        if self.middle is not None:
            middle_in = resample_signal(encoded, self.signal_freq, self.inner_freq)
            if is_first and alpha != 1.0:
                middle_in = alpha * middle_in + (1.0 - alpha) * self.middle.encoder.fromRGB(x)
            middle_out = self.middle(middle_in, y=y, z=z)
            inner = resample_signal(middle_out, self.inner_freq, self.signal_freq)
            encoded = torch.cat([encoded, inner], dim=1)
        decoded = self.decoder(encoded)
        if is_first:
            if self.middle is None or alpha == 1.0:
                return self.decoder.toRGB(decoded)
            return self.decoder.toRGB(decoded) * alpha + (1.0 - alpha) * self.middle.decoder.toRGB(middle_out)
        return decoded


class UnetGenerator(nn.Module):
    def __init__(self, ch_rgb_in, ch_rgb_out, latent_size, z_to_bn, equalized, spectral, init, act_alpha, dropout,
                 num_classes, act_norm, separable, progression_scale_up, progression_scale_down, z_distribution,
                 normalize_latents, fmap_base, fmap_min, fmap_max, shared_embedding_size, k_size=3,
                 initial_kernel_size=None, split_z=False, is_residual=False, no_tanh=False, deep=False,
                 per_channel_noise=False, to_rgb_mode='pggan', rgb_generation_mode='pggan', input_to_all_layers=False):
        super().__init__()
        R = len(progression_scale_up)
        assert len(progression_scale_up) == len(progression_scale_down)
        self.progression_scale_up = progression_scale_up
        self.progression_scale_down = progression_scale_down
        self.depth = 0
        self.alpha = 1.0
        if deep:
            split_z = False
        self.split_z = split_z
        self.z_distribution = z_distribution
        self.initial_kernel_size = initial_kernel_size
        self.normalize_latents = normalize_latents
        self.z_to_bn = z_to_bn

        def nf(stage):
            return min(max(int(fmap_base / (2.0 ** stage)), fmap_min), fmap_max)

        if latent_size is None:
            latent_size = nf(0)
        self.input_latent_size = latent_size
        if num_classes != 0:
            if shared_embedding_size > 0:
                self.y_encoder = GeneralConv(num_classes, shared_embedding_size, kernel_size=1,
                                             equalized=False, act_alpha=act_alpha, spectral=False, bias=False)
                num_classes = shared_embedding_size
            else:
                self.y_encoder = nn.Sequential()
        else:
            self.y_encoder = None
        if split_z:
            latent_size //= R + 2  # we also give part of the z to the first layer
        self.latent_size = latent_size
        inner = None
        self.blocks = nn.ModuleList()
        decoder_layer_settings = dict(z_to_bn_size=latent_size if z_to_bn else 0, equalized=equalized,
                                      spectral=spectral, init=init, act_alpha=act_alpha, do=dropout,
                                      num_classes=num_classes, act_norm=act_norm, bias=True, separable=separable)
        encoder_layer_settings = dict(equalized=equalized, spectral=spectral, init=init, act_alpha=act_alpha,
                                      do=dropout, num_classes=0, act_norm=act_norm, bias=True, separable=separable)
        psunp = np.array(progression_scale_up)
        psdnp = np.array(progression_scale_down)
        signal_lens = [0] + [initial_kernel_size * np.sum(psunp[:i] / psdnp[:i]) for i in
                             range(len(progression_scale_up) + 1)]
        for i in range(R + 1):
            inner = UnetGBlock(nf(i), nf(i), ch_rgb_in, ch_rgb_out,
                               signal_lens[i], signal_lens[i - 1],
                               decoder_layer_settings,
                               encoder_layer_settings, inner, k_size,
                               initial_kernel_size if i == 0 else None, is_residual, no_tanh, deep,
                               per_channel_noise, to_rgb_mode)
            self.blocks.append(inner)
        self.max_depth = len(self.blocks)
        self.deep = deep
        assert rgb_generation_mode == 'pggan', 'We do not support non pggan image generation in the UGenerator, yet!'
        self.rgb_generation_mode = rgb_generation_mode
        assert input_to_all_layers == False, 'We do not support non pggan image generation in the UGenerator, yet!'
        self.input_to_all_layers = input_to_all_layers

    def forward(self, z):
        if isinstance(z, dict):
            x, z, y = z['x'], z['z'], z.get('y', None)
        elif isinstance(z, tuple):
            x, z, y = z
        else:
            raise ValueError('invalid input')
        if y is not None:
            if y.ndimension() == 2:
                y = y.unsqueeze(2)
            if self.y_encoder is not None:
                y = self.y_encoder(y)
            else:
                y = None
        if self.normalize_latents:
            z = pixel_norm(z)
        if z.ndimension() == 2:
            z = z.unsqueeze(2)
        return self.blocks[self.depth](x, self.alpha, is_first=True, y=y, z=z)


class GBlock(nn.Module):
    def __init__(self, ch_in, ch_out, ch_rgb, k_size=3, initial_kernel_size=None, is_residual=False,
                 no_tanh=False, deep=False, per_channel_noise=False, to_rgb_mode='pggan', **layer_settings):
        super().__init__()
        is_first = initial_kernel_size is not None
        first_k_size = initial_kernel_size if is_first else k_size
        hidden_size = (ch_in // 4) if deep else ch_out
        self.c1 = GeneralConv(ch_in, hidden_size, kernel_size=first_k_size,
                              pad=initial_kernel_size - 1 if is_first else None, **layer_settings)
        self.c2 = GeneralConv(hidden_size, hidden_size, kernel_size=k_size, **layer_settings)
        if per_channel_noise:
            self.c1_noise_weight = nn.Parameter(torch.zeros(1, hidden_size, 1))
            self.c2_noise_weight = nn.Parameter(torch.zeros(1, hidden_size, 1))
        else:
            self.c1_noise_weight, self.c2_noise_weight = None, None
        if deep:
            self.c3 = GeneralConv(hidden_size, hidden_size, kernel_size=k_size, **layer_settings)
            self.c4 = GeneralConv(hidden_size, ch_out, kernel_size=k_size, **layer_settings)
            if per_channel_noise:
                self.c3_noise_weight = nn.Parameter(torch.zeros(1, hidden_size, 1))
                self.c4_noise_weight = nn.Parameter(torch.zeros(1, ch_out, 1))
            else:
                self.c3_noise_weight, self.c4_noise_weight = None, None
        reduced_layer_settings = dict(equalized=layer_settings['equalized'], spectral=layer_settings['spectral'],
                                      init=layer_settings['init'])
        if to_rgb_mode == 'pggan':
            to_rgb = GeneralConv(ch_out, ch_rgb, kernel_size=1, act_alpha=-1, **reduced_layer_settings)
        elif to_rgb_mode in {'sngan', 'sagan'}:
            to_rgb = GeneralConv(ch_out, ch_rgb if to_rgb_mode == 'sngan' else ch_out,
                                 kernel_size=3, act_alpha=0.2, **reduced_layer_settings)
            if to_rgb_mode == 'sagan':
                to_rgb = nn.Sequential(
                    GeneralConv(ch_out, ch_rgb, kernel_size=1, act_alpha=-1, **reduced_layer_settings), to_rgb)
        elif to_rgb_mode == 'biggan':
            to_rgb = nn.Sequential(nn.BatchNorm1d(ch_out), nn.ReLU(),
                                   GeneralConv(ch_out, ch_rgb, kernel_size=3, act_alpha=-1, **reduced_layer_settings))
        else:
            raise ValueError()
        if no_tanh:
            self.toRGB = to_rgb
        else:
            self.toRGB = nn.Sequential(to_rgb, ScaledTanh())
        if deep:
            self.residual = PassChannelResidual()
        else:
            if not is_first and is_residual:
                self.residual = nn.Sequential() if ch_in == ch_out else \
                    GeneralConv(ch_in, ch_out, 1, act_alpha=-1, **reduced_layer_settings)
            else:
                self.residual = None
        self.deep = deep

    @staticmethod
    def get_per_channel_noise(noise_weight):
        return None if noise_weight is None else torch.randn(*noise_weight.size()) * noise_weight

    def forward(self, x, y=None, z=None, last=False):
        h = self.c1(x, y=y, z=z, conv_noise=self.get_per_channel_noise(self.c1_noise_weight))
        h = self.c2(h, y=y, z=z, conv_noise=self.get_per_channel_noise(self.c2_noise_weight))
        if self.deep:
            h = self.c3(h, y=y, z=z, conv_noise=self.get_per_channel_noise(self.c3_noise_weight))
            h = self.c4(h, y=y, z=z, conv_noise=self.get_per_channel_noise(self.c4_noise_weight))
            h = self.residual(h, x)
        elif self.residual is not None:
            h = h + self.residual(x)
        if last:
            return self.toRGB(h)
        return h


class Generator(nn.Module):
    def __init__(self, initial_kernel_size, num_rgb_channels, fmap_base, fmap_max, fmap_min, kernel_size,
                 self_attention_layers, progression_scale_up, progression_scale_down, residual, separable,
                 equalized, init, act_alpha, num_classes, deep, z_distribution, spectral=False,
                 latent_size=256, no_tanh=False, per_channel_noise=False, to_rgb_mode='pggan', z_to_bn=False,
                 split_z=False, dropout=0.2, act_norm='pixel', conv_only=False, shared_embedding_size=32,
                 normalize_latents=True, rgb_generation_mode='pggan'):
        """
        :param initial_kernel_size: int, this should be always correct regardless of conv_only
        :param num_rgb_channels: int
        :param fmap_base: int
        :param fmap_max: int
        :param fmap_min: int
        :param kernel_size: int
        :param self_attention_layers: list[int]
        :param progression_scale_up: list[int]
        :param progression_scale_down: list[int]
        :param residual: bool
        :param separable: bool
        :param equalized: bool
        :param spectral: bool
        :param init: 'kaiming_normal' or 'xavier_uniform' or 'orthogonal'
        :param act_alpha: float, 0 is relu, -1 is linear and 0.2 is recommended
        :param z_distribution: 'normal' or 'bernoulli' or 'censored'
        :param latent_size: int
        :param no_tanh: bool
        :param deep: bool, in case it's true it will turn off split_z
        :param per_channel_noise: bool
        :param to_rgb_mode: 'pggan' or 'sagan' or 'sngan' or 'biggan'
        :param z_to_bn: bool, whether to concatenate z with y(if available) to feed to cbn or not
        :param split_z: bool
        :param dropout: float
        :param num_classes: int, input y.shape == (batch_size, num_classes, T_y)
        :param act_norm: 'batch' or 'pixel' or None
        :param conv_only: bool
        :param shared_embedding_size: int, in case it's none zero, y will be transformed to (batch_size, shared_embedding_size, T_y)
        :param normalize_latents: bool
        :param rgb_generation_mode: 'residual'sum([rgbs]) or 'mean'mean([rgbs]) or 'pggan'(last_rgb)
        """
        super().__init__()
        R = len(progression_scale_up)
        assert len(progression_scale_up) == len(progression_scale_down)
        self.progression_scale_up = progression_scale_up
        self.progression_scale_down = progression_scale_down
        self.depth = 0
        self.alpha = 1.0
        if deep:
            split_z = False
        self.split_z = split_z
        self.z_distribution = z_distribution
        self.conv_only = conv_only
        self.initial_kernel_size = initial_kernel_size
        self.normalize_latents = normalize_latents
        self.z_to_bn = z_to_bn

        def nf(stage):
            return min(max(int(fmap_base / (2.0 ** stage)), fmap_min), fmap_max)

        if latent_size is None:
            latent_size = nf(0)
        self.input_latent_size = latent_size
        if num_classes != 0:
            if shared_embedding_size > 0:
                self.y_encoder = GeneralConv(num_classes, shared_embedding_size, kernel_size=1,
                                             equalized=False, act_alpha=act_alpha, spectral=False, bias=False)
                num_classes = shared_embedding_size
            else:
                self.y_encoder = nn.Sequential()
        else:
            self.y_encoder = None
        if split_z:
            latent_size //= R + 2  # we also give part of the z to the first layer
        self.latent_size = latent_size
        block_settings = dict(ch_rgb=num_rgb_channels, k_size=kernel_size, is_residual=residual, deep=deep,
                              no_tanh=no_tanh, per_channel_noise=per_channel_noise, to_rgb_mode=to_rgb_mode)
        layer_settings = dict(z_to_bn_size=latent_size if z_to_bn else 0, equalized=equalized, spectral=spectral,
                              init=init, act_alpha=act_alpha, do=dropout, num_classes=num_classes, act_norm=act_norm,
                              bias=True, separable=separable)
        self.block0 = GBlock(latent_size, nf(1), **block_settings, **layer_settings,
                             initial_kernel_size=None if conv_only else initial_kernel_size)
        dummy = []  # to make SA layers registered
        self.self_attention = dict()
        for layer in self_attention_layers:
            dummy.append(SelfAttention(nf(layer + 1), spectral, init))
            self.self_attention[layer] = dummy[-1]
        if len(dummy) != 0:
            self.dummy = nn.ModuleList(dummy)
        self.blocks = nn.ModuleList(
            [GBlock(nf(i + 1), nf(i + 2), **block_settings, **layer_settings) for i in range(R)])
        self.max_depth = len(self.blocks)
        self.deep = deep
        self.rgb_generation_mode = rgb_generation_mode

    def _split_z(self, l, z):
        if not self.z_to_bn:
            return None
        if self.split_z:
            return z[:, (2 + l) * self.latent_size:(3 + l) * self.latent_size]
        return z

    def _do_layer(self, l, h, y, z):
        if l in self.self_attention:
            h, attention_map = self.self_attention[l](h)
        else:
            attention_map = None
        h = resample_signal(h, self.progression_scale_down[l], self.progression_scale_up[l], True)
        return self.blocks[l](h, y, self._split_z(l, z), last=False), attention_map

    def _combine_rgbs(self, last_rgb, saved_rgbs):
        if self.rgb_generation_mode == 'residual':
            return_value = saved_rgbs[0]
            for rgb in saved_rgbs[1:]:
                return_value = resample_signal(return_value, return_value.size(2), rgb.size(2), True) + rgb
            if self.alpha == 1.0:
                return return_value
            return return_value - (1.0 - self.alpha) * saved_rgbs[-1]
        elif self.rgb_generation_mode == 'mean':
            return_value = saved_rgbs[0]
            for rgb in saved_rgbs[1:]:
                return_value = resample_signal(return_value, return_value.size(2), rgb.size(2), True) + rgb
            return_value = return_value / len(saved_rgbs)
            if self.alpha == 1.0:
                return return_value
            return (return_value * len(saved_rgbs) - saved_rgbs[-1]) / (len(saved_rgbs) - 1) * (
                    1.0 - self.alpha) + return_value * self.alpha
        return last_rgb

    def _wrap_output(self, last_rgb, all_rgbs, y):
        return {'x': self._combine_rgbs(last_rgb, all_rgbs), 'y': y}

    def forward(self, z):
        if isinstance(z, dict):
            z, y = z['z'], z.get('y', None)
        elif isinstance(z, tuple):
            z, y = z
        else:
            y = None
        if y is not None:
            if y.ndimension() == 2:
                y = y.unsqueeze(2)
            if self.y_encoder is not None:
                y = self.y_encoder(y)
            else:
                y = None
        if self.normalize_latents:
            z = pixel_norm(z)
        if z.ndimension() == 2:
            z = z.unsqueeze(2)
        if self.split_z and not self.deep:
            h = z[:, :self.latent_size, :]
        else:
            h = z
        save_rgb = self.rgb_generation_mode != 'pggan'
        saved_rgbs = []
        if self.depth == 0:
            h = self.block0(h, y, self._split_z(-1, z), last=True)
            if save_rgb:
                saved_rgbs.append(h)
            return self._wrap_output(h, saved_rgbs, y), {}
        h = self.block0(h, y, self._split_z(-1, z))
        if save_rgb:
            saved_rgbs.append(self.block0.toRGB(h))
        all_attention_maps = {}
        for i in range(self.depth - 1):
            h, attention_map = self._do_layer(i, h, y, z)
            if save_rgb:
                saved_rgbs.append(self.blocks[i].toRGB(h))
            if attention_map is not None:
                all_attention_maps[i] = attention_map
        h = resample_signal(h, self.progression_scale_down[self.depth - 1], self.progression_scale_up[self.depth - 1],
                            True)
        ult = self.blocks[self.depth - 1](h, y, self._split_z(self.depth - 1, z), True)
        if save_rgb:
            saved_rgbs.append(ult)
        if self.alpha == 1.0:
            return self._wrap_output(ult, saved_rgbs, y), all_attention_maps
        preult_rgb = self.blocks[self.depth - 2].toRGB(h) if self.depth > 1 else self.block0.toRGB(h)
        return self._wrap_output(preult_rgb * (1.0 - self.alpha) + ult * self.alpha, saved_rgbs, y), all_attention_maps


class DBlock(nn.Module):
    def __init__(self, ch_in, ch_out, ch_rgb, k_size=3, initial_kernel_size=None, is_residual=False,
                 deep=False, group_size=4, temporal_groups_per_window=1, conv_disc=False, sinc_ks=0, **layer_settings):
        super().__init__()
        is_last = initial_kernel_size is not None
        self.net = []
        if is_last and group_size >= 0:
            self.net.append(MinibatchStddev(group_size, temporal_groups_per_window, initial_kernel_size))
        hidden_size = (ch_out // 4) if deep else ch_in
        self.net.append(
            GeneralConv(ch_in + (1 if is_last and group_size >= 0 else 0),
                        hidden_size, kernel_size=k_size, **layer_settings))
        if deep:
            self.net.append(
                GeneralConv(hidden_size, hidden_size, kernel_size=k_size, **layer_settings))
            self.net.append(
                GeneralConv(hidden_size, hidden_size, kernel_size=k_size, **layer_settings))
        is_linear_last = is_last and not conv_disc
        self.net.append(GeneralConv(hidden_size, ch_out, kernel_size=initial_kernel_size if is_linear_last else k_size,
                                    pad=0 if is_linear_last else None, **layer_settings))
        self.net = nn.Sequential(*self.net)
        reduced_layer_settings = dict(equalized=layer_settings['equalized'], spectral=layer_settings['spectral'],
                                      init=layer_settings['init'])
        if sinc_ks == 0:
            self.fromRGB = GeneralConv(ch_rgb, ch_in, kernel_size=1, act_alpha=layer_settings['act_alpha'],
                                       **reduced_layer_settings)
        else:
            assert sinc_ks % ch_rgb == 0, 'sinc_ks must be divisible by ch_rgb'
            # TODO read sample_rate from input
            self.fromRGB = SincEncoder(ch_rgb, is_shared=False, kernel_size=sinc_ks, num_kernels=sinc_ks // ch_rgb,
                                       sample_rate=60.0, min_low_hz=0.01, min_band_hz=1.0)
        if deep:
            self.residual = ConcatResidual(ch_in, ch_out, **reduced_layer_settings)
        else:
            if is_residual and (not is_last or conv_disc):
                self.residual = nn.Sequential() if ch_in == ch_out else GeneralConv(ch_in, ch_out, kernel_size=1,
                                                                                    act_alpha=-1,
                                                                                    **reduced_layer_settings)
            else:
                self.residual = None
        self.deep = deep

    def forward(self, x):
        h = self.net(x)
        if self.deep:
            return self.residual(h, x)
        if self.residual:
            h = h + self.residual(x)
        return h


class Discriminator(nn.Module):
    def __init__(self, initial_kernel_size, num_rgb_channels, fmap_base, fmap_max, fmap_min, kernel_size,
                 self_attention_layers, progression_scale_up, progression_scale_down, residual, separable,
                 equalized, init, act_alpha, num_classes, deep, spectral=False, dropout=0.2, act_norm=None,
                 group_size=4, temporal_groups_per_window=1, conv_only=False, input_to_all_layers=False, sinc_ks=0):
        """
        NOTE we only support global conidtioning(not temporal) for now
        :param initial_kernel_size:
        :param num_rgb_channels:
        :param fmap_base:
        :param fmap_max:
        :param fmap_min:
        :param kernel_size:
        :param self_attention_layers:
        :param progression_scale_up:
        :param progression_scale_down:
        :param residual:
        :param separable:
        :param equalized:
        :param spectral:
        :param init:
        :param act_alpha:
        :param num_classes:
        :param deep:
        :param dropout:
        :param act_norm:
        :param group_size:
        :param temporal_groups_per_window:
        :param conv_only:
        :param input_to_all_layers:
        """
        super().__init__()
        R = len(progression_scale_up)
        assert len(progression_scale_up) == len(progression_scale_down)
        self.progression_scale_up = progression_scale_up
        self.progression_scale_down = progression_scale_down
        self.depth = 0
        self.alpha = 1.0
        self.input_to_all_layers = input_to_all_layers

        def nf(stage):
            return min(max(int(fmap_base / (2.0 ** stage)), fmap_min), fmap_max)

        layer_settings = dict(equalized=equalized, spectral=spectral, init=init, act_alpha=act_alpha,
                              do=dropout, num_classes=0, act_norm=act_norm, bias=True, separable=separable)
        block_settings = dict(ch_rgb=num_rgb_channels, k_size=kernel_size, is_residual=residual, conv_disc=conv_only,
                              group_size=group_size, temporal_groups_per_window=temporal_groups_per_window, deep=deep,
                              sinc_ks=sinc_ks)

        last_block = DBlock(nf(1), nf(0), initial_kernel_size=initial_kernel_size, **block_settings, **layer_settings)
        dummy = []  # to make SA layers registered
        self.self_attention = dict()
        for layer in self_attention_layers:
            dummy.append(SelfAttention(nf(layer + 1), spectral, init))
            self.self_attention[layer] = dummy[-1]
        if len(dummy):
            self.dummy = nn.ModuleList(dummy)
        self.blocks = nn.ModuleList(
            [DBlock(nf(i + 2), nf(i + 1), **block_settings, **layer_settings) for i in range(R - 1, -1, -1)] + [
                last_block])

        if num_classes != 0:
            self.class_emb = nn.Linear(num_classes, nf(0), False)
            if spectral:
                self.class_emb = spectral_norm(self.class_emb)
        else:
            self.class_emb = None
        self.linear = GeneralConv(nf(0), 1, kernel_size=1, equalized=equalized, act_alpha=-1,
                                  spectral=spectral, init=init)
        self.max_depth = len(self.blocks) - 1

    def forward(self, x):
        if isinstance(x, dict):
            x, y = x['x'], x.get('y', None)
        elif isinstance(x, tuple):
            x, y = x
        else:
            y = None
        h = self.blocks[-(self.depth + 1)](x, True)
        if self.depth > 0:
            h = resample_signal(h, self.progression_scale_up[self.depth - 1],
                                self.progression_scale_down[self.depth - 1], True)
            if self.alpha < 1.0 or self.input_to_all_layers:
                x_lowres = resample_signal(x, self.progression_scale_up[self.depth - 1],
                                           self.progression_scale_down[self.depth - 1], True)
                preult_rgb = self.blocks[-self.depth].fromRGB(x_lowres)
                if self.input_to_all_layers:
                    h = (h * self.alpha + preult_rgb) / (1.0 + self.alpha)
                else:
                    h = h * self.alpha + (1.0 - self.alpha) * preult_rgb
        all_attention_maps = {}
        for i in range(self.depth, 0, -1):
            h = self.blocks[-i](h)
            if i > 1:
                h = resample_signal(h, self.progression_scale_up[i - 2], self.progression_scale_down[i - 2], True)
                if self.input_to_all_layers:
                    x_lowres = resample_signal(x_lowres, self.progression_scale_up[i - 2],
                                               self.progression_scale_down[i - 2], True)
                    h = (h + self.blocks[-i + 1].fromRGB(x_lowres)) / 2.0
            if (i - 2) in self.self_attention:
                h, attention_map = self.self_attention[i - 2](h)
                if attention_map is not None:
                    all_attention_maps[i] = attention_map
        o = self.linear(h).mean(dim=2).squeeze()
        if y is not None:
            emb = self.class_emb(y)
            cond_loss = (emb * h.squeeze()).sum(dim=1)
        else:
            cond_loss = 0.0
        return o + cond_loss, h, all_attention_maps


class MultiDiscriminator(nn.Module):
    def __init__(self, initial_kernel_size, num_rgb_channels, fmap_base, fmap_max, fmap_min, kernel_size,
                 self_attention_layers, progression_scale_up, progression_scale_down, residual, separable,
                 equalized, init, act_alpha, num_classes, deep, spectral=False, dropout=0.2, act_norm=None,
                 group_size=4, temporal_groups_per_window=1, conv_only=False, input_to_all_layers=False, sinc_ks=121,
                 all_sinc_weight=0.0, all_time_weight=1.0, shared_sinc_weight=1.0, shared_time_weight=1.0,
                 one_sec_weight=1.0):
        super().__init__()
        self.all_sinc_weight = all_sinc_weight if sinc_ks > 0 else 0.0
        self.all_time_weight = all_time_weight
        self.shared_sinc_weight = shared_sinc_weight if sinc_ks > 0 else 0.0
        self.shared_time_weight = shared_time_weight
        self.one_sec_weight = one_sec_weight
        all_params = [initial_kernel_size, num_rgb_channels, fmap_base, fmap_max, fmap_min, kernel_size,
                      self_attention_layers, progression_scale_up, progression_scale_down, residual, separable,
                      equalized, init, act_alpha, num_classes, deep, spectral, dropout, act_norm, group_size,
                      temporal_groups_per_window, conv_only, input_to_all_layers]
        # TODO write alpha and depth setters
        if all_sinc_weight != 0:
            self.all_sinc_net = Discriminator(*all_params, sinc_ks)
        if all_time_weight != 0:
            self.all_time_net = Discriminator(*all_params, 0)
        shared_params = [1, fmap_base // 2, fmap_max // 2, fmap_min // 2, kernel_size,
                         self_attention_layers, progression_scale_up, progression_scale_down, residual, separable,
                         equalized, init, act_alpha, num_classes, deep, spectral, dropout, act_norm]
        shared_k_params = dict(group_size=group_size, temporal_groups_per_window=temporal_groups_per_window,
                               conv_only=conv_only, input_to_all_layers=input_to_all_layers)
        if shared_sinc_weight != 0:
            self.shared_sinc_net = Discriminator(initial_kernel_size, *shared_params, **shared_k_params,
                                                 sinc_ks=sinc_ks)
        if shared_time_weight != 0:
            self.shared_time_net = Discriminator(initial_kernel_size, *shared_params, **shared_k_params, sinc_ks=0)
        if one_sec_weight != 0:
            psunp = np.array(progression_scale_up)
            psdnp = np.array(progression_scale_down)
            self.signal_lens = [np.sum(psunp[:i] / psdnp[:i]) for i in range(len(progression_scale_up) + 1)]
            self.one_sec_net = Discriminator(1, *shared_params, group_size=0, temporal_groups_per_window=0,
                                             conv_only=conv_only, input_to_all_layers=input_to_all_layers, sinc_ks=0)

    def forward(self, x):
        o = 0.0
        if self.all_sinc_weight != 0:
            o = o + self.all_sinc_weight * self.all_sinc_net(x)[0]
        if self.all_time_weight != 0:
            o = o + self.all_time_weight * self.all_time_net(x)[0]
        if self.shared_sinc_weight != 0:
            o = o + self.shared_sinc_weight * self.shared_sinc_net(x)[0]
        if self.shared_time_weight != 0:
            o = o + self.shared_time_weight * self.shared_time_net(x)[0]
        if self.one_sec_weight != 0:
            if isinstance(x, dict):
                x, y = x['x'], x.get('y', None)
            elif isinstance(x, tuple):
                x, y = x
            else:
                y = None
            stride = self.signal_lens[self.depth]
            x = torch.cat([x[:, :, i * stride:(i + 1) * stride] for i in range(x.size(2) // stride)], dim=0)
            if y is not None:
                y = torch.repeat(y, x.size(2) // stride, 0)
            o = o + self.one_sec_weight * self.one_sec_weight((x, y))[0]
        return o


def main():
    initial_kernel_size = 8
    num_rgb_channels = 3
    fmap_base = 256
    fmap_max = 256
    fmap_min = 8
    kernel_size = 3
    self_attention_layers = []
    progression_scale_up = [3, 4, 2]
    progression_scale_down = [1, 3, 1]
    residual = True
    separable = True
    equalized = True
    spectral = False
    init = 'orthogonal'
    act_alpha = 0.2
    num_classes = 0
    deep = False
    # z_distribution = 'normal'
    # latent_size = 64
    # no_tanh = False
    # per_channel_noise = True
    # to_rgb_mode = 'pggan'
    # z_to_bn = True
    # split_z = False
    dropout = 0.2
    act_norm = 'batch'
    # conv_only = True
    # shared_embedding_size = 32
    # normalize_latents = True
    # rgb_generation_mode = 'pggan'
    group_size = 4
    temporal_groups_per_window = 2
    conv_only = True
    input_to_all_layers = False
    # g = Generator(initial_kernel_size, num_rgb_channels, fmap_base, fmap_max, fmap_min, kernel_size,
    #               self_attention_layers, progression_scale_up, progression_scale_down, residual, separable, equalized,
    #               spectral, init, act_alpha, num_classes, deep, z_distribution, latent_size, no_tanh, per_channel_noise,
    #               to_rgb_mode, z_to_bn, split_z, dropout, act_norm, conv_only, shared_embedding_size, normalize_latents,
    #               rgb_generation_mode)
    d = Discriminator(initial_kernel_size, num_rgb_channels, fmap_base, fmap_max, fmap_min, kernel_size,
                      self_attention_layers, progression_scale_up, progression_scale_down, residual, separable,
                      equalized, spectral, init, act_alpha, num_classes, deep, dropout, act_norm,
                      group_size, temporal_groups_per_window, conv_only, input_to_all_layers)
    d.alpha = 1.0
    d.depth = 3
    print(d(torch.randn(4, num_rgb_channels, initial_kernel_size * 8)))


if __name__ == '__main__':
    # main()
    pass
