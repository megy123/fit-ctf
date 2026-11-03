import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import fit_ctf.components.container_client.container_client_interface as c_client
from fit_ctf.components.types import ErrorCode, HealthCheckDict, TaskSuccess
from yaml import safe_load


class PodmanClient(c_client.ContainerClientInterface):
    def generate_container_prefix(self, *names: str) -> str:
        return f"{'_'.join(names)}_"

    async def get_images(self, contains: str | list[str] | None = None) -> list[str]:
        cmd = ["podman", "images", "--format", '"{{ .Repository }}"']
        return await self._process_get_commands(cmd, contains)

    def create_networks(
        self, logger_name: str, network_names: list[str], to_stdout: bool = False
    ) -> ErrorCode:
        cmd = ["podman", "network", "create", *network_names]
        # TODO: subnets
        # TODO: create if not exist
        return self._run_logged_sync(cmd, logger_name, to_stdout=to_stdout)

    async def get_networks(self, contains: str | list[str] | None = None) -> list[str]:
        cmd = ["podman", "network", "ls", "--format", '"{{ .Name }}"']
        return await self._process_get_commands(cmd, contains)

    def rm_network(self, logger_name: str, name: str, to_stdout: bool = False) -> ErrorCode:
        cmd = ["podman", "network", "rm", name]
        return self._run_logged_sync(cmd, logger_name, to_stdout=to_stdout)

    async def rm_images(
        self, logger_name: str, contains: str | list[str], to_stdout: bool = False
    ) -> ErrorCode:
        images = await self.get_images(contains)
        if not images:
            return -1
        cmd = ["podman", "rmi"] + images
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)
        await proc.wait()
        return proc.returncode if proc.returncode is not None else 255

    async def rm_networks(
        self, logger_name: str, contains: str | list[str], to_stdout: bool = False
    ) -> ErrorCode:
        network_names = await self.get_networks(contains)
        if not network_names:
            return -1
        cmd = ["podman", "network", "rm"] + network_names
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)
        return proc.returncode if proc.returncode is not None else 255

    async def compose_up(
        self, logger_name: str, files: list[Path], to_stdout: bool = False
    ) -> ErrorCode:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f"podman-compose {' '.join(file_flags)} up -d"
        proc = await asyncio.create_subprocess_exec(
            *cmd.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)

        await proc.wait()
        if proc.returncode == 0:
            # podman-compose 1.6 can leave services only on its default network
            # when external networks are declared. Explicitly attach every
            # compose container to the networks declared by the rendered files.
            services = []
            for path in files:
                document = safe_load(path.read_text(encoding="utf-8")) or {}
                project_name = str(document.get("name", ""))
                for service_name, service in document.get("services", {}).items():
                    services.append((project_name, service_name, service))
            list_proc = await asyncio.create_subprocess_exec(
                "podman",
                "ps",
                "-a",
                "--format",
                "{{.Names}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            listed, _ = await list_proc.communicate()
            containers = [line.strip() for line in listed.decode().splitlines() if line.strip()]
            for project_name, service_name, service in services:
                prefix = f"{project_name}_"
                # podman-compose uses the first compose file's project name
                # for all merged files, so the scenario directory is not
                # reliable here. Match the actual service component instead.
                matching = [
                    name
                    for name in containers
                    if name.startswith(prefix) and f"_{service_name}_" in name
                ]
                for container in matching:
                    for network, network_config in (service.get("networks") or {}).items():
                        disconnect = await asyncio.create_subprocess_exec(
                            "podman", "network", "disconnect", "--force", network, container,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        await disconnect.communicate()
                        args = ["podman", "network", "connect"]
                        aliases = []
                        if isinstance(network_config, dict):
                            aliases.extend(network_config.get("aliases", []))
                        if service.get("hostname"):
                            aliases.append(service["hostname"])
                        for alias in dict.fromkeys(aliases):
                            args.extend(["--alias", str(alias)])
                        args.extend([network, container])
                        connect = await asyncio.create_subprocess_exec(
                            *args,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await connect.communicate()
                    # A network connected after container creation does not
                    # update resolv.conf. Refresh it from the shared-network
                    # gateway once all interfaces are present.
                    refresh_dns = await asyncio.create_subprocess_exec(
                        "podman",
                        "exec",
                        container,
                        "sh",
                        "-c",
                        "dns=$(ip -4 route show default dev eth1 2>/dev/null | awk 'NR == 1 {print $3}'); "
                        "if [ -n \"$dns\" ]; then printf 'nameserver %s\\n' \"$dns\" > /etc/resolv.conf; fi",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await refresh_dns.communicate()
                    # podman-compose creates merged services on its default
                    # network even when the compose file declares only
                    # external A&D networks. Remove that escape route after
                    # the intended networks are attached.
                    disconnect_default = await asyncio.create_subprocess_exec(
                        "podman",
                        "network",
                        "disconnect",
                        "--force",
                        "podman",
                        container,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await disconnect_default.communicate()
        return proc.returncode if proc.returncode is not None else 255

    async def compose_down(
        self, logger_name: str, files: list[Path], to_stdout: bool = False
    ) -> tuple[ErrorCode, TaskSuccess]:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f"podman-compose {' '.join(file_flags)} ps -q"
        proc = await asyncio.create_subprocess_exec(
            *cmd.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if not stdout.decode().strip():
            return 0, False
        cmd = f"podman-compose {' '.join(file_flags)} down"
        proc = await asyncio.create_subprocess_exec(
            *cmd.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)
        await proc.wait()
        if proc.returncode is None:
            return 255, False
        if proc.returncode:
            return proc.returncode, False
        # return code 0 means correct clear
        return proc.returncode, not proc.returncode

    async def compose_ps(self, files: list[Path]) -> list[str]:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f'podman-compose {" ".join(file_flags)} ps --format "{{ .Names }}"'
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        return [data.strip('"') for data in stdout.decode().rsplit()]

    async def compose_ps_json(self, files: list[Path]) -> list[dict[str, Any]]:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f"podman-compose {' '.join(file_flags)} ps --format json"
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
        return data

    async def compose_build(
        self, logger_name: str, files: list[Path], to_stdout: bool = False
    ) -> int:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f"podman-compose {' '.join(file_flags)} build"
        proc = await asyncio.create_subprocess_exec(
            *cmd.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)
        await proc.wait()
        return proc.returncode if proc.returncode is not None else 255

    async def compose_logs(
        self,
        logger_name: str,
        files: list[Path],
        *,
        tail: int = 500,
        service: str | None = None,
        to_stdout: bool = True,
    ) -> ErrorCode:
        if not files:
            return 1
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        tail_arg = f"--tail={tail}"
        parts = ["podman-compose"] + file_flags + ["logs", tail_arg]
        if service:
            parts.append(service)
        cmd = " ".join(parts)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)
        await proc.wait()
        return proc.returncode if proc.returncode is not None else 255

    def compose_shell(
        self, files: list[Path], service: str, command: str
    ) -> subprocess.CompletedProcess:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f"podman-compose {' '.join(file_flags)} exec {service} {command}"
        return subprocess.run(cmd.split(), stdout=sys.stdout, stderr=sys.stderr)

    async def stats(self, project_name: str) -> list[dict[str, str]]:
        cmd = [
            "podman",
            "stats",
            "--no-stream",
            "--format",
            # "table {{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.UpTime}}",
            "json",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
        return [d for d in data if d["name"].startswith(project_name)]

    async def ps(self, project_name: str) -> list[str]:
        cmd = [
            "podman",
            "ps",
            "-a",
            "--format",
            "table {{.Names}} {{.Networks}} {{.Ports}} {{.State}} {{.CreatedHuman}}",
            f"--filter=name=^{project_name}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        return [data.strip('"') for data in stdout.decode().rsplit("\n") if data]

    async def ps_json(self, project_name: str) -> list[dict[str, Any]]:
        cmd = [
            "podman",
            "ps",
            "-a",
            "--format",
            "json",
            f"--filter=name=^{project_name}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
        return data

    async def ps_csv(self, project_name: str, output_file: Path) -> None:
        # TODO: implement
        pass

    async def compose_states(self, files: list[Path]) -> list[HealthCheckDict]:
        file_flags = [f"-f {str(path.resolve())}" for path in files]
        cmd = f"podman-compose {' '.join(file_flags)} ps --format json"
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        if not stdout.strip():
            return []
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        return [
            {
                "name": service["Names"][0],
                "state": service["State"],
                "image": service["Image"],
            }
            for service in data
        ]

    async def project_stats(self, project_name: str) -> list[dict]:
        cmd = [
            "podman",
            "ps",
            "-a",
            "--format",
            "json",
            "--filter",
            f"label=project={project_name}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
        return data

    async def build_image(
        self,
        logger_name: str,
        context_path: Path,
        image_name: str,
        containerfile: str = "Containerfile",
        to_stdout: bool = False,
    ) -> ErrorCode:
        """Build a container image from a Containerfile/Dockerfile.

        :param logger_name: Logger name for output
        :type logger_name: str
        :param context_path: Path to build context directory
        :type context_path: Path
        :param image_name: Name to tag the built image
        :type image_name: str
        :param containerfile: Name of Containerfile/Dockerfile (default: "Containerfile")
        :type containerfile: str
        :param to_stdout: Pipe output to stdout as well
        :type to_stdout: bool
        :return: An exit code
        :rtype: ErrorCode
        """
        cmd = [
            "podman",
            "build",
            "-t",
            image_name,
            "-f",
            str(context_path / containerfile),
            str(context_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._process_logging(proc, logger_name=logger_name, to_stdout=to_stdout)
        await proc.wait()
        return proc.returncode if proc.returncode is not None else 255
