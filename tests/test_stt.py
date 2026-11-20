import argparse
import os
import sys
import time
import wave
import numpy as np

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from faster_whisper import WhisperModel

AUDIO_DIR = "audio_utterances"
ESTIMATED_NOISE_FLOOR = 350.0  # INMP441 nominal ambient noise floor on hardware


def analyze_audio_quality(filename: str) -> dict:
    """Computes comprehensive signal statistics to measure mic gain, SNR, DC bias, and clipping."""
    with wave.open(filename, 'rb') as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_bytes = wf.readframes(n_frames)

    duration = n_frames / framerate
    samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)

    if len(samples) == 0:
        return {
            "duration": 0, "peak": 0, "rms": 0, "dc_bias": 0,
            "clipping_ratio": 0, "snr_db": 0, "framerate": framerate
        }

    dc_bias = float(np.mean(samples))
    zero_centered = samples - dc_bias
    peak = float(np.max(np.abs(zero_centered)))
    rms = float(np.sqrt(np.mean(zero_centered**2)))

    # Check if signal is hitting int16 limits (clipping)
    clipping_samples = np.sum(np.abs(samples) >= 32700)
    clipping_ratio = (clipping_samples / len(samples)) * 100.0

    # Signal-to-Noise Ratio (SNR) in dB relative to baseline ambient floor
    snr_db = 20.0 * np.log10(max(1.0, rms) / max(1.0, ESTIMATED_NOISE_FLOOR))

    return {
        "duration": duration,
        "peak": peak,
        "rms": rms,
        "dc_bias": dc_bias,
        "clipping_ratio": clipping_ratio,
        "snr_db": snr_db,
        "framerate": framerate,
    }


def run_benchmark(sample_count: int = 10, run_all: bool = False):
    if not os.path.exists(AUDIO_DIR):
        print(f"❌ Directory '{AUDIO_DIR}' not found.")
        return

    wav_files = sorted(
        [os.path.join(AUDIO_DIR, f) for f in os.listdir(AUDIO_DIR) if f.endswith(".wav")],
        key=os.path.getmtime
    )

    if not wav_files:
        print(f"⚠️ No .wav files found in '{AUDIO_DIR}'. Speak into your glasses first!")
        return

    target_files = wav_files if run_all else wav_files[-sample_count:]

    print("=" * 80)
    print("🚀 NextSight AI Glasses — Audio Input & STT Calibration Benchmark")
    print("=" * 80)
    print(f"📂 Dataset: {len(wav_files)} total recordings found in '{AUDIO_DIR}'")
    print(f"🧪 Testing: {len(target_files)} recordings\n")

    print("⏳ Loading Faster-Whisper 'base.en' (CPU int8, 6 threads, 2 workers)...")
    t_load = time.perf_counter()
    model = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=6, num_workers=2)
    print(f"✅ Faster-Whisper loaded in {(time.perf_counter() - t_load)*1000:.0f} ms\n")

    results = []

    for idx, fpath in enumerate(target_files, 1):
        fname = os.path.basename(fpath)
        stats = analyze_audio_quality(fpath)
        mtime = os.path.getmtime(fpath)

        # Check interval from previous file to detect potential sentence splitting
        interval_from_prev = None
        if idx > 1:
            prev_mtime = os.path.getmtime(target_files[idx - 2])
            interval_from_prev = mtime - prev_mtime

        # Run speech-to-text
        t_start = time.perf_counter()
        segments, info = model.transcribe(fpath, beam_size=5, language="en")
        text = " ".join([s.text for s in segments]).strip()
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        rtf = (elapsed_ms / 1000.0) / max(0.001, stats['duration'])

        split_warning = ""
        if interval_from_prev is not None and interval_from_prev < 2.5:
            split_warning = f" ⚠️ [Split Alert: {interval_from_prev:.1f}s after prev]"

        print(f"[{idx:02d}/{len(target_files)}] File: {fname}{split_warning}")
        print(f"  📊 Audio: Duration={stats['duration']:.2f}s | RMS={stats['rms']:4.0f} | Peak={stats['peak']:5.0f} | SNR={stats['snr_db']:+.1f}dB | Clip={stats['clipping_ratio']:.1f}%")
        
        if text:
            print(f"  📝 Transcript: \"{text}\"")
        else:
            print(f"  ⚠️ Transcript: (No intelligible speech detected)")

        print(f"  ⚡ Latency:    {elapsed_ms:4.0f} ms (RTF: {rtf:.2f}x)")
        print("-" * 80)

        results.append({
            "name": fname,
            "duration": stats['duration'],
            "rms": stats['rms'],
            "peak": stats['peak'],
            "snr_db": stats['snr_db'],
            "clipping": stats['clipping_ratio'],
            "latency_ms": elapsed_ms,
            "rtf": rtf,
            "has_text": bool(text),
            "is_split": interval_from_prev is not None and interval_from_prev < 2.5,
        })

    # Summary Statistics
    total_dur = sum(r["duration"] for r in results)
    avg_rms = np.mean([r["rms"] for r in results])
    avg_snr = np.mean([r["snr_db"] for r in results])
    avg_latency = np.mean([r["latency_ms"] for r in results])
    avg_rtf = np.mean([r["rtf"] for r in results])
    split_count = sum(1 for r in results if r["is_split"])
    recognized_count = sum(1 for r in results if r["has_text"])

    print("\n" + "=" * 80)
    print("📈 BENCHMARK SUMMARY & MICROPHONE CALIBRATION REPORT")
    print("=" * 80)
    print(f"• Total Recordings Tested : {len(results)}")
    print(f"• Speech Recognition Rate : {recognized_count}/{len(results)} ({recognized_count/len(results)*100:.1f}%)")
    print(f"• Average Utterance Length: {total_dur/len(results):.2f}s (Total Audio: {total_dur:.1f}s)")
    print(f"• Average Voice RMS Energy: {avg_rms:.0f} (Target: 1,500 - 4,500)")
    print(f"• Average Estimated SNR   : {avg_snr:+.1f} dB (Good voice clarity: > +10 dB)")
    print(f"• Average STT Latency     : {avg_latency:.0f} ms (Avg RTF: {avg_rtf:.2f}x)")
    print(f"• Utterances Split by VAD : {split_count}/{len(results)} ({split_count/len(results)*100:.1f}%)")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NextSight STT & Audio Benchmark")
    parser.add_argument("--count", type=int, default=10, help="Number of latest recordings to benchmark")
    parser.add_argument("--all", action="store_true", help="Benchmark all recordings in audio_utterances")
    args = parser.parse_args()

    run_benchmark(sample_count=args.count, run_all=args.all)
