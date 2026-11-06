"""Offline checks of explicitly sourced HCS 8.5.1 request contracts.

Only parse the two authorized implementation files: never import the application,
read configuration, connect to a database, call a cloud, or manufacture responses.
These checks detect source/contract drift; they do not prove live compatibility.
Run independently: python3 backend/tests/test_hcs851_resource_evidence.py
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FETCHERS = ROOT / "backend/app/ledger/fetchers.py"
PAGER = ROOT / "backend/app/hcs/pager.py"


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef)
    }


class ResourceEvidenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = _functions(FETCHERS)
        pager = _functions(PAGER)["page_all"]
        cls.pager_defaults = {
            argument.arg: ast.literal_eval(default)
            for argument, default in zip(pager.args.kwonlyargs, pager.args.kw_defaults, strict=True)
            if default is not None
        }

    def _calls(self, function: str, name: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(self.functions[function])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        ]

    def _get(self, function: str, service: str) -> ast.Call:
        calls = [
            node
            for node in ast.walk(self.functions[function])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == service
        ]
        self.assertEqual(len(calls), 1, f"Review the documented {service} request in {function}")
        return calls[0]

    def _page(self, function: str) -> tuple[ast.Call, dict]:
        calls = self._calls(function, "page_all")
        self.assertEqual(len(calls), 1, f"Review pagination against the cited source: {function}")
        call = calls[0]
        arguments = dict(self.pager_defaults)
        arguments.update({keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords})
        return call, arguments

    def test_ecs_documented_page_number_contract(self):
        """[ECS1]: 0 and 1 mean first page; increasing pages should start at 1."""
        get = self._get("fetch_ecs", "ecs")
        self.assertEqual(ast.literal_eval(get.args[1]), "/v1/{project_id}/cloudservers/detail")
        page, arguments = self._page("fetch_ecs")
        self.assertEqual(ast.literal_eval(page.args[1]), "servers")
        self.assertEqual(arguments["mode"], "page")
        self.assertEqual(arguments["start"], 1)
        self.assertEqual(arguments["offset_param"], "offset")
        self.assertEqual(arguments["total_key"], "count")
        self.assertTrue(0 < arguments["limit"] <= 1000)

    def test_rds_documented_record_offset_and_limit(self):
        """[RDS1]: offset starts at zero; limit 1..100; instances/total_count."""
        get = self._get("fetch_rds", "rds")
        self.assertEqual(ast.literal_eval(get.args[1]), "/v3/{project_id}/instances")
        page, arguments = self._page("fetch_rds")
        self.assertEqual(ast.literal_eval(page.args[1]), "instances")
        self.assertEqual(arguments["mode"], "offset")
        self.assertEqual(arguments["start"], 0)
        self.assertEqual(arguments["offset_param"], "offset")
        self.assertEqual(arguments["total_key"], "total_count")
        self.assertTrue(1 <= arguments["limit"] <= 100)

    def test_dws_documented_list_path_and_response_key(self):
        """[DWS1]: v1.0 list, clusters array; no pagination parameters documented."""
        get = self._get("fetch_dws", "dws")
        self.assertEqual(ast.literal_eval(get.args[1]), "/v1.0/{project_id}/clusters")
        parameters = next((item.value for item in get.keywords if item.arg == "params"), None)
        if parameters is not None:
            self.assertIn(ast.literal_eval(parameters), (None, {}))
        reads = self._calls("fetch_dws", "response_items")
        self.assertTrue(any(ast.literal_eval(call.args[1]) == "clusters" for call in reads))

    def test_vpc_and_subnet_documented_marker_contracts(self):
        """[VPC1/VPC2]: v1 lists use last-resource-ID markers, not page numbers."""
        for function, path, key in (
            ("fetch_vpc", "/v1/{project_id}/vpcs", "vpcs"),
            ("_subnets_raw", "/v1/{project_id}/subnets", "subnets"),
        ):
            with self.subTest(function=function):
                get = self._get(function, "vpc")
                self.assertEqual(ast.literal_eval(get.args[1]), path)
                marker_calls = self._calls(function, "marker_all")
                self.assertEqual(len(marker_calls), 1)
                self.assertEqual(ast.literal_eval(marker_calls[0].args[1]), key)

    def test_eip_and_elb_native_paths_are_not_community_paths(self):
        """[EIP0/ELB2]: both native paths are in the explicit 8.5.1 guides."""
        for function, service, path in (
            ("fetch_eip", "vpc", "/v1/{project_id}/publicips"),
            ("fetch_elb", "elb", "/v3/{project_id}/elb/loadbalancers"),
        ):
            with self.subTest(function=function):
                get = self._get(function, service)
                self.assertEqual(ast.literal_eval(get.args[1]), path)
        # ELB's request table does not define limit/marker. Do not manufacture
        # parameter guarantees from its response page_info or a community API.

    def test_kafka_documented_paging_limit_and_required_engine(self):
        """[DMS1]: engine=kafka is required; each page has limit 1..50 (default 10)."""
        get = self._get("fetch_kafka", "dms")
        self.assertEqual(ast.literal_eval(get.args[1]), "/v2/{project_id}/instances")
        parameters = next((item.value for item in get.keywords if item.arg == "params"), None)
        self.assertIsInstance(parameters, ast.Dict)
        self.assertTrue(any(
            isinstance(key, ast.Constant) and key.value == "engine"
            and isinstance(value, ast.Constant) and value.value == "kafka"
            for key, value in zip(parameters.keys, parameters.values, strict=True)
        ))
        page, arguments = self._page("fetch_kafka")
        self.assertEqual(ast.literal_eval(page.args[1]), "instances")
        self.assertEqual(arguments["mode"], "offset")
        self.assertEqual(arguments["start"], 0)
        self.assertEqual(arguments["offset_param"], "offset")
        self.assertTrue(1 <= arguments["limit"] <= 50)
        # instance_num is merely called "instance quantity" in the source;
        # this test does not invent a documented total-count guarantee.

    def test_evs_record_paging_does_not_skip_first_volume(self):
        """[EVC1/EVC3]: preserve the >0 prose ambiguity; never start offset at 1."""
        calls = self._calls("_evs_raw", "page_all")
        for call in calls:
            arguments = dict(self.pager_defaults)
            arguments.update({keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords})
            self.assertEqual(arguments["mode"], "offset")
            self.assertEqual(arguments["start"], 0)
        # Marker pagination can omit the first marker; do not force one EVS path
        # on both product variants or assert an undocumented count field.
        self.assertTrue(calls or self._calls("_evs_raw", "marker_all"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
