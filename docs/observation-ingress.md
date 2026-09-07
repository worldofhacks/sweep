# Relay observation ingress

Set `SWEEP_OBSERVATIONS_FILE` to a host-owned JSON configuration before starting the relay. The file contains `bindings`, `frames`, `clock_mappings`, and `minimum_interval_ms`. The relay admits no observations when this setting is absent. Each binding pins one session, device, connection epoch, source, node type, allowed payload kinds, and allowed frames. Reconnecting with a new epoch requires a matching configured binding and new local frame declarations.

The configuration uses the fields of `SourceBinding`, `FrameDeclaration`, and `ClockMapping` described in [the observation contract](observation-contract.md). Arrays replace Python tuples. `frames` is an array of declarations. The loader rejects duplicate keys and files larger than 1 MiB; it permits at most 128 bindings, 256 frames, and 128 clock mappings. `minimum_interval_ms` is an integer from 1 through 60,000 and applies independently to each bound source.

Authenticate on `/ws/<session>` as `adapter` with the configured device credential, then join the session. Send the observation submission without `t_ingest`. The relay checks the authenticated device and current membership before applying the source binding, frame, payload, and clock checks. The admitted event carries the relay's Unix-millisecond ingest time and is written unchanged into the audit record's `event` field.

Source receipt time must remain in the same clock domain and move forward within an epoch. Multiple events may share one receipt time, for example tags detected in the same image; they need distinct event IDs and must obey the configured relay admission interval. A receipt group contains at most 1,024 events. Older receipt groups are rejected permanently within that epoch. A source clock reset requires a new connection epoch.

Only authenticated console connections receive observation events. Adapter, localization, keyboard, webcam, and language connections do not receive this stream. An observation is not a control acknowledgement and the submitting adapter receives no success echo. Refused submissions produce the existing relay refusal event.

Post-authentication WebSocket JSON is limited to 1 MiB of text and 32 nesting levels. Observation messages have the stricter 64 KiB UTF-8 limit, including whitespace. Duplicate JSON keys are refused before routing. Existing telemetry, control, and membership messages retain their contracts.

This ingress records diagnostic evidence. It does not update the aircraft compatibility telemetry projection or authorize navigation. Node type comes from host configuration; the producer cannot change it. A local odometry frame needs a measured registration before a world-frame consumer can use it. Missing exposure timestamps remain `null`; an unmapped source clock supplies no capture-latency evidence.
