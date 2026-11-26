# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "yt-dlp[default]>=2026.10.26",
#   "deno>=2.0",
#   "imageio-ffmpeg>=0.6.0",
#   "faster-whisper>=1.1.0",
#   "opencv-python-headless>=4.9,<5",
#   "numpy>=1.26",
#   "google-genai>=1.0",
# ]
# ///
# kredit-maliki: Template oleh Maliki Saputra (@malikisaputra) — https://malikisaputra.biz.id · Lisensi Nonkomersial Maliki Studio 1.0 (alat/LISENSI.md): dilarang dijual ulang.
"""Klip — potong video panjang jadi klip pendek 9:16 (TikTok/Reels/Shorts).

Ditulis dari nol oleh Maliki Motion. Jalan lokal di Mac, tanpa API berbayar.

Contoh:
  uv run klip.py video.mp4
  uv run klip.py "https://www.youtube.com/watch?v=..." --jumlah 3 --durasi 30
  uv run klip.py video.mp4 --hanya-prompt          # Opsi B: siapkan prompt untuk Claude
  uv run klip.py video.mp4 --pilihan pilihan.json  # Opsi B: render pilihan Claude
  uv run klip.py video.mp4 --ai gemini              # pakai AI milik pengguna (kunci di file .env)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from klipkit.umum import *  # noqa: F401,F403
from klipkit.sumber import *  # noqa: F401,F403
from klipkit.transkrip import *  # noqa: F401,F403
from klipkit.momen import *  # noqa: F401,F403
from klipkit.ai import *  # noqa: F401,F403
from klipkit.wajah import *  # noqa: F401,F403
from klipkit.render import *  # noqa: F401,F403
from klipkit.umum import AKAR  # noqa: F401
from klipkit.wajah import _JEJAK_SIAP


# ── Utama ───────────────────────────────────────────────────────────────────
def muat_transkrip(file: Path, a, kerja: Path) -> list[Kalimat]:
    if a.subtitle_file:
        log(f"Memakai subtitle dari {Path(a.subtitle_file).name}.")
        return baca_srt(Path(a.subtitle_file))
    mesin = getattr(a, "transkrip", "whisper") or "whisper"
    # --rentang berakhir sebelum video selesai → transkripsi hanya sampai batas itu (video panjang jauh lebih cepat)
    awal, akhir = 0.0, None
    if getattr(a, "rentang", None):
        panjang = info_video(file)[0]
        ra, rb = baca_rentang(a.rentang, panjang)
        if panjang and (ra > 5 or rb < panjang - 5):
            awal, akhir = max(0.0, ra - 2), min(panjang, rb + 2)  # sedikit lebih lebar supaya kalimat di tepi utuh
    akhiran = f"-{int(awal)}-{int(akhir)}" if akhir else ""
    cache = kerja / f"transkrip-{file.stem}-{'gemini' if mesin == 'gemini' else a.model}{akhiran}.json"
    if cache.exists():
        log("Transkrip diambil dari cache.")
        return [Kalimat(k["mulai"], k["selesai"], k["teks"], [Kata(**x) for x in k["kata"]])
                for k in json.loads(cache.read_text(encoding="utf-8"))]
    bahasa = None if a.bahasa in ("auto", "", None) else a.bahasa
    sub_yt = sorted(kerja.glob("sumber*.json3"))
    if sub_yt and os.environ.get("KLIP_SUB_YT") == "1":
        kal = baca_json3(sub_yt[0])
        g = getattr(a, "_geser", 0.0)
        if g:  # subtitle YouTube memakai waktu video utuh; file hanya potongan
            for k in kal:
                k.mulai, k.selesai = round(k.mulai - g, 3), round(k.selesai - g, 3)
                for w in k.kata:
                    w.mulai, w.selesai = round(w.mulai - g, 3), round(w.selesai - g, 3)
            kal = [k for k in kal if k.mulai >= 0]
        kal = [k for k in kal if not akhir or (k.selesai > awal and k.mulai < akhir)]
        if len(kal) >= 5:
            log(f"Memakai subtitle otomatis YouTube ({len(kal)} kalimat): transkripsi dilewati.")
            a._sub_yt = True
            cache.write_text(json.dumps([asdict(k) for k in kal], ensure_ascii=False), encoding="utf-8")
            return kal
    t0 = time.time()
    if akhir:
        potong = kerja / f"potong{akhiran}.mp4"
        if not potong.exists():  # potong ulang (bukan -c copy) supaya titik awal tepat → waktu subtitle tidak bergeser
            subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", f"{awal:.2f}", "-i", str(file), "-t", f"{akhir - awal:.2f}",
                            "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                            "-c:a", "aac", "-b:a", "96k", str(potong)], check=True)
        log(f"Transkripsi hanya menit {awal / 60:.0f}–{akhir / 60:.0f}.")
        file = potong
    if mesin == "gemini":
        try:
            kal = transkrip_gemini(file, bahasa, kerja, getattr(a, "kunci_gemini", None))
        except RuntimeError as e:
            sys.exit(str(e))
    else:
        kal = transkrip(file, a.model, bahasa)
    log(f"Transkripsi selesai ({time.time() - t0:.0f} dtk).")
    if awal:
        for k in kal:
            k.mulai, k.selesai = round(k.mulai + awal, 3), round(k.selesai + awal, 3)
            for w in k.kata:
                w.mulai, w.selesai = round(w.mulai + awal, 3), round(w.selesai + awal, 3)
    cache.write_text(json.dumps([asdict(k) for k in kal], ensure_ascii=False), encoding="utf-8")
    return kal


def bagi_segmen(kal: list, a) -> list[list]:
    """Mode live/VOD panjang: transkrip dibagi per SEGMEN menit supaya AI membaca semuanya dengan teliti
    (satu prompt raksasa membuat momen di tengah-tengah sering terlewat)."""
    if not kal:
        return [kal]
    menit = float(getattr(a, "segmen", 0) or 0)
    if menit < 0:
        return [kal]
    rentang = kal[-1].selesai - kal[0].mulai
    if menit <= 0:  # otomatis: hanya untuk video di atas 40 menit
        if rentang <= 40 * 60:
            return [kal]
        menit = 20
    lebar = menit * 60
    hasil, awal = [], kal[0].mulai
    while awal < kal[-1].selesai:
        isi = [k for k in kal if k.mulai >= awal and k.mulai < awal + lebar]
        if isi:
            hasil.append(isi)
        awal += lebar
    if len(hasil) > 1 and sum(k.selesai - k.mulai for k in hasil[-1]) < lebar * 0.25:
        hasil[-2] += hasil.pop()  # sisa pendek digabung ke segmen sebelumnya
    return hasil


def tanya_per_segmen(penyedia: str, segmen: list[list], a, kerja: Path, heatmap: list) -> dict:
    per = max(2, -(-a.jumlah // len(segmen)) + 1)  # kandidat per segmen, sedikit lebih banyak dari kebutuhan
    calon, ringkasan, hashtag = [], [], []
    for i, isi in enumerate(segmen, 1):
        log(f"Mode video panjang: segmen {i}/{len(segmen)} ({isi[0].mulai / 60:.0f}–{isi[-1].selesai / 60:.0f} mnt), "
            f"meminta {per} kandidat ke AI '{penyedia}'…")
        f = kerja / f"prompt-segmen-{i:02d}.md"
        tulis_prompt(isi, a.durasi, per, f, a.arahan)
        try:
            data = ambil_json(tanya_ai(penyedia, f.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001 — satu segmen gagal tidak menggagalkan semuanya
            log(f"Segmen {i} dilewati: {e}")
            continue
        if isinstance(data, dict):
            if data.get("ringkasan"):
                ringkasan.append(data["ringkasan"])
            hashtag += [t for t in data.get("hashtag", []) if t not in hashtag]
            data = data.get("klip", [])
        for d in data or []:
            try:
                d["mulai"], d["selesai"] = float(d["mulai"]), float(d["selesai"])
                if d["selesai"] <= d["mulai"]:
                    continue
                d["skor"] = float(d.get("skor", 0) or 0) + (0.3 * nilai_heatmap(heatmap, float(d["mulai"]), float(d["selesai"]))
                                                             if heatmap else 0)
                calon.append(d)
            except (KeyError, TypeError, ValueError):
                pass
    if not calon:
        sys.exit("AI tidak mengembalikan momen dari segmen mana pun.")
    pilih = []
    for d in sorted(calon, key=lambda x: -x["skor"]):  # ambil skor tertinggi, tanpa tumpang tindih
        if all(float(d["selesai"]) <= float(p["mulai"]) or float(d["mulai"]) >= float(p["selesai"]) for p in pilih):
            pilih.append(d)
        if len(pilih) >= a.jumlah:
            break
    log(f"Mode video panjang: {len(calon)} kandidat dari {len(segmen)} segmen → {len(pilih)} klip terbaik.")
    return {"ringkasan": " ".join(ringkasan[:2]), "hashtag": hashtag[:5],
            "klip": sorted(pilih, key=lambda x: float(x["mulai"]))}


def analisis(a, keluar: Path, kerja: Path) -> None:
    bagian = None
    if a.rentang and re.match(r"^https?://", a.sumber) and os.environ.get("KLIP_UNDUH_BAGIAN") == "1":
        try:
            r0, r1 = (detik(x.strip()) for x in a.rentang.split("-", 1))
            bagian = (max(0.0, r0), r1) if r1 > r0 else None
        except ValueError:
            bagian = None
    sudah_ada = any(f.suffix in (".mp4", ".mkv", ".webm") for f in kerja.glob("sumber.*"))
    try:
        file, heatmap, judul = ambil_sumber(a.sumber, kerja, None if sudah_ada else bagian)
    except RuntimeError as e:
        sys.exit(str(e))
    geser = 0.0
    if bagian and (kerja / "bagian.json").exists() or (bagian and not sudah_ada):
        if not sudah_ada:
            (kerja / "bagian.json").write_text(json.dumps(list(bagian)), encoding="utf-8")
        geser = float(json.loads((kerja / "bagian.json").read_text(encoding="utf-8"))[0])
    if geser:
        # file hanya berisi potongan → semua waktu digeser supaya 0 = awal potongan
        heatmap = [{**x, "start_time": x["start_time"] - geser, "end_time": x["end_time"] - geser}
                   for x in heatmap if x.get("end_time", 0) > geser]
        a._geser = geser
        a.rentang = None
    panjang, w, h = info_video(file)
    log(f"Video: {judul} — {panjang:.0f} detik, {w}x{h}. Heatmap YouTube: {'ada' if heatmap else 'tidak ada'}.")
    semua = muat_transkrip(file, a, kerja)
    ra, rb = baca_rentang(a.rentang, panjang)
    kal = [k for k in semua if k.selesai > ra and k.mulai < rb]
    if a.rentang:
        log(f"Rentang analisis: {ra:.0f}–{rb:.0f} detik ({len(kal)} kalimat).")

    if a.hanya_prompt:
        out = keluar / "prompt-claude.md"
        tulis_prompt(kal, a.durasi, a.jumlah, out, a.arahan)
        log(f"Prompt siap: {out}\n  Tempel ke Claude, simpan balasannya sebagai pilihan.json, lalu jalankan:\n"
            f"  uv run klip.py \"{a.sumber}\" --pilihan pilihan.json")
        return

    ringkasan, hashtag = "", []
    penyedia = (a.ai or baca_env().get("AI", "lokal")).lower()
    if a.pilihan or penyedia != "lokal":
        if a.pilihan:
            data = json.loads(Path(a.pilihan).read_text(encoding="utf-8"))
            sumber_ai = "pilihan.json"
        else:
            segmen = bagi_segmen(kal, a)
            if len(segmen) > 1:
                data = tanya_per_segmen(penyedia, segmen, a, kerja, heatmap)
            else:
                log(f"Meminta AI '{penyedia}' memilih momen…")
                tulis_prompt(kal, a.durasi, a.jumlah, kerja / "prompt.md", a.arahan)
                data = ambil_json(tanya_ai(penyedia, (kerja / "prompt.md").read_text(encoding="utf-8")))
            sumber_ai = penyedia
        if isinstance(data, dict):
            ringkasan, hashtag = data.get("ringkasan", ""), data.get("hashtag", [])
            data = data.get("klip", [])
        klips = [Klip(float(d["mulai"]), float(d["selesai"]), d.get("judul", ""), float(d.get("skor", 0) or 0),
                      d.get("alasan", ""), d.get("caption", ""), "",
                      [str(x) for x in (d.get("kata_kunci") or []) if x][:8]) for d in data]
        hooks = [d.get("hook") for d in data]
        # model kecil (mis. Ollama) kadang memilih potongan terlalu pendek → perpanjang ke akhir kalimat terdekat dari target
        for k in klips:
            if k.selesai - k.mulai < a.durasi * 0.6:
                target = k.mulai + a.durasi
                akhir = [x.selesai for x in kal if x.selesai >= target - 2]
                k.selesai = min(akhir[0] if akhir else target, panjang or target)
                log(f"Momen “{k.judul}” terlalu pendek → diperpanjang ke {k.selesai - k.mulai:.0f} dtk.")
        log(f"Memakai {len(klips)} momen dari {sumber_ai}.")
    else:
        log("Mencari momen terbaik (heatmap + energi suara + kepadatan kata + kata hook)…")
        klips = pilih_momen(kal, energi_audio(file), heatmap, a.durasi, a.jumlah, panjang, a.arahan)
        hooks = [None] * len(klips)
        sumber_ai = "lokal"

    daftar = []
    for i, (k, hk) in enumerate(zip(klips, hooks), 1):
        isi = [x for x in kal if x.selesai > k.mulai and x.mulai < k.selesai]
        k.kutipan = " ".join(x.teks for x in isi)[:400]
        if not k.caption:
            k.caption = k.judul
        if hk is None:
            kena = [x for x in isi if set(re.findall(r"\w+", x.teks.lower())) & KATA_HOOK or "?" in x.teks]
            hk = (kena[0] if kena else (isi[0] if isi else k)).mulai
        daftar.append({**asdict(k), "no": i, "hook": round(float(hk), 1), "file": ""})

    if (os.environ.get("SELARAS_NANTI") == "1" and getattr(a, "transkrip", "") == "gemini"
            and not getattr(a, "_sub_yt", False) and daftar):
        t0 = time.time()
        jangkar = []  # (indeks kalimat pertama, terakhir, selisih awal, selisih akhir) per klip, dari waktu kasar
        for d in daftar:
            isi = [i for i, x in enumerate(kal) if x.selesai > d["mulai"] and x.mulai < d["selesai"]]
            jangkar.append((isi[0], isi[-1], d["mulai"] - kal[isi[0]].mulai, d["selesai"] - kal[isi[-1]].selesai)
                           if isi else None)
        ok = selaraskan_bagian(file, kal, [(d["mulai"], d["selesai"]) for d in daftar])
        for d, j in zip(daftar, jangkar):  # geser batas klip mengikuti kalimat yang sudah diselaraskan
            if j:
                geser = kal[j[0]].mulai + j[2] - d["mulai"]
                d["mulai"] = round(max(0.0, kal[j[0]].mulai + j[2]), 2)
                d["selesai"] = round(kal[j[1]].selesai + j[3], 2)
                d["hook"] = round(float(d["hook"]) + geser, 1)
        log(f"Waktu subtitle klip diselaraskan ({ok} kalimat, {time.time() - t0:.0f} dtk).")

    try:
        en = energi_audio(file)
        langkah = max(len(en) // 120, 1)
        energi_ringkas = [round(float(en[i:i + langkah].mean()), 3) for i in range(0, len(en), langkah)]
    except Exception:  # noqa: BLE001
        energi_ringkas = []
    # sumber untuk pratinjau di Studio + gambar kecil untuk riwayat
    tautan = keluar / f"sumber{file.suffix}"
    if not tautan.exists():
        try:
            tautan.symlink_to(file)
        except OSError:
            import shutil
            shutil.copy(file, tautan)
    t_gambar = (klips[0].mulai + 2) if klips else min(5, panjang / 2)
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-ss", f"{t_gambar:.1f}", "-i", str(file), "-frames:v", "1",
                    "-vf", "scale=320:-2", str(keluar / "gambar.jpg")], check=False)
    (keluar / "info.json").write_text(json.dumps({
        "judul": judul, "durasi": round(panjang, 1), "lebar": w, "tinggi": h, "heatmap": heatmap,
        "energi": energi_ringkas, "ai": sumber_ai, "ringkasan": ringkasan, "hashtag": hashtag,
        "sumber": str(file), "pratinjau": tautan.name, "rentang": [ra, rb],
    }, ensure_ascii=False), encoding="utf-8")
    (keluar / "klip.json").write_text(json.dumps(daftar, ensure_ascii=False, indent=2), encoding="utf-8")
    (keluar / "transkrip.json").write_text(json.dumps([asdict(k) for k in kal], ensure_ascii=False), encoding="utf-8")
    log(f"Analisis selesai: {len(daftar)} momen.")


def siapkan_unggahan(keluar: Path, d: dict, info: dict) -> None:
    """Sampul (bingkai saat judul pembuka tampil) + teks unggahan siap tempel untuk satu klip."""
    mp4 = keluar / d["file"]
    sampul = mp4.with_name(mp4.stem + "-sampul.jpg")
    detik = 1.2 if (d["selesai"] - d["mulai"]) > 2 else 0
    r = subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", str(detik), "-i", str(mp4), "-frames:v", "1",
                        "-q:v", "3", str(sampul)], capture_output=True)
    if r.returncode == 0 and sampul.exists():
        d["sampul"] = sampul.name
    tag = [t for t in (info.get("hashtag") or []) if t not in (d.get("caption") or "")]
    teks = "\n\n".join(x for x in (d.get("judul", ""), (d.get("caption") or "").strip(), " ".join(tag)) if x)
    (mp4.with_name(mp4.stem + "-unggahan.txt")).write_text(teks + "\n", encoding="utf-8")
    d["teks_unggahan"] = mp4.stem + "-unggahan.txt"


def render_terpilih(keluar: Path, kerja: Path, pilih: str | None, gaya: dict) -> None:
    info = json.loads((keluar / "info.json").read_text(encoding="utf-8"))
    daftar = json.loads((keluar / "klip.json").read_text(encoding="utf-8"))
    kal = [Kalimat(k["mulai"], k["selesai"], k["teks"], [Kata(**x) for x in k["kata"]])
           for k in json.loads((keluar / "transkrip.json").read_text(encoding="utf-8"))]
    nomor = {int(x) for x in pilih.split(",")} if pilih else {d["no"] for d in daftar}
    file = Path(info["sumber"])
    if not file.exists():
        file = keluar / info["pratinjau"]
    w, h = info.get("lebar"), info.get("tinggi")
    if not w:
        _, w, h = info_video(file)
    target = [d for d in daftar if d["no"] in nomor]
    for d in daftar:
        if d.get("jejak"):
            _JEJAK_SIAP[(round(d["mulai"], 2), round(d["selesai"], 2))] = d["jejak"]
    for n, d in enumerate(target, 1):
        k = Klip(d["mulai"], d["selesai"], d.get("judul_banner") or d["judul"], d.get("skor", 0),
                 kata_kunci=d.get("kata_kunci") or [])
        out = keluar / f"klip-{d['no']:02d}.mp4"
        log(f"Render {n}/{len(target)} {out.name}: {k.mulai:.1f}–{k.selesai:.1f}s  “{d['judul']}”")
        sisipan, kredit = siapkan_broll(gaya, kal, k)
        if sisipan:
            log(f"B-roll: {len(sisipan)} sisipan ({', '.join(x['kunci'] for x in sisipan)}).")
        render(file, k, kal, out, kerja, w, h, {**gaya, "_sisipan": sisipan})
        d["file"] = out.name
        d["broll"] = [{"detik": round(x["a"], 1), "kunci": x["kunci"], "file": Path(x["file"]).name} for x in sisipan]
        if kredit:
            d["kredit"] = kredit
            if kredit not in (d.get("caption") or ""):
                d["caption"] = ((d.get("caption") or "") + f"\n{kredit}").strip()
        siapkan_unggahan(keluar, d, info)
        (keluar / "klip.json").write_text(json.dumps(daftar, ensure_ascii=False, indent=2), encoding="utf-8")
    (keluar / "gaya.json").write_text(json.dumps(gaya, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Selesai. {len(target)} klip di {keluar}")


def main() -> None:
    p = argparse.ArgumentParser(description="Potong video panjang jadi klip pendek 9:16.")
    p.add_argument("sumber", nargs="?", help="file video atau link YouTube")
    p.add_argument("--jumlah", type=int, default=3, help="jumlah klip (default 3)")
    p.add_argument("--durasi", type=int, default=30, help="target detik per klip (default 30)")
    p.add_argument("--model", default="small", help="model whisper: tiny/base/small/medium/large-v3")
    p.add_argument("--bahasa", default="id", help="kode bahasa, mis. id atau en; 'auto' = deteksi otomatis (default id)")
    p.add_argument("--transkrip", choices=["whisper", "gemini"], default="whisper",
                   help="mesin transkripsi: whisper (lokal, default) atau gemini (audio dikirim ke Gemini, butuh GEMINI_API_KEY; "
                        "waktu subtitle diselaraskan ke audio bila torch+torchaudio terpasang)")
    p.add_argument("--kunci-gemini", help=argparse.SUPPRESS)  # untuk worker; lebih aman lewat GEMINI_API_KEY di lingkungan
    p.add_argument("--keluar", default="hasil-klip", help="folder hasil")
    p.add_argument("--tanpa-wajah", action="store_true", help="bingkai tengah, jangan ikuti wajah")
    p.add_argument("--tanpa-subtitle", action="store_true")
    p.add_argument("--warna", default=None, help="warna sorot subtitle (hex); kosong = warna preset")
    p.add_argument("--hanya-prompt", action="store_true", help="Opsi B: tulis prompt Claude lalu berhenti")
    p.add_argument("--pilihan", help="Opsi B: JSON momen dari Claude")
    p.add_argument("--segmen", type=float, default=0,
                   help="mode live/VOD panjang: analisis AI per N menit (0 = otomatis untuk video > 40 menit, -1 = mati)")
    p.add_argument("--arahan", default="", help="momen yang dicari, mis. 'momen lucu' atau 'tips praktis'")
    p.add_argument("--ai", default=None, help="pemilih momen: lokal (default), gemini, openai (Ollama dll.), "
                        "claude-code (langganan Claude), codex (langganan ChatGPT), opencode")
    p.add_argument("--rentang", help="bagian video yang dianalisis, mis. 10:00-25:30")
    p.add_argument("--subtitle-file", help="pakai subtitle .srt sendiri (lewati transkripsi)")
    p.add_argument("--hanya-analisis", action="store_true", help="cari momen tanpa render")
    p.add_argument("--render", action="store_true", help="render dari hasil analisis di --keluar")
    p.add_argument("--jejak", type=int, help="hitung jejak wajah klip nomor N (disimpan di klip.json, dipakai pratinjau)")
    p.add_argument("--pilih", help="nomor klip yang dirender, mis. 1,3,4 (default semua)")
    p.add_argument("--uji-ai", help="uji sambungan AI (lokal/gemini/openai/claude-code/codex/opencode)")
    p.add_argument("--broll", choices=["pixabay", "file"], help="sisipkan B-roll: pixabay (butuh PIXABAY_API_KEY) atau file sendiri")
    p.add_argument("--broll-file", nargs="*", default=[], help="video/foto milikmu untuk B-roll (dengan --broll file)")
    p.add_argument("--watermark", help="teks kecil di pojok kanan bawah, mis. @akunmu")
    p.add_argument("--gaya", help="JSON gaya render (rasio, latar, fokus, layout, banner, subtitle, musik, …)")
    a = p.parse_args()

    if a.uji_ai:
        if a.uji_ai == "lokal":
            print("SIAP (mode lokal tidak memakai AI luar)")
            return
        jawab = tanya_ai(a.uji_ai, "Ini uji sambungan. Balas persis satu kata saja: SIAP").strip()
        print(jawab[:200])
        return

    keluar = Path(a.keluar).resolve()
    kerja = keluar / ".kerja"
    kerja.mkdir(parents=True, exist_ok=True)
    gaya = {**({"warna": a.warna} if a.warna else {}), "subtitle": not a.tanpa_subtitle, "fokus": "tengah" if a.tanpa_wajah else "auto",
            "banner": False, **({"broll": a.broll, "broll_file": a.broll_file} if a.broll else {}),
            **({"watermark": a.watermark} if a.watermark else {})}
    if a.gaya:
        isi = a.gaya if a.gaya.lstrip().startswith("{") else Path(a.gaya).read_text(encoding="utf-8")
        gaya.update(json.loads(isi))

    if a.jejak:
        info = json.loads((keluar / "info.json").read_text(encoding="utf-8"))
        daftar = json.loads((keluar / "klip.json").read_text(encoding="utf-8"))
        d = next(x for x in daftar if x["no"] == a.jejak)
        file = Path(info["sumber"])
        if not file.exists():
            file = keluar / info["pratinjau"]
        w, h = info.get("lebar"), info.get("tinggi")
        if not w:
            _, w, h = info_video(file)
        d["jejak"] = [[round(t, 2), round(x, 3)] for t, x in jejak_wajah(file, d["mulai"], d["selesai"], w, h)]
        (keluar / "klip.json").write_text(json.dumps(daftar, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(d["jejak"]))
        return
    if a.render:
        render_terpilih(keluar, kerja, a.pilih, gaya)
        return
    if not a.sumber:
        sys.exit("Isi file video atau link YouTube.")
    analisis(a, keluar, kerja)
    if a.hanya_analisis or a.hanya_prompt:
        return
    render_terpilih(keluar, kerja, a.pilih, gaya)


if __name__ == "__main__":
    main()
