from utils import cudize, num_params, dict_add, divide_dict, merge_pred_accs, AttrDict
from cpc_network import Network
from dataset import ThinEEGDataset

import torch
import numpy as np
from torch.optim import Adam
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader, SubsetRandomSampler

hp = AttrDict(
    generate_long_sequence=True,
    pool_or_stride='stride',
    use_shared_sinc=True,
    seed=12658,
    prediction_k=4,
    validation_ratio=0.1,
    ds_stride=0.5,
    num_channels=17,
    use_sinc_encoder=False,

    bidirectional=False,
    causal_prediction=True,

    prediction_loss_weight=3.0,
    global_loss_weight=1.0,
    local_loss_weight=2.0,

    use_transformer=False,
    contextualizer_num_layers=1,
    contextualizer_dropout=0.0,

    batch_size=128,
    lr=2e-3,
    epochs=100,
    weight_decay=0.005,

    encoder_dropout=0.1,
    encoder_activation='glu',
    tiny_encoder=False,
)


def main(summary):
    train_dataset, val_dataset = ThinEEGDataset.from_config(validation_ratio=hp.validation_ratio,
                                                            num_channels=hp.num_channels, stride=hp.ds_stride)
    if hp.validation_ratio == 0:
        indices = np.arange(len(train_dataset))
        np.random.seed(hp.seed)
        np.random.shuffle(indices)
        train_dataloader = DataLoader(train_dataset, batch_size=hp.batch_size, num_workers=0, drop_last=True,
                                      sampler=SubsetRandomSampler(indices[:int(len(indices) * 9 / 10)]))
        val_dataloader = DataLoader(train_dataset, batch_size=hp.batch_size, num_workers=0, drop_last=False,
                                    sampler=SubsetRandomSampler(indices[-int(len(indices) * 9 / 10):]))
        print('dataset_size', train_dataset.shape)
    else:
        train_dataloader = DataLoader(train_dataset, batch_size=hp.batch_size,
                                      num_workers=0, drop_last=True, shuffle=True)
        val_dataloader = DataLoader(val_dataset, batch_size=hp.batch_size,
                                    num_workers=0, drop_last=False, shuffle=False)
        print('train_size', train_dataset.shape)
        print('val_size', val_dataset.shape)
    network = cudize(Network(train_dataset.num_channels, generate_long_sequence=hp.generate_long_sequence,
                             pooling=hp.pool_or_stride == 'pool', encoder_dropout=hp.encoder_dropout,
                             use_sinc_encoder=hp.use_sinc_encoder, use_shared_sinc=hp.use_shared_sinc,
                             bidirectional=hp.bidirectional, contextualizer_num_layers=hp.contextualizer_num_layers,
                             contextualizer_dropout=hp.contextualizer_dropout, use_transformer=hp.use_transformer,
                             causal_prediction=hp.causal_prediction, prediction_k=hp.prediction_k,
                             encoder_activation=hp.encoder_activation, tiny_encoder=hp.tiny_encoder))
    num_parameters = num_params(network)
    print('num_parameters', num_parameters)
    network_optimizer = Adam(network.parameters(), lr=hp.lr, weight_decay=hp.weight_decay)
    scheduler = ReduceLROnPlateau(network_optimizer, patience=3, verbose=True)
    best_val_loss = float('inf')
    for epoch in range(hp.epochs):
        for training, data_loader in zip((False, True), (val_dataloader, train_dataloader)):
            if training:
                network.train()
            else:
                network.eval()
            total_network_loss = 0.0
            total_prediction_loss = 0.0
            total_global_discriminator_loss = 0.0
            total_local_discriminator_loss = 0.0
            total_global_accuracy_one = 0.0
            total_global_accuracy_two = 0.0
            total_local_accuracy_one = 0.0
            total_local_accuracy_two = 0.0
            total_pred_acc = {}
            total_count = 0
            with torch.set_grad_enabled(training):
                for batch in data_loader:
                    x = cudize(batch)
                    prediction_loss, global_discriminator_loss, local_discriminator_loss, c_pooled, global_accuracy, local_accuracy, pred_accuracy = network(
                        x)
                    global_accuracy_one, global_accuracy_two = global_accuracy
                    local_accuracy_one, local_accuracy_two = local_accuracy
                    network_loss = hp.prediction_loss_weight * prediction_loss + hp.global_loss_weight * global_discriminator_loss + hp.local_loss_weight * local_discriminator_loss
                    this_batch_size = x.size(0)
                    total_count += this_batch_size
                    total_network_loss += network_loss.item() * this_batch_size
                    total_prediction_loss += prediction_loss.item() * this_batch_size
                    total_global_discriminator_loss += global_discriminator_loss.item() * this_batch_size
                    total_local_discriminator_loss += local_discriminator_loss.item() * this_batch_size
                    total_global_accuracy_one += global_accuracy_one * this_batch_size
                    total_global_accuracy_two += global_accuracy_two * this_batch_size
                    total_local_accuracy_one += local_accuracy_one * this_batch_size
                    total_local_accuracy_two += local_accuracy_two * this_batch_size
                    dict_add(total_pred_acc, pred_accuracy, this_batch_size)
                    if training:
                        network_optimizer.zero_grad()
                        network_loss.backward()
                        network_optimizer.step()
            total_global_accuracy_one /= total_count
            total_global_accuracy_two /= total_count
            total_local_accuracy_one /= total_count
            total_local_accuracy_two /= total_count
            divide_dict(total_pred_acc, total_count)

            total_prediction_loss /= total_count
            total_pred_acc = merge_pred_accs(total_pred_acc, network.prediction_loss_network.k,
                                             network.prediction_loss_network.bidirectional)
            total_global_discriminator_loss /= total_count
            total_global_accuracy = (total_global_accuracy_one + total_global_accuracy_two) / 2
            total_local_discriminator_loss /= total_count
            total_local_accuracy = (total_local_accuracy_one + total_local_accuracy_two) / 2
            total_network_loss /= total_count

            metrics = dict(prediction_loss=total_prediction_loss, prediction_acc=total_pred_acc,
                           global_loss=total_global_discriminator_loss, global_acc=total_global_accuracy,
                           local_loss=total_local_discriminator_loss, local_acc=total_local_accuracy,
                           net_loss=total_network_loss)
            if not training:
                scheduler.step(metrics['net_loss'])
            if summary:
                print('train' if training else 'validation', epoch, metrics['net_loss'])
            else:
                print('train' if training else 'validation', epoch, metrics)
            if not training and (metrics['net_loss'] < best_val_loss):
                best_val_loss = metrics['net_loss']
                torch.save(network.state_dict(), 'best_network.pth')


if __name__ == '__main__':
    main(summary=True)
