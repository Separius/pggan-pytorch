import os
import gc
import time
import torch
import matplotlib
import numpy as np
import pandas as pd
from glob import glob
from scipy import misc
from scipy import linalg
from copy import deepcopy
from trainer import Trainer
from datetime import timedelta
from scipy.stats import entropy
import torch.nn.functional as F
from inception_net import ChronoNet
from sklearn.utils.extmath import randomized_svd
from torch_utils import Plugin, LossMonitor, Logger
from utils import generate_samples, cudize, EPSILON, load_pkl, save_pkl

matplotlib.use('Agg')
import matplotlib.pyplot as plt


class DepthManager(Plugin):
    # minibatch_override_big = {0: 8192, 1: 4096, 2: 2048 + 1024, 3: 2048, 4: 1024 + 512, 5: 1024, 6: 256, 7: 128 + 64}
    # tick_kimg_override_big = {0: 5, 1: 5, 2: 5, 3: 4, 4: 4, 5: 3, 6: 3, 7: 2}
    # training_kimg_override_big = {0: 200, 1: 200, 2: 200, 3: 300, 4: 400, 5: 400, 6: 400, 7: 400}
    # transition_kimg_override_big = {0: 200, 1: 200, 2: 200, 3: 300, 4: 400, 5: 400, 6: 400, 7: 400}

    minibatch_override_tiny = {}
    tick_kimg_override_tiny = {}
    training_kimg_override_tiny = {}
    transition_kimg_override_tiny = {}

    minibatch_override_big = {4: 128, 5: 128, 6: 128, 7: 64, 8: 64, 9: 32, 10: 32, 11: 16, 12: 16}
    tick_kimg_override_big = {4: 4, 5: 4, 6: 4, 7: 3, 8: 3, 9: 2, 10: 2, 11: 1, 12: 1}
    training_kimg_override_big = {1: 200, 2: 200, 3: 200, 4: 200}
    transition_kimg_override_big = {1: 200, 2: 200, 3: 200, 4: 200}

    def __init__(self,  # everything starts from 0 or 1
                 create_dataloader_fun, create_rlg, max_depth,
                 tick_kimg_default, has_attention, get_optimizer, default_lr,
                 tiny_sizes: bool = False, reset_optimizer: bool = True, disable_progression=False,
                 minibatch_default=256, depth_offset=0,  # starts form 0
                 attention_transition_kimg=400, lod_training_kimg=400, lod_transition_kimg=400):
        super().__init__([(1, 'iteration')])
        if tiny_sizes:
            self.minibatch_override = self.minibatch_override_tiny
            self.tick_kimg_override = self.tick_kimg_override_tiny
            self.training_kimg_override = self.training_kimg_override_tiny
            self.transition_kimg_override = self.transition_kimg_override_tiny
        else:
            self.minibatch_override = self.minibatch_override_big
            self.tick_kimg_override = self.tick_kimg_override_big
            self.training_kimg_override = self.training_kimg_override_big
            self.transition_kimg_override = self.transition_kimg_override_big
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
        self.attention_transition_kimg = attention_transition_kimg
        self.alpha_map, self.start_gamma, self.end_gamma = self.pre_compute_alpha_map(self.depth_offset, max_depth,
                                                                                      lod_training_kimg,
                                                                                      self.training_kimg_override,
                                                                                      lod_transition_kimg,
                                                                                      self.transition_kimg_override,
                                                                                      has_attention,
                                                                                      attention_transition_kimg)

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['minibatch_size'] = self.minibatch_default
        self.trainer.stats['alpha'] = {'log_name': 'alpha', 'log_epoch_fields': ['{val:.2f}'], 'val': self.alpha}
        if self.start_gamma is not None:
            self.trainer.stats['gamma'] = {'log_name': 'gamma', 'log_epoch_fields': ['{val:.2f}'], 'val': 0}
        self.iteration(is_resuming=self.trainer.optimizer_d is not None)

    @staticmethod
    def pre_compute_alpha_map(start_depth, max_depth, lod_training_kimg, lod_training_kimg_overrides,
                              lod_transition_kimg, lod_transition_kimg_overrides, has_attention,
                              attention_transition_kimg):
        start_gamma = None
        end_gamma = None
        points = []
        pointer = 0
        for i in range(start_depth, max_depth):
            pointer += int(lod_training_kimg_overrides.get(i + 1, lod_training_kimg) * 1000)
            points.append(pointer)
            if (i == max_depth - 1) and has_attention:
                start_gamma = pointer
                end_gamma = pointer + int(attention_transition_kimg * 1000)
            pointer += int(lod_transition_kimg_overrides.get(i + 1, lod_transition_kimg) * 1000)
            points.append(pointer)
        return points, start_gamma, end_gamma

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
        if self.start_gamma is not None:
            cur_kimg = self.trainer.cur_nimg
            if self.disable_progression:
                gamma = cur_kimg / self.attention_transition_kimg
            else:
                gamma = (cur_kimg - self.start_gamma) / (self.end_gamma - self.start_gamma)
            gamma = min(1, max(0, gamma))
            self.trainer.discriminator.set_gamma(gamma)
            self.trainer.generator.set_gamma(gamma)
            self.trainer.stats['gamma']['val'] = gamma


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
    def __init__(self, create_dataloader_fun, output_snapshot_ticks, is_tiny):
        super().__init__([(1, 'epoch')])
        self.create_dataloader_fun = create_dataloader_fun
        self.output_snapshot_ticks = output_snapshot_ticks
        self.is_tiny = is_tiny

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
                if i == 3 and self.is_tiny:
                    break
        values = np.array(values).mean()
        self.trainer.stats['memorization']['val'] = values
        self.trainer.stats['memorization']['epoch'] = epoch_index


class OutputGenerator(Plugin):

    def __init__(self, sample_fn, checkpoints_dir: str, seq_len: int, max_freq, res_len: int,
                 samples_count: int = 8, output_snapshot_ticks: int = 25, old_weight: float = 0.9):
        super().__init__([(1, 'epoch')])
        self.old_weight = old_weight
        self.sample_fn = sample_fn
        self.samples_count = samples_count
        self.res_len = res_len
        self.checkpoints_dir = checkpoints_dir
        self.seq_len = seq_len
        self.max_freq = max_freq
        self.my_g_clone = None
        self.output_snapshot_ticks = output_snapshot_ticks

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
    def get_images(seq_len, frequency, epoch, generated, my_range=range):
        num_channels = generated.shape[1]
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
            image = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
            image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
            images.append(image)
            plt.close(fig)
        return images

    def epoch(self, epoch_index):
        for p, avg_p in zip(self.trainer.generator.parameters(), self.my_g_clone):
            avg_p.mul_(self.old_weight).add_((1.0 - self.old_weight) * p.data)
        if epoch_index % self.output_snapshot_ticks == 0:
            z = self.sample_fn(self.samples_count)
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
            res_len = min(self.res_len, out.shape[2])
            images = self.get_images(res_len, frequency, epoch_index, out[:, :, :res_len])
            for i, image in enumerate(images):
                misc.imsave(os.path.join(self.checkpoints_dir, '{}_{}.png'.format(epoch_index, i)), image)


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
                z = self.trainer.random_latents_generator()
                fake_latents_in = cudize(z)
                all_fakes.append(self.trainer.generator(fake_latents_in)[0].data.cpu())
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


class InceptionScore(Plugin):
    def __init__(self, inception_network, num_samples: int = 16384):
        super().__init__([(1, 'end')])
        self.inception_model = inception_network
        self.num_samples = num_samples

    def end(self, *args):
        inception_score, inception_std = self.inception_score()
        self.trainer.inception_result = (inception_score, inception_std)

    def inception_score(self, splits=4):
        preds = np.zeros((self.num_samples, self.inception_model.num_classes))
        with torch.no_grad():
            current_start = 0
            while current_start < self.num_samples:
                fake_latents_in = cudize(self.trainer.random_latents_generator())
                g_, _ = self.trainer.generator(fake_latents_in)
                y, _ = self.inception_model(g_)
                if current_start + y.size(0) > self.num_samples:
                    y = y[:self.num_samples - current_start]
                preds[current_start:current_start + y.size(0)] = F.softmax(y, dim=1).data.cpu().numpy()
                current_start += y.size(0)
        split_scores = []
        for k in range(splits):
            part = preds[k * (self.num_samples // splits): (k + 1) * (self.num_samples // splits), :]
            py = np.mean(part, axis=0)
            scores = []
            for i in range(part.shape[0]):
                pyx = part[i, :]
                scores.append(entropy(pyx, py))
            split_scores.append(np.exp(np.mean(scores)))
        return np.mean(split_scores), np.std(split_scores)

    def register(self, trainer):
        self.trainer = trainer


class FID(Plugin):
    def __init__(self, inception_network: ChronoNet, real_stats_location='./data/fid.pkl', num_samples=65536):
        super().__init__([(1, 'end')])
        self.inception_network = inception_network
        self.num_samples = num_samples
        self.real_stats_location = real_stats_location
        real_stats = load_pkl(real_stats_location)
        if real_stats is not None:
            self.real_mu, self.real_sigma = real_stats
        else:
            self.real_mu, self.real_sigma = None, None

    @staticmethod
    def calc_mu_sigma(tensor):
        return tensor.mean(dim=0).squeeze().data.cpu().numpy(), np.cov(tensor.data.cpu().numpy(), rowvar=False)

    def end(self, *args):
        with torch.no_grad():
            if self.real_mu is None:
                remaining_items = self.num_samples
                reals = []
                while remaining_items > 0:
                    reals.append(self.inception_network(cudize(next(self.trainer.dataiter)['x'][:remaining_items]))[
                                     1].data.cpu())
                    remaining_items -= reals[-1].size(0)
                reals = torch.cat(reals, dim=0)
                self.real_mu, self.real_sigma = self.calc_mu_sigma(reals)
                save_pkl(self.real_stats_location, (self.real_mu, self.real_sigma))
            remaining_items = self.num_samples
            all_fakes = []
            while remaining_items > 0:
                fake_latents_in = cudize(
                    {k: v[:remaining_items] for k, v in self.trainer.random_latents_generator().items()})
                all_fakes.append(self.inception_network(self.trainer.generator(fake_latents_in)[0])[1].data.cpu())
                remaining_items -= all_fakes[-1].size(0)
            all_fakes = torch.cat(all_fakes, dim=0)
            fake_mu, fake_sigma = self.calc_mu_sigma(all_fakes)
        self.trainer.fid_result = self.calculate_frechet_distance(fake_mu, fake_sigma, self.real_mu, self.real_sigma)

    def register(self, trainer):
        self.trainer = trainer

    @staticmethod
    def calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
        """Numpy implementation of the Frechet Distance.
        The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
        and X_2 ~ N(mu_2, C_2) is
                d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).

        Stable version by Dougal J. Sutherland.
        Params:
        -- mu1 : Numpy array containing the activations of the pool_3 layer of the
                 inception net ( like returned by the function 'get_predictions')
                 for generated samples.
        -- mu2   : The sample mean over activations of the pool_3 layer, precalcualted
                   on an representive data set.
        -- sigma1: The covariance matrix over activations of the pool_3 layer for
                   generated samples.
        -- sigma2: The covariance matrix over activations of the pool_3 layer,
                   precalcualted on an representive data set.
        Returns:
        --   : The Frechet Distance.
        """

        mu1 = np.atleast_1d(mu1)
        mu2 = np.atleast_1d(mu2)

        sigma1 = np.atleast_2d(sigma1)
        sigma2 = np.atleast_2d(sigma2)

        assert mu1.shape == mu2.shape, "Training and test mean vectors have different lengths"
        assert sigma1.shape == sigma2.shape, "Training and test covariances have different dimensions"

        diff = mu1 - mu2

        # product might be almost singular
        covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
        if not np.isfinite(covmean).all():
            print("fid calculation produces singular product; adding %s to diagonal of cov estimates" % eps)
            offset = np.eye(sigma1.shape[0]) * eps
            covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))

        # numerical error might give slight imaginary component
        if np.iscomplexobj(covmean):
            if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
                m = np.max(np.abs(covmean.imag))
                raise ValueError("Imaginary component {}".format(m))
            covmean = covmean.real

        tr_covmean = np.trace(covmean)

        return diff.dot(diff) + np.trace(sigma1) + np.trace(sigma2) - 2 * tr_covmean
