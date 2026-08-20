from conftest import signed_task

from agent_scheduler.domain.models import ExecutionPlan, PrepareManifest, new_id, utc_now
from agent_scheduler.integrity import sign_model
from agent_scheduler.storage import EventStore
from agent_scheduler.worker.docker import ContainerInspection, DockerError, DockerTimeout, Mount
from agent_scheduler.worker.driver import DockerWorkerDriver

IMAGE_ID = "sha256:601d5316da88819c0729b33767b4efad7eb6b50a0e006fffc08624bc647bcbed"


class StartThenFailDocker:
    def __init__(self, cleanup_fails: bool):
        self.running = False
        self.cleanup_fails = cleanup_fails
        self.stop_calls = 0

    def inspect(self, name):
        return ContainerInspection(
            exists=True,
            running=self.running,
            image_id=IMAGE_ID,
            config_image="tag",
            user="root",
            working_dir="/workspace",
            privileged=True,
            network_mode="host",
            ipc_mode="host",
            shm_size=16 * 1024**3,
            devices=(
                ("/dev/dri", "/dev/dri"),
                ("/dev/kfd", "/dev/kfd"),
                ("/dev/mkfd", "/dev/mkfd"),
            ),
            mounts=(
                Mount("/public/share", "/data", True),
                Mount("/public/home/zz_chentian", "/home", True),
                Mount("/mnt/nvme1/models", "/models", False),
                Mount("/opt/hyhal", "/opt/hyhal", False),
            ),
        )

    def image_repo_digests(self, image):
        return (
            "harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238",
        )

    def start(self, name):
        self.running = True
        raise DockerTimeout("client timed out after daemon started container")

    def stop(self, name, grace_seconds=30):
        self.stop_calls += 1
        if self.cleanup_fails:
            raise DockerError("daemon unavailable")
        self.running = False
        return self.inspect(name), None


def manifest_and_plan(task, identity):
    unit = task.units[0]
    manifest = sign_model(
        PrepareManifest(
            key_id=identity.key_id,
            manifest_id=new_id("manifest"),
            task_id=task.task_id,
            execution_id=task.execution_id,
            assignment_id=new_id("assign"),
            dispatch_generation=1,
            worker_id=unit.worker_id,
            gpu_ids=(0,),
            lease_epoch=1,
            container_name=unit.container_name,
            created_at=utc_now(),
        ),
        identity.signing_private_key,
    )
    plan = sign_model(
        ExecutionPlan(
            key_id=identity.key_id,
            plan_id=new_id("plan"),
            assignment_id=manifest.assignment_id,
            execution_id=task.execution_id,
            task_id=task.task_id,
            task_content_hash=task.content_hash or "0" * 64,
            dispatch_generation=1,
            worker_id=unit.worker_id,
            unit_id=unit.unit_id,
            gpu_ids=(0,),
            lease_epoch=1,
            container_name=unit.container_name,
            submitter_username=unit.submitter_username,
            container_user=unit.container_user,
            image_digest=unit.image_digest,
            created_at=utc_now(),
        ),
        identity.signing_private_key,
    )
    return manifest, plan


def test_start_side_effect_always_triggers_cleanup(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    docker = StartThenFailDocker(cleanup_fails=False)
    driver = DockerWorkerDriver(
        docker, EventStore(root), identity.signing_public_key, identity.key_id
    )
    manifest, plan = manifest_and_plan(task, identity)
    assert driver.prepare(manifest, task)
    assert driver.acknowledge_plan(plan, task)
    result = driver.execute(plan, task)
    assert docker.stop_calls == 1
    assert result.cleanup_ok
    assert not docker.running


def test_start_uncertainty_reports_cleanup_failure(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    docker = StartThenFailDocker(cleanup_fails=True)
    driver = DockerWorkerDriver(
        docker, EventStore(root), identity.signing_public_key, identity.key_id
    )
    manifest, plan = manifest_and_plan(task, identity)
    assert driver.prepare(manifest, task)
    assert driver.acknowledge_plan(plan, task)
    result = driver.execute(plan, task)
    assert docker.stop_calls == 1
    assert not result.cleanup_ok
    assert docker.running
