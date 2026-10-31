from unittest.mock import Mock

import cpu_diagnostics as cpu


def test_cgroup_quota_and_cpu_throttle_counters(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 900000\nnr_periods 20\nnr_throttled 6\nthrottled_usec 1200000\n")
    monkeypatch.setattr(cpu, "_CGROUP", tmp_path)
    status = cpu.report()
    assert status["quota_cores"] == 0.5
    assert status["throttled_seconds"] == 1.2
    assert status["throttled_periods"] == 6 and status["periods"] == 20


def test_cpu_call_window_reports_counter_deltas_and_keeps_unknown_values(monkeypatch):
    first = {"wall_seconds": 10, "process_cpu_seconds": 3, "quota_cores": 0.5,
             "throttled_seconds": 1, "throttled_periods": 2, "periods": None}
    second = {**first, "wall_seconds": 14, "process_cpu_seconds": 4,
              "throttled_seconds": 1.8, "throttled_periods": 5}
    monkeypatch.setattr(cpu, "snapshot", Mock(return_value=second))
    assert cpu.report(first) == {"scope": "process_and_container", "quota_cores": 0.5,
                                 "process_cpu_seconds": 1, "throttled_seconds": 0.8,
                                 "throttled_periods": 3, "periods": None, "window_seconds": 4}


def test_unlimited_or_missing_cgroup_data_is_not_reported_as_zero_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(cpu, "_CGROUP", tmp_path)
    assert cpu.report()["quota_cores"] is None
    (tmp_path / "cpu.max").write_text("max 100000\n")
    (tmp_path / "cpu.stat").write_text("nr_throttled invalid\n")
    status = cpu.report()
    assert status["quota_cores"] is None and status["throttled_periods"] is None
