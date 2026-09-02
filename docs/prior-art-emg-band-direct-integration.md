# EMG wristband direct-integration check

Checked against Wearable Devices' product, SDK, and sample repositories.

## Decision

Mudra Link clears the direct host-access gate. It is currently sold as a standalone wristband, and Wearable Devices documents two ways for software we control to receive its data:

1. Mudra Companion connects to the band and publishes JSON on `ws://127.0.0.1:8766`. The vendor's browser samples subscribe to gesture, button, pressure, and SNC signals.
2. The official Python and Android SDKs connect to Mudra devices over BLE and invoke application callbacks directly. The documented callbacks include Tap, Double Tap, Twist, Double Twist, Press/Release, pressure, navigation, connection state, and battery state. A separate raw-data entitlement exposes three SNC channels plus accelerometer and gyroscope data.

The second route provides Companion-independent access. Vendor dependencies remain: the SDK requires cloud sign-in, internet access, and feature licenses. Raw data may cost extra. The Companion route also requires developer-preview approval.

Select Mudra Link as the candidate, conditional on a one-device access spike. Budget a bounded integration task. The smallest path is a localhost WebSocket client in the webcam console. The Companion-free path is a thin Python BLE producer that normalizes callbacks into Sweep's existing input-source contract. Both terminate at the producer seam, leaving the relay, planner, arbiter, and adapter contracts unchanged.

The hardware gate should require a real device event to pass through the console and intent conformance tests. The public samples synthesize events when Companion is absent, so acceptance evidence must come from connected hardware.

## Integration surfaces and limits

| Surface | Host path | Exposed data | Dependency | Sweep fit |
|---|---|---|---|---|
| Mudra Studio | Band -> Companion -> localhost WebSocket -> browser | Gesture/button JSON, pressure, SNC, accelerometer, gyroscope | Proprietary Companion and approved Studio account | Least code; add a WebSocket source, reconnect handling, and normalization |
| Mudra SDK | Band -> BLE -> Python or Android process | Discrete and continuous gestures, navigation, pressure, state; licensed raw SNC/IMU | Vendor account, internet, and Main or RawData license | Companion-free; add a small native source process and bridge |
| Bluetooth HID | Band -> OS keyboard/mouse events | Configured mouse and keyboard actions | Vendor setup | Technically simple but poor provenance and weaker event semantics; unsuitable as the primary intent source |

The work is a small integration around the selected producer seam. Supported gesture names differ across the Studio page, SDK documentation, and samples, so Sweep should capture real firmware payloads before freezing its mapping enum.

## Procurement status

[Mudra Link](https://mudra-band.com/products/mudra-link) is actively orderable at $249. This is documentary evidence only; stock and delivery were not independently tested.

Myo is discontinued and remains reference material for signal-to-gesture mapping. Mudra passed the direct-host criterion. If its approval or licensing cannot be obtained during the spike, OYMotion's current gForcePro+ is the next candidate to verify: its official documentation advertises BLE, public Python/C++/C#/Android SDKs, recognized gesture IDs with probabilities, and raw eight-channel EMG, but sales availability is less clear than Mudra's retail purchase flow.

## Primary sources

- [Mudra Studio and Companion protocol](https://mudra-band.com/pages/mudra-studio)
- [Mudra Studio gesture browser sample](https://github.com/wearable-devices/mudrastudio-sample/blob/main/gesture-assistant.html)
- [Mudra Studio SNC browser sample](https://github.com/wearable-devices/mudrastudio-sample/blob/main/emg-visualizer.html)
- [Mudra SDK documentation](https://wearable-devices.github.io/)
- [Official Python example](https://github.com/wearable-devices/PythonAppExample)
- [Official `mudra-sdk` package](https://pypi.org/project/mudra-sdk/)
- [OYMotion gForcePro+](https://www.oymotion.com/en/product32)
- [OYMotion SDK list](https://developer.oymotion.com/SDK/SDKList/)
