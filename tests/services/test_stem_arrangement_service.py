import io
import shutil
import uuid

import pytest
import soundfile as sf

from app.services.stem_arrangement_service import timeline
from app.utils.ffmpeg_helpers import concat_wavs, silent_wav, wav_duration_seconds

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class _Item:
    def __init__(self, part_id, position, repeat_count):
        self.part_id = part_id
        self.position = position
        self.repeat_count = repeat_count


def test_timeline_expands_repeats_in_position_order():
    intro, chorus, verse = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Deliberately out of order — the running order comes from `position`, not
    # from however the rows happened to come back.
    items = [
        _Item(chorus, 2, 3),
        _Item(intro, 0, 1),
        _Item(verse, 1, 1),
    ]

    assert timeline(items) == [intro, verse, chorus, chorus, chorus]


def test_timeline_treats_a_zero_repeat_as_one_play():
    part = uuid.uuid4()
    assert timeline([_Item(part, 0, 0)]) == [part]


def _tone_wav(seconds: float, sample_rate: int = 44100) -> bytes:
    import numpy as np

    frames = int(seconds * sample_rate)
    audio = np.zeros((frames, 2), dtype="float32")
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@needs_ffmpeg
def test_silent_wav_has_the_requested_length():
    assert wav_duration_seconds(silent_wav(1.5)) == pytest.approx(1.5, abs=0.05)


@needs_ffmpeg
def test_concat_wavs_adds_the_segments_up():
    joined = concat_wavs([_tone_wav(1.0), _tone_wav(0.5), _tone_wav(2.0)])
    assert wav_duration_seconds(joined) == pytest.approx(3.5, abs=0.1)


@needs_ffmpeg
def test_concat_wavs_resamples_mismatched_segments():
    # Parts are independent uploads and may not share a sample rate; the render
    # has to join them anyway rather than refuse the pack.
    joined = concat_wavs([_tone_wav(1.0, 44100), _tone_wav(1.0, 48000)])
    assert wav_duration_seconds(joined) == pytest.approx(2.0, abs=0.1)


@needs_ffmpeg
def test_silence_stands_in_for_an_absent_instrument():
    # A guitar that sits out the intro still has to occupy the intro's length,
    # or every later part in its track lands early.
    intro_length = wav_duration_seconds(_tone_wav(2.0))
    track = concat_wavs([silent_wav(intro_length), _tone_wav(1.0)])
    assert wav_duration_seconds(track) == pytest.approx(3.0, abs=0.1)
