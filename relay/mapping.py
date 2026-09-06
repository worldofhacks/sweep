"""Occupancy mapping: node lidar scans into one log-odds grid per session.

The grid is a display artefact. It is built in memory from accepted ``sensor`` frames,
served as a PNG over the map endpoint, and cleared on request; nothing in the planner,
the arbiter, or the audit reads it, and it is not restored after a relay restart.

Cells hold a clamped int8 log-odds value: negative is free, positive is occupied, zero
is unknown. Every valid range casts one Bresenham ray from the device's pose: the cells
the ray crosses lose confidence and the cell it ends in gains it. Row 0 of the stored
array is the minimum y; ``to_png`` flips it so row 0 of the image is the maximum y, the
orientation a canvas draws top-down.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil, floor
from threading import Lock
from typing import TYPE_CHECKING

import cv2
import numpy as np

from planner.models import Geofence
from relay.contracts import SensorFrame

if TYPE_CHECKING:  # pragma: no cover - imported for typing only; session composes this
    from relay.session import RelaySession

Clock = Callable[[], int]

DEFAULT_RESOLUTION_M = 0.05
# The scans a device takes near the geofence edge still carry returns from the wall
# behind it, so the grid covers more floor than the fence encloses.
GEOFENCE_MARGIN_M = 2.0
FALLBACK_HALF_EXTENT_M = 10.0
# A grid larger than this is a misconfiguration, not a room: 8 million 0.05 m cells cover
# a 140 m square. Refusing it keeps one bad geofence from exhausting the relay's memory.
MAX_GRID_CELLS = 8_000_000

# One scan should not be able to flip a cell that many scans agreed on, and the totals
# stay inside int8 either way.
FREE_LOG_ODDS = -1
OCCUPIED_LOG_ODDS = 3
LOG_ODDS_LIMIT = 100

OCCUPIED_PIXEL = 0
UNKNOWN_PIXEL = 128
FREE_PIXEL = 255


def _epoch_ms() -> int:
    return int(time.time() * 1_000)


def _header_number(value: float) -> str:
    """A header value the console can read back with ``parseFloat`` without surprises."""
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True, slots=True)
class MapImage:
    """An encoded grid plus everything a viewer needs to place it in world coordinates."""

    png: bytes
    resolution_m: float
    origin_x: float
    origin_y: float
    width: int
    height: int
    updated_at: int

    def headers(self) -> dict[str, str]:
        """The ``X-Sweep-Map-*`` headers served beside the image.

        The origin is the world position of the bottom-left cell corner, which is the
        bottom-left of the image only after the viewer accounts for row 0 being the
        maximum y.
        """
        return {
            "X-Sweep-Map-Resolution-M": _header_number(self.resolution_m),
            "X-Sweep-Map-Origin-X": _header_number(self.origin_x),
            "X-Sweep-Map-Origin-Y": _header_number(self.origin_y),
            "X-Sweep-Map-Width": str(self.width),
            "X-Sweep-Map-Height": str(self.height),
            "X-Sweep-Map-Updated-At": str(self.updated_at),
        }


class MappingError(RuntimeError):
    """The grid could not be rendered."""


class OccupancyGrid:
    """A log-odds occupancy grid over a fixed world rectangle.

    The rectangle is snapped outward to whole cells so the origin is a multiple of the
    resolution and a world coordinate always lands in exactly one cell.
    """

    def __init__(
        self,
        *,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        resolution_m: float = DEFAULT_RESOLUTION_M,
    ) -> None:
        if not resolution_m > 0:
            raise ValueError("resolution_m must be positive")
        if not (max_x > min_x and max_y > min_y):
            raise ValueError("grid bounds must be ordered")
        self._resolution_m = float(resolution_m)
        self._origin_x = floor(min_x / resolution_m) * resolution_m
        self._origin_y = floor(min_y / resolution_m) * resolution_m
        self._width = ceil(max_x / resolution_m) - floor(min_x / resolution_m)
        self._height = ceil(max_y / resolution_m) - floor(min_y / resolution_m)
        if self._width * self._height > MAX_GRID_CELLS:
            raise ValueError(
                f"an occupancy grid of {self._width}x{self._height} cells exceeds "
                f"{MAX_GRID_CELLS}; narrow the geofence or coarsen the resolution"
            )
        self._cells = np.zeros((self._height, self._width), dtype=np.int8)
        self._flat = self._cells.reshape(-1)
        self._updated_at = 0

    @property
    def resolution_m(self) -> float:
        return self._resolution_m

    @property
    def origin_x(self) -> float:
        return self._origin_x

    @property
    def origin_y(self) -> float:
        return self._origin_y

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def updated_at(self) -> int:
        """When the grid last changed, in relay milliseconds; 0 before the first scan."""
        return self._updated_at

    def cells(self) -> np.ndarray:
        """A copy of the log-odds array with row 0 at the minimum y."""
        return self._cells.copy()

    def cell_index(self, x: float, y: float) -> tuple[int, int]:
        """The ``(row, column)`` a world point falls in, whether or not it is on the grid."""
        return (
            floor((y - self._origin_y) / self._resolution_m),
            floor((x - self._origin_x) / self._resolution_m),
        )

    def value_at(self, x: float, y: float) -> int:
        """The log-odds of the cell holding a world point; 0 (unknown) when it is off-grid."""
        row, column = self.cell_index(x, y)
        if not (0 <= row < self._height and 0 <= column < self._width):
            return 0
        return int(self._cells[row, column])

    def reset(self, *, now: int = 0) -> None:
        """Forget every observation; the extent and resolution stay."""
        self._cells.fill(0)
        self._updated_at = now

    def integrate(self, frame: SensorFrame, *, now: int) -> None:
        """Cast one ray per range and fold the result into the grid.

        A zero range is no return and is skipped entirely: its bearing gains neither free
        space nor an obstacle. A range below ``range_min_m`` is not trustworthy and is
        skipped the same way. A range beyond ``range_max_m`` clears the ray out to the
        maximum without marking a hit, because nothing was seen inside the sensor's reach.

        A scan taken from a pose outside the grid changes nothing: its rays would claim
        free space the grid cannot place, so the frame is dropped rather than guessed at.
        """
        rows, columns, occupied = self._ray_cells(frame)
        if not rows.size:
            return
        self._apply(rows, columns, occupied)
        self._updated_at = now

    def _ray_cells(self, frame: SensorFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        empty = np.empty(0, dtype=np.int64)
        nothing = (empty, empty, np.empty(0, dtype=bool))
        start_row, start_column = self.cell_index(frame.pose.x, frame.pose.y)
        if not (0 <= start_row < self._height and 0 <= start_column < self._width):
            return nothing

        ranges_m = np.asarray(frame.ranges_cm, dtype=np.float64) / 100.0
        bearings = np.deg2rad(
            frame.pose.yaw_deg
            + frame.angle_min_deg
            + frame.angle_increment_deg * np.arange(ranges_m.size, dtype=np.float64)
        )
        cast = (ranges_m > 0.0) & (ranges_m >= frame.range_min_m)
        hit = cast & (ranges_m <= frame.range_max_m)
        reach = np.where(hit, ranges_m, frame.range_max_m)[cast]
        bearings = bearings[cast]
        hit = hit[cast]
        if not reach.size:
            return nothing

        end_row = np.floor(
            (frame.pose.y + reach * np.sin(bearings) - self._origin_y) / self._resolution_m
        ).astype(np.int64)
        end_column = np.floor(
            (frame.pose.x + reach * np.cos(bearings) - self._origin_x) / self._resolution_m
        ).astype(np.int64)

        rows, columns, endpoint, live = _bresenham(
            start_row,
            start_column,
            end_row,
            end_column,
            # A straight ray that starts inside the rectangle has left it by then, so
            # anything further is clipped away regardless; the cap keeps an unbounded
            # ``range_max_m`` from allocating an unbounded step axis.
            max_steps=max(self._width, self._height),
        )
        occupied = endpoint & hit[:, None]
        keep = live & (rows >= 0) & (rows < self._height) & (columns >= 0) & (columns < self._width)
        return rows[keep], columns[keep], occupied[keep]

    def _apply(self, rows: np.ndarray, columns: np.ndarray, occupied: np.ndarray) -> None:
        """Accumulate one scan's deltas per touched cell, then clamp back into int8."""
        deltas = np.where(occupied, OCCUPIED_LOG_ODDS, FREE_LOG_ODDS).astype(np.int32)
        indices = rows * self._width + columns
        touched, inverse = np.unique(indices, return_inverse=True)
        totals = np.zeros(touched.size, dtype=np.int32)
        np.add.at(totals, inverse, deltas)
        totals += self._flat[touched].astype(np.int32)
        np.clip(totals, -LOG_ODDS_LIMIT, LOG_ODDS_LIMIT, out=totals)
        self._flat[touched] = totals.astype(np.int8)

    def to_png(self) -> MapImage:
        """Encode the grid as 8-bit grayscale: 0 occupied, 255 free, 128 unknown."""
        image = np.full((self._height, self._width), UNKNOWN_PIXEL, dtype=np.uint8)
        image[self._cells < 0] = FREE_PIXEL
        image[self._cells > 0] = OCCUPIED_PIXEL
        # Row 0 of the PNG is the maximum y; the stored array runs the other way.
        encoded, buffer = cv2.imencode(".png", np.ascontiguousarray(image[::-1]))
        if not encoded:
            raise MappingError("the occupancy grid could not be encoded as a PNG")
        return MapImage(
            png=buffer.tobytes(),
            resolution_m=self._resolution_m,
            origin_x=self._origin_x,
            origin_y=self._origin_y,
            width=self._width,
            height=self._height,
            updated_at=self._updated_at,
        )


def _bresenham(
    start_row: int,
    start_column: int,
    end_row: np.ndarray,
    end_column: np.ndarray,
    *,
    max_steps: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Every cell on each integer ray from one cell to many, as ``(rays, steps)`` arrays.

    Returns rows, columns, a mask of each ray's last cell, and a mask of the entries that
    belong to a ray at all (rays are padded to the longest one). A ray longer than
    ``max_steps`` is truncated there, so its last cell is a cut, not an endpoint.
    """
    row_delta = end_row - start_row
    column_delta = end_column - start_column
    row_step = np.sign(row_delta)[:, None]
    column_step = np.sign(column_delta)[:, None]
    row_span = np.abs(row_delta)
    column_span = np.abs(column_delta)
    length = np.minimum(np.maximum(row_span, column_span), max_steps)

    steps = np.arange(int(length.max()) + 1, dtype=np.int64)[None, :]
    column_major = (column_span >= row_span)[:, None]
    major = np.where(column_major, column_span[:, None], row_span[:, None])
    minor = np.where(column_major, row_span[:, None], column_span[:, None])
    # The midpoint rule, in integers: the minor axis advances once the accumulated error
    # crosses half a cell. ``major`` is zero only when the ray is a single cell, where the
    # quotient is unused, so the denominator is floored at one.
    denominator = np.maximum(major, 1)
    advance = (2 * steps * minor + denominator) // (2 * denominator)

    rows = start_row + row_step * np.where(column_major, advance, steps)
    columns = start_column + column_step * np.where(column_major, steps, advance)
    live = steps <= length[:, None]
    endpoint = steps == length[:, None]
    return rows, columns, endpoint, live


class SessionMapper:
    """One occupancy grid per relay session, fed by accepted sensor frames.

    ``attach`` subscribes to a session's ``sensor_listeners``; the relay calls those after
    the frame's audit operation commits, on the worker thread that processed the frame,
    while the map endpoint reads from a request thread, so every grid access is locked.
    A grid appears the first time a session produces a scan, which is what makes the map
    endpoint answer 404 until then.
    """

    def __init__(
        self,
        *,
        geofence: Geofence | None = None,
        resolution_m: float = DEFAULT_RESOLUTION_M,
        margin_m: float = GEOFENCE_MARGIN_M,
        clock: Clock | None = None,
    ) -> None:
        self._bounds = _bounds(geofence, margin_m)
        self._resolution_m = resolution_m
        self._clock = clock or _epoch_ms
        self._lock = Lock()
        self._grids: dict[str, OccupancyGrid] = {}

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(min_x, min_y, max_x, max_y)`` every grid this mapper builds covers."""
        return self._bounds

    def attach(self, session: RelaySession) -> None:
        """Subscribe to one session's accepted scans."""
        session.sensor_listeners.append(self.observe)

    def observe(self, frame: SensorFrame) -> None:
        """Fold one accepted scan into its session's grid, creating the grid on demand."""
        now = self._clock()
        with self._lock:
            grid = self._grids.get(frame.session)
            if grid is None:
                min_x, min_y, max_x, max_y = self._bounds
                grid = OccupancyGrid(
                    min_x=min_x,
                    min_y=min_y,
                    max_x=max_x,
                    max_y=max_y,
                    resolution_m=self._resolution_m,
                )
                self._grids[frame.session] = grid
            grid.integrate(frame, now=now)

    def grid(self, session_id: str) -> OccupancyGrid | None:
        """The session's grid, or ``None`` before its first scan."""
        with self._lock:
            return self._grids.get(session_id)

    def render(self, session_id: str) -> MapImage | None:
        """Encode the session's grid, or ``None`` before its first scan."""
        with self._lock:
            grid = self._grids.get(session_id)
            return None if grid is None else grid.to_png()

    def reset(self, session_id: str) -> bool:
        """Clear the session's grid in place; ``False`` when it has none yet.

        The grid survives the reset so the endpoint keeps answering with an all-unknown
        image rather than flipping back to 404 until the next scan arrives.
        """
        now = self._clock()
        with self._lock:
            grid = self._grids.get(session_id)
            if grid is None:
                return False
            grid.reset(now=now)
            return True


def _bounds(geofence: Geofence | None, margin_m: float) -> tuple[float, float, float, float]:
    if margin_m < 0:
        raise ValueError("margin_m must not be negative")
    if geofence is None:
        return (
            -FALLBACK_HALF_EXTENT_M,
            -FALLBACK_HALF_EXTENT_M,
            FALLBACK_HALF_EXTENT_M,
            FALLBACK_HALF_EXTENT_M,
        )
    return (
        geofence.min_x - margin_m,
        geofence.min_y - margin_m,
        geofence.max_x + margin_m,
        geofence.max_y + margin_m,
    )
