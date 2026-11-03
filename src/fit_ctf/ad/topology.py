"""Declarative A&D networks, replicated team clusters and routed attachments."""

from __future__ import annotations

import ipaddress
import json
import random
import re
from collections import deque
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Service(StrictModel):
    image: str
    command: list[str] | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    volumes: list[str] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)


class Node(StrictModel):
    kind: Literal["router", "server"] = "router"
    networks: list[str] = Field(default_factory=list)
    services: dict[str, Service] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_services(self):
        if self.kind == "router" and self.services:
            raise ValueError("Services belong to server nodes; routers only forward packets")
        if self.kind == "server" and not self.services:
            raise ValueError("A server must define at least one service")
        return self


class Cluster(StrictModel):
    networks: list[str] = Field(default_factory=list)
    nodes: dict[str, Node]

    @model_validator(mode="after")
    def validate_networks(self):
        if not self.nodes:
            raise ValueError("A cluster must contain nodes")
        if len(set(self.networks)) != len(self.networks):
            raise ValueError("Duplicate network names")
        for name, node in self.nodes.items():
            if len(set(node.networks)) != len(node.networks):
                raise ValueError(f"Duplicate interfaces on {name}")
            if set(node.networks) - set(self.networks):
                raise ValueError(f"Unknown network on node {name}")
        return self


class Endpoint(StrictModel):
    cluster: str
    node: str


class Link(StrictModel):
    team: Endpoint
    main: str | None = None


class Topology(StrictModel):
    version: Literal[1] = 1
    address_pool: str = "10.128.0.0/9"
    general: Cluster = Field(default_factory=lambda: Cluster(nodes={"router": Node()}))
    team_clusters: dict[str, Cluster]
    links: dict[str, Link]

    @model_validator(mode="after")
    def validate_topology(self):
        pool = ipaddress.ip_network(self.address_pool)
        if pool.version != 4 or pool.prefixlen > 24 or not pool.is_private:
            raise ValueError("address_pool must be a private IPv4 network of /24 or larger")
        names = list(self.team_clusters) + list(self.links) + list(self.general.nodes)
        for cluster in [self.general, *self.team_clusters.values()]:
            names.extend(cluster.networks)
            names.extend(cluster.nodes)
            for node in cluster.nodes.values():
                names.extend(node.services)
        if any(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", n) is None for n in names):
            raise ValueError("Names must match [a-z][a-z0-9_]{0,39}")
        routers = {n for n, node in self.general.nodes.items() if node.kind == "router"}
        if not routers:
            raise ValueError("General topology needs at least one router")
        for name, link in self.links.items():
            cluster = self.team_clusters.get(link.team.cluster)
            node = cluster.nodes.get(link.team.node) if cluster else None
            if node is None or node.kind != "router":
                raise ValueError(f"Link {name}: team endpoint must name an existing router")
            if link.main is not None and link.main not in routers:
                raise ValueError(f"Link {name}: main endpoint must name an existing router")
        for name in self.team_clusters:
            if not any(link.team.cluster == name for link in self.links.values()):
                raise ValueError(f"Team cluster {name} needs at least one link")
        return self


def compile_topology(spec: Topology, teams: list[str], selections: dict | None = None) -> dict:
    """Allocate disjoint networks and compute next hops through router nodes only."""
    if len(teams) != len(set(teams)) or any(
        re.fullmatch(r"[a-z0-9_]+", team) is None for team in teams
    ):
        raise ValueError(
            "Team names must be unique and contain lowercase letters/digits/underscores"
        )
    selections = selections or {}
    nodes, networks, links, choices = {}, {}, {}, {}
    subnets = iter(ipaddress.ip_network(spec.address_pool).subnets(new_prefix=24))

    def add_network(key):
        try:
            subnet = next(subnets)
        except StopIteration as exc:
            raise ValueError("Address pool exhausted") from exc
        networks[key] = {"subnet": str(subnet), "members": {}}

    def attach(node_id, net):
        members = networks[net]["members"]
        if len(members) >= 253:
            raise ValueError(f"Too many interfaces on {net}")
        address = str(
            ipaddress.ip_network(networks[net]["subnet"]).network_address + len(members) + 2
        )
        members[node_id] = address
        nodes[node_id]["interfaces"][net] = address

    def add_cluster(prefix, cluster):
        for net in sorted(cluster.networks):
            add_network(f"{prefix}/net/{net}")
        for name, node in sorted(cluster.nodes.items()):
            key = f"{prefix}/node/{name}"
            nodes[key] = {
                "kind": node.kind,
                "interfaces": {},
                "services": {k: v.model_dump(exclude_none=True) for k, v in node.services.items()},
                "routes": [],
            }
            for net in sorted(node.networks):
                attach(key, f"{prefix}/net/{net}")

    add_cluster("main", spec.general)
    candidates = sorted(n for n, node in spec.general.nodes.items() if node.kind == "router")
    for team in sorted(teams):
        for name, cluster in sorted(spec.team_clusters.items()):
            add_cluster(f"team/{team}/{name}", cluster)
        for name, link in sorted(spec.links.items()):
            key = f"team/{team}/link/{name}"
            main = link.main or selections.get(key)
            if main not in candidates:
                main = random.SystemRandom().choice(candidates)
            choices[key] = main
            add_network(key)
            a = f"main/node/{main}"
            b = f"team/{team}/{link.team.cluster}/node/{link.team.node}"
            attach(a, key)
            attach(b, key)
            links[key] = {"main": a, "team": b, "network": key}

    # A multi-service server shares one network namespace; only routers are transit nodes.
    for source, node in nodes.items():
        visited = {source: None}
        distance = {source: 0}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            if current != source and nodes[current]["kind"] != "router":
                continue
            for net in nodes[current]["interfaces"]:
                for neighbor, ip in networks[net]["members"].items():
                    if neighbor in visited:
                        continue
                    visited[neighbor] = ip if current == source else visited[current]
                    distance[neighbor] = distance[current] + 1
                    queue.append(neighbor)
        for net, data in networks.items():
            if net in node["interfaces"]:
                continue
            reachable = [n for n in data["members"] if n in visited]
            if not reachable:
                raise ValueError(f"No routed path from {source} to {net}")
            closest = min(reachable, key=lambda n: distance[n])
            node["routes"].append({"subnet": data["subnet"], "via": visited[closest]})
        if not node["interfaces"]:
            # A lone default router is valid until teams are enrolled.
            if len(nodes) != 1:
                raise ValueError(f"Node {source} has no network attachment")
    return {
        "version": 1,
        "teams": sorted(teams),
        "nodes": nodes,
        "networks": networks,
        "links": links,
        "selections": choices,
    }


def write_runtime(plan: dict, project: str, destination: Path) -> Path:
    """Render self-contained Compose, route scripts, and router build context."""
    import hashlib

    destination.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(project.encode()).hexdigest()[:12]

    def key(value):
        return "n_" + hashlib.sha256(value.encode()).hexdigest()[:20]

    router = destination / "router"
    router.mkdir(exist_ok=True)
    (router / "Containerfile").write_text(
        "FROM alpine:3.20\nRUN apk add --no-cache iproute2 iptables\n", encoding="utf-8"
    )
    compose = {
        "name": f"fitctf-topology-{identity}",
        "x-podman": {"in_pod": False},
        "services": {},
        "networks": {},
    }
    for net, data in plan["networks"].items():
        compose["networks"][key(net)] = {
            "internal": True,
            "ipam": {"config": [{"subnet": data["subnet"]}]},
            "labels": {"fit_ctf.project": project, "fit_ctf.topology.network": net},
        }
    published = set()
    router_build_defined = False
    for name, node in plan["nodes"].items():
        node_key = key(name)
        script = destination / f"{node_key}.sh"
        commands = ["#!/bin/sh", "set -eu"]
        for route in node["routes"]:
            commands.append(f"ip route replace {route['subnet']} via {route['via']}")
        commands += ["touch /tmp/network-ready", "exec sleep infinity"]
        script.write_text("\n".join(commands) + "\n", encoding="utf-8", newline="\n")
        service = {
            "build": {"context": "./router", "dockerfile": "Containerfile"},
            "image": f"fitctf-topology-router:{identity}",
            "cap_add": ["NET_ADMIN"],
            "sysctls": {
                "net.ipv4.ip_forward": "1" if node["kind"] == "router" else "0",
                "net.ipv4.conf.all.rp_filter": "0",
            },
            "entrypoint": ["/bin/sh", "/network.sh"],
            "volumes": [f"./{script.name}:/network.sh:ro"],
            "healthcheck": {
                "test": ["CMD", "test", "-f", "/tmp/network-ready"],
                "interval": "1s",
                "timeout": "1s",
                "retries": 30,
            },
            "labels": {"fit_ctf.project": project, "fit_ctf.topology.node": name},
        }
        if router_build_defined:
            service.pop("build")
        router_build_defined = True
        if node["interfaces"]:
            service["networks"] = {
                key(net): {"ipv4_address": ip} for net, ip in node["interfaces"].items()
            }
        else:
            service["network_mode"] = "none"
        ports = []
        for app_name, app in node["services"].items():
            parts = name.split("/")
            context = {"project": project}
            if parts[0] == "team":
                team, cluster = parts[1:3]
                cluster_dir = destination.parent / "teams" / team / cluster
                (cluster_dir / "runtime" / "flags").mkdir(parents=True, exist_ok=True)
                context.update(
                    team=team, cluster=cluster, cluster_dir=cluster_dir.resolve().as_posix()
                )

            def expand(value):
                if isinstance(value, str):
                    for token, replacement in context.items():
                        value = value.replace("{" + token + "}", replacement)
                    return value
                if isinstance(value, list):
                    return [expand(item) for item in value]
                if isinstance(value, dict):
                    return {k: expand(v) for k, v in value.items()}
                return value

            app = expand(app)
            for port in app.pop("ports", []):
                # Require explicit host ports to catch collisions across replicas.
                host = port.rsplit(":", 1)[0]
                protocol = port.split("/", 1)[1] if "/" in port else "tcp"
                if ":" not in port or (host, protocol) in published:
                    raise ValueError(f"Duplicate or implicit host port: {port} on {name}")
                published.add((host, protocol))
                ports.append(port)
            compose["services"][key(name + "/service/" + app_name)] = {
                **app,
                "network_mode": f"service:{node_key}",
                "depends_on": {node_key: {"condition": "service_healthy"}},
                "labels": {
                    "fit_ctf.project": project,
                    "fit_ctf.topology.node": name,
                    "fit_ctf.topology.service": app_name,
                },
            }
        if ports:
            service["ports"] = ports
        compose["services"][node_key] = service
    output = destination / "compose.yaml"
    output.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    (destination / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return output


class TopologyManager:
    def __init__(self, app):
        self.app = app
        self.collection = app.ctf_db["ad_topology"]
        self.collection.create_index("project_id", unique=True)

    def root(self, project):
        return self.app.paths.project_path(project.name) / "topology"

    def load(self, project, raw):
        spec = Topology.model_validate(raw)
        existing = self.collection.find_one({"project_id": project.id}) or {}
        if existing.get("running"):
            raise ValueError("Stop the topology before changing its definition")
        self.collection.update_one(
            {"project_id": project.id},
            {
                "$set": {
                    "spec": spec.model_dump(),
                    "running": False,
                },
                "$unset": {"plan": ""},
            },
            upsert=True,
        )
        return spec

    def compile(self, project):
        doc = self.collection.find_one({"project_id": project.id})
        if not doc:
            raise ValueError("Load a topology definition first")
        if doc.get("running"):
            raise ValueError("Stop the topology before compiling it")
        teams = [
            self.app.team_mgr.get_doc_by_id(e.team_id.id).name
            for e in self.app.team_enroll_mgr.list_for_project(project)
        ]
        plan = compile_topology(
            Topology.model_validate(doc["spec"]), teams, doc.get("selections", {})
        )
        # A failed render must never leave an old plan startable with new files.
        self.collection.update_one({"project_id": project.id}, {"$unset": {"plan": ""}})
        write_runtime(plan, project.name, self.root(project))
        self.collection.update_one(
            {"project_id": project.id},
            {
                "$set": {
                    "plan": plan,
                    "selections": plan["selections"],
                }
            },
        )
        return plan

    def plan(self, project):
        doc = self.collection.find_one({"project_id": project.id}) or {}
        if "plan" not in doc:
            raise ValueError("Compile the topology first")
        return doc["plan"]

    async def run(self, project, action, verbose=False):
        self.plan(project)
        files = [self.root(project) / "compose.yaml"]
        if action == "status":
            return await self.app.c_client.compose_states(files)
        if action == "stop":
            code, _ = await self.app.c_client.compose_down(project.name, files, to_stdout=verbose)
        elif action == "build":
            code = await self.app.c_client.compose_build(project.name, files, to_stdout=verbose)
        else:
            doc = self.collection.find_one({"project_id": project.id})
            teams = sorted(
                self.app.team_mgr.get_doc_by_id(e.team_id.id).name
                for e in self.app.team_enroll_mgr.list_for_project(project)
            )
            if teams != doc["plan"]["teams"]:
                raise ValueError("Team enrollments changed; stop and compile the topology again")
            # Keep replacement locked even when Compose only starts some containers.
            self.collection.update_one({"project_id": project.id}, {"$set": {"running": True}})
            code = await self.app.c_client.compose_up(project.name, files, to_stdout=verbose)
        if code:
            raise RuntimeError(f"Topology {action} failed with exit code {code}")
        if action in {"start", "stop"}:
            self.collection.update_one(
                {"project_id": project.id},
                {
                    "$set": {
                        "running": action == "start",
                    }
                },
            )
        return code
