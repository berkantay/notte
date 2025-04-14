import time
import traceback
import typing
import random
from collections.abc import Callable
from enum import StrEnum

from litellm import AllMessageValues, override
from loguru import logger
from patchright.async_api import Locator

import notte
from notte.agents.falco.perception import FalcoPerception
from notte.agents.falco.prompt import FalcoPrompt
from notte.agents.falco.trajectory_history import FalcoTrajectoryHistory
from notte.agents.falco.types import StepAgentOutput
from notte.browser.observation import Observation
from notte.browser.window import BrowserWindow
from notte.common.agent.base import BaseAgent
from notte.common.agent.config import AgentConfig, RaiseCondition
from notte.common.agent.types import AgentResponse
from notte.common.credential_vault.base import BaseVault
from notte.common.tools.conversation import Conversation
from notte.common.tools.safe_executor import ExecutionStatus, SafeActionExecutor
from notte.common.tools.validator import CompletionValidator
from notte.common.tracer import LlmUsageDictTracer
from notte.controller.actions import BaseAction, CompletionAction, FallbackObserveAction, InteractionAction
from notte.env import NotteEnv, NotteEnvConfig
from notte.llms.engine import LLMEngine
from notte.pipe.preprocessing.dom.locate import locate_element
from notte.errors.actions import InvalidActionIDError
from typing import Union

# TODO: list
# handle tooling calling methods for different providers (if not supported by litellm)
# Handle control flags
# Done callback
# Setup telemetry
# Setup memory
# Handle custom functions, e.g. `Upload file to element`ç
# Remove base 64 images from current state
# TODO: add fault tolerance LLM parsing
# TODO: only display modal actions when modal is open (same as before)
# TODO: handle prevent default click JS events
# TODO: add some tree structure for menu elements (like we had in notte before. Ex. Menu in Arxiv)


class HistoryType(StrEnum):
    FULL_CONVERSATION = "full_conversation"
    SHORT_OBSERVATIONS = "short_observations"
    SHORT_OBSERVATIONS_WITH_RAW_DATA = "short_observations_with_raw_data"
    SHORT_OBSERVATIONS_WITH_SHORT_DATA = "short_observations_with_short_data"
    COMPRESSED = "compressed"


class FalcoAgentConfig(AgentConfig):
    max_actions_per_step: int = 1
    history_type: HistoryType = HistoryType.SHORT_OBSERVATIONS_WITH_SHORT_DATA
    max_retry_action_errors: int = 3
    

    @classmethod
    @override
    def default_env(cls) -> NotteEnvConfig:
        return NotteEnvConfig().disable_perception()


class FalcoAgent(BaseAgent):
    def __init__(
        self,
        config: FalcoAgentConfig,
        window: BrowserWindow | None = None,
        vault: BaseVault | None = None,
        step_callback: Callable[[str, StepAgentOutput], None] | None = None,
    ):
        self.config: FalcoAgentConfig = config
        self.vault: BaseVault | None = vault

        if config.include_screenshot and not config.env.window.screenshot:
            raise ValueError("Cannot `include_screenshot=True` if `screenshot` is not enabled in the browser config")
        self.tracer: LlmUsageDictTracer = LlmUsageDictTracer()
        self.llm: LLMEngine = LLMEngine(
            model=config.reasoning_model,
            tracer=self.tracer,
            structured_output_retries=config.env.structured_output_retries,
            verbose=self.config.verbose,
        )

        self.step_callback: Callable[[str, StepAgentOutput], None] | None = step_callback
        # Users should implement their own parser to customize how observations
        # and actions are formatted for their specific LLM and use case
        self.env: NotteEnv = NotteEnv(
            config=config.env,
            window=window,
        )

        if self.vault is not None:
            # hide vault leaked credentials within llm inputs
            self.llm.structured_completion = self.vault.patch_structured_completion(0, self.vault.get_replacement_map)(
                self.llm.structured_completion
            )

            # hide vault leaked credentials within screenshots
            self.env._window.vault_replacement_fn = self.vault.get_replacement_map  # type: ignore

        self.perception: FalcoPerception = FalcoPerception()
        self.validator: CompletionValidator = CompletionValidator(llm=self.llm, perception=self.perception)
        self.prompt: FalcoPrompt = FalcoPrompt(max_actions_per_step=config.max_actions_per_step)
        self.conv: Conversation = Conversation(
            max_tokens=config.max_history_tokens,
            convert_tools_to_assistant=True,
            autosize=True,
            model=config.reasoning_model,
        )
        self.history_type: HistoryType = config.history_type
        self.trajectory: FalcoTrajectoryHistory = FalcoTrajectoryHistory(max_error_length=config.max_error_length)

        async def precheck_action(action: BaseAction):
            if not self._is_first_step():
                valid_action_set = self._get_valid_action_set(self.trajectory.last_obs())
                if action.id not in valid_action_set:
                    raise InvalidActionIDError(action.id, list(valid_action_set))
        
        def invalid_action_id_handler(e: InvalidActionIDError):
            self.conv.add_user_message(content=e.agent_message)
            return None
        
        
        async def execute_action(action: BaseAction) -> Observation:
            if self.vault is not None and self.vault.contains_credentials(action):
                action_with_selector = await self.env._node_resolution_pipe.forward(action, self.env.snapshot)  # type: ignore
                locator: Locator = await locate_element(self.env._window.page, action_with_selector.selector)  # type: ignore

                assert isinstance(action_with_selector, InteractionAction) and action_with_selector.selector is not None

                action = await self.vault.replace_credentials(
                    action,
                    locator,
                    self.env.snapshot,
                )
            return await self.env.act(action)

        self.step_executor: SafeActionExecutor[BaseAction, Observation, Union[InvalidActionIDError]] = SafeActionExecutor(
            func=execute_action,
            precheck_func=precheck_action,
            on_failure_handlers={InvalidActionIDError: invalid_action_id_handler},
            raise_on_failure=(self.config.raise_condition is RaiseCondition.IMMEDIATELY),
            max_consecutive_failures=config.max_consecutive_failures,
        )

    async def reset(self) -> None:
        self.conv.reset()
        self.trajectory.reset()
        self.step_executor.reset()
        await self.env.reset()

    def output(self, answer: str, success: bool) -> AgentResponse:
        return AgentResponse(
            answer=answer,
            success=success,
            env_trajectory=self.env.trajectory,
            agent_trajectory=self.trajectory.steps,  # type: ignore[reportArgumentType]
            messages=self.conv.messages(),
            duration_in_s=time.time() - self.start_time,
            llm_usage=self.tracer.usage,
        )

    async def get_messages(self, task: str) -> list[AllMessageValues]:
        self.conv.reset()
        system_msg, task_msg = self.prompt.system(), self.prompt.task(task)
        if self.vault is not None:
            system_msg += "\n" + await self.vault.instructions()
        self.conv.add_system_message(content=system_msg)
        self.conv.add_user_message(content=task_msg)
        # just for logging
        traj_msg = self.trajectory.perceive()
        if self.config.verbose:
            logger.info(f"🔍 Trajectory history:\n{traj_msg}")
        # add trajectory to the conversation
        match self.history_type:
            case HistoryType.COMPRESSED:
                self.conv.add_user_message(content=traj_msg)
            case _:
                if len(self.trajectory.steps) == 0:
                    self.conv.add_user_message(content=self.trajectory.start_rules())
                for step in self.trajectory.steps:
                    # TODO: choose if we want this to be an assistant message or a tool message
                    # self.conv.add_tool_message(step.agent_response, tool_id="step")
                    self.conv.add_assistant_message(step.agent_response.model_dump_json(exclude_none=True))
                    for result in step.results:
                        short_step_msg = self.trajectory.perceive_step_result(result, include_ids=True)
                        self.conv.add_user_message(content=short_step_msg)
                        if not result.success:
                            continue
                        # add observation data to the conversation
                        obs = result.get()
                        match (self.history_type, obs.has_data()):
                            case (HistoryType.FULL_CONVERSATION, _):
                                self.conv.add_user_message(
                                    content=self.perception.perceive(obs),
                                    image=(obs.screenshot if self.config.include_screenshot else None),
                                )
                            case (HistoryType.SHORT_OBSERVATIONS_WITH_RAW_DATA, True):
                                # add data if data was scraped
                                self.conv.add_user_message(content=self.perception.perceive_data(obs, raw=True))

                            case (HistoryType.SHORT_OBSERVATIONS_WITH_SHORT_DATA, True):
                                self.conv.add_user_message(content=self.perception.perceive_data(obs, raw=False))
                            case _:
                                pass

        last_valid_obs = self.trajectory.last_obs()
        if last_valid_obs is not None and self.history_type is not HistoryType.FULL_CONVERSATION:
            self.conv.add_user_message(
                content=self.perception.perceive(last_valid_obs),
                image=(last_valid_obs.screenshot if self.config.include_screenshot else None),
            )

        if len(self.trajectory.steps) > 0:
            self.conv.add_user_message(self.prompt.action_message())

        return self.conv.messages()
    

    async def step(self, task: str) -> CompletionAction | None:
        """Execute a single step of the agent"""
        messages = await self.get_messages(task)
        response: StepAgentOutput = self.llm.structured_completion(messages, response_format=StepAgentOutput)
        if self.step_callback is not None:
            self.step_callback(task, response)

        if self.config.verbose:
            logger.info(f"🔍 LLM response:\n{response}")

        for line in response.pretty_string().split("\n"):
            logger.opt(colors=True).info(line)

        self.trajectory.add_output(response)
        # check for completion
        if response.output is not None:
            return response.output
        # Execute the actions
        
        for action in response.get_actions(self.config.max_actions_per_step):
            result = await self.step_executor.execute(action)
            
            
            if result.should_rerun_step_agent:
                logger.warning("Wrong action id, rerunning step agent")
                return await self.step(task)
                
            self.trajectory.add_step(result)
            step_msg = self.trajectory.perceive_step_result(result, include_ids=True)
            logger.info(f"{step_msg}\n\n")
            if not result.success:
                # observe again
                obs = await self.env.observe()

                # cast is necessary because we cant have covariance
                # in ExecutionStatus
                ex_status = ExecutionStatus(
                    input=typing.cast(BaseAction, FallbackObserveAction()),
                    output=obs,
                    success=True,
                    message="Observed",
                )
                self.trajectory.add_output(response)
                self.trajectory.add_step(ex_status)

                # stop the loop
                break
            # Successfully executed the action
        return None
    
    @override
    async def run(self, task: str, url: str | None = None) -> AgentResponse:
        logger.info(f"Running task: {task}")
        self.start_time: float = time.time()
        try:
            return await self._run(task, url=url)

        except Exception as e:
            if self.config.raise_condition is RaiseCondition.NEVER:
                return self.output(f"Failed due to {e}: {traceback.format_exc()}", False)
            raise e

    async def _run(self, task: str, url: str | None = None) -> AgentResponse:
        """Execute the task with maximum number of steps"""
        # change this to DEV if you want more explicit error messages
        # when you are developing your own agent
        notte.set_error_mode("agent")
        if url is not None:
            task = f"Start on '{url}' and {task}"

        # Loop through the steps
        async with self.env:
            for step in range(self.env.config.max_steps):
                logger.info(f"💡 Step {step}")
                output: CompletionAction | None = await self.step(task)

                if output is None:
                    continue
                # validate the output
                if not output.success:
                    logger.error(f"🚨 Agent terminated early with failure: {output.answer}")
                    return self.output(output.answer, False)
                # Sucessful execution and LLM output is not None
                # Need to validate the output
                logger.info(f"🔥 Validating agent output:\n{output.model_dump_json()}")
                val = self.validator.validate(task, output, self.env.trajectory[-1])
                if val.is_valid:
                    logger.info("✅ Task completed successfully")
                    return self.output(output.answer, output.success)
                else:
                    # TODO handle that differently
                    failed_val_msg = f"Final validation failed: {val.reason}. Continuing..."
                    logger.error(failed_val_msg)
                    # add the validation result to the trajectory and continue
                    self.trajectory.add_step(
                        ExecutionStatus(
                            input=output,
                            output=None,
                            success=False,
                            message=failed_val_msg,
                        )
                    )

        error_msg = f"Failed to solve task in {self.env.config.max_steps} steps"
        logger.info(f"🚨 {error_msg}")
        notte.set_error_mode("developer")
        return self.output(error_msg, False)
    
    
    
    def _get_valid_action_set(self, last_obs: Observation) -> set[str]:
        valid_action_set = set()
        if last_obs and last_obs.space and last_obs.space.actions:
            for action in last_obs.space.actions("all"):
                valid_action_set.add(action.id)
        return valid_action_set
    
    def _is_first_step(self) -> bool:
        return len(self.trajectory.steps) == 1
    
    
    