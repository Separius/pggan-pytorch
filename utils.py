import os
import yaml
import torch
import random
import inspect
import numpy as np
from pickle import load, dump
from functools import partial
import torch.nn.functional as F
from argparse import ArgumentParser

EPSILON = 1e-8
half_tensor = None


def generate_samples(generator, gen_input):
    return generator(gen_input)[0].data.cpu().numpy()


def save_pkl(file_name, obj):
    with open(file_name, 'wb') as f:
        dump(obj, f, protocol=4)


def load_pkl(file_name):
    if not os.path.exists(file_name):
        return None
    with open(file_name, 'rb') as f:
        return load(f)


def get_half(num_latents, latent_size):
    global half_tensor
    if half_tensor is None or half_tensor.size() != (num_latents, latent_size):
        half_tensor = torch.ones(num_latents, latent_size) * 0.5
    return half_tensor


def random_latents(num_latents, latent_size, z_distribution='normal'):
    if z_distribution == 'normal':
        return torch.randn(num_latents, latent_size)
    elif z_distribution == 'censored':
        return F.relu(torch.randn(num_latents, latent_size))
    elif z_distribution == 'bernoulli':
        return torch.bernoulli(get_half(num_latents, latent_size))
    else:
        raise ValueError()


def create_result_subdir(results_dir, experiment_name, dir_pattern='{new_num:03}-{exp_name}'):
    if not os.path.exists(results_dir):
        os.makedirs(results_dir)
    fnames = os.listdir(results_dir)
    max_num = max(map(int, filter(lambda x: all(y.isdigit() for y in x), (x.split('-')[0] for x in fnames))), default=0)
    path = os.path.join(results_dir, dir_pattern.format(new_num=max_num + 1, exp_name=experiment_name))
    os.makedirs(path, exist_ok=False)
    return path


def num_params(net):
    model_parameters = trainable_params(net)
    return sum([np.prod(p.size()) for p in model_parameters])


def generic_arg_parse(x, hinttype=None):
    if hinttype is int or hinttype is float or hinttype is str:
        return hinttype(x)
    try:
        for _ in range(2):
            x = x.strip('\'').strip("\"")
        __special_tmp = eval(x, {}, {})
    except:  # the string contained some name - probably path, treat as string
        __special_tmp = x  # treat as string
    return __special_tmp


def create_params(classes, excludes=None, overrides=None):
    params = {}
    if not excludes:
        excludes = {}
    if not overrides:
        overrides = {}
    for cls in classes:
        nm = cls.__name__
        params[nm] = {
            k: (v.default if nm not in overrides or k not in overrides[nm] else overrides[nm][k])
            for k, v in dict(inspect.signature(cls.__init__).parameters).items()
            if v.default != inspect._empty and (nm not in excludes or k not in excludes[nm])
        }
    return params


def get_structured_params(params):
    new_params = {}
    for p in params:
        if '.' in p:
            [cls, attr] = p.split('.', 1)
            if cls not in new_params:
                new_params[cls] = {}
            new_params[cls][attr] = params[p]
        else:
            new_params[p] = params[p]
    return new_params


def cudize(thing):
    if thing is None:
        return None
    has_cuda = torch.cuda.is_available()
    if not has_cuda:
        return thing
    if isinstance(thing, (list, tuple)):
        return [item.cuda(non_blocking=True) for item in thing]
    if isinstance(thing, dict):
        return {k: v.cuda(non_blocking=True) for k, v in thing.items()}
    return thing.cuda(non_blocking=True)


def trainable_params(model):
    return filter(lambda p: p.requires_grad, model.parameters())


def pixel_norm(h):
    mean = torch.mean(h * h, dim=1, keepdim=True)
    dom = torch.rsqrt(mean + EPSILON)
    return h * dom


def simple_argparser(default_params):
    parser = ArgumentParser()
    for k in default_params:
        parser.add_argument('--{}'.format(k), type=partial(generic_arg_parse, hinttype=type(default_params[k])))
    parser.set_defaults(**default_params)
    return get_structured_params(vars(parser.parse_args()))


def enable_benchmark():
    torch.backends.cudnn.benchmark = True  # for fast training(if network input size is almost constant)


def load_model(model_path, return_all=False):
    state = torch.load(model_path, map_location='cpu')
    if not return_all:
        return state['model']
    return state['model'], state['optimizer'], state['cur_nimg']


def parse_config(default_params, need_arg_classes):
    parser = ArgumentParser()
    excludes = {'Adam': {'lr', 'amsgrad'}}
    default_overrides = {'Adam': {'betas': (0.0, 0.99)}}
    auto_args = create_params(need_arg_classes, excludes, default_overrides)
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
    if params['cpu_deterministic']:
        params['num_data_workers'] = 0
    random.seed(params['random_seed'])
    np.random.seed(params['random_seed'])
    torch.manual_seed(params['random_seed'])
    if torch.cuda.is_available():
        torch.cuda.set_device(params['cuda_device'])
        torch.cuda.manual_seed_all(params['random_seed'])
        enable_benchmark()
    return params


def random_onehot(num_classes, num_samples):
    return np.eye(num_classes, dtype=np.float32)[np.random.choice(num_classes, num_samples)]
