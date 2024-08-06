"""Utility functions for running pipelines."""

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional, Union
from uuid import UUID

from zenml import constants
from zenml.client import Client
from zenml.config.pipeline_run_configuration import PipelineRunConfiguration
from zenml.config.step_configurations import StepConfigurationUpdate
from zenml.enums import ExecutionStatus
from zenml.logger import get_logger
from zenml.models import (
    FlavorFilter,
    PipelineDeploymentResponse,
    PipelineRunRequest,
    PipelineRunResponse,
    StackResponse,
)
from zenml.orchestrators.utils import get_run_name
from zenml.stack import Flavor, Stack
from zenml.zen_stores.base_zen_store import BaseZenStore

if TYPE_CHECKING:
    StepConfigurationUpdateOrDict = Union[
        Dict[str, Any], StepConfigurationUpdate
    ]

logger = get_logger(__name__)


def get_default_run_name(pipeline_name: str) -> str:
    """Gets the default name for a pipeline run.

    Args:
        pipeline_name: Name of the pipeline which will be run.

    Returns:
        Run name.
    """
    return f"{pipeline_name}-{{date}}-{{time}}"


def create_placeholder_run(
    deployment: "PipelineDeploymentResponse",
) -> Optional["PipelineRunResponse"]:
    """Create a placeholder run for the deployment.

    If the deployment contains a schedule, no placeholder run will be
    created.

    Args:
        deployment: The deployment for which to create the placeholder run.

    Returns:
        The placeholder run or `None` if no run was created.
    """
    assert deployment.user

    if deployment.schedule:
        return None

    run_request = PipelineRunRequest(
        name=get_run_name(run_name_template=deployment.run_name_template),
        # We set the start time on the placeholder run already to
        # make it consistent with the {time} placeholder in the
        # run name. This means the placeholder run will usually
        # have longer durations than scheduled runs, as for them
        # the start_time is only set once the first step starts
        # running.
        start_time=datetime.utcnow(),
        orchestrator_run_id=None,
        user=deployment.user.id,
        workspace=deployment.workspace.id,
        deployment=deployment.id,
        pipeline=deployment.pipeline.id if deployment.pipeline else None,
        status=ExecutionStatus.INITIALIZING,
    )
    return Client().zen_store.create_run(run_request)


def get_placeholder_run(
    deployment_id: UUID,
) -> Optional["PipelineRunResponse"]:
    """Get the placeholder run for a deployment.

    Args:
        deployment_id: ID of the deployment for which to get the placeholder
            run.

    Returns:
        The placeholder run or `None` if there exists no placeholder run for the
        deployment.
    """
    runs = Client().list_pipeline_runs(
        sort_by="asc:created",
        size=1,
        deployment_id=deployment_id,
        status=ExecutionStatus.INITIALIZING,
    )
    if len(runs.items) == 0:
        return None

    run = runs.items[0]
    if run.orchestrator_run_id is None:
        return run

    return None


def deploy_pipeline(
    deployment: "PipelineDeploymentResponse",
    stack: "Stack",
    placeholder_run: Optional["PipelineRunResponse"] = None,
) -> None:
    """Run a deployment.

    Args:
        deployment: The deployment to run.
        stack: The stack on which to run the deployment.
        placeholder_run: An optional placeholder run for the deployment. This
            will be deleted in case the pipeline deployment failed.

    Raises:
        Exception: Any exception that happened while deploying or running
            (in case it happens synchronously) the pipeline.
    """
    stack.prepare_pipeline_deployment(deployment=deployment)

    # Prevent execution of nested pipelines which might lead to
    # unexpected behavior
    previous_value = constants.SHOULD_PREVENT_PIPELINE_EXECUTION
    constants.SHOULD_PREVENT_PIPELINE_EXECUTION = True
    try:
        stack.deploy_pipeline(deployment=deployment)
    except Exception as e:
        if (
            placeholder_run
            and Client().get_pipeline_run(placeholder_run.id).status
            == ExecutionStatus.INITIALIZING
        ):
            # The run hasn't actually started yet, which means that we
            # failed during initialization -> We don't want the
            # placeholder run to stay in the database
            Client().delete_pipeline_run(placeholder_run.id)

        raise e
    finally:
        constants.SHOULD_PREVENT_PIPELINE_EXECUTION = previous_value


def wait_for_pipeline_run_to_finish(run_id: UUID) -> "PipelineRunResponse":
    """Waits until a pipeline run is finished.

    Args:
        run_id: ID of the run for which to wait.

    Returns:
        Model of the finished run.
    """
    sleep_interval = 1
    max_sleep_interval = 64

    while True:
        run = Client().get_pipeline_run(run_id)

        if run.status.is_finished:
            return run

        logger.info(
            "Waiting for pipeline run with ID %s to finish (current status: %s)",
            run_id,
            run.status,
        )
        time.sleep(sleep_interval)
        if sleep_interval < max_sleep_interval:
            sleep_interval *= 2


def validate_stack_is_runnable_from_server(
    zen_store: BaseZenStore, stack: StackResponse
) -> None:
    """Validate if a stack model is runnable from the server.

    Args:
        zen_store: ZenStore to use for listing flavors.
        stack: The stack to validate.

    Raises:
        ValueError: If the stack has components of a custom flavor or local
            components.
    """
    for component_list in stack.components.values():
        assert len(component_list) == 1
        component = component_list[0]
        flavors = zen_store.list_flavors(
            FlavorFilter(name=component.flavor, type=component.type)
        )
        assert len(flavors) == 1
        flavor_model = flavors[0]

        if flavor_model.workspace is not None:
            raise ValueError("No custom stack component flavors allowed.")

        flavor = Flavor.from_model(flavor_model)
        component_config = flavor.config_class(**component.configuration)

        if component_config.is_local:
            raise ValueError("No local stack components allowed.")


def validate_run_config_is_runnable_from_server(
    run_configuration: "PipelineRunConfiguration",
) -> None:
    """Validates that the run configuration can be used to run from the server.

    Args:
        run_configuration: The run configuration to validate.

    Raises:
        ValueError: If there are values in the run configuration that are not
            allowed when running a pipeline from the server.
    """
    if run_configuration.parameters:
        raise ValueError(
            "Can't set parameters when running pipeline via Rest API."
        )

    if run_configuration.build:
        raise ValueError("Can't set build when running pipeline via Rest API.")

    if run_configuration.schedule:
        raise ValueError(
            "Can't set schedule when running pipeline via Rest API."
        )

    if run_configuration.settings.get("docker"):
        raise ValueError(
            "Can't set DockerSettings when running pipeline via Rest API."
        )

    for step_update in run_configuration.steps.values():
        if step_update.settings.get("docker"):
            raise ValueError(
                "Can't set DockerSettings when running pipeline via Rest API."
            )
