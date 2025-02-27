from collections import deque
from typing import Dict, Tuple, Union

import gymnasium as gym
import numpy as np
import torch as th
from omegaconf import DictConfig
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, TensorDataset

from hsuanwu.xplore.reward import BaseIntrinsicRewardModule


class Encoder(nn.Module):
    """Encoder for encoding observations.

    Args:
        obs_shape (Tuple): The data shape of observations.
        action_dim (int): The dimension of actions.
        latent_dim (int): The dimension of encoding vectors.

    Returns:
        Encoder instance.
    """

    def __init__(self, obs_shape: Tuple, action_dim: int, latent_dim: int) -> None:
        super().__init__()

        # visual
        if len(obs_shape) == 3:
            self.trunk = nn.Sequential(
                nn.Conv2d(obs_shape[0], 32, 8, 4),
                nn.ReLU(),
                nn.Conv2d(32, 64, 4, 2),
                nn.ReLU(),
                nn.Conv2d(64, 64, 3, 1),
                nn.ReLU(),
                nn.Flatten(),
            )
            with th.no_grad():
                sample = th.ones(size=tuple(obs_shape))
                n_flatten = self.trunk(sample.unsqueeze(0)).shape[1]

            self.linear = nn.Linear(n_flatten, latent_dim)
        else:
            self.trunk = nn.Sequential(nn.Linear(obs_shape[0], 256), nn.ReLU())
            self.linear = nn.Linear(256, latent_dim)

        # TODO: output actions
        self.policy = nn.Linear(latent_dim * 2, action_dim)

    def forward(self, obs: th.Tensor, next_obs: th.Tensor) -> th.Tensor:
        """Forward function for outputing predicted actions.

        Args:
            obs (Tensor): Current observations.
            next_obs (Tensor): Next observations.

        Returns:
            Predicted actions.
        """
        h = F.relu(self.linear(self.trunk(obs)))
        next_h = F.relu(self.linear(self.trunk(next_obs)))

        actions = self.policy(th.cat([h, next_h], dim=1))
        return actions

    def encode(self, obs: th.Tensor) -> th.Tensor:
        """Encode the input tensors.

        Args:
            obs (Tensor): Observations.

        Returns:
            Encoding tensors.
        """
        return F.relu(self.linear(self.trunk(obs)))


class PseudoCounts(BaseIntrinsicRewardModule):
    """Pseudo-counts based on "Never Give Up: Learning Directed Exploration Strategies (NGU)".
        See paper: https://arxiv.org/pdf/2002.06038

    Args:
        observation_space (Space or DictConfig): The observation space of environment. When invoked by Hydra,
            'observation_space' is a 'DictConfig' like {"shape": observation_space.shape, }.
        action_space (Space or DictConfig): The action space of environment. When invoked by Hydra,
            'action_space' is a 'DictConfig' like
            {"shape": action_space.shape, "n": action_space.n, "type": "Discrete", "range": [0, n - 1]} or
            {"shape": action_space.shape, "type": "Box", "range": [action_space.low[0], action_space.high[0]]}.
        device (str): Device (cpu, cuda, ...) on which the code should be run.
        beta (float): The initial weighting coefficient of the intrinsic rewards.
        kappa (float): The decay rate.
        latent_dim (int): The dimension of encoding vectors.
        lr (float): The learning rate.
        batch_size (int): The batch size for update.
        capacity (int): The of capacity the episodic memory.
        k (int): Number of neighbors.
        kernel_cluster_distance (float): The kernel cluster distance.
        kernel_epsilon (float): The kernel constant.
        c (float): The pseudo-counts constant.
        sm (float): The kernel maximum similarity.

    Returns:
        Instance of PseudoCounts.
    """

    def __init__(
        self,
        observation_space: Union[gym.Space, DictConfig],
        action_space: Union[gym.Space, DictConfig],
        device: str = "cpu",
        beta: float = 0.05,
        kappa: float = 0.000025,
        latent_dim: int = 32,
        lr: float = 0.001,
        batch_size: int = 64,
        capacity: int = 1000,
        k: int = 10,
        kernel_cluster_distance: float = 0.008,
        kernel_epsilon: float = 0.0001,
        c: float = 0.001,
        sm: float = 8.0,
    ) -> None:
        super().__init__(observation_space, action_space, device, beta, kappa)

        self.encoder = Encoder(
            obs_shape=self._obs_shape,
            action_dim=self._action_dim,
            latent_dim=latent_dim,
        ).to(self._device)
        self.episodic_memory = deque(maxlen=capacity)
        self.k = k
        self.kernel_cluster_distance = kernel_cluster_distance
        self.kernel_epsilon = kernel_epsilon
        self.c = c
        self.sm = sm

        self.opt = th.optim.Adam(self.encoder.parameters(), lr=lr)
        if self._action_type == "Discrete":
            self.loss = nn.CrossEntropyLoss()
        else:
            self.loss = nn.MSELoss()
        self.batch_size = batch_size

    def pseudo_counts(self, e: th.Tensor) -> th.Tensor:
        """Pseudo counts.

        Args:
            e (Tensor): Encoded observations.

        Returns:
            Conut values.
        """
        num_steps = e.size()[0]
        counts = th.zeros(size=(num_steps,))
        memory = th.stack(list(self.episodic_memory)).squeeze(1)
        for step in range(num_steps):
            dist = th.norm(e[step] - memory, p=2, dim=1).sort().values[: self.k]
            # moving average
            dist = dist / (dist.mean() + 1e-11)
            dist = th.maximum(dist - self.kernel_cluster_distance, th.zeros_like(dist))
            kernel = self.kernel_epsilon / (dist + self.kernel_epsilon)
            s = th.sqrt(kernel.sum()) + self.c

            if s is th.nan or s > self.sm:
                counts[step] = 0.0
            else:
                counts[step] = 1.0 / s

        return counts

    def compute_irs(self, samples: Dict, step: int = 0) -> th.Tensor:
        """Compute the intrinsic rewards for current samples.

        Args:
            samples (Dict): The collected samples. A python dict like
                {obs (n_steps, n_envs, *obs_shape) <class 'th.Tensor'>,
                actions (n_steps, n_envs, *action_shape) <class 'th.Tensor'>,
                rewards (n_steps, n_envs) <class 'th.Tensor'>,
                next_obs (n_steps, n_envs, *obs_shape) <class 'th.Tensor'>}.
            step (int): The global training step.

        Returns:
            The intrinsic rewards.
        """
        # compute the weighting coefficient of timestep t
        beta_t = self._beta * np.power(1.0 - self._kappa, step)
        num_steps = samples["obs"].size()[0]
        num_envs = samples["obs"].size()[1]
        obs_tensor = samples["obs"].to(self._device)
        intrinsic_rewards = th.zeros(size=(num_steps, num_envs))

        try:
            with th.no_grad():
                for i in range(num_envs):
                    e = self.encoder.encode(obs_tensor[:, i])
                    # TODO: add encodings into memory
                    self.episodic_memory.extend(e.split(1))
                    n_eps = self.pseudo_counts(e=e)
                    intrinsic_rewards[:, i] = n_eps

            # udpate the module
            self.update(samples)
        except KeyboardInterrupt:
            exit(0)

        return intrinsic_rewards * beta_t

    def update(self, samples: Dict) -> None:
        """Update the intrinsic reward module if necessary.

        Args:
            samples: The collected samples. A python dict like
                {obs (n_steps, n_envs, *obs_shape) <class 'th.Tensor'>,
                actions (n_steps, n_envs, *action_shape) <class 'th.Tensor'>,
                rewards (n_steps, n_envs) <class 'th.Tensor'>,
                next_obs (n_steps, n_envs, *obs_shape) <class 'th.Tensor'>}.

        Returns:
            None
        """
        num_steps = samples["obs"].size()[0]
        num_envs = samples["obs"].size()[1]
        obs_tensor = samples["obs"].view((num_envs * num_steps, *self._obs_shape)).to(self._device)
        next_obs_tensor = samples["next_obs"].view((num_envs * num_steps, *self._obs_shape)).to(self._device)

        if self._action_type == "Discrete":
            actions_tensor = samples["actions"].view(num_envs * num_steps).to(self._device)
            actions_tensor = F.one_hot(actions_tensor.long(), self._action_dim).float()
        else:
            actions_tensor = samples["actions"].view((num_envs * num_steps, self._action_dim)).to(self._device)

        dataset = TensorDataset(obs_tensor, actions_tensor, next_obs_tensor)
        loader = DataLoader(dataset=dataset, batch_size=self.batch_size, drop_last=True)

        for _idx, batch in enumerate(loader):
            obs, actions, next_obs = batch
            pred_actions = self.encoder(obs, next_obs)
            self.opt.zero_grad()
            loss = self.loss(pred_actions, actions)
            loss.backward()
            self.opt.step()
