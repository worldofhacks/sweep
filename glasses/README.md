# glasses

Owner: A (Interaction); C hosts the app and writes the contract tests. Phase 4, starts when the glasses arrive.

Meta Ray-Ban Display web app (Meta Web Apps SDK). Renders one video feed, a minimap, and the alert line. Emits the same intents as the webcam console from pinch (select, confirm), D-pad (cycle drones, step formation), drag (altitude), head direction (translate direction, sweep box), middle pinch (cancel; held, e-stop), and Neural Handwriting (language). Needs HTTPS for the app and a `wss://` path to the relay on the same network (a page served over HTTPS cannot open a plain `ws://` connection); the shared token lives in the config page, not the URL.

Contract tests: the glasses pass the same intent tests as the webcam console (PRD section 5.1).

PRD: sections 5.9, 7.2.
