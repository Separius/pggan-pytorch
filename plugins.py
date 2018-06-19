import os
import time
from datetime import timedelta
from glob import glob
import torch
from scipy.io import wavfile
import numpy as np
from sklearn.neighbors import BallTree
from torch.autograd import Variable
from torch.utils.trainer.plugins import LossMonitor, Logger
from torch.utils.trainer.plugins.plugin import Plugin
from utils import generate_samples, cudize, get_features, get_accuracy, ll
from scipy import misc
import matplotlib
import imageio

matplotlib.use('Agg')
import matplotlib.pyplot as plt


class DepthManager(Plugin):

    def __init__(self,
                 create_dataloader_fun,
                 create_rlg,
                 max_depth,
                 tick_kimg_default,
                 attention_start_depth=None,
                 attention_transition_kimg=200,
                 depth_offset=0,
                 minibatch_default=64,
                 minibatch_overrides={6: 32, 7: 32, 8: 16, 9: 16, 10: 8, 11: 8, 12: 4},  # starts from depth_offset+1
                 tick_kimg_overrides={6: 4, 7: 4, 8: 3, 9: 3, 10: 2, 11: 2, 12: 1},  # starts from depth_offset+1
                 lod_training_kimg=400,
                 lod_training_kimg_overrides={1: 250, 2: 250, 3: 300, 4: 350},  # starts from depth_offset+1
                 lod_transition_kimg=100,
                 lod_transition_kimg_overrides={1: 50, 2: 60, 3: 80, 4: 90}):  # starts from depth_offset+1
        super(DepthManager, self).__init__([(1, 'iteration')])
        self.minibatch_default = minibatch_default
        self.minibatch_overrides = minibatch_overrides
        self.tick_kimg_default = tick_kimg_default
        self.tick_kimg_overrides = tick_kimg_overrides
        self.create_dataloader_fun = create_dataloader_fun
        self.create_rlg = create_rlg
        self.trainer = None
        self.depth = -1
        self.alpha = -1
        self.depth_offset = depth_offset
        self.max_depth = max_depth
        self.alpha_map, (self.start_gamma, self.end_gamma) = self.pre_compute_alpha_map(depth_offset, max_depth,
                                                                                        lod_training_kimg,
                                                                                        lod_training_kimg_overrides,
                                                                                        lod_transition_kimg,
                                                                                        lod_transition_kimg_overrides,
                                                                                        attention_start_depth,
                                                                                        attention_transition_kimg)

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['minibatch_size'] = self.minibatch_default
        self.trainer.stats['alpha'] = {'log_name': 'alpha', 'log_epoch_fields': ['{val:.2f}'], 'val': self.alpha}
        if self.start_gamma is not None:
            self.trainer.stats['gamma'] = {'log_name': 'alpha', 'log_epoch_fields': ['{val:.2f}'], 'val': 0}
        self.iteration()

    @staticmethod
    def pre_compute_alpha_map(start_depth, max_depth, lod_training_kimg, lod_training_kimg_overrides,
                              lod_transition_kimg, lod_transition_kimg_overrides, attention_start_depth,
                              attention_transition_kimg):
        start_gamma = None
        end_gamma = None
        points = []
        pointer = 0
        for i in range(start_depth, max_depth):
            pointer += int(lod_training_kimg_overrides.get(i + 1, lod_training_kimg) * 1000)
            if i == attention_start_depth:
                start_gamma = pointer
                pointer += int(attention_transition_kimg * 1000)
                end_gamma = pointer
            points.append(pointer)
            pointer += int(lod_transition_kimg_overrides.get(i + 1, lod_transition_kimg) * 1000)
            points.append(pointer)
        return points, (start_gamma, end_gamma)

    def calc_progress(self):
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
        return depth, alpha

    def iteration(self, *args):
        depth, alpha = self.calc_progress()
        dataset = self.trainer.dataset
        if depth != self.depth:
            self.trainer.D.depth = self.trainer.G.depth = dataset.model_depth = depth
            self.depth = depth
            minibatch_size = self.minibatch_overrides.get(depth, self.minibatch_default)
            self.data_loader = self.create_dataloader_fun(minibatch_size)
            self.trainer.dataiter = iter(self.data_loader)
            self.trainer.random_latents_generator = self.create_rlg(minibatch_size)
            tick_duration_kimg = self.tick_kimg_overrides.get(depth, self.tick_kimg_default)
            self.trainer.tick_duration_nimg = int(tick_duration_kimg * 1000)
            self.trainer.stats['minibatch_size'] = minibatch_size
        if alpha != self.alpha:
            self.trainer.D.alpha = self.trainer.G.alpha = dataset.alpha = alpha
            self.alpha = alpha
        self.trainer.stats['depth'] = depth
        self.trainer.stats['alpha']['val'] = alpha
        if self.start_gamma is not None:
            cur_kimg = self.trainer.cur_nimg
            gamma = min(1, max(0, (cur_kimg - self.start_gamma) / (self.end_gamma - self.start_gamma)))
            self.trainer.D.set_gamma(gamma)
            self.trainer.G.set_gamma(gamma)
            self.trainer.stats['gamma']['val'] = gamma


class LRScheduler(Plugin):

    def __init__(self, lr_scheduler_d, lr_scheduler_g):
        super(LRScheduler, self).__init__([(1, 'iteration')])
        self.lrs_d = lr_scheduler_d
        self.lrs_g = lr_scheduler_g

    def register(self, trainer):
        self.trainer = trainer
        self.iteration()

    def iteration(self, *args):
        self.lrs_d.step(self.trainer.cur_nimg)
        self.lrs_g.step(self.trainer.cur_nimg)


class EfficientLossMonitor(LossMonitor):

    def __init__(self, loss_no, stat_name):
        super(EfficientLossMonitor, self).__init__()
        self.loss_no = loss_no
        self.stat_name = stat_name

    def _get_value(self, iteration, *args):
        val = args[self.loss_no]
        return val.item()


class AbsoluteTimeMonitor(Plugin):
    stat_name = 'time'

    def __init__(self, base_time=0):
        super(AbsoluteTimeMonitor, self).__init__([(1, 'epoch')])
        self.base_time = base_time
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
        self.trainer.stats['time'] = timedelta(seconds=time.time() - self.start_time + self.base_time)
        self.trainer.stats['sec']['tick'] = tick_time
        self.trainer.stats['sec']['kimg'] = kimg_time


class SaverPlugin(Plugin):
    last_pattern = 'network-snapshot-{}-{}.dat'

    def __init__(self, checkpoints_path, keep_old_checkpoints=True, network_snapshot_ticks=50, use_3way_test=False):
        super().__init__([(network_snapshot_ticks, 'epoch'), (1, 'end')])
        self.checkpoints_path = checkpoints_path
        self.keep_old_checkpoints = keep_old_checkpoints
        self._best_val_loss = float('+inf')  # TODO use the validator's loss for this
        # TODO use_3way_test here

    def register(self, trainer):
        self.trainer = trainer

    def epoch(self, epoch_index):
        if not self.keep_old_checkpoints:
            self._clear(self.last_pattern.format('*', '*'))
        for model, name in [(self.trainer.G, 'generator'), (self.trainer.D, 'discriminator')]:
            torch.save(
                model,
                os.path.join(
                    self.checkpoints_path,
                    self.last_pattern.format(name, '{:06}'.format(self.trainer.cur_nimg // 1000))
                )
            )

    def end(self, *args):
        self.epoch(*args)

    def _clear(self, pattern):
        pattern = os.path.join(self.checkpoints_path, pattern)
        for file_name in glob(pattern):
            os.remove(file_name)


class OutputGenerator(Plugin):

    def __init__(self, sample_fn, checkpoints_dir, seq_len, max_freq, res_len, is_audio, samples_count=8,
                 output_snapshot_ticks=10):
        super(OutputGenerator, self).__init__([(output_snapshot_ticks, 'epoch'), (1, 'end')])
        self.sample_fn = sample_fn
        self.samples_count = samples_count
        self.res_len = res_len
        self.checkpoints_dir = checkpoints_dir
        self.seq_len = seq_len
        self.max_freq = max_freq
        self.is_audio = is_audio

    def register(self, trainer):
        self.trainer = trainer

    def get_images(self, seq_len, frequency, epoch, generated):
        num_channels = generated.shape[1]
        t = np.linspace(0, seq_len / frequency, seq_len)
        f = np.fft.rfftfreq(seq_len, d=1. / frequency)
        images = []
        for index in range(len(generated)):
            fig, (axs) = plt.subplots(num_channels, 2)
            if num_channels == 1:
                axs = axs.reshape(1, -1)
            fig.set_figheight(20)
            fig.set_figwidth(20)
            for ch in range(num_channels):
                data = generated[index, ch, :]
                axs[ch][0].plot(t, data, color=(0.8, 0, 0, 0.5), label='time domain')
                axs[ch][1].semilogy(f, np.abs(np.fft.rfft(data)), color=(0.8, 0, 0, 0.5), label='freq domain')
                axs[ch][0].set_ylim([-1.1, 1.1])
                axs[ch][0].legend()
                axs[ch][1].legend()
            fig.suptitle('epoch: {}, sample: {}'.format(epoch, index))
            fig.canvas.draw()
            image = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
            image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
            images.append(image)
            plt.close(fig)
        return images

    def get_audios(self, generated):
        return [generated[i, 0, :] for i in range(len(generated))]

    def epoch(self, epoch_index):
        gen_input = cudize(Variable(self.sample_fn(self.samples_count)))
        out = generate_samples(self.trainer.G, gen_input)
        frequency = self.max_freq * out.shape[2] / self.seq_len
        res_len = min(self.res_len, out.shape[2])
        if self.is_audio:
            images = self.get_audios(out[:, :, :res_len])
        else:
            images = self.get_images(res_len, frequency, epoch_index, out[:, :, :res_len])
        for i, image in enumerate(images):
            if self.is_audio:
                wavfile.write(os.path.join(self.checkpoints_dir, '{}_{}.wav'.format(epoch_index, i)), frequency, image)
            else:
                misc.imsave(os.path.join(self.checkpoints_dir, '{}_{}.png'.format(epoch_index, i)), image)

    def end(self, *args):
        self.epoch(*args)


class FixedNoise(OutputGenerator):
    def __init__(self, *args, **kwargss):
        super(FixedNoise, self).__init__(*args, **kwargss)
        self.gen_input = cudize(Variable(self.sample_fn(self.samples_count)))

    def epoch(self, epoch_index):
        out = generate_samples(self.trainer.G, self.gen_input)
        frequency = self.max_freq * out.shape[2] / self.seq_len
        res_len = min(self.res_len, out.shape[2])
        if self.is_audio:
            images = self.get_audios(out[:, :, :res_len])
        else:
            images = self.get_images(res_len, frequency, epoch_index, out[:, :, :res_len])
        for i, image in enumerate(images):
            if self.is_audio:
                wavfile.write(os.path.join(self.checkpoints_dir, 'fixed_{}_{}.wav'.format(epoch_index, i)), frequency,
                              image)
            else:
                misc.imsave(os.path.join(self.checkpoints_dir, 'fixed_{}_{}.png'.format(epoch_index, i)), image)


class GifGenerator(OutputGenerator):

    def __init__(self, sample_fn, checkpoints_dir, seq_len, max_freq, output_snapshot_ticks, res_len, is_audio,
                 num_frames=30, fps=5):
        super(GifGenerator, self).__init__(sample_fn, checkpoints_dir, seq_len, max_freq, res_len, is_audio,
                                           samples_count=num_frames, output_snapshot_ticks=output_snapshot_ticks)
        self.fps = fps

    def epoch(self, epoch_index):
        gen_input = self.sample_fn(2).numpy()
        gen_input = self.slerp(np.arange(self.samples_count) / self.samples_count, gen_input[0], gen_input[1])
        gen_input = cudize(Variable(torch.from_numpy(gen_input.astype(np.float32))))
        out = generate_samples(self.trainer.G, gen_input)
        frequency = self.max_freq * out.shape[2] / self.seq_len
        res_len = min(self.res_len, out.shape[2])
        images = self.get_images(res_len, frequency, epoch_index, out[:, :, :res_len])
        imageio.mimsave(os.path.join(self.checkpoints_dir, '{}.gif'.format(epoch_index)), images, fps=self.fps)

    @staticmethod
    def slerp(val, low, high):
        omega = np.arccos(np.clip(np.dot(low / np.linalg.norm(low), high / np.linalg.norm(high)), -1, 1))
        so = np.sin(omega)
        if so == 0:
            return np.outer(1.0 - val, low) + np.outer(val, high)
        return np.outer(np.sin((1.0 - val) * omega) / so, low) + np.outer(np.sin(val * omega) / so, high)


class AggregationGraphValidator(Plugin):
    def __init__(self, sample_fn, valid_set, output_snapshot_ticks, checkpoints_dir, res_len, seq_len, max_freq):
        super(AggregationGraphValidator, self).__init__([(1, 'epoch'), (1, 'end')])
        self.sample_fn = sample_fn
        self.valid_set = valid_set
        self.real_bounds = None
        self.real_bounds_f = None
        self.last_depth = None
        self.output_snapshot_ticks = output_snapshot_ticks
        self.checkpoints_dir = checkpoints_dir
        self.res_len = res_len
        self.seq_len = seq_len
        self.max_freq = max_freq

    @staticmethod
    def get_bounds(a):
        return np.max(a, axis=0), np.mean(a, axis=0), np.min(a, axis=0)

    def update_real_bounds(self):
        if self.last_depth != self.trainer.D.depth:
            all_real = torch.cat([batch for batch in self.valid_set], dim=0).cpu().numpy()
            self.real_bounds = self.get_bounds(all_real)
            self.real_bounds_f = self.get_bounds(np.abs(np.fft.rfft(all_real)))
            self.last_depth = self.trainer.D.depth

    def register(self, trainer):
        self.trainer = trainer

    def end(self, *args):
        self.epoch(*args)

    def epoch(self, epoch):
        if len(self.valid_set) == 0:
            return
        if (epoch - 1) % self.output_snapshot_ticks != 0:
            return
        self.update_real_bounds()
        self.trainer.G.eval()
        fakes = []
        for batch in self.valid_set:
            x_fake = self.trainer.G(cudize(Variable(self.sample_fn(batch.shape[0])))).detach()
            fakes.append(x_fake)
        fakes = torch.cat(fakes, dim=0).data.cpu().numpy()
        fake_bounds = self.get_bounds(fakes)
        fake_bounds_f = self.get_bounds(np.abs(np.fft.rfft(fakes)))
        frequency = self.max_freq * x_fake.shape[2] / self.seq_len
        res_len = min(self.res_len, x_fake.shape[2])
        self.plot_bounds(self.real_bounds, fake_bounds, self.real_bounds_f, fake_bounds_f, res_len, frequency, epoch)

    def plot_bounds(self, real_bounds, fake_bounds, f_real_bounds, f_fake_bounds, seq_len, frequency, epoch):
        num_channels = real_bounds.shape[0]
        t = np.linspace(0, seq_len / frequency, seq_len)
        f = np.fft.rfftfreq(seq_len, d=1. / frequency)
        fig, (axs) = plt.subplots(num_channels, 2)
        if num_channels == 1:
            axs = axs.reshape(1, -1)
        fig.set_figheight(20)
        fig.set_figwidth(20)
        for ch in range(num_channels):
            axs[ch][0].plot(t, real_bounds[1][ch, :], lw=2, color=(0.8, 0, 0, 0.5), label='real_mean_t')
            axs[ch][0].fill_between(t, real_bounds[2][ch, :], real_bounds[1][ch, :], facecolor='yellow', alpha=0.5,
                                    label='real_bound_t')
            axs[ch][0].plot(t, fake_bounds[1][ch, :], lw=2, color=(0.2, 0, 0, 0.8), label='fake_mean_t')
            axs[ch][0].fill_between(t, real_bounds[2][ch, :], real_bounds[1][ch, :], facecolor='green', alpha=0.5,
                                    label='fake_bound_t')

            axs[ch][1].plot(f, f_real_bounds[1][ch, :], color=(0.8, 0, 0, 0.5), label='real_mean_f')
            axs[ch][1].fill_between(f, f_real_bounds[2][ch, :], f_real_bounds[1][ch, :], facecolor='yellow', alpha=0.5,
                                    label='real_bound_f')
            axs[ch][1].plot(f, f_fake_bounds[1][ch, :], color=(0.2, 0, 0, 0.8), label='fake_mean_f')
            axs[ch][1].fill_between(f, f_fake_bounds[2][ch, :], f_fake_bounds[1][ch, :], facecolor='green', alpha=0.5,
                                    label='fake_bound_f')
            axs[ch][0].set_ylim([-1.1, 1.1])
            axs[ch][0].legend()
            axs[ch][1].legend()
            axs[ch][1].set_yscale("log")
        fig.canvas.draw()
        image = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
        image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
        plt.close(fig)
        misc.imsave(os.path.join(self.checkpoints_dir, 'aggregation_{}.png'.format(epoch)), image)


class ClassifierValidator(Plugin):
    def __init__(self, sample_fn, valid_set, output_snapshot_ticks=20):
        super(ClassifierValidator, self).__init__([(1, 'epoch'), (1, 'end')])
        self.sample_fn = sample_fn
        self.valid_set = valid_set
        self.real_features = None
        self.last_depth = None
        self.output_snapshot_ticks = output_snapshot_ticks

    def get_real_features(self):
        if self.last_depth == self.trainer.D.depth:
            return self.real_features
        self.real_features = get_features(torch.cat([batch for batch in self.valid_set], dim=0))
        self.last_depth = self.trainer.D.depth
        return self.real_features

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['validation'] = {'log_format': ':.4f'}

    def update_stats(self, new_dict):
        self.stats = new_dict
        for k, v in new_dict.items():
            self.trainer.stats['validation'][k] = v

    def epoch(self, epoch):
        if len(self.valid_set) == 0:
            return
        if (epoch - 1) % self.output_snapshot_ticks != 0:
            self.update_stats(self.stats)
            return
        self.trainer.G.eval()
        self.trainer.D.eval()
        fakes = []
        d_loss = 0.0
        for batch in self.valid_set:
            x_real = cudize(batch)
            x_fake = self.trainer.G(cudize(Variable(self.sample_fn(x_real.shape[0])))).detach()
            fakes.append(x_fake)
            d_loss += ll(
                self.trainer.D_loss(self.trainer.D, self.trainer.G, x_real, cudize(self.sample_fn(x_real.shape[0]))))
            del x_real
        d_loss /= len(self.valid_set)
        fakes = get_features(torch.cat(fakes, dim=0).data)
        reals = self.get_real_features()
        self.trainer.G.train()
        self.trainer.D.train()
        valid_dict = {'d_loss': d_loss}
        valid_dict.update(get_accuracy(reals, fakes))
        self.update_stats(valid_dict)

    def end(self, *args):
        self.epoch(*args)


class NearestNeighborValidator(Plugin):
    def __init__(self, sample_fn, valid_set, output_snapshot_ticks=20):
        super(NearestNeighborValidator, self).__init__([(1, 'epoch'), (1, 'end')])
        self.sample_fn = sample_fn
        self.valid_set = valid_set
        self.real_tree = None
        self.last_depth = None
        self.output_snapshot_ticks = output_snapshot_ticks

    def get_real_tree(self):
        if self.last_depth == self.trainer.D.depth:
            return self.real_tree
        all_real = torch.cat([batch for batch in self.valid_set], dim=0).cpu().numpy()
        self.real_tree = [BallTree(all_real[:, i, :], leaf_size=30, metric='euclidean') for i in
                          range(all_real.shape[1])]
        self.last_depth = self.trainer.D.depth
        return self.real_tree

    def register(self, trainer):
        self.trainer = trainer
        self.trainer.stats['nn_validation'] = {'log_format': ':.4f'}
        # ['nn_validation.' + x for x in ['rrd', 'rfd', 'rri', 'rfi', 'frd', 'ffd', 'fri', 'ffi']] * channels

    def update_stats(self, new_dict):
        self.stats = new_dict
        for k, v in new_dict.items():
            self.trainer.stats['nn_validation'][k] = v

    def epoch(self, epoch):
        if len(self.valid_set) == 0:
            return
        if (epoch - 1) % self.output_snapshot_ticks != 0:
            self.update_stats(self.stats)
            return
        self.trainer.G.eval()
        fakes = []
        for batch in self.valid_set:
            x_fake = self.trainer.G(cudize(Variable(self.sample_fn(batch.shape[0])))).detach()
            fakes.append(x_fake)
        fakes = torch.cat(fakes, dim=0).data.cpu().numpy()
        fakes = [BallTree(fakes[:, i, :], leaf_size=30, metric='euclidean') for i in range(fakes.shape[1])]
        reals = self.get_real_tree()
        self.trainer.G.train()
        self.update_stats(dict())  # TODO fill the dict with queries(kdt.query(X, k=2, return_distance=True))

    def end(self, *args):
        self.epoch(*args)


class TeeLogger(Logger):

    def __init__(self, log_file, *args, **kwargs):
        super(TeeLogger, self).__init__(*args, **kwargs)
        self.log_file = open(log_file, 'a', 1)

    def log(self, msg):
        print(msg, flush=True)
        self.log_file.write(msg + '\n')

    def epoch(self, epoch_idx):
        self._log_all('log_epoch_fields')
