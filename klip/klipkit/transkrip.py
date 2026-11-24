"""Transkrip: faster-whisper (opsional, impor malas), Gemini, dan berkas .srt."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np
from .umum import *  # noqa: F401,F403
from .sumber import *  # noqa: F401,F403
from .umum import AKAR

# ── 3. Transkrip ────────────────────────────────────────────────────────────
@dataclass
class Kata:
    mulai: float
    selesai: float
    teks: str


@dataclass
class Kalimat:
    mulai: float
    selesai: float
    teks: str
    kata: list[Kata]


def muat_model_whisper(model: str) -> str:
    """Unduh model Whisper dengan coba-ulang. Koneksi ke huggingface.co sering putus/diblok di sebagian
    jaringan (laporan: httpcore error di langkah Transkrip), jadi coba lagi lalu pakai cermin hf-mirror.com."""
    import time
    try:
        from faster_whisper.utils import _MODELS
        from huggingface_hub import snapshot_download
    except Exception:
        return model
    repo = _MODELS.get(model)
    if not repo:
        return model  # path lokal / nama repo langsung
    pola = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
    try:  # sudah pernah diunduh: jangan sentuh internet
        return snapshot_download(repo, allow_patterns=pola, local_files_only=True)
    except Exception:
        pass
    galat = None
    for ujung in [None, None, "https://hf-mirror.com"]:
        try:
            if ujung:
                log(f"Unduhan model dari huggingface.co gagal, mencoba cermin {ujung}…")
            return snapshot_download(repo, allow_patterns=pola, endpoint=ujung, etag_timeout=30)
        except Exception as e:
            galat = e
            log(f"Unduh model gagal ({type(e).__name__}): {str(e)[:160]}")
            time.sleep(3)
    raise RuntimeError("Model subtitle (Whisper) gagal diunduh. Cek koneksi internet (coba Wi-Fi/hotspot lain "
                       "atau matikan VPN/DNS pemblokir), lalu ulangi.") from galat


def transkrip(file: Path, model: str, bahasa: str | None) -> list[Kalimat]:
    from faster_whisper import WhisperModel
    log(f"Transkripsi dengan faster-whisper model '{model}' (pertama kali akan mengunduh model)…")
    m = WhisperModel(muat_model_whisper(model), device="cpu", compute_type="int8")
    sr = 16000  # decode sendiri dengan ffmpeg bawaan → tidak bergantung versi PyAV
    raw = subprocess.run([ffmpeg(), "-v", "error", "-i", str(file), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    # Akurasi bahasa Indonesia lisan: beam search, tanpa "condition_on_previous_text" (mencegah salah dengar yang
    # menular ke kalimat berikutnya / pengulangan), dan prompt awal berisi gaya bahasa sehari-hari sebagai petunjuk ejaan.
    petunjuk = ("Percakapan santai bahasa Indonesia: nggak, enggak, gimana, kayak, banget, sih, dong, kok, gue, lo, "
                "YouTube, podcast, konten, viral.") if (bahasa or "id") == "id" else None
    segs, info = m.transcribe(audio, language=bahasa, word_timestamps=True, vad_filter=True, beam_size=5,
                              condition_on_previous_text=False, initial_prompt=petunjuk)
    hasil = []
    for s in segs:
        kata = [Kata(w.start, w.end, w.word.strip()) for w in (s.words or []) if w.word.strip()]
        hasil.append(Kalimat(s.start, s.end, s.text.strip(), kata))
    log(f"Bahasa terdeteksi: {info.language}, {len(hasil)} kalimat.")
    return hasil



# ── 3b. Transkrip lewat Gemini (tanpa faster-whisper; untuk worker web/VPS) ──────
GEMINI_POTONG = 180  # detik per permintaan: waktu dari Gemini makin melenceng di audio panjang (uji: 10 mnt → -6 dtk)
GEMINI_TEPI = 3      # detik tambahan di kedua sisi potongan supaya kalimat di batas tidak terpotong
GEMINI_PARALEL = 3


def _waktu(x) -> float:
    """'MM:SS.s' / 'HH:MM:SS' / angka → detik."""
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return detik(str(x))
    except ValueError:
        return 0.0


def _bagi_kata(a: float, b: float, teks: str) -> list[Kata]:
    """Waktu per kata dibagi sesuai panjang huruf (Gemini tidak memberi waktu per kata yang andal)."""
    kata = teks.split()
    if not kata:
        return []
    bobot = [len(k) + 2 for k in kata]
    total, t, out = sum(bobot), a, []
    for k, w in zip(kata, bobot):
        d = (b - a) * w / total
        out.append(Kata(round(t, 3), round(t + d, 3), k))
        t += d
    return out


def _audio_kecil(file: Path, a: float, lama: float, out: Path) -> tuple[Path, str]:
    """Potongan audio 16 kHz mono: opus (kecil); mp3 bila ffmpeg tidak punya libopus."""
    for ext, codec, mime in ((".ogg", ["-c:a", "libopus", "-b:a", "24k"], "audio/ogg"),
                             (".mp3", ["-c:a", "libmp3lame", "-b:a", "32k"], "audio/mp3")):
        f = out.with_suffix(ext)
        r = subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{lama:.3f}", "-i", str(file),
                            "-vn", "-ac", "1", "-ar", "16000", *codec, str(f)], capture_output=True)
        if r.returncode == 0 and f.exists() and f.stat().st_size > 0:
            return f, mime
    raise RuntimeError("Gagal mengekstrak audio untuk Gemini.")


_KUOTA_HABIS: set[str] = set()  # model yang sudah 429 di proses ini → potongan berikutnya tidak mencobanya lagi


def _minta_gemini(klien, urutan: list[str], data: bytes, mime: str, lama: float, bahasa: str | None) -> list[dict]:
    from google.genai import errors as gerr, types as gtypes
    bhs = {"id": "bahasa Indonesia", "en": "English"}.get(bahasa or "", bahasa or "bahasa aslinya")
    prompt = (f"Transkripsikan audio ini kata demi kata dalam {bhs} (panjang audio {lama:.0f} detik). "
              "Tulis persis yang diucapkan, termasuk kata sehari-hari (nggak, gimana, gue, sih, dong); jangan meringkas, "
              "jangan menerjemahkan, lewati bagian tanpa ucapan. Pecah per kalimat atau frasa pendek (maks ±12 kata). "
              "Beri waktu mulai dan selesai tiap kalimat dalam detik sejak awal audio (angka desimal, teliti). "
              'Balas HANYA JSON: [{"mulai": 1.2, "selesai": 4.8, "teks": "..."}]')
    skema = gtypes.Schema(type="ARRAY", items=gtypes.Schema(type="OBJECT", required=["mulai", "selesai", "teks"], properties={
        "mulai": gtypes.Schema(type="NUMBER"), "selesai": gtypes.Schema(type="NUMBER"), "teks": gtypes.Schema(type="STRING")}))
    cfg = gtypes.GenerateContentConfig(response_mime_type="application/json", response_schema=skema, temperature=0)
    isi = [gtypes.Part.from_bytes(data=data, mime_type=mime), prompt]
    pesan = ""
    import os
    # 503/429 (server penuh) sering sesaat: dua putaran semua model dengan jeda makin panjang.
    # Model yang 429 (kuota habis) dilewati untuk sisa proses; 503 dicoba ulang. GEMINI_CEPAT=1 (web): jeda lebih pendek.
    cepat = os.environ.get("GEMINI_CEPAT") == "1"
    coba_urut = [m for m in urutan if m not in _KUOTA_HABIS] or urutan
    for putaran, model in [(p, m) for p in range(2) for m in coba_urut]:
        if model in _KUOTA_HABIS:
            continue
        for coba in range(2):
            try:
                r = klien.models.generate_content(model=model, contents=isi, config=cfg)
                hasil = json.loads(r.text or "[]")
                if isinstance(hasil, list):
                    return hasil
            except gerr.APIError as e:
                pesan = f"{model}: {e.code} {getattr(e, 'message', '') or e}"
                if e.code in (400, 401, 403) and "API key" in str(e):
                    raise RuntimeError(f"Kunci Gemini ditolak ({e.code}). Cek kuncinya.") from e
                if e.code == 404:
                    break  # model tidak ada → model berikutnya
                if e.code == 429:
                    _KUOTA_HABIS.add(model)
                if e.code == 429:
                    log(f"Gemini {model}: kuota habis, pindah model…")
                    break  # kuota per model → model lain (model lite sering masih tersedia)
            except (ValueError, json.JSONDecodeError) as e:
                pesan = f"{model}: jawaban bukan JSON ({e})"
            log(f"Gemini belum berhasil ({pesan[:90]}), mencoba lagi…")
            time.sleep((4 + coba * 6 + putaran * 10) if cepat else 5 + coba * 10 + putaran * 30)
    raise RuntimeError(f"Transkripsi Gemini gagal: {pesan}")


def transkrip_gemini(file: Path, bahasa: str | None, kerja: Path, kunci: str | None = None) -> list[Kalimat]:
    """Audio → Gemini per potongan 3 menit (teks) → penyelaras MMS (waktu per kata dari audio) → Kalimat.
    Kunci dari argumen atau GEMINI_API_KEY (.env / lingkungan); tidak ditulis ke disk/log."""
    from google import genai
    from google.genai import types as gtypes
    from .ai import baca_env
    env = baca_env()
    kunci = kunci or env.get("GEMINI_API_KEY")
    if not kunci:
        raise RuntimeError("Transkripsi Gemini butuh GEMINI_API_KEY (gratis: aistudio.google.com/apikey).")
    urutan = list(dict.fromkeys([env.get("GEMINI_MODEL", "gemini-3.8-flash"), "gemini-3.8-flash", "gemini-3.6-flash",
                                 "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]))
    total = info_video(file)[0]
    n = max(1, int(np.ceil(total / GEMINI_POTONG)))
    log(f"Transkripsi dengan Gemini ({n} potongan audio @{GEMINI_POTONG} dtk, {total / 60:.1f} menit)…")
    from concurrent.futures import ThreadPoolExecutor

    def jendela(i: int) -> tuple[float, float, float, float]:
        awal, akhir = i * GEMINI_POTONG, min((i + 1) * GEMINI_POTONG, total)
        a = max(0.0, awal - GEMINI_TEPI)
        return awal, akhir, a, min(akhir + GEMINI_TEPI, total) - a

    def minta(klien, i: int) -> list[Kalimat]:
        """Kalimat mentah satu potongan (waktu relatif potongan, belum disaring)."""
        _, _, a, lama = jendela(i)
        f, mime = _audio_kecil(file, a, lama, kerja / f"gemini-audio-{i}")
        try:
            seg = _minta_gemini(klien, urutan, f.read_bytes(), mime, lama, bahasa)
        finally:
            f.unlink(missing_ok=True)
        out, batas = [], 0.0
        for s in sorted(seg, key=lambda s: _waktu(s.get("mulai"))):
            teks = " ".join(str(s.get("teks", "")).split())
            m, e = _waktu(s.get("mulai")), _waktu(s.get("selesai"))
            if not teks:
                continue
            m = min(max(m, batas), lama)          # jaga urutan & tidak tumpang tindih
            e = min(max(e, m + 0.3), lama)
            batas = e
            out.append(Kalimat(round(a + m, 3), round(a + e, 3), teks, _bagi_kata(a + m, a + e, teks)))
        return out

    with genai.Client(api_key=kunci, http_options=gtypes.HttpOptions(timeout=180_000)) as klien:
        with ThreadPoolExecutor(GEMINI_PARALEL) as ex:
            mentah = list(ex.map(lambda i: minta(klien, i), range(n)))

    # Waktu dari Gemini bisa melenceng puluhan detik → selaraskan teks tiap potongan ke audionya (forced alignment).
    pen = None
    try:
        from .selaras import Penyelaras, SR
        pen = Penyelaras()
    except ImportError:
        log("Penyelaras waktu tidak terpasang (butuh torch + torchaudio); memakai waktu kasar dari Gemini.")
    if pen:
        raw = subprocess.run([ffmpeg(), "-v", "error", "-i", str(file), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                             capture_output=True, check=True).stdout
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
        for i, ks in enumerate(mentah):
            _, _, a, lama = jendela(i)
            ok = pen.selaraskan(audio, ks, a, a + lama)
            log(f"Potongan {i + 1}/{n}: {ok}/{len(ks)} kalimat diselaraskan ke audio.")
        del audio

    hasil: list[Kalimat] = []
    for i, ks in enumerate(mentah):
        awal, akhir, _, _ = jendela(i)
        for k in ks:
            if k.selesai - k.mulai < 0.2 or not (awal <= k.mulai < akhir):  # kalimat di tepi milik potongan tetangga
                continue
            if hasil and k.mulai < hasil[-1].selesai:  # rapikan tumpang tindih di sambungan
                k.mulai = hasil[-1].selesai
                if k.selesai - k.mulai < 0.2:
                    continue
                k.kata = [w for w in k.kata if w.mulai >= k.mulai] or _bagi_kata(k.mulai, k.selesai, k.teks)
            hasil.append(k)
    log(f"Transkrip Gemini: {len(hasil)} kalimat.")
    return hasil


# ── 8. Subtitle dari berkas .srt & rentang ──────────────────────────────────
def detik(t: str) -> float:
    t = t.strip().replace(",", ".")
    bag = [float(x) for x in t.split(":")]
    s = 0.0
    for b in bag:
        s = s * 60 + b
    return s


def baca_srt(p: Path) -> list[Kalimat]:
    """SRT/TXT bertanda waktu → kalimat; waktu per kata dibagi rata (cukup untuk subtitle)."""
    teks = p.read_text(encoding="utf-8", errors="replace")
    hasil = []
    for blok in re.split(r"\n\s*\n", teks.strip()):
        m = re.search(r"(\d[\d:.,]*)\s*-->\s*(\d[\d:.,]*)", blok)
        if not m:
            continue
        isi = " ".join(b for b in blok[m.end():].strip().splitlines() if b.strip())
        isi = re.sub(r"<[^>]+>", "", isi).strip()
        a, b = detik(m.group(1)), detik(m.group(2))
        kata = isi.split()
        if not kata:
            continue
        lang = (b - a) / len(kata)
        hasil.append(Kalimat(a, b, isi, [Kata(a + i * lang, a + (i + 1) * lang, k) for i, k in enumerate(kata)]))
    if not hasil:
        sys.exit("Berkas subtitle tidak berisi penanda waktu (format SRT: 00:00:01,000 --> 00:00:03,000).")
    return hasil


def baca_rentang(r: str | None, panjang: float) -> tuple[float, float]:
    if not r:
        return 0.0, panjang
    a, _, b = r.partition("-")
    return detik(a) if a else 0.0, min(detik(b), panjang) if b else panjang
