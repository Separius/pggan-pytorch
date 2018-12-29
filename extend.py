import os
import torch
from utils import cudize, enable_benchmark, load_model, save_pkl, simple_argparser


# TODO G_1(Z) = o1, o2, o3, o4 -> F(o1,o2,o3) = o4 and H(o2, o3, o4) = o1

def run_static(gen, disc, pop_size=128, stage=3, ratio=0.95, y=None):
    latent_size = gen.latent_size
    # TODO it should be based on z_distribution + add condition by instantiating a DataSet
    z1 = cudize(torch.randn(1, latent_size))
    z2 = z1 * ratio + cudize(torch.randn(pop_size, latent_size)) * (1.0 - ratio)
    fake = gen.consistent_forward(z1, z2, stage=stage, y=y)  # TODO add this function to generator
    scores = disc.consistent_forward(fake, y=y)  # TODO add this function to discriminator
    return fake[scores.argmax().item()], scores, z1, z2


params = dict(
    checkpoints_path='results/001-test',
    pattern='network-snapshot-{}-{}.dat',
    snapshot_epoch='000040',
    pop_size=128,
    g_stage=3,
    static_ratio=0.95,
    num_classes=0,
    static_output_location=None
)
params = simple_argparser(params)

# TODO change to first instantiate a G and D
G = load_model(
    os.path.join(params['checkpoints_path'], params['pattern'].format('generator', params['snapshot_epoch'])))
D = load_model(
    os.path.join(params['checkpoints_path'], params['pattern'].format('discriminator', params['snapshot_epoch'])))

enable_benchmark()
G = cudize(G)
D = cudize(D)
G.eval() #TODO also use torch.no_grad()
D.eval()

generated, scores, z1, z2 = run_static(G, D, pop_size=params['pop_size'], stage=params['g_stage'],
                                       ratio=params['static_ratio'])
print('min:', scores.min().item(), 'max:', scores.max().item(), 'mean:', scores.mean().item())
generated = generated.unsqueeze(0).data.cpu().numpy()
if params['static_output_location'] is None:
    loc = os.path.join(params['checkpoints_path'], 'static_generated.pkl')
else:
    loc = params['static_output_location']
save_pkl(loc, generated)