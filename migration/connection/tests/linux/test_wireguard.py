"""Real helper/WireGuard/firewall test in two disposable network namespaces.

Requires explicit execution, a prebuilt test image and Linux native binaries.
Touches only labelled Docker containers/network. All keys are artificial and
sent on stdin; no host interfaces, real clusters or customer credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HELPER = "/usr/local/libexec/migration-net-helper"
CONNECTION = "wireguard-test-00000001"


def docker(
    *args: str, data: bytes | None = None, timeout: float = 30, check: bool = True
):
    result = subprocess.run(
        ["docker", *args], input=data, capture_output=True, timeout=timeout, check=False
    )
    if check and result.returncode:
        # Never include command input or raw output (which could contain keys).
        raise RuntimeError("Docker test operation failed: " + args[0])
    return result


def execute(
    container: str, *args: str, data: bytes | None = None, timeout=30, check=True
):
    return docker(
        "exec", "-i", container, *args, data=data, timeout=timeout, check=check
    )


def helper_call(container: str, operation: str, *, check=True, **kwargs):
    payload = {
        "operation": operation,
        "connection_id": CONNECTION,
        "epoch": 1,
        "private_key": None,
        "public_key": None,
        "psk": None,
        **kwargs,
    }
    return execute(container, HELPER, data=json.dumps(payload).encode(), check=check)


def helper(container: str, operation: str, **kwargs):
    return json.loads(helper_call(container, operation, **kwargs).stdout)


def mark_binding_expired(container: str, *, mismatched_peer: bool = False):
    # Modify only the disposable root-owned test state to avoid waiting 30 minutes.
    execute(
        container,
        "python3",
        "-c",
        "import json,pathlib,sys; p=pathlib.Path('/run/migration-connector-network.json'); "
        "d=json.loads(p.read_text()); d['key_expires_at_ms']=0; "
        "d['public_key']='not-the-current-peer' if sys.argv[1]=='True' else d['public_key']; "
        "p.write_text(json.dumps(d))",
        str(mismatched_peer),
    )


PROBE = r"""
import socket,sys,time
local,peer,role=sys.argv[1:]
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind((local,51821));s.settimeout(.3)
mine=('migration-probe/1:artificial-test-nonce:'+role).encode()
expected=('migration-probe/1:artificial-test-nonce:'+('gateway' if role=='client' else 'client')).encode()
seen=acked=False
deadline=time.monotonic()+15
while time.monotonic()<deadline:
 s.sendto(mine,(peer,51821))
 if seen:s.sendto(b'ack:'+expected,(peer,51821))
 try:
  value,source=s.recvfrom(256)
 except TimeoutError:continue
 if source!=(peer,51821):continue
 seen=seen or value==expected
 acked=acked or value==b'ack:'+mine
 if seen and acked:
  for _ in range(3):s.sendto(b'ack:'+expected,(peer,51821))
  sys.exit(0)
sys.exit(2)
"""

LISTENER = r"""
import socket,sys,threading
def serve(port):
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 s.bind((sys.argv[1],port));s.listen()
 while True:
  c,_=s.accept();c.sendall(b'bounded-test');c.close()
threading.Thread(target=serve,args=(9091,),daemon=True).start()
serve(9090)
"""

CONNECT = r"""
import socket,sys
try:
 with socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=3) as s:
  s.settimeout(3);value=s.recv(64)
  sys.exit(0 if value==b'bounded-test' else 2)
except OSError:sys.exit(3)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--binaries", required=True, type=Path)
    args = parser.parse_args()
    binaries = args.binaries.resolve()
    if not (binaries / "migration-net-helper").is_file():
        raise RuntimeError("prebuilt Linux helper is required")
    tag = "mc-wg-test-" + uuid.uuid4().hex[:12]
    network = tag + "-net"
    names = [tag + "-client", tag + "-gateway"]
    tunnel = ["198.18.73.1", "198.18.73.2"]
    report = {"status": "failed"}
    try:
        docker(
            "network", "create", "--label", "migration.connection.test=" + tag, network
        )
        for name in names:
            docker(
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                "migration.connection.test=" + tag,
                "--network",
                network,
                "--cap-add",
                "NET_ADMIN",
                "--mount",
                "type=bind,src=" + str(binaries) + ",dst=/test-binaries,readonly",
                args.image,
            )
            execute(
                name,
                "install",
                "-D",
                "-m",
                "0755",
                "/test-binaries/migration-net-helper",
                HELPER,
            )
            execute(name, "mkdir", "-p", "/etc/migration-connector")
        addresses = [
            docker(
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                name,
            )
            .stdout.decode()
            .strip()
            for name in names
        ]
        defaults = [
            execute(name, "ip", "-j", "route", "show", "default").stdout
            for name in names
        ]
        for index, name in enumerate(names):
            policy = {
                "uid": 0,
                "interface": "mctest0",
                "local_address": tunnel[index],
                "peer_address": tunnel[1 - index],
                "endpoint": addresses[1 - index] + ":51820",
                "listen_port": 51820,
                "job_port": 9090,
                "probe_port": 51821,
                "role": "client" if index == 0 else "gateway",
            }
            execute(
                name,
                "python3",
                "-c",
                "import pathlib,sys; p=pathlib.Path('/etc/migration-connector/net-policy.json'); p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o600)",
                data=json.dumps(policy).encode(),
            )
            if not helper(name, "check")["ready"]:
                raise RuntimeError("helper preflight failed")
        private = [execute(name, "wg", "genkey").stdout.strip() for name in names]
        public = [
            execute(name, "wg", "pubkey", data=key + b"\n").stdout.strip()
            for name, key in zip(names, private)
        ]
        psk = execute(names[0], "wg", "genpsk").stdout.strip()
        for index, name in enumerate(names):
            helper(
                name,
                "configure",
                private_key=private[index].decode(),
                public_key=public[1 - index].decode(),
                psk=psk.decode(),
                key_expires_at_ms=int(time.time() * 1000) + 1800000,
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    execute,
                    name,
                    "python3",
                    "-c",
                    PROBE,
                    tunnel[index],
                    tunnel[1 - index],
                    "client" if index == 0 else "gateway",
                )
                for index, name in enumerate(names)
            ]
            for future in futures:
                future.result()
        if not all(helper(name, "status")["handshake_at"] > 0 for name in names):
            raise RuntimeError("new WireGuard handshakes missing")
        report["bidirectional_probe"] = True
        if any(
            execute(name, "ip", "-j", "route", "show", "default").stdout != before
            for name, before in zip(names, defaults)
        ):
            raise RuntimeError("default route changed")
        report["default_routes_unchanged"] = True
        docker("exec", "--detach", names[1], "python3", "-c", LISTENER, tunnel[1])
        time.sleep(0.5)
        execute(names[0], "python3", "-c", CONNECT, tunnel[1], "9090")
        denied = execute(
            names[0], "python3", "-c", CONNECT, tunnel[1], "9091", check=False
        )
        if denied.returncode == 0:
            raise RuntimeError("unapproved tunnel service reachable")
        report["unapproved_service_blocked"] = True
        if helper(names[0], "prepare-client", connection_id="new-request-00000001")[
            "ready"
        ]:
            raise RuntimeError("a live prior binding was removed")
        if helper_call(names[1], "prepare-client", check=False).returncode == 0:
            raise RuntimeError("gateway allowed client recovery")
        # Kernel timeout must expire even with no helper/agent refresh process.
        print(
            json.dumps({"stage": "waiting_for_kernel_lease_expiry", "seconds": 62}),
            flush=True,
        )
        time.sleep(62)
        expired = execute(
            names[0], "python3", "-c", CONNECT, tunnel[1], "9090", check=False
        )
        if expired.returncode == 0:
            raise RuntimeError("expired lease still permits job traffic")
        report["lease_expiry_blocks_job_traffic"] = True
        client, gateway = names
        replacement = {
            "connection_id": "wireguard-test-00000002",
            "private_key": private[0].decode(),
            "public_key": public[1].decode(),
            "psk": psk.decode(),
            "key_expires_at_ms": int(time.time() * 1000) + 1800000,
        }
        # Expiring the firewall lease alone must not let a new request take over.
        if helper_call(client, "configure", check=False, **replacement).returncode == 0:
            raise RuntimeError("another request replaced an unexpired key")
        if (
            helper_call(
                client,
                "remove",
                check=False,
                connection_id=replacement["connection_id"],
            ).returncode
            == 0
        ):
            raise RuntimeError("another request removed an unexpired key")
        # Startup can reclaim an orphan whose kernel lease has expired.
        if not helper(
            client, "prepare-client", connection_id=replacement["connection_id"]
        )["ready"]:
            raise RuntimeError("expired prior binding still blocks enrollment")
        if (
            execute(client, "ip", "link", "show", "mctest0", check=False).returncode
            == 0
        ):
            raise RuntimeError("the orphaned interface was not removed")
        helper(client, "configure", **{**replacement, "connection_id": CONNECTION})
        report["orphaned_client_lease_recovery"] = True
        if helper_call(client, "remove-exact", check=False, epoch=2).returncode == 0:
            raise RuntimeError("replacement removed a different key epoch")
        helper(client, "remove-exact")
        helper(client, "remove-exact")  # Retry after losing the cleanup response.
        helper(client, "configure", **{**replacement, "connection_id": CONNECTION})
        mark_binding_expired(client)
        helper(client, "configure", **replacement)
        mark_binding_expired(client)
        helper(client, "remove")  # Resume with a different saved binding.
        # A root-owned record that no longer matches the kernel peer is insufficient.
        helper(client, "configure", **replacement)
        mark_binding_expired(client, mismatched_peer=True)
        if helper_call(client, "prepare-client", check=False).returncode == 0:
            raise RuntimeError("startup removed an interface with an unverified peer")
        if helper_call(client, "remove", check=False).returncode == 0:
            raise RuntimeError("mismatched kernel peer was removed")
        execute(client, "ip", "link", "show", "mctest0")
        report["expired_client_binding_recovery"] = True
        for name, connection_id in (
            (client, replacement["connection_id"]),
            (gateway, CONNECTION),
        ):
            helper(name, "remove", connection_id=connection_id)
            if (
                execute(name, "ip", "link", "show", "mctest0", check=False).returncode
                == 0
            ):
                raise RuntimeError("owned interface was not removed")
        report["owned_interfaces_removed"] = True
        report["status"] = "passed"
        print(json.dumps(report), flush=True)
    finally:
        for name in names:
            docker("rm", "--force", name, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    main()
