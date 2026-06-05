from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree as ET

import requests


PLEX_PRODUCT_NAME = "DVD Mediaplayer"
APP_VERSION = "0.1.0-python"
PLATFORM_NAME = "Linux"


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
            "https://plex.tv/api/resources?includeHttps=1",
            headers=self._headers(token),
            timeout=8,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        for dev in root.findall("Device"):
            if dev.attrib.get("provides", "").find("server") < 0:
                continue
            conn = dev.find("Connection")
            if conn is None:
                continue
            self.state["server_name"] = dev.attrib.get("name", "Plex")
            self.state["server_uri"] = conn.attrib.get("uri")
            self.state["server_token"] = token
            self._save_state()
            return
        raise RuntimeError("no Plex server resource found")

    def _server_xml(self, path: str) -> str:
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
            # Use the /folder endpoint (not /all) so the on-disk directory
            # structure is preserved instead of a flat dump of every file.
            out.append(PlexNode(title=title, key=f"/library/sections/{key}/folder", subtitle=node.attrib.get("type", "library"), kind="section"))
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
