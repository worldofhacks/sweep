# Ohmni encoder sampler evidence

The current Ohmni bot-shell encoder path does not qualify a ground node for motion. A single-reader, 60-second probe on unit 11 lost its position at 0.981858 seconds between complete pairs. A right-first retry probe reduced rejected reads but still produced a 0.636941-second gap. The runtime must continue to withdraw pose quality after a gap over 0.35 seconds, and motion remains unsupported until a qualified sampler is measured.

## Recorded probes

All recordings are retained outside the repository at `/var/tmp/gauntlet/sweep-production/odometry-probe-20260907/controlled-11/`.

| Recording | Result | SHA-256 |
| --- | --- | --- |
| `result.json` | Candidate `e55577a8…` ran for 60.000230 seconds. It completed 542 of 600 pairs and latched loss at sample 20 after a 0.981858012-second pair gap. | `a7af7b6f34c9605d714cb91acc2f3500ba0ee140ca2d3eb17ba78b8da6a08571` |
| `apos_response_probe.json` | With `apos 0` followed by `apos 1`, left returned 200 of 200 and right returned 197 of 200. Each missing right reply was an empty response at about 120 ms. | `6ec021bfda858b3d03b5c83225a764035f917a1e915d0ffe6b32c84688798df1` |
| `apos_response_probe_right_first.json` | Reversing the same reads returned both sides in all 200 samples. | `60593d83fbba667ca1e6a5f4b004710f213defbb1628e6425da9ba4bd86ded9c` |
| `right_first_retry_pair_probe.json` | A right-first sampler retried only a missing side. It produced 593 usable pairs of 600, seven rejected cycles, and a 0.636941076-second maximum usable-pair gap. | `e020ac81a34d4b1349f386fadb85cae5fcfb9f80c5ae99dec8a6b83693c233b0` |

The probes used only established `apos 0` and `apos 1` reads while the Sweep runtime was stopped. They did not activate motion.

## Protocol finding

Installed Telebot node source is retained beside the recordings in `native-node-source-11/`. `cmd_apos` sends an individual `query_ram(sid, [58, 2])`; its callback is removed after 100 ms. The Python candidate allowed 120 ms for each command. Callback matching includes both servo ID and register address, so native odometry reads at register 59 cannot consume an `apos` response at register 58.

The node's 500 ms telemetry tick writes separate register-59 requests for right and left wheels five milliseconds apart, then emits its cached `{type: "odo", l, r}` object. It records neither a poll identifier nor receipt times and can combine values from different requests. The shared serial writer has no application-level request serialization. The order-sensitive timeouts show contention on that bus or its firmware response path.

## Follow-up

Add a serialized, owner-side vendor-bus sampler. Each poll must issue the two register-59 reads under one poll ID, record each receipt time, and publish a pair only after both replies arrive. The consumer must reject a missing response, a pair whose receipt skew exceeds the configured bound, or a gap over 0.35 seconds. It must never carry one wheel's prior value into a fresh pair.

The native `node.sock` cannot provide this unchanged: it holds one client and its existing `odo` cache has no paired-read guarantee. The follow-up needs a fan-out telemetry path that preserves the Android owner's connection. A hardware run should demonstrate a continuous qualified pair sequence before enabling ground motion.
