from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from enum import StrEnum
from typing import Any, ClassVar, Self, get_origin, get_type_hints

from pydantic import Field, model_validator

from notte.common.config import FrozenConfig
from notte.env import NotteEnvConfig
from notte.sdk.types import DEFAULT_MAX_NB_STEPS


class RaiseCondition(StrEnum):
    """How to raise an error when the agent fails to complete a step.

    Either immediately upon failure, after retry, or never.
    """

    IMMEDIATELY = "immediately"
    RETRY = "retry"
    NEVER = "never"


class AgentConfig(FrozenConfig, ABC):
    # make env private to avoid exposing the NotteEnvConfig class
    env: NotteEnvConfig = Field(init=False)
    reasoning_model: str = Field(
        default="openai/gpt-4o", description="The model to use for reasoning (i.e taking actions)."
    )
    include_screenshot: bool = Field(default=False, description="Whether to include a screenshot in the response.")
    max_history_tokens: int = Field(
        default=16000,
        description="The maximum number of tokens in the history. When the history exceeds this limit, the oldest messages are discarded.",
    )
    max_error_length: int = Field(
        default=500, description="The maximum length of an error message to be forwarded to the reasoning model."
    )
    raise_condition: RaiseCondition = Field(
        default=RaiseCondition.RETRY, description="How to raise an error when the agent fails to complete a step."
    )
    max_consecutive_failures: int = Field(
        default=3, description="The maximum number of consecutive failures before the agent gives up."
    )
    force_env: bool | None = Field(
        default=None,
        description="Whether to allow the user to set the environment.",
    )

    @classmethod
    @abstractmethod
    def default_env(cls) -> NotteEnvConfig:
        raise NotImplementedError("Subclasses must implement this method")

    @model_validator(mode="before")
    @classmethod
    def set_env(cls, values: dict[str, Any]) -> dict[str, Any]:
        if "env" in values:
            if "force_env" in values and values["force_env"]:
                del values["force_env"]
                return values
            raise ValueError("Env should not be set by the user. Set `default_env` instead.")
        values["env"] = cls.default_env()  # Set the env field using the subclass's method
        return values

    def groq(self: Self) -> Self:
        return self._copy_and_validate(reasoning_model="groq/llama-3.3-70b-versatile")

    def openai(self: Self) -> Self:
        return self._copy_and_validate(reasoning_model="openai/gpt-4o")

    def cerebras(self: Self) -> Self:
        return self._copy_and_validate(reasoning_model="cerebras/llama-3.3-70b")

    def use_vision(self: Self) -> Self:
        return self._copy_and_validate(include_screenshot=True)

    def dev_mode(self: Self) -> Self:
        return self._copy_and_validate(
            raise_condition=RaiseCondition.IMMEDIATELY,
            max_error_length=1000,
            env=self.env.dev_mode(),
            force_env=True,
        )

    def set_raise_condition(self: Self, value: RaiseCondition) -> Self:
        return self._copy_and_validate(raise_condition=value)

    def map_env(self: Self, env: Callable[[NotteEnvConfig], NotteEnvConfig]) -> Self:
        return self._copy_and_validate(env=env(self.env), force_env=True)

    @staticmethod
    def _get_arg_type(python_type: Any) -> Any:
        """Maps Python types to argparse types."""
        type_map = {
            str: str,
            int: int,
            float: float,
            bool: bool,
        }
        return type_map.get(python_type, str)

    @staticmethod
    def create_base_parser() -> ArgumentParser:
        """Creates a base ArgumentParser with all the fields from the config."""
        parser = ArgumentParser()
        _ = parser.add_argument(
            "--env.headless", type=bool, default=False, help="Whether to run the browser in headless mode."
        )
        _ = parser.add_argument(
            "--env.disable_web_security", type=bool, default=False, help="Whether to disable web security."
        )
        _ = parser.add_argument(
            "--env.perception_model", type=str, default=None, help="The model to use for perception."
        )
        _ = parser.add_argument(
            "--env.max_steps",
            type=int,
            default=DEFAULT_MAX_NB_STEPS,
            help="The maximum number of steps the agent can take.",
        )
        return parser

    @classmethod
    def create_parser(cls) -> ArgumentParser:
        """Creates an ArgumentParser with all the fields from the config."""
        parser = cls.create_base_parser()
        hints = get_type_hints(cls)

        for field_name, field_info in cls.model_fields.items():
            if field_name == "env":
                continue
            field_type = hints.get(field_name)
            if get_origin(field_type) is ClassVar:
                continue

            default = field_info.default
            help_text = field_info.description or "no description available"
            arg_type = cls._get_arg_type(field_type)

            _ = parser.add_argument(
                f"--{field_name.replace('_', '-')}",
                type=arg_type,
                default=default,
                help=f"{help_text} (default: {default})",
            )

        return parser

    @classmethod
    def from_args(cls: type[Self], args: Namespace) -> Self:
        """Creates an AgentConfig from a Namespace of arguments.

        The return type will match the class that called this method.
        """
        disallowed_args = ["task", "env.window.headless"]

        env_args = {
            k.replace("env.", "").replace("-", "_"): v
            for k, v in vars(args).items()
            if k.startswith("env.") and k not in disallowed_args
        }
        agent_args = {
            k.replace("-", "_"): v
            for k, v in vars(args).items()
            if not k.startswith("env.") and k not in disallowed_args
        }

        def update_env(env: NotteEnvConfig) -> NotteEnvConfig:
            env = env._copy_and_validate(**env_args)
            if "env.headless" in env_args:
                env = env.headless(env_args["env.headless"])
            if "env.disable_web_security" in env_args and env_args["env.disable_web_security"]:
                env = env.disable_web_security()
            return env

        return cls(**agent_args).map_env(update_env)
