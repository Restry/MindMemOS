from abc import ABC, abstractmethod

from mindmemos_skill.typing import EnvConfig


class BaseEnv(ABC):
    def __init__(self, config: EnvConfig):
        self.config = config

    @abstractmethod
    def setup(self):
        """环境启动前开始准备"""
        ...

    @abstractmethod
    def cleanup(self):
        """环境清理"""
        ...

    @abstractmethod
    def rollout(self, agent, skills, workspace: str):
        ...

    @abstractmethod
    def rollout_batch(self, agent, n: int, skills, workspace: str):
        ...
