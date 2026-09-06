"""Per-aircraft video evidence for the state projection: node claims plus MediaMTX readiness.

The projection is exactly ``{"status": live|offline|unreported, "last_frame_at": int|null}``,
the shape the console contract (``console/src/relay/contract.ts`` ``MediaStreamState``) accepts.
MediaMTX decides the status while its API answers, because it is what the console can play;
when it is unreachable, failing, or unconfigured the projection degrades to the node's own
``node_status.video_publish_state`` and never upgrades anything to live on its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

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


@dataclass(slots=True)
class _PathState:
    online: bool
    last_frame_at: int | None
    observed_at: int
    inbound_bytes: int | None


class MediaMonitor:
    """Polls MediaMTX path readiness on its own task and answers evidence reads at once.

    The poll never runs inside the relay lock or the fan-out: readers get the last completed
    cycle. A cycle counts only when every path answered; one timeout leaves the previous
    evidence in place until it ages past ``stale_after_ms`` and the projection degrades.
    """

    def __init__(
        self,
        client: MediaPathClient,
        *,
        clock: Clock,
        drone_ids: Iterable[int] = DEFAULT_MEDIA_DRONE_IDS,
        poll_interval_ms: int = 1_000,
        stale_after_ms: int = 3_000,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be positive")
        if stale_after_ms < poll_interval_ms:
            raise ValueError("stale_after_ms must be at least poll_interval_ms")
        self._client = client
        self._clock = clock
        self._drone_ids = tuple(drone_ids)
        self._poll_interval_s = poll_interval_ms / 1_000
        self._stale_after_ms = stale_after_ms
        self._paths: dict[int, _PathState] = {}
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
        """Read every path once; return whether the whole cycle completed."""
        results = await asyncio.gather(
            *(self._client.read_path(stream_name(drone_id)) for drone_id in self._drone_ids),
            return_exceptions=True,
        )
        now = self._clock()
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            self._note_reachable(False, failures[0])
            return False
        for drone_id, result in zip(self._drone_ids, results, strict=True):
            observation = result if isinstance(result, MediaPathObservation) else None
            self._paths[drone_id] = self._merge(self._paths.get(drone_id), observation, now)
        self._note_reachable(True, None)
        return True

    def evidence(self, drone_id: int, now_ms: int) -> MediaEvidence | None:
        state = self._paths.get(drone_id)
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
        # Bytes that grew since the last read are frames; an unchanged count is a stalled path,
        # still online but with an ageing last frame. Without a byte count, online is the evidence.
        if (
            inbound is None
            or previous is None
            or previous.inbound_bytes is None
            # A restarted path can reset the counter without an offline sample between
            # polls. Any counter change is therefore fresh media evidence; equality is
            # the only observation that proves a stall.
            or inbound != previous.inbound_bytes
        ):
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
    if evidence is not None and evidence.fresh:
        status = "live" if evidence.online else "offline"
    elif membership is Membership.DISCONNECTED:
        status = "offline" if last_frame_at is not None else "unreported"
    elif node_status is None:
        status = "unreported"
    elif node_status.video_publish_state is VideoPublishState.PUBLISHING:
        status = "live"
    else:
        status = "offline"
    return {"status": status, "last_frame_at": last_frame_at}
