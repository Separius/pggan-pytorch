import json
import torch
from tqdm import trange
from torch.optim import Adam
from torch.utils.data import DataLoader
from pytorch_pretrained_bert import BertAdam
from torch.optim.lr_scheduler import ReduceLROnPlateau

from dataset import EEGDataset
from cpc.cpc_network import Network
from utils import cudize, num_params, dict_add, divide_dict, AttrDict

hp = AttrDict(
    validation_seed=12658,
    validation_ratio=0.1,
    prediction_k=4,
    cross_entropy=True,
    use_transformer=False,
    residual_encoder=False,  # I like to set it to True
    use_scheduler=True,
    use_bert_adam=False,
    prediction_loss_weight=3.0,
    global_loss_weight=1.0,
    local_loss_weight=2.0,
    contextualizer_num_layers=1,
    contextualizer_dropout=0,
    batch_size=128,
    lr=2e-3,
    epochs=31,
    weight_decay=0.005,
    sinc_encoder=False,
)


def main(summary):
    train_dataset, val_dataset = EEGDataset.from_config(validation_ratio=hp.validation_ratio,
                                                        validation_seed=hp.validation_seed,
                                                        dir_path='./data/prepared_eegs_mat_th5',
                                                        data_sampling_freq=220, start_sampling_freq=1,
                                                        end_sampling_freq=60, start_seq_len=32,
                                                        num_channels=17, return_long=False)
    train_dataloader = DataLoader(train_dataset, batch_size=hp.batch_size, num_workers=0, drop_last=True)
    val_dataloader = DataLoader(val_dataset, batch_size=hp.batch_size, num_workers=0, drop_last=False, pin_memory=True)
    network = cudize(
        Network(train_dataset.num_channels, bidirectional=False, contextualizer_num_layers=hp.contextualizer_num_layers,
                contextualizer_dropout=hp.contextualizer_dropout, use_transformer=hp.use_transformer,
                prediction_k=hp.prediction_k * (hp.prediction_loss_weight != 0.0),
                have_global=(hp.global_loss_weight != 0.0), have_local=(hp.local_loss_weight != 0.0),
                residual_encoder=hp.residual_encoder, sinc_encoder=hp.sinc_encoder))
    num_parameters = num_params(network)
    print('num_parameters', num_parameters)
    if hp.use_bert_adam:
        network_optimizer = BertAdam(network.parameters(), lr=hp.lr, weight_decay=hp.weight_decay,
                                     warmup=0.2, t_total=hp.epochs * len(train_dataloader), schedule='warmup_linear')
    else:
        network_optimizer = Adam(network.parameters(), lr=hp.lr, weight_decay=hp.weight_decay)
    if hp.use_scheduler:
        scheduler = ReduceLROnPlateau(network_optimizer, patience=3, verbose=True)
    best_val_loss = float('inf')
    for epoch in trange(hp.epochs):
        for training, data_loader in zip((False, True), (val_dataloader, train_dataloader)):
            if training:
                if epoch == hp.epochs - 1:
                    break
                network.train()
            else:
                network.eval()
            total_network_loss = 0.0
            total_prediction_loss = 0.0
            total_global_loss = 0.0
            total_local_loss = 0.0
            total_global_accuracy = 0.0
            total_local_accuracy = 0.0
            total_k_pred_acc = {}
            total_pred_acc = 0.0
            total_count = 0
            with torch.set_grad_enabled(training):
                for batch in data_loader:
                    x = cudize(batch['x'])
                    network_return = network.forward(x)
                    network_loss = hp.prediction_loss_weight * network_return.losses.prediction_
                    network_loss = network_loss + hp.global_loss_weight * network_return.losses.global_
                    network_loss = network_loss + hp.local_loss_weight * network_return.losses.local_

                    bs = x.size(0)
                    total_count += bs
                    total_network_loss += network_loss.item() * bs
                    total_prediction_loss += network_return.losses.prediction_.item() * bs
                    total_global_loss += network_return.losses.global_.item() * bs
                    total_local_loss += network_return.losses.local_.item() * bs

                    total_global_accuracy += network_return.accuracies.global_ * bs
                    total_local_accuracy += network_return.accuracies.local_ * bs
                    dict_add(total_k_pred_acc, network_return.accuracies.prediction_, bs)
                    len_pred = len(network_return.accuracies.prediction_)
                    if len_pred > 0:
                        total_pred_acc += sum(network_return.accuracies.prediction_.values()) / len_pred * bs

                    if training:
                        network_optimizer.zero_grad()
                        network_loss.backward()
                        network_optimizer.step()

            metrics = dict(net_loss=total_network_loss)
            if network.prediction_loss_network.k > 0 and hp.prediction_loss_weight != 0:
                metrics.update(dict(avg_prediction_acc=total_pred_acc, prediction_loss=total_prediction_loss,
                                    k_prediction_acc=total_k_pred_acc))
            if hp.global_loss_weight != 0:
                metrics.update(dict(global_loss=total_global_loss, global_acc=total_global_accuracy))
            if hp.local_loss_weight != 0:
                metrics.update(dict(local_loss=total_local_loss, local_acc=total_local_accuracy))
            divide_dict(metrics, total_count)

            if not training and hp.use_scheduler:
                scheduler.step(metrics['net_loss'])
            if summary:
                print('train' if training else 'validation', epoch, metrics['net_loss'])
            else:
                print('train' if training else 'validation', epoch)
                print(json.dumps(metrics, indent=4))
            if not training and (metrics['net_loss'] < best_val_loss):
                best_val_loss = metrics['net_loss']
                print('update best to', best_val_loss)
                torch.save(network.state_dict(), 'best_network.pth')


if __name__ == '__main__':
    main(summary=False)
