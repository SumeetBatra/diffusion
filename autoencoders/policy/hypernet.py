import torch
import torch.nn as nn
import torch.nn.functional as F

from autoencoders.policy.resnet3d import Encoder as Resnet3DEncoder
from autoencoders.autoencoder_base import AutoEncoderBase
from models.hyper.ghn_modules import *
from RL.actor_critic import Actor


class HypernetAutoEncoder(AutoEncoderBase):
    def __init__(self, emb_channels: int, z_channels: int):
        """
        :param emb_channels: is the number of dimensions in the quantized embedding space
        :param z_channels: is the number of channels in the embedding space
        """
        AutoEncoderBase.__init__(self, emb_channels, z_channels)

        # TODO: refactor
        obs_shape, action_shape = 18, np.array([6])
        self.encoder = ModelEncoder(obs_shape, action_shape, z_channels)

        # config dict for the hypernet decoder
        action_dim, obs_dim = 6, 18
        config = {}
        config['max_shape'] = (256, 256, 1, 1)
        config['num_classes'] = 2 * action_dim
        config['num_observations'] = obs_dim
        config['weight_norm'] = True
        config['ve'] = 1 > 1
        config['layernorm'] = True
        config['hid'] = 16
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.decoder = MLP_GHN(**config, debug_level=0, device=device)

        def make_actor():
            return Actor(obs_shape=18, action_shape=np.array([action_dim]), deterministic=True)

        self.dummy_actor = make_actor

    def decode(self, z: torch.Tensor):
        """
        ### Decode policies from latent representation
        :param z: is the latent representation with shape `[batch_size, emb_channels, z_height, z_height]`
        """
        # Map to embedding space from the quantized representation
        z = self.post_quant_conv(z)
        # Decode the image of shape `[batch_size, channels, height, width]`
        return self.decoder([self.dummy_actor() for _ in range(z.shape[0])], z)


class ModelEncoder(nn.Module):
    def __init__(self, obs_shape, action_shape, z_channels):
        super().__init__()
        dummy_actor = Actor(obs_shape=obs_shape, action_shape=action_shape, normalize_obs=True, normalize_returns=True)

        self.channels = [1, 32, 64, 64, 128, 256, 256, 512, 512, 512]
        self.kernel_sizes = [3, 3, 3, 3, 3, 3, 3, 3, 3, 2]
        self.strides = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
        self.paddings = [1, 1, 1, 1, 1, 1, 1, 1, 1, 0]

        self.max_pool_kernel_sizes = [2, 2, 2, 2, 2, 2, 2, 2, 2, 1]
        self.max_pool_strides = [2, 2, 2, 2, 2, 2, 2, 2, 2, 1]

        self.cnns = {}
        total_op_shape = 0
        self.list_of_weight_names = []
        self.list_of_real_weight_names = []
        for name, param in dummy_actor.named_parameters():

            self.list_of_real_weight_names.append(name)
            key_name = "_".join(name.split('.'))
            self.list_of_weight_names.append(key_name)

            if 'weight' in name:
                shape = (1, 1,) + tuple(param.data.shape)
                self.cnns[key_name], op_shape = self._create_cnn_backbone(shape)

            elif 'bias' in name:
                shape = (1, 1,) + tuple(param.data.shape) + (1,)
                self.cnns[key_name], op_shape = self._create_fc_backbone(shape)

            else:
                shape = (1,) + tuple(param.data.shape) + (1,)
                self.cnns[key_name], op_shape = self._create_fc_backbone(shape)

            total_op_shape += np.prod(op_shape)

        self.cnns = nn.ModuleDict(self.cnns)

        self.out = nn.Linear(total_op_shape, 8 * 4 * z_channels)

    # create a cnn backbone to extract features from tensor of shape (batch_size, *shape)
    def _create_cnn_backbone(self, shape, leaky_relu=False):

        cnn = nn.Sequential()

        def conv_relu(i, batch_norm=False):
            # shape of input: (batch, input_channel, height, width)
            input_channel = self.channels[i]
            output_channel = self.channels[i + 1]

            cnn_block = nn.Sequential()
            cnn_block.add_module(
                f'conv{i}',
                nn.Conv2d(input_channel, output_channel, self.kernel_sizes[i], self.strides[i], self.paddings[i])
            )

            if batch_norm:
                cnn_block.add_module(f'batchnorm{i}', nn.BatchNorm2d(output_channel))

            relu = nn.LeakyReLU(0.2, inplace=True) if leaky_relu else nn.ReLU(inplace=True)
            cnn_block.add_module(f'relu{i}', relu)
            cnn_block.add_module(f'maxpool_{i}', nn.MaxPool2d(self.max_pool_kernel_sizes[i], self.max_pool_strides[i]))

            cnn.add_module(f'cnn_block_{i}', cnn_block)

        def get_output_shape(input_shape, channel, kernel_size, stride, padding, pool_size=2, pool_stride=2):
            # input shape can be rectangular
            dim1 = (input_shape[2] - kernel_size + 2 * padding) // stride + 1
            dim2 = (input_shape[3] - kernel_size + 2 * padding) // stride + 1
            dim1 = (dim1 - pool_size) // pool_stride + 1
            dim2 = (dim2 - pool_size) // pool_stride + 1
            return (1, channel, int(dim1), int(dim2))

        for i in range(len(self.channels) - 2):
            channel = self.channels[i + 1]
            kernel_size = self.kernel_sizes[i]
            stride = self.strides[i]
            padding = self.paddings[i]
            pool_size = self.max_pool_kernel_sizes[i]
            pool_stride = self.max_pool_strides[i]

            output_shape = get_output_shape(shape, channel, kernel_size, stride, padding, pool_size, pool_stride)

            if min(output_shape) > 0:
                conv_relu(i, batch_norm=True)
                shape = output_shape
            else:
                break
        return cnn, shape

    def _create_fc_backbone(self, shape):
        fc = nn.Sequential()
        fc.add_module('fc1', nn.Linear(shape[2], 256))
        fc.add_module('relu1', nn.ReLU(inplace=True))
        fc.add_module('fc2', nn.Linear(256, 256))
        fc.add_module('relu2', nn.ReLU(inplace=True))
        fc.add_module('fc3', nn.Linear(256, 256))
        fc.add_module('relu3', nn.ReLU(inplace=True))
        return fc, (1, 1, 256, 1)

    def forward(self, x):
        outs = []
        for k in range(len(self.list_of_weight_names)):
            name = self.list_of_weight_names[k]
            real_name = self.list_of_real_weight_names[k]
            if 'weight' in name:
                out = self.cnns[name](x[real_name].unsqueeze(1))
                out = out.view(out.size(0), -1)
                outs.append(out)
            elif 'bias' in name:
                out = self.cnns[name](x[real_name].unsqueeze(1))
                out = out.view(out.size(0), -1)
                outs.append(out)
            else:
                out = self.cnns[name](x[real_name].unsqueeze(1))
                out = out.view(out.size(0), -1)
                outs.append(out)

        x = torch.cat(outs, dim=1)
        x = self.out(x)
        return x.reshape(-1, 8, 4, 4)

    def to(self, device):
        # self.dummy_actor.to(device)
        for cnn in self.cnns.values():
            cnn.to(device)
        self.out.to(device)
        return self