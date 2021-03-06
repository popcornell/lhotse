from typing import Tuple, Optional, List

import click
import numpy as np
from cytoolz.itertoolz import groupby

from lhotse.bin.modes.cli_base import cli
from lhotse.cut import (
    CutSet,
    make_cuts_from_features,
    make_cuts_from_supervisions,
    make_windowed_cuts_from_features,
    append_cuts,
    mix_cuts,
)
from lhotse.features import FeatureSet
from lhotse.manipulation import split, combine
from lhotse.supervision import SupervisionSet
from lhotse.utils import Pathlike, fix_random_seed


@cli.group()
def cut():
    """Group of commands used to create CutSets."""
    pass


@cut.command()
@click.argument('feature_manifest', type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
@click.option('-s', '--supervision_manifest', type=click.Path(exists=True, dir_okay=False),
              help='Optional supervision manifest - will be used to attach the supervisions to the cuts.')
def simple(
        feature_manifest: Pathlike,
        output_cut_manifest: Pathlike,
        supervision_manifest: Optional[Pathlike],
):
    """
    Create a CutSet stored in OUTPUT_CUT_MANIFEST that contains the regions and features supplied by FEATURE_MANIFEST.
    Optionally it can use a SUPERVISION_MANIFEST to select the regions and attach the corresponding supervisions to
    the cuts. This is the simplest way to create Cuts.
    """
    feature_set = FeatureSet.from_yaml(feature_manifest)
    if supervision_manifest is None:
        cut_set = make_cuts_from_features(feature_set)
    else:
        supervision_set = SupervisionSet.from_yaml(supervision_manifest)
        cut_set = make_cuts_from_supervisions(feature_set=feature_set, supervision_set=supervision_set)
    cut_set.to_yaml(output_cut_manifest)


@cut.command()
@click.argument('feature_manifest', type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
@click.option('-d', '--cut-duration', type=float, default=5.0, help='How long should the cuts be in seconds.')
@click.option('-s', '--cut-shift', type=float, default=None,
              help='How much to shift the cutting window in seconds (by default the shift is equal to CUT_DURATION).')
@click.option('--keep-shorter-windows/--discard-shorter-windows', type=bool, default=False,
              help='When true, the last window will be used to create a Cut even if its duration is '
                   'shorter than CUT_DURATION.')
def windowed(
        feature_manifest: Pathlike,
        output_cut_manifest: Pathlike,
        cut_duration: float,
        cut_shift: Optional[float],
        keep_shorter_windows: bool
):
    """
    Create a CutSet stored in OUTPUT_CUT_MANIFEST from feature regions in FEATURE_MANIFEST.
    The feature matrices are traversed in windows with CUT_SHIFT increments, creating cuts of constant CUT_DURATION.
    """
    feature_set = FeatureSet.from_yaml(feature_manifest)
    cut_set = make_windowed_cuts_from_features(
        feature_set=feature_set,
        cut_duration=cut_duration,
        cut_shift=cut_shift,
        keep_shorter_windows=keep_shorter_windows
    )
    cut_set.to_yaml(output_cut_manifest)


@cut.command()
@click.argument('supervision_manifest', type=click.Path(exists=True, dir_okay=False))
@click.argument('feature_manifest', type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
@click.option('-r', '--random-seed', default=42, type=int, help='Random seed value.')
@click.option('-s', '--snr-range', type=(float, float), default=(20, 20),
              help='Range of SNR values (in dB) that will be uniformly sampled in order to overlay the signals.')
@click.option('-o', '--offset-range', type=(float, float), default=(0.5, 0.5),
              help='Range of relative offset values (0 - 1), which will offset the "right" signal by this many times '
                   'the duration of the "left" signal. It is uniformly sampled for each overlay operation.')
def random_overlayed(
        supervision_manifest: Pathlike,
        feature_manifest: Pathlike,
        output_cut_manifest: Pathlike,
        random_seed: int,
        snr_range: Tuple[float, float],
        offset_range: Tuple[float, float]
):
    """
    Create a CutSet stored in OUTPUT_CUT_MANIFEST that contains supervision regions from SUPERVISION_MANIFEST
    and features supplied by FEATURE_MANIFEST. It first creates a trivial CutSet, splits it into two equal, randomized
    parts and overlays their features to create a mix.
    The parameters of the mix are controlled via SNR_RANGE and OFFSET_RANGE.
    """
    fix_random_seed(random_seed)

    supervision_set = SupervisionSet.from_yaml(supervision_manifest)
    feature_set = FeatureSet.from_yaml(feature_manifest)

    source_cut_set = make_cuts_from_supervisions(supervision_set=supervision_set, feature_set=feature_set)
    left_cuts, right_cuts = split(source_cut_set, num_splits=2, randomize=True)

    snrs = np.random.uniform(*snr_range, size=len(left_cuts)).tolist()
    relative_offsets = np.random.uniform(*offset_range, size=len(left_cuts)).tolist()

    overlayed_cut_set = CutSet.from_cuts(
        left_cut.overlay(
            right_cut,
            offset_other_by=left_cut.duration * relative_offset,
            snr=snr
        )
        for left_cut, right_cut, snr, relative_offset in zip(left_cuts, right_cuts, snrs, relative_offsets)
    )
    overlayed_cut_set.to_yaml(output_cut_manifest)


@cut.command()
@click.argument('cut_manifests', nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
def mix_sequential(
        cut_manifests: List[Pathlike],
        output_cut_manifest: Pathlike
):
    """
    Create a CutSet stored in OUTPUT_CUT_MANIFEST by iterating jointly over CUT_MANIFESTS and mixing the Cuts
    on the same positions. E.g. the first output cut is created from the first cuts in each input manifest.
    The mix is performed by summing the features from all Cuts.
    If the CUT_MANIFESTS have different number of Cuts, the mixing ends when the shorter manifest is depleted.
    """
    cut_manifests = [CutSet.from_yaml(path) for path in cut_manifests]
    mixed_cut_set = CutSet.from_cuts(mix_cuts(cuts) for cuts in zip(*cut_manifests))
    mixed_cut_set.to_yaml(output_cut_manifest)


@cut.command()
@click.argument('cut_manifests', nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
def mix_by_recording_id(
        cut_manifests: List[Pathlike],
        output_cut_manifest: Pathlike
):
    """
    Create a CutSet stored in OUTPUT_CUT_MANIFEST by matching the Cuts from CUT_MANIFESTS by their recording IDs
    and mixing them together.
    """
    all_cuts = combine(*[CutSet.from_yaml(path) for path in cut_manifests])
    recording_id_to_cuts = groupby(lambda cut: cut.recording_id, all_cuts)
    mixed_cut_set = CutSet.from_cuts(mix_cuts(cuts) for recording_id, cuts in recording_id_to_cuts.items())
    mixed_cut_set.to_yaml(output_cut_manifest)


@cut.command(context_settings=dict(show_default=True))
@click.argument('cut_manifest', type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
@click.option('--preserve-id', is_flag=True,
              help='Should the cuts preserve IDs (by default, they will get new, random IDs)')
@click.option('-d', '--max-duration', type=float, required=True,
              help='The maximum duration in seconds of a cut in the resulting manifest.')
@click.option('-o', '--offset-type', type=click.Choice(['start', 'end', 'random']), default='start',
              help='Where should the truncated cut start: "start" - at the start of the original cut, '
                   '"end" - MAX_DURATION before the end of the original cut, '
                   '"random" - randomly choose somewhere between "start" and "end" options.')
@click.option('--keep-overflowing-supervisions/--discard-overflowing-supervisions', type=bool, default=False,
              help='When a cut is truncated in the middle of a supervision segment, should the supervision be kept.')
@click.option('-r', '--random-seed', default=42, type=int, help='Random seed value.')
def truncate(
        cut_manifest: Pathlike,
        output_cut_manifest: Pathlike,
        preserve_id: bool,
        max_duration: float,
        offset_type: str,
        keep_overflowing_supervisions: bool,
        random_seed: int
):
    """
    Truncate the cuts in the CUT_MANIFEST and write them to OUTPUT_CUT_MANIFEST.
    Cuts shorter than MAX_DURATION will not be modified.
    """
    fix_random_seed(random_seed)
    cut_set = CutSet.from_yaml(cut_manifest)
    truncated_cut_set = cut_set.truncate(
        max_duration=max_duration,
        offset_type=offset_type,
        keep_excessive_supervisions=keep_overflowing_supervisions,
        preserve_id=preserve_id
    )
    truncated_cut_set.to_yaml(output_cut_manifest)


@cut.command()
@click.argument('cut_manifests', nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.argument('output_cut_manifest', type=click.Path())
def append(
        cut_manifests: List[Pathlike],
        output_cut_manifest: Pathlike,
):
    """
    Create a new CutSet by appending the cuts in CUT_MANIFESTS. CUT_MANIFESTS are iterated position-wise (the
    cuts on i'th position in each manfiest are appended to each other).
    The cuts are appended in the order in which they appear in the
    input argument list.
    If CUT_MANIFESTS have different lengths, the script stops once the shortest CutSet is depleted.
    """
    cut_sets = [CutSet.from_yaml(path) for path in cut_manifests]
    appended_cut_set = CutSet.from_cuts(append_cuts(cuts) for cuts in zip(*cut_sets))
    appended_cut_set.to_yaml(output_cut_manifest)
