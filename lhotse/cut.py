import random
from dataclasses import dataclass
from functools import reduce
from math import ceil, floor
from typing import Dict, List, Optional, Iterable, Union
from uuid import uuid4

import numpy as np

from lhotse.audio import RecordingSet
from lhotse.features import Features, FeatureSet, FbankMixer
from lhotse.supervision import SupervisionSegment, SupervisionSet
from lhotse.utils import (
    Seconds,
    Decibels,
    overlaps,
    TimeSpan,
    overspans,
    Pathlike,
    asdict_nonull,
    load_yaml,
    save_to_yaml
)

# One of the design principles for Cuts is a maximally "lazy" implementation, e.g. when overlaying/mixing Cuts,
# we'd rather sum the feature matrices only after somebody actually calls "load_features". It helps to avoid
# an excessive storage size for data augmented in various ways.


# Helper "typedef" to artbitrary Cut type as they do not share a common base class.
# The class names are strings here so that the Python interpreter resolves them after parsing the whole file.
AnyCut = Union['Cut', 'MixedCut']


@dataclass
class Cut:
    """
    A Cut is a single "segment" that we'll train on. It contains the features corresponding to
    a piece of a recording, with zero or more SupervisionSegments.
    """
    id: str

    # Begin and duration are needed to specify which chunk of features to load.
    start: Seconds
    duration: Seconds

    # The features can span longer than the actual cut - the Features object "knows" its start and end time
    # within the underlying recording. We can expect the interval [begin, begin + duration] to be a subset of the
    # interval represented in features.
    features: Features

    # Supervisions that will be used as targets for model training later on. They don't have to cover the whole
    # cut duration. They also might overlap.
    supervisions: List[SupervisionSegment]

    @property
    def channel(self) -> int:
        return self.features.channel_id

    @property
    def recording_id(self) -> str:
        return self.features.recording_id

    @property
    def end(self) -> Seconds:
        return self.start + self.duration

    @property
    def num_frames(self) -> int:
        return round(self.duration / self.features.frame_shift)

    @property
    def num_features(self) -> int:
        return self.features.num_features

    def load_features(self, root_dir: Optional[Pathlike] = None) -> np.ndarray:
        """
        Load the features from the underlying storage and cut them to the relevant
        [begin, duration] region of the current Cut.
        Optionally specify a `root_dir` prefix to prefix the features path with.
        """
        return self.features.load(root_dir=root_dir, start=self.start, duration=self.duration)

    def load_audio(self, recording_set: RecordingSet, root_dir: Optional[Pathlike] = None) -> np.ndarray:
        """
        Load the audio by locating the appropriate recording in the supplied RecordingSet.
        The audio is trimmed to the [begin, end] range specified by the Cut.
        Optionally specify a `root_dir` prefix to prefix the features path with.

        :param recording_set: RecordingSet object containing the Recording pointed to by recording_id
            member of this Cut.
        :param root_dir: optional Path prefix to find the recording in the filesystem.
        :return: a numpy ndarray with audio samples, with shape (1 <channel>, N <samples>)
        """
        return recording_set.load_audio(
            self.recording_id,
            channels=self.channel,
            offset_seconds=self.start,
            duration_seconds=self.duration,
            root_dir=root_dir
        )

    def truncate(
            self,
            *,
            offset: Seconds = 0.0,
            duration: Optional[Seconds] = None,
            keep_excessive_supervisions: bool = True,
            preserve_id: bool = False
    ) -> 'Cut':
        """
        Returns a new Cut that is a sub-region of the current Cut.

        Note that no operation is done on the actual features - it's only during the call to load_features()
        when the actual changes happen (a subset of features is loaded).

        :param offset: float (seconds), controls the start of the new cut relative to the current Cut's start.
            E.g., if the current Cut starts at 10.0, and offset is 2.0, the new start is 12.0.
        :param duration: optional float (seconds), controls the duration of the resulting Cut.
            By default, the duration is (end of the cut before truncation) - (offset).
        :param keep_excessive_supervisions: bool. Since trimming may happen inside a SupervisionSegment,
            the caller has an option to either keep or discard such supervisions.
        :param preserve_id: bool. Should the truncated cut keep the same ID or get a new, random one.
        :return: a new Cut instance.
        """
        new_start = self.start + offset
        until = offset + (duration if duration is not None else self.duration)
        new_duration = self.duration - new_start if duration is None else until - offset
        assert new_duration > 0.0
        assert new_start + new_duration <= self.start + self.duration + 1e-5
        new_time_span = TimeSpan(start=new_start, end=new_start + new_duration)
        criterion = overlaps if keep_excessive_supervisions else overspans
        return Cut(
            id=self.id if preserve_id else str(uuid4()),
            start=new_start,
            duration=new_duration,
            supervisions=[
                segment for segment in self.supervisions if criterion(new_time_span, segment)
            ],
            features=self.features
        )

    def overlay(self, other: AnyCut, offset_other_by: Seconds = 0.0, snr: Optional[Decibels] = None) -> 'MixedCut':
        """
        Overlay, or mix, this Cut with the `other` Cut. Optionally the `other` Cut may be shifted by `offset_other_by`
        Seconds and scaled down (positive SNR) or scaled up (negative SNR).
        Returns a MixedCut, which only keeps the information about the mix; actual mixing is performed
        during the call to `load_features`.
        """
        assert self.num_features == other.num_features, "Cannot overlay cuts with different feature dimensions."
        assert offset_other_by <= self.duration, f"Cannot overlay cut '{other.id}' with offset {offset_other_by}, " \
                                                 f"which is greater than cuts {self.id} duration of {self.duration}"
        new_tracks = (
            [MixTrack(cut=other, offset=offset_other_by, snr=snr)]
            if isinstance(other, Cut)
            else other.tracks
        )
        return MixedCut(
            id=str(uuid4()),
            tracks=[MixTrack(cut=self)] + new_tracks
        )

    def append(self, other: 'Cut', snr: Optional[Decibels] = None) -> 'MixedCut':
        """
        Append the `other` Cut after the current Cut. Conceptually the same as `overlay` but with an offset
        matching the current cuts length. Optionally scale down (positive SNR) or scale up (negative SNR)
        the `other` cut.
        Returns a MixedCut, which only keeps the information about the mix; actual mixing is performed
        during the call to `load_features`.
        """
        return self.overlay(other=other, offset_other_by=self.duration, snr=snr)

    @staticmethod
    def from_dict(data: dict) -> 'Cut':
        feature_info = data.pop('features')
        supervision_infos = data.pop('supervisions')
        return Cut(
            **data,
            features=Features.from_dict(feature_info),
            supervisions=[SupervisionSegment.from_dict(s) for s in supervision_infos]
        )


@dataclass
class MixTrack:
    """
    Represents a single track in a mix of Cuts. Points to a specific Cut and holds information on
    how to mix it with other Cuts, relative to the first track in a mix.
    """
    cut: Cut
    offset: Seconds = 0.0
    snr: Optional[Decibels] = None

    @staticmethod
    def from_dict(data: dict):
        raw_cut = data.pop('cut')
        return MixTrack(cut=Cut.from_dict(raw_cut), **data)


@dataclass
class MixedCut:
    """
    Represents a Cut that's created from other Cuts via overlay or append operations.
    The actual mixing operations are performed upon loading the features into memory.
    In order to load the features, it needs to access the CutSet object that holds the "ingredient" cuts,
    as it only holds their IDs ("pointers").
    The SNR and offset of all the tracks are specified relative to the first track.
    """
    id: str
    tracks: List[MixTrack]

    @property
    def supervisions(self) -> List[SupervisionSegment]:
        """
        Lists the supervisions of the underlying source cuts.
        Each segment start time will be adjusted by the track offset.
        """
        return [
            segment.with_offset(track.offset)
            for track in self.tracks
            for segment in track.cut.supervisions
        ]

    @property
    def duration(self) -> Seconds:
        track_durations = (track.offset + track.cut.duration for track in self.tracks)
        return max(track_durations)

    @property
    def num_frames(self) -> int:
        return round(self.duration / self.tracks[0].cut.features.frame_shift)

    @property
    def num_features(self) -> int:
        return self.tracks[0].cut.num_features

    def overlay(
            self,
            other: AnyCut,
            offset_other_by: Seconds = 0.0,
            snr: Optional[Decibels] = None
    ) -> 'MixedCut':
        """
        Overlay, or mix, this Cut with the `other` Cut. Optionally the `other` Cut may be shifted by `offset_other_by`
        Seconds and scaled down (positive SNR) or scaled up (negative SNR).
        Returns a MixedCut, which only keeps the information about the mix; actual mixing is performed
        during the call to `load_features`.
        """
        assert self.num_features == other.num_features, "Cannot overlay cuts with different feature dimensions."
        assert offset_other_by <= self.duration, f"Cannot overlay cut '{other.id}' with offset {offset_other_by}, " \
                                                 f"which is greater than cuts {self.id} duration of {self.duration}"
        new_tracks = (
            [MixTrack(cut=other, offset=offset_other_by, snr=snr)]
            if isinstance(other, Cut)
            else other.tracks
        )
        return MixedCut(
            id=str(uuid4()),
            tracks=self.tracks + new_tracks
        )

    def append(self, other: AnyCut, snr: Optional[Decibels] = None) -> 'MixedCut':
        """
        Append the `other` Cut after the current Cut. Conceptually the same as `overlay` but with an offset
        matching the current cuts length. Optionally scale down (positive SNR) or scale up (negative SNR)
        the `other` cut.
        Returns a MixedCut, which only keeps the information about the mix; actual mixing is performed
        during the call to `load_features`.
        """
        return self.overlay(other=other, offset_other_by=self.duration, snr=snr)

    def truncate(
            self,
            *,
            offset: Seconds = 0.0,
            duration: Optional[Seconds] = None,
            keep_excessive_supervisions: bool = True,
            preserve_id: bool = False,
    ) -> 'MixedCut':
        """
        Returns a new MixedCut that is a sub-region of the current MixedCut. This method truncates the underlying Cuts
        and modifies their offsets in the mix, as needed. Tracks that do not fit in the truncated cut are removed.

        Note that no operation is done on the actual features - it's only during the call to load_features()
        when the actual changes happen (a subset of features is loaded).

        :param offset: float (seconds), controls the start of the new cut relative to the current MixedCut's start.
        :param duration: optional float (seconds), controls the duration of the resulting MixedCut.
            By default, the duration is (end of the cut before truncation) - (offset).
        :param keep_excessive_supervisions: bool. Since trimming may happen inside a SupervisionSegment, the caller has
            an option to either keep or discard such supervisions.
        :param preserve_id: bool. Should the truncated cut keep the same ID or get a new, random one.
        :return: a new MixedCut instance.
        """

        new_tracks = []
        old_duration = self.duration
        new_mix_end = old_duration - offset if duration is None else offset + duration

        for track in sorted(self.tracks, key=lambda t: t.offset):
            # First, determine how much of the beginning of the current track we're going to truncate:
            # when the track offset is larger than the truncation offset, we are not truncating the cut;
            # just decreasing the track offset.

            # 'cut_offset' determines how much we're going to truncate the Cut for the current track.
            cut_offset = max(offset - track.offset, 0)
            # 'track_offset' determines the new track's offset after truncation.
            track_offset = max(track.offset - offset, 0)
            # 'track_end' is expressed relative to the beginning of the mix
            # (not to be confused with the 'start' of the underlying Cut)
            track_end = track.offset + track.cut.duration

            if track_end < offset:
                # Omit a Cut that ends before the truncation offset.
                continue

            cut_duration_decrease = 0
            if track_end > new_mix_end:
                if duration is not None:
                    cut_duration_decrease = track_end - new_mix_end
                else:
                    cut_duration_decrease = track_end - old_duration

            # Compute the new Cut's duration after trimming the start and the end.
            new_duration = track.cut.duration - cut_offset - cut_duration_decrease
            if new_duration <= 0:
                # Omit a Cut that is completely outside the time span of the new truncated MixedCut.
                continue

            new_tracks.append(
                MixTrack(
                    cut=track.cut.truncate(
                        offset=cut_offset,
                        duration=new_duration,
                        keep_excessive_supervisions=keep_excessive_supervisions,
                        preserve_id=preserve_id
                    ),
                    offset=track_offset,
                    snr=track.snr
                )
            )
        return MixedCut(id=str(uuid4()), tracks=new_tracks)

    def load_features(self, root_dir: Optional[Pathlike] = None) -> np.ndarray:
        """Loads the features of the source cuts and overlays them on-the-fly."""
        cuts = [track.cut for track in self.tracks]
        frame_shift = cuts[0].features.frame_shift
        # TODO: check if the 'pad_shorter' call is still necessary, it shouldn't be
        # feats = pad_shorter(*feats)
        mixer = FbankMixer(
            base_feats=cuts[0].load_features(root_dir=root_dir),
            frame_shift=frame_shift,
        )
        for cut, track in zip(cuts[1:], self.tracks[1:]):
            mixer.add_to_mix(
                feats=cut.load_features(root_dir=root_dir),
                snr=track.snr,
                offset=track.offset
            )
        return mixer.mixed_feats

    @staticmethod
    def from_dict(data: dict) -> 'MixedCut':
        return MixedCut(id=data['id'], tracks=[MixTrack.from_dict(track) for track in data['tracks']])


@dataclass
class CutSet:
    """
    CutSet combines features with their corresponding supervisions.
    It may have wider span than the actual supervisions, provided the features for the whole span exist.
    It is the basic building block of PyTorch-style Datasets for speech/audio processing tasks.
    """
    cuts: Dict[str, AnyCut]

    @property
    def mixed_cuts(self) -> Dict[str, MixedCut]:
        return {id_: cut for id_, cut in self.cuts.items() if isinstance(cut, MixedCut)}

    @property
    def simple_cuts(self) -> Dict[str, Cut]:
        return {id_: cut for id_, cut in self.cuts.items() if isinstance(cut, Cut)}

    @staticmethod
    def from_cuts(cuts: Iterable[AnyCut]) -> 'CutSet':
        return CutSet({cut.id: cut for cut in cuts})

    @staticmethod
    def from_yaml(path: Pathlike) -> 'CutSet':
        raw_cuts = load_yaml(path)

        def deserialize_one(raw_cut: dict) -> AnyCut:
            cut_type = raw_cut.pop('type')
            if cut_type == 'Cut':
                return Cut.from_dict(raw_cut)
            if cut_type == 'MixedCut':
                return MixedCut.from_dict(raw_cut)
            raise ValueError(f"Unexpected cut type during deserialization: '{cut_type}'")

        return CutSet.from_cuts(deserialize_one(cut) for cut in raw_cuts)

    def to_yaml(self, path: Pathlike):
        data = [{**asdict_nonull(cut), 'type': type(cut).__name__} for cut in self]
        save_to_yaml(data, path)

    def truncate(
            self,
            max_duration: Seconds,
            offset_type: str,
            keep_excessive_supervisions: bool = True,
            preserve_id: bool = False
    ) -> 'CutSet':
        """
        Return a new CutSet with the Cuts truncated so that their durations are at most `max_duration`.
        Cuts shorter than `max_duration` will not be changed.
        :param max_duration: float, the maximum duration in seconds of a cut in the resulting manifest.
        :param offset_type: str, can be:
            - 'start' => cuts are truncated from their start;
            - 'end' => cuts are truncated from their end minus max_duration;
            - 'random' => cuts are truncated randomly between their start and their end minus max_duration
        :param keep_excessive_supervisions: bool. When a cut is truncated in the middle of a supervision segment,
            should the supervision be kept.
        :param preserve_id: bool. Should the truncated cut keep the same ID or get a new, random one.
        :return: a new CutSet instance with truncated cuts.
        """
        truncated_cuts = []
        for cut in self:
            if cut.duration <= max_duration:
                truncated_cuts.append(cut)
                continue

            def compute_offset():
                if offset_type == 'start':
                    return 0.0
                last_offset = cut.duration - max_duration
                if offset_type == 'end':
                    return last_offset
                if offset_type == 'random':
                    return random.uniform(0.0, last_offset)
                raise ValueError(f"Unknown 'offset_type' option: {offset_type}")

            truncated_cuts.append(cut.truncate(
                offset=compute_offset(),
                duration=max_duration,
                keep_excessive_supervisions=keep_excessive_supervisions,
                preserve_id=preserve_id
            ))
        return CutSet.from_cuts(truncated_cuts)

    def __contains__(self, item: Union[str, Cut, MixedCut]) -> bool:
        if isinstance(item, str):
            return item in self.cuts
        else:
            return item.id in self.cuts

    def __getitem__(self, item: str) -> 'AnyCut':
        return self.cuts[item]

    def __len__(self) -> int:
        return len(self.cuts)

    def __iter__(self) -> Iterable[AnyCut]:
        return iter(self.cuts.values())

    def __add__(self, other: 'CutSet') -> 'CutSet':
        assert not set(self.cuts.keys()).intersection(other.cuts.keys()), "Conflicting IDs when concatenating CutSets!"
        return CutSet(cuts={**self.cuts, **other.cuts})


def make_cuts_from_features(feature_set: FeatureSet) -> CutSet:
    """
    Utility that converts a FeatureSet to a CutSet without any adjustment of the segment boundaries.
    """
    return CutSet.from_cuts(
        Cut(
            id=str(uuid4()),
            start=features.start,
            duration=features.duration,
            features=features,
            supervisions=[]
        )
        for features in feature_set
    )


def make_windowed_cuts_from_features(
        feature_set: FeatureSet,
        cut_duration: Seconds,
        cut_shift: Optional[Seconds] = None,
        keep_shorter_windows: bool = False
) -> CutSet:
    """
    Converts a FeatureSet to a CutSet by traversing each Features object in - possibly overlapping - windows, and
    creating a Cut out of that area. By default, the last window in traversal will be discarded if it cannot satisfy
    the `cut_duration` requirement.

    :param feature_set: a FeatureSet object.
    :param cut_duration: float, duration of created Cuts in seconds.
    :param cut_shift: optional float, specifies how many seconds are in between the starts of consecutive windows.
        Equals `cut_duration` by default.
    :param keep_shorter_windows: bool, when True, the last window will be used to create a Cut even if
        its duration is shorter than `cut_duration`.
    :return: a CutSet object.
    """
    if cut_shift is None:
        cut_shift = cut_duration
    round_fn = ceil if keep_shorter_windows else floor
    cuts = []
    for features in feature_set:
        # Determine the number of cuts, depending on `keep_shorter_windows` argument.
        # When its true, we'll want to include the residuals in the output; otherwise,
        # we provide only full duration cuts.
        n_cuts = round_fn(features.duration / cut_shift)
        if (n_cuts - 1) * cut_shift + cut_duration > features.duration and not keep_shorter_windows:
            n_cuts -= 1
        for idx in range(n_cuts):
            offset = features.start + idx * cut_shift
            duration = min(cut_duration, features.end - offset)
            cuts.append(
                Cut(
                    id=str(uuid4()),
                    start=offset,
                    duration=duration,
                    features=features,
                    supervisions=[]
                )
            )
    return CutSet.from_cuts(cuts)


def make_cuts_from_supervisions(supervision_set: SupervisionSet, feature_set: FeatureSet) -> CutSet:
    """
    Utility that converts a SupervisionSet to a CutSet without any adjustment of the segment boundaries.
    It attaches the relevant features from the corresponding FeatureSet.
    """
    return CutSet.from_cuts(
        Cut(
            id=str(uuid4()),
            start=supervision.start,
            duration=supervision.duration,
            features=feature_set.find(
                recording_id=supervision.recording_id,
                channel_id=supervision.channel_id,
                start=supervision.start,
                duration=supervision.duration,
            ),
            supervisions=[supervision]
        )
        for idx, supervision in enumerate(supervision_set)
    )


def mix(
        left_cut: AnyCut,
        right_cut: AnyCut,
        offset_right_by: Seconds = 0,
        snr: Optional[Decibels] = None
) -> MixedCut:
    """Helper method for functional-style mixing of Cuts."""
    return left_cut.overlay(right_cut, offset_other_by=offset_right_by, snr=snr)


def append(
        left_cut: AnyCut,
        right_cut: AnyCut,
        snr: Optional[Decibels] = None
) -> MixedCut:
    """Helper method for functional-style appending of Cuts."""
    return left_cut.append(right_cut, snr=snr)


def mix_cuts(cuts: Iterable[AnyCut]) -> MixedCut:
    """Return a MixedCut that consists of the input Cuts overlayed on each other as-is."""
    # The following is a fold (accumulate/aggregate) operation; it starts with cuts[0], and overlays it with cuts[1];
    #  then takes their mix and overlays it with cuts[2]; and so on.
    return reduce(mix, cuts)


def append_cuts(cuts: Iterable[AnyCut]) -> AnyCut:
    """Return a MixedCut that consists of the input Cuts appended to each other as-is."""
    # The following is a fold (accumulate/aggregate) operation; it starts with cuts[0], and appends cuts[1] to it;
    #  then takes their it concatenation and appends cuts[2] to it; and so on.
    return reduce(append, cuts)
