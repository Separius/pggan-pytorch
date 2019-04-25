import math
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from collections import namedtuple
from pytorch_pretrained_bert.modeling import BertLayer

from cpc.cpc_loss import KPredLoss, OneOneMI, SeqOneMI, IIC, myIIC


class SincEncoder(nn.Module):
    def __init__(self, num_channels, is_shared=True, kernel_size=121,
                 num_kernels=16, sample_rate=60.0, min_low_hz=0.0, min_band_hz=1.0):
        super().__init__()
        self.is_shared = is_shared
        if is_shared:
            self.sinc = SincConv(kernel_size, num_kernels, sample_rate, min_low_hz, min_band_hz)
        else:
            self.sinc = nn.ModuleList([SincConv(kernel_size, num_kernels) for i in range(num_channels)])

    def forward(self, x):
        B, C, T = x.shape
        if self.is_shared:
            return self.sinc(x.view(B * C, 1, T)).view(B, -1, T)
        return torch.cat([self.sinc[i](x[:, i:i + 1, :]) for i in range(C)], dim=1)


class SincConv(nn.Module):
    def __init__(self, kernel_size, out_channels, sample_rate=60.0, min_low_hz=0.0, min_band_hz=1.0):
        super().__init__()
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        if kernel_size % 2 == 0:  # Forcing the filters to be odd (i.e, perfectly symmetrics)
            self.kernel_size = self.kernel_size + 1
        self.sample_rate = sample_rate
        self.min_low_hz = min_low_hz
        self.min_band_hz = min_band_hz

        hz = np.linspace(min_low_hz, sample_rate / 2 - (min_low_hz + min_band_hz), out_channels + 1)
        # filter lower frequency (out_channels, 1)
        self.low_hz_ = nn.Parameter(torch.Tensor(hz[:-1]).view(-1, 1))
        # filter frequency band (out_channels, 1)
        self.band_hz_ = nn.Parameter(torch.Tensor(np.diff(hz)).view(-1, 1))
        # Hamming window
        n_lin = torch.linspace(0, (self.kernel_size / 2) - 1, steps=int((self.kernel_size / 2)))
        self.window_ = 0.54 - 0.46 * torch.cos(2.0 * math.pi * n_lin / self.kernel_size)
        n = (self.kernel_size - 1) / 2.0
        self.n_ = 2 * math.pi * torch.arange(-n, 0).view(1, -1) / self.sample_rate

    def forward(self, waveforms):
        self.n_ = self.n_.to(waveforms.device)
        self.window_ = self.window_.to(waveforms.device)
        low = self.min_low_hz + torch.abs(self.low_hz_)
        high = torch.clamp(low + self.min_band_hz + torch.abs(self.band_hz_), self.min_low_hz, self.sample_rate / 2)
        band = (high - low)[:, 0]
        f_times_t_low = torch.matmul(low, self.n_)
        f_times_t_high = torch.matmul(high, self.n_)
        band_pass_left = ((torch.sin(f_times_t_high) - torch.sin(f_times_t_low)) / (self.n_ / 2)) * self.window_
        band_pass_center = 2 * band.view(-1, 1)
        band_pass_right = torch.flip(band_pass_left, dims=[1])
        band_pass = torch.cat([band_pass_left, band_pass_center, band_pass_right], dim=1)
        band_pass = band_pass / (2 * band[:, None])
        self.filters = band_pass.view(self.out_channels, 1, self.kernel_size)
        x_p = F.pad(waveforms, (self.kernel_size // 2, self.kernel_size // 2), mode='reflect')
        return F.conv1d(x_p, self.filters, bias=None)


class ResidualEncoder(nn.Module):
    class ResidualBlock(nn.Module):
        def __init__(self, in_channels, out_channels, stride=1, downsample=None):
            super().__init__()
            self.conv1 = ResidualEncoder.conv(in_channels, out_channels, stride)
            self.bn1 = nn.BatchNorm1d(out_channels)
            self.relu = nn.ReLU()
            self.conv2 = ResidualEncoder.conv(out_channels, out_channels)
            self.bn2 = nn.BatchNorm1d(out_channels)
            self.downsample = downsample

        def forward(self, x):
            residual = x
            out = self.conv1(x)
            out = self.bn1(out)
            out = self.relu(out)
            out = self.conv2(out)
            out = self.bn2(out)
            if self.downsample:
                residual = self.downsample(x)
            out += residual
            out = self.relu(out)
            return out

    @staticmethod
    def conv(in_channels, out_channels, stride=1):
        return nn.Conv1d(in_channels, out_channels, kernel_size=5,
                         stride=stride, padding=2, bias=False)

    def __init__(self, input_channels=5, _=0):
        super().__init__()
        self.in_channels = 16
        conv = self.conv(input_channels, self.in_channels)
        bn = nn.BatchNorm1d(self.in_channels)
        relu = nn.ReLU()
        # generates 32 codes of size 128
        layers = [
            self.make_layer(self.in_channels, 1),
            self.make_layer(32, 2, 5),
            self.make_layer(64, 1, 2),
            self.make_layer(128, 1, 2),
            nn.AvgPool1d(3),
        ]
        self.z_size = 128
        self.network = nn.Sequential(conv, bn, relu, *layers)
        for m in self.modules():
            if isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def make_layer(self, out_channels, blocks, stride=1):
        downsample = None
        if (stride != 1) or (self.in_channels != out_channels):
            downsample = nn.Sequential(
                self.conv(self.in_channels, out_channels, stride=stride),
                nn.BatchNorm1d(out_channels))
        layers = [self.ResidualBlock(self.in_channels, out_channels, stride, downsample)]
        self.in_channels = out_channels
        for i in range(1, blocks):
            layers.append(self.ResidualBlock(out_channels, out_channels))
        return nn.Sequential(*layers)

    def forward(self, x):
        return self.network(x)


class ConvEncoder(nn.Module):
    def __init__(self, input_channels=5, dropout=0.1):
        super().__init__()
        down_ratios = [5, 4, 3]  # generates 32 codes of size 128
        channel_sizes = [32, 64, 128]
        kernel_sizes = [2 * dr - 1 for dr in down_ratios]
        self.z_size = channel_sizes[-1]
        act = nn.ReLU()
        net = []
        last_layer = len(down_ratios) - 1
        for i, (dr, cs, ks) in enumerate(zip(down_ratios, channel_sizes, kernel_sizes)):
            net.append(nn.Conv1d(input_channels, cs, ks, stride=dr, padding=(ks - 1) // 2))
            net.append(act)
            if dropout != 0.0 and i != last_layer:
                net.append(nn.Dropout(dropout))
            if i != last_layer:
                net.append(nn.BatchNorm1d(cs))
            input_channels = cs
        self.network = nn.Sequential(*net)

    def forward(self, x):
        return self.network(x)


class PNormPooling(nn.Module):
    def __init__(self, input_size, batch_norm=True, mlp_sizes=None, p=None, be_mean_pool=False):
        super().__init__()
        if be_mean_pool:
            self.pool_size = input_size
            self.is_pool = True
        self.is_pool = False
        if p is None:
            p = [1.0, float('+inf')]
        if mlp_sizes is None:
            mlp_sizes = [input_size * 2, input_size]
        self.p = p
        network = []
        mlp_sizes = [input_size * len(p)] + mlp_sizes
        last_layer = len(mlp_sizes) - 2
        self.pool_size = mlp_sizes[-1]
        for enu, (i, o) in enumerate(zip(mlp_sizes, mlp_sizes[1:])):
            network.append(nn.Linear(i, o))
            if enu != last_layer:
                network.append(nn.ReLU())
                if batch_norm:
                    network.append(nn.BatchNorm1d(o))
        self.network = nn.Sequential(*network)

    def forward(self, x):
        if self.is_pool:
            return x.mean(dim=2)
        return self.network(torch.cat([torch.norm(x, p, 2) for p in self.p], dim=1))


class AutoRNN(nn.Module):
    def __init__(self, input_size, hidden_size, bidirectional=False, cell_type='GRU', num_layers=1, dropout=0):
        super().__init__()
        cell_type = cell_type.lower()
        if cell_type == 'gru':
            cell = nn.GRU
        elif cell_type == 'lstm':
            cell = nn.LSTM
        elif cell_type == 'rnn':
            cell = nn.RNN
        else:
            raise ValueError('invalid cell_type')
        self.forward_rnn = cell(input_size, hidden_size, num_layers, dropout=dropout)
        if bidirectional:
            self.backward_rnn = cell(input_size, hidden_size, num_layers, dropout=dropout)
            self.c_size = hidden_size * 2
        else:
            self.backward_rnn = None
            self.c_size = hidden_size

    def forward(self, x):
        x_permuted = x.permute(2, 0, 1)
        result = self.forward_rnn(x_permuted)[0]
        if self.backward_rnn is not None:
            result = torch.cat([result, self.backward_rnn(x_permuted.flip(0))[0]], dim=2)
        return result.permute(1, 2, 0)


class Transformer(nn.Module):
    class BertConfig:
        def __init__(self, hidden_size, num_heads, dropout):
            self.hidden_size = hidden_size
            self.num_attention_heads = num_heads
            self.attention_probs_dropout_prob = dropout
            self.hidden_dropout_prob = dropout
            self.intermediate_size = hidden_size * 2
            self.hidden_act = 'gelu'  # gelu, relu, swish

    def create_attention_mask(self, seq_len, forward=True):
        if self.causal:
            if forward:
                attention_mask = torch.tril(torch.ones(1, 1, seq_len, seq_len))
            else:
                attention_mask = torch.triu(torch.ones(1, 1, seq_len, seq_len))
        else:
            attention_mask = torch.ones(1, 1, 1, seq_len)
        return (1.0 - attention_mask) * -10000.0

    def __init__(self, input_size, causal=True, bidirectional=False,
                 num_layers=3, num_heads=4, dropout=0.2, max_seq_len=32):
        super().__init__()
        self.pos_embedding = nn.Embedding(max_seq_len, input_size)
        self.causal = causal
        self.bidirectional = bidirectional
        bert_config = self.BertConfig(input_size, num_heads, dropout)
        self.forward_transformer = nn.ModuleList([BertLayer(bert_config) for _ in range(num_layers)])
        self.c_size = input_size
        if bidirectional and causal:
            self.backward_transformer = nn.ModuleList([BertLayer(bert_config) for _ in range(num_layers)])
            self.c_size *= 2
        else:
            self.backward_transformer = None

    def forward(self, x):  # BCT
        pos_embedding = self.pos_embedding(torch.arange(x.size(2)).to(x.device)).permute(1, 0).unsqueeze(0)
        forward_mask = self.create_attention_mask(x.size(2), True).to(x)
        forward_output = (x + pos_embedding).permute(0, 2, 1)
        for bl in self.forward_transformer:
            forward_output = bl(forward_output, forward_mask)
        forward_output = forward_output.permute(0, 2, 1)
        if self.backward_transformer is None:
            return forward_output
        backward_mask = self.create_attention_mask(x.size(2), False).to(x)
        backward_output = (x + pos_embedding).permute(0, 2, 1)
        for bl in self.backward_transformer:
            backward_output = bl(backward_output, backward_mask)
        backward_output = backward_output.permute(0, 2, 1)
        return torch.cat([forward_output, backward_output], dim=1)


NetworkLosses = namedtuple('NetworkLosses', ['global_', 'local_', 'prediction_', 'z_iic_', 'c_iic_'])
NetworkAccuracies = namedtuple('NetworkAccuracies', ['global_', 'local_', 'prediction_', 'z_iic_', 'c_iic_'])
NetworkLatents = namedtuple('NetworkLatents', ['z', 'c', 'z_p', 'c_p', 'z_iic', 'c_iic'])
NetworkOutput = namedtuple('NetworkOutput', ['losses', 'accuracies', 'latents'])


class Network(nn.Module):
    def __init__(self, input_channels, encoder_dropout=0.1, bidirectional=False, contextualizer_num_layers=1,
                 contextualizer_dropout=0, use_transformer=False, causal_prediction=True, prediction_k=4,
                 have_global=True, have_local=True, residual_encoder=False, rnn_hidden_multiplier=2,
                 global_mode='mlp', local_mode='mlp', num_z_iic_classes=0, num_c_iic_classes=0):
        super().__init__()
        if residual_encoder:
            encoder = ResidualEncoder(input_channels)
        else:
            encoder = ConvEncoder(input_channels, encoder_dropout)
        if have_global:
            z_pooler = PNormPooling(encoder.z_size)
        else:
            z_pooler = None
        if num_z_iic_classes != 0:
            self.z_iic = nn.Sequential(
                nn.Linear(z_pooler.pool_size if z_pooler is not None else encoder.z_size, num_z_iic_classes),
                nn.Softmax(-1))
        else:
            self.z_iic = None
        self.num_z_iic_classes = num_z_iic_classes
        if use_transformer:
            contextualizer = Transformer(input_size=encoder.z_size, causal=True, bidirectional=bidirectional,
                                         num_heads=4, max_seq_len=32, num_layers=contextualizer_num_layers,
                                         dropout=contextualizer_dropout)
        else:
            contextualizer = AutoRNN(input_size=encoder.z_size, hidden_size=rnn_hidden_multiplier * encoder.z_size,
                                     bidirectional=bidirectional, num_layers=contextualizer_num_layers,
                                     dropout=contextualizer_dropout, cell_type='GRU')
        if have_global or have_local:
            c_pooler = PNormPooling(contextualizer.c_size)
        else:
            c_pooler = None
        if num_z_iic_classes != 0:
            self.c_iic = nn.Sequential(
                nn.Linear(c_pooler.pool_size if c_pooler is not None else contextualizer.c_size, num_c_iic_classes),
                nn.Softmax(-1))
        else:
            self.c_iic = None
        self.num_c_iic_classes = num_c_iic_classes
        prediction_loss_network = KPredLoss(contextualizer.c_size, encoder.z_size, k=prediction_k,
                                            auto_is_bidirectional=bidirectional, look_both=not causal_prediction)
        if have_global:
            c_pooled_mi_z_pooled = OneOneMI(c_pooler.pool_size, z_pooler.pool_size, mode=global_mode,
                                            hidden_size=min(c_pooler.pool_size, z_pooler.pool_size) * 2)
        else:
            c_pooled_mi_z_pooled = None
        if have_local:
            c_pooled_mi_z = SeqOneMI(c_pooler.pool_size, encoder.z_size, mode=local_mode,
                                     hidden_size=min(c_pooler.pool_size, encoder.z_size) * 2)
        else:
            c_pooled_mi_z = None

        self.encoder = encoder
        self.z_pooler = z_pooler
        self.contextualizer = contextualizer
        self.c_pooler = c_pooler
        self.prediction_loss_network = prediction_loss_network
        self.c_pooled_mi_z_pooled = c_pooled_mi_z_pooled
        self.c_pooled_mi_z = c_pooled_mi_z

    @staticmethod
    def calculate_iic_stats(l1, l2, device, my_iic=False):
        if l1 is None:
            iic_loss = torch.tensor(0.0).to(device)
            iic_accuracy = 0.0
        else:
            iic_loss = myIIC(l1, l2) if my_iic else IIC(l1, l2)
            iic_accuracy = (l1.argmax(dim=1) == l2.argmax(dim=1)).sum().item() / l1.size(0)
        return iic_loss, iic_accuracy

    def forward(self, x, input_is_long=False, no_loss=False):
        if not input_is_long:
            return self.half_forward(x, no_loss)
        batch_size, num_channels, long_seq_len = x.size()
        seq_len = int(long_seq_len * 2 / 3)
        if no_loss:
            start = (long_seq_len - seq_len) // 2
            return self.half_forward(x[..., start:start + seq_len], no_loss)
        # NOTE you can also do this: https://stackoverflow.com/a/55787074/2796084
        batch_starts = torch.randint(long_seq_len - seq_len, (2 * batch_size,))
        x_1 = torch.stack([x[i, :, batch_starts[2 * i]:batch_starts[2 * i] + seq_len] for i in range(batch_size)],
                          dim=0)
        x_2 = torch.stack(
            [x[i, :, batch_starts[2 * i + 1]:batch_starts[2 * i + 1] + seq_len] for i in range(batch_size)], dim=0)
        x1_res = self.half_forward(x_1, no_loss)
        x2_res = self.half_forward(x_2, no_loss)
        iic_loss_z, iic_accuracy_z = self.calculate_iic_stats(x1_res.latents.z_iic, x2_res.latents.z_iic, x)
        iic_loss_c, iic_accuracy_c = self.calculate_iic_stats(x1_res.latents.c_iic, x2_res.latents.c_iic, x)
        return NetworkOutput(losses=NetworkLosses((x1_res.losses.global_ + x2_res.losses.global_) / 2,
                                                  (x1_res.losses.local_ + x2_res.losses.local_) / 2,
                                                  (x1_res.losses.prediction_ + x2_res.losses.prediction_) / 2,
                                                  iic_loss_z, iic_loss_c),
                             accuracies=NetworkAccuracies(
                                 (x1_res.accuracies.global_ + x2_res.accuracies.global_) / 2,
                                 (x1_res.accuracies.local_ + x2_res.accuracies.local_) / 2,
                                 {k: (v + x2_res.accuracies.prediction_[k]) / 2
                                  for k, v in x1_res.accuracies.prediction_.items()},
                                 iic_accuracy_z, iic_accuracy_c), latents=x1_res.latents)

    def half_forward(self, x, no_loss):
        z = self.encoder(x)
        if self.z_pooler is not None:
            z_pooled = self.z_pooler(z)
        else:
            z_pooled = z.mean(dim=2)
        if self.z_iic is not None:
            z_iic = self.z_iic(z_pooled)
        else:
            z_iic = None
        c = self.contextualizer(z)
        if self.c_pooler is not None:
            c_pooled = self.c_pooler(c)
        else:
            c_pooled = c.mean(dim=2)
        if self.c_iic is not None:
            c_iic = self.c_iic(c_pooled)
        else:
            c_iic = None
        if no_loss:
            return NetworkOutput(losses=None, accuracies=None,
                                 latents=NetworkLatents(z, c, z_pooled, c_pooled, z_iic, c_iic))
        prediction_loss, pred_acc = self.prediction_loss_network(c, z)
        if self.c_pooled_mi_z_pooled is not None:
            global_loss, global_accuracy = self.c_pooled_mi_z_pooled(c_pooled, z_pooled)
        else:
            global_loss, global_accuracy = torch.tensor(0.0).to(c_pooled), 0.0
        if self.c_pooled_mi_z is not None:
            local_loss, local_accuracy = self.c_pooled_mi_z(c_pooled, z)
        else:
            local_loss, local_accuracy = torch.tensor(0.0).to(c_pooled), 0.0
        return NetworkOutput(losses=NetworkLosses(global_loss, local_loss, prediction_loss, torch.tensor(0.0).to(x),
                                                  torch.tensor(0.0).to(x)),
                             accuracies=NetworkAccuracies(global_accuracy, local_accuracy, pred_acc, 0.0, 0.0),
                             latents=NetworkLatents(z, c, z_pooled, c_pooled, z_iic, c_iic))
