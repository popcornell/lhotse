import click

from lhotse.bin.modes import recipe
from lhotse.recipes.librimix import prepare_librimix
from lhotse.utils import Pathlike

__all__ = ['librimix']


@recipe.command(context_settings=dict(show_default=True))
@click.argument('librimix-csv', type=click.Path(exists=True, dir_okay=False))
@click.argument('output_dir', type=click.Path())
@click.option('--sampling-rate', type=int, default=16000, help='Sampling rate to set in the RecordingSet manifest.')
@click.option(
    '--min-segment-seconds', type=float, default=3.0,
    help='Remove segments shorter than MIN_SEGMENT_SECONDS.'
)
@click.option(
    '--with-precomputed-mixtures/--no-precomputed-mixtures', type=bool, default=False,
    help='Optionally create an RecordingSet manifest including the precomputed LibriMix mixtures.'
)
def librimix(
        librimix_csv: Pathlike,
        output_dir: Pathlike,
        sampling_rate: int,
        min_segment_seconds: float,
        with_precomputed_mixtures: bool
):
    """Recipe to prepare the manifests for LibrMix source separation task."""
    prepare_librimix(
        librimix_csv=librimix_csv,
        output_dir=output_dir,
        sampling_rate=sampling_rate,
        min_segment_seconds=min_segment_seconds,
        with_precomputed_mixtures=with_precomputed_mixtures
    )
