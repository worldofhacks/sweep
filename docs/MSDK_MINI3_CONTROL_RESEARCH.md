# Mini 3 MSDK control path: 2026-09-07 research note

The 15:33 failure began after DJI accepted `KeyStartTakeoff`; it occurred when the bridge later tried to enter Virtual Stick advanced mode for the supervised climb. The evidence records `auto takeoff started` at `1788795228032`, an inferred entry to the Virtual Stick phase 3,982 ms later, and `virtual_stick_unavailable` exactly 4,000 ms after that. The exported SessionModel report resolves the earlier listener question: it records three pre-timeout Virtual Stick state changes with `enabled=true` and `authority=UNKNOWN`. The bridge therefore withheld stick parameters because it observed no MSDK authority. It did not time out through a missing callback. The same report has product ID 0, product type `UNRECOGNIZED`, and failed aircraft/RC identity reads, so the flight configuration remains unqualified.

MSDK 5.18 officially lists DJI Mini 3 as supported. That establishes the product family and SDK version as a supported combination. It does not establish that every operation works with this aircraft firmware (`01.00.0500`), the attached controller, and the installed app build. The app must record the product type, aircraft firmware, RC firmware, registration result, and supported status of every key/action it will use on this particular connection. [DJI’s 5.18 release README](https://github.com/dji-sdk/Mobile-SDK-Android-V5/tree/07d37cfdff865cdda9d523b00b723c9984575f8f) lists both its version and Mini 3 support.

## What the evidence proves

The capture at `/home/gauntlet/sweep-deploy/evidence/quick-hover-1788795225951` contains these adapter acknowledgements for intent `7e9b4443-d6a8-4e1b-b61f-3a581c0af0cd`:

| Time, relay event epoch ms | Observed result |
| --- | --- |
| 1788795228032 | `auto takeoff started; target z 1.80 m` |
| 1788795232014 | Inferred start of the post-takeoff Virtual Stick phase, from the later four-second timeout. This is 3,982 ms after the takeoff acknowledgement. |
| 1788795236014 | `virtual stick enabled but MSDK control authority was not confirmed within 4000 ms` |
| 1788795236189 | A following safety-hold path reports `virtual_stick_dropped: flight control authority is UNKNOWN`. |
| 1788795239190 | The relay refuses land because its `control_authority` gate is false while the aircraft is reported hovering. |

The first acknowledgement means the `KeyStartTakeoff` action completed successfully. It does not prove the aircraft finished the climb or that the app subsequently gained Virtual Stick authority. The exported SessionModel report adds pre-timeout observations: event 32 says enabled true, advanced false, authority UNKNOWN; event 33 says enabled true, advanced true, authority UNKNOWN; and event 36 repeats enabled true, advanced true, authority UNKNOWN. These values occur before event 37, the four-second authority timeout. The `UNKNOWN` owner is the observed condition that caused the refusal.

## DJI’s API contracts

DJI exposes two distinct control mechanisms.

`FlightControllerKey.KeyStartTakeoff` and `KeyStartAutoLanding` are flight-controller actions. DJI’s own 5.18 sample calls these actions directly through `FlightControllerKey`; its sample UI exposes Virtual Stick separately. [The sample’s basic aircraft control model](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/07d37cfdff865cdda9d523b00b723c9984575f8f/SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/models/BasicAircraftControlVM.kt) shows that separation.

Virtual Stick is a separate manager. `enableVirtualStick` has a completion callback. Advanced parameters require advanced mode first, and DJI recommends sending parameters at 5 to 25 Hz. DJI says Virtual Stick can be unavailable near a restricted zone or restricted-distance boundary, and describes other advanced-mode limits, including obstacle-avoidance constraints. [The 5.18 Virtual Stick API](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html) and [DJI’s Virtual Stick tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/virtual-stick.html) define those requirements.

The locally resolved `dji-sdk-v5-aircraft-provided-5.18.0.jar` is the exact 5.18 API artifact used by the app (SHA-256 recorded below). Its public signatures match the tagged DJI source and API reference, including a `void` advanced-parameter dispatch.

`VirtualStickState` reports three values: enabled, advanced-mode enabled, and the current authority owner. The manager does not expose a synchronous `getCurrentVirtualStickState()` method. The installed 5.18 `DJIFlightControllerKey` API exposes `KeyVirtualStickEnabled`, `KeyFlightControlCurrentAuthority`, and `KeyFlightControlAuthorityChangeReason`. The first two can get and listen; the reason key can listen only. `KeyManager.getValue(key, callback)` is the asynchronous hardware-read overload; the synchronous overload reads only the MSDK cache. `isKeySupported` is the per-connected-product capability check, distinct from each key’s API-level get/listen flags. The two hardware reads must be issued after the accepted enable for that attempt generation. The listener documentation is specific: it calls `onVirtualStickStateUpdate` when the state changes and `onChangeReasonUpdate` when flight-control authority changes. It does not promise an immediate snapshot when a listener is installed. [VirtualStickState](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager_VirtualStickState.html) and [VirtualStickStateListener](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager_VirtualStickStateListener.html) are the authoritative descriptions.

The tagged DJI 5.18 sample registers a `VirtualStickStateListener` to update display state, calls `enableVirtualStick` with its completion callback, and does not wait for a later listener event before treating that callback as successful. [Its VirtualStickVM](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/07d37cfdff865cdda9d523b00b723c9984575f8f/SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/models/VirtualStickVM.kt) is useful as an API-ordering reference. It is not a safety policy.

DJI’s normal initialization path calls `SDKManager.init`, waits for `INITIALIZE_COMPLETE`, and then calls `registerApp`. Product connection is a separate callback. [The 5.18 sample manager](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/07d37cfdff865cdda9d523b00b723c9984575f8f/SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/models/MSDKManagerVM.kt) implements that order.

## Behaviour of the APK used for the failed flight

`SdkSession` follows DJI’s initialization order. It calls `registerApp` on `INITIALIZE_COMPLETE`, attaches probe/listener work after registration, and reads product and RC identity keys only in the SDK product-connect and product-change callbacks. The exported report records one product connection, generation 1, followed by `UNRECOGNIZED (0)` and `CORE REQUEST_HANDLER_NOT_FOUND` identity reads. It later receives telemetry, including `KeyConnection`, without another product callback or identity retry. This is a lifecycle gap: identity may be read before the aircraft-side keys are usable and then remain stale. [SdkSession](../adapters/dji_mini3/pilot-app/app/src/probe/kotlin/org/worldofhacks/sweep/bridge/SdkSession.kt) contains the affected lifecycle code.

The supervised takeoff path intentionally starts the aircraft with `KeyStartTakeoff`, waits for the observed flight state, and then uses Virtual Stick to close the climb to the requested height. [FlightController](../adapters/dji_mini3/pilot-app/bridge-core/src/main/kotlin/org/worldofhacks/sweep/bridge/core/flight/FlightController.kt) labels the combined command as takeoff, but the failing operation is the Virtual Stick climb stage. The operator-facing result should distinguish “auto takeoff accepted” from “post-takeoff climb unavailable.”

`DjiFlightPort` maps `enableVirtualStick` to DJI’s completion callback, enables advanced mode after that completion, and forwards state-listener values to the controller. [DjiFlightPort](../adapters/dji_mini3/pilot-app/app/src/probe/kotlin/org/worldofhacks/sweep/bridge/flight/DjiFlightPort.kt) has no independent fresh owner read. `FlightController` then waits up to four seconds for an enabled listener state whose owner is `MSDK`. In this attempt the listener did publish state for the enable generation, and every pre-timeout owner value was `UNKNOWN`. A per-enable hardware read remains useful to corroborate the state and expose support/read errors, but it does not replace the observed authority failure.

The controller correctly releases Virtual Stick after its timeout. Its subsequent safety-hold attempt observes `UNKNOWN` and latches an authority loss. The relay’s earlier admission gate then refuses its land request while the aircraft is hovering. The local controller treats land as a safety command and does not apply its local `relayMotion` authority check to it. The relay’s upstream admission still rejects the request before it reaches the phone. This is a concrete architectural gap: the DJI auto-landing action may be available, while the relay blocks the request solely because app authority is unknown. The relay must not automatically override an unknown authority state, and the RC operator remains the immediate landing path.

## Ranked, falsifiable hypotheses

1. **The flight controller did not grant MSDK authority for this enable attempt.** This is the leading explanation. The listener reported enabled and advanced mode with authority `UNKNOWN` three times before the deadline. A hardware read of `KeyFlightControlCurrentAuthority` after enable can corroborate the state or return a distinct read error.
2. **The app connected before aircraft identity keys became usable.** The report has one SDK product connection, then product ID 0 and request-handler failures, followed later by live telemetry without a second identity query. Retry identity after a `KeyConnection=true` transition and record the result.
3. **A Mini 3 firmware, controller mode, flight mode, geofence, or safety condition prevents authority handoff.** The report cannot identify the connected aircraft or controller. A grounded trace must capture a valid product identity, firmware, RC connection and flight mode before attributing the refusal to a Mini 3 limitation.
4. **The owner key is unsupported while Virtual Stick is supported.** Record `isKeySupported` separately for `KeyVirtualStickEnabled` and `KeyFlightControlCurrentAuthority`. If the owner key is unsupported, the current strict policy has no hardware-read proof path and cannot qualify Virtual Stick control on that configuration.
5. **An advanced parameter or its rate is rejected after authority is granted.** This remains untested because the bridge correctly sent no control parameter while authority was UNKNOWN. DJI’s parameter send is `void`, so dispatch does not prove aircraft acceptance.

## Minimal qualification before another flight command

Run this on the ground, with no takeoff, and with the RC operator ready to take over. It is a diagnostic sequence, not a flight procedure.

1. Start a new SDK session. Record app build hash, MSDK version, registration result, product type, aircraft firmware, RC type/firmware, product and RC connection, current flight mode, and `isKeySupported` for every action/key used. If the first identity read is unrecognized or fails, retry it after a `KeyConnection=true` telemetry transition. For the Virtual Stick enabled, owner, and change-reason keys, also record their API-level get/listen flags.
2. Install the Virtual Stick listener before the attempt. Give the attempt a new generation and record listener installation time, every state value, every change reason, and its generation.
3. Record the enable issue time and its completion result. After completion, use `KeyVirtualStickEnabled` and `KeyFlightControlCurrentAuthority`, each created with `KeyTools.createKey`, after `isKeySupported` succeeds. Record each asynchronous `KeyManager.getValue` issue/completion time and exact value. Capture `KeyFlightControlAuthorityChangeReason` through its listener for diagnosis. Do not reuse a connection-time cache.
4. Enable advanced mode only after enable success. Record the operation time. After fresh MSDK authority is confirmed, dispatch one neutral advanced parameter and record the dispatch. If the bridge starts its normal neutral stream after authority confirmation, use the live relay setting of 10 Hz and record every dispatch. Do not treat the void dispatch as aircraft acceptance.
5. Hold the four-second deadline. A pass requires a generation-matched fresh observation of `enabled=true` and `owner=MSDK` before the deadline. A failure retains the exact owner, reason, and disable outcome. An `UNKNOWN` owner observed before the deadline is an authority refusal, not a listener timeout.
6. On every exit, dispatch neutral only if Virtual Stick remains enabled, request disable, and record its completion. A listener callback after disable belongs to the completed generation and cannot confirm a later attempt.
7. Exercise the chosen landing path only after the ownership result is known. Capture whether the local `KeyStartAutoLanding` action is supported and how the relay policy handles a hovering aircraft with unknown app authority. The RC operator remains the immediate fallback.

This trace distinguishes a state-observation defect from a true DJI control rejection. It also produces the exact firmware and controller evidence needed to ask DJI support about a Mini 3 specific limitation.

## Diagnostic build installed after this investigation

At 16:38 UTC, registered APK `b4953916` was installed and its package hash verified.
It refreshes identity on physical aircraft connection, fences old identity callbacks,
and performs fresh paired hardware reads after each Virtual Stick enable. Its grounded
check waits for the disable callback and fails on a bounded cleanup timeout. The
registered build passed 188 core, 51 bridge-node, 14 bench, and 53 probe tests.
Physical authority acquisition and release remain pending; no new flight was attempted.
[The source PR](https://github.com/worldofhacks/sweep/pull/312) describes this diagnostic.

## Motion prediction between camera updates

The installed 5.18 key catalog exposes fused aircraft attitude and velocity, plus
IMU health/calibration information. It does not expose a raw three-axis angular-rate
or acceleration key. `Attitude` and `Velocity3D` contain their value components,
without an acquisition timestamp. [DJI's IMUState reference](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Value_FlightController_Struct_IMUState.html)
describes calibration states and biases rather than a raw inertial sample stream.

The phone records callback receipt time for these values. Its canonical telemetry
still has `t_capture=null`; receipt time does not establish acquisition time.
The shared integration estimator already supports tag, velocity, and height updates
in `perception/_kalman_replay.py`, while the webcam preview currently supplies only
tag fixes to a constant-velocity model. The estimator capability exists; a qualified
live connection from the Mini 3 telemetry and camera producer remains unfinished.

A next implementation can use measured DJI velocity to predict between tag fixes,
then let accepted tags correct drift. It first needs measured callback rate and
latency, coordinate alignment, uncertainty bounds, and the camera's changing gimbal
transform. Raw visual-inertial odometry would additionally require a suitable IMU
sample stream and sensor calibration. [OpenVINS documents camera/IMU bias, spatial,
and timing requirements](https://docs.openvins.com/gs-calibration.html). The current
RC phone's IMU measures the operator-held phone, so it cannot stand in for an
aircraft-mounted sensor.

## Sources

- [DJI Mobile SDK Android V5 tag V5.18.0](https://github.com/dji-sdk/Mobile-SDK-Android-V5/tree/07d37cfdff865cdda9d523b00b723c9984575f8f)
- [DJI Virtual Stick API reference](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html)
- [DJI KeyManager API reference](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/IKeyManager.html)
- [DJI VirtualStickState API reference](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager_VirtualStickState.html)
- [DJI VirtualStickStateListener API reference](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager_VirtualStickStateListener.html)
- [DJI Virtual Stick tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/virtual-stick.html)
- [DJI V5.18 sample VirtualStickVM](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/07d37cfdff865cdda9d523b00b723c9984575f8f/SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/models/VirtualStickVM.kt)
- [DJI V5.18 sample MSDKManagerVM](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/07d37cfdff865cdda9d523b00b723c9984575f8f/SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/models/MSDKManagerVM.kt)
- Local SDK API artifact: `/home/gauntlet/.gradle/caches/modules-2/files-2.1/com.dji/dji-sdk-v5-aircraft-provided/5.18.0/9e9b804f34cbc8ca97155c984d844f4ec6d38b2f/dji-sdk-v5-aircraft-provided-5.18.0.jar`, SHA-256 `3dd3de8e25f96686ede3e75856f8983b994e263932fa715434939913d6a9a614`. JDK 21 `javap` confirms API flags: enabled is Boolean/get/set/listen; current authority is `FlightControlAuthority`/get/listen; authority-change reason is `FlightControlAuthorityChangeReason`/listen only.
- Local evidence: `/home/gauntlet/sweep-deploy/evidence/quick-hover-1788795225951/events.json`, `phone-logcat.txt`, `telemetry-keys.jsonl`, and `session-probe-report-after-flight.txt`
