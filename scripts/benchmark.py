"""Measure synthetic RGB -> H.265/MKV throughput, including encoder drain.

This isolates encoding. It does not certify USB capture or Bayer conversion.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Also runnable directly from a checkout, without editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av
import numpy as np

from slicing_recorder.encoder import CODECS, Encoder
from slicing_recorder.models import CameraInfo, Frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=2048)
    parser.add_argument("--height", type=int, default=1536)
    parser.add_argument("--fps", type=float, default=104.8,
                        help="待验证的目标帧率；默认值只是设计预算，不是设备规格确认")
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--codec", choices=CODECS, default="auto")
    parser.add_argument("--quality", type=int, default=23)
    parser.add_argument("--output", type=Path, default=Path("benchmark-output"))
    args = parser.parse_args()
    if args.frames < 2 or args.samples < 1:
        parser.error("frames >= 2 and samples >= 1 are required")
    args.output.mkdir(parents=True, exist_ok=True)
    info = CameraInfo("synthetic RGB benchmark", "SYNTHETIC", args.width,
                      args.height, "RGB8", args.fps)
    # Fixed seed, spatial detail and changing content. Reuse bounded storage;
    # native encoder allocations are not represented by Python tracemalloc.
    rng = np.random.default_rng(42)
    picture = rng.integers(0, 256, (args.height, args.width, 3), dtype=np.uint8)
    report = {"python": sys.version.split()[0], "platform": platform.platform(),
              "processor": platform.processor(), "pyav": av.__version__,
              "numpy": np.__version__, "ffmpeg_libraries": av.library_versions,
              "width": args.width, "height": args.height,
              "target_fps": args.fps, "frames": args.frames,
              "quality": args.quality, "seed": 42, "samples": []}
    wall_start = datetime.now(timezone.utc)
    run_id = wall_start.strftime("%Y%m%d_%H%M%S_%f")
    for sample in range(args.samples):
        output = args.output / f"{run_id}_{sample + 1}.mkv"
        setup_start = time.perf_counter()
        enc = Encoder(output, info, args.codec, args.quality)
        setup_s = time.perf_counter() - setup_start
        started = time.perf_counter()
        try:
            for i in range(args.frames):
                # Ufunc updates in native code; no per-pixel Python loop.
                np.add(picture, np.uint8(1), out=picture)
                stamp_ns = round(i * 1e9 / args.fps)
                enc.write(Frame(picture, i, stamp_ns,
                                wall_start + timedelta(seconds=i / args.fps)))
        finally:
            enc.close()
        encode_s = time.perf_counter() - started
        with av.open(str(output)) as decoded:
            frames = 0
            first = last = None
            for image in decoded.decode(video=0):
                timestamp = float(image.pts * image.time_base)
                first = timestamp if first is None else first
                last = timestamp
                frames += 1
        sample_report = {
            "file": str(output.resolve()), "encoder": enc.codec,
            "fallback_reasons": enc.fallback_reasons,
            "setup_probe_seconds": setup_s, "encode_flush_seconds": encode_s,
            "throughput_fps": args.frames / encode_s,
            "target_realtime_ratio": args.frames / encode_s / args.fps,
            "decoded_frames": frames, "decoded_span_seconds": last - first,
            "expected_span_seconds": (args.frames - 1) / args.fps,
            "file_size_bytes": output.stat().st_size,
        }
        if frames != args.frames or abs((last - first) - (args.frames - 1) / args.fps) > .002:
            raise RuntimeError(f"编码内容验证失败：{sample_report}")
        report["samples"].append(sample_report)
    rates = [s["throughput_fps"] for s in report["samples"]]
    report["throughput_fps_median"] = statistics.median(rates)
    report["throughput_fps_min"] = min(rates)
    report["throughput_fps_max"] = max(rates)
    report["scope"] = "synthetic RGB encoding and flush; excludes USB/SDK/Bayer/GUI"
    report_path = args.output / f"{run_id}_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
