"""ONVIF WS-Discovery camera binding without persisting network addresses."""

from __future__ import annotations

import json
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

WS_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)
SOAP_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><w:MessageID>uuid:{message_id}</w:MessageID>
 <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
 <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


@dataclass(frozen=True, slots=True)
class OnvifDevice:
    endpoint_uuid: str
    xaddrs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    state: str
    endpoint_uuid: str = ""
    rtsp_url: str = ""
    error: str = ""


def _interface_ipv4(interface: str) -> str:
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    for item in payload if isinstance(payload, list) else []:
        for address in item.get("addr_info", []) or []:
            if address.get("family") == "inet" and address.get("scope") == "global":
                return str(address.get("local") or "")
    return ""


def parse_probe_matches(payload: bytes) -> list[OnvifDevice]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    devices: list[OnvifDevice] = []
    for match in root.iter():
        if not match.tag.endswith("ProbeMatch"):
            continue
        endpoint = ""
        xaddrs: tuple[str, ...] = ()
        for child in match.iter():
            if child.tag.endswith("Address") and child.text and not endpoint:
                endpoint = child.text.strip().lower()
            elif child.tag.endswith("XAddrs") and child.text:
                xaddrs = tuple(value for value in child.text.split() if value)
        if endpoint and xaddrs:
            devices.append(OnvifDevice(endpoint_uuid=endpoint, xaddrs=xaddrs))
    unique: dict[str, OnvifDevice] = {device.endpoint_uuid: device for device in devices}
    return list(unique.values())


def probe_onvif(interface: str, timeout_seconds: float = 3.0) -> list[OnvifDevice]:
    interface_ip = _interface_ipv4(interface)
    if not interface_ip:
        return []
    message = SOAP_PROBE.format(message_id=uuid.uuid4()).encode("utf-8")
    found: dict[str, OnvifDevice] = {}
    deadline = time.monotonic() + max(0.2, timeout_seconds)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as client:
        client.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        client.settimeout(0.2)
        client.sendto(message, WS_DISCOVERY_ADDRESS)
        while time.monotonic() < deadline:
            try:
                body, _address = client.recvfrom(64 * 1024)
            except TimeoutError:
                continue
            for device in parse_probe_matches(body):
                found[device.endpoint_uuid] = device
    return list(found.values())


class CameraBindingStore:
    """Persist stable ONVIF identities only; DHCP addresses remain ephemeral."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        bindings = payload.get("bindings", {}) if isinstance(payload, dict) else {}
        return {
            str(key): str(value).lower()
            for key, value in bindings.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def bind(self, camera_id: str, endpoint_uuid: str) -> None:
        bindings = self.load()
        bindings[camera_id] = endpoint_uuid.lower()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "bindings": bindings}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class OnvifSourceResolver:
    def __init__(self, store: CameraBindingStore) -> None:
        self.store = store

    def resolve(self, camera_id: str, discovery: dict[str, object]) -> DiscoveryResult:
        interface = str(discovery.get("interface") or "eno1")
        timeout = float(discovery.get("timeout_seconds") or 3.0)
        devices = probe_onvif(interface, timeout)
        binding = self.store.load().get(camera_id, "")
        selected = next((item for item in devices if item.endpoint_uuid == binding), None)
        if binding and selected is None:
            return DiscoveryResult("UNAVAILABLE", endpoint_uuid=binding, error="bound camera not found")
        if not binding:
            if not devices:
                return DiscoveryResult("UNAVAILABLE", error="no ONVIF camera found")
            if len(devices) != 1:
                return DiscoveryResult("AMBIGUOUS", error="multiple ONVIF cameras found")
            selected = devices[0]
            self.store.bind(camera_id, selected.endpoint_uuid)
        assert selected is not None
        host = ""
        for xaddr in selected.xaddrs:
            parsed = urlsplit(xaddr)
            if parsed.hostname:
                host = parsed.hostname
                break
        if not host:
            return DiscoveryResult("UNAVAILABLE", endpoint_uuid=selected.endpoint_uuid, error="ONVIF XAddr has no host")
        port = int(discovery.get("rtsp_port") or 554)
        path = str(discovery.get("rtsp_path") or "").strip()
        if not path.startswith("/"):
            path = "/" + path
        parsed_path = urlsplit(path)
        rtsp_url = urlunsplit(("rtsp", f"{host}:{port}", parsed_path.path, parsed_path.query, ""))
        return DiscoveryResult("READY", endpoint_uuid=selected.endpoint_uuid, rtsp_url=rtsp_url)
