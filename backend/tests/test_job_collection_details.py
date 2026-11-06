"""作业面板所需原始执行状态、结果和实例信息必须由采集事实提供。"""
from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from app.collectors.mrs import MrsJobCollector


class DetailClient:
    def get(self, service, path, **kwargs):
        if path.endswith("/jobs"):
            return {"jobs": [{"name": "stream-a", "jobType": "REAL_TIME", "engine": "Flink"}], "total": 1}
        if path.endswith("/status"):
            return {"status": "NORMAL", "startTime": 1000,
                    "nodes": [{"name": "source", "status": "NORMAL"},
                              {"name": "sink", "status": "EXCEPTION"}]}
        if path.endswith("/instances/detail"):
            return {"instances": [
                {"jobName": "batch-a", "instanceId": 81, "status": "success", "jobType": "BATCH",
                 "planTime": 1000, "startTime": 2000, "endTime": 122000},
                {"jobName": "batch-waiting", "instanceId": 82, "status": "waiting", "planTime": 1000},
            ], "total": 2}
        if path.endswith("/cluster_infos"):
            return {"clusters": [{"clusterId": "cluster-a", "clusterName": "MRS A"}], "clusterTotal": 1}
        if path.endswith("/job-executions"):
            return {"job_list": [
                {"job_name": "flink-a", "job_id": "execution-a", "job_type": "Flink",
                 "job_state": "RUNNING", "job_result": "UNDEFINED", "submitted_time": 1000,
                 "elapsed_time": 120000, "queue": "production"},
                {"job_name": "spark-a", "job_id": "execution-b", "job_type": "Spark",
                 "job_state": "FINISHED", "job_result": "FAILED", "submitted_time": 1000},
            ], "total_record": 2}
        raise AssertionError(path)


def context():
    return RunContext(client=DetailClient(), account="test", project_id="project-a", workspaces=["workspace-a"])


def test_realtime_preserves_engine_and_individual_node_states():
    jobs = DataArtsRealtimeCollector().collect(context())
    job = next(o for o in jobs if o.check_item == "job_status")
    assert job.attrs["engine"] == "Flink"
    assert job.attrs["job_type"] == "REAL_TIME"
    assert job.attrs["start_time"] == 1000
    assert job.attrs["job_status"] == "NORMAL"
    assert job.metrics == {"nodes": 2, "bad_nodes": 1}
    assert job.attrs["nodes"][1] == {"name": "sink", "status": "EXCEPTION", "type": None}
    assert len([o for o in jobs if o.check_item == "node_status"]) == 2


def test_batch_instance_times_and_missing_duration_are_distinct():
    batch, waiting = DataArtsBatchCollector().collect(context())
    assert batch.attrs["instance_id"] == 81
    assert batch.attrs["plan_time"] == 1000
    assert batch.attrs["start_time"] == 2000
    assert batch.attrs["end_time"] == 122000
    assert batch.attrs["instance_status"] == "success"
    assert batch.metrics["run_minutes"] == 2.0
    assert waiting.metrics["run_minutes"] is None
    assert waiting.attrs["engine"] is None  # 不从作业名字或实时/批分类猜测引擎。


def test_mrs_running_state_is_not_hidden_by_undefined_result():
    ctx = context()
    ctx.params["elapsed_time_unit"] = "milliseconds"  # 本测试替身明确使用毫秒，不推广到未知局点。
    running, failed = MrsJobCollector().collect(ctx)
    assert running.status_raw == "RUNNING"
    assert running.attrs["job_result"] == "UNDEFINED"
    assert running.attrs["job_state"] == "RUNNING"
    assert running.attrs["cluster_id"] == "cluster-a"
    assert running.attrs["job_id"] == "execution-a"
    assert running.attrs["job_type"] == "Flink"
    assert running.attrs["submitted_time"] == 1000
    assert running.metrics["elapsed_min"] == 2.0
    assert failed.status_raw == "FINISHED"
    assert failed.attrs["job_result"] == "FAILED"
    assert failed.metrics["elapsed_min"] is None
    assert failed.facts["job_result"] == "FAILED"  # 巡检规则仍按原事实判定。
