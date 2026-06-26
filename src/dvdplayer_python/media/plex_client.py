from __future__ import annotations

import json
import re
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree as ET

import requests

from dvdplayer_python.core.debuglog import log_event


PLEX_PRODUCT_NAME = "DVD Mediaplayer"
APP_VERSION = "0.1.0-python"
PLATFORM_NAME = "Linux"


def _local_subnet_prefix() -> Optional[str]:
    """This host's own LAN /24 prefix (e.g. ``"192.168.1."``), or None.

    Used to prefer the Plex connection on the same subnet as us — the real
    LAN address — over the server's other (Docker-internal, WAN, relay)
    addresses, which Plex also advertises as ``local``.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip.rsplit(".", 1)[0] + "." if ip.count(".") == 3 else None
    except Exception:
        return None


def _plexdirect_ip(uri: str) -> Optional[str]:
    """Extract the embedded IP from a plex.direct URI.

    ``https://192-168-1-3.<hash>.plex.direct:32400`` -> ``"192.168.1.3"``.
    """
    m = re.search(r"//(\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3})\.", uri)
    return m.group(1).replace("-", ".") if m else None


@dataclass
class DeviceLinkCode:
    id: int
    code: str
    expires_in: int


@dataclass
class PlexNode:
    title: str
    key: str
    subtitle: str
    kind: str
    container: Optional[str] = None
    media_url: Optional[str] = None


class PlexClient:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "plex_state.json"
        self.cache_path = self.state_dir / "plex_cache.json"
        self.state = {
            "client_id": str(uuid.uuid4()),
            "auth_token": None,
            "server_name": None,
            "server_uri": None,
            "server_token": None,
            # When True, sections are browsed via the /folder endpoint (on-disk
            # directory tree) instead of /all (flat dump). Toggled with SELECT.
            "folder_view": False,
        }
        self.cache = {"sections": []}
        self._load()

    def _load(self):
        if self.state_path.is_file():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        if self.cache_path.is_file():
            self.cache.update(json.loads(self.cache_path.read_text(encoding="utf-8")))
        self._save_state()

    def _save_state(self):
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _save_cache(self):
        self.cache_path.write_text(json.dumps(self.cache, indent=2), encoding="utf-8")

    def _headers(self, token: Optional[str] = None):
        headers = {
            "X-Plex-Product": PLEX_PRODUCT_NAME,
            "X-Plex-Version": APP_VERSION,
            "X-Plex-Platform": PLATFORM_NAME,
            "X-Plex-Client-Identifier": self.state["client_id"],
            "X-Plex-Device-Name": PLEX_PRODUCT_NAME,
        }
        use_token = token or self.state.get("auth_token")
        if use_token:
            headers["X-Plex-Token"] = use_token
        return headers

    def has_token(self) -> bool:
        return bool(self.state.get("auth_token"))

    def client_id(self) -> str:
        return self.state["client_id"]

    def server_name(self) -> str:
        return self.state.get("server_name") or "Plex"

    def server_token(self) -> str:
        return self.state.get("server_token") or self.state.get("auth_token") or ""

    def folder_view(self) -> bool:
        return bool(self.state.get("folder_view", False))

    def set_folder_view(self, value: bool) -> None:
        self.state["folder_view"] = bool(value)
        self._save_state()

    def section_browse_key(self, section_key: str) -> str:
        """Build the browse path for a section, honouring the folder-view flag.

        `section_key` is the bare section path (`/library/sections/{id}`); any
        stale `/all` or `/folder` suffix from cache is stripped first so the
        flag stays the single source of truth.
        """
        base = section_key
        for suffix in ("/folder", "/all"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return base + ("/folder" if self.folder_view() else "/all")

    def reset_link(self) -> None:
        self.state["auth_token"] = None
        self.state["server_name"] = None
        self.state["server_uri"] = None
        self.state["server_token"] = None
        self.cache = {"sections": []}
        self._save_state()
        self._save_cache()

    def begin_device_link(self) -> DeviceLinkCode:
        response = requests.post(
            "https://plex.tv/api/v2/pins",
            headers={**self._headers(), "Accept": "application/json"},
            timeout=6,
        )
        response.raise_for_status()
        data = response.json()
        return DeviceLinkCode(
            id=int(data.get("id", 0)),
            code=str(data.get("code", "")),
            expires_in=int(data.get("expiresIn", data.get("expires_in", 600))),
        )

    def poll_device_link(self, pin_id: int) -> bool:
        response = requests.get(
            f"https://plex.tv/api/v2/pins/{pin_id}",
            headers={**self._headers(), "Accept": "application/json"},
            timeout=6,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("authToken") or data.get("auth_token")
        if token:
            self.state["auth_token"] = token
            self.discover_server()
            self._save_state()
            return True
        return False

    def discover_server(self) -> None:
        token = self.state.get("auth_token")
        if not token:
            return
        response = requests.get(
            "https://plex.tv/api/resources?includeHttps=1&includeRelay=1",
            headers=self._headers(token),
            timeout=8,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        # Collect every server device, owned ones first (your own box beats a
        # friend's shared server).
        servers = []  # (owned, name, device_token, [Connection, ...])
        for dev in root.findall("Device"):
            if dev.attrib.get("provides", "").find("server") < 0:
                continue
            conns = dev.findall("Connection")
            if not conns:
                continue
            owned = dev.attrib.get("owned") == "1"
            dev_token = dev.attrib.get("accessToken") or token  # shared servers carry their own
            servers.append((owned, dev.attrib.get("name", "Plex"), dev_token, conns))
        if not servers:
            raise RuntimeError("no Plex server resource found")
        servers.sort(key=lambda s: 0 if s[0] else 1)
        for _owned, name, dev_token, conns in servers:
            uri = self._pick_reachable_connection(conns, dev_token)
            if uri:
                self.state["server_name"] = name
                self.state["server_uri"] = uri
                self.state["server_token"] = dev_token
                self._save_state()
                return
        raise RuntimeError("no reachable Plex connection found")

    def _pick_reachable_connection(self, conns, token: str) -> Optional[str]:
        """Choose the connection that actually answers from THIS host.

        Plex advertises every server-side interface — including Docker bridge
        networks (172.x) that are unreachable from the LAN — and marks them all
        ``local=1``, so ordering by the ``local`` flag is useless. Rank by
        likelihood (same /24 as us > other local > WAN > relay) to probe the
        best candidate first, then return the first URI that responds. The probe
        is what guarantees correctness; the ranking only makes it fast.
        """
        my_prefix = _local_subnet_prefix()

        def rank(c) -> int:
            uri = c.attrib.get("uri", "")
            relay = c.attrib.get("relay") == "1"
            local = c.attrib.get("local") == "1"
            ip = _plexdirect_ip(uri)
            if my_prefix and ip and ip.startswith(my_prefix):
                return 0  # our own subnet → the real LAN address
            if local and not relay:
                return 1
            if not relay:
                return 2  # WAN (works, but hairpins out to the internet)
            return 3  # relay: slow, bandwidth-capped, last resort

        ordered = sorted(conns, key=rank)
        for c in ordered:
            uri = c.attrib.get("uri")
            if uri and self._connection_alive(uri, token):
                return uri
        # Nothing answered (e.g. server momentarily down) → keep the best-ranked
        # URI so a later retry can still reach it.
        return ordered[0].attrib.get("uri") if ordered else None

    def _connection_alive(self, uri: str, token: str) -> bool:
        """True if ``{uri}/identity`` answers quickly (reachable from here)."""
        try:
            r = requests.get(uri + "/identity", headers=self._headers(token), timeout=2.5)
            return r.status_code == 200
        except Exception:
            return False

    def _server_xml(self, path: str) -> str:
        """Fetch a server XML path, re-resolving the server URI on a dead link.

        The cached ``server_uri`` is picked once at link time and never
        refreshed, so it goes stale the moment we change networks (the LAN
        address chosen at home is unreachable from anywhere else). On a
        connection/timeout error — and only then, never on an HTTP error where
        the server actually answered — re-run discovery to pick a connection
        reachable from the current network, then retry once.
        """
        try:
            return self._server_xml_once(path)
        except (requests.ConnectionError, requests.Timeout):
            stale_uri = self.state.get("server_uri")
            log_event("plex_server_uri_stale", uri=stale_uri)
            self.discover_server()
            log_event("plex_server_uri_reresolved", uri=self.state.get("server_uri"))
            return self._server_xml_once(path)

    def _server_xml_once(self, path: str) -> str:
        uri = self.state.get("server_uri")
        token = self.state.get("server_token") or self.state.get("auth_token")
        if not uri or not token:
            raise RuntimeError("missing Plex server URI/token")
        full = uri + (path if path.startswith("/") else "/" + path)
        response = requests.get(full, headers=self._headers(token), timeout=10)
        response.raise_for_status()
        return response.text

    def library_sections(self) -> List[PlexNode]:
        xml = self._server_xml("/library/sections")
        root = ET.fromstring(xml)
        out: List[PlexNode] = []
        for node in root.findall("Directory"):
            key = node.attrib.get("key", "")
            title = node.attrib.get("title", "")
            if not key or not title:
                continue
            # Store the bare section path; the /all vs /folder suffix is chosen
            # at browse time from the folder-view flag (see section_browse_key).
            out.append(PlexNode(title=title, key=f"/library/sections/{key}", subtitle=node.attrib.get("type", "library"), kind="section"))
        self.cache["sections"] = [asdict(item) for item in out]
        self._save_cache()
        return out

    def cached_sections(self) -> List[PlexNode]:
        return [PlexNode(**item) for item in self.cache.get("sections", []) if isinstance(item, dict)]

    def browse_path(self, path_key: str) -> List[PlexNode]:
        xml = self._server_xml(path_key)
        root = ET.fromstring(xml)
        out: List[PlexNode] = []
        for node in list(root):
            if node.tag == "Directory":
                key = node.attrib.get("key", "")
                title = node.attrib.get("title", "")
                if key and title:
                    out.append(PlexNode(title=title, key=key, subtitle=node.attrib.get("summary", ""), kind="directory"))
            elif node.tag == "Video":
                title = node.attrib.get("title", "")
                key = node.attrib.get("key", "")
                part = node.find(".//Part")
                media = node.find(".//Media")
                part_key = part.attrib.get("key") if part is not None else key
                out.append(
                    PlexNode(
                        title=title,
                        key=key,
                        subtitle=node.attrib.get("originallyAvailableAt", ""),
                        kind="video",
                        container=media.attrib.get("container") if media is not None else None,
                        media_url=self._build_media_url(part_key) if part_key else None,
                    )
                )
        return out

    def resolve_playback_url(self, node: PlexNode) -> str:
        if node.media_url:
            return node.media_url
        xml = self._server_xml(node.key)
        root = ET.fromstring(xml)
        part = root.find(".//Part")
        if part is None:
            raise RuntimeError("plex item does not expose direct-play part")
        return self._build_media_url(part.attrib.get("key", ""))

    def _build_media_url(self, part_key: str) -> str:
        uri = self.state.get("server_uri")
        token = self.state.get("server_token") or self.state.get("auth_token")
        if not uri or not token:
            raise RuntimeError("missing Plex server URI/token")
        path = part_key if part_key.startswith("/") else "/" + part_key
        sep = "&" if "?" in path else "?"
        return f"{uri}{path}{sep}download=1&X-Plex-Token={token}"
