import asyncio
import copy
import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import click
import mongomock
import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from fit_ctf.ad.topology import Topology, TopologyManager, compile_topology, write_runtime

ROOT = Path(__file__).resolve().parents[2]


def definition(name="minimal"):
    return yaml.safe_load((ROOT / "config" / "topology" / f"{name}.yaml").read_text())


def test_default_router_and_real_routes():
    plan = compile_topology(Topology.model_validate(definition()), ["red", "blue"])
    assert list(n for n in plan["nodes"] if n.startswith("main/")) == ["main/node/router"]
    red = plan["nodes"]["team/red/web/node/server"]
    blue_network = plan["networks"]["team/blue/web/net/lan"]["subnet"]
    gateway_ip = plan["nodes"]["team/red/web/node/gateway"]["interfaces"]["team/red/web/net/lan"]
    assert {"subnet": blue_network, "via": gateway_ip} in red["routes"]


def test_every_pair_has_a_loop_free_bidirectional_packet_path():
    plan = compile_topology(Topology.model_validate(definition("five-clusters")), ["red", "blue"])
    nodes = plan["nodes"]
    for source in nodes:
        for target, target_data in nodes.items():
            address = next(iter(target_data["interfaces"].values()))
            current, visited = source, set()
            while current != target:
                assert current not in visited, (source, target, visited)
                visited.add(current)
                connected = [
                    net
                    for net in nodes[current]["interfaces"]
                    if address in plan["networks"][net]["members"].values()
                ]
                if connected:
                    current = target
                    continue
                route = next(
                    r
                    for r in nodes[current]["routes"]
                    if ipaddress.ip_address(address) in ipaddress.ip_network(r["subnet"])
                )
                neighbors = [
                    member
                    for net in nodes[current]["interfaces"]
                    for member, ip in plan["networks"][net]["members"].items()
                    if ip == route["via"]
                ]
                assert len(neighbors) == 1
                current = neighbors[0]
                assert nodes[current]["kind"] == "router"


def test_five_clusters_multiple_links_and_persisted_random_choices(tmp_path):
    spec = Topology.model_validate(definition("five-clusters"))
    plan = compile_topology(spec, ["blue", "red"])
    assert len(plan["nodes"]) == 22
    assert len(plan["links"]) == 12
    assert len(plan["networks"]) == 23
    assert len(set(n["subnet"] for n in plan["networks"].values())) == 23
    assert compile_topology(spec, ["red", "blue"], plan["selections"]) == plan
    file = write_runtime(plan, "competition", tmp_path / "topology")
    compose = yaml.safe_load(file.read_text())
    assert len(compose["services"]) == 42  # 22 namespaces and 20 application services
    assert sum("build" in service for service in compose["services"].values()) == 1
    assert compose["x-podman"]["in_pod"] is False
    for service in compose["services"].values():
        if "network_mode" in service:
            assert service["network_mode"].startswith("service:")
            assert "entrypoint" not in service  # Preserve the application image entrypoint
    assert json.loads((file.parent / "plan.json").read_text()) == plan


def test_different_cluster_sizes_and_service_counts():
    raw = definition()
    large = copy.deepcopy(raw["team_clusters"]["web"])
    large["nodes"]["second"] = copy.deepcopy(large["nodes"]["server"])
    large["nodes"]["second"]["services"]["metrics"] = {
        "image": "python:3.12-alpine",
        "command": ["python", "-m", "http.server", "9090"],
    }
    raw["team_clusters"]["large"] = large
    raw["links"]["second"] = {"team": {"cluster": "large", "node": "gateway"}}
    plan = compile_topology(Topology.model_validate(raw), ["red", "blue", "green"])
    assert len(plan["nodes"]) == 16  # main + 3 * (2 + 3)
    assert len(plan["nodes"]["team/red/large/node/second"]["services"]) == 2


@pytest.mark.parametrize(
    "modify",
    [
        lambda raw: raw["links"]["uplink"].pop("team"),
        lambda raw: raw["links"]["uplink"].update(main="missing"),
        lambda raw: raw["links"]["uplink"]["team"].update(node="server"),
        lambda raw: raw["team_clusters"]["web"]["nodes"]["server"].update(networks=["missing"]),
        lambda raw: raw.update(address_pool="8.0.0.0/8"),
        lambda raw: raw.update(unknown=True),
    ],
)
def test_invalid_definitions(modify):
    raw = definition()
    modify(raw)
    with pytest.raises(ValidationError):
        Topology.model_validate(raw)


def test_disconnected_general_network_is_rejected():
    raw = definition()
    raw["general"] = {"nodes": {"a": {}, "b": {}}}
    raw["links"]["uplink"]["main"] = "a"
    with pytest.raises(ValueError, match="No routed path"):
        compile_topology(Topology.model_validate(raw), ["red"])


def test_non_router_is_not_used_as_transit():
    raw = definition()
    cluster = raw["team_clusters"]["web"]
    cluster["networks"].append("isolated")
    cluster["nodes"]["server"]["networks"].append("isolated")
    cluster["nodes"]["hidden"] = copy.deepcopy(cluster["nodes"]["server"])
    cluster["nodes"]["hidden"]["networks"] = ["isolated"]
    with pytest.raises(ValueError, match="No routed path"):
        compile_topology(Topology.model_validate(raw), ["red"])


def fake_app(tmp_path):
    app = SimpleNamespace(ctf_db=mongomock.MongoClient().db)
    app.paths = SimpleNamespace(
        project_path=lambda project: (
            tmp_path / (project if isinstance(project, str) else project.name)
        )
    )
    app.team_mgr = SimpleNamespace(get_doc_by_id=lambda team: SimpleNamespace(name=team))
    app.team_enroll_mgr = SimpleNamespace(
        list_for_project=lambda p: [
            SimpleNamespace(team_id=SimpleNamespace(id=team)) for team in ["red", "blue"]
        ]
    )
    app.c_client = SimpleNamespace(
        compose_up=AsyncMock(return_value=0),
        compose_down=AsyncMock(return_value=(0, True)),
        compose_build=AsyncMock(return_value=0),
        compose_states=AsyncMock(return_value=[]),
    )
    app.topology = TopologyManager(app)
    app.prj_mgr = SimpleNamespace(get_project=lambda n: SimpleNamespace(id="project", name=n))
    return app


def test_manager_load_compile_lifecycle_and_enrollment_changes(tmp_path):
    app = fake_app(tmp_path)
    project = app.prj_mgr.get_project("competition")
    app.topology.load(project, definition())
    plan = app.topology.compile(project)
    assert app.topology.compile(project) == plan
    asyncio.run(app.topology.run(project, "build"))
    asyncio.run(app.topology.run(project, "start"))
    with pytest.raises(ValueError, match="Stop"):
        app.topology.compile(project)
    with pytest.raises(ValueError, match="Stop"):
        app.topology.load(project, definition())
    asyncio.run(app.topology.run(project, "stop"))
    app.team_enroll_mgr.list_for_project = lambda p: []
    with pytest.raises(ValueError, match="enrollments changed"):
        asyncio.run(app.topology.run(project, "start"))


def test_project_disable_stops_topology_before_deactivating(tmp_path):
    from fit_ctf.models.core.project import ProjectManager

    app = fake_app(tmp_path)
    project = app.prj_mgr.get_project("competition")
    project.active = True
    app.topology.load(project, definition())
    app.topology.compile(project)
    manager = SimpleNamespace(
        _db=app.ctf_db,
        paths=app.paths,
        c_client=app.c_client,
        get_project=lambda p: p,
        _project_cluster_mgr=SimpleNamespace(
            get_cluster=lambda p: object(), stop_cluster=AsyncMock()
        ),
        update_doc=lambda p: None,
    )
    enroll = SimpleNamespace(
        get_enrollments_for_project=lambda p: [], disable_multiple_enrollments=AsyncMock()
    )
    app.c_client.compose_down.return_value = (1, False)
    with pytest.raises(RuntimeError, match="stopping its A&D topology failed"):
        asyncio.run(ProjectManager.disable_project(manager, project, enroll))
    assert project.active
    app.c_client.compose_down.return_value = (0, True)
    asyncio.run(ProjectManager.disable_project(manager, project, enroll))
    assert not project.active


def test_cli_commands(tmp_path):
    from fit_ctf_cli.cli.ad import ad

    root = click.Group()
    root.add_command(ad)

    app = fake_app(tmp_path)
    runner = CliRunner()
    commands = [
        ["load", "-f", str(ROOT / "config/topology/minimal.yaml")],
        ["compile"],
        ["plan"],
        ["build"],
        ["start"],
        ["status"],
        ["stop"],
    ]
    for command in commands:
        result = runner.invoke(
            root, ["ad", "topology", *command, "-pn", "competition"], obj={"ctf_app": app}
        )
        assert result.exit_code == 0, (result.output, result.exception)
