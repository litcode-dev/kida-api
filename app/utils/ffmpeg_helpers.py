import subprocess
import os
import tempfile
from pathlib import Path


def generate_preview_mp3(wav_bytes: bytes, duration_seconds: int = 15) -> bytes:
    """
    Cut the first duration_seconds of a WAV and encode to MP3 using ffmpeg.
    Returns MP3 bytes.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        tmp_in.write(wav_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = str(Path(tmp_in_path).with_suffix("")) + "_preview.mp3"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", tmp_in_path,
                "-t", str(duration_seconds),
                "-q:a", "2",
                "-vn",
                tmp_out_path,
            ],
            check=True,
            capture_output=True,
        )
        with open(tmp_out_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_in_path):
            os.unlink(tmp_in_path)
        if os.path.exists(tmp_out_path):
            os.unlink(tmp_out_path)


def trim_wav_to_duration(wav_bytes: bytes, max_seconds: int = 60) -> bytes:
    """
    If the WAV is longer than max_seconds, trim it. Returns WAV bytes.
    If shorter or equal, returns the original bytes unchanged.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        tmp_in.write(wav_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = str(Path(tmp_in_path).with_suffix("")) + "_trimmed.wav"
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", tmp_in_path],
            capture_output=True, text=True,
        )
        duration = float(result.stdout.strip() or 0)
        if duration <= max_seconds:
            return wav_bytes

        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in_path, "-t", str(max_seconds), "-c", "copy", tmp_out_path],
            check=True,
            capture_output=True,
        )
        with open(tmp_out_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_in_path):
            os.unlink(tmp_in_path)
        if os.path.exists(tmp_out_path):
            os.unlink(tmp_out_path)


def wav_duration_seconds(wav_bytes: bytes) -> float:
    """Exact duration of a WAV, as a float.

    Arrangement rendering lines parts up end to end, so the whole-second
    ``duration`` columns are too coarse here — a tenth of a second dropped from
    every part walks the click track off the beat by the end of a song.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        tmp_in.write(wav_bytes)
        tmp_in_path = tmp_in.name
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", tmp_in_path],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip() or 0)
    finally:
        if os.path.exists(tmp_in_path):
            os.unlink(tmp_in_path)


def silent_wav(duration_seconds: float, sample_rate: int = 44100, channels: int = 2) -> bytes:
    """A silent WAV of a given length.

    Stands in for an instrument that sits out a part: the guitar being silent
    through the intro still has to occupy the intro's worth of time, or every
    later part in that instrument's rendered track lands early.
    """
    layout = "stereo" if channels == 2 else "mono"
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
        tmp_out_path = tmp_out.name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r={sample_rate}:cl={layout}",
                "-t", f"{max(duration_seconds, 0.001):.6f}",
                "-c:a", "pcm_s16le",
                tmp_out_path,
            ],
            check=True,
            capture_output=True,
        )
        with open(tmp_out_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_out_path):
            os.unlink(tmp_out_path)


def concat_wavs(
    wav_blobs: list[bytes], sample_rate: int = 44100, channels: int = 2
) -> bytes:
    """Join WAVs end to end into one WAV.

    Uses ffmpeg's concat *filter* rather than the concat demuxer because the
    segments are independent uploads: they may differ in sample rate, bit depth
    or channel count, which the demuxer refuses and the filter resamples.
    """
    if not wav_blobs:
        raise ValueError("concat_wavs needs at least one segment")

    tmp_paths: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
        tmp_out_path = tmp_out.name
    try:
        for blob in wav_blobs:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
                tmp_in.write(blob)
                tmp_paths.append(tmp_in.name)

        cmd = ["ffmpeg", "-y"]
        for path in tmp_paths:
            cmd += ["-i", path]
        filter_inputs = "".join(f"[{i}:a]" for i in range(len(tmp_paths)))
        cmd += [
            "-filter_complex", f"{filter_inputs}concat=n={len(tmp_paths)}:v=0:a=1[out]",
            "-map", "[out]",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-c:a", "pcm_s16le",
            tmp_out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        with open(tmp_out_path, "rb") as f:
            return f.read()
    finally:
        for path in tmp_paths + [tmp_out_path]:
            if os.path.exists(path):
                os.unlink(path)


def convert_mp3_to_wav(mp3_bytes: bytes) -> bytes:
    """Convert MP3 bytes to WAV bytes using ffmpeg. Used for Suno-generated audio."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_in:
        tmp_in.write(mp3_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = str(Path(tmp_in_path).with_suffix("")) + "_converted.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in_path, "-ar", "44100", "-ac", "2", tmp_out_path],
            check=True,
            capture_output=True,
        )
        with open(tmp_out_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_in_path):
            os.unlink(tmp_in_path)
        if os.path.exists(tmp_out_path):
            os.unlink(tmp_out_path)
