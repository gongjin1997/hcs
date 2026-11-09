"""作业面板所需原始执行状态、结果和实例信息必须由采集事实提供。"""
from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from app.collectors.mrs import DataArtsMrsNodeCollector


class DetailClient:
    def get(self, service, path, **kwargs):
        if path.endswith("/jobs"):
            if kwargs["params"]["jobType"] == "BATCH":
                return {"jobs": [{"name": name, "jobType": "BATCH"} for name in ("batch-a", "batch-waiting")], "total": 2}
            return {"jobs": [{"name": "stream-a", "jobType": "REAL_TIME"}], "total": 1}
        if path in ("/v1/{project_id}/jobs/stream-a", "/v1/{project_id}/jobs/batch-a", "/v1/{project_id}/jobs/batch-waiting"):
            name = path.rsplit("/", 1)[1]
            return {"name": name, "processType": "REAL_TIME" if name == "stream-a" else "BATCH",
                    "singleNodeJobType": "FlinkSQL" if name == "stream-a" else None, "nodes": []}
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
        tail = path.rsplit("/", 1)[1]
        if "/instances/" in path and tail.isdigit():
            if tail == "81":
                nodes = [{"nodeName": "spark", "type": "MRSSpark", "status": "fail", "startTime": 2000, "endTime": 62000,
                          "retryTimes": 2, "logPath": "obs://logs/spark"},
                         {"nodeName": "notify", "type": "SMN", "status": "success", "startTime": 62000}]
            else:
                nodes = [{"nodeName": "flink", "type": "MRSFlinkJob", "status": "waiting"}]
            return {"jobName": path.split("/jobs/")[1].split("/")[0], "instanceId": int(tail), "nodes": nodes}
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


def test_mrs_nodes_keep_node_status_instance_status_and_retry_facts():
    failed, waiting = DataArtsMrsNodeCollector().collect(context())
    assert failed.resource_id == "workspace-a/batch-a/spark" and failed.attrs["node_type"] == "MRSSpark"
    assert failed.status_raw == "fail" and failed.facts["node_status"] == "fail"
    assert failed.facts["job_status"] == "success"  # 节点失败不改写所属实例状态
    assert failed.attrs["instance_id"] == 81 and failed.attrs["retry_times"] == 2
    assert failed.attrs["start_time"] == 2000 and failed.attrs["end_time"] == 62000
    assert failed.metrics["run_minutes"] == 1.0 and failed.attrs["log_path"] == "obs://logs/spark"
    assert waiting.status_raw == "waiting" and waiting.attrs["node_type"] == "MRSFlinkJob"
    assert waiting.metrics["run_minutes"] is None and "run_minutes" not in waiting.facts
