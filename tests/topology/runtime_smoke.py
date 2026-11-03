"""Run with Linux rootful Podman: python runtime_smoke.py /path/to/podman-compose."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from fit_ctf.ad.topology import Topology, compile_topology, write_runtime


def main():
    engine = sys.argv[2] if len(sys.argv) > 2 else "podman"
    root = Path(__file__).resolve().parents[2]
    spec = Topology.model_validate(
        yaml.safe_load((root / "config/topology/five-clusters.yaml").read_text())
    )
    plan = compile_topology(spec, ["red", "blue"])
    with tempfile.TemporaryDirectory(prefix="fitctf-topology-") as tmp:
        output = write_runtime(plan, "topology-runtime-verification", Path(tmp) / "topology")
        if os.getenv("FITCTF_BUILD_NETWORK"):
            compose = yaml.safe_load(output.read_text())
            for service in compose["services"].values():
                if "build" in service:
                    service["build"]["network"] = os.environ["FITCTF_BUILD_NETWORK"]
            output.write_text(yaml.safe_dump(compose))
        prefix = [sys.argv[1], "-f", str(output)]
        try:
            subprocess.run(prefix + ["build"], check=True)
            subprocess.run(prefix + ["up", "-d"], check=True)
            checked = 0
            for team, other in [("red", "blue"), ("blue", "red")]:
                source = subprocess.check_output(
                    [
                        engine,
                        "ps",
                        "-q",
                        "--filter",
                        "label=fit_ctf.topology.service=web",
                        "--filter",
                        f"label=fit_ctf.topology.node=team/{team}/frontend/node/app",
                    ],
                    text=True,
                ).strip()
                assert source
                for cluster in spec.team_clusters:
                    node = plan["nodes"][f"team/{other}/{cluster}/node/app"]
                    address = next(iter(node["interfaces"].values()))
                    for port in [8080, 9090]:
                        code = (
                            "import urllib.request; "
                            f"r=urllib.request.urlopen('http://{address}:{port}', timeout=5); "
                            "assert r.status == 200"
                        )
                        for attempt in range(20):
                            result = subprocess.run(
                                [engine, "exec", source, "python", "-c", code]
                            )
                            if result.returncode == 0:
                                break
                            time.sleep(1)
                        else:
                            raise AssertionError(
                                f"Cannot reach {other}/{cluster}:{port} from {team}"
                            )
                        checked += 1
            print(
                json.dumps(
                    {
                        "nodes": len(plan["nodes"]),
                        "links": len(plan["links"]),
                        "networks": len(plan["networks"]),
                        "http_checks": checked,
                    }
                )
            )
        finally:
            subprocess.run(prefix + ["down"], check=True)


if __name__ == "__main__":
    main()
