import datetime
import os
import time

import pytest
from dagster import (
    DagsterEventType,
    _check as check,
)
from dagster._core.storage.dagster_run import DagsterRunStatus
from dagster._core.storage.tags import DOCKER_IMAGE_TAG
from dagster._utils.merger import merge_dicts
from dagster._utils.yaml_utils import load_yaml_from_path, merge_yamls
from dagster_k8s.client import DagsterKubernetesClient
from dagster_k8s.test import wait_for_job_and_get_raw_logs
from dagster_k8s_test_infra.integration_utils import (
    can_terminate_run_over_graphql,
    image_pull_policy,
    launch_run_over_graphql,
    terminate_run_over_graphql,
)
from dagster_test.test_project import (
    get_test_project_docker_image,
    get_test_project_environments_path,
)


@pytest.mark.integration
def test_k8s_run_launcher_default(
    dagster_instance_for_k8s_run_launcher,
    user_code_namespace_for_k8s_run_launcher,
    webserver_url_for_k8s_run_launcher,
):
    pods = DagsterKubernetesClient.production_client().core_api.list_namespaced_pod(
        namespace=user_code_namespace_for_k8s_run_launcher
    )
    celery_pod_names = [p.metadata.name for p in pods.items if "celery-workers" in p.metadata.name]
    check.invariant(not celery_pod_names)

    run_config = load_yaml_from_path(os.path.join(get_test_project_environments_path(), "env.yaml"))
    job_name = "demo_job"

    run_id = launch_run_over_graphql(
        webserver_url_for_k8s_run_launcher, run_config=run_config, job_name=job_name
    )

    result = wait_for_job_and_get_raw_logs(
        job_name="dagster-run-%s" % run_id, namespace=user_code_namespace_for_k8s_run_launcher
    )

    assert "PIPELINE_SUCCESS" in result, f"no match, result: {result}"

    updated_run = dagster_instance_for_k8s_run_launcher.get_run_by_id(run_id)
    assert updated_run.tags[DOCKER_IMAGE_TAG] == get_test_project_docker_image()


IS_BUILDKITE = os.getenv("BUILDKITE") is not None


def get_celery_engine_config(dagster_docker_image, job_namespace):
    return {
        "execution": {
            "config": {
                "job_image": dagster_docker_image,
                "job_namespace": job_namespace,
                "image_pull_policy": image_pull_policy(),
            }
        },
    }


def test_k8s_run_launcher_with_celery_executor_fails(
    dagster_docker_image,
    dagster_instance_for_k8s_run_launcher,
    user_code_namespace_for_k8s_run_launcher,
    webserver_url_for_k8s_run_launcher,
):
    run_config = merge_dicts(
        merge_yamls(
            [
                os.path.join(get_test_project_environments_path(), "env.yaml"),
                os.path.join(get_test_project_environments_path(), "env_s3.yaml"),
            ]
        ),
        get_celery_engine_config(
            dagster_docker_image=dagster_docker_image,
            job_namespace=user_code_namespace_for_k8s_run_launcher,
        ),
    )

    job_name = "demo_job_celery_k8s"

    run_id = launch_run_over_graphql(
        webserver_url_for_k8s_run_launcher, run_config=run_config, job_name=job_name
    )

    timeout = datetime.timedelta(0, 120)

    start_time = datetime.datetime.now()

    while True:
        assert datetime.datetime.now() < start_time + timeout, "Timed out waiting for job failure"
        event_records = dagster_instance_for_k8s_run_launcher.all_logs(run_id)

        found_job_failure = False
        for event_record in event_records:
            if event_record.dagster_event:
                if event_record.dagster_event.event_type == DagsterEventType.PIPELINE_FAILURE:
                    found_job_failure = True

        if found_job_failure:
            break

        time.sleep(5)

    assert (
        dagster_instance_for_k8s_run_launcher.get_run_by_id(run_id).status
        == DagsterRunStatus.FAILURE
    )


@pytest.mark.integration
def test_failing_k8s_run_launcher(
    dagster_instance_for_k8s_run_launcher,
    user_code_namespace_for_k8s_run_launcher,
    webserver_url_for_k8s_run_launcher,
):
    run_config = load_yaml_from_path(os.path.join(get_test_project_environments_path(), "env.yaml"))

    job_name = "always_fail_job"

    run_id = launch_run_over_graphql(
        webserver_url_for_k8s_run_launcher, run_config=run_config, job_name=job_name
    )

    result = wait_for_job_and_get_raw_logs(
        job_name="dagster-run-%s" % run_id, namespace=user_code_namespace_for_k8s_run_launcher
    )

    assert "PIPELINE_SUCCESS" not in result, f"no match, result: {result}"

    event_records = dagster_instance_for_k8s_run_launcher.all_logs(run_id)

    assert any(["Op Exception Message" in str(event) for event in event_records])


@pytest.mark.integration
def test_k8s_run_launcher_terminate(
    dagster_instance_for_k8s_run_launcher,
    user_code_namespace_for_k8s_run_launcher,
    webserver_url_for_k8s_run_launcher,
):
    job_name = "slow_job_k8s"

    run_config = load_yaml_from_path(
        os.path.join(get_test_project_environments_path(), "env_s3.yaml")
    )

    run_id = launch_run_over_graphql(
        webserver_url_for_k8s_run_launcher, run_config=run_config, job_name=job_name
    )

    DagsterKubernetesClient.production_client().wait_for_job(
        job_name="dagster-run-%s" % run_id, namespace=user_code_namespace_for_k8s_run_launcher
    )

    timeout = datetime.timedelta(0, 30)
    start_time = datetime.datetime.now()
    while True:
        assert datetime.datetime.now() < start_time + timeout, "Timed out waiting for can_terminate"
        if can_terminate_run_over_graphql(webserver_url_for_k8s_run_launcher, run_id):
            break
        time.sleep(5)

    terminate_run_over_graphql(webserver_url_for_k8s_run_launcher, run_id=run_id)

    start_time = datetime.datetime.now()
    dagster_run = None
    while True:
        assert datetime.datetime.now() < start_time + timeout, "Timed out waiting for termination"
        dagster_run = dagster_instance_for_k8s_run_launcher.get_run_by_id(run_id)
        if dagster_run.status == DagsterRunStatus.CANCELED:
            break
        time.sleep(5)

    assert dagster_run.status == DagsterRunStatus.CANCELED

    assert not can_terminate_run_over_graphql(webserver_url_for_k8s_run_launcher, run_id)


@pytest.mark.integration
def test_k8s_run_launcher_secret_from_deployment(
    user_code_namespace_for_k8s_run_launcher,
    webserver_url_for_k8s_run_launcher,
):
    # This run_config requires that WORD_FACTOR be set on both the user code deployment
    # and the run launcher. It will only work if secrets are propagated from the deployment
    # to the run launcher, since TEST_DEPLOYMENT_SECRET_NAME is only set on the user code
    # deployment but not on the run launcher config.
    run_config = load_yaml_from_path(
        os.path.join(get_test_project_environments_path(), "env_config_from_secrets.yaml")
    )
    job_name = "demo_job"

    run_id = launch_run_over_graphql(
        webserver_url_for_k8s_run_launcher, run_config=run_config, job_name=job_name
    )

    result = wait_for_job_and_get_raw_logs(
        job_name="dagster-run-%s" % run_id, namespace=user_code_namespace_for_k8s_run_launcher
    )

    assert "PIPELINE_SUCCESS" in result, f"no match, result: {result}"
