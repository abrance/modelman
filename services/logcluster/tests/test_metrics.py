"""Prometheus 文本暴露。"""

from __future__ import annotations

from src.metrics import Metrics


def test_render_has_the_documented_series():
    metrics = Metrics(version="0.1.0", commit="abc123")
    text = metrics.render(uptime_secs=1.5)

    assert "# TYPE logcluster_up gauge" in text
    assert "logcluster_up 1\n" in text
    assert "logcluster_uptime_seconds 1.500\n" in text
    assert 'logcluster_build_info{version="0.1.0",commit="abc123"} 1' in text
    assert "logcluster_clusters 0\n" in text
    assert "logcluster_state_loaded 0\n" in text


def test_counters_and_histograms_are_recorded():
    metrics = Metrics(version="0.1.0", commit="abc123")
    metrics.record_success("cluster", seconds=0.012, lines=3)
    metrics.record_failure("match", "overloaded")
    metrics.record_rejected("too_many_lines")
    metrics.record_state_save(True)
    metrics.record_state_save(False)
    metrics.set_state(clusters=7, messages=42, loaded=True)

    text = metrics.render(uptime_secs=0.0)
    assert 'logcluster_requests_total{endpoint="cluster",status="ok"} 1' in text
    assert 'logcluster_requests_total{endpoint="match",status="overloaded"} 1' in text
    assert 'logcluster_lines_total{endpoint="cluster"} 3' in text
    assert 'logcluster_request_duration_seconds_count{endpoint="cluster"} 1' in text
    assert (
        'logcluster_request_duration_seconds_sum{endpoint="cluster"} 0.012000' in text
    )
    assert 'logcluster_rejected_total{reason="too_many_lines"} 1' in text
    assert 'logcluster_state_saves_total{result="ok"} 1' in text
    assert 'logcluster_state_saves_total{result="failed"} 1' in text
    assert "logcluster_clusters 7\n" in text
    assert "logcluster_cluster_messages 42\n" in text
    assert "logcluster_state_loaded 1\n" in text

    # 第一个桶是 0.005s，12ms 落在 0.025 桶里，累计计数应为 1
    assert (
        'logcluster_request_duration_seconds_bucket{endpoint="cluster",le="0.025"} 1'
        in text
    )
    assert "+Inf" in text


def test_in_flight_is_scoped():
    metrics = Metrics(version="0.1.0", commit="abc123")
    assert "logcluster_requests_in_flight 0\n" in metrics.render(uptime_secs=0.0)
    with metrics.in_flight():
        assert "logcluster_requests_in_flight 1\n" in metrics.render(uptime_secs=0.0)
    assert "logcluster_requests_in_flight 0\n" in metrics.render(uptime_secs=0.0)


def test_labels_are_escaped():
    metrics = Metrics(version='1"2', commit="a\nb")
    text = metrics.render(uptime_secs=0.0)
    assert 'version="1\\"2"' in text
    assert 'commit="a\\nb"' in text
