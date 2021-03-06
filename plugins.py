import gc
import os
import time
from copy import deepcopy
from datetime import timedelta
from glob import glob
from scipy import linalg
from scipy.stats import truncnorm

import matplotlib
import numpy as np
import pandas as pd
import torch
from imageio import imwrite
from sklearn.utils.extmath import randomized_svd

from metrics.ndb import NDB
from torch_utils import Plugin, LossMonitor, Logger
from trainer import Trainer
from utils import generate_samples, cudize, EPSILON, resample_signal
from cpc.cpc_network import Network as CpcNetwork
from cpc.cpc_train import hp as cpc_hp

matplotlib.use('Agg')
import matplotlib.pyplot as plt


class DepthManager(Plugin):
    minibatch_override = {0: 256, 1: 256, 2: 128, 3: 128, 4: 48, 5: 32,
                          6: 32, 7: 32, 8: 16, 9: 16, 10: 8, 11: 8}

    tick_kimg_override = {4: 4, 5: 4, 6: 4, 7: 3, 8: 3, 9: 2, 10: 2, 11: 1}
    training_kimg_override = {1: 200, 2: 200, 3: 200, 4: 200}
    transition_kimg_override = {1: 200, 2: 200, 3: 200, 4: 200}

    def __init__(self,  # everything starts from 0 or 1
                 create_dataloader_fun, create_rlg, max_depth,
                 tick_kimg_default, get_optimizer, default_lr,
                 reset_optimizer: bool = True, disable_progression=False,
                 minibatch_default=256, depth_offset=0,  # starts form 0
                 lod_training_kimg=400, lod_transition_kimg=400):
        super().__init__([(1, 'iteration')])
        self.reset_optimizer = reset_optimizer
        self.minibatch_default = minibatch_default
        self.tick_kimg_default = tick_kimg_default
        self.create_dataloader_fun = create_dataloader_fun
        self.create_rlg = create_rlg
        self.trainer = None
        self.depth = -1
        self.alpha = -1
        self.get_optimizer = get_optimizer
        self.disable_progression = disable_progression
        self.depth_offset = depth_offset
        self.max_depth = max_depth
        self.default_lr = default_lr
        self.alpha_map = self.pre_compute_alpha_map(self.depth_offset, max_depth, lod_training_kimg,
                                                    self.training_kimg_override, lod_transition_kimg,
                                                    self.transition_kimg_override)

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['minibatch_size'] = self.minibatch_default
        self.trainer.stats['alpha'] = {'log_name': 'alpha', 'log_epoch_fields': ['{val:.2f}'], 'val': self.alpha}
        self.iteration(is_resuming=self.trainer.optimizer_d is not None)

    @staticmethod
    def pre_compute_alpha_map(start_depth, max_depth, lod_training_kimg, lod_training_kimg_overrides,
                              lod_transition_kimg, lod_transition_kimg_overrides):
        points = []
        pointer = 0
        for i in range(start_depth, max_depth):
            pointer += int(lod_training_kimg_overrides.get(i + 1, lod_training_kimg) * 1000)
            points.append(pointer)
            pointer += int(lod_transition_kimg_overrides.get(i + 1, lod_transition_kimg) * 1000)
            points.append(pointer)
        return points

    def calc_progress(self, cur_nimg=None):
        if cur_nimg is None:
            cur_nimg = self.trainer.cur_nimg
        depth = self.depth_offset
        alpha = 1.0
        for i, point in enumerate(self.alpha_map):
            if cur_nimg == point:
                break
            if cur_nimg > point and i % 2 == 0:
                depth += 1
            if cur_nimg < point and i % 2 == 1:
                alpha = (cur_nimg - self.alpha_map[i - 1]) / (point - self.alpha_map[i - 1])
                break
            if cur_nimg < point:
                break
        depth = min(self.max_depth, depth)
        if self.disable_progression:
            depth = self.max_depth
            alpha = 1.0
        return depth, alpha

    def iteration(self, is_resuming=False, *args):
        depth, alpha = self.calc_progress()
        dataset = self.trainer.dataset
        if depth != self.depth:
            self.trainer.discriminator.depth = self.trainer.generator.depth = dataset.model_depth = depth
            self.depth = depth
            minibatch_size = self.minibatch_override.get(depth - self.depth_offset, self.minibatch_default)
            if self.reset_optimizer and not is_resuming:
                self.trainer.optimizer_g, self.trainer.optimizer_d, self.trainer.lr_scheduler_g, self.trainer.lr_scheduler_d = self.get_optimizer(
                    self.minibatch_default * self.default_lr / minibatch_size)
            self.data_loader = self.create_dataloader_fun(minibatch_size)
            self.trainer.dataiter = iter(self.data_loader)
            self.trainer.random_latents_generator = self.create_rlg(minibatch_size)
            tick_duration_kimg = self.tick_kimg_override.get(depth - self.depth_offset, self.tick_kimg_default)
            self.trainer.tick_duration_nimg = int(tick_duration_kimg * 1000)
            self.trainer.stats['minibatch_size'] = minibatch_size
        if alpha != self.alpha:
            self.trainer.discriminator.alpha = self.trainer.generator.alpha = dataset.alpha = alpha
            self.alpha = alpha
        self.trainer.stats['depth'] = depth
        self.trainer.stats['alpha']['val'] = alpha


class EfficientLossMonitor(LossMonitor):
    def __init__(self, loss_no, stat_name, monitor_threshold: float = 10.0,
                 monitor_warmup: int = 50, monitor_patience: int = 5):
        super().__init__()
        self.loss_no = loss_no
        self.stat_name = stat_name
        self.threshold = monitor_threshold
        self.warmup = monitor_warmup
        self.patience = monitor_patience
        self.counter = 0

    def _get_value(self, iteration, *args):
        val = args[self.loss_no].item()
        if val != val:
            raise ValueError('loss value is NaN :((')
        return val

    def epoch(self, idx):
        super().epoch(idx)
        if idx > self.warmup:
            loss_value = self.trainer.stats[self.stat_name]['epoch_mean']
            if abs(loss_value) > self.threshold:
                self.counter += 1
                if self.counter > self.patience:
                    raise ValueError('loss value exceeded the threshold')
            else:
                self.counter = 0


class AbsoluteTimeMonitor(Plugin):
    def __init__(self):
        super().__init__([(1, 'epoch')])
        self.start_time = time.time()
        self.epoch_start = self.start_time
        self.start_nimg = None
        self.epoch_time = 0

    def register(self, trainer):
        self.trainer = trainer
        self.start_nimg = trainer.cur_nimg
        self.trainer.stats['sec'] = {'log_format': ':.1f'}

    def epoch(self, epoch_index):
        cur_time = time.time()
        tick_time = cur_time - self.epoch_start
        self.epoch_start = cur_time
        kimg_time = tick_time / (self.trainer.cur_nimg - self.start_nimg) * 1000
        self.start_nimg = self.trainer.cur_nimg
        self.trainer.stats['time'] = timedelta(seconds=time.time() - self.start_time)
        self.trainer.stats['sec']['tick'] = tick_time
        self.trainer.stats['sec']['kimg'] = kimg_time


class SaverPlugin(Plugin):
    last_pattern = 'network-snapshot-{}-{}.dat'

    def __init__(self, checkpoints_path, keep_old_checkpoints: bool = True, network_snapshot_ticks: int = 50):
        super().__init__([(network_snapshot_ticks, 'epoch')])
        self.checkpoints_path = checkpoints_path
        self.keep_old_checkpoints = keep_old_checkpoints

    def register(self, trainer: Trainer):
        self.trainer = trainer

    def epoch(self, epoch_index):
        if not self.keep_old_checkpoints:
            self._clear(self.last_pattern.format('*', '*'))
        dest = os.path.join(self.checkpoints_path,
                            self.last_pattern.format('{}', '{:06}'.format(self.trainer.cur_nimg // 1000)))
        for model, optimizer, name in [(self.trainer.generator, self.trainer.optimizer_g, 'generator'),
                                       (self.trainer.discriminator, self.trainer.optimizer_d, 'discriminator')]:
            torch.save({'cur_nimg': self.trainer.cur_nimg, 'model': model.state_dict(),
                        'optimizer': optimizer.state_dict()}, dest.format(name))

    def end(self, *args):
        self.epoch(*args)

    def _clear(self, pattern):
        pattern = os.path.join(self.checkpoints_path, pattern)
        for file_name in glob(pattern):
            os.remove(file_name)


class EvalDiscriminator(Plugin):
    def __init__(self, create_dataloader_fun, output_snapshot_ticks):
        super().__init__([(1, 'epoch')])
        self.create_dataloader_fun = create_dataloader_fun
        self.output_snapshot_ticks = output_snapshot_ticks

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['memorization'] = {
            'log_name': 'memorization',
            'log_epoch_fields': ['{val:.2f}', '{epoch:.2f}'],
            'val': float('nan'), 'epoch': 0,
        }

    def epoch(self, epoch_index):
        if epoch_index % self.output_snapshot_ticks != 0:
            return
        values = []
        with torch.no_grad():
            i = 0
            for data in self.create_dataloader_fun(min(self.trainer.stats['minibatch_size'], 1024), False,
                                                   self.trainer.dataset.model_depth, self.trainer.dataset.alpha):
                d_real, _, _ = self.trainer.discriminator(cudize(data))
                values.append(d_real.mean().item())
                i += 1
        values = np.array(values).mean()
        self.trainer.stats['memorization']['val'] = values
        self.trainer.stats['memorization']['epoch'] = epoch_index


# TODO
class OutputGenerator(Plugin):

    def __init__(self, sample_fn, checkpoints_dir: str, seq_len: int, max_freq: float,
                 samples_count: int = 8, output_snapshot_ticks: int = 25, old_weight: float = 0.59):
        super().__init__([(1, 'epoch')])
        self.old_weight = old_weight
        self.sample_fn = sample_fn
        self.samples_count = samples_count
        self.checkpoints_dir = checkpoints_dir
        self.seq_len = seq_len
        self.max_freq = max_freq
        self.my_g_clone = None
        self.output_snapshot_ticks = output_snapshot_ticks

    @staticmethod
    def truncated_z_sample(batch_size, z_dim, truncation=0.5):
        values = truncnorm.rvs(-2, 2, size=(batch_size, z_dim))
        return truncation * values

    @staticmethod
    def flatten_params(model):
        return deepcopy(list(p.data for p in model.parameters()))

    @staticmethod
    def load_params(flattened, model):
        for p, avg_p in zip(model.parameters(), flattened):
            p.data.copy_(avg_p)

    def register(self, trainer):
        self.trainer = trainer
        self.my_g_clone = self.flatten_params(self.trainer.generator)

    @staticmethod
    def running_mean(x, n=8):
        return pd.Series(x).rolling(window=n).mean().values

    @staticmethod
    def get_images(frequency, epoch, generated, my_range=range):
        num_channels = generated.shape[1]
        seq_len = generated.shape[2]
        t = np.linspace(0, seq_len / frequency, seq_len)
        f = np.fft.rfftfreq(seq_len, d=1. / frequency)
        images = []
        for index in my_range(len(generated)):
            fig, (axs) = plt.subplots(num_channels, 4)
            if num_channels == 1:
                axs = axs.reshape(1, -1)
            fig.set_figheight(40)
            fig.set_figwidth(40)
            for ch in range(num_channels):
                data = generated[index, ch, :]
                axs[ch][0].plot(t, data, color=(0.8, 0, 0, 0.5), label='time domain')
                axs[ch][1].plot(f, np.abs(np.fft.rfft(data)), color=(0.8, 0, 0, 0.5), label='freq domain')
                axs[ch][2].plot(f, OutputGenerator.running_mean(np.abs(np.fft.rfft(data))),
                                color=(0.8, 0, 0, 0.5), label='freq domain(smooth)')
                axs[ch][3].semilogy(f, np.abs(np.fft.rfft(data)), color=(0.8, 0, 0, 0.5), label='freq domain(log)')
                axs[ch][0].set_ylim([-1.1, 1.1])
                axs[ch][0].legend()
                axs[ch][1].legend()
                axs[ch][2].legend()
                axs[ch][3].legend()
            fig.suptitle('epoch: {}, sample: {}'.format(epoch, index))
            fig.canvas.draw()
            image = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
            image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
            images.append(image)
            plt.close(fig)
        return images

    def epoch(self, epoch_index):
        for p, avg_p in zip(self.trainer.generator.parameters(), self.my_g_clone):
            avg_p.mul_(self.old_weight).add_((1.0 - self.old_weight) * p.data)
        if epoch_index % self.output_snapshot_ticks == 0:
            z = next(self.sample_fn(self.samples_count))
            gen_input = cudize(z)
            original_param = self.flatten_params(self.trainer.generator)
            self.load_params(self.my_g_clone, self.trainer.generator)
            dest = os.path.join(self.checkpoints_dir, SaverPlugin.last_pattern.format('smooth_generator',
                                                                                      '{:06}'.format(
                                                                                          self.trainer.cur_nimg // 1000)))
            torch.save({'cur_nimg': self.trainer.cur_nimg, 'model': self.trainer.generator.state_dict()}, dest)
            out = generate_samples(self.trainer.generator, gen_input)
            self.load_params(original_param, self.trainer.generator)
            frequency = self.max_freq * out.shape[2] / self.seq_len
            images = self.get_images(frequency, epoch_index, out)
            for i, image in enumerate(images):
                imwrite(os.path.join(self.checkpoints_dir, '{}_{}.png'.format(epoch_index, i)), image)


class TeeLogger(Logger):

    def __init__(self, log_file, exp_name, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_file = open(log_file, 'a', 1)
        self.exp_name = exp_name

    def log(self, msg):
        print(self.exp_name, msg, flush=True)
        self.log_file.write(msg + '\n')

    def epoch(self, epoch_idx):
        self._log_all('log_epoch_fields')


class WatchSingularValues(Plugin):
    def __init__(self, network, one_divided_two: float = 10.0, output_snapshot_ticks: int = 20):
        super().__init__([(1, 'epoch')])
        self.network = network
        self.one_divided_two = one_divided_two
        self.output_snapshot_ticks = output_snapshot_ticks

    def register(self, trainer):
        self.trainer = trainer

    def epoch(self, epoch_index):
        if epoch_index % self.output_snapshot_ticks != 0:
            return
        for module in self.network.modules:
            if isinstance(module, torch.nn.Conv1d):
                weight = module.weight.data.cpu().numpy()
                _, s, _ = randomized_svd(weight.reshape(weight.shape[0], -1), n_components=2)
                if abs(s[0] / s[1]) > self.one_divided_two:
                    raise ValueError(module)


class SlicedWDistance(Plugin):
    def __init__(self, progression_scale: int, output_snapshot_ticks: int, patches_per_item: int = 16,
                 patch_size: int = 49, max_items: int = 1024, number_of_projections: int = 512,
                 dir_repeats: int = 4, dirs_per_repeat: int = 128):
        super().__init__([(1, 'epoch')])
        self.output_snapshot_ticks = output_snapshot_ticks
        self.progression_scale = progression_scale
        self.patches_per_item = patches_per_item
        self.patch_size = patch_size
        self.max_items = max_items
        self.number_of_projections = number_of_projections
        self.dir_repeats = dir_repeats
        self.dirs_per_repeat = dirs_per_repeat

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['swd'] = {
            'log_name': 'swd',
            'log_epoch_fields': ['{val:.2f}', '{epoch:.2f}'],
            'val': float('nan'), 'epoch': 0,
        }

    def sliced_wasserstein(self, A, B):
        results = []
        for repeat in range(self.dir_repeats):
            dirs = torch.randn(A.shape[1], self.dirs_per_repeat)  # (descriptor_component, direction)
            dirs /= torch.sqrt(
                (dirs * dirs).sum(dim=0, keepdim=True) + EPSILON)  # normalize descriptor components for each direction
            projA = torch.matmul(A, dirs)  # (neighborhood, direction)
            projB = torch.matmul(B, dirs)
            projA = torch.sort(projA, dim=0)[0]  # sort neighborhood projections for each direction
            projB = torch.sort(projB, dim=0)[0]
            dists = (projA - projB).abs()  # pointwise wasserstein distances
            results.append(dists.mean())  # average over neighborhoods and directions
        return torch.mean(torch.stack(results)).item()  # average over repeats

    def epoch(self, epoch_index):
        if epoch_index % self.output_snapshot_ticks != 0:
            return
        gc.collect()
        all_fakes = []
        all_reals = []
        with torch.no_grad():
            remaining_items = self.max_items
            while remaining_items > 0:
                z = next(self.trainer.random_latents_generator)
                fake_latents_in = cudize(z)
                all_fakes.append(self.trainer.generator(fake_latents_in)[0]['x'].data.cpu())
                if all_fakes[-1].size(2) < self.patch_size:
                    break
                remaining_items -= all_fakes[-1].size(0)
            all_fakes = torch.cat(all_fakes, dim=0)
            remaining_items = self.max_items
            while remaining_items > 0:
                all_reals.append(next(self.trainer.dataiter)['x'])
                if all_reals[-1].size(2) < self.patch_size:
                    break
                remaining_items -= all_reals[-1].size(0)
            all_reals = torch.cat(all_reals, dim=0)
        swd = self.get_descriptors(all_fakes, all_reals)
        if len(swd) > 0:
            swd.append(np.array(swd).mean())
        self.trainer.stats['swd']['val'] = swd
        self.trainer.stats['swd']['epoch'] = epoch_index

    def get_descriptors(self, batch1, batch2):
        b, c, t_max = batch1.shape
        t = t_max
        num_levels = 0
        while t >= self.patch_size:
            num_levels += 1
            t //= self.progression_scale
        swd = []
        for level in range(num_levels):
            both_descriptors = [None, None]
            batchs = [batch1, batch2]
            for i in range(2):
                descriptors = []
                max_index = batchs[i].shape[2] - self.patch_size
                for j in range(b):
                    for k in range(self.patches_per_item):
                        rand_index = np.random.randint(0, max_index)
                        descriptors.append(batchs[i][j, :, rand_index:rand_index + self.patch_size])
                descriptors = torch.stack(descriptors, dim=0)  # N, c, patch_size
                descriptors = descriptors.reshape((-1, c))
                descriptors -= torch.mean(descriptors, dim=0, keepdim=True)
                descriptors /= torch.std(descriptors, dim=0, keepdim=True) + EPSILON
                both_descriptors[i] = descriptors
                batchs[i] = batchs[i][:, :, ::self.progression_scale]
            swd.append(self.sliced_wasserstein(both_descriptors[0], both_descriptors[1]))
        return swd


class FidCalculator(Plugin):
    def __init__(self, num_channels, create_dataloader_fun, target_seq_len, num_samples=1024 * 16,
                 output_snapshot_ticks=25, calc_for_z=True, calc_for_zp=True, calc_for_c=True, calc_for_cp=True):
        super().__init__([(1, 'epoch')])
        self.create_dataloader_fun = create_dataloader_fun
        self.output_snapshot_ticks = output_snapshot_ticks
        self.last_depth = -1
        self.last_alpha = -1
        self.target_seq_len = target_seq_len
        self.calc_z = calc_for_z
        self.calc_zp = calc_for_zp
        self.calc_c = calc_for_c
        self.calc_cp = calc_for_cp
        self.num_samples = num_samples
        hp = cpc_hp
        self.network = cudize(CpcNetwork(num_channels, generate_long_sequence=hp.generate_long_sequence,
                                         pooling=hp.pool_or_stride == 'pool', encoder_dropout=hp.encoder_dropout,
                                         use_sinc_encoder=hp.use_sinc_encoder, use_shared_sinc=hp.use_shared_sinc,
                                         bidirectional=hp.bidirectional,
                                         contextualizer_num_layers=hp.contextualizer_num_layers,
                                         contextualizer_dropout=hp.contextualizer_dropout,
                                         use_transformer=hp.use_transformer,
                                         causal_prediction=hp.causal_prediction, prediction_k=hp.prediction_k,
                                         encoder_activation=hp.encoder_activation, tiny_encoder=hp.tiny_encoder)).eval()

    def register(self, trainer):
        self.trainer = trainer
        fields = []
        dd = {'epoch': 0}
        if self.calc_c:
            fields.append('{c_fake:.2f}')
            dd['c_fake'] = float('nan')
        if self.calc_z:
            fields.append('{z_fake:.2f}')
            dd['z_fake'] = float('nan')
        if self.calc_cp:
            fields.append('{cp_fake:.2f}')
            dd['cp_fake'] = float('nan')
        if self.calc_zp:
            fields.append('{zp_fake:.2f}')
            dd['zp_fake'] = float('nan')
        fields.append('{epoch:.2f}')

        self.trainer.stats['FID'] = {
            'log_name': 'FID',
            'log_epoch_fields': fields,
            **dd
        }

    @staticmethod
    def torch_cov(m, rowvar=False):
        if m.dim() > 2:
            raise ValueError('m has more than 2 dimensions')
        if m.dim() < 2:
            m = m.view(1, -1)
        if not rowvar and m.size(0) != 1:
            m = m.t()
        fact = 1.0 / (m.size(1) - 1)
        m -= torch.mean(m, dim=1, keepdim=True)
        mt = m.t()
        return fact * m.matmul(mt).squeeze()

    @staticmethod
    def numpy_calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
        mu1 = np.atleast_1d(mu1)
        mu2 = np.atleast_1d(mu2)
        sigma1 = np.atleast_2d(sigma1)
        sigma2 = np.atleast_2d(sigma2)
        assert mu1.shape == mu2.shape, 'Training and test mean vectors have different lengths'
        assert sigma1.shape == sigma2.shape, 'Training and test covariances have different dimensions'
        diff = mu1 - mu2
        covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
        if not np.isfinite(covmean).all():
            print('fid calculation produces singular product; adding %s to diagonal of cov estimates' % eps)
            offset = np.eye(sigma1.shape[0]) * eps
            covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))
        if np.iscomplexobj(covmean):
            if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-1):
                m = np.max(np.abs(covmean.imag))
                raise ValueError('Imaginary component {}'.format(m))
            covmean = covmean.real
        tr_covmean = np.trace(covmean)
        out = diff.dot(diff) + np.trace(sigma1) + np.trace(sigma2) - 2 * tr_covmean
        return out

    @staticmethod
    def sqrt_newton_schulz(A, numIters, dtype=None):
        with torch.no_grad():
            if dtype is None:
                dtype = A.type()
            batchSize = A.shape[0]
            dim = A.shape[1]
            normA = A.mul(A).sum(dim=1).sum(dim=1).sqrt()
            Y = A.div(normA.view(batchSize, 1, 1).expand_as(A))
            I = torch.eye(dim, dim).view(1, dim, dim).repeat(batchSize, 1, 1).type(dtype)
            Z = torch.eye(dim, dim).view(1, dim, dim).repeat(batchSize, 1, 1).type(dtype)
            for i in range(numIters):
                T = 0.5 * (3.0 * I - Z.bmm(Y))
                Y = Y.bmm(T)
                Z = T.bmm(Z)
            sA = Y * torch.sqrt(normA).view(batchSize, 1, 1).expand_as(A)
        return sA

    @staticmethod
    def calc_fid(mu1, std1, mu2, std2):
        FID = FidCalculator.torch_calculate_frechet_distance(mu1, std1, mu2, std2).item()
        if FID != FID:
            FID = FidCalculator.numpy_calculate_frechet_distance(mu1.numpy(), std1.numpy(),
                                                                 mu2.numpy(), std2.numpy())
        return FID

    @staticmethod
    def torch_calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
        assert mu1.shape == mu2.shape, 'Training and test mean vectors have different lengths'
        assert sigma1.shape == sigma2.shape, 'Training and test covariances have different dimensions'
        diff = mu1 - mu2
        covmean = FidCalculator.sqrt_newton_schulz(sigma1.mm(sigma2).unsqueeze(0), 50).squeeze()
        out = (diff.dot(diff) + torch.trace(sigma1) + torch.trace(sigma2) - 2 * torch.trace(covmean))
        return out

    def epoch(self, epoch_index):
        if epoch_index % self.output_snapshot_ticks != 0:
            return
        if not (self.last_depth == self.trainer.dataset.model_depth and self.last_alpha == self.trainer.dataset.alpha):
            with torch.no_grad():
                all_z = []
                all_c = []
                all_zp = []
                all_cp = []
                i = 0
                for data in self.create_dataloader_fun(min(self.trainer.stats['minibatch_size'], 1024), False,
                                                       self.trainer.dataset.model_depth, self.trainer.dataset.alpha):
                    x = cudize(data['x'])
                    x = resample_signal(x, x.size(2), self.target_seq_len, True)
                    batch_size = x.size(0)
                    z, c, zp, cp = self.network.inference_forward(x)
                    if self.calc_z:
                        all_z.append(z.view(-1, z.size(1)).cpu())
                    if self.calc_c:
                        all_c.append(c.contiguous().view(-1, c.size(1)).cpu())
                    if self.calc_zp:
                        all_zp.append(zp.cpu())
                    if self.calc_cp:
                        all_cp.append(cp.cpu())
                    i += batch_size
                    if i >= self.num_samples:
                        break
                if self.calc_z:
                    all_z = torch.cat(all_z, dim=0)
                    self.z_mu, self.z_std = torch.mean(all_z, 0), self.torch_cov(all_z, rowvar=False)
                if self.calc_c:
                    all_c = torch.cat(all_c, dim=0)
                    self.c_mu, self.c_std = torch.mean(all_c, 0), self.torch_cov(all_c, rowvar=False)
                if self.calc_zp:
                    all_zp = torch.cat(all_zp, dim=0)
                    self.zp_mu, self.zp_std = torch.mean(all_zp, 0), self.torch_cov(all_zp, rowvar=False)
                if self.calc_c:
                    all_cp = torch.cat(all_cp, dim=0)
                    self.cp_mu, self.cp_std = torch.mean(all_cp, 0), self.torch_cov(all_cp, rowvar=False)
        with torch.no_grad():
            i = 0
            all_z = []
            all_c = []
            all_zp = []
            all_cp = []
            while i < self.num_samples:
                fake_latents_in = cudize(next(self.trainer.random_latents_generator))
                x = self.trainer.generator(fake_latents_in)[0]['x']
                x = resample_signal(x, x.size(2), self.target_seq_len, True)
                z, c, zp, cp = self.network.inference_forward(x)
                if self.calc_z:
                    all_z.append(z.view(-1, z.size(1)).cpu())
                if self.calc_c:
                    all_c.append(c.view(-1, z.size(1)).cpu())
                if self.calc_zp:
                    all_zp.append(zp.cpu())
                if self.calc_cp:
                    all_cp.append(cp.cpu())
            if self.calc_z:
                all_z = torch.cat(all_z, dim=0)
                fz_mu, fz_std = torch.mean(all_z, 0), self.torch_cov(all_z, rowvar=False)
                self.trainer.stats['FID']['z_fake'] = self.calc_fid(fz_mu, fz_std, self.z_mu, self.z_std)
            if self.calc_c:
                all_c = torch.cat(all_c, dim=0)
                fc_mu, fc_std = torch.mean(all_c, 0), self.torch_cov(all_c, rowvar=False)
                self.trainer.stats['FID']['c_fake'] = self.calc_fid(fc_mu, fc_std, self.c_mu, self.c_std)
            if self.calc_zp:
                all_zp = torch.cat(all_zp, dim=0)
                fzp_mu, fzp_std = torch.mean(all_zp, 0), self.torch_cov(all_zp, rowvar=False)
                self.trainer.stats['FID']['zp_fake'] = self.calc_fid(fzp_mu, fzp_std, self.zp_mu, self.zp_std)
            if self.calc_c:
                all_cp = torch.cat(all_cp, dim=0)
                fcp_mu, fcp_std = torch.mean(all_cp, 0), self.torch_cov(all_cp, rowvar=False)
                self.trainer.stats['FID']['cp_fake'] = self.calc_fid(fcp_mu, fcp_std, self.cp_mu, self.cp_std)
            self.trainer.stats['FID']['epoch'] = epoch_index


class NDBScore(Plugin):
    def __init__(self, create_dataloader_fun, output_dir, output_snapshot_ticks=25,
                 number_bins=100, num_samples=1024 * 32):
        super().__init__([(1, 'epoch')])
        self.output_dir = output_dir
        self.create_dataloader_fun = create_dataloader_fun
        self.output_snapshot_ticks = output_snapshot_ticks
        self.num_samples = num_samples
        self.last_stage = -1
        self.num_bins = number_bins

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['ndb'] = {
            'log_name': 'ndb',
            'log_epoch_fields': ['{ndb:.2f}', '{js:.2f}', '{epoch:.2f}'],
            'ndb': float('nan'), 'epoch': 0, 'js': float('nan')
        }

    def epoch(self, epoch_index):
        if epoch_index % self.output_snapshot_ticks != 0:
            return
        if self.last_stage != (self.trainer.dataset.model_depth + self.trainer.dataset.alpha):
            self.last_stage = self.trainer.dataset.model_depth + self.trainer.dataset.alpha
            values = []
            i = 0
            for data in self.create_dataloader_fun(min(self.trainer.stats['minibatch_size'], 1024), False,
                                                   self.trainer.dataset.model_depth, self.trainer.dataset.alpha):
                x = data['x']
                x = x.view(x.size(0), -1).numpy()
                values.append(x)
                i += x.shape[0]
                if i >= self.num_samples:
                    break
            values = np.stack(values)
            self.ndb = NDB(values, self.num_bins, cache_folder=self.output_dir, stage=self.last_stage)
        with torch.no_grad():
            values = []
            i = 0
            while i < self.num_samples:
                fake_latents_in = cudize(next(self.trainer.random_latents_generator))
                x = self.trainer.generator(fake_latents_in)[0]['x'].cpu()
                i += x.size(0)
                x = x.view(x.size(0), -1).numpy()
                values.append(x)
        values = np.stack(values)
        result = self.ndb.evaluate(values)
        self.trainer.stats['ndb']['ndb'] = result[0]
        self.trainer.stats['ndb']['js'] = result[1]
        self.trainer.stats['ndb']['epoch'] = epoch_index
