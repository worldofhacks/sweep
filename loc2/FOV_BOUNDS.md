# FOV bounds for the DJI Mini 3 calibration candidate

`calibration/tag_intrinsics.py` rejects a pinhole candidate whose `pipeline` lacks
`fov_bounds_deg`, and separately checks the fitted FOV against it
(`calibration/README.md`: "Do not derive these bounds from the calibration result
being checked"). The published spec is the Mini 3's diagonal FOV, 82.1 degrees; the
decoded stream is a 16:9, 1280x720 crop, not the sensor's native diagonal, so the
bound has to come from decomposing that diagonal for a 16:9 frame, not from
measuring this camera's own pixels.

## Derivation

For a pinhole camera, `tan(half-FOV along an axis) = (axis fraction of the
diagonal) * tan(half-diagonal FOV)`, because the half-width and half-height are the
two legs of the same right triangle whose hypotenuse is the half-diagonal. For a
16:9 frame, width and height are `16/sqrt(16^2+9^2) = 0.8716` and `9/sqrt(16^2+9^2)
= 0.4903` of the diagonal.

**Upper bound** -- assume the video crop only selects a 16:9 region and does not
narrow the field further (the loosest case consistent with the spec):

```
tan(41.05 deg) = 0.8697
H_max = 2*atan(0.8716 * 0.8697) = 74.4 deg
V_max = 2*atan(0.4903 * 0.8697) = 46.2 deg
```

**Lower bound** -- DJI's stabilization/EIS crop narrows the field further than a
plain aspect crop; there is no published number for how much, so this allows a
conservative 25% additional reduction in the linear (tangent) extent, not derived
from this fit:

```
H_min = 2*atan(0.75 * tan(H_max/2)) = 59.3 deg
V_min = 2*atan(0.75 * tan(V_max/2)) = 35.5 deg
```

Rounded outward: `horizontal: [58.0, 75.0]`, `vertical: [34.0, 47.0]`.

## Why this isn't circular

Neither bound uses this camera's fitted focal length or pixel data -- only the
published 82.1-degree diagonal spec, the known 16:9 output aspect ratio, and one
explicitly stated conservative crop-margin assumption. The fitted pinhole FOV from
this calibration (`horizontal: 70.53`, `vertical: 43.51`) lands well inside both
bounds, not against either edge, which is what independent bounds are supposed to
show: the fit is consistent with the lens spec, not just admissible because the
bounds were built to admit it.

## Where this lives

`pipeline/dji-mini3-pipeline.json` carries these bounds and is the pipeline object
embedded in `dji-mini3-real-navigation-calibration.json`.
