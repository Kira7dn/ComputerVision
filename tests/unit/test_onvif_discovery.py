from __future__ import annotations

import json

from adapters.media import onvif_discovery
from adapters.media.onvif_discovery import (
    CameraBindingStore,
    OnvifDevice,
    OnvifSourceResolver,
    parse_probe_matches,
)


def test_parse_probe_match_uses_stable_endpoint_identity() -> None:
    payload = b"""<?xml version='1.0'?>
    <e:Envelope xmlns:e='http://www.w3.org/2003/05/soap-envelope'
      xmlns:a='http://schemas.xmlsoap.org/ws/2004/08/addressing'
      xmlns:d='http://schemas.xmlsoap.org/ws/2005/04/discovery'>
      <e:Body><d:ProbeMatches><d:ProbeMatch>
        <a:EndpointReference><a:Address>URN:UUID:DAHUA-001</a:Address></a:EndpointReference>
        <d:XAddrs>http://192.168.50.20/onvif/device_service</d:XAddrs>
      </d:ProbeMatch></d:ProbeMatches></e:Body>
    </e:Envelope>"""

    assert parse_probe_matches(payload) == [
        OnvifDevice("urn:uuid:dahua-001", ("http://192.168.50.20/onvif/device_service",))
    ]


def test_resolver_rebinds_current_ip_without_persisting_it(tmp_path, monkeypatch) -> None:
    store = CameraBindingStore(tmp_path / "camera-bindings.json")
    devices = [
        OnvifDevice("urn:uuid:dahua-001", ("http://192.168.50.20/onvif/device_service",))
    ]
    monkeypatch.setattr(onvif_discovery, "probe_onvif", lambda *_args: devices)
    resolver = OnvifSourceResolver(store)
    discovery = {
        "interface": "eno1",
        "rtsp_port": 554,
        "rtsp_path": "/cam/realmonitor?channel=5&subtype=0",
    }

    first = resolver.resolve("DMS", discovery)
    assert first.state == "READY"
    assert first.rtsp_url == "rtsp://192.168.50.20:554/cam/realmonitor?channel=5&subtype=0"
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted["bindings"] == {"DMS": "urn:uuid:dahua-001"}
    assert "192.168.50.20" not in store.path.read_text(encoding="utf-8")

    devices[:] = [
        OnvifDevice("urn:uuid:dahua-001", ("http://10.42.0.9/onvif/device_service",))
    ]
    second = resolver.resolve("DMS", discovery)
    assert second.rtsp_url.startswith("rtsp://10.42.0.9:554/")


def test_first_boot_refuses_ambiguous_camera_binding(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        onvif_discovery,
        "probe_onvif",
        lambda *_args: [
            OnvifDevice("urn:uuid:one", ("http://192.0.2.1/onvif",)),
            OnvifDevice("urn:uuid:two", ("http://192.0.2.2/onvif",)),
        ],
    )

    result = OnvifSourceResolver(
        CameraBindingStore(tmp_path / "camera-bindings.json")
    ).resolve("DMS", {"interface": "eno1", "rtsp_path": "/live"})

    assert result.state == "AMBIGUOUS"
    assert not (tmp_path / "camera-bindings.json").exists()
