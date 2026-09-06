# Console and demo review

The console paths from all eleven pinned heads were reviewed against the code at each head and reconciled with the current language-bound request path. Navigation and search remain configured capabilities. A spoken navigation step freezes its route with the relay-minted language intent, retains the route and binding in the confirmation draft, and emits through the language client only after confirmation.

## Reconciliation changes

- `console/src/control/use-control-console.ts` restores configured navigation and search preparation. Pending HTTP previews are discarded after session, connection, selection, roster, capability, or preview-generation changes. Their deadline starts before the HTTP call.
- `console/src/modules/speech/SpeechModule.tsx` stages an asynchronous spoken navigation preview without changing the plan step's ID or source. It discards a late response after a replacement plan, cancellation, expiry, session change, or selection revision.
- `console/src/relay/contract.ts` keeps the exact language-step validator and adds the navigation/search envelopes. C1 advertises its earned controls. C2 has the fleet additions including disarm. Custom configured profiles can add navigation and search, while disarm is accepted only by the exact C2 profile.
- `console/src/modules/speech/SpeechModule.test.tsx` verifies that route preview receives the bound `language` request and the confirmed request uses the same ID and source.

## Pinned-head review

| PR | Head | Console result | Call-site coverage |
| --- | --- | --- | --- |
| 223 | `19b00ed1e6` | Fixed during reconciliation. Its broad console supported-intent set would make configured navigation/search appear locally implemented regardless of the effective relay profile. | Relay state parser, fixture capability state, C2/profile checks. |
| 222 | `0b3cd0bdb0` | Integrated. C2 now permits disarm and fleet operations, while C1 remains limited to its advertised controls. | `isCapabilityAdvertisement`, `SUPPORTED_INTENTS`, Control gating, fixture client. |
| 221 | `018ccc41f9` | Browser proof only. No product console module change. The smoke script is retained as an end-to-end dependency on configured search and the signed-node demo. | Browser search flow, confirmation dock, status and acknowledgement assertions. |
| 216 | `aebdd79a55` | No console source change. The simulator composition supplies the search runtime consumed by the console clients. | Search demo dependency only. |
| 215 | `c2a0e1d31a` | No console source change. Its relay setting controls the C2 profile seen by the console. | Capability profile dependency only. |
| 214 | `89c10a8d3f` | No console source change. Its release profile defines the C2 intents represented by the console contract. | C2 capability dependency only. |
| 213 | `c500a4d461` | Browser proof only. No product console module change. | Browser navigation preview, cancellation, confirmation, telemetry and landing assertions. |
| 201 | `f340509623` | No console source change. The demo is the signed-node runtime used by navigation browser proof. | Navigation demo dependency only. |
| 194 | `b413963e34` | Integrated with fixes. Search keeps a frozen preview in the dock, uses the same request ID on confirmation, and polls/acknowledges findings without issuing another motion request. | `prepareSearch`, `SearchModule`, `HttpSearchClient`, runtime bootstrap and fixture state. |
| 193 | `f303ed4960` | Reworked. The original stages spoken navigation as a console request, which breaks source binding. The reconciled path preserves the relay-issued language envelope and attaches route plus voice binding before confirmation. | `prepareVoicePlanStep`, `SpeechModule.stageStep`, navigation HTTP client, dock confirmation. |
| 164 | `521d59966c` | Integrated where still applicable. Fixture and C2 capability changes are checked against current confirmation, selection, and source behavior. | Fixture relay state, fleet controls, gesture and browser-demo dependencies. |

## Validation

`pnpm test` passed with 476 tests. The focused reconciliation set passed with 98 tests. TypeScript, ESLint, production build, and `git diff --check` passed after the final reconciliation. Browser navigation and search scripts require the merged Python demo runtime and were not launched while that merge was still in progress.
