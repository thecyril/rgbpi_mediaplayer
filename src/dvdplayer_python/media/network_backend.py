from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from dvdplayer_python.core.debuglog import log_event


CONFIG_FILE_NAME = "network_sources.json"
SMB_LS_RE = re.compile(r"^\s*(?P<name>.+?)\s+(?P<attrs>[A-Z]+)\s+(?P<size>\d+)\s+\w{3}\s+\w{3}\s+.+$")


@dataclass
class SavedNetworkRoot:
    id: str
    protocol: str
    display_name: str
    host: str
    address: str
    root_name: str
    path: str
    username: Optional[str]
    password: Optional[str]


@dataclass
class DiscoveryHost:
    protocol: str
    host: str
    display_name: str
    address: str


@dataclass
class BrowseEntry:
    protocol: str
    title: str
    subtitle: str
    root_name: str
    path: str
    is_dir: bool
    size: Optional[int]


class NetworkBackend:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.config_path = app_dir / CONFIG_FILE_NAME
        self.mount_root = app_dir / "state" / "network_mounts"
        self.mount_root.mkdir(parents=True, exist_ok=True)
        self.config = self._load()

    def _load(self) -> dict:
        if self.config_path.is_file():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        return {"roots": [], "credentials": []}

    def _save(self) -> None:
        self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def list_saved_roots(self) -> List[SavedNetworkRoot]:
        return [SavedNetworkRoot(**item) for item in self.config.get("roots", [])]

    def add_root(self, root: SavedNetworkRoot) -> None:
        roots = self.config.setdefault("roots", [])
        if any(r.get("id") == root.id for r in roots):
            return
        roots.append(asdict(root))
        roots.sort(key=lambda r: r.get("display_name", "").lower())
        self._save()

    def remove_root(self, root_id: str) -> bool:
        roots = self.config.setdefault("roots", [])
        remaining = [r for r in roots if r.get("id") != root_id]
        if len(remaining) == len(roots):
            return False
        self.config["roots"] = remaining
        self._save()
        return True

    def has_root(
        self,
        protocol: str,
        address: str,
        root_name: str,
        path: str,
        username: Optional[str],
    ) -> bool:
        for root in self.list_saved_roots():
            if (
                root.protocol == protocol
                and root.address == address
                and root.root_name == root_name
                and root.path == _normalize(path)
                and (root.username or "") == (username or "")
            ):
                return True
        return False

    def save_credentials(self, protocol: str, host: str, address: str, username: str, password: str) -> None:
        if not username.strip():
            return
        creds = self.config.setdefault("credentials", [])
        updated = False
        for item in creds:
            if item.get("protocol") == protocol and item.get("address") == address:
                item.update({"host": host, "username": username, "password": password})
                updated = True
                break
        if not updated:
            creds.append(
                {
                    "protocol": protocol,
                    "address": address,
                    "host": host,
                    "username": username,
                    "password": password,
                }
            )
        self._save()

    def saved_credentials(self, protocol: str, address: str) -> Optional[Tuple[str, str]]:
        for item in self.config.get("credentials", []):
            if item.get("protocol") == protocol and item.get("address") == address:
                return item.get("username", ""), item.get("password", "")
        return None

    def discover_hosts(self, protocol: str) -> List[DiscoveryHost]:
        prefix = _local_subnet_prefix()
        hosts: List[DiscoveryHost] = []
        ports = [445, 139] if protocol == "SMB" else [2049, 111]
        for host in prefix.hosts():
            ip = str(host)
            if _probe_any(ip, ports, timeout=0.08):
                hosts.append(DiscoveryHost(protocol=protocol, host=ip, display_name=ip, address=ip))
        return hosts

    def browse_smb_root(
        self,
        host: str,
        username: Optional[str],
        password: Optional[str],
    ) -> List[BrowseEntry]:
        user = username or "guest"
        pwd = password or ""
        cmd = ["smbclient", "-g", "-L", f"//{host}", "-U", f"{user}%{pwd}"]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        except FileNotFoundError:
            # smbclient not installed — the #1 cause of a silent "no shares".
            log_event("smb_list_failed", host=host, error="smbclient not installed (apt install smbclient)")
            return []
        except subprocess.CalledProcessError as exc:
            # auth failure / wrong protocol / unreachable — surface the real
            # smbclient message in the log instead of swallowing it.
            tail = (exc.output or "").strip().splitlines()[-1:] if exc.output else []
            log_event("smb_list_failed", host=host, rc=exc.returncode, error="; ".join(tail) or "smbclient error")
            return []
        except Exception as exc:
            log_event("smb_list_failed", host=host, error=str(exc))
            return []
        entries: List[BrowseEntry] = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 2 or parts[0] != "Disk":
                continue
            share = parts[1].strip()
            if not share or share.endswith("$"):
                continue
            entries.append(
                BrowseEntry(
                    protocol="SMB",
                    title=share,
                    subtitle="SMB share",
                    root_name=share,
                    path="/",
                    is_dir=True,
                    size=None,
                )
            )
        return entries

    def browse_smb_share(
        self,
        host: str,
        share: str,
        path: str,
        username: Optional[str],
        password: Optional[str],
    ) -> List[BrowseEntry]:
        user = username or "guest"
        pwd = password or ""
        rel = _normalize(path).lstrip("/")
        # `ls <dir>` treats <dir> as a filename *mask* in the current working
        # directory — it matches only the folder entry itself, so you end up
        # "inside" a folder that lists a single child of the same name and then
        # "Folder is empty" one level deeper. To list a subfolder's *contents*
        # we must cd into it first; smbclient's path separator is backslash.
        if rel:
            smb_dir = rel.replace("/", "\\")
            command = f'cd "{smb_dir}"; ls'
        else:
            command = "ls"
        cmd = ["smbclient", f"//{host}/{share}", "-U", f"{user}%{pwd}", "-c", command]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        except FileNotFoundError:
            log_event("smb_browse_failed", host=host, share=share, error="smbclient not installed")
            return []
        except subprocess.CalledProcessError as exc:
            tail = (exc.output or "").strip().splitlines()[-1:] if exc.output else []
            log_event("smb_browse_failed", host=host, share=share, path=rel, rc=exc.returncode, error="; ".join(tail) or "smbclient error")
            return []
        except Exception as exc:
            log_event("smb_browse_failed", host=host, share=share, path=rel, error=str(exc))
            return []
        items: List[BrowseEntry] = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line or "blocks of size" in line:
                continue
            parsed = _parse_smb_ls_line(line)
            if not parsed:
                continue
            name, is_dir, size = parsed
            if not name or name in (".", ".."):
                continue
            if name.startswith("."):
                # Skip dotfiles (.DS_Store, ._AppleDouble, …) — same as the
                # NFS browser, so media listings aren't cluttered with junk.
                continue
            items.append(
                BrowseEntry(
                    protocol="SMB",
                    title=name,
                    subtitle="Folder" if is_dir else "File",
                    root_name=share,
                    path=_join(path, name),
                    is_dir=is_dir,
                    size=size,
                )
            )
        items.sort(key=lambda item: (not item.is_dir, item.title.lower()))
        return items

    def browse_nfs_root(self, host: str) -> List[BrowseEntry]:
        try:
            out = subprocess.check_output(["showmount", "-e", host], stderr=subprocess.DEVNULL, text=True)
        except Exception:
            return []
        entries = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("Export"):
                continue
            export = line.split()[0]
            entries.append(
                BrowseEntry(
                    protocol="NFS",
                    title=export,
                    subtitle="NFS export",
                    root_name=export,
                    path="/",
                    is_dir=True,
                    size=None,
                )
            )
        return entries

    def browse_nfs_export(self, host: str, export: str, path: str) -> List[BrowseEntry]:
        mount_root = Path(tempfile.mkdtemp(prefix="rgbpi-dvdplayer-nfs-"))
        try:
            cmd = [
                "mount",
                "-t",
                "nfs",
                "-o",
                "ro,soft,timeo=20,retrans=1",
                f"{host}:{export}",
                str(mount_root),
            ]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rel = _normalize(path).lstrip("/")
            target = mount_root / rel if rel else mount_root
            if not target.exists() or not target.is_dir():
                return []
            items: List[BrowseEntry] = []
            for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if entry.name.startswith("."):
                    continue
                is_dir = entry.is_dir()
                items.append(
                    BrowseEntry(
                        protocol="NFS",
                        title=entry.name,
                        subtitle="Folder" if is_dir else "File",
                        root_name=export,
                        path=_join(path, entry.name),
                        is_dir=is_dir,
                        size=None if is_dir else _safe_stat_size(entry),
                    )
                )
            return items
        except Exception:
            return []
        finally:
            try:
                subprocess.call(["umount", str(mount_root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            try:
                mount_root.rmdir()
            except Exception:
                pass

    def resolve_media_path(
        self,
        protocol: str,
        host: str,
        root_name: str,
        path: str,
        username: Optional[str],
        password: Optional[str],
    ) -> Optional[str]:
        mounted = self._ensure_media_mount(protocol, host, root_name, username, password)
        if not mounted:
            log_event("network_media_mount_failed", protocol=protocol, host=host, root_name=root_name)
            return None
        rel = _normalize(path).lstrip("/")
        target = mounted / rel if rel else mounted
        exists = target.exists()
        log_event(
            "network_media_path",
            protocol=protocol,
            host=host,
            root_name=root_name,
            requested_path=path,
            mount_dir=str(mounted),
            resolved_path=str(target),
            exists=exists,
        )
        return str(target) if target.exists() else None

    def _ensure_media_mount(
        self,
        protocol: str,
        host: str,
        root_name: str,
        username: Optional[str],
        password: Optional[str],
    ) -> Optional[Path]:
        mount_dir = self.mount_root / _mount_id(protocol, host, root_name, username)
        mount_dir.mkdir(parents=True, exist_ok=True)
        if _is_mounted(mount_dir):
            return mount_dir
        if protocol == "SMB":
            target = f"//{host}/{root_name}"
            options = ["ro"]
            if username:
                options.append(f"username={username}")
                options.append(f"password={password or ''}")
            else:
                options.append("guest")
            cmd = ["mount", "-t", "cifs", "-o", ",".join(options), target, str(mount_dir)]
        elif protocol == "NFS":
            target = f"{host}:{root_name}"
            cmd = ["mount", "-t", "nfs", "-o", "ro,soft,timeo=20,retrans=1", target, str(mount_dir)]
        else:
            return None
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_event("network_mount_ok", protocol=protocol, host=host, root_name=root_name, mount_dir=str(mount_dir))
            return mount_dir
        except Exception:
            log_event("network_mount_failed", protocol=protocol, host=host, root_name=root_name, mount_dir=str(mount_dir))
            return None


def make_saved_root(
    protocol: str,
    display_name: str,
    host: str,
    address: str,
    root_name: str,
    path: str,
    username: Optional[str],
    password: Optional[str],
) -> SavedNetworkRoot:
    path = _normalize(path)
    ident = f"{protocol}:{address}:{root_name}:{path}:{username or ''}"
    return SavedNetworkRoot(
        id=ident,
        protocol=protocol,
        display_name=display_name,
        host=host,
        address=address,
        root_name=root_name,
        path=path,
        username=username,
        password=password,
    )


def _normalize(path: str) -> str:
    clean = "/" + path.strip().strip("/")
    return "/" if clean == "/" else clean


def _join(parent: str, child: str) -> str:
    parent = _normalize(parent)
    if parent == "/":
        return "/" + child.strip("/")
    return parent.rstrip("/") + "/" + child.strip("/")


def _probe_any(ip: str, ports: list[int], timeout: float) -> bool:
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _local_subnet_prefix() -> ipaddress.IPv4Network:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        local = s.getsockname()[0]
    finally:
        s.close()
    iface = ipaddress.IPv4Interface(f"{local}/24")
    return iface.network


def _parse_smb_ls_line(line: str) -> Optional[tuple[str, bool, Optional[int]]]:
    match = SMB_LS_RE.match(line)
    if not match:
        return None
    name = match.group("name").rstrip()
    attrs = match.group("attrs")
    size_raw = match.group("size")
    try:
        size = int(size_raw)
    except Exception:
        size = None
    return name, ("D" in attrs), size


def _safe_stat_size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except Exception:
        return None


def _mount_id(protocol: str, host: str, root_name: str, username: Optional[str]) -> str:
    key = f"{protocol}:{host}:{root_name}:{username or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _is_mounted(path: Path) -> bool:
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
    except Exception:
        return False
    mount_path = str(path)
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == mount_path:
            return True
    return False
