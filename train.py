from torch.optim import Adam, ASGD, RMSprop
from torch.optim.lr_scheduler import LambdaLR
from network import Generator, Discriminator
from losses import G_loss, D_loss
from functools import partial
from trainer import Trainer
from dataset import MyDataset
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SequentialSampler, SubsetRandomSampler
from plugins import FixedNoise, OutputGenerator, Validator, GifGenerator, SaverPlugin, LRScheduler, AbsoluteTimeMonitor, \
    EfficientLossMonitor, DepthManager, TeeLogger
from utils import load_pkl, save_pkl, cudize, random_latents, trainable_params, create_result_subdir, num_params, \
    create_params, generic_arg_parse, get_structured_params
import numpy as np
import torch
import os
import signal
import yaml
from argparse import ArgumentParser
from collections import OrderedDict

torch.manual_seed(1337)

default_params = OrderedDict(
    result_dir='results',
    exp_name='exp_name',
    lr_rampup_kimg=50,
    G_lr_max=0.001,
    D_lr_max=0.001,
    total_kimg=4000,
    resume_network='',
    resume_time=0,
    num_data_workers=2,
    random_seed=1337,
    grad_lambda=10.0,
    iwass_epsilon=0.001,
    save_dataset='',
    load_dataset='',
    loss_type='wgan_theirs',
    label_smoothing=0.05,
    use_mixup=False,
    apply_sigmoid=False,
    cuda_device=0,
    validation_split=0.1,
    test_mode=False,
    LAMBDA_2=2,
    weight_decay=0,
    optimizer='adam',  # or amsgrad or asgd or rmsprop or ttur
    config_file=None
)


class InfiniteRandomSampler(SubsetRandomSampler):

    def __iter__(self):
        while True:
            it = super().__iter__()
            for x in it:
                yield x


def load_models(resume_network, result_dir, logger):
    logger.log('Resuming {}'.format(resume_network))
    G = torch.load(os.path.join(result_dir, resume_network.format('generator')))
    D = torch.load(os.path.join(result_dir, resume_network.format('discriminator')))
    return G, D


def thread_exit(_signal, frame):
    exit(0)


def worker_init(x):
    signal.signal(signal.SIGINT, thread_exit)


def main(params):
    if params['test_mode']:
        print('switching to test mode')
        params['exp_name'] = 'test'
        params['total_kimg'] = 4
        params['Trainer']['tick_kimg_default'] = 0.2
        params['Generator']['fmap_base'] = 8
        params['Generator']['fmap_max'] = 8
        params['Generator']['fmap_min'] = 4
        params['Generator']['latent_size'] = 8
        params['Discriminator']['fmap_base'] = 8
        params['Discriminator']['fmap_max'] = 8
        params['Discriminator']['fmap_min'] = 4
        params['DepthManager']['lod_training_kimg'] = 0.15
        params['DepthManager']['lod_transition_kimg'] = 0.15
        params['SaverPlugin']['network_snapshot_ticks'] = 1
        params['SaverPlugin']['keep_old_checkpoints'] = True
        params['OutputGenerator']['output_snapshot_ticks'] = 1
        params['Validator']['output_snapshot_ticks'] = 1
        params['GifGenerator']['num_frames'] = 25
        ds_path = 'results/dataset{}.pkl'.format(params['MyDataset']['num_channels'])
        if os.path.exists(ds_path):
            params['load_dataset'] = ds_path
        else:
            params['save_dataset'] = ds_path
    if params['load_dataset']:
        print('loading dataset from file')
        dataset = load_pkl(params['load_dataset'])
    else:
        print('loading dataset from scratch')
        dataset = MyDataset(**params['MyDataset'])
        if params['save_dataset']:
            print('saving dataset to file')
            save_pkl(params['save_dataset'], dataset)
    result_dir = create_result_subdir(params['result_dir'], params['exp_name'])

    losses = ['G_loss', 'D_loss']
    stats_to_log = ['tick_stat', 'kimg_stat']
    stats_to_log.extend(['depth', 'alpha', 'minibatch_size'])
    stats_to_log.extend(['time', 'sec.tick', 'sec.kimg'] + losses)
    num_channels = dataset.shape[1]
    if params['validation_split'] > 0:
        val_stats = ['d_loss']
        if num_channels != 1:
            for ch in range(num_channels):
                for cs in ['linear_svm', 'rbf_svm', 'decision_tree', 'random_forest']:
                    val_stats.append(cs + '_ch_' + str(ch))
        for cs in ['linear_svm', 'rbf_svm', 'decision_tree', 'random_forest']:
            val_stats.append(cs + '_all')
        stats_to_log.extend(['validation.' + x for x in val_stats])
    logger = TeeLogger(os.path.join(result_dir, 'log.txt'), stats_to_log, [(1, 'epoch')])

    if params['resume_network']:
        G, D = load_models(params['resume_network'], params['result_dir'], logger)
    else:
        # G = Generator(dataset.shape, params['MyDataset']['model_dataset_depth_offset'], **params['Generator'])
        G = Generator(dataset.shape, **params['Generator'])
        D = Discriminator(dataset.shape, params['apply_sigmoid'], **params['Discriminator'])
        # D = Discriminator(dataset.shape, params['MyDataset']['model_dataset_depth_offset'], params['apply_sigmoid'],
        #                   **params['Discriminator'])
    assert G.max_depth == D.max_depth
    G = cudize(G)
    D = cudize(D)
    latent_size = params['Generator']['latent_size']
    logger.log('dataset shape: {}'.format(dataset.shape))
    logger.log('Total number of parameters in Generator: {}'.format(num_params(G)))
    logger.log('Total number of parameters in Discriminator: {}'.format(num_params(D)))
    # logger.log('Total number of parameters in Main Discriminator: {}'.format(num_params(D.main_disc)))
    # if D.gang:
    #     logger.log('Total number of parameters in Each Mini Discriminator: {}'.format(num_params(D.discs[0])))
    #     logger.log('Total number of parameters in Whole Discriminator: {}'.format(num_params(D)))

    mb_def = params['DepthManager']['minibatch_default']
    dataset_len = len(dataset)
    indices = list(range(dataset_len))
    np.random.shuffle(indices)
    split = int(np.floor(params['validation_split'] * dataset_len))
    train_idx, valid_idx = indices[split:], indices[:split]
    valid_data_loader = DataLoader(dataset, batch_size=mb_def, sampler=SequentialSampler(valid_idx), drop_last=False,
                                   num_workers=params['num_data_workers'])

    def get_dataloader(minibatch_size):
        return DataLoader(dataset, minibatch_size, sampler=InfiniteRandomSampler(train_idx), worker_init_fn=worker_init,
                          num_workers=params['num_data_workers'], pin_memory=False, drop_last=True)

    def rl(bs):
        return lambda: random_latents(bs, latent_size)

    if params['optimizer'] in ('amsgrad', 'adam', 'ttur'):
        if params['optimizer'] == 'ttur':
            params['G_lr_max'] = params['D_lr_max'] / 5.0
        opt_g = Adam(trainable_params(G), params['G_lr_max'], amsgrad=params['optimizer'] == 'amsgrad',
                     weight_decay=params['weight_decay'], **params['Adam'])
        opt_d = Adam(trainable_params(D), params['D_lr_max'], amsgrad=params['optimizer'] == 'amsgrad',
                     weight_decay=params['weight_decay'], **params['Adam'])
    elif params['optimizer'] in ('asgd',):
        opt_g = ASGD(trainable_params(G), params['G_lr_max'], weight_decay=params['weight_decay'], **params['ASGD'])
        opt_d = ASGD(trainable_params(D), params['D_lr_max'], weight_decay=params['weight_decay'], **params['ASGD'])
    elif params['optimizer'] in ('rmsprop',):
        opt_g = RMSprop(trainable_params(G), params['G_lr_max'], weight_decay=params['weight_decay'],
                        **params['RMSprop'])
        opt_d = RMSprop(trainable_params(D), params['D_lr_max'], weight_decay=params['weight_decay'],
                        **params['RMSprop'])

    def rampup(cur_nimg):
        if cur_nimg < params['lr_rampup_kimg'] * 1000:
            p = max(0.0, 1 - cur_nimg / (params['lr_rampup_kimg'] * 1000))
            return np.exp(-p * p * 5.0)
        else:
            return 1.0

    lr_scheduler_d = LambdaLR(opt_d, rampup)
    lr_scheduler_g = LambdaLR(opt_g, rampup)

    D_loss_fun = partial(D_loss, loss_type=params['loss_type'], iwass_epsilon=params['iwass_epsilon'],
                         grad_lambda=params['grad_lambda'], label_smoothing=params['label_smoothing'],
                         use_mixup=params['use_mixup'], apply_sigmoid=params['apply_sigmoid'],
                         loss_of_mean=params['loss_of_mean'], iwass_target=1.0)
    G_loss_fun = partial(G_loss, loss_type=params['loss_type'], label_smoothing=params['label_smoothing'],
                         apply_sigmoid=params['apply_sigmoid'], loss_of_mean=params['loss_of_mean'])
    trainer = Trainer(D, G, D_loss_fun, G_loss_fun,
                      opt_d, opt_g, dataset, iter(get_dataloader(mb_def)), rl(mb_def), **params['Trainer'])
    max_depth = min(G.max_depth, D.max_depth)
    trainer.register_plugin(
        DepthManager(get_dataloader, rl, max_depth, params['Trainer']['tick_kimg_default'], **params['DepthManager']))
    for i, loss_name in enumerate(losses):
        trainer.register_plugin(EfficientLossMonitor(i, loss_name))

    trainer.register_plugin(SaverPlugin(result_dir, **params['SaverPlugin']))
    if params['validation_split'] > 0:
        trainer.register_plugin(
            Validator(lambda x: random_latents(x, latent_size), valid_data_loader, **params['Validator']))
    trainer.register_plugin(
        OutputGenerator(lambda x: random_latents(x, latent_size), result_dir, params['MyDataset']['seq_len'],
                        params['MyDataset']['max_freq'], **params['OutputGenerator']))
    trainer.register_plugin(
        FixedNoise(lambda x: random_latents(x, latent_size), result_dir, params['MyDataset']['seq_len'],
                   params['MyDataset']['max_freq'], **params['OutputGenerator']))
    trainer.register_plugin(
        GifGenerator(lambda x: random_latents(x, latent_size), result_dir, params['MyDataset']['seq_len'],
                     params['MyDataset']['max_freq'], params['OutputGenerator']['output_snapshot_ticks'],
                     params['OutputGenerator']['res_len'], **params['GifGenerator']))
    trainer.register_plugin(AbsoluteTimeMonitor(params['resume_time']))
    trainer.register_plugin(LRScheduler(lr_scheduler_d, lr_scheduler_g))
    trainer.register_plugin(logger)
    trainer.run(params['total_kimg'])


if __name__ == "__main__":
    parser = ArgumentParser()
    needarg_classes = [Trainer, Generator, Discriminator, DepthManager, SaverPlugin, OutputGenerator, Adam,
                       GifGenerator, Validator, MyDataset, ASGD, RMSprop]
    excludes = {'Adam': {'lr', 'amsgrad', 'weight_decay'}, 'ASGD': {'lr', 'weight_decay'},
                'RMSprop': {'lr', 'weight_decay'}}
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
    # yaml.dump(params, open('{}.yml'.format(params['exp_name']), 'w'))
    params = get_structured_params(params)
    if torch.cuda.is_available():
        torch.cuda.set_device(params['cuda_device'])
    main(params)
