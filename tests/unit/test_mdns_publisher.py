from __future__ import annotations

from interfaces import mdns_publisher


def test_select_lan_address_follows_lowest_metric_default_route(monkeypatch) -> None:
    def fake_run_ip(*args: str):
        if args == ("route", "show", "default"):
            return [
                {"dev": "wlan0", "metric": 600},
                {"dev": "usb0", "metric": 100},
                {"dev": "eno1", "metric": 10},
            ]
        interface = args[-1]
        address = {"usb0": "192.168.55.1", "wlan0": "10.20.30.40"}.get(interface)
        return (
            [{"addr_info": [{"family": "inet", "scope": "global", "local": address}]}]
            if address
            else []
        )

    monkeypatch.setattr(mdns_publisher, "_run_ip", fake_run_ip)

    assert mdns_publisher.select_lan_address() == ("usb0", "192.168.55.1")


def test_select_lan_address_waits_when_only_camera_interface_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        mdns_publisher,
        "_run_ip",
        lambda *args: [{"dev": "eno1", "metric": 10}]
        if args == ("route", "show", "default")
        else [],
    )

    assert mdns_publisher.select_lan_address() is None
