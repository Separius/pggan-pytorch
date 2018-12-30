import torch
import torch.nn as nn
from torch.optim import Adam
from tqdm import trange, tqdm
from dataset import EEGDataset
from torch.utils.data import DataLoader
from utils import parse_config, cudize, trainable_params, mkdir

default_params = {
    'config_file': None,
    'num_data_workers': 1,
    'random_seed': 1373,
    'cuda_device': 0,
    'minibatch_size': 128,
    'num_epochs': 20,
    'save_location': './results/inception.pth',
    'tiny_sizes': False,
    'age_weight': 0.0,
    'attr_weight': 1.0
}


def get_conv(input_channels, kernel_size):
    return nn.Sequential(
        nn.Conv1d(input_channels, input_channels, kernel_size=kernel_size, padding=(kernel_size - 1) // 2, stride=2),
        nn.ReLU(inplace=True), nn.BatchNorm1d(input_channels))


class InceptionModule(nn.Module):
    def __init__(self, input_channels):
        super().__init__()
        self.p1 = get_conv(input_channels, 3)
        self.p2 = get_conv(input_channels, 5)
        self.p3 = get_conv(input_channels, 9)
        self.aggregate = nn.Sequential(nn.Conv1d(3 * input_channels, input_channels, kernel_size=1, padding=0),
                                       nn.ReLU(inplace=True), nn.BatchNorm1d(input_channels))
        self.residual = nn.AvgPool1d(kernel_size=2)

    def forward(self, x):
        return self.residual(x) + self.aggregate(torch.cat([self.p1(x), self.p2(x), self.p3(x)], dim=1))


class ChronoNet(nn.Module):
    num_block_map = {2 ** (5 + i): i + 1 for i in range(9)}

    def __init__(self, num_channels, seq_len, target_classes, network_channels=64):
        super().__init__()
        self.num_classes = target_classes
        network = [InceptionModule(network_channels) for i in range(self.num_block_map[seq_len])]
        self.network = nn.Sequential(nn.Conv1d(num_channels, network_channels, kernel_size=1), *network,
                                     nn.AdaptiveAvgPool1d(1))
        self.linear = nn.Linear(network_channels, target_classes)

    def forward(self, x):
        h = self.network(x).squeeze()
        return self.linear(h), h


def calc_loss(x):
    y_pred, _ = network(cudize(x['x']))
    y = cudize(x['y'])
    if params['age_weight'] != 0.0:
        loss_age = loss_function_age(y_pred[:, 0], y[:, 0])
    else:
        loss_age = 0.0
    if params['attr_weight'] != 0.0:
        loss_attr = loss_function_attrs(y_pred[:, 1:], y[:, 1:]) * num_attrs
    else:
        loss_attr = 0.0
    return loss_attr * params['attr_weight'] + loss_age * params['age_weight']


if __name__ == '__main__':
    params = parse_config(default_params, [EEGDataset, ChronoNet, Adam], False)
    train_dataset, val_dataset = EEGDataset.from_config(**params['EEGDataset'])
    depth = train_dataset.max_dataset_depth - train_dataset.model_dataset_depth_offset
    train_dataset.model_depth = val_dataset.model_depth = depth
    train_dataset.alpha = val_dataset.alpha = 1.0
    mkdir('./results')

    train_dataloader = DataLoader(train_dataset, params['minibatch_size'], shuffle=True, drop_last=True)
    val_dataloader = DataLoader(val_dataset, params['minibatch_size'], shuffle=False, drop_last=False)

    network = cudize(ChronoNet(train_dataset.num_channels, train_dataset.seq_len, train_dataset.y.shape[1],
                               **params['ChronoNet']))
    num_attrs = train_dataset.y.shape[1] - 1
    print('num_attrs', num_attrs)
    network.train()
    optimizer = Adam(trainable_params(network), **params['Adam'])
    loss_function_age = nn.MSELoss()
    loss_function_attrs = nn.BCEWithLogitsLoss()
    best_loss = None

    for i in trange(params['num_epochs']):
        network.train()
        train_tqdm = tqdm(train_dataloader, dynamic_ncols=True)
        for i, x in enumerate(train_tqdm):
            loss = calc_loss(x)
            network.zero_grad()
            loss.backward()
            optimizer.step()
            if i % 20 == 0:
                train_tqdm.set_description(loss.item())
        train_tqdm.close()
        network.eval()
        with torch.no_grad():
            total_loss = 0
            val_tqdm = tqdm(val_dataloader, leave=False, dynamic_ncols=True)
            for i, x in enumerate(val_tqdm):
                loss = calc_loss(x)
                total_loss += loss.item()
            new_loss = total_loss / i
            val_tqdm.set_description(new_loss)
            val_tqdm.close()
            if best_loss is None or new_loss < best_loss:
                torch.save(network, params['save_location'])
                best_loss = new_loss
