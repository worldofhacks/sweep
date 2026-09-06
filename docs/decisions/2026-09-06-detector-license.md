# Detector licensing: Apache default, AGPL opt-in only

Status: accepted, 2026-09-06. Resolves
[#96](https://github.com/worldofhacks/sweep/issues/96).

## Decision

Sweep's default fixed-class detector remains the pinned YOLOX-s ONNX artifact used through
OpenCV DNN. The implementation and reference model are Apache-2.0-compatible and require no
Ultralytics package at runtime.

An AGPL detector or dependency, including an Ultralytics YOLO deployment, may not enter a
default, optional, evaluation, container, or hosted-service dependency set without all of the
following in a separate reviewed change:

1. explicit owner approval for that exact use and distribution/deployment model;
2. a recorded license assessment and the obligations the project will satisfy;
3. an isolated, capability-gated integration that cannot be imported or selected accidentally;
4. lockfile, image, and software-bill-of-materials evidence showing the dependency boundary.

No abstraction or second detector implementation is added merely to reserve that option.

## Why

The current YOLOX path already satisfies the demo's bounded person/common-object detection
needs and is implemented, hash-pinned, and tested. Adding an AGPL alternative now would add a
second runtime and a materially different distribution obligation without improving the fixed
coordination-demo exit.

## Consequences

- CI must remain free of an undeclared `ultralytics` dependency.
- Detector events continue to bind the exact model/configuration digest; this decision does not
  replace recorded-footage or site acceptance.
- A future owner-approved AGPL experiment is a new decision, not an amendment inferred from
  this record.
