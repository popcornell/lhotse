from typing import Type

from lhotse.audio import RecordingSet, Recording
from lhotse.cut import Cut
from lhotse.features import FeatureSet, Features
from lhotse.manipulation import Manifest
from lhotse.supervision import SupervisionSet, SupervisionSegment


# noinspection PyPep8Naming
def DummyManifest(type_: Type, *, begin_id: int, end_id: int) -> Manifest:
    if type_ == RecordingSet:
        return RecordingSet.from_recordings(dummy_recording(idx) for idx in range(begin_id, end_id))
    if type_ == SupervisionSet:
        return SupervisionSet.from_segments(dummy_supervision(idx) for idx in range(begin_id, end_id))
    if type_ == FeatureSet:
        # noinspection PyTypeChecker
        return FeatureSet(
            features=[dummy_features(idx) for idx in range(begin_id, end_id)],
            feature_extractor='irrelevant'
        )


def dummy_recording(unique_id: int) -> Recording:
    return Recording(
        id=f'dummy-recording-{unique_id:04d}',
        sources=[],
        sampling_rate=16000,
        num_samples=16000,
        duration_seconds=1.0
    )


def dummy_supervision(unique_id: int, start: float = 0.0, duration: float = 1.0) -> SupervisionSegment:
    return SupervisionSegment(
        id=f'dummy-segment-{unique_id:04d}',
        recording_id=f'dummy-recording',
        start=start,
        duration=duration
    )


def dummy_features(unique_id: int) -> Features:
    return Features(
        recording_id=f'dummy-recording-{unique_id:04d}',
        channel_id=0,
        start=0.0,
        duration=1.0,
        type='fbank',
        num_frames=100,
        num_features=20,
        storage_type='lilcom',
        storage_path='irrelevant'
    )


def dummy_cut(id: str = 'irrelevant', start: float = 0.0, duration: float = 1.0, supervisions=None):
    return Cut(
        id=id,
        start=start,
        duration=duration,
        features=dummy_features(0),
        supervisions=supervisions if supervisions is not None else [],
    )
