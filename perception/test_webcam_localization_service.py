from perception.webcam_localization import WebcamLocalizationService


class Reader:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def start(self):
        return self

    def read(self, timeout=0):
        assert timeout == 0
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


class Localizer:
    def __init__(self, results):
        self.results = list(results)

    def update(self, _image, _decoded_at, _now):
        return self.results.pop(0)

    def at(self, _now):
        return {"accepted": False, "pose_observation": None}


def _accepted(label):
    return {
        "type": "webcam_localization",
        "accepted": True,
        "pose_observation": {"accepted": True, "label": label},
    }


def test_production_service_pause_releases_decoder_and_resume_revalidates_a_new_fix():
    first = Reader([("first", 1.1)])
    second = Reader([("stale", 1.1), ("fresh", 3.1)])
    readers = iter((first, second))
    localizer = Localizer((_accepted("first"), _accepted("fresh")))
    service = WebcamLocalizationService(
        localizer, "rtsp://media.example/drone12", stream_factory=lambda _url: next(readers)
    )

    service.resume(1)
    assert service.poll(1.2)["localization_consumer_state"] == "active"
    paused = service.pause()
    resumed = service.resume(3)

    assert first.closed
    assert paused.state.value == "paused"
    assert resumed.state.value == "revalidating"
    assert service.poll(3.2)["localization_consumer_state"] == "revalidating"
    active = service.poll(3.3)
    assert active["localization_consumer_state"] == "active"
    assert active["stream_status"] == "active"
    assert active["pose_observation"]["label"] == "fresh"
