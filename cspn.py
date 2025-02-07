import logging
from typing import Dict, Type, Tuple, Optional

import numpy as np
import torch as th
import torch.nn.functional as F
from dataclasses import dataclass
from torch import nn

from layers import CrossProduct, Sum

from rat_spn import RatSpn, RatSpnConfig
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def print_cspn_params(cspn):
    def count_params(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Total params in CSPN: {count_params(cspn)}")
    print(f"Params to extract features from the conditional: {count_params(cspn.feat_layers)}")
    print(f"Params in MLP for the sum params, excluding the heads: {count_params(cspn.sum_layers)}")
    print(f"Params in the heads of the sum param MLPs: {sum([count_params(head) for head in cspn.sum_param_heads])}")
    print(f"Params in MLP for the dist params, excluding the heads: {count_params(cspn.dist_layers)}")
    print(f"Params in the heads of the dist param MLPs: "
          f"{count_params(cspn.dist_mean_head) + count_params(cspn.dist_std_head)}")


@dataclass
class CspnConfig(RatSpnConfig):
    is_ratspn: bool = False
    F_cond: tuple = 0
    feat_layers: list = None
    conv_kernel_size: int = 5
    conv_pooling_kernel_size: int = 3
    conv_pooling_stride: int = 3
    sum_param_layers: list = None
    dist_param_layers: list = None
    cond_layers_inner_act: Type[nn.Module] = nn.LeakyReLU

    def __setattr__(self, key, value):
        if hasattr(self, key):
            super().__setattr__(key, value)
        else:
            raise AttributeError(f"CspnConfig object has no attribute {key}")


class CSPN(RatSpn):
    def __init__(self, config: CspnConfig):
        """
        Create a CSPN

        Args:
            config (CspnConfig): Cspn configuration object.
        """
        super().__init__(config=config)
        self.config: CspnConfig = config
        self.dist_std_head = None
        self.dist_mean_head = None
        self.dist_layers = None
        self.sum_param_heads = None
        self.sum_layers = None
        self.feat_layers = None
        self.replace_layer_params()
        self.create_feat_layers(config.F_cond)

    @property
    def device(self):
        """Small hack to obtain the current device."""
        return self.dist_mean_head.bias.device

    def forward(self, x: th.Tensor, condition: th.Tensor = None, **kwargs) -> th.Tensor:
        """
        Forward pass through RatSpn. Computes the conditional log-likelihood P(X | C).

        Args:
            x: Input of shape [batch, weight_sets, in_features, channel].
                batch: Number of samples per weight set (= per conditional in the CSPN sense).
                weight_sets: In CSPNs, weights are different for each conditional. In RatSpn, this is 1.
            condition: Conditional for the distribution
        Returns:
            th.Tensor: Conditional log-likelihood P(X | C) of the input.
        """
        if condition is not None:
            self.set_params(condition)
        weight_sets = self._leaf.base_leaf.means.shape[0]
        if x.dim() == 3:
            assert x.shape[1] == weight_sets, \
                f"The input data shape says there are {x.shape[1]} samples of each conditional. " \
                f"But there are only samples of {x.shape[1]} conditionals, " \
                f"while the CSPN weights expect {weight_sets} conditionals! " \
                f"Did you forget to set the weights of the CSPN?"
        elif x.dim() == 2:
            assert x.shape[0] == weight_sets, \
                f"Dim of input is 2. This means that each input sample belongs to one conditional. " \
                f"But the number of samples ({x.shape[0]}) doesn't match the number of conditionals ({weight_sets})!" \
                f"Did you forget to set the weights of the CSPN?"
            x = x.unsqueeze(0)

        return super().forward(x, **kwargs)

    def recursive_entropy_approx(self, condition: th.Tensor = None, **kwargs) -> Tuple[th.Tensor, Optional[dict]]:
        if condition is not None:
            self.set_params(condition)
        return super().recursive_entropy_approx(**kwargs)

    def naive_entropy_approx(self, condition: th.Tensor = None, **kwargs) -> th.Tensor:
        if condition is not None:
            self.set_params(condition)
        return super().naive_entropy_approx(**kwargs)

    def huber_entropy_lb(self, condition: th.Tensor = None, **kwargs) -> th.Tensor:
        if condition is not None:
            self.set_params(condition)
        return super().huber_entropy_lb(**kwargs)

    def sum_node_entropies(self, condition=None, reduction='mean'):
        """
            Calculate the entropies of the hidden categorical random variables in the sum nodes
        """
        if condition is not None:
            self.set_params(condition)
        return super().sum_node_entropies(reduction)

    def sample(self, mode: str = None, condition: th.Tensor = None, class_index=None,
               evidence: th.Tensor = None, **kwargs):
        """
        Sample from the random variable encoded by the CSPN.

        Args:
            mode: Two sampling modes are supported:
                'index': Sampling mechanism with indexes, which are non-differentiable.
                'onehot': This sampling mechanism work with one-hot vectors, grouped into tensors.
                          This way of sampling is differentiable, but also takes almost twice as long.
            condition (th.Tensor): Batch of conditionals.
            class_index: See doc of RatSpn.sample_index_style()
            evidence: See doc of RatSpn.sample_index_style()
        """
        if condition is not None:
            self.set_params(condition)
        assert class_index is None or condition.shape[0] == len(class_index), \
            "The batch size of the condition must equal the length of the class index list if they are provided!"
        # TODO add assert to check dimension of evidence, if given.

        return super().sample(mode=mode, class_index=class_index, evidence=evidence, **kwargs)

    def sample_index_style(self, **kwargs):
        return self.sample(mode='index', **kwargs)

    def sample_onehot_style(self, **kwargs):
        return self.sample(mode='onehot', **kwargs)

    def replace_layer_params(self):
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                placeholder = th.zeros_like(layer.weight_param)
                del layer.weight_param
                layer.weight_param = placeholder
        placeholder = th.zeros_like(self.root.weight_param)
        del self.root.weight_param
        self.root.weight_param = placeholder

        placeholder = th.zeros_like(self._sampling_root.weight_param)
        del self._sampling_root.weight_param
        self._sampling_root.weight_param = placeholder

        placeholder = th.zeros_like(self._leaf.base_leaf.mean_param)
        del self._leaf.base_leaf.mean_param
        del self._leaf.base_leaf.std_param
        self._leaf.base_leaf.mean_param = placeholder
        self._leaf.base_leaf.std_param = placeholder

    def create_feat_layers(self, feature_dim: tuple):
        assert len(feature_dim) == 3 or len(feature_dim) == 1, \
            f"Don't know how to construct feature extraction layers for features of dim {len(feature_dim)}."
        if len(feature_dim) == 3:
            # feature_dim = (channels, rows, columns)
            assert False, "Adapt conv layer feat extraction to config.feat_layer_sizes"
            conv_kernel = self.config.conv_kernel_size
            pool_kernel = self.config.conv_pooling_kernel_size
            pool_stride = self.config.conv_pooling_stride
            nr_feat_layers = 0
            conv_layers = [] if nr_feat_layers > 0 else [nn.Identity()]
            for j in range(nr_feat_layers):
                # feature_dim = [int(np.floor((n - (pool_kernel-1) - 1)/pool_stride + 1)) for n in feature_dim]
                in_channels = feature_dim[0]
                if j == nr_feat_layers-1:
                    out_channels = 1
                else:
                    out_channels = feature_dim[0]
                conv_layers += [nn.Conv2d(in_channels, out_channels,
                                          kernel_size=(conv_kernel, conv_kernel), padding='same'),
                                nn.ReLU(),
                                nn.MaxPool2d(kernel_size=pool_kernel, stride=pool_stride),
                                nn.Dropout()]
            self.feat_layers = nn.Sequential(*conv_layers)
        elif len(feature_dim) == 1:
            if self.config.feat_layers:
                feat_layers = []
                layer_sizes = [feature_dim[0]] + self.config.feat_layers
                for j in range(len(layer_sizes) - 1):
                    feat_layers += [nn.Linear(layer_sizes[j], layer_sizes[j+1]),
                                    self.config.cond_layers_inner_act()]
            else:
                feat_layers = [nn.Identity()]
            self.feat_layers = nn.Sequential(*feat_layers)

        output_activation = nn.Identity

        feature_dim = int(np.prod(self.feat_layers(th.ones((1, *feature_dim))).shape))
        # print(f"The feature extraction layer for the CSPN conditional reduce the {int(np.prod(feature_dim))} "
              # f"inputs (e.g. pixels in an image) down to {feature_dim} features. These are the inputs of the "
              # f"MLPs which set the sum and dist params.")
        # sum_layer_sizes = [int(feature_dim * 10 ** (-i)) for i in range(1 + self.config.fc_sum_param_layers)]
        sum_layer_sizes = [feature_dim]
        if self.config.sum_param_layers:
            sum_layers = []
            sum_layer_sizes += self.config.sum_param_layers
            for j in range(len(sum_layer_sizes) - 1):
                act = self.config.cond_layers_inner_act if j < len(sum_layer_sizes) - 2 else output_activation
                sum_layers += [nn.Linear(sum_layer_sizes[j], sum_layer_sizes[j + 1]), act()]
        else:
            sum_layers = [nn.Identity()]
        self.sum_layers = nn.Sequential(*sum_layers)

        self.sum_param_heads = nn.ModuleList()
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                self.sum_param_heads.append(nn.Linear(sum_layer_sizes[-1], layer.weight_param.numel()))
                # print(f"Sum layer has {layer.weight_param.numel()} weights.")
        self.sum_param_heads.append(nn.Linear(sum_layer_sizes[-1], self.root.weight_param.numel()))
        # print(f"Root sum layer has {self.root.weight_param.numel()} weights.")

        # dist_layer_sizes = [int(feature_dim * 10 ** (-i)) for i in range(1 + self.config.fc_dist_param_layers)]
        dist_layer_sizes = [feature_dim]
        if self.config.dist_param_layers:
            dist_layers = []
            dist_layer_sizes += self.config.dist_param_layers
            for j in range(len(dist_layer_sizes) - 1):
                act = self.config.cond_layers_inner_act if j < len(dist_layer_sizes) - 2 else output_activation
                dist_layers += [nn.Linear(dist_layer_sizes[j], dist_layer_sizes[j + 1]), act()]
        else:
            dist_layers = [nn.Identity()]
        self.dist_layers = nn.Sequential(*dist_layers)

        self.dist_mean_head = nn.Linear(dist_layer_sizes[-1], self._leaf.base_leaf.mean_param.numel())
        self.dist_std_head = nn.Linear(dist_layer_sizes[-1], self._leaf.base_leaf.std_param.numel())

    def create_one_hot_in_channel_mapping(self):
        for lay in self._inner_layers:
            if isinstance(lay, CrossProduct):
                lay.one_hot_in_channel_mapping = F.one_hot(lay.unraveled_channel_indices).float().requires_grad_(False)

    def set_no_tanh_log_prob_correction(self):
        self._leaf.base_leaf.set_no_tanh_log_prob_correction()

    def set_params(self, feat_inp: th.Tensor):
        """
            Sets the weights of the sum and dist nodes, using the input from the conditional passed through the
            feature extraction layers.
            The weights of the sum nodes are normalized in log space (log-softmaxed) over the input channel dimension.
            The distribution parameters are bounded as well via the bounding function of the leaf layer.
            So in the RatSpn class, any normalizing and bounding must only be done if the weights are of dimension 4,
            meaning that it is not a Cspn.
        """
        num_conditionals = feat_inp.shape[0]
        features = self.feat_layers(feat_inp)
        features = features.flatten(start_dim=1)
        sum_weights_pre_output = self.sum_layers(features)

        # Set normalized sum node weights of the inner RatSpn layers
        i = 0
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                weight_shape = (num_conditionals, layer.in_features, layer.in_channels, layer.out_channels, layer.num_repetitions)
                weights = self.sum_param_heads[i](sum_weights_pre_output).view(weight_shape)
                layer.weight_param = F.log_softmax(weights, dim=2)
                i += 1
            else:
                layer.num_conditionals = num_conditionals

        # Set normalized weights of the root sum layer
        weight_shape = (num_conditionals, self.root.in_features, self.root.in_channels, self.root.out_channels, self.root.num_repetitions)
        weights = self.sum_param_heads[i](sum_weights_pre_output).view(weight_shape)
        self.root.weight_param = F.log_softmax(weights, dim=2)

        # Sampling root weights need to have 5 dims as well
        weight_shape = (num_conditionals, 1, 1, 1, 1)
        self._sampling_root.weight_param = th.ones(weight_shape, device=self.device).mul_(1/self.config.C).log_()

        # Set bounded weights of the Gaussian distributions in the leaves
        dist_param_shape = (num_conditionals, self._leaf.base_leaf.in_features, self.config.I, self.config.R)
        dist_weights_pre_output = self.dist_layers(features)
        dist_means = self.dist_mean_head(dist_weights_pre_output).view(dist_param_shape)
        dist_stds = self.dist_std_head(dist_weights_pre_output).view(dist_param_shape)
        dist_means = self._leaf.base_leaf.bounded_means(dist_means)
        dist_stds = self._leaf.base_leaf.bounded_stds(dist_stds)
        # if (dist_stds <= 0.0).any() or dist_stds.isnan().any():
            # print(1)
        # if (dist_stds ** 2 <= 0.0).any():
            # print(2)
        self._leaf.base_leaf.mean_param = dist_means
        # depending on self._leaf.base_leaf_stds_are_in_lin_space, the stds are in log space or in linear space
        self._leaf.base_leaf.std_param = dist_stds

    def clear_params(self):
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                weight_shape = (0, layer.in_features, layer.in_channels, layer.out_channels, layer.num_repetitions)
                layer.weight_param = th.zeros(weight_shape)
            else:
                layer.num_conditionals = 0

        # Set normalized weights of the root sum layer
        weight_shape = (0, self.root.in_features, self.root.in_channels, self.root.out_channels, self.root.num_repetitions)
        self.root.weight_param = th.zeros(weight_shape)

        # Sampling root weights need to have 5 dims as well
        weight_shape = (0, 1, 1, 1, 1)
        self._sampling_root.weight_param = th.zeros(weight_shape).to(self.device)

        # Set bounded weights of the Gaussian distributions in the leaves
        dist_param_shape = (0, self._leaf.base_leaf.in_features, self.config.I, self.config.R)
        self._leaf.base_leaf.mean_param = th.zeros(dist_param_shape)
        self._leaf.base_leaf.std_param = th.zeros(dist_param_shape)

    def save(self, *args, **kwargs):
        save_model = CSPN(self.config)
        save_model.load_state_dict(self.state_dict())
        th.save(save_model, *args, **kwargs)


