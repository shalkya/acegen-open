import datetime
import json
import logging
import os
import random
import shutil
from copy import deepcopy
from pathlib import Path

import hydra
import numpy as np

import torch
import torch.nn.functional as F
import tqdm
import yaml
from acegen.data import (
    remove_duplicated_keys,
    remove_keys_in_reference,
    smiles_to_tensordict,
)
from acegen.models import adapt_state_dict, create_gru_actor
from acegen.rl_env import sample_completed_smiles, SMILESEnv
from acegen.transforms import PenaliseRepeatedSMILES, SMILESReward
from acegen.vocabulary import SMILESVocabulary
from omegaconf import OmegaConf

from tensordict import TensorDict
from torchrl.data import (
    LazyTensorStorage,
    TensorDictMaxValueWriter,
    TensorDictReplayBuffer,
)
from torchrl.data.tensor_specs import UnboundedContinuousTensorSpec
from torchrl.envs import (
    CatFrames,
    InitTracker,
    StepCounter,
    TensorDictPrimer,
    TransformedEnv,
    UnsqueezeTransform,
)
from torchrl.record.loggers import get_logger


try:
    import molscore
    from molscore.manager import MolScore

    _has_molscore = True
except ImportError as err:
    _has_molscore = False
    MOLSCORE_ERR = err


@hydra.main(config_path=".", config_name="config", version_base="1.2")
def main(cfg: "DictConfig"):

    # Set seeds
    seed = cfg.seed
    random.seed(int(seed))
    np.random.seed(int(seed))
    torch.manual_seed(int(seed))

    # Save config
    current_time = datetime.datetime.now()
    timestamp_str = current_time.strftime("%Y_%m_%d_%H%M%S")
    save_dir = f"{cfg.log_dir}_{timestamp_str}"
    os.makedirs(save_dir)
    with open(Path(save_dir) / "config.yaml", "w") as yaml_file:
        cfg_dict = OmegaConf.to_container(cfg, resolve=True)
        yaml.dump(cfg_dict, yaml_file, default_flow_style=False)

    # Get available device
    device = (
        torch.device("cuda:0") if torch.cuda.device_count() > 0 else torch.device("cpu")
    )

    # Load Vocabulary
    ckpt = Path(__file__).resolve().parent.parent.parent / "priors" / cfg.vocabulary
    with open(ckpt, "r") as f:
        tokens = f.read().splitlines()
    tokens_dict = dict(zip(tokens, range(len(tokens))))
    vocabulary = SMILESVocabulary.create_from_dict(
        tokens_dict, start_token="GO", end_token="EOS"
    )

    # Models
    ####################################################################################################################

    ckpt = torch.load(
        Path(__file__).resolve().parent.parent.parent / "priors" / cfg.prior
    )
    actor_training, actor_inference = create_gru_actor(len(vocabulary))
    actor_inference.load_state_dict(
        adapt_state_dict(ckpt, actor_inference.state_dict())
    )
    actor_training.load_state_dict(adapt_state_dict(ckpt, actor_training.state_dict()))
    actor_inference = actor_inference.to(device)
    actor_training = actor_training.to(device)

    prior = deepcopy(actor_training)

    # Environment
    ####################################################################################################################

    # Create a transform to populate initial tensordict with rnn recurrent states equal to 0.0
    primers = actor_training.rnn_spec.expand(cfg.num_envs)
    rhs_primer = TensorDictPrimer(primers)

    env_kwargs = {
        "start_token": vocabulary.vocab[vocabulary.start_token],
        "end_token": vocabulary.vocab[vocabulary.end_token],
        "length_vocabulary": len(vocabulary),
        "batch_size": cfg.num_envs,
        "device": device,
    }

    def create_env_fn():
        """Create a single RL rl_env."""
        env = SMILESEnv(**env_kwargs)
        env = TransformedEnv(env)
        env.append_transform(
            UnsqueezeTransform(
                in_keys=["observation"], out_keys=["SMILES"], unsqueeze_dim=-1
            )
        )
        env.append_transform(
            CatFrames(
                N=100,
                dim=-1,
                padding="constant",
                in_keys=["SMILES"],
                out_keys=["SMILES"],
                padding_value=-1,
            )
        )
        env.append_transform(StepCounter())
        env.append_transform(InitTracker())
        env.append_transform(rhs_primer)
        return env

    # Independent transforms
    ####################################################################################################################

    # 1. Define scoring transform
    # it is more efficient to score all SMILES in a single call after data collection
    if not _has_molscore:
        raise RuntimeError(
            "MolScore library not found, unable to create a scoring function. "
        ) from MOLSCORE_ERR

    if cfg.molscore is None:
        raise RuntimeError(
            "MolScore config file not provided, unable to create a scoring function. "
            "Please provide a config file,"
            "e.g. ../MolScore/molscore/configs/GuacaMol/Albuterol_similarity.json "
        )

    # Save molscore output. Also redirect output to save_dir
    cfg.molscore = shutil.copy(cfg.molscore, save_dir)
    data = json.load(open(cfg.molscore, "r"))
    data["output_dir"] = save_dir
    json.dump(data, open(cfg.molscore, "w"), indent=4)

    # Create scoring function
    scoring = MolScore(model_name="ppo", task_config=cfg.molscore)
    scoring.configs["save_dir"] = save_dir
    scoring_function = scoring.score

    # Create reward transform
    rew_transform = SMILESReward(
        reward_function=scoring_function, vocabulary=vocabulary
    )

    # 2. Define a transform to penalise repeated SMILES
    penalty_transform = None
    if cfg.penalize_repetition is True:
        penalty_transform = PenaliseRepeatedSMILES(
            check_duplicate_key="SMILES",
            in_key="reward",
            out_key="reward",
            penalty=cfg.repetition_penalty,
            device=device,
        )

    # Replay buffer
    ####################################################################################################################

    experience_replay_buffer = TensorDictReplayBuffer(
        storage=LazyTensorStorage(100, device=device),
        batch_size=cfg.replay_batch_size,
        writer=TensorDictMaxValueWriter(rank_key="priority"),
    )

    # Optimizer
    ####################################################################################################################

    optim = torch.optim.Adam(
        actor_training.parameters(),
        lr=cfg.lr,
        eps=cfg.eps,
        weight_decay=cfg.weight_decay,
    )

    # Logger
    ####################################################################################################################

    logger = None
    if cfg.logger_backend:
        logger = get_logger(
            cfg.logger_backend,
            logger_name="reinvent",
            experiment_name=cfg.agent_name,
            project_name=cfg.experiment_name,
        )

    # Training loop
    ####################################################################################################################

    total_done = 0
    collected_frames = 0
    env = create_env_fn()
    sigma = cfg.sigma
    frames_in_batch = cfg.num_envs

    for _ in tqdm.tqdm(range(0, cfg.total_frames, frames_in_batch)):

        data = sample_completed_smiles(policy=actor_inference, environment=env)

        log_info = {}
        total_done += frames_in_batch
        collected_frames += frames_in_batch

        # Compute reward
        data = rew_transform(data)

        # Register smiles lengths and real rewards
        mask = data.get("mask").squeeze(-1)
        done = data.get(("next", "done")).squeeze(-1) * mask
        episode_rewards = data["next", "reward"][done]
        episode_length = data["next", "step_count"][done]
        if len(episode_rewards) > 0:
            log_info.update(
                {
                    "train/total_smiles": total_done,
                    "train/reward": episode_rewards.mean().item(),
                    "train/min_reward": episode_rewards.min().item(),
                    "train/max_reward": episode_rewards.max().item(),
                    "train/episode_length": episode_length.sum().item() / len(
                        episode_length
                    ),
                }
            )

        # removed_duplicated = remove_duplicated_keys(data.clone(), key="action")

        # Select only the necessary tensors
        data = data.select(
            "action",
            "done",
            "mask",
            "is_init",
            "observation",
            "recurrent_state",
            "sample_log_prob",
            "terminated",
            ("next", "done"),
            ("next", "mask"),
            ("next", "is_init"),
            ("next", "observation"),
            ("next", "recurrent_state"),
            ("next", "terminated"),
            ("next", "reward"),
        )

        # Identify unique sequences
        arr = data.get("action").cpu().numpy()
        arr_ = np.ascontiguousarray(arr).view(
            np.dtype((np.void, arr.dtype.itemsize * arr.shape[1]))
        )
        _, idxs = np.unique(arr_, return_index=True)
        unique_idxs = torch.tensor(np.sort(idxs), dtype=torch.int32, device=device)
        data = data[unique_idxs]

        # assert data.shape[0] == removed_duplicated.shape[0]

        # Compute prior log_probs
        with torch.no_grad():
            prior_logits = prior(data.select(*prior.in_keys).clone()).get("logits")
            prior_log_prob = F.log_softmax(prior_logits, dim=-1)
            prior_log_prob = prior_log_prob.gather(
                -1, data.get("action").unsqueeze(-1)
            ).squeeze(-1)

        # Compute loss
        agent_logits = actor_training(data.select(*actor_training.in_keys).clone()).get(
            "logits"
        )
        agent_log_prob = F.log_softmax(agent_logits, dim=-1)
        agent_log_prob = agent_log_prob.gather(
            -1, data.get("action").unsqueeze(-1)
        ).squeeze(-1)

        agent_likelihood = (agent_log_prob * mask).sum(-1)
        prior_likelihood = (prior_log_prob * mask).sum(-1)
        score = data.get(("next", "reward")).squeeze(-1).sum(-1)
        augmented_likelihood = prior_likelihood + sigma * score
        loss = torch.pow((augmented_likelihood - agent_likelihood), 2)

        # Compute experience replay loss
        if (
            cfg.experience_replay
            and len(experience_replay_buffer) > cfg.replay_batch_size
        ):
            import ipdb

            ipdb.set_trace()
            replay_batch = experience_replay_buffer.sample()
            exp_agent_likelihood = (
                actor_training(replay_batch).get("sample_log_prob").sum(-1)
            )
            exp_augmented_likelihood = replay_batch.get(
                "prior_log_prob"
            ) + sigma * replay_batch.get("reward")
            exp_loss = torch.pow((exp_augmented_likelihood - exp_agent_likelihood), 2)
            loss = torch.cat((loss, exp_loss), 0)
            agent_likelihood = torch.cat((agent_likelihood, exp_agent_likelihood), 0)

        # Average loss over the batch
        loss = loss.mean()

        # Add regularizer that penalizes high likelihood for the entire sequence
        loss_p = -(1 / agent_likelihood).mean()
        loss += 5 * 1e3 * loss_p

        # Calculate gradients and make an update to the network weights
        optim.zero_grad()
        loss.backward()
        optim.step()

        # Then add new experience to replay buffer
        if cfg.experience_replay is True:

            replay_data = data.get("next").clone()
            replay_data.set("prior_log_prob", prior_log_prob)
            reward = replay_data.get("reward").squeeze(-1)
            replay_data.set("priority", reward)

            # Remove duplicated SMILES
            replay_data = remove_duplicated_keys(replay_data, "observation")

            # Remove SMILES that are already in the replay buffer
            if len(experience_replay_buffer) > 0:
                td = remove_keys_in_reference(
                    experience_replay_buffer[:], td, "observation"
                )

            # Add data to the replay buffer
            indices = experience_replay_buffer.extend(replay_data)

            # Update priorities with the rewards
            experience_replay_buffer.update_priority(indices, reward)

        # Log
        if logger:
            for key, value in log_info.items():
                logger.log_scalar(key, value, collected_frames)


if __name__ == "__main__":
    main()
