import itertools
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Tuple, Union

import gymnasium as gym
import torch as th
import numpy as np
from omegaconf import DictConfig
from torch import nn

from hsuanwu.xploit.agent.base import BaseAgent
from hsuanwu.xploit.agent.networks import OnPolicyDecoupledActorCritic, get_network_init
from hsuanwu.xploit.storage import DecoupledRolloutStorage as Storage

MATCH_KEYS = {
    "trainer": "OnPolicyTrainer",
    "storage": ["VanillaRolloutStorage"],
    "distribution": ["Categorical", "DiagonalGaussian", "Bernoulli"],
    "augmentation": [],
    "reward": [],
}

DEFAULT_CFGS = {
    ## TODO: Train setup
    "device": "cpu",
    "seed": 1,
    "num_train_steps": 25000000,
    "num_steps": 256,  # The sample length of per rollout.
    ## TODO: Test setup
    "test_every_episodes": 10,  # only for on-policy algorithms
    "num_test_episodes": 10,
    ## TODO: xploit part
    "encoder": {
        "name": "EspeholtResidualEncoder",
        "observation_space": dict(),
        "feature_dim": 256,
    },
    "agent": {
        "name": "DAAC",
        "observation_space": dict(),
        "action_space": dict(),
        "device": str,
        "feature_dim": int,
        "lr": 5e-4,
        "eps": 0.00001,
        "hidden_dim": 256,
        "clip_range": 0.2,
        "clip_range_vf": 0.2,
        "policy_epochs": 1,
        "value_freq": 1,
        "value_epochs": 9,
        "vf_coef": 0.5,
        "ent_coef": 0.01,
        "adv_coef": 0.25,
        "aug_coef": 0.1,
        "max_grad_norm": 0.5,
    },
    "storage": {"name": "VanillaRolloutStorage"},
    ## TODO: xplore part
    "distribution": {"name": "Categorical"},
    "augmentation": {"name": None},
    "reward": {"name": None},
}


class DAAC(BaseAgent):
    """Decoupled Advantage Actor-Critic (DAAC) agent.
        When 'augmentation' module is invoked, this learner will transform into
        Data Regularized Decoupled Actor-Critic (DrAAC) agent.
        Based on: https://github.com/rraileanu/idaac

    Args:
        observation_space (Space or DictConfig): The observation space of environment. When invoked by Hydra,
            'observation_space' is a 'DictConfig' like {"shape": observation_space.shape, }.
        action_space (Space or DictConfig): The action space of environment. When invoked by Hydra,
            'action_space' is a 'DictConfig' like
            {"shape": action_space.shape, "n": action_space.n, "type": "Discrete", "range": [0, n - 1]} or
            {"shape": action_space.shape, "type": "Box", "range": [action_space.low[0], action_space.high[0]]}.
        device (str): Device (cpu, cuda, ...) on which the code should be run.
        feature_dim (int): Number of features extracted by the encoder.
        lr (float): The learning rate.
        eps (float): Term added to the denominator to improve numerical stability.

        hidden_dim (int): The size of the hidden layers.
        clip_range (float): Clipping parameter.
        clip_range_vf (float): Clipping parameter for the value function.
        policy_epochs (int): Times of updating the policy network.
        value_freq (int): Update frequency of the value network.
        value_epochs (int): Times of updating the value network.
        vf_coef (float): Weighting coefficient of value loss.
        ent_coef (float): Weighting coefficient of entropy bonus.
        aug_coef (float): Weighting coefficient of augmentation loss.
        adv_ceof (float): Weighting coefficient of advantage loss.
        max_grad_norm (float): Maximum norm of gradients.
        network_init_method (str): Network initialization method name.

    Returns:
        DAAC learner instance.
    """

    def __init__(
        self,
        observation_space: Union[gym.Space, DictConfig],
        action_space: Union[gym.Space, DictConfig],
        device: str,
        feature_dim: int,
        lr: float,
        eps: float,
        hidden_dim: int,
        clip_range: float,
        clip_range_vf: float,
        policy_epochs: int,
        value_freq: int,
        value_epochs: int,
        vf_coef: float,
        ent_coef: float,
        aug_coef: float,
        adv_coef: float,
        max_grad_norm: float,
        network_init_method: str
    ) -> None:
        super().__init__(observation_space, action_space, device, feature_dim, lr, eps)

        self.policy_epochs = policy_epochs
        self.value_freq = value_freq
        self.value_epochs = value_epochs
        self.clip_range = clip_range
        self.clip_range_vf = clip_range_vf
        self.vf_coef = vf_coef
        self.ent_coef = ent_coef
        self.aug_coef = aug_coef
        self.adv_coef = adv_coef
        self.max_grad_norm = max_grad_norm
        self.network_init_method = network_init_method

        # create models
        self.ac = OnPolicyDecoupledActorCritic(
            obs_shape=self.obs_shape,
            action_dim=self.action_dim,
            action_type=self.action_type,
            feature_dim=feature_dim,
            hidden_dim=hidden_dim,
        )

        self.num_policy_updates = 0
        self.prev_total_critic_loss = 0

    def train(self, training: bool = True) -> None:
        """Set the train mode.

        Args:
            training (bool): True (training) or False (testing).

        Returns:
            None.
        """
        self.training = training
        self.ac.train(training)

    def integrate(self, **kwargs) -> None:
        """Integrate agent and other modules (encoder, reward, ...) together"""
        # set encoder and distribution
        self.dist = kwargs["dist"]
        self.ac.actor_encoder = kwargs["encoder"]
        self.ac.critic_encoder = deepcopy(kwargs["encoder"])
        self.ac.dist = kwargs["dist"]
        # network initialization
        self.ac.apply(get_network_init(self.network_init_method))
        # to device
        self.ac.to(self.device)
        # create optimizers
        self.actor_params = itertools.chain(
            self.ac.actor_encoder.parameters(), 
            self.ac.actor.parameters(), 
            self.ac.gae.parameters())
        self.critic_params = itertools.chain(
            self.ac.critic_encoder.parameters(),
            self.ac.critic.parameters())
        self.actor_opt = th.optim.Adam(self.actor_params, lr=self.lr, eps=self.eps)
        self.critic_opt = th.optim.Adam(self.critic_params, lr=self.lr, eps=self.eps)
        # set training mode
        self.train()
        # set augmentation and intrinsic reward
        if kwargs["aug"] is not None:
            self.aug = kwargs["aug"]
        if kwargs["irs"] is not None:
            self.irs = kwargs["irs"]

    def get_value(self, obs: th.Tensor) -> th.Tensor:
        """Get estimated values for observations.

        Args:
            obs (Tensor): Observations.

        Returns:
            Estimated values.
        """
        return self.ac.get_value(obs)

    def act(self, obs: th.Tensor, training: bool = True, step: int = 0) -> Union[Tuple[th.Tensor, ...], Dict[str, Any]]:
        """Sample actions based on observations.

        Args:
            obs: Observations.
            training: training mode, True or False.
            step: Global training step.

        Returns:
            Sampled actions.
        """
        if training:
            actions, adv_preds, values, log_probs = self.ac.get_action_and_value(obs)
            return {
                "actions": actions,
                "values": values,
                "log_probs": log_probs,
            }
        else:
            actions = self.ac.get_det_action(obs)
            return actions

    def update(self, rollout_storage: Storage, episode: int = 0) -> Dict[str, float]:
        """Update the learner.

        Args:
            rollout_storage (Storage): Hsuanwu rollout storage.
            episode (int): Global training episode.

        Returns:
            Training metrics such as actor loss, critic_loss, etc.
        """
        total_actor_loss = []
        total_adv_loss = []
        total_critic_loss = []
        total_entropy_loss = []
        total_aug_loss = []
        num_steps, num_envs = rollout_storage.actions.size()[:2]

        if self.irs is not None:
            intrinsic_reward = self.irs.compute_irs(
                samples={
                    "obs": rollout_storage.obs[:-1],
                    "actions": rollout_storage.actions,
                    "next_obs": rollout_storage.obs[1:],
                },
                step=episode * num_envs * num_steps,
            )
            rollout_storage.rewards += intrinsic_reward.to(self.device)

        for _ in range(self.policy_epochs):
            generator = rollout_storage.sample()

            for batch in generator:
                (
                    batch_obs,
                    batch_actions,
                    batch_values,
                    batch_returns,
                    batch_terminateds,
                    batch_truncateds,
                    batch_old_log_probs,
                    adv_targ,
                ) = batch

                # evaluate sampled actions
                new_adv_preds, _, new_log_probs, entropy = self.ac.evaluate_actions(obs=batch_obs, actions=batch_actions)

                # actor loss part
                ratio = th.exp(new_log_probs - batch_old_log_probs)
                surr1 = ratio * adv_targ
                surr2 = th.clamp(ratio, 1.0 - self.clip_range, 1.0 + self.clip_range) * adv_targ
                actor_loss = - th.min(surr1, surr2).mean()
                adv_loss = (new_adv_preds.flatten() - adv_targ).pow(2).mean()

                if self.aug is not None:
                    # augmentation loss part
                    batch_obs_aug = self.aug(batch_obs)
                    new_batch_actions, _, _, _ = self.ac.get_action_and_value(obs=batch_obs)

                    _, _, log_probs_aug, _ = self.ac.evaluate_actions(
                        obs=batch_obs_aug, actions=new_batch_actions
                    )
                    action_loss_aug = - log_probs_aug.mean()
                    aug_loss = self.aug_coef * action_loss_aug
                else:
                    aug_loss = th.scalar_tensor(s=0.0, requires_grad=False, device=adv_loss.device)

                # update
                self.actor_opt.zero_grad(set_to_none=True)
                (adv_loss * self.adv_coef + actor_loss - entropy * self.ent_coef + aug_loss).backward()
                nn.utils.clip_grad_norm_(self.actor_params, self.max_grad_norm)
                self.actor_opt.step()

                total_actor_loss.append(actor_loss.item())
                total_adv_loss.append(adv_loss.item())
                total_entropy_loss.append(entropy.item())
                total_aug_loss.append(aug_loss.item())

        if self.num_policy_updates % self.value_freq == 0:
            for _ in range(self.value_epochs):
                generator = rollout_storage.sample()

                for batch in generator:
                    (
                        batch_obs,
                        batch_actions,
                        batch_values,
                        batch_returns,
                        batch_terminateds,
                        batch_truncateds,
                        batch_old_log_probs,
                        adv_targ,
                    ) = batch

                    # evaluate sampled actions
                    _, new_values, _, _ = self.ac.evaluate_actions(obs=batch_obs, actions=batch_actions)

                    # critic loss part
                    if self.clip_range_vf is None:
                        critic_loss = 0.5 * (new_values.flatten() - batch_returns).pow(2).mean()
                    else:
                        values_clipped = batch_values + (new_values.flatten() - batch_values).clamp(-self.clip_range_vf, self.clip_range_vf)
                        values_losses = (new_values.flatten() - batch_returns).pow(2)
                        values_losses_clipped = (values_clipped - batch_returns).pow(2)
                        critic_loss = 0.5 * th.max(values_losses, values_losses_clipped).mean()
                    
                    if self.aug is not None:
                        # augmentation loss part
                        batch_obs_aug = self.aug(batch_obs)
                        new_batch_actions, _, new_values, _ = self.ac.get_action_and_value(obs=batch_obs)

                        _, values_aug, _, _ = self.ac.evaluate_actions(
                            obs=batch_obs_aug, actions=new_batch_actions
                        )
                        value_loss_aug = 0.5 * (th.detach(new_values) - values_aug).pow(2).mean()
                        aug_loss = self.aug_coef * value_loss_aug
                    else:
                        aug_loss = th.scalar_tensor(s=0.0, requires_grad=False, device=adv_loss.device)

                    self.critic_opt.zero_grad(set_to_none=True)
                    (critic_loss + aug_loss).backward()
                    nn.utils.clip_grad_norm_(self.critic_params, self.max_grad_norm)
                    self.critic_opt.step()

                    total_critic_loss.append(critic_loss.item())

            self.prev_total_critic_loss = total_critic_loss
        else:
            total_critic_loss = self.prev_total_critic_loss

        self.num_policy_updates += 1

        return {
            "actor_loss": np.mean(total_actor_loss),
            "critic_loss": np.mean(total_critic_loss),
            "entropy": np.mean(total_entropy_loss),
            "aug_loss": np.mean(total_aug_loss),
        }

    def save(self, path: Path) -> None:
        """Save models.

        Args:
            path (Path): Storage path.

        Returns:
            None.
        """
        if "pretrained" in str(path):  # pretraining
            th.save(self.ac.state_dict(), path / "actor_critic.pth")
        else:
            del self.ac.critic, self.ac.gae, self.ac.critic_encoder
            th.save(self.ac, path / "actor.pth")

    def load(self, path: str) -> None:
        """Load initial parameters.

        Args:
            path (str): Import path.

        Returns:
            None.
        """
        actor_critic_params = th.load(os.path.join(path, "actor_critic.pth"), map_location=self.device)
        self.ac.load_state_dict(actor_critic_params)
