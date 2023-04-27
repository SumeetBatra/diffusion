import pandas
import torch
import torch.nn as nn
import os
import pickle
import matplotlib.pyplot as plt
import numpy as np
import jax
import jax.numpy as jnp

from attrdict import AttrDict
from ribs.archives import CVTArchive, GridArchive
from ribs.visualize import cvt_archive_heatmap, grid_archive_heatmap
from typing import Optional
from RL.actor_critic import Actor, PGAMEActor
from models.vectorized import VectorizedActor
from envs.brax_custom.brax_env import make_vec_env_brax
from envs.brax_custom import reward_offset
from utils.normalize import ObsNormalizer


def save_heatmap(archive, heatmap_path, emitter_loc: Optional[tuple[float, ...]] = None,
                 forces: Optional[tuple[float, ...]] = None):
    """Saves a heatmap of the archive to the given path.
    Args:
        archive (GridArchive or CVTArchive): The archive to save.
        heatmap_path: Image path for the heatmap.
        emitter_loc: Where the emitter is in the archive. Determined by the measures of the mean solution point
        force: the direction that the emitter is being pushed towards. Determined by the gradient coefficients of
        the mean solution point
    """
    if isinstance(archive, GridArchive):
        plt.figure(figsize=(8, 6))
        grid_archive_heatmap(archive, emitter_loc=emitter_loc, forces=forces)
        plt.tight_layout()
        plt.savefig(heatmap_path)
    elif isinstance(archive, CVTArchive):
        plt.figure(figsize=(16, 12))
        cvt_archive_heatmap(archive)
        plt.tight_layout()
        plt.savefig(heatmap_path)
    plt.close('all')

    # return the image as a numpy array
    return plt.imread(heatmap_path)


def load_scheduler_from_checkpoint(scheduler_path, seed, device):
    assert os.path.exists(scheduler_path), f'Error! {scheduler_path=} does not exist'
    with open(scheduler_path, 'rb') as f:
        scheduler = pickle.load(f)
    # reinstantiate the pytorch generator with the correct seed
    scheduler.emitters[0].opt.problem._generator = torch.Generator(device=device)
    scheduler.emitters[0].opt.problem._generator.manual_seed(seed)
    return scheduler


def load_archive(archive_path):
    assert os.path.exists(archive_path), f'Error! {archive_path=} does not exist'
    with open(archive_path, 'rb') as f:
        archive = pickle.load(f)
    return archive


def evaluate(vec_agent, vec_env, num_dims, use_action_means=True, normalize_obs=False):
    '''
    Evaluate all agents for one episode
    :param vec_agent: Vectorized agents for vectorized inference
    :returns: Sum rewards and measures for all agents
    '''

    total_reward = np.zeros(vec_env.num_envs)
    traj_length = 0
    num_steps = 1000
    device = torch.device('cuda')

    obs = vec_env.reset()
    obs = obs.to(device)
    dones = torch.BoolTensor([False for _ in range(vec_env.num_envs)])
    all_dones = torch.zeros((num_steps, vec_env.num_envs)).to(device)
    measures_acc = torch.zeros((num_steps, vec_env.num_envs, num_dims)).to(device)
    measures = torch.zeros((vec_env.num_envs, num_dims)).to(device)

    if normalize_obs:
        repeats = vec_env.num_envs // vec_agent.num_models
        obs_mean = [normalizer.obs_rms.mean for normalizer in vec_agent.obs_normalizers]
        obs_mean = torch.vstack(obs_mean).to(device)
        obs_mean = torch.repeat_interleave(obs_mean, dim=0, repeats=repeats)
        obs_var = [normalizer.obs_rms.var for normalizer in vec_agent.obs_normalizers]
        obs_var = torch.vstack(obs_var).to(device)
        obs_var = torch.repeat_interleave(obs_var, dim=0, repeats=repeats)

    while not torch.all(dones):
        with torch.no_grad():
            if normalize_obs:
                obs = (obs - obs_mean) / torch.sqrt(obs_var + 1e-8)
            if use_action_means:
                acts = vec_agent(obs)
            else:
                acts, _, _ = vec_agent.get_action(obs)
            acts = acts.to(torch.float32)
            obs, rew, next_dones, infos = vec_env.step(acts)
            measures_acc[traj_length] = infos['measures']
            obs = obs.to(device)
            total_reward += rew.detach().cpu().numpy() * ~dones.cpu().numpy()
            dones = torch.logical_or(dones, next_dones.cpu())
            all_dones[traj_length] = dones.long().clone()
            traj_length += 1

    # the first done in each env is where that trajectory ends
    traj_lengths = torch.argmax(all_dones, dim=0) + 1
    # avg_traj_lengths = traj_lengths.to(torch.float32).reshape(
        # (vec_agent.num_models, vec_env.num_envs // vec_agent.num_models)).mean(dim=1).cpu().numpy()
    avg_traj_lengths = traj_lengths.to(torch.float32).cpu().numpy()
    # TODO: figure out how to vectorize this
    for i in range(vec_env.num_envs):
        measures[i] = measures_acc[:traj_lengths[i], i].sum(dim=0) / traj_lengths[i]
    # measures = measures.reshape(vec_agent.num_models, vec_env.num_envs // vec_agent.num_models, -1).mean(dim=1)

    metadata = np.array([{'traj_length': t} for t in avg_traj_lengths])
    # total_reward = total_reward.reshape((vec_agent.num_models, vec_env.num_envs // vec_agent.num_models))
    # total_reward = total_reward.mean(axis=1)
    return total_reward.reshape(-1, ), measures.reshape(-1, num_dims).detach().cpu().numpy(), metadata


def reconstruct_agents_from_vae(original_agents: list[Actor], vae: nn.Module, device,
                            inp_coefs: tuple[float] = (1.0, 1.0),
                            center_data: bool = False,
                            weight_denormalizer = None,
                            weight_normalizer = None,
                            obsnorm_denormalizer = None,
                            obsnorm_normalizer = None,):
    batch_size = len(original_agents)
    weights_dict = {}
    obsnorm_dict = {}
    state_dict = original_agents[0].state_dict()
    for key in state_dict.keys():
        if 'weight' in key or 'bias' in key or 'logstd' in key:
            params_batch = []
            shape = state_dict[key].shape
            for agent in original_agents:
                params_batch.append(agent.state_dict()[key])
            params_batch = torch.vstack(params_batch).reshape(batch_size, *tuple(shape))
            weights_dict[key] = params_batch
        
        if 'obs_normalizer' in key:
            params_batch = []
            for agent in original_agents:
                params_batch.append(agent.state_dict()[key])
            params_batch = torch.vstack(params_batch)
            obsnorm_dict[key] = params_batch
    
    gt_obsnorm_dict = {
        'obs_rms.mean': obsnorm_dict['obs_normalizer.obs_rms.mean'],
        'obs_rms.logstd' : torch.log(torch.sqrt(obsnorm_dict['obs_normalizer.obs_rms.var']+ 1e-8))
    }

    
    if center_data:
        weights_dict = weight_normalizer(weights_dict)
        gt_obsnorm_dict = obsnorm_normalizer(gt_obsnorm_dict)

    (rec_agents, rec_obsnorms), _ = vae(weights_dict, gt_obsnorm_dict)
    if original_agents[0].obs_normalizer is not None:
        for i, (orig_agent, rec_agent) in enumerate(zip(original_agents, rec_agents)):

            rec_obsnorm = {key: rec_obsnorms[key][i] for key in rec_obsnorms.keys()}
            if center_data:
                rec_agent_state_dict = rec_agent.state_dict()
                weight_denormalizer(rec_agent_state_dict)

                obsnorm_denormalizer(rec_obsnorm)
            
            rec_agent.load_state_dict(rec_agent_state_dict)

            rec_agent.obs_normalizer = orig_agent.obs_normalizer
            rec_agent.obs_normalizer.obs_rms.mean = rec_obsnorm['obs_rms.mean']
            rec_agent.obs_normalizer.obs_rms.var = torch.exp(2 * rec_obsnorm['obs_rms.logstd'])

    return rec_agents


def reconstruct_agents_from_ldm(original_agents, original_measures, vae: nn.Module, device, sampler, scale_factor,
                                diffusion_model):
    batch_size = len(original_measures)
    original_measures = torch.tensor(original_measures).reshape(batch_size, -1).to(device).to(torch.float32)
    samples = sampler.sample(diffusion_model, shape=[batch_size, 4, 4, 4], cond=original_measures)
    samples *= (1 / scale_factor)
    rec_agents = vae.decode(samples)

    if original_agents[0].obs_normalizer is not None:
        for orig_agent, rec_agent in zip(original_agents, rec_agents):
            rec_agent.obs_normalizer = orig_agent.obs_normalizer

    return rec_agents


def reevaluate_ppga_archive(env_cfg: AttrDict,
                            normalize_obs: bool,
                            normalize_returns: bool,
                            original_archive: GridArchive,
                            solution_batch_size: int = 100,
                            reconstructed_agents: bool = False,
                            vae: nn.Module = None,
                            sampler=None,
                            scale_factor=None,
                            diffusion_model=None,
                            save_path=None,
                            inp_coefs: tuple[float] = (1.0, 1.0),
                            center_data: bool = False,
                            weight_denormalizer = None,
                            weight_normalizer = None,
                            obsnorm_denormalizer = None,
                            obsnorm_normalizer = None,
                            ):
    num_sols = len(original_archive)
    print(f'{num_sols=}')
    env_cfg.env_batch_size = 50 * solution_batch_size
    vec_env = make_vec_env_brax(env_cfg)

    obs_shape, action_shape = vec_env.single_observation_space.shape, vec_env.single_action_space.shape
    device = torch.device('cuda')

    if vae is not None:
        vae.to(device)

    if diffusion_model is not None:
        diffusion_model.to(device)

    if reconstructed_agents:
        assert vae is not None and isinstance(vae, nn.Module), 'reconstructed_agents was set to true, but a valid VAE ' \
                                                               'model was not passed in'

    agents = []
    measures_list = []
    for elite in original_archive:
        agent = Actor(obs_shape[0], action_shape, normalize_obs, normalize_returns).deserialize(elite.solution).to(
            device)
        if normalize_obs:
            obs_norm = elite.metadata['obs_normalizer']
            if isinstance(obs_norm, dict):
                agent.obs_normalizer.load_state_dict(obs_norm)
            else:
                agent.obs_normalizer = obs_norm
        agents.append(agent)
        measures_list.append(elite.measures)
    agents = np.array(agents)
    measures_list = np.array(measures_list)

    all_objs, all_measures, all_metadata = [], [], []
    for i in range(0, num_sols, solution_batch_size):
        agent_batch = agents[i: i + solution_batch_size]
        measure_batch = measures_list[i: i + solution_batch_size]

        if reconstructed_agents:
            if diffusion_model is None:
                agent_batch = reconstruct_agents_from_vae(agent_batch, vae, device, inp_coefs = inp_coefs,
                                                          center_data=center_data,
                                                          weight_denormalizer=weight_denormalizer,
                                                          weight_normalizer=weight_normalizer,
                                                          obsnorm_denormalizer=obsnorm_denormalizer,
                                                          obsnorm_normalizer=obsnorm_normalizer)
            else:
                agent_batch = reconstruct_agents_from_ldm(agent_batch, measure_batch, vae, device, sampler,
                                                          scale_factor, diffusion_model)

        if env_cfg.env_batch_size % len(agent_batch) != 0 and len(original_archive) % solution_batch_size != 0:
            del vec_env
            env_cfg.env_batch_size = len(agent_batch) * 50
            vec_env = make_vec_env_brax(env_cfg)
        print(f'Evaluating solution batch {i}')
        vec_inference = VectorizedActor(agent_batch, Actor, obs_shape=obs_shape, action_shape=action_shape,
                                        normalize_obs=normalize_obs, normalize_returns=normalize_returns,
                                        deterministic=True).to(device)
        objs, measures, metadata = evaluate(vec_inference, vec_env, env_cfg.num_dims, normalize_obs=normalize_obs)
        all_objs.append(objs)
        all_measures.append(measures)
        all_metadata.append(metadata)

    all_objs, all_measures = np.concatenate(all_objs).reshape(1, -1).mean(axis=0), \
        np.concatenate(all_measures).reshape(1, -1, env_cfg.num_dims).mean(axis=0)
    all_metadata = np.concatenate(all_metadata).reshape(-1)

    print(f'{all_objs.shape=}, {all_measures.shape=}')

    # create a new archive
    archive_dims = [env_cfg.grid_size] * env_cfg.num_dims
    bounds = [(0., 1.0) for _ in range(env_cfg.num_dims)]
    new_archive = GridArchive(solution_dim=1,
                              dims=archive_dims,
                              ranges=bounds,
                              threshold_min=-10000,
                              seed=env_cfg.seed,
                              qd_offset=reward_offset[env_cfg.env_name])
    # add the re-evaluated solutions to the new archive
    new_archive.add(
        np.ones((len(all_objs), 1)),
        all_objs,
        all_measures,
        all_metadata
    )
    print(f'Coverage: {new_archive.stats.coverage} \n'
          f'Max fitness: {new_archive.stats.obj_max} \n'
          f'Avg Fitness: {new_archive.stats.obj_mean} \n'
          f'QD Score: {new_archive.offset_qd_score}')

    if save_path is not None:
        archive_fp = os.path.join(save_path, f'{env_cfg.env_name}_reeval_archive.pkl')
        with open(archive_fp, 'wb') as f:
            pickle.dump(new_archive, f)

    return new_archive


def archive_df_to_archive(archive_df: pandas.DataFrame, type: str = 'grid', **kwargs):
    if type == 'grid':
        archive_fn = GridArchive
    elif type == 'cvt':
        archive_fn = CVTArchive
    else:
        raise NotImplementedError
    solution_batch = archive_df.filter(regex='solution*').to_numpy()
    measures_batch = archive_df.filter(regex='measure*').to_numpy()
    obj_batch = archive_df.filter(regex='objective').to_numpy().flatten()
    metadata_batch = archive_df.filter(regex='metadata').to_numpy().flatten()
    archive = archive_fn(**kwargs)
    archive.add(solution_batch, obj_batch, measures_batch, metadata_batch)
    return archive


def sample_agents_from_archive(env_cfg, archive_df):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    q1 = archive_df.query('0 <= measure_0 < 0.5').query('0 <= measure_1 < 0.5').sample(10)
    q2 = archive_df.query('0.5 <= measure_0 < 1.0').query('0 <= measure_1 < 0.5').sample(10)
    q3 = archive_df.query('0 <= measure_0 < 0.5').query('0.5 <= measure_1 < 1.0').sample(10)
    q4 = archive_df.query('0.5 <= measure_0 < 1.0').query('0.5 <= measure_1 < 1.0').sample(10)

    agents = []
    for quad in [q1, q2, q3, q4]:
        sols = quad.filter(regex='solution*')
        md = quad['metadata'].to_numpy()
        for i, sol in enumerate(sols):
            agent = Actor(env_cfg.obs_shape, env_cfg.action_shape, normalize_obs=True,
                          normalize_returns=False).deserialize(sol)
            agent.to(device)
            agent.obs_normalizer.load_state_dict(md[i]['obs_normalizer'])
            agents.append(agent)
    return agents


if __name__ == '__main__':
    # evaluate_pga_me_archive('/home/sumeet/QDax/experiments/walker2d_checkpoint/checkpoint_00731')
    # load_and_eval_archive('/home/sumeet/QDax/experiments/pga_me_ant_uni_testrun_seed_1111/checkpoints/checkpoint_00399/ribs_archive.pkl')
    cp_path = '/home/sumeet/QDax/experiments/pga_me_walker2d_uni_baseline/pga_me_walker2d_uni_baseline_seed_1111_v2/checkpoints/checkpoint_00399'
    # archive = pgame_repertoire_to_pyribs_archive(cp_path)
    # archive.as_pandas().to_pickle(cp_path + 'pgame_archive.pkl')
