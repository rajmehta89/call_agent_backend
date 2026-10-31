"""Read-only process/container CPU counters; deltas cover all concurrent requests, not just one call."""

from pathlib import Path
import time

_CGROUP = Path("/sys/fs/cgroup")


def snapshot():
    result = {"wall_seconds": time.monotonic(), "process_cpu_seconds": time.process_time(),
              "quota_cores": None, "throttled_seconds": None, "throttled_periods": None, "periods": None}
    try:
        quota, period = (_CGROUP / "cpu.max").read_text().split()
        if quota != "max" and int(quota) > 0 and int(period) > 0:
            result["quota_cores"] = round(int(quota) / int(period), 3)
    except (OSError, ValueError):
        pass  # Windows, cgroup v1, or inaccessible counters: remain unknown.
    try:
        counters = dict(line.split() for line in (_CGROUP / "cpu.stat").read_text().splitlines())
        for source, destination, scale in (("throttled_usec", "throttled_seconds", 1e-6),
                                           ("nr_throttled", "throttled_periods", 1),
                                           ("nr_periods", "periods", 1)):
            if source in counters:
                value = int(counters[source])
                if value >= 0:
                    result[destination] = value * scale
    except (OSError, ValueError):
        pass
    return result


def report(start=None):
    current = snapshot()
    result = {"scope": "process_and_container", "quota_cores": current["quota_cores"]}
    for key in ("process_cpu_seconds", "throttled_seconds", "throttled_periods", "periods"):
        value = current[key]
        if start is not None:
            before = start.get(key)
            value = max(0, value - before) if value is not None and before is not None else None
        result[key] = round(value, 3) if value is not None else None
    if start is not None:
        result["window_seconds"] = round(max(0, current["wall_seconds"] - start["wall_seconds"]), 3)
    return result
