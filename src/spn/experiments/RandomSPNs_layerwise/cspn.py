import logging
from typing import Dict, Type

import numpy as np
import torch
from dataclasses import dataclass, field
from torch import nn

from spn.algorithms.layerwise.distributions import Leaf
from spn.algorithms.layerwise.layers import CrossProduct, Sum
from spn.algorithms.layerwise.type_checks import check_valid
from spn.algorithms.layerwise.utils import provide_evidence, SamplingContext
from spn.experiments.RandomSPNs_layerwise.distributions import IndependentMultivariate, RatNormal, truncated_normal_

from rat_spn import RatSpn, RatSpnConfig

logger = logging.getLogger(__name__)


@dataclass
class CspnConfig(RatSpnConfig):
    nr_conv_layers: int = 1
    conv_kernel_size: int = 5
    conv_pooling_kernel_size: int = 3
    conv_pooling_stride: int = 3
    # fc_sum_param_layer_sizes: list = field(default_factory=list)
    # fc_dist_param_layer_sizes: list = field(default_factory=list)

    def __setattr__(self, key, value):
        if hasattr(self, key):
            super().__setattr__(key, value)
        else:
            raise AttributeError(f"CspnConfig object has no attribute {key}")


class CSPN(RatSpn):
    def __init__(self, config: CspnConfig, feature_input_dim):
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
        self.conv_layers = None
        self.replace_layer_params()
        self.create_feat_layers(feature_input_dim)

    def replace_layer_params(self):
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                placeholder = torch.zeros_like(layer.weights)
                del layer.weights
                layer.weights = placeholder
        placeholder = torch.zeros_like(self.root.weights)
        del self.root.weights
        self.root.weights = placeholder

        placeholder = torch.zeros_like(self._leaf.base_leaf.means)
        del self._leaf.base_leaf.means
        del self._leaf.base_leaf.stds
        self._leaf.base_leaf.means = placeholder
        self._leaf.base_leaf.stds = placeholder

    def create_feat_layers(self, feature_input_dim: torch.Tensor):
        nr_conv_layers = self.config.nr_conv_layers
        conv_kernel = self.config.conv_kernel_size
        pool_kernel = self.config.conv_pooling_kernel_size
        pool_stride = self.config.conv_pooling_stride
        feature_dim = feature_input_dim
        if True:
            conv_layers = [] if nr_conv_layers > 0 else [nn.Identity()]
            for j in range(nr_conv_layers):
                feature_dim = [int(np.floor((n - (pool_kernel-1) - 1)/pool_stride + 1)) for n in feature_dim]

                conv_layers += [nn.Conv2d(1, 1, kernel_size=(conv_kernel, conv_kernel), padding='same'),
                                nn.ReLU(),
                                nn.MaxPool2d(kernel_size=pool_kernel, stride=pool_stride),
                                nn.Dropout()]
            self.conv_layers = nn.Sequential(*conv_layers)
            feature_dim = int(np.prod(feature_dim))
        elif feature_dim.dim() == 1:
            conv_layers = [] if nr_conv_layers > 0 else [nn.Identity()]
            for j in range(nr_conv_layers):
                conv_layers += [nn.Linear(feature_dim.shape[0], feature_dim.shape[0]), nn.ReLU()]
            self.conv_layers = nn.Sequential(*conv_layers)

        activation = nn.ReLU
        output_activation = nn.Identity

        sum_layer_sizes = [feature_dim]# + self.config.fc_sum_param_layer_sizes
        sum_layers = []
        for j in range(len(sum_layer_sizes) - 1):
            act = activation if j < len(sum_layer_sizes) - 2 else output_activation
            sum_layers += [nn.Linear(sum_layer_sizes[j], sum_layer_sizes[j + 1]), act()]
        self.sum_layers = nn.Sequential(*sum_layers)

        self.sum_param_heads = nn.ModuleList()
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                self.sum_param_heads.append(nn.Linear(sum_layer_sizes[-1], layer.weights.numel()))
        self.sum_param_heads.append(nn.Linear(sum_layer_sizes[-1], self.root.weights.numel()))

        dist_layer_sizes = [feature_dim]# + self.config.fc_dist_param_layer_sizes
        dist_layers = []
        for j in range(len(dist_layer_sizes) - 1):
            act = activation if j < len(dist_layer_sizes) - 2 else output_activation
            dist_layers += [nn.Linear(dist_layer_sizes[j], dist_layer_sizes[j + 1]), act()]
        self.dist_layers = nn.Sequential(*dist_layers)

        self.dist_mean_head = nn.Linear(dist_layer_sizes[-1], self._leaf.base_leaf.means.numel())
        self.dist_std_head = nn.Linear(dist_layer_sizes[-1], self._leaf.base_leaf.stds.numel())

    def forward(self, x: torch.Tensor, condition: torch.Tensor) -> torch.Tensor:
        self.compute_weights(condition)
        return super().forward(x)

    def sample(self, condition, n: int = None, class_index=None, evidence: torch.Tensor = None, is_mpe: bool = False):
        self.compute_weights(condition)
        assert n is None or condition.shape[0] == n, "The batch size of the condition must equal n if n is given!"
        assert class_index is None or condition.shape[0] == len(class_index), \
            "The batch size of the condition must equal the length of the class index list if they are provided!"
        # TODO add assert to check dimension of evidence, if given.
        return super().sample(n, class_index, evidence, is_mpe)

    def compute_weights(self, feat_inp):
        batch_size = feat_inp.shape[0]
        features = self.conv_layers(feat_inp)
        features = features.flatten(start_dim=1)
        sum_weights_pre_output = self.sum_layers(features)

        i = 0
        for layer in self._inner_layers:
            if isinstance(layer, Sum):
                weight_shape = (batch_size, layer.in_features, layer.in_channels, layer.out_channels, layer.num_repetitions)
                weights = self.sum_param_heads[i](sum_weights_pre_output).view(weight_shape)
                layer.weights = weights
                i += 1
        weight_shape = (batch_size, self.root.in_features, self.root.in_channels, self.root.out_channels, self.root.num_repetitions)
        weights = self.sum_param_heads[i](sum_weights_pre_output).view(weight_shape)
        self.root.weights = weights

        dist_param_shape = (batch_size, self._leaf.base_leaf.in_features, self.config.I, self.config.R)
        dist_weights_pre_output = self.dist_layers(features)
        dist_means = self.dist_mean_head(dist_weights_pre_output).view(dist_param_shape)
        dist_stds = self.dist_std_head(dist_weights_pre_output).view(dist_param_shape)
        self._leaf.base_leaf.means = dist_means
        self._leaf.base_leaf.stds = dist_stds