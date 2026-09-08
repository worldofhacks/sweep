"""Per-aircraft video evidence for the state projection: node claims plus MediaMTX readiness.

The projection is exactly ``{"status": live|offline|unreported, "last_frame_at": int|null}``,
the shape the console contract (``console/src/relay/contract.ts`` ``MediaStreamState``) accepts.
Runtime camera projection requires current-epoch MediaMTX byte progress. It describes
producer transport evidence; browser decoding is a separate status. The legacy registry
projection retains node claims for compatibility, while every runtime camera uses the
stricter ``project_camera_video`` projection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

from media.streams import CameraStream, validate_camera_mapping
from relay.contracts import Membership, NodeStatusFrame, VideoPublishState

_LOGGER = logging.getLogger(__name__)

VIDEO_STATUSES = ("live", "offline", "unreported")
DEFAULT_MEDIA_DRONE_IDS = (1, 2, 3, 4)

Clock = Callable[[], int]


def stream_name(drone_id: int) -> str:
    """The MediaMTX path an aircraft publishes to; the console derives the same name."""
    return f"drone{drone_id}"


class MediaUnreachable(RuntimeError):
    """The MediaMTX API did not answer with a usable path document."""


@dataclass(frozen=True, slots=True)
class MediaPathObservation:
    """One read of a MediaMTX path. ``online`` is the publisher's presence, bytes are inbound."""

    online: bool
    inbound_bytes: int | None


class MediaPathClient(Protocol):
    async def read_path(self, name: str) -> MediaPathObservation | None:
        """Return the path document, ``None`` when MediaMTX has no such path, or raise
        :class:`MediaUnreachable`."""

    async def close(self) -> None: ...


def parse_path_observation(payload: object) -> MediaPathObservation:
    """Read a v3 Path document: ``online``/``inboundBytes``, else their deprecated aliases."""
    if not isinstance(payload, Mapping):
        raise MediaUnreachable("MediaMTX path document is not an object")
    online = payload.get("online", payload.get("ready"))
    if not isinstance(online, bool):
        raise MediaUnreachable("MediaMTX path document has no online flag")
    inbound = payload.get("inboundBytes", payload.get("bytesReceived"))
    if inbound is not None and (
        isinstance(inbound, bool) or not isinstance(inbound, int) or inbound < 0
    ):
        raise MediaUnreachable("MediaMTX path document has a non-negative integer byte count")
    return MediaPathObservation(online=online, inbound_bytes=inbound)


class MediaMtxClient:
    """HTTP Basic client for ``/v3/paths/get/{name}`` with one bounded timeout per request."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(username, password),
            timeout=timeout_s,
            transport=transport,
        )

    async def read_path(self, name: str) -> MediaPathObservation | None:
        try:
            response = await self._client.get(f"/v3/paths/get/{name}")
        except httpx.HTTPError as error:
            raise MediaUnreachable(
                f"MediaMTX API request failed: {type(error).__name__}"
            ) from error
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MediaUnreachable(f"MediaMTX API answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise MediaUnreachable("MediaMTX API returned malformed JSON") from error
        return parse_path_observation(payload)

    async def close(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True, slots=True)
class MediaEvidence:
    """What MediaMTX last said about one path, and whether that read is still fresh."""

    online: bool
    last_frame_at: int | None
    observed_at: int
    fresh: bool


MediaEvidenceProvider = Callable[[int, int], MediaEvidence | None]
CameraEvidenceProvider = Callable[[int, str, int], MediaEvidence | None]


@dataclass(slots=True)
class _PathState:
    online: bool
    last_frame_at: int | None
    observed_at: int
    inbound_bytes: int | None


class MediaMonitor:
    """Polls MediaMTX path readiness on its own task and answers evidence reads at once.

    Polling stays outside the relay lock. Each camera updates and expires independently;
    a failed secondary path cannot freeze the primary feed.
    """

    def __init__(
        self,
        client: MediaPathClient,
        *,
        clock: Clock,
        drone_ids: Iterable[int] = DEFAULT_MEDIA_DRONE_IDS,
        poll_interval_ms: int = 1_000,
        stale_after_ms: int = 3_000,
        streams: Mapping[int, str] | None = None,
        cameras: Mapping[int, tuple[CameraStream, ...]] | None = None,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be positive")
        if stale_after_ms < poll_interval_ms:
            raise ValueError("stale_after_ms must be at least poll_interval_ms")
        self._client = client
        self._clock = clock
        self._drone_ids = tuple(drone_ids)
        primary = dict(streams or {})
        configured = (
            cameras
            if cameras is not None
            else {
                device_id: (
                    CameraStream(
                        "primary", "Primary camera", primary.get(device_id, stream_name(device_id))
                    ),
                )
                for device_id in self._drone_ids
            }
        )
        self._cameras = validate_camera_mapping(configured, set(self._drone_ids))
        self._streams = {
            (device_id, camera.camera_id): camera.stream
            for device_id, entries in self._cameras.items()
            for camera in entries
        }
        self._poll_interval_s = poll_interval_ms / 1_000
        self._stale_after_ms = stale_after_ms
        self._paths: dict[tuple[int, str], _PathState] = {}
        self._reachable: bool | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def reachable(self) -> bool | None:
        """``None`` before the first cycle, then whether the last cycle completed."""
        return self._reachable

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._client.close()

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("media monitor cycle failed unexpectedly")
            await asyncio.sleep(self._poll_interval_s)

    async def poll_once(self) -> bool:
        """Read every configured device's path once; return whether the cycle completed."""
        results = await asyncio.gather(
            *(self._client.read_path(stream) for stream in self._streams.values()),
            return_exceptions=True,
        )
        now = self._clock()
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        failures = [result for result in results if isinstance(result, Exception)]
        for key, result in zip(self._streams, results, strict=True):
            if isinstance(result, Exception):
                continue
            observation = result if isinstance(result, MediaPathObservation) else None
            self._paths[key] = self._merge(self._paths.get(key), observation, now)
        self._note_reachable(not failures, failures[0] if failures else None)
        return not failures

    def evidence(self, drone_id: int, now_ms: int) -> MediaEvidence | None:
        entries = self._cameras.get(drone_id, ())
        return None if not entries else self.camera_evidence(drone_id, entries[0].camera_id, now_ms)

    def camera_evidence(self, drone_id: int, camera_id: str, now_ms: int) -> MediaEvidence | None:
        state = self._paths.get((drone_id, camera_id))
        if state is None:
            return None
        age_ms = now_ms - state.observed_at
        fresh = 0 <= age_ms <= self._stale_after_ms
        return MediaEvidence(
            online=state.online,
            last_frame_at=state.last_frame_at,
            observed_at=state.observed_at,
            fresh=fresh,
        )

    @staticmethod
    def _merge(
        previous: _PathState | None, observation: MediaPathObservation | None, now: int
    ) -> _PathState:
        last_frame_at = None if previous is None else previous.last_frame_at
        if observation is None or not observation.online:
            return _PathState(
                online=False, last_frame_at=last_frame_at, observed_at=now, inbound_bytes=None
            )
        inbound = observation.inbound_bytes
        # A ready publisher and its first byte count establish only a baseline.
        # Require observed byte progress before dating media; this is transport
        # evidence, not proof that a browser decoded a video frame. A reconnect,
        # unavailable counter or counter reset requires a new baseline too.
        if (
            inbound is None
            or previous is None
            or not previous.online
            or previous.inbound_bytes is None
            or inbound < previous.inbound_bytes
        ):
            last_frame_at = None
        elif inbound > previous.inbound_bytes:
            last_frame_at = now
        return _PathState(
            online=True, last_frame_at=last_frame_at, observed_at=now, inbound_bytes=inbound
        )

    def _note_reachable(self, reachable: bool, error: Exception | None) -> None:
        if reachable == self._reachable:
            return
        self._reachable = reachable
        if reachable:
            _LOGGER.info("MediaMTX API reachable; video projection follows path readiness")
        else:
            _LOGGER.warning(
                "MediaMTX API unreachable (%s); video projection degrades to node claims",
                error,
            )


def project_video(
    *,
    membership: Membership,
    node_status: NodeStatusFrame | None,
    node_publishing_at: int | None,
    evidence: MediaEvidence | None,
) -> dict[str, object]:
    """Derive the console's ``video`` field from the node claim and the MediaMTX evidence."""
    candidates = [node_publishing_at]
    if evidence is not None:
        candidates.append(evidence.last_frame_at)
    known = [value for value in candidates if value is not None]
    last_frame_at = max(known) if known else None
    if membership in {Membership.DISCONNECTED, Membership.LEAVING}:
        status = "offline" if last_frame_at is not None else "unreported"
    elif evidence is not None and evidence.fresh:
        status = "live" if evidence.online else "offline"
    elif node_status is None:
        status = "unreported"
    elif node_status.video_publish_state is VideoPublishState.PUBLISHING:
        status = "live"
    else:
        status = "offline"
    return {"status": status, "last_frame_at": last_frame_at}


def project_camera_video(
    *,
    membership: Membership,
    epoch_started_at: int,
    now_ms: int,
    evidence: MediaEvidence | None,
) -> dict[str, object]:
    """One camera's current-epoch byte progress; node claims cannot grant it liveness."""
    if membership in {Membership.DISCONNECTED, Membership.LEAVING}:
        return {"status": "offline", "last_frame_at": None}
    current = evidence
    if current is not None and (
        not epoch_started_at <= current.observed_at <= now_ms
        or current.last_frame_at is not None
        and not epoch_started_at <= current.last_frame_at <= current.observed_at
    ):
        current = None
    result = project_video(
        membership=membership,
        node_status=None,
        node_publishing_at=None,
        evidence=current,
    )
    frame = result["last_frame_at"]
    if result["status"] == "live" and (frame is None or not 0 <= now_ms - frame <= 5000):
        result["status"] = "unreported"
    return result
