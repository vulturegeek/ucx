import json
import os
import random
import uuid
from functools import partial

import pytest
from _pytest.fixtures import SubRequest
from databricks.sdk import AccountClient
from databricks.sdk.service.compute import (
    ClusterDetails,
    CreateInstancePoolResponse,
    CreatePolicyResponse,
)
from databricks.sdk.service.iam import PermissionLevel
from databricks.sdk.service.jobs import CreateResponse
from databricks.sdk.service.pipelines import (
    CreatePipelineResponse,
    NotebookLibrary,
    PipelineLibrary,
)
from utils import (
    EnvironmentInfo,
    InstanceProfile,
    _cleanup_groups,
    _create_groups,
    _get_basic_job_cluster,
    _get_basic_task,
    _set_random_permissions,
    initialize_env,
)

from uc_migration_toolkit.config import (
    AuthConfig,
    InventoryTable,
    RateLimitConfig,
    WorkspaceAuthConfig,
)
from uc_migration_toolkit.managers.inventory.types import RequestObjectType
from uc_migration_toolkit.providers.client import ImprovedWorkspaceClient, provider
from uc_migration_toolkit.providers.logger import logger
from uc_migration_toolkit.utils import Request, ThreadedExecution

initialize_env()

NUM_TEST_GROUPS = os.environ.get("NUM_TEST_GROUPS", 5)
NUM_TEST_INSTANCE_PROFILES = os.environ.get("NUM_TEST_INSTANCE_PROFILES", 3)
NUM_TEST_CLUSTERS = os.environ.get("NUM_TEST_CLUSTERS", 3)
NUM_TEST_INSTANCE_POOLS = os.environ.get("NUM_TEST_INSTANCE_POOLS", 3)
NUM_TEST_CLUSTER_POLICIES = os.environ.get("NUM_TEST_CLUSTER_POLICIES", 3)
NUM_TEST_PIPELINES = os.environ.get("NUM_TEST_PIPELINES", 3)
NUM_TEST_JOBS = os.environ.get("NUM_TEST_JOBS", 3)

NUM_THREADS = os.environ.get("NUM_TEST_THREADS", 20)
DB_CONNECT_CLUSTER_NAME = os.environ.get("DB_CONNECT_CLUSTER_NAME", "ucx-integration-testing")
UCX_TESTING_PREFIX = os.environ.get("UCX_TESTING_PREFIX", "ucx")
Threader = partial(ThreadedExecution, num_threads=NUM_THREADS, rate_limit=RateLimitConfig())


@pytest.fixture(scope="session")
def ws() -> ImprovedWorkspaceClient:
    auth_config = AuthConfig(
        workspace=WorkspaceAuthConfig(
            host=os.environ["DATABRICKS_WS_HOST"],
            client_id=os.environ["DATABRICKS_COMMON_CLIENT_ID"],
            client_secret=os.environ["DATABRICKS_COMMON_CLIENT_SECRET"],
        )
    )
    provider.set_ws_client(auth_config, pool_size=NUM_THREADS)
    yield provider.ws


@pytest.fixture(scope="session", autouse=True)
def acc() -> AccountClient:
    acc_client = AccountClient(
        host=os.environ["DATABRICKS_ACC_HOST"],
        client_id=os.environ["DATABRICKS_COMMON_CLIENT_ID"],
        client_secret=os.environ["DATABRICKS_COMMON_CLIENT_SECRET"],
        account_id=os.environ["DATABRICKS_ACC_ACCOUNT_ID"],
    )
    yield acc_client


@pytest.fixture(scope="session", autouse=True)
def dbconnect(ws: ImprovedWorkspaceClient):
    dbc_cluster = next(filter(lambda c: c.cluster_name == DB_CONNECT_CLUSTER_NAME, ws.clusters.list()), None)

    if dbc_cluster:
        logger.debug(f"Integration testing cluster {DB_CONNECT_CLUSTER_NAME} already exists, skipping it's creation")
    else:
        logger.debug("Creating a cluster for integration testing")
        request = {
            "cluster_name": DB_CONNECT_CLUSTER_NAME,
            "spark_version": "13.2.x-scala2.12",
            "instance_pool_id": os.environ["TEST_POOL_ID"],
            "driver_instance_pool_id": os.environ["TEST_POOL_ID"],
            "num_workers": 0,
            "spark_conf": {"spark.master": "local[*, 4]", "spark.databricks.cluster.profile": "singleNode"},
            "custom_tags": {
                "ResourceClass": "SingleNode",
            },
            "data_security_mode": "SINGLE_USER",
            "autotermination_minutes": 180,
            "runtime_engine": "PHOTON",
        }

        dbc_cluster = ws.clusters.create(spark_version="13.2.x-scala2.12", request=Request(request))

        logger.debug(f"Cluster {dbc_cluster.cluster_id} created")

    os.environ["DATABRICKS_CLUSTER_ID"] = dbc_cluster.cluster_id
    yield


@pytest.fixture(scope="session", autouse=True)
def env(ws: ImprovedWorkspaceClient, acc: AccountClient, request: SubRequest) -> EnvironmentInfo:
    # prepare environment
    test_uid = f"{UCX_TESTING_PREFIX}_{str(uuid.uuid4())[:8]}"
    logger.debug(f"Creating environment with uid {test_uid}")
    groups = _create_groups(ws, acc, test_uid, NUM_TEST_GROUPS, Threader)

    def post_cleanup():
        print("\n")
        logger.debug("Cleaning up the environment")
        logger.debug("Deleting test groups")
        cleanups = [partial(_cleanup_groups, ws, acc, g) for g in groups]

        def error_silencer(func):
            def _wrapped(*args, **kwargs):
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"Cannot delete temp group, skipping it. Original exception {e}")

            return _wrapped

        silent_delete = error_silencer(ws.groups.delete)

        temp_cleanups = [
            partial(silent_delete, g.id) for g in ws.groups.list(filter=f"displayName sw 'db-temp-{test_uid}'")
        ]
        new_ws_groups_cleanups = [
            partial(silent_delete, g.id) for g in ws.groups.list(filter=f"displayName sw '{test_uid}'")
        ]

        all_cleanups = cleanups + temp_cleanups + new_ws_groups_cleanups
        Threader(all_cleanups).run()
        logger.debug(f"Finished cleanup for the environment {test_uid}")

    request.addfinalizer(post_cleanup)
    yield EnvironmentInfo(test_uid=test_uid, groups=groups)


@pytest.fixture(scope="session", autouse=True)
def instance_profiles(env: EnvironmentInfo, ws: ImprovedWorkspaceClient) -> list[InstanceProfile]:
    logger.debug("Adding test instance profiles")
    profiles: list[InstanceProfile] = []

    for i in range(NUM_TEST_INSTANCE_PROFILES):
        profile_arn = f"arn:aws:iam::123456789:instance-profile/{env.test_uid}-test-{i}"
        iam_role_arn = f"arn:aws:iam::123456789:role/{env.test_uid}-test-{i}"
        ws.instance_profiles.add(instance_profile_arn=profile_arn, iam_role_arn=iam_role_arn, skip_validation=True)
        profiles.append(InstanceProfile(instance_profile_arn=profile_arn, iam_role_arn=iam_role_arn))

    for ws_group, _ in env.groups:
        roles = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "roles",
                    "value": [{"value": p.instance_profile_arn} for p in random.choices(profiles, k=2)],
                }
            ],
        }
        provider.ws.api_client.do("PATCH", f"/api/2.0/preview/scim/v2/Groups/{ws_group.id}", data=json.dumps(roles))

    yield profiles

    logger.debug("Deleting test instance profiles")
    for profile in profiles:
        ws.instance_profiles.remove(profile.instance_profile_arn)
    logger.debug("Test instance profiles deleted")


@pytest.fixture(scope="session", autouse=True)
def instance_pools(env: EnvironmentInfo, ws: ImprovedWorkspaceClient) -> list[CreateInstancePoolResponse]:
    logger.debug("Creating test instance pools")

    test_instance_pools: list[CreateInstancePoolResponse] = [
        ws.instance_pools.create(instance_pool_name=f"{env.test_uid}-test-{i}", node_type_id="i3.xlarge")
        for i in range(NUM_TEST_INSTANCE_POOLS)
    ]

    _set_random_permissions(
        test_instance_pools,
        "instance_pool_id",
        RequestObjectType.INSTANCE_POOLS,
        env,
        ws,
        permission_levels=[PermissionLevel.CAN_ATTACH_TO, PermissionLevel.CAN_MANAGE],
    )

    yield test_instance_pools

    logger.debug("Deleting test instance pools")
    executables = [partial(ws.instance_pools.delete, p.instance_pool_id) for p in test_instance_pools]
    Threader(executables).run()


@pytest.fixture(scope="session", autouse=True)
def pipelines(env: EnvironmentInfo, ws: ImprovedWorkspaceClient) -> list[CreatePipelineResponse]:
    logger.debug("Creating test instance pools")

    test_pipelines: list[CreatePipelineResponse] = [
        ws.pipelines.create(
            name=f"{env.test_uid}-test-{i}",
            continuous=False,
            development=True,
            libraries=[PipelineLibrary(notebook=NotebookLibrary(path="/Workspace/sample-notebook"))],
        )
        for i in range(NUM_TEST_PIPELINES)
    ]

    _set_random_permissions(
        test_pipelines,
        "pipeline_id",
        RequestObjectType.PIPELINES,
        env,
        ws,
        permission_levels=[PermissionLevel.CAN_VIEW, PermissionLevel.CAN_RUN, PermissionLevel.CAN_MANAGE],
    )

    yield test_pipelines

    logger.debug("Deleting test instance pools")
    executables = [partial(ws.pipelines.delete, p.pipeline_id) for p in test_pipelines]
    Threader(executables).run()


@pytest.fixture(scope="session", autouse=True)
def jobs(env: EnvironmentInfo, ws: ImprovedWorkspaceClient) -> list[CreateResponse]:
    logger.debug("Creating test jobs")

    test_jobs: list[CreateResponse] = [
        ws.jobs.create(
            name=f"{env.test_uid}-test-{i}", job_clusters=[_get_basic_job_cluster()], tasks=[_get_basic_task()]
        )
        for i in range(NUM_TEST_JOBS)
    ]

    _set_random_permissions(
        test_jobs,
        "job_id",
        RequestObjectType.JOBS,
        env,
        ws,
        permission_levels=[PermissionLevel.CAN_VIEW, PermissionLevel.CAN_MANAGE_RUN, PermissionLevel.CAN_MANAGE],
    )

    yield test_jobs

    logger.debug("Deleting test jobs")
    executables = [partial(ws.jobs.delete, j.job_id) for j in test_jobs]
    Threader(executables).run()


@pytest.fixture(scope="session", autouse=True)
def cluster_policies(env: EnvironmentInfo, ws: ImprovedWorkspaceClient) -> list[CreatePolicyResponse]:
    logger.debug("Creating test cluster policies")

    test_cluster_policies: list[CreatePolicyResponse] = [
        ws.cluster_policies.create(
            name=f"{env.test_uid}-test-{i}",
            definition="""
        {
          "spark_version": {
                "type": "unlimited",
                "defaultValue": "auto:latest-lts"
            }
        }
        """,
        )
        for i in range(NUM_TEST_CLUSTER_POLICIES)
    ]

    _set_random_permissions(
        test_cluster_policies,
        "policy_id",
        RequestObjectType.CLUSTER_POLICIES,
        env,
        ws,
        permission_levels=[PermissionLevel.CAN_USE],
    )

    yield test_cluster_policies

    logger.debug("Deleting test instance pools")
    executables = [partial(ws.cluster_policies.delete, p.policy_id) for p in test_cluster_policies]
    Threader(executables).run()


@pytest.fixture(scope="session", autouse=True)
def clusters(env: EnvironmentInfo, ws: ImprovedWorkspaceClient) -> list[ClusterDetails]:
    logger.debug("Creating test clusters")

    test_clusters = [
        ws.clusters.create(
            spark_version="13.2.x-scala2.12",
            instance_pool_id=os.environ["TEST_POOL_ID"],
            driver_instance_pool_id=os.environ["TEST_POOL_ID"],
            cluster_name=f"{env.test_uid}-test-{i}",
            num_workers=1,
        )
        for i in range(NUM_TEST_CLUSTERS)
    ]

    _set_random_permissions(
        test_clusters,
        "cluster_id",
        RequestObjectType.CLUSTERS,
        env,
        ws,
        permission_levels=[PermissionLevel.CAN_ATTACH_TO, PermissionLevel.CAN_MANAGE, PermissionLevel.CAN_RESTART],
    )

    yield test_clusters

    logger.debug("Deleting test clusters")
    executables = [partial(ws.clusters.permanent_delete, c.cluster_id) for c in test_clusters]
    Threader(executables).run()
    logger.debug("Test clusters deleted")


@pytest.fixture(scope="session", autouse=True)
def verifiable_objects(
    clusters, instance_pools, cluster_policies, pipelines, jobs
) -> tuple[list, str, RequestObjectType]:
    _verifiable_objects = [
        (clusters, "cluster_id", RequestObjectType.CLUSTERS),
        (instance_pools, "instance_pool_id", RequestObjectType.INSTANCE_POOLS),
        (cluster_policies, "policy_id", RequestObjectType.CLUSTER_POLICIES),
        (pipelines, "pipeline_id", RequestObjectType.PIPELINES),
        (jobs, "job_id", RequestObjectType.JOBS),
    ]
    yield _verifiable_objects


@pytest.fixture()
def inventory_table(env: EnvironmentInfo) -> InventoryTable:
    table = InventoryTable(
        catalog="main",
        database="default",
        name=f"test_inventory_{env.test_uid}",
    )

    yield table

    logger.debug(f"Cleaning up inventory table {table}")
    try:
        provider.ws.tables.delete(table.to_spark())
        logger.debug(f"Inventory table {table} deleted")
    except Exception as e:
        logger.warning(f"Cannot delete inventory table, skipping it. Original exception {e}")