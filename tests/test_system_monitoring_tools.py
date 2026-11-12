from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import psutil

import agent


def test_cpu_and_memory_details_are_formatted_from_mocked_metrics(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(agent.psutil, "cpu_percent", lambda *, interval: 42.5)
    monkeypatch.setattr(
        agent.psutil,
        "cpu_count",
        lambda *, logical: 8 if logical else 4,
    )
    monkeypatch.setattr(
        agent.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=16 * 1024**3,
            available=6 * 1024**3,
            used=10 * 1024**3,
            percent=62.5,
        ),
    )
    monkeypatch.setattr(
        agent.psutil,
        "swap_memory",
        lambda: SimpleNamespace(
            total=2 * 1024**3,
            used=512 * 1024**2,
            free=1536 * 1024**2,
            percent=25.0,
        ),
    )

    cpu = agent.get_cpu_details()
    memory = agent.get_memory_details()

    assert cpu["logical_cores"] == 8
    assert cpu["physical_cores"] == 4
    assert "42.5%" in cpu["summary"]
    assert "mantiqiy yadrolar: 8" in cpu["summary"]
    assert memory["ram"]["total_bytes"] == 16 * 1024**3
    assert memory["swap"]["percent"] == 25.0
    assert "10.0/16.0 GiB" in memory["summary"]
    assert "0.5/2.0 GiB" in memory["summary"]


def test_disk_details_obey_limit_and_report_permission_errors(
    monkeypatch: Any,
) -> None:
    partitions = [
        SimpleNamespace(mountpoint="/", fstype="ext4"),
        SimpleNamespace(mountpoint="/secret", fstype="ext4"),
    ]
    monkeypatch.setattr(agent.psutil, "disk_partitions", lambda *, all: partitions)

    def disk_usage(mountpoint: str) -> Any:
        if mountpoint == "/secret":
            raise psutil.AccessDenied(pid=None)
        return SimpleNamespace(total=1000, used=400, free=600, percent=40.0)

    monkeypatch.setattr(agent.psutil, "disk_usage", disk_usage)
    limited = agent.get_disk_details(limit=1)
    assert limited["scanned"] == 1
    assert limited["truncated"] is True
    assert len(limited["filesystems"]) == 1
    assert limited["filesystems"][0]["mountpoint"] == "/"

    denied = agent.get_disk_details(limit=2)
    assert denied["permission_denied"] == 1
    assert len(denied["filesystems"]) == 1
    assert "1 ta ulangan" in denied["summary"]


def test_processes_are_sorted_and_environment_is_never_read(
    monkeypatch: Any,
) -> None:
    class Process:
        def __init__(self, pid: int, name: str, cpu: float, memory: float) -> None:
            self.info = {"pid": pid, "name": name}
            self._cpu = cpu
            self._memory = memory

        def cpu_percent(self, *, interval: None) -> float:
            return self._cpu

        def memory_percent(self) -> float:
            return self._memory

        def environ(self) -> None:
            raise AssertionError("environment must not be accessed")

    processes = [
        Process(1, "low-cpu", 1.0, 40.0),
        Process(2, "high-memory", 10.0, 70.0),
        Process(3, "high-cpu", 80.0, 5.0),
    ]
    monkeypatch.setattr(
        agent.psutil,
        "process_iter",
        lambda *, attrs: iter(processes),
    )

    by_cpu = agent.list_processes(sort_by="cpu", limit=2)
    by_memory = agent.list_processes(sort_by="memory", limit=2)

    assert [item["name"] for item in by_cpu["processes"]] == [
        "high-cpu",
        "high-memory",
    ]
    assert [item["name"] for item in by_memory["processes"]] == [
        "high-memory",
        "low-cpu",
    ]
    assert by_cpu["truncated"] is True
    assert "CPU sarfi bo'yicha saralandi" in by_cpu["summary"]


def test_empty_process_list_and_access_denied_are_reported(
    monkeypatch: Any,
) -> None:
    class DeniedProcess:
        info = {"pid": 7, "name": "private"}

        def cpu_percent(self, *, interval: None) -> float:
            raise psutil.AccessDenied(pid=7)

        def memory_percent(self) -> float:
            return 0

    monkeypatch.setattr(
        agent.psutil,
        "process_iter",
        lambda *, attrs: iter(()),
    )
    empty = agent.list_processes()
    assert empty["processes"] == []
    assert empty["scanned"] == 0
    assert "0 ta jarayon" in empty["summary"]

    monkeypatch.setattr(
        agent.psutil,
        "process_iter",
        lambda *, attrs: iter((DeniedProcess(),)),
    )
    denied = agent.list_processes()
    assert denied["processes"] == []
    assert denied["permission_denied"] == 1
