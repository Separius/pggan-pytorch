from torch.optim import Adam
from torch.optim.lr_scheduler import LambdaLR
from network import Generator, Discriminator
from losses import G_loss, D_loss
from functools import partial
from trainer import Trainer
from dataset import EEGDataset
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from plugins import OutputGenerator, SaverPlugin, LRScheduler, AbsoluteTimeMonitor, EfficientLossMonitor, DepthManager, \
    TeeLogger
from utils import load_pkl, save_pkl, cudize, random_latents, trainable_params, create_result_subdir, num_params, \
    create_params, generic_arg_parse, get_structured_params, enable_benchmark, load_model
import numpy as np
import torch
import os
import time
import signal
import yaml
import subprocess
from argparse import ArgumentParser
from collections import OrderedDict

default_params = OrderedDict(
    result_dir='results',
    exp_name='',
    lr_rampup_kimg=50,
    G_lr_max=0.005,
    D_lr_max=0.005,
    total_kimg=4000,
    resume_network='',  # 001-test/network-snapshot-{}-000025.dat
    resume_time=0,
    num_data_workers=2,
    random_seed=1373,
    grad_lambda=10.0,
    iwass_epsilon=0.001,
    load_dataset='',
    loss_type='wgan_gp',  # wgan_gp, wgan_ct, hinge, wgan_theirs, wgan_theirs_ct
    cuda_device=0,
    LAMBDA_2=2,
    optimizer='adam',  # adam, amsgrad, ttur
    config_file=None,
    verbose=False,
    fmap_base=2048,
    fmap_max=256,
    fmap_min=64,
    equalized=True,
    kernel_size=3,
    self_attention_layer=None,  # starts from 0 or null (for G it means putting it after ith layer)
    progression_scale=2,  # single number or a list where prod(list) == seq_len
    num_classes=0,
    monitor_threshold=10,
    monitor_warmup=50,
    monitor_patience=5,
    LAMBDA_3=1
)


class InfiniteRandomSampler(SubsetRandomSampler):

    def __iter__(self):
        while True:
            it = super().__iter__()
            for x in it:
                yield x


def load_models(resume_network, result_dir, logger):
    logger.log('Resuming {}'.format(resume_network))
    G = load_model(os.path.join(result_dir, resume_network.format('generator')))
    D = load_model(os.path.join(result_dir, resume_network.format('discriminator')))
    return G, D


def thread_exit(_signal, frame):
    exit(0)


def worker_init(x):
    signal.signal(signal.SIGINT, thread_exit)


def main(params):
    dataset_params = params['EEGDataset']
    if params['load_dataset'] and os.path.exists(params['load_dataset']):
        print('loading dataset from file')
        dataset = load_pkl(params['load_dataset'])
    else:
        print('creating dataset from scratch')
        dataset = EEGDataset(params['progression_scale'], **dataset_params)
        if params['load_dataset']:
            print('saving dataset to file')
            save_pkl(params['load_dataset'], dataset)
    if params['config_file'] and params['exp_name'] == '':
        params['exp_name'] = params['config_file'].split('/')[-1].split('.')[0]
    if not params['verbose']:
        result_dir = create_result_subdir(params['result_dir'], params['exp_name'])

    losses = ['G_loss', 'D_loss']
    stats_to_log = ['tick_stat', 'kimg_stat']
    stats_to_log.extend(['depth', 'alpha', 'minibatch_size'])
    if not params['self_attention_layer'] is None:
        stats_to_log.extend(['gamma'])
    stats_to_log.extend(['time', 'sec.tick', 'sec.kimg'] + losses)
    num_channels = dataset.shape[1]

    if params['verbose'] and params['resume_network']:
        print('resuming does not work in verbose mode')
        params['verbose'] = False
    if not params['verbose']:
        logger = TeeLogger(os.path.join(result_dir, 'log.txt'), params['exp_name'], stats_to_log, [(1, 'epoch')])
    if params['resume_network']:
        G, D = load_models(params['resume_network'], params['result_dir'], logger)
    else:
        G = Generator(num_classes=params['num_classes'], progression_scale=params['progression_scale'],
                      dataset_shape=dataset.shape, initial_size=dataset_params['model_dataset_depth_offset'],
                      fmap_base=params['fmap_base'], fmap_max=params['fmap_max'], fmap_min=params['fmap_min'],
                      kernel_size=params['kernel_size'], is_extended=dataset_params['extra_factor'] != 1,
                      depth_offset=params['DepthManager']['depth_offset'], equalized=params['equalized'],
                      self_attention_layer=params['self_attention_layer'], **params['Generator'])
        if params['Discriminator']['param_norm'] == 'spectral':
            params['Discriminator']['act_norm'] = None
        D = Discriminator(num_classes=params['num_classes'], progression_scale=params['progression_scale'],
                          dataset_shape=dataset.shape, initial_size=dataset_params['model_dataset_depth_offset'],
                          fmap_base=params['fmap_base'], fmap_max=params['fmap_max'], fmap_min=params['fmap_min'],
                          kernel_size=params['kernel_size'], equalized=params['equalized'],
                          depth_offset=params['DepthManager']['depth_offset'],
                          self_attention_layer=params['self_attention_layer'], **params['Discriminator'])
    latent_size = G.latent_size
    assert G.max_depth == D.max_depth
    G = cudize(G)
    D = cudize(D)
    D_loss_fun = partial(D_loss, loss_type=params['loss_type'], iwass_epsilon=params['iwass_epsilon'],
                         grad_lambda=params['grad_lambda'], LAMBDA_2=params['LAMBDA_2'], LAMBDA_3=params['LAMBDA_3'])
    G_loss_fun = partial(G_loss, LAMBDA_3=params['LAMBDA_3'])
    max_depth = min(G.max_depth, D.max_depth)
    if params['verbose']:
        from torchsummary import summary
        from matplotlib import pyplot as plt
        from PIL import Image
        G.is_extended = False
        G.set_gamma(1)
        G.depth = G.max_depth
        summary(G, (latent_size,))
        D.set_gamma(1)
        D.depth = D.max_depth
        summary(D, (dataset_params['num_channels'], dataset_params['seq_len']))
        D.train()
        G.train()
        dm = DepthManager(None, None, max_depth, params['Trainer']['tick_kimg_default'], **params['DepthManager'])
        print('start_gamma:', dm.start_gamma, '\tend_gamma:', dm.end_gamma, '\tmax_depth:', dm.max_depth,
              '\tdepth_offset:', dm.depth_offset, '\tseq_len:', dataset_params['seq_len'], '\tdataset_offset:',
              dataset_params['model_dataset_depth_offset'])
        print('alpha_map:', dm.alpha_map)
        print(D)
        print(G)
        points = [dm.calc_progress(i)[0] + dm.calc_progress(i)[1] - 1 for i in
                  range(0, params['total_kimg'] * 1000, 1000)]
        plt.plot(points)
        fig = plt.gcf()
        fig.canvas.draw()
        image = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
        image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
        image = Image.fromarray(image, 'RGB')
        image.show()
        z = cudize(torch.randn(2, latent_size))
        l = D_loss_fun(D, G, G(z), z, None)
        l.backward()
        print('non long loss:', l.item())
        G.is_extended = True
        z = (cudize(torch.randn(8, latent_size // 4)), cudize(torch.randn(8, 3 * latent_size // 4, 4)))
        l = D_loss_fun(D, G, G(*z), z, None)
        l.backward()
        print('long loss:', l.item())
        G.is_extended = False
        print('d_final_basic: ', D(G(cudize(torch.randn(2, latent_size))))[1].shape)
        G.is_extended = True
        print('d_final_long: ', D(G(*z))[1].shape)
        l = G_loss_fun(G, D, z, None)
        print('generator loss:', l.item())
        l.backward()
        real_images_expr, real_images_mixed, z_d = Trainer.prepare_d_data(G(*z), z, 4, False, 1)
        l = D_loss_fun(D, G, real_images_expr, z_d, real_images_mixed)
        l.backward()
        z_g, z_mixed = Trainer.prepare_g_data(z, 4, False, 1)
        l = G_loss_fun(G, D, z_g, z_mixed)
        l.backward()
        exit()
    logger.log('exp name: {}'.format(params['exp_name']))
    try:
        logger.log('commit hash: {}'.format(subprocess.check_output(["git", "describe", "--always"]).strip()))
    except:
        logger.log('current time: {}'.format(time.time()))
    logger.log('dataset shape: {}'.format(dataset.shape))
    logger.log('Total number of parameters in Generator: {}'.format(num_params(G)))
    logger.log('Total number of parameters in Discriminator: {}'.format(num_params(D)))

    mb_def = params['DepthManager']['minibatch_default']
    dataset_len = len(dataset)
    train_idx = list(range(dataset_len))
    np.random.shuffle(train_idx)

    def get_dataloader(minibatch_size):
        return DataLoader(dataset, minibatch_size, sampler=InfiniteRandomSampler(train_idx), worker_init_fn=worker_init,
                          num_workers=params['num_data_workers'], pin_memory=False, drop_last=True)

    def rl(bs):
        return partial(random_latents, num_latents=bs, latent_size=latent_size)

    if params['optimizer'] == 'ttur':
        params['D_lr_max'] = params['G_lr_max'] * 4.0
        params['Adam']['betas'] = (0, 0.9)
    opt_g = Adam(trainable_params(G), params['G_lr_max'], amsgrad=params['optimizer'] == 'amsgrad', **params['Adam'])
    opt_d = Adam(trainable_params(D), params['D_lr_max'], amsgrad=params['optimizer'] == 'amsgrad', **params['Adam'])

    def rampup(cur_nimg):
        if cur_nimg < params['lr_rampup_kimg'] * 1000:
            p = max(0.0, 1 - cur_nimg / (params['lr_rampup_kimg'] * 1000))
            return np.exp(-p * p * 5.0)
        else:
            return 1.0

    lr_scheduler_d = LambdaLR(opt_d, rampup)
    lr_scheduler_g = LambdaLR(opt_g, rampup)

    trainer = Trainer(D, G, D_loss_fun, G_loss_fun, opt_d, opt_g, dataset, rl(mb_def), dataset_params['extra_factor'],
                      params['LAMBDA_3'], params['Generator']['is_morph'], **params['Trainer'])
    trainer.register_plugin(DepthManager(get_dataloader, rl, max_depth, params['Trainer']['tick_kimg_default'],
                                         params['self_attention_layer'] is not None, **params['DepthManager']))
    for i, loss_name in enumerate(losses):
        trainer.register_plugin(
            EfficientLossMonitor(i, loss_name, params['monitor_threshold'], params['monitor_warmup'],
                                 params['monitor_patience']))

    trainer.register_plugin(SaverPlugin(result_dir, **params['SaverPlugin']))
    trainer.register_plugin(
        OutputGenerator(lambda x: random_latents(x, latent_size, dataset_params['extra_factor']), result_dir,
                        dataset_params['seq_len'], dataset_params['max_freq'], dataset_params['seq_len'],
                        **params['OutputGenerator']))
    trainer.register_plugin(AbsoluteTimeMonitor(params['resume_time']))
    trainer.register_plugin(LRScheduler(lr_scheduler_d, lr_scheduler_g))
    trainer.register_plugin(logger)
    yaml.dump(params, open(os.path.join(result_dir, 'conf.yml'), 'w'))
    trainer.run(params['total_kimg'])
    del trainer


if __name__ == "__main__":
    parser = ArgumentParser()
    needarg_classes = [Trainer, Generator, Discriminator, DepthManager, SaverPlugin, OutputGenerator, Adam, EEGDataset]
    excludes = {'Adam': {'lr', 'amsgrad', 'weight_decay'}}
    default_overrides = {'Adam': {'betas': (0.0, 0.99)}}
    auto_args = create_params(needarg_classes, excludes, default_overrides)
    for k in default_params:
        parser.add_argument('--{}'.format(k), type=partial(generic_arg_parse, hinttype=type(default_params[k])))
    for cls in auto_args:
        group = parser.add_argument_group(cls, 'Arguments for initialization of class {}'.format(cls))
        for k in auto_args[cls]:
            name = '{}.{}'.format(cls, k)
            group.add_argument('--{}'.format(name), type=generic_arg_parse)
            default_params[name] = auto_args[cls][k]
    parser.set_defaults(**default_params)
    params = vars(parser.parse_args())
    if params['config_file']:
        print('loading config_file')
        params.update(yaml.load(open(params['config_file'], 'r')))
    params = get_structured_params(params)
    np.random.seed(params['random_seed'])
    torch.manual_seed(params['random_seed'])
    if torch.cuda.is_available():
        torch.cuda.set_device(params['cuda_device'])
        torch.cuda.manual_seed_all(params['random_seed'])
    enable_benchmark()
    main(params)
    print('training finished!')
