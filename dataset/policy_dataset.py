import pickle
import pandas
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from typing import Union
from pandas import DataFrame
from torch.utils.data import Dataset, DataLoader
from RL.actor_critic import Actor
from ribs.archives._elite import EliteBatch
from tqdm import tqdm


def readonly(arr):
    """Sets an array to be readonly."""
    arr.flags.writeable = False
    return arr

def preprocess_model(model: nn.Module, mlp_shape: Union[list, tuple]):
    '''Performs the same ops as ElitesDataset._preprocess_data(), except on a single model'''
    largest_layer_size = max(mlp_shape)
    params = model.serialize()
    padded_policy = []
    arr_idx = 0
    for name, param in model.named_parameters():
        if name == 'actor_logstd':
            arr_idx += mlp_shape[-1]
            continue
        if 'weight' in name:
            weight_cols = param.data.shape[1]
            shape = tuple(param.data.shape)
            length = np.product(shape)
            block = params[arr_idx: arr_idx + length]
            block = torch.from_numpy(np.reshape(block, shape))
        else:
            # bias term
            orig_shape = length = param.data.shape[0]
            block = params[arr_idx: arr_idx + length]
            block = torch.from_numpy(block).view(-1, 1).repeat((1, weight_cols)).reshape(orig_shape, weight_cols)
            shape = tuple(block.shape)
            # pad all sides of the weight matrix so that the final shape is (largest_layer_size x largest_layer_size)
        padding = ((largest_layer_size - shape[1]) // 2, (largest_layer_size - shape[1]) // 2,
                   (largest_layer_size - shape[0]) // 2, (largest_layer_size - shape[0]) // 2)
        block = F.pad(block, pad=padding)
        padded_policy.append(block)
        arr_idx += length
    padded_policy = torch.stack(padded_policy).unsqueeze(dim=0).type(
        torch.float32)  # this makes it (1 x depth x largest_layer_size x largest_layer_size)
    return padded_policy


def postprocess_model(model_in: nn.Module, padded_params: torch.Tensor, mlp_shape: Union[list, tuple], return_model=True, deterministic=False):
    '''Reconstruct the original policy given the padded policy tensor'''
    largest_layer_size = max(mlp_shape)
    i = 0
    all_params = []
    for name, param in model_in.named_parameters():
        if name == 'actor_logstd':
            if not deterministic:
                all_params.append(np.zeros(mlp_shape[-1]))
            continue
        if 'weight' in name:
            shape = weight_shape = tuple(param.data.shape)
            padding = ((largest_layer_size - shape[0]) // 2, (largest_layer_size - shape[0]) // 2,
                       (largest_layer_size - shape[1]) // 2, (largest_layer_size - shape[1]) // 2)
            layer = padded_params[0][i]

            actual_params = layer[padding[0]:padding[0] + param.data.shape[0],
                            padding[2]:padding[2] + param.data.shape[1]]
        else:
            # bias term
            shape = weight_shape
            padding = ((largest_layer_size - shape[0]) // 2, (largest_layer_size - shape[0]) // 2,
                       (largest_layer_size - shape[1]) // 2, (largest_layer_size - shape[1]) // 2)
            layer = padded_params[0][i]

            if padding[0] != 0:
                actual_params = layer[padding[0]: padding[0] + shape[0], padding[0]]
            else:
                actual_params = layer[:, padding[2]]

        all_params.append(actual_params.detach().cpu().numpy().flatten())
        i += 1
    all_params = np.concatenate(all_params).flatten()
    if return_model:
        return model_in.deserialize(all_params)
    return all_params


class ElitesDataset(Dataset):
    def __init__(self, archive_dfs: list[DataFrame], mlp_shape: Union[tuple, list], dummy_model: nn.Module):
        self.elites_list = []
        self.dummy_model = dummy_model
        self._preprocess_data(archive_dfs, mlp_shape)

    def __len__(self):
        return len(self.elites_list)

    def __getitem__(self, item):
        return self.elites_list[item]

    def _preprocess_data(self, archive_dfs, mlp_shape):
        '''
        Given a mlp of depth D and largest hidden layer of size L, we will pad all the network weights
        to produce tensors of size (L x L x D)
        '''
        archive_df = pandas.concat(archive_dfs)
        params_batch = archive_df.filter(regex='solution*').to_numpy()
        measures_batch = archive_df.filter(regex='measure*').to_numpy()
        largest_layer_size = max(mlp_shape)

        padded_elites = []
        for params, measure in zip(params_batch, measures_batch):
            padded_policy = []
            arr_idx = 0
            for name, param in self.dummy_model.named_parameters():
                if name == 'actor_logstd':
                    arr_idx += mlp_shape[-1]
                    continue
                if 'weight' in name:
                    weight_cols = param.data.shape[1]
                    shape = tuple(param.data.shape)
                    length = np.product(shape)
                    block = params[arr_idx: arr_idx + length]
                    block = torch.from_numpy(np.reshape(block, shape))
                else:
                    # bias term
                    orig_shape = length = param.data.shape[0]
                    block = params[arr_idx: arr_idx + length]
                    block = torch.from_numpy(block).view(-1, 1).repeat((1, weight_cols)).reshape(orig_shape, weight_cols)
                    shape = tuple(block.shape)
                # pad all sides of the weight matrix so that the final shape is (largest_layer_size x largest_layer_size)
                padding = ((largest_layer_size - shape[1]) // 2, (largest_layer_size - shape[1]) // 2,
                           (largest_layer_size - shape[0]) // 2, (largest_layer_size - shape[0]) // 2)
                block = F.pad(block, pad=padding)
                padded_policy.append(block)
                arr_idx += length
            padded_policy = torch.stack(padded_policy).unsqueeze(dim=0).type(torch.float32)  # this makes it (1 x depth x largest_layer_size x largest_layer_size)
            padded_elites.append((padded_policy, measure))
        self.elites_list = padded_elites


class ShapedEliteDataset(Dataset):
    def __init__(self, archive_dfs: list[DataFrame], obs_dim, action_shape, device, normalize_obs=False):
        archive_df = pandas.concat(archive_dfs)

        self.obs_dim = obs_dim
        self.action_shape = action_shape
        self.device = device

        self.elites_list = archive_df.filter(regex='solution*').to_numpy()
        self.measures_list = archive_df.filter(regex='measure*').to_numpy()

        self.normalize_obs = normalize_obs
        self.metadata = archive_df.filter(regex='metadata*').to_numpy()

    def __len__(self):
        return self.elites_list.shape[0]

    def __getitem__(self, item):
        params, measures = self.elites_list[item], self.measures_list[item]
        weights_dict = Actor(self.obs_dim, self.action_shape, False, True).to(self.device).get_deserialized_weights(params)
        if self.normalize_obs:
            obs_normalizer = self.metadata[item]['obs_normalizer']
            weights_dict['rms_mean'] = obs_normalizer.obs_rms.mean
            weights_dict['rms_var'] = obs_normalizer.obs_rms.var
            weights_dict['rms_count'] = obs_normalizer.obs_rms.count
        return weights_dict, measures




if __name__ == '__main__':
    archive_df_path = '/home/sumeet/QDPPO/experiments/ppga_halfcheetah_100x100_no_obs_norm/1111/checkpoints/cp_00002000/archive_df_00002000.pkl'
    with open(archive_df_path, 'rb') as f:
        archive_df = pickle.load(f)

    mlp_shape = (128, 128, 6)
    obs_dim, action_shape = 18, np.array([6])

    dummy_agent = Actor(obs_shape=18, action_shape=np.array([6]))
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    ds = ShapedEliteDataset([archive_df], obs_dim=obs_dim, action_shape=action_shape, device=device, normalize_obs=True)




