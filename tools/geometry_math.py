import math

from tools.map_common import finite_number

_EPS = 1e-9


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def distance_to_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.dist(p, a)
    ux, uy = dx / length, dy / length
    along = max(0, min(length, (p[0] - a[0]) * ux + (p[1] - a[1]) * uy))
    return math.hypot(p[0] - a[0] - along * ux, p[1] - a[1] - along * uy)


def segments_intersect(a, b, c, d):
    """Touching segments and gaps within 1 nm count as intersecting."""
    signs = (_cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b))
    if signs[0] * signs[1] < 0 and signs[2] * signs[3] < 0:
        return True
    return (
        min(
            distance_to_segment(a, c, d),
            distance_to_segment(b, c, d),
            distance_to_segment(c, a, b),
            distance_to_segment(d, a, b),
        )
        <= _EPS
    )


def polygon(value):
    """Validate a closed, simple finite XY polygon; malformed inputs raise ValueError."""
    if not isinstance(value, list) or len(value) < 4:
        raise ValueError("polygon needs three vertices and closure")
    points = []
    for p in value:
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError("polygon point must be [x,y]")
        points.append([finite_number(v, "polygon coordinate") for v in p])
    if points[0] != points[-1]:
        raise ValueError("polygon must be closed")
    count = len(points) - 1
    if len({tuple(p) for p in points[:-1]}) != count:
        raise ValueError("polygon repeats a vertex")
    area = sum(_cross(points[0], a, b) for a, b in zip(points, points[1:], strict=False))
    if not math.isfinite(area) or abs(area) <= _EPS:
        raise ValueError("polygon has zero or unrepresentable area")
    for i in range(count):
        a, b, c = points[i - 1 if i else count - 1], points[i], points[i + 1]
        if distance_to_segment(a, b, c) <= _EPS or distance_to_segment(c, a, b) <= _EPS:
            raise ValueError("polygon has overlapping adjacent edges")
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if segments_intersect(points[i], points[i + 1], points[j], points[j + 1]):
                raise ValueError("polygon self-intersects")
    return points


def point_inside(poly, p):
    """Return containment including the exact polygon boundary."""
    inside = False
    for a, b in zip(poly, poly[1:], strict=False):
        if _cross(a, b, p) == 0 and all(
            min(a[i], b[i]) <= p[i] <= max(a[i], b[i]) for i in range(2)
        ):
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x > p[0]:
                inside = not inside
    return inside


def _rect_points(rect):
    x0, y0, x1, y1 = rect
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _segment_enters_rect(a, b, rect):
    low, high = 0.0, 1.0
    for axis in range(2):
        delta = b[axis] - a[axis]
        if delta == 0:
            if not rect[axis] < a[axis] < rect[axis + 2]:
                return False
            continue
        t0 = (rect[axis] - a[axis]) / delta
        t1 = (rect[axis + 2] - a[axis]) / delta
        low, high = max(low, min(t0, t1)), min(high, max(t0, t1))
    return low < high


def rect_inside_polygon(rect, poly):
    """Require full containment, including concave notches between rectangle corners."""
    corners = _rect_points(rect)[:-1]
    center = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    return all(point_inside(poly, p) for p in [*corners, center]) and not any(
        _segment_enters_rect(a, b, rect) for a, b in zip(poly, poly[1:], strict=False)
    )


def _segment_distance(a, b, c, d):
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        distance_to_segment(a, c, d),
        distance_to_segment(b, c, d),
        distance_to_segment(c, a, b),
        distance_to_segment(d, a, b),
    )


def rect_segment_distance(rect, a, b):
    """Return metric clearance; contact or a gap within 1 nm returns zero."""
    corners = _rect_points(rect)
    if point_inside(corners, a) or point_inside(corners, b):
        return 0.0
    return min(_segment_distance(a, b, c, d) for c, d in zip(corners, corners[1:], strict=False))


def rect_polygon_distance(rect, poly):
    """Return metric clearance; overlapping interiors or boundary contact returns zero."""
    if point_inside(poly, _rect_points(rect)[0]):
        return 0.0
    return min(rect_segment_distance(rect, a, b) for a, b in zip(poly, poly[1:], strict=False))


def polygon_cell_intersects(poly, rect):
    return rect_polygon_distance(rect, poly) == 0.0


def inset_cell(rect, poly, inset_m):
    """Require full containment and boundary clearance; numerical uncertainty rejects the cell."""
    if not rect_inside_polygon(rect, poly):
        return False
    if inset_m == 0:
        return True
    distance = min(rect_segment_distance(rect, a, b) for a, b in zip(poly, poly[1:], strict=False))
    return distance >= inset_m + _EPS
