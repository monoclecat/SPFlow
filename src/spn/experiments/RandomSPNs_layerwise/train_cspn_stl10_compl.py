import os
import random
import sys
import time

import imageio
import numpy as np
import skimage
import torch
import torchvision
from torch import optim
from torch import nn
from torchvision import datasets, transforms
import matplotlib.pyplot as plt

from spn.experiments.RandomSPNs_layerwise.distributions import RatNormal
from spn.experiments.RandomSPNs_layerwise.cspn import CSPN, CspnConfig

from train_mnist import one_hot, count_params, ensure_dir, set_seed


def print_cspn_params(cspn: CSPN):
    print(f"Total params in CSPN: {count_params(cspn)}")
    print(f"Params to extract features from the conditional: {count_params(cspn.feat_layers)}")
    print(f"Params in MLP for the sum params, excluding the heads: {count_params(cspn.sum_layers)}")
    print(f"Params in the heads of the sum param MLPs: {sum([count_params(head) for head in cspn.sum_param_heads])}")
    print(f"Params in MLP for the dist params, excluding the heads: {count_params(cspn.dist_layers)}")
    print(f"Params in the heads of the dist param MLPs: "
          f"{count_params(cspn.dist_mean_head) + count_params(cspn.dist_std_head)}")


def time_delta(t_delta: float) -> str:
    """
    Convert a timestamp into a human readable timestring.
    Args:
        t_delta (float): Difference between two timestamps of time.time()

    Returns:
        Human readable timestring.
    """
    hours = round(t_delta // 3600)
    minutes = round(t_delta // 60 % 60)
    seconds = round(t_delta % 60)
    millisecs = round(t_delta % 1 * 1000)
    return f"{hours} hours, {minutes} minutes, {seconds} seconds, {millisecs} milliseconds"


def get_stl_loaders(dataset_dir, use_cuda, device, batch_size):
    """
    Get the STL10 pytorch data loader.

    Args:
        use_cuda: Use cuda flag.

    """
    kwargs = {"num_workers": 8, "pin_memory": True} if use_cuda else {}

    test_batch_size = batch_size

    transformer = transforms.Compose([transforms.ToTensor()])
    # Train data loader
    train_loader = torch.utils.data.DataLoader(
        datasets.STL10(dataset_dir, split='train+unlabeled', download=True, transform=transformer),
        batch_size=batch_size,
        shuffle=True,
        **kwargs,
    )

    # Test data loader
    test_loader = torch.utils.data.DataLoader(
        datasets.STL10(dataset_dir, split='test', transform=transformer),
        batch_size=test_batch_size,
        shuffle=True,
        **kwargs,
    )
    return train_loader, test_loader


def evaluate_model(model: torch.nn.Module, cut_fcn, insert_fcn, save_dir, device, loader, tag):
    """
    Description for method evaluate_model.

    Args:
        model (nn.Module): PyTorch module.
        cut_fcn: Function that cuts the cond out of the image
        insert_fcn: Function that inserts sample back into the image
        device: Execution device.
        loader: Data loader.
        tag (str): Tag for information.

    Returns:
        float: Tuple of loss and accuracy.
    """
    model.eval()
    loss = 0
    with torch.no_grad():
        n = 50
        for image, _ in loader:
            image = image.to(device)
            _, cond = cut_fcn(image)
            sample = model.sample(condition=cond)
            log_like = model(x=sample, condition=None)
            loss += -log_like.sum()

            if n > 0:
                insert_fcn(sample[:n], cond[:n])
                plot_samples(cond[:n], save_dir)
                n = 0
    loss /= len(loader.dataset)
    print("{} set: Average log-likelihood of samples: {:.4f}".format(tag, loss))


def plot_samples(x: torch.Tensor, path):
    """
    Plot a single sample with the target and prediction in the title.

    Args:
        x (torch.Tensor): Batch of input images. Has to be shape: [N, C, H, W].
    """
    # Normalize in valid range
    for i in range(x.shape[0]):
        x[i, :] = (x[i, :] - x[i, :].min()) / (x[i, :].max() - x[i, :].min())

    tensors = torchvision.utils.make_grid(x, nrow=10, padding=1).cpu()
    arr = tensors.permute(1, 2, 0).numpy()
    arr = skimage.img_as_ubyte(arr)
    imageio.imwrite(path, arr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--device', '-dev', type=str, default='cpu', choices=['cpu', 'cuda'])
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--epochs', '-ep', type=int, default=100)
    parser.add_argument('--batch_size', '-bs', type=int, default=256)
    parser.add_argument('--results_dir', type=str, default='.',
                        help='The base directory where the directory containing the results will be saved to.')
    parser.add_argument('--dataset_dir', type=str, default='../data',
                        help='The base directory to provide to the PyTorch Dataloader.')
    parser.add_argument('--exp_name', type=str, default='stl', help='Experiment name. The results dir will contain it.')
    parser.add_argument('--repetitions', '-R', type=int, default=5, help='Number of parallel CSPNs to learn at once. ')
    parser.add_argument('--cspn_depth', '-D', type=int, default=3, help='Depth of the CSPN.')
    parser.add_argument('--num_dist', '-I', type=int, default=5, help='Number of Gauss dists per pixel.')
    parser.add_argument('--num_sums', '-S', type=int, default=5, help='Number of sums per RV in each sum layer.')
    parser.add_argument('--dropout', type=float, default=0.0, help='Dropout to apply')
    parser.add_argument('--verbose', '-V', action='store_true', help='Output more debugging information when running.')
    args = parser.parse_args()

    set_seed(args.seed)

    results_dir = os.path.join(args.results_dir, f"results_{args.exp_name}")
    model_dir = os.path.join(results_dir, "models")
    sample_dir = os.path.join(results_dir, "samples")

    for d in [results_dir, model_dir, sample_dir, args.dataset_dir]:
        if not os.path.exists(d):
            os.makedirs(d)

    if args.device == "cpu":
        device = torch.device("cpu")
        use_cuda = False
    else:
        device = torch.device("cuda:0")
        use_cuda = True
        torch.cuda.benchmark = True

    batch_size = args.batch_size

    # The task is to do image in-painting - to fill in a cut-out square in the image.
    # The CSPN needs to learn the distribution of the cut-out given the image with the cut-out part set to 0 as
    # the conditional.
    img_size = (3, 96, 96)  # 3 channels
    center_cutout = (3, 32, 32)

    config = CspnConfig()
    # config also needed for standard RATSPN
    config.F = int(np.prod(center_cutout))
    config.F_cond = img_size
    config.R = args.repetitions
    config.D = args.cspn_depth
    config.I = args.num_dist
    config.S = args.num_sums
    config.C = 1
    config.dropout = 0.0
    config.leaf_base_class = RatNormal
    config.leaf_base_kwargs = {}
    # config specific to CSPN
    config.nr_conv_layers = 1
    config.conv_kernel_size = 3
    config.conv_pooling_kernel_size = 3
    config.conv_pooling_stride = 3
    config.fc_sum_param_layers = 1
    config.fc_dist_param_layers = 1

    # Construct Cspn from config
    model = CSPN(config)
    model = model.to(device)
    model.train()

    print("Using device:", device)
    print(model)
    print_cspn_params(model)
    # print("Number of pytorch parameters: ", count_params(model))

    # Define optimizer
    # loss_fn = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    train_loader, test_loader = get_stl_loaders(args.dataset_dir, use_cuda, batch_size=batch_size, device=device)

    cutout_rows = [img_size[1] // 2 - center_cutout[1] // 2, img_size[1] // 2 + center_cutout[1] // 2]
    cutout_cols = [img_size[2] // 2 - center_cutout[2] // 2, img_size[2] // 2 + center_cutout[2] // 2]

    def cut_out_center(image: torch.Tensor) -> (torch.Tensor, torch.Tensor):
        data = image[:, :, cutout_rows[0]:cutout_rows[1], cutout_cols[0]:cutout_cols[1]].clone()
        cond = image
        cond[:, :, cutout_rows[0]:cutout_rows[1], cutout_cols[0]:cutout_cols[1]] = 0
        return data, cond

    def insert_center(sample: torch.Tensor, cond: torch.Tensor):
        cond[:, :, cutout_rows[0]:cutout_rows[1], cutout_cols[0]:cutout_cols[1]] = sample.view(-1, *center_cutout)

    sample_interval = 10  # number of epochs
    for epoch in range(args.epochs):
        t_start = time.time()
        running_loss = 0.0
        cond = None
        for batch_index, (image, _) in enumerate(train_loader):
            # Send data to correct device
            image = image.to(device)
            data, cond = cut_out_center(image)
            # plt.imshow(data[0].permute(1, 2, 0))
            # plt.show()
            data = data.reshape(data.shape[0], -1)

            # evaluate_model(model, cut_out_center, insert_center, device, args.results_dir, train_loader, "Train")

            # Reset gradients
            optimizer.zero_grad()

            # Inference
            output: torch.Tensor = model(data, cond)

            # Compute loss
            loss = -output.mean()

            # Backprop
            loss.backward()
            optimizer.step()
            # scheduler.step()

            # Log stuff
            running_loss += loss.item()
            if args.verbose:
                batch_delta = time_delta((time.time()-t_start)/(batch_index+1))
                print(f"Epoch {epoch} ({100.0 * batch_index / len(train_loader):.1f}%) "
                      f"Avg. loss: {running_loss/(batch_index+1):.2f} - Batch {batch_index} - "
                      f"Avg. batch time {batch_delta}",
                      end="\r")

        t_delta = time_delta(time.time()-t_start)
        print("Train Epoch: {} took {}".format(epoch, t_delta))
        if epoch % sample_interval == (sample_interval-1):
            print("Saving and evaluating model ...")
            torch.save(model, os.path.join(model_dir, f"epoch-{epoch:03}.pt"))
            save_dir = os.path.join(sample_dir, f"epoch-{epoch:03}.png")
            evaluate_model(model, cut_out_center, insert_center, save_dir, device, train_loader, "Train")
            evaluate_model(model, cut_out_center, insert_center, save_dir, device, test_loader, "Test")


