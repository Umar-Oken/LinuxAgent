from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

import psutil
import pytest

import agent
from core.tool_dispatcher import ToolExecutionError


def test_network_interfaces_report_state_and_counters_without_addresses(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        agent.psutil,
        "net_if_stats",
        lambda: {
            "eth0": SimpleNamespace(isup=True, speed=1000, mtu=1500),
            "lo": SimpleNamespace(isup=True, speed=0, mtu=65536),
        },
    )
    monkeypatch.setattr(
        agent.psutil,
        "net_io_counters",
        lambda *, pernic: {
            "eth0": SimpleNamespace(
                bytes_sent=120,
                bytes_recv=340,
                packets_sent=3,
                packets_recv=5,
            )
        },
    )

    result = agent.get_network_interfaces()

    assert result["total"] == 2
    assert result["up"] == 2
    assert result["interfaces"][0]["bytes_received"] == 340
    assert all("address" not in interface for interface in result["interfaces"])
    assert "2 ta tarmoq interfeysi" in result["summary"]


def test_connectivity_check_sends_one_timed_request_to_configured_endpoint(
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, float]] = []

    class Response:
        status = 204

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(request: Any, *, timeout: float) -> Response:
        calls.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(agent, "urlopen", fake_urlopen)
    endpoint = "https://status.example.test/ping"
    result = agent.check_internet_connectivity(endpoint)

    assert result["connected"] is True
    assert result["status_code"] == 204
    assert calls == [(endpoint, 3)]


def test_connectivity_failure_and_unavailable_network_are_reported(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        agent,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    result = agent.check_internet_connectivity("https://status.example.test/ping")
    assert result["connected"] is False
    assert result["status_code"] is None
    assert "tekshirib bo'lmadi" in result["summary"]

    monkeypatch.setattr(
        agent.psutil,
        "net_if_stats",
        lambda: (_ for _ in ()).throw(psutil.Error("unavailable")),
    )
    with pytest.raises(ToolExecutionError) as error:
        agent.get_network_interfaces()
    assert error.value.code == "NETWORK_METRICS_UNAVAILABLE"


def test_inspect_process_reports_resources_and_formats_summary(
    monkeypatch: Any,
) -> None:
    class Process:
        def name(self) -> str:
            return "worker"

        def status(self) -> str:
            return psutil.STATUS_RUNNING

        def cpu_percent(self, *, interval: None) -> float:
            return 12.5

        def memory_info(self) -> Any:
            return SimpleNamespace(rss=64 * 1024**2, vms=128 * 1024**2)

        def memory_percent(self) -> float:
            return 1.25

        def num_threads(self) -> int:
            return 4

        def environ(self) -> None:
            raise AssertionError("environment must not be accessed")

    monkeypatch.setattr(agent.psutil, "Process", lambda pid: Process())
    result = agent.inspect_process(4321)

    assert result["pid"] == 4321
    assert result["memory_rss_bytes"] == 64 * 1024**2
    assert result["threads"] == 4
    assert "worker (PID 4321)" in result["summary"]
    assert "CPU 12.5%" in result["summary"]


def test_inspect_process_rejects_bad_pid_and_handles_disappearing_process(
    monkeypatch: Any,
) -> None:
    with pytest.raises(ToolExecutionError) as invalid:
        agent.inspect_process(0)
    assert invalid.value.code == "INVALID_PID"

    class VanishingProcess:
        def name(self) -> str:
            raise psutil.NoSuchProcess(9876)

    monkeypatch.setattr(agent.psutil, "Process", lambda pid: VanishingProcess())
    with pytest.raises(ToolExecutionError) as vanished:
        agent.inspect_process(9876)
    assert vanished.value.code == "PROCESS_NOT_FOUND"
    assert "9876" not in vanished.value.safe_message


def test_uptime_is_reported_as_duration_and_utc_boot_time(
    monkeypatch: Any,
) -> None:
    boot_time = datetime.now(timezone.utc).timestamp() - (2 * 86_400 + 3_661)
    monkeypatch.setattr(agent.psutil, "boot_time", lambda: boot_time)

    result = agent.get_system_uptime()

    assert result["duration"]["days"] == 2
    assert result["duration"]["hours"] == 1
    assert result["duration"]["minutes"] == 1
    assert result["boot_time_utc"].endswith("+00:00")
    assert "2 kun, 1 soat, 1 daqiqa" in result["summary"]
