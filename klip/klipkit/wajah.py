"""Bingkai: deteksi & jejak wajah, potong."""
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
from .transkrip import *  # noqa: F401,F403
from .momen import *  # noqa: F401,F403
from .ai import *  # noqa: F401,F403
from .umum import AKAR

# ── 5. Bingkai: deteksi wajah ───────────────────────────────────────────────
_YUNET: dict = {}


def wajah_di(fr, w: int, h: int) -> list[tuple[float, float, float]]:
    """Semua wajah di satu frame: (x tengah, y tengah, lebar) dalam 0..1. YuNet bila modelnya ada, Haar sebagai cadangan."""
    import cv2
    lb = 640
    tb = int(h * lb / w)
    kecil = cv2.resize(fr, (lb, tb))
    model = AKAR / "model" / "face_detection_yunet_2023mar.onnx"
    if model.exists() and hasattr(cv2, "FaceDetectorYN"):
        det = _YUNET.get((lb, tb))
        if det is None:
            det = _YUNET[(lb, tb)] = cv2.FaceDetectorYN.create(str(model), "", (lb, tb), 0.6, 0.3, 50)
        _, f = det.detect(kecil)
        return [((x + fw / 2) / lb, (y + fh / 2) / tb, fw / lb) for x, y, fw, fh, *_ in (f if f is not None else [])]
    haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    g = cv2.cvtColor(kecil, cv2.COLOR_BGR2GRAY)
    return [((x + fw / 2) / lb, (y + fh / 2) / tb, fw / lb) for x, y, fw, fh in haar.detectMultiScale(g, 1.1, 5, minSize=(24, 24))]


def info_wajah(file: Path, a: float, b: float, w: int, h: int) -> dict:
    """Median posisi & ukuran wajah terbesar (0..1) di rentang klip. Tanpa wajah → tengah."""
    import cv2
    cap = cv2.VideoCapture(str(file))
    xs, ys, ss = [], [], []
    t = a
    langkah = max((b - a) / 12, 0.8)
    while t < b:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        f = wajah_di(fr, w, h)
        if f:
            x, y, s_ = max(f, key=lambda r: r[2])
            xs.append(x); ys.append(y); ss.append(s_)
        t += langkah
    cap.release()
    if not xs:
        return {"ada": False, "x": 0.5, "y": 0.35, "s": 0.18}
    return {"ada": True, "x": float(np.median(xs)), "y": float(np.median(ys)), "s": float(np.median(ss))}


def dua_wajah(file: Path, a: float, b: float, w: int, h: int) -> list[dict] | None:
    """Dua orang (kiri & kanan) untuk layout Split Podcast: semua wajah dikumpulkan lalu dibelah di celah x terbesar.
    None bila hanya satu orang terlihat (layout jatuh ke tunggal)."""
    import cv2
    cap = cv2.VideoCapture(str(file))
    semua: list[tuple[float, float, float]] = []
    t, langkah = a, max((b - a) / 16, 0.6)
    while t < b:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        semua.extend(wajah_di(fr, w, h))
        t += langkah
    cap.release()
    if len(semua) < 4:
        return None
    xs = sorted(p[0] for p in semua)
    celah, belah = max((xs[i + 1] - xs[i], (xs[i + 1] + xs[i]) / 2) for i in range(len(xs) - 1))
    if celah < 0.12:
        return None
    hasil = []
    for sisi in ([p for p in semua if p[0] < belah], [p for p in semua if p[0] >= belah]):
        if len(sisi) < 2:
            return None
        hasil.append({"x": float(np.median([p[0] for p in sisi])), "y": float(np.median([p[1] for p in sisi])),
                      "s": float(np.median([p[2] for p in sisi]))})
    return hasil


_JEJAK_SIAP: dict[tuple[float, float], list] = {}  # jejak tersimpan di klip.json (dipakai pratinjau Studio & render)


def amplop_suara(file: Path, a: float, b: float, fps: float) -> np.ndarray:
    """Kekerasan suara (RMS) per 1/fps detik di rentang [a, b]."""
    sr = 16000
    raw = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(file),
                          "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    n = int((b - a) * fps) + 1
    per = int(sr / fps)
    out = np.zeros(n)
    for k in range(n):
        bag = x[k * per:(k + 1) * per]
        if len(bag):
            out[k] = float(np.sqrt((bag ** 2).mean()))
    return out


def _korelasi(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 4 or x.std() < 1e-9 or y.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def jejak_wajah(file: Path, a: float, b: float, w: int, h: int) -> list[tuple[float, float]]:
    """Titik kunci (detik relatif klip, posisi-x 0..1) yang mengikuti PEMBICARA AKTIF.

    1) sampel 6 frame/dtk; sidik 12×12 abu-abu → pindah kamera di video sumber;
    2) tiap wajah diukur gerak mulutnya (perubahan sepertiga bawah wajah, dikurangi gerak kepala);
    3) pembicara = wajah yang gerak mulutnya paling SEIRAMA dengan suara di jendela ±1,5 dtk,
       jadi pendengar yang tertawa/mengangguk tidak dipilih;
    4) kamera diam (tidak menggeser lewat ruang kosong), pindah seketika di potongan kamera
       atau saat pembicara berganti ≥ 0,8 dtk; celah tanpa wajah memakai posisi terakhir, bukan tengah.
    """
    siap = _JEJAK_SIAP.get((round(a, 2), round(b, 2)))
    if siap:
        return [tuple(x) for x in siap]
    import cv2
    fps = 6.0
    depan = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profil = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    try:
        suara = amplop_suara(file, a, b, fps)
    except Exception:  # noqa: BLE001 — video tanpa audio
        suara = np.zeros(int((b - a) * fps) + 1)
    LB = 640
    sk = LB / w
    TB = int(h * sk)
    yunet = None
    model = AKAR / "model" / "face_detection_yunet_2023mar.onnx"
    if model.exists() and hasattr(cv2, "FaceDetectorYN"):
        yunet = cv2.FaceDetectorYN.create(str(model), "", (LB, TB), 0.6, 0.3, 50)
    n = int((b - a) * fps) + 1
    cap = cv2.VideoCapture(str(file))
    cap.set(cv2.CAP_PROP_POS_MSEC, a * 1000)
    sumber_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    sampel: list[list[dict]] = []
    potong = [False] * n
    sidik_lalu = None
    k, fr_i = 0, 0
    while k < n:
        ok, fr = cap.read()
        if not ok:
            break
        t_fr = a + fr_i / sumber_fps
        fr_i += 1
        if t_fr + 1e-6 < a + k / fps:
            continue
        kecil = cv2.resize(fr, (LB, TB))
        abu = cv2.cvtColor(kecil, cv2.COLOR_BGR2GRAY)
        sd = cv2.resize(abu, (12, 12), interpolation=cv2.INTER_AREA).astype(np.float32)
        if sidik_lalu is not None:
            potong[k] = float(np.abs(sd - sidik_lalu).mean()) > 28
        sidik_lalu = sd
        kotak = []
        if yunet is not None:  # YuNet: tahan wajah miring, tertawa, berhijab
            _, hasil = yunet.detect(kecil)
            for r in (hasil if hasil is not None else []):
                x, y, fw, fh = (int(max(v, 0)) for v in r[:4])
                if fw > 8 and fh > 8:
                    kotak.append((x, y, min(fw, LB - x), min(fh, TB - y), 1.0))
        else:
            kotak = [(x, y, fw, fh, 1.0) for (x, y, fw, fh) in depan.detectMultiScale(abu, 1.1, 5, minSize=(24, 24))]
        if not kotak:  # profil hanya bila tidak ada wajah depan (hemat waktu)
            kotak += [(x, y, fw, fh, 0.8) for (x, y, fw, fh) in profil.detectMultiScale(abu, 1.1, 5, minSize=(24, 24))]
            kotak += [(LB - x - fw, y, fw, fh, 0.8) for (x, y, fw, fh) in
                      profil.detectMultiScale(cv2.flip(abu, 1), 1.1, 5, minSize=(24, 24))]
        wajah = []
        for (x, y, fw, fh, bobot) in kotak:
            mulut = cv2.resize(abu[y + int(fh * 0.62):y + fh, x + int(fw * 0.2):x + int(fw * 0.8)], (24, 12)).astype(np.float32)
            atas = cv2.resize(abu[y:y + int(fh * 0.5), x:x + fw], (24, 12)).astype(np.float32)
            wajah.append({"cx": (x + fw / 2) / LB, "luas": fw * fh / (LB * TB) * bobot, "mulut": mulut, "atas": atas})
        # buang wajah dobel (depan & profil di tempat sama)
        unik: list[dict] = []
        for f in sorted(wajah, key=lambda f: -f["luas"]):
            if all(abs(f["cx"] - u["cx"]) > 0.06 for u in unik):
                unik.append(f)
        sampel.append(unik)
        k += 1
    cap.release()
    n = len(sampel)
    if n == 0 or all(not s for s in sampel):
        return [(0.0, 0.5)]
    suara = np.pad(suara, (0, max(0, n - len(suara))))[:n]
    potong = potong[:n]
    DEKAT = 0.09

    def cari(j: int, c: float):
        if 0 <= j < n:
            f = min(sampel[j], key=lambda f: abs(f["cx"] - c), default=None)
            if f is not None and abs(f["cx"] - c) < DEKAT:
                return f
        return None

    # gerak mulut per wajah per sampel (dibandingkan sampel sebelumnya pada wajah yang sama)
    for j in range(n):
        for f in sampel[j]:
            g = cari(j - 1, f["cx"]) if not potong[j] else None
            if g is None:
                f["gerak"] = 0.0
            else:
                gm = float(np.abs(f["mulut"] - g["mulut"]).mean())
                ga = float(np.abs(f["atas"] - g["atas"]).mean())
                f["gerak"] = max(gm - 0.6 * ga, 0.0)  # kurangi gerak kepala/kamera

    awal_shot = [j for j in range(n) if j == 0 or potong[j]]
    target = [float("nan")] * n
    jendela = int(1.5 * fps)
    for si, s0 in enumerate(awal_shot):
        s1 = (awal_shot[si + 1] if si + 1 < len(awal_shot) else n) - 1
        pusat: list[float] = []
        for j in range(s0, s1 + 1):
            for f in sampel[j]:
                if all(abs(c - f["cx"]) >= DEKAT for c in pusat):
                    pusat.append(f["cx"])
        if not pusat:
            continue
        if len(pusat) == 1:
            for j in range(s0, s1 + 1):
                target[j] = pusat[0]
            continue
        for j in range(s0, s1 + 1):
            lo, hi = max(s0, j - jendela), min(s1, j + jendela)
            terbaik, skor_terbaik = pusat[0], -9.0
            for c in pusat:
                gerak, ada, luas = [], 0, 0.0
                for q in range(lo, hi + 1):
                    f = cari(q, c)
                    if f is not None:
                        ada += 1
                        luas += f["luas"]
                    gerak.append(f["gerak"] if f is not None else 0.0)
                if not ada:
                    continue
                skor = (_korelasi(np.array(gerak), suara[lo:hi + 1]) + 0.15 * ada / len(gerak)
                        + 0.5 * luas / ada)
                if skor > skor_terbaik:
                    terbaik, skor_terbaik = c, skor
            target[j] = terbaik
    # celah tanpa wajah → posisi terdekat sebelumnya (jangan kembali ke tengah)
    akhir = next((x for x in target if x == x), 0.5)
    for j in range(n):
        if target[j] != target[j]:
            target[j] = akhir
        else:
            akhir = target[j]
    # kamera menahan posisi; pindah seketika di potongan kamera, atau bila pembicara baru bertahan ≥ 0,8 dtk
    kam, calon, lama = target[0], target[0], 0
    kunci: list[tuple[float, float]] = [(0.0, kam)]
    for j in range(1, n):
        baru = None
        if potong[j]:
            baru = target[j]
            calon, lama = baru, 0
        elif abs(target[j] - kam) > DEKAT:
            if abs(target[j] - calon) < DEKAT:
                lama += 1
            else:
                calon, lama = target[j], 1
            if lama >= int(0.8 * fps):
                baru, lama = calon, 0
        else:
            lama = 0
        if baru is not None and abs(baru - kam) > 0.01:
            t = j / fps
            kunci.append((max(t - 0.02, kunci[-1][0] + 0.01), kam))  # tahan sampai detik pindah …
            kunci.append((t, baru))                                   # … lalu lompat tegas
            kam = baru
    kunci.append(((n - 1) / fps + 1 / fps, kam))
    return kunci


def potong_jejak(w: int, h: int, rasio: float, kunci: list[tuple[float, float]]) -> str:
    """Crop berasio tetap dengan posisi-x yang berubah menurut waktu (interpolasi linear antar titik kunci)."""
    if w / h > rasio:
        ch = h // 2 * 2
        cw = int(ch * rasio) // 2 * 2
    else:
        cw = w // 2 * 2
        ch = int(cw / rasio) // 2 * 2
    if len(kunci) == 1 or cw >= w:
        x0 = int(min(max(kunci[0][1] * w - cw / 2, 0), w - cw))
        return f"crop={cw}:{ch}:{x0}:{(h - ch) // 2}"
    px = [(t, min(max(x * w - cw / 2, 0), w - cw)) for t, x in kunci]
    # ffmpeg membatasi kedalaman ekspresi bertingkat: klip panjang dengan banyak pindah kamera gagal
    # ("Missing ')' or too many args"). Buang titik yang tidak mengubah posisi, lalu batasi jumlah titik.
    ringkas = [px[0]]
    for i, p in enumerate(px[1:-1], 1):
        if abs(p[1] - ringkas[-1][1]) > 2 or abs(px[i + 1][1] - p[1]) > 2:
            ringkas.append(p)
    ringkas.append(px[-1])
    MAKS = 48
    if len(ringkas) > MAKS:  # pertahankan titik awal/akhir, ambil sampel merata di antaranya
        langkah = (len(ringkas) - 1) / (MAKS - 1)
        ringkas = [ringkas[round(i * langkah)] for i in range(MAKS)]
    px = ringkas
    # bangun ekspresi bertingkat: if(lt(t,t1), lerp, if(lt(t,t2), …))
    expr = f"{px[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(px, px[1:]))):
        if t1 - t0 < 0.05:
            seg = f"{x1:.1f}"
        else:
            seg = f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{t1 - t0:.2f}"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    expr = expr.replace(",", "\\,")
    return f"crop={cw}:{ch}:{expr}:{(h - ch) // 2}"


def potong(w: int, h: int, rasio: float, cx: float, cy: float = 0.5, skala: float = 1.0) -> str:
    """Filter crop ffmpeg berasio `rasio` (lebar/tinggi) di sekitar titik (cx, cy). skala<1 = memperbesar (zoom)."""
    if w / h > rasio:
        ch = int(h * skala) // 2 * 2
        cw = int(ch * rasio) // 2 * 2
    else:
        cw = int(w * skala) // 2 * 2
        ch = int(cw / rasio) // 2 * 2
    cw, ch = min(cw, w // 2 * 2), min(ch, h // 2 * 2)
    x = int(min(max(cx * w - cw / 2, 0), w - cw))
    y = int(min(max(cy * h - ch / 2, 0), h - ch))
    return f"crop={cw}:{ch}:{x}:{y}"
