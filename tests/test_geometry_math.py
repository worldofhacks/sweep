import math

import pytest

from tools.geometry_math import (
    distance_to_segment,
    inset_cell,
    point_inside,
    polygon,
    polygon_cell_intersects,
    rect_inside_polygon,
    rect_polygon_distance,
    rect_segment_distance,
    segments_intersect,
)

SQUARE = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
NOTCH = [[0, 0], [4, 0], [4, 4], [2.1, 4], [2.1, 1], [1.9, 1], [1.9, 4], [0, 4], [0, 0]]


def test_polygon_and_boundary_containment():
    assert polygon(SQUARE) == SQUARE
    assert polygon(list(reversed(SQUARE))) == list(reversed(SQUARE))
    assert point_inside(SQUARE, (0, 2))
    assert point_inside(SQUARE, (2, 2))
    assert not point_inside(SQUARE, (-1e-12, 2))
    assert not point_inside(NOTCH, (2, 3))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        [[0, 0], [1, 0], [0, 1]],
        [[0, 0], [1, 0], [math.nan, 1], [0, 0]],
        [[0, 0], [True, 0], [0, 1], [0, 0]],
        [[0, 0], [1, 0], [2, 0], [0, 0]],
        [[0, 0], [4, 4], [0, 3], [4, 0], [0, 0]],
        [[0, 0], [3, 0], [1, 0], [1, 2], [0, 0]],
        [[0, 0], [1e308, 0], [1e308, 1e308], [0, 0]],
        [[0, 0], [1, 0], [1, 1], [1, 0], [0, 0]],
    ],
)
def test_polygon_rejects_malformed_geometry(value):
    with pytest.raises(ValueError):
        polygon(value)


def test_straight_adjacent_edges_are_valid_without_backtracking():
    polygon([[0, 0], [1, 0], [2, 0], [2, 2], [0, 2], [0, 0]])


def test_concave_notch_cannot_be_missed_by_corner_sampling():
    assert all(point_inside(NOTCH, p) for p in [(1, 2), (3, 2), (3, 3), (1, 3)])
    assert not rect_inside_polygon((1, 2, 3, 3), NOTCH)
    assert rect_inside_polygon((0.2, 0.2, 1, 3), NOTCH)
    assert rect_inside_polygon((0, 0, 4, 4), SQUARE)
    assert not rect_inside_polygon((-1, 0, 4, 4), SQUARE)


def test_boundary_aligned_concave_slot_is_not_contained():
    slot = [
        [0, 0],
        [4, 0],
        [4, 4],
        [2.1, 4],
        [2.1, 1],
        [1.9, 1],
        [1.9, 4],
        [0, 4],
        [0, 0],
    ]
    assert all(point_inside(slot, point) for point in [(1.9, 1), (2.1, 1), (2.1, 4), (1.9, 4)])
    assert not point_inside(slot, (2, 2.5))
    assert not rect_inside_polygon((1.9, 1, 2.1, 4), slot)


def test_segment_distance_projection_endpoints_and_degenerate_segments():
    assert distance_to_segment((2, 3), (0, 0), (4, 0)) == 3
    assert distance_to_segment((7, 4), (0, 0), (4, 0)) == 5
    assert distance_to_segment((7, 4), (4, 0), (0, 0)) == 5
    assert distance_to_segment((3, 4), (0, 0), (0, 0)) == 5


@pytest.mark.parametrize(
    "a,b,c,d,expected",
    [
        ((0, 0), (2, 2), (0, 2), (2, 0), True),
        ((0, 0), (2, 0), (2, 0), (4, 0), True),
        ((0, 0), (2, 0), (3, 0), (4, 0), False),
        ((0, 0), (0, 0), (0, 0), (0, 0), True),
        ((0, 0), (0, 0), (1, 0), (1, 0), False),
        ((0, 0), (2, 0), (1, 0), (1, 0), True),
    ],
)
def test_segments_intersect_under_endpoint_reversal(a, b, c, d, expected):
    for first, second in [(a, b), (b, a)]:
        assert segments_intersect(first, second, c, d) is expected
        assert segments_intersect(c, d, first, second) is expected


def test_rectangle_clearance_and_contact_are_conservative():
    rect = (0, 0, 1, 1)
    assert rect_segment_distance(rect, (4, 5), (4, 6)) == 5
    assert rect_segment_distance(rect, (0.5, -1), (0.5, 2)) == 0
    assert rect_segment_distance(rect, (1, 1), (2, 2)) == 0
    assert rect_segment_distance(rect, (0.5, 0.5), (0.5, 0.5)) == 0
    assert rect_polygon_distance((5, 5, 6, 6), SQUARE) == pytest.approx(math.sqrt(2))
    assert polygon_cell_intersects(SQUARE, (4, 1, 5, 2))
    assert polygon_cell_intersects(SQUARE, (-1, -1, 5, 5))
    assert polygon_cell_intersects(SQUARE, (1, 1, 2, 2))
    assert not polygon_cell_intersects(SQUARE, (5, 1, 6, 2))
    assert polygon_cell_intersects(NOTCH, (1.8, 2, 2.2, 3))
    assert not polygon_cell_intersects(NOTCH, (1.95, 2, 2.05, 3))


def test_inset_requires_clearance_everywhere_and_rounds_toward_blocked():
    assert inset_cell((1, 1, 3, 3), SQUARE, 0.9)
    assert not inset_cell((1, 1, 3, 3), SQUARE, 1)
    assert not inset_cell((1, 1, 3, 3), SQUARE, 1.1)
    assert inset_cell((0, 0, 4, 4), SQUARE, 0)
    assert not inset_cell((1, 2, 3, 3), NOTCH, 0)
    assert not inset_cell((0.2, 2, 1.8, 3), NOTCH, 0.15)
