from __future__ import annotations

import json
from collections.abc import Mapping
from threading import Lock

from relay.observations import Observation


class GroundNavigationIdentityStore:
    def __init__(self, session: str) -> None:
        self.session = session
        self._records: dict[int, tuple[int, str, int, dict[str, str] | None, int]] = {}
        self._lock = Lock()

    def accept(self, observation: Observation) -> None:
        """Consume only observations already authenticated and admitted by relay ingress."""
        submission = observation.submission
        payload = submission.payload
        if (
            submission.session != self.session
            or submission.node_type != "ground"
            or payload.get("kind") != "status"
            or payload.get("code") != "ground_navigation_identity"
        ):
            return
        try:
            value = json.loads(payload["detail"])
            if (
                type(value) is not dict
                or set(value)
                != {"odom_origin_id", "pose_source_id", "registration_id", "configuration_sha256"}
                or any(type(item) is not str or not item for item in value.values())
            ):
                value = None
        except (ValueError, TypeError):
            value = None
        with self._lock:
            previous = self._records.get(submission.device_id)
            if previous is not None and (
                submission.connection_epoch < previous[0] or observation.t_ingest < previous[2]
            ):
                return
            if previous is None and len(self._records) >= 128:
                return
            generation = (
                1
                if previous is None
                else previous[4]
                + int(
                    (previous[0], previous[1], previous[3])
                    != (submission.connection_epoch, submission.source_id, value)
                )
            )
            self._records[submission.device_id] = (
                submission.connection_epoch,
                submission.source_id,
                observation.t_ingest,
                value,
                generation,
            )

    def require(
        self,
        device_id: int,
        epoch: int,
        source_id: str,
        expected: Mapping[str, str],
        *,
        now_ms: int,
        max_age_ms: int,
        expected_generation: int | None = None,
    ) -> int:
        with self._lock:
            record = self._records.get(device_id)
        if (
            record is None
            or record[0] != epoch
            or record[1] != source_id
            or not 0 <= now_ms - record[2] < max_age_ms
            or record[3] != dict(expected)
            or (expected_generation is not None and record[4] != expected_generation)
        ):
            raise ValueError("current ground navigation identity is unavailable or changed")
        return record[4]
