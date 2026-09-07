from perception.localization_lease import (
    LocalizationLeaseState,
    ManagedWebcamLocalizer,
)
from perception.webcam_stream import WebcamStream


class Reader:
    def __init__(self, frames=(), *, start_error=False):
        self.frames = list(frames)
        self.start_error = start_error
        self.started = False
        self.closed = False

    def start(self):
        if self.start_error:
            raise OSError("cannot open stream")
        self.started = True
        return self

    def read(self, timeout=0):
        assert timeout == 0
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


class Localizer:
    def __init__(self, states=()):
        self.states = list(states)
        self.calls = []

    def update(self, image, decode_time, now):
        self.calls.append((image, decode_time, now))
        return self.states.pop(0)

    def at(self, now):
        return _accepted()


def _accepted():
    return {"accepted": True, "pose_observation": {"accepted": True, "fix": "new"}}


def _unaccepted():
    return {"accepted": True, "pose_observation": {"accepted": False}}


def test_pause_releases_only_the_reader_and_discards_pre_pause_fix():
    reader = Reader()
    localizer = Localizer()
    lease = ManagedWebcamLocalizer(lambda: reader, localizer)

    lease.resume(10)
    assert lease.poll(10.1).state is LocalizationLeaseState.REVALIDATING
    paused = lease.pause()

    assert reader.closed
    assert paused.state is LocalizationLeaseState.PAUSED
    assert paused.current_fix is None
    assert not paused.localization_usable


def test_resume_requires_a_new_accepted_pose_after_the_new_reader_starts():
    first = Reader([("old", 10)])
    second = Reader([("stale", 9), ("unaccepted", 11), ("fresh", 12)])
    readers = iter((first, second))
    localizer = Localizer((_accepted(), _unaccepted(), _accepted()))
    lease = ManagedWebcamLocalizer(lambda: next(readers), localizer)

    lease.resume(0)
    assert lease.poll(10.1).state is LocalizationLeaseState.ACTIVE
    lease.pause()
    resumed = lease.resume(10)

    assert first.closed
    assert resumed.state is LocalizationLeaseState.REVALIDATING
    assert resumed.current_fix is None
    assert lease.poll(10.1).failure_reason == "frame_time_invalid"
    assert lease.poll(11.1).state is LocalizationLeaseState.REVALIDATING
    assert lease.status.failure_reason == "fresh_fix_required"
    active = lease.poll(12.1)
    assert active.state is LocalizationLeaseState.ACTIVE
    assert active.current_fix == _accepted()
    assert [call[0] for call in localizer.calls] == ["old", "unaccepted", "fresh"]


def test_reader_start_failure_is_fail_closed_and_does_not_reuse_old_localization():
    lease = ManagedWebcamLocalizer(lambda: Reader(start_error=True), Localizer())

    status = lease.resume(1)

    assert status.state is LocalizationLeaseState.UNAVAILABLE
    assert status.failure_reason == "reader_start_failed"
    assert status.current_fix is None
    assert not status.localization_usable


def test_active_reader_failure_clears_control_eligibility_without_a_flight_side_effect():
    class BrokenReader(Reader):
        def read(self, timeout=0):
            raise OSError("stream dropped")

    lease = ManagedWebcamLocalizer(lambda: BrokenReader(), Localizer())
    lease.resume(1)

    status = lease.poll(1.1)

    assert status.state is LocalizationLeaseState.UNAVAILABLE
    assert status.failure_reason == "reader_failed"
    assert status.current_fix is None


def test_active_reader_requires_a_new_fix_when_the_existing_fix_ages_out():
    class AgingLocalizer(Localizer):
        def at(self, now):
            assert now == 2
            return {"accepted": False, "pose_observation": {"accepted": True}}

    lease = ManagedWebcamLocalizer(lambda: Reader([("fresh", 1.1)]), AgingLocalizer((_accepted(),)))
    lease.resume(1)
    assert lease.poll(1.2).state is LocalizationLeaseState.ACTIVE

    status = lease.poll(2)

    assert status.state is LocalizationLeaseState.REVALIDATING
    assert status.failure_reason == "fresh_fix_required"
    assert status.current_fix is None


def test_resume_does_not_start_a_second_reader_when_release_fails():
    class StuckReader(Reader):
        def close(self):
            raise OSError("still decoding")

    created = []

    def reader_factory():
        reader = StuckReader() if not created else Reader()
        created.append(reader)
        return reader

    lease = ManagedWebcamLocalizer(reader_factory, Localizer())
    lease.resume(0)

    status = lease.resume(1)

    assert status.state is LocalizationLeaseState.UNAVAILABLE
    assert status.failure_reason == "reader_close_failed"
    assert len(created) == 1


def test_resume_keeps_a_webcam_stream_that_cannot_stop():
    class StubbornProcess:
        def __init__(self):
            self.alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, _timeout):
            pass

        def terminate(self):
            pass

        def kill(self):
            pass

        def close(self):
            raise AssertionError("a live decoder must not be closed")

    stream = WebcamStream("rtsp://localhost/drone1")
    stream._process = StubbornProcess()
    factories = []

    def reader_factory():
        factories.append(True)
        return stream

    lease = ManagedWebcamLocalizer(reader_factory, Localizer())
    lease.resume(0)

    status = lease.resume(1)

    assert status.state is LocalizationLeaseState.UNAVAILABLE
    assert status.failure_reason == "reader_close_failed"
    assert lease._reader is stream
    assert factories == [True]
