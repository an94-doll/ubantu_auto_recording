from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import av
import numpy as np

from slicing_recorder.encoder import Encoder, EncoderError
from slicing_recorder.models import CameraInfo, Frame


def info() -> CameraInfo:
    return CameraInfo("synthetic", "TEST", 64, 48, "RGB8", 25.0)


def frame(index: int, stamp_ns: int) -> Frame:
    return Frame(np.full((48, 64, 3), index * 20, dtype=np.uint8), index,
                 stamp_ns, datetime.now(timezone.utc))


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "实验一次.mkv"

    def test_h265_decode_frame_count_and_timestamp_gap(self):
        stamps = [1_000_000_000, 1_040_000_000, 1_160_000_000, 1_200_000_000]
        with Encoder(self.path, info(), "libx265") as enc:
            for i, stamp in enumerate(stamps):
                enc.write(frame(i, stamp))
            self.assertEqual(enc.frames_written, 4)
            self.assertEqual(enc.codec, "libx265")
        with av.open(str(self.path)) as video:
            self.assertEqual(video.streams.video[0].codec_context.name, "hevc")
            decoded = list(video.decode(video=0))
        self.assertEqual(len(decoded), 4)
        self.assertEqual([(f.width, f.height) for f in decoded], [(64, 48)] * 4)
        for actual, expected in zip(decoded, [0, .04, .16, .20]):
            self.assertAlmostEqual(float(actual.pts * actual.time_base), expected, places=3)
        self.assertGreater(self.path.stat().st_size, 0)

    def test_reject_duplicate_and_regressing_timestamps(self):
        with Encoder(self.path, info(), "libx265") as enc:
            enc.write(frame(0, 100_000))
            for stamp in [100_000, 99_000, 100_500]:
                with self.assertRaises(EncoderError):
                    enc.write(frame(1, stamp))
            enc.write(frame(2, 200_000))

    def test_reject_clock_source_or_resolution_change(self):
        with Encoder(self.path, info(), "libx265") as enc:
            enc.write(frame(0, 100_000))
            wrong_clock = frame(1, 200_000)
            wrong_clock.timestamp_source = "camera_device"
            with self.assertRaises(EncoderError):
                enc.write(wrong_clock)
            wrong_size = frame(2, 300_000)
            wrong_size.rgb = wrong_size.rgb[:20]
            with self.assertRaises(EncoderError):
                enc.write(wrong_size)

    def test_codec_probe_failure_does_not_create_recording_file(self):
        with patch("slicing_recorder.encoder._probe", side_effect=RuntimeError("no device")):
            with self.assertRaisesRegex(EncoderError, "no device"):
                Encoder(self.path, info(), "auto")
        self.assertFalse(self.path.exists())

    def test_auto_fallback_is_explicit_and_h265_only(self):
        import slicing_recorder.encoder as module
        real_probe = module._probe

        def probe(camera, codec, quality):
            if codec != "libx265":
                raise RuntimeError("hardware unavailable")
            return real_probe(camera, codec, quality)

        with patch("slicing_recorder.encoder._probe", side_effect=probe):
            with Encoder(self.path, info(), "auto") as enc:
                self.assertEqual(enc.codec, "libx265")
                self.assertEqual(len(enc.fallback_reasons), 3)
                enc.write(frame(0, 0))

    def test_explicit_codec_does_not_fallback(self):
        with patch("slicing_recorder.encoder._probe", side_effect=RuntimeError("hardware")) as probe:
            with self.assertRaises(EncoderError):
                Encoder(self.path, info(), "hevc_nvenc")
        self.assertEqual(probe.call_count, 1)

    def test_no_overwrite_and_close_idempotence(self):
        self.path.write_text("existing", encoding="utf-8")
        with self.assertRaises(EncoderError):
            Encoder(self.path, info(), "libx265")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "existing")
        self.path.unlink()
        enc = Encoder(self.path, info(), "libx265")
        enc.write(frame(0, 0))
        enc.close()
        enc.close()
        with self.assertRaises(EncoderError):
            enc.write(frame(1, 1_000_000))

    def test_close_error_still_releases_file(self):
        enc = Encoder(self.path, info(), "libx265")
        original = enc._stream
        class Broken:
            def encode(self, value):
                raise OSError("disk full")
        enc._stream = Broken()
        with self.assertRaisesRegex(EncoderError, "disk full"):
            enc.close()
        self.assertTrue(enc._closed)
        self.assertIsNone(enc._file)
        self.assertIsNone(enc._container)
        del original

    def test_invalid_geometry_and_codec(self):
        camera = info()
        camera.width = 63
        with self.assertRaises(EncoderError):
            Encoder(self.path, camera)
        with self.assertRaises(EncoderError):
            Encoder(self.path, info(), "libx264")


if __name__ == "__main__":
    unittest.main()
