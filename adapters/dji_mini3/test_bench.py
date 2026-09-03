import json

from adapters.dji_mini3.bench import BenchHarness, main


def test_bench_reports_observed_command_rate_without_rechecking_admission() -> None:
    harness = BenchHarness()

    harness.record_command_sent(sent_at_ms=1_000, round_trip_ms=120)
    harness.record_command_sent(sent_at_ms=1_050, round_trip_ms=100)
    harness.record_command_rejection("expired")
    harness.record_command_rejection("out_of_order")

    assert harness.report()["command_rtt_ms"] == {
        "count": 2,
        "p95": 120.0,
        "jitter_p95": 20.0,
        "dropped": 0,
    }
    assert harness.report()["command_rejections"] == {"expired": 1, "out_of_order": 1}
    assert harness.report()["virtual_stick_hz"] == 20.0


def test_bench_report_measures_telemetry_video_and_phone_load_without_hardware() -> None:
    harness = BenchHarness()
    harness.record_telemetry(1_000)
    harness.record_telemetry(1_100)
    harness.record_telemetry(1_200)
    harness.record_video_frame(
        captured_at_ms=1_000,
        controller_at_ms=1_120,
        decoded_at_ms=1_145,
        delivered_at_ms=1_180,
    )
    harness.record_command_drop(2)
    harness.record_video_drop(3)
    harness.record_phone_sample(thermal_c=39.5, throttled=False, battery_draw_ma=1_200)

    report = harness.report()

    assert report["telemetry_hz"] == 10.0
    assert report["video_latency_ms"] == {
        "aircraft_to_controller_p95": 120.0,
        "android_processing_p95": 25.0,
        "lan_delivery_p95": 35.0,
        "glass_to_glass_p95": 180.0,
        "dropped_frames": 3,
    }
    assert report["phone"] == {
        "max_thermal_c": 39.5,
        "throttled_samples": 0,
        "max_battery_draw_ma": 1_200.0,
    }


def test_cli_replays_jsonl_without_hardware_network_or_credentials(tmp_path, capsys) -> None:
    source = tmp_path / "bench.jsonl"
    source.write_text(
        "\n".join(
            (
                json.dumps({"type": "command", "sent_at_ms": 150, "round_trip_ms": 50}),
                json.dumps({"type": "telemetry", "observed_at_ms": 100}),
                json.dumps({"type": "telemetry", "observed_at_ms": 200}),
            )
        )
        + "\n"
    )

    exit_code = main(["--input", str(source)])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["command_rtt_ms"] == {
        "count": 1,
        "p95": 50.0,
        "jitter_p95": None,
        "dropped": 0,
    }
    assert report["telemetry_hz"] == 10.0
    assert report["virtual_stick_hz"] is None
