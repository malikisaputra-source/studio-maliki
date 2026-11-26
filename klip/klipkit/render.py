"""Subtitle ASS, B-roll, dan render klip."""
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
from .wajah import *  # noqa: F401,F403
from .umum import AKAR

# ── 6. Subtitle & banner (ASS) ──────────────────────────────────────────────
def waktu_ass(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def warna_ass(hexa: str, alfa: str = "00") -> str:
    hexa = (hexa or "#FFFFFF").lstrip("#")
    return f"&H{alfa}{hexa[4:6]}{hexa[2:4]}{hexa[0:2]}".upper()


def tanpa_emoji(t: str) -> str:
    # libass tidak bisa menggambar emoji berwarna (muncul kotak) → buang
    return re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", t).strip()


def ubah_huruf(t: str, mode: str) -> str:
    t = tanpa_emoji(t)
    return t.upper() if mode == "kapital" else t.lower() if mode == "kecil" else t


UKURAN = {"kecil": 0.82, "sedang": 1.0, "besar": 1.22}  # pengali ukuran preset
FONT_DIR = AKAR / "font"  # font OFL dibundel → hasil sama di Mac & Windows

# Preset "Gaya klip" — pola dari riset dokumentasi/26 (bukan tiruan tampilan alat/kreator mana pun).
# mode: warna (kata aktif berwarna+membesar) · pop (grup memantul masuk, kata kunci berwarna) · komik (tepi tebal,
# kata kunci berwarna, kata aktif memantul) · tebal (kata aktif tipis→tebal) · karaoke (warna mengisi kata) ·
# terbaca (kotak hitam, teks kuning, kata aktif putih) · polos (tanpa sorot)
PRESET = {
    "sorot":   dict(font="Archivo Black", tebal=0, huruf="kapital", per=3, uk=78, warna="#FFD400", mode="warna", tepi=6, bayang=2),
    "pop":     dict(font="Anton", tebal=0, huruf="kapital", per=2, uk=112, warna="#FFD400", mode="pop", tepi=4, bayang=5),
    "komik":   dict(font="Bangers", tebal=0, huruf="kapital", per=3, uk=104, warna="#3BE477", mode="komik", tepi=10, bayang=0, spasi=2),
    "halus":   dict(font="Montserrat Light", font_aktif="Montserrat", tebal=0, huruf="kecil", per=4, uk=62, warna="#FFFFFF",
                    mode="tebal", tepi=1, bayang=4),
    "karaoke": dict(font="Montserrat ExtraBold", tebal=0, huruf="biasa", per=6, uk=66, warna="#FFD400", mode="karaoke", tepi=5, bayang=0),
    "terbaca": dict(font="Montserrat ExtraBold", tebal=0, huruf="kapital", per=4, uk=66, warna="#FFE600", mode="terbaca",
                    kotak="&H00000000", tepi=14, bayang=0),
    "minimal": dict(font="Montserrat Medium", tebal=0, huruf="biasa", per=6, uk=60, warna="#FFFFFF", mode="polos", tepi=2, bayang=3),
    "podcast": dict(font="Montserrat ExtraBold", tebal=0, huruf="kapital", per=3, uk=74, warna="#FFD400", mode="warna", tepi=5,
                    bayang=0, latar="blur", banner_kotak="&H00000000", progres=True),
    "maliki":  dict(font="Space Grotesk", tebal=1, huruf="biasa", per=3, uk=72, warna="#22D3EE", mode="warna",
                    kotak="&H38170F0B", tepi=16, bayang=0, banner_kotak="&H38170F0B", banner_warna="#22D3EE", progres=True),
    # 2.1 — kotak sorot mengikuti kata aktif, hook neon + coretan, judul serif elegan, judul kertas sobek
    "lime":    dict(font="Montserrat ExtraBold", tebal=0, huruf="kapital", per=3, uk=76, warna="#C6FF3D", mode="kotak", tepi=4, bayang=0),
    "neon":    dict(font="Bangers", tebal=0, huruf="kapital", per=2, uk=108, warna="#FF4FA3", mode="pop", tepi=8, bayang=0,
                    judul="neon"),
    "elegan":  dict(font="Georgia", tebal=1, huruf="biasa", per=4, uk=68, warna="#FFD400", mode="warna", tepi=3, bayang=3,
                    judul="serif"),
    "sobek":   dict(font="Archivo Black", tebal=0, huruf="kapital", per=3, uk=74, warna="#FF4FA3", mode="warna", tepi=6, bayang=2,
                    judul="sobek"),
}
GAYA_JUDUL = ("kuning", "neon", "serif", "kotak", "sobek")  # gaya judul pembuka 3 dtk
ANIM_DIR = AKAR / "anim"  # hiasan animasi transparan (WebP 540×960, 30 fps) dari Remotion
LAMA = {"sorot": "sorot", "kotak": "terbaca", "garis": "minimal"}  # gaya_sub versi lama → preset


def gaya_akhir(g: dict) -> dict:
    """Preset sebagai dasar, lalu pilihan manual pengguna menimpanya."""
    nama = g.get("preset") or LAMA.get(g.get("gaya_sub", "sorot"), "sorot")
    c = {**PRESET.get(nama, PRESET["sorot"]), "preset": nama}
    for k in ("font", "warna", "huruf", "latar"):
        if g.get(k):
            if k == "font" and g[k] != c["font"]:
                c.pop("font_aktif", None)
                c["tebal"] = 1 if g[k] not in ("Archivo Black", "Anton", "Bangers") and "Bold" not in g[k] else 0
            c[k] = g[k]
    c.setdefault("progres", True)
    c.setdefault("zoom", True)
    for k in ("progres", "zoom", "akun", "emoji", "watermark"):
        if k in g:
            c[k] = g[k]
    c.setdefault("emoji", True)
    c.setdefault("judul", "kuning")
    if g.get("gaya_judul") in GAYA_JUDUL:
        c["judul"] = g["gaya_judul"]
    c["uk"] = int(c["uk"] * UKURAN.get(g.get("ukuran", "sedang"), 1.0))
    return c


def lama_judul(c: dict) -> float:
    return 2.5 if c.get("judul") == "sobek" else 3.0


def baris_judul(teks: str, c: dict, panjang: float) -> str:
    """Satu baris Dialogue ASS untuk judul pembuka sesuai gaya (animasi masuk lewat tag ASS)."""
    t = waktu_ass
    j = c.get("judul", "kuning")
    sl = min(lama_judul(c), panjang)
    if j == "neon":
        uk = max(min(int(5200 / max(len(teks), 1)), 210), 120)  # boleh 2 baris
        return (f"Dialogue: 2,{t(0)},{t(sl)},JudulNeon,,0,0,0,,{{\\an5\\pos(540,840)\\frz-4\\fs{uk}\\fscx30\\fscy30"
                f"\\t(0,180,\\fscx110\\fscy110)\\t(180,320,\\fscx100\\fscy100)\\fad(0,250)}}{teks.upper()}")
    if j == "serif":
        kata = teks.split()
        if len(kata) >= 2:  # kata kedua miring, seperti majalah
            kata[1] = "{\\i1}" + kata[1] + "{\\i0}"
        uk = max(min(int(2600 / max(len(teks), 1)), 104), 58)
        return (f"Dialogue: 2,{t(0)},{t(sl)},JudulSerif,,0,0,0,,{{\\an5\\move(540,890,540,840,0,350)\\fs{uk}\\fad(250,300)}}"
                + " ".join(kata))
    if j == "kotak":
        return (f"Dialogue: 2,{t(0)},{t(sl)},JudulKotak,,0,0,0,,{{\\an5\\pos(540,840)\\fscx80\\fscy80\\t(0,150,\\fscx100\\fscy100)"
                f"\\fad(0,250)}}{teks.upper()}")
    if j == "sobek":  # teks di atas pita kertas (anim/sobek): masuk 0,3 dtk, keluar 2,1 dtk
        uk = max(min(int(1500 / max(len(teks), 1)), 92), 48)
        return (f"Dialogue: 2,{t(0.3)},{t(min(2.1, sl))},JudulSobek,,0,0,0,,{{\\an5\\pos(540,880)\\frz3\\fs{uk}\\fad(120,120)}}"
                f"{teks.upper()}")
    return f"Dialogue: 2,{t(0)},{t(sl)},Judul,,0,0,0,,{{\\fad(150,250)}}{teks}"


def _norm(t: str) -> str:
    return re.sub(r"[^\w]", "", t.lower())


KATA_UMUM = set("yang dengan untuk karena tetapi kemudian sebenarnya mereka sendiri seperti bagaimana apakah sesuatu memang begitu "
                "akhirnya ternyata pokoknya soalnya makanya sebelumnya sesudahnya biasanya because really actually something".split())


def kunci_klip(kata: list, klip: Klip) -> set:
    """Kata kunci dari AI; kalau tidak ada (mode Lokal), pilih kata panjang/angka ±1 per 6 kata."""
    if klip.kata_kunci:
        return {_norm(w) for x in klip.kata_kunci for w in str(x).split()
                if (len(_norm(w)) >= 3 or _norm(w).isdigit()) and _norm(w) not in KATA_UMUM | {"sih", "dong", "deh", "nih", "kok", "yang"}}
    hasil = set()
    for i in range(0, len(kata), 6):
        calon = [_norm(k.teks) for k in kata[i:i + 6]]
        calon = [c for c in calon if (any(ch.isdigit() for ch in c) or len(c) >= 7) and c not in KATA_UMUM]
        if calon:
            hasil.add(max(calon, key=lambda c: (any(ch.isdigit() for ch in c), len(c))))
    return hasil


HIJAU = warna_ass("#3DFF8A")

EMOJI = [  # kata (awalan) → emoji, sama dengan aplikasi Klip HP; gambar Twemoji di folder emoji/
    ("uang|duit|gaji|kaya|cuan|bisnis|jual|untung", "1f4b0"), ("cinta|sayang|hati|nikah|istri|suami", "2764"),
    ("api|panas|viral|hebat|keren", "1f525"), ("ide|pikir|otak|ilmu|belajar|paham", "1f4a1"),
    ("waktu|jam|cepat|lambat|telat", "23f0"), ("rahasia|diam|bisik", "1f92b"), ("penting|bahaya|awas|hati-hati", "26a0"),
    ("salah|jangan|dilarang|gagal", "274c"), ("benar|betul|berhasil|sukses|bisa", "2705"), ("lucu|ketawa|tertawa|ngakak", "1f602"),
    ("ternyata|kaget|gila|parah|serius", "1f631"), ("allah|tuhan|doa|sholat|salat|ibadah|quran|alquran|nabi|islam", "1f932"),
    ("makan|lapar|masak|kopi", "1f37d"), ("kerja|kantor|karier|karir", "1f4bc"), ("sehat|olahraga|kuat|otot", "1f4aa"),
    ("uji|tes|soal|sekolah|kuliah|buku", "1f4da"), ("rumah|keluarga|anak|ibu|ayah|bapak", "1f3e0"),
    ("hp|handphone|aplikasi|internet|ai", "1f4f1"), ("mati|meninggal|takut|seram|hantu|jin|setan|iblis", "1f47b"),
    ("mahal|murah|harga", "1f3f7"),
]
EMOJI_RE = [(re.compile(f"^({pola})"), kode) for pola, kode in EMOJI]
EMOJI_DIR = AKAR / "emoji"


def emoji_untuk(w: str) -> str | None:
    b = re.sub(r"[^a-z\-]", "", w.lower())
    return next((kode for pola, kode in EMOJI_RE if b and pola.match(b)), None)


def rencana_emoji(kal: list[Kalimat], klip: Klip, per: int) -> list[tuple[float, float, str]]:
    """(mulai, selesai, kode) relatif klip: tiap grup subtitle yang berisi kata ber-emoji."""
    kata = [k for s in kal for k in s.kata if k.selesai > klip.mulai and k.mulai < klip.selesai]
    out = []
    for i in range(0, len(kata), per):
        grup = kata[i:i + per]
        kode = next((e for e in (emoji_untuk(x.teks) for x in grup) if e), None)
        if kode and (EMOJI_DIR / f"{kode}.png").exists():
            out.append((max(grup[0].mulai - klip.mulai, 0), grup[-1].selesai - klip.mulai, kode))
    return out


def tulis_ass(kal: list[Kalimat], klip: Klip, out: Path, g: dict, bar_atas: int) -> None:
    c = gaya_akhir(g)
    font, uk, mode = c["font"], c["uk"], c["mode"]
    sorot = warna_ass(c["warna"])
    putih = "&H00FFFFFF"
    # posisi_y = persen dari atas → MarginV untuk perataan bawah
    marg = int(1920 * (1 - float(g.get("posisi_y", 72)) / 100))
    spasi = c.get("spasi", 0)
    if c.get("kotak"):  # BorderStyle 3: kotak memakai OutlineColour
        utama = (f"Style: Utama,{font},{uk},{sorot if mode == 'terbaca' else putih},{putih},{c['kotak']},{c['kotak']},"
                 f"{c['tebal']},0,0,0,100,100,{spasi},0,3,{c['tepi']},0,2,90,90,{marg},1")
    elif mode == "karaoke":  # Primary = sudah terisi, Secondary = belum
        utama = f"Style: Utama,{font},{uk},{sorot},{putih},&H00000000,&H64000000,{c['tebal']},0,0,0,100,100,0,0,1,{c['tepi']},{c['bayang']},2,90,90,{marg},1"
    else:
        bayang = "&H78000000" if c["bayang"] else "&H64000000"
        utama = (f"Style: Utama,{font},{uk},{putih},{putih},&H00000000,{bayang},{c['tebal']},0,0,0,100,100,{spasi},0,1,"
                 f"{c['tepi']},{c['bayang']},2,90,90,{marg},1")
    banner_v = max(70, bar_atas // 2 - 70) if bar_atas > 160 else 150
    bw = warna_ass(c.get("banner_warna", "#FFFFFF"))
    fb, tb = (c["font_aktif"], 1) if c.get("font_aktif") else (font, c["tebal"])  # banner selalu tebal
    if c.get("banner_kotak"):
        banner = f"Style: Banner,{fb},{int(uk * 0.72)},{bw},{bw},{c['banner_kotak']},{c['banner_kotak']},{tb},0,0,0,100,100,0,0,3,18,0,8,80,80,{banner_v},1"
    else:
        banner = f"Style: Banner,{fb},{max(int(uk * 0.85), 54)},{bw},{bw},&H00000000,&H00000000,{tb},0,0,0,100,100,0,0,1,5,0,8,80,80,{banner_v},1"
    judul_style = (f"Style: Judul,{fb},54,&H000D0705,&H000D0705,{warna_ass('#FFD400')},{warna_ass('#FFD400')},1,0,0,0,100,100,0,0,3,"
                   f"20,0,8,110,110,{int(1920 * 0.14)},1")
    judul_style += (
        f"\nStyle: JudulNeon,Bangers,110,{warna_ass('#FF4FA3')},{warna_ass('#FF4FA3')},&H00111111,{warna_ass('#C6FF3D')},0,0,0,0,"
        f"100,100,2,0,1,10,8,5,70,70,0,1"
        f"\nStyle: JudulSerif,Georgia,104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,1,0,0,0,100,100,0,0,1,0,4,5,90,90,0,1"
        f"\nStyle: JudulKotak,Archivo Black,72,&H00FFFFFF,&H00FFFFFF,&H00111111,&H00111111,0,0,0,0,100,100,0,0,3,22,0,5,100,100,0,1"
        f"\nStyle: JudulSobek,Archivo Black,84,&H00111111,&H00111111,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,120,120,0,1")
    tanda_style = "Style: Tanda,Montserrat,30,&H33FFFFFF,&H33FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,1,2,3,40,40,46,1"
    akun_style = f"Style: Akun,{font},34,&H40FFFFFF,&H40FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,2,0,8,80,80,{banner_v + int(uk * 1.1)},1"
    kepala = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{utama}
{banner}
{akun_style}
{judul_style}
{tanda_style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    a, b = klip.mulai, klip.selesai
    baris = []
    bersih = lambda t: t.replace("{", "").replace("}", "")  # noqa: E731
    if g.get("banner", True) and klip.judul:
        teks = bersih(f"{g.get('awalan', '')}{klip.judul}{g.get('akhiran', '')}")
        baris.append(f"Dialogue: 1,{waktu_ass(0)},{waktu_ass(b - a)},Banner,,0,0,0,,{ubah_huruf(teks, 'kapital')}")
    elif g.get("judul_buka", True) and klip.judul:  # judul pembuka 3 dtk (bila banner tidak dipakai)
        teks = bersih(tanpa_emoji(klip.judul))
        baris.append(baris_judul(teks, c, b - a))
    if c.get("watermark"):
        baris.append(f"Dialogue: 2,{waktu_ass(0)},{waktu_ass(b - a)},Tanda,,0,0,0,,{bersih(tanpa_emoji(str(c['watermark'])))[:40]}")
    if c.get("akun"):
        baris.append(f"Dialogue: 1,{waktu_ass(0)},{waktu_ass(b - a)},Akun,,0,0,0,,{bersih(str(c['akun']))[:40]}")
    if g.get("subtitle", True):
        kata = [k for s in kal for k in s.kata if k.selesai > a and k.mulai < b]
        kunci = kunci_klip(kata, klip)
        per = c["per"]
        huruf = c.get("huruf", "kapital")
        for i in range(0, len(kata), per):
            grup = kata[i:i + per]
            teks = [bersih(ubah_huruf(x.teks, huruf)) for x in grup]
            if mode == "karaoke":
                isi = []
                for j, x in enumerate(grup):
                    dur = ((grup[j + 1].mulai if j + 1 < len(grup) else x.selesai) - x.mulai)
                    isi.append("{\\kf" + str(max(int(dur * 100), 1)) + "}" + teks[j])
                if grup[-1].selesai - grup[0].mulai >= 0.02:
                    baris.append(f"Dialogue: 0,{waktu_ass(grup[0].mulai - a)},{waktu_ass(grup[-1].selesai - a)},Utama,,0,0,0,,{' '.join(isi)}")
                continue
            for j, aktif in enumerate(grup):
                m = aktif.mulai - a
                sl = (grup[j + 1].mulai if j + 1 < len(grup) else grup[-1].selesai) - a
                if sl - m < 0.02:  # baris berdurasi nol membuat libass menghilangkan judul pembuka → lewati
                    continue
                isi = []
                for x, t in zip(grup, teks):
                    kk = _norm(x.teks) in kunci
                    hijau = "{\\c" + HIJAU + "&}" + t + "{\\r}" if kk else t  # kata kunci = hijau
                    if mode == "polos":
                        isi.append(hijau)
                    elif mode == "tebal":
                        isi.append(("{\\fn" + c["font_aktif"] + "\\b1}" if c.get("font_aktif") else "{\\b1}") + t + "{\\r}"
                                   if x is aktif else t)
                    elif mode == "terbaca":
                        isi.append("{\\c" + putih + "&}" + t + "{\\r}" if x is aktif else hijau)
                    elif mode == "kotak":  # kotak warna di belakang kata aktif (tepi tebal sewarna = blok), teks gelap
                        isi.append("{\\c&H00111111&\\3c" + sorot + "&\\bord18\\shad0\\fscx104\\fscy104}" + t + "{\\r}"
                                   if x is aktif else hijau)
                    elif mode in ("pop", "komik"):
                        warna = "{\\c" + HIJAU + "&}" if kk else ""
                        if x is aktif:
                            skala = "125" if mode == "komik" else "110"
                            isi.append("{" + warna.strip("{}") + "\\fscx" + skala + "\\fscy" + skala +
                                       "\\t(0,110,\\fscx106\\fscy106)}" + t + "{\\r}")
                        else:
                            isi.append(warna + t + ("{\\r}" if warna else ""))
                    else:  # warna
                        isi.append("{\\c" + sorot + "&\\fscx110\\fscy110}" + t + "{\\r}" if x is aktif else hijau)
                awal = "{\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)}" if mode == "pop" and j == 0 else ""
                baris.append(f"Dialogue: 0,{waktu_ass(m)},{waktu_ass(sl)},Utama,,0,0,0,,{awal}{' '.join(isi)}")
    out.write_text(kepala + "\n".join(baris) + "\n", encoding="utf-8")


def detik_kunci(kal: list[Kalimat], klip: Klip) -> list[float]:
    """Detik (relatif klip) kata pemikat (hook) — atau kata kunci bila tidak ada — untuk zoom halus. Jarak ≥ 3 dtk, maks 8."""
    kata = [k for s in kal for k in s.kata if k.selesai > klip.mulai and k.mulai < klip.selesai]
    calon = [k for k in kata if is_hook(k.teks)]
    if not calon:
        kunci = kunci_klip(kata, klip)
        calon = [k for k in kata if _norm(k.teks) in kunci]
    hasil: list[float] = []
    for k in calon:
        if not hasil or k.mulai - klip.mulai - hasil[-1] >= 3:
            hasil.append(max(k.mulai - klip.mulai, 0))
    return hasil[:8]


# ── 6b. B-roll: file sendiri atau Pixabay (kunci API milik pengguna) ──────────
STOP_BROLL = set("""yang dan di ke dari ini itu dengan untuk pada adalah akan juga tidak nggak gak enggak kita kami kamu saya aku dia
mereka anda ada sudah udah belum bisa lagi kalau kalo karena jadi atau tapi tetapi seperti kayak banyak sangat banget sekali lebih
paling semua setiap masih harus mau ingin pernah sama begitu gitu begini gini terus lalu kemudian sih dong kok nah ya iya oke aja
saja memang emang sebenarnya mungkin kenapa gimana bagaimana apa siapa mana kapan orang hal cara waktu sesuatu namanya tadi nanti
sekarang dulu the and that this with for you""".split())

# Kamus kata benda konkret (Indonesia → kueri Inggris), sama dengan aplikasi Klip HP. Kata di luar kamus tidak diberi B-roll stok.
KAMUS_BROLL = {
    "jin": "dark smoke mystery", "setan": "dark shadow horror", "hantu": "ghost dark night", "iblis": "fire darkness",
    "malaikat": "heavenly light clouds", "masjid": "mosque", "doa": "praying hands", "sholat": "muslim prayer",
    "salat": "muslim prayer", "quran": "quran reading", "alquran": "quran reading", "allah": "sky light clouds",
    "surga": "paradise nature", "neraka": "fire flames", "langit": "sky clouds", "bintang": "night stars", "bulan": "moon night",
    "matahari": "sunrise", "hujan": "rain", "laut": "ocean waves", "gunung": "mountains", "hutan": "forest", "pantai": "beach",
    "kota": "city traffic", "jalan": "street walking", "malam": "city night", "uang": "money cash", "duit": "money cash",
    "gaji": "salary money", "bisnis": "business meeting", "kantor": "office work", "kerja": "working laptop", "toko": "small shop",
    "mobil": "car driving", "motor": "motorcycle", "pesawat": "airplane", "kereta": "train", "rumah": "house", "keluarga": "family",
    "anak": "children playing", "ibu": "mother", "ayah": "father", "istri": "wife couple", "suami": "husband couple",
    "cewek": "woman", "perempuan": "woman", "wanita": "woman", "pacar": "couple love", "cowok": "man", "laki": "man",
    "nikah": "wedding", "pernikahan": "wedding", "cinta": "love couple", "hati": "heart", "sekolah": "school",
    "kuliah": "university students", "buku": "books reading", "belajar": "studying", "guru": "teacher", "dokter": "doctor",
    "rumahsakit": "hospital", "sakit": "sick person", "makan": "eating food", "makanan": "food", "kopi": "coffee",
    "hp": "smartphone", "handphone": "smartphone", "internet": "internet technology", "komputer": "computer",
    "ai": "artificial intelligence", "robot": "robot", "olahraga": "workout", "lari": "running", "sepakbola": "football",
    "bola": "football", "musik": "music", "api": "fire", "air": "water", "waktu": "clock time", "jam": "clock",
    "tidur": "sleeping", "mimpi": "dream surreal", "takut": "fear dark", "marah": "angry", "sedih": "sad rain",
    "senang": "happy people", "tertawa": "laughing", "perang": "war", "dunia": "earth globe", "negara": "flag",
    "indonesia": "indonesia", "jakarta": "jakarta", "pasar": "market", "petani": "farmer", "sawah": "rice field",
}
GAMBAR = (".jpg", ".jpeg", ".png", ".webp")


def rencana_broll(kata: list[Kata], mulai: float, durasi: float, lama: float = 2.4, jarak: float = 7.0,
                  kamus: bool = False) -> list[tuple[float, str]]:
    """Titik sisip tiap ±8 dtk (setelah 4 dtk pertama, supaya pembicara tetap jadi pembuka) → [(detik relatif, kueri)]."""
    bersih = lambda w: re.sub(r"[^a-z]", "", w.lower())  # noqa: E731
    out, t, dipakai = [], 4.0, set()
    while t + lama < durasi - 1.5:
        jendela = [(bersih(k.teks), k.mulai - mulai) for k in kata if t - 0.5 <= k.mulai - mulai < t + lama + 2.0]
        if kamus:
            calon = next(((w, KAMUS_BROLL[w], tk) for w, tk in jendela if w in KAMUS_BROLL and w not in dipakai), None)
        else:
            cocok = [(w, tk) for w, tk in jendela if len(w) >= 4 and w not in STOP_BROLL and w not in dipakai]
            calon = (lambda x: (x[0], x[0], x[1]))(max(cocok, key=lambda x: len(x[0]))) if cocok else None
        if calon and calon[2] + lama < durasi - 0.5:
            dipakai.add(calon[0])
            out.append((max(calon[2], 0.0), calon[1]))
            t = calon[2] + lama + jarak
        else:
            t += 1.5
    return out


def cache_pixabay() -> Path:
    d = Path.home() / ".studio-maliki" / "pixabay"
    d.mkdir(parents=True, exist_ok=True)
    batas = time.time() - 24 * 3600  # aturan Pixabay: cache maksimal 24 jam
    for f in d.iterdir():
        if f.stat().st_mtime < batas:
            f.unlink(missing_ok=True)
    return d


def cari_pixabay(kunci: str, q: str, cache: Path) -> tuple[int, str] | None:
    import urllib.error
    import urllib.parse
    import urllib.request
    cj = cache / ("q_" + re.sub(r"[^0-9A-Za-z]", "", q) + ".json")
    if cj.exists():
        teks = cj.read_text(encoding="utf-8")
    else:
        url = ("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
            {"key": kunci, "q": q, "lang": "en", "video_type": "film", "safesearch": "true", "per_page": 10}))
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                teks = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                sys.exit("API key Pixabay ditolak. Periksa lagi kuncinya di Pengaturan.")
            if e.code == 429:
                log("Batas permintaan Pixabay tercapai, B-roll dilewati.")
            return None
        except OSError:
            return None
        cj.write_text(teks, encoding="utf-8")
    for h in json.loads(teks).get("hits", []):
        if (h.get("duration") or 0) < 3 or h.get("type") == "animation":
            continue
        v = h.get("videos") or {}
        pilih = next((v[k]["url"] for k in ("medium", "small", "large") if v.get(k, {}).get("url")), None)
        if pilih:
            return int(h["id"]), pilih
    return None


def siapkan_broll(g: dict, kal: list[Kalimat], klip: Klip) -> tuple[list[dict], str]:
    """Kembalikan (sisipan, kredit). g["broll"] = "pixabay" atau "file"; g["broll_file"] = daftar path."""
    jenis = g.get("broll")
    if not jenis:
        return [], ""
    lama = float(g.get("broll_lama", 2.4))
    durasi = klip.selesai - klip.mulai
    kata = [k for s in kal for k in s.kata if k.selesai > klip.mulai and k.mulai < klip.selesai]
    if jenis == "file":
        files = [f for f in (g.get("broll_file") or []) if Path(f).exists()]
        if not files:
            return [], ""
        rencana = rencana_broll(kata, klip.mulai, durasi, lama)
        return [{"a": t, "b": t + lama, "file": str(Path(files[i % len(files)]).resolve()), "kunci": q,
                 "gambar": Path(files[i % len(files)]).suffix.lower() in GAMBAR} for i, (t, q) in enumerate(rencana)], ""
    kunci = g.get("pixabay_key") or baca_env().get("PIXABAY_API_KEY")
    if not kunci:
        log("B-roll Pixabay dilewati: isi API key Pixabay dulu (gratis di pixabay.com/api/docs).")
        return [], ""
    import urllib.request
    cache = cache_pixabay()
    hasil: list[dict] = []
    rencana = rencana_broll(kata, klip.mulai, durasi, lama, kamus=True)
    for i, (t, q) in enumerate(rencana, 1):
        log(f"Mencari B-roll “{q}” di Pixabay ({i}/{len(rencana)})…")
        ketemu = cari_pixabay(kunci, q, cache)
        if not ketemu:
            continue
        vid, link = ketemu
        f = cache / f"pixabay_{vid}.mp4"
        if not f.exists() or f.stat().st_size < 10_000:
            try:
                with urllib.request.urlopen(link, timeout=30) as r, open(f, "wb") as out:
                    out.write(r.read())
            except OSError:
                f.unlink(missing_ok=True)
                continue
        if all(h["file"] != str(f) for h in hasil):
            hasil.append({"a": t, "b": t + lama, "file": str(f), "kunci": q, "gambar": False})
    return hasil, ("Video dari Pixabay" if hasil else "")


# ── 7. Render satu klip dengan gaya ─────────────────────────────────────────
RASIO = {"9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "16:9": 16 / 9}
FOKUS = {"kiri": 0.35, "tengah": 0.5, "kanan": 0.65}


def render(file: Path, klip: Klip, kal: list[Kalimat], out: Path, kerja: Path, w: int, h: int, g: dict) -> None:
    rasio = g.get("rasio", "9:16")
    layout = g.get("layout", "tunggal")
    fokus = g.get("fokus", "auto")
    wajah = info_wajah(file, klip.mulai, klip.selesai, w, h) if layout in ("facecam", "pip") else None
    dua = dua_wajah(file, klip.mulai, klip.selesai, w, h) if layout == "split" else None
    if layout == "split" and not dua:
        log("   Split Podcast: hanya satu orang terlihat → bingkai tunggal")
    cx = FOKUS.get(fokus, 0.5)
    durasi = klip.selesai - klip.mulai
    graf = []
    bar_atas = 0

    if layout == "split" and dua:
        # dua pembicara ditumpuk: kiri di atas, kanan di bawah, garis pemisah tipis
        graf.append("[0:v]split=2[va][vb]")
        for lbl, p in (("atas", dua[0]), ("bawah", dua[1])):
            zoom = min(max(p["s"] * 3.0, 0.3), 1.0)
            graf.append(f"[{'va' if lbl == 'atas' else 'vb'}]{potong(w, h, 1080 / 958, p['x'], p['y'], zoom)},"
                        f"scale=1080:958:flags=lanczos,setsar=1[{lbl}]")
        graf.append("color=c=0x111111:s=1080x4:d=1[gp];[atas][gp][bawah]vstack=inputs=3[vid]")
    elif layout == "facecam" and wajah:
        # atas 40%: wajah diperbesar · bawah 60%: isi layar (gameplay/presentasi)
        zoom = min(max(wajah["s"] * 3.2, 0.25), 1.0)
        graf.append(f"[0:v]split=2[va][vb]")
        graf.append(f"[va]{potong(w, h, 1080 / 768, wajah['x'], wajah['y'], zoom)},scale=1080:768,setsar=1[atas]")
        graf.append(f"[vb]{potong(w, h, 1080 / 1152, 0.5, 0.5)},scale=1080:1152,setsar=1[bawah]")
        graf.append("[atas][bawah]vstack=inputs=2[vid]")
    elif layout == "pip" and wajah:
        zoom = min(max(wajah["s"] * 2.4, 0.2), 1.0)
        graf.append(f"[0:v]split=2[va][vb]")
        graf.append(f"[va]{potong(w, h, 9 / 16, 0.5, 0.5)},scale=1080:1920,setsar=1[utama]")
        graf.append(f"[vb]{potong(w, h, 1.0, wajah['x'], wajah['y'], zoom)},scale=380:380,setsar=1,"
                    f"pad=392:392:6:6:color=0x22D3EE[kecil]")
        graf.append("[utama][kecil]overlay=x=W-w-60:y=220[vid]")
    elif rasio == "9:16":
        if fokus == "auto":  # ikuti pembicara per adegan, termasuk saat kamera berganti
            crop = potong_jejak(w, h, 9 / 16, jejak_wajah(file, klip.mulai, klip.selesai, w, h))
        else:
            crop = potong(w, h, 9 / 16, cx)
        graf.append(f"[0:v]{crop},scale=1080:1920:flags=lanczos,setsar=1[vid]")
    else:
        r = RASIO.get(rasio, 1.0)
        ih = int(1080 / r) // 2 * 2
        bar_atas = (1920 - ih) // 2
        graf.append("[0:v]split=2[va][vb]")
        crop = potong_jejak(w, h, r, jejak_wajah(file, klip.mulai, klip.selesai, w, h)) if fokus == "auto" \
            else potong(w, h, r, cx)
        graf.append(f"[va]{crop},scale=1080:{ih}:flags=lanczos,setsar=1[isi]")
        if gaya_akhir(g).get("latar", "hitam") == "blur":
            graf.append(f"[vb]{potong(w, h, 9 / 16, 0.5)},scale=540:960,boxblur=20:2,scale=1080:1920,eq=brightness=-0.08,setsar=1[latar]")
        else:
            graf.append(f"[vb]scale=1080:1920,drawbox=c=black:t=fill,setsar=1[latar]")
        graf.append(f"[latar][isi]overlay=x=0:y={bar_atas}[vid]")

    c = gaya_akhir(g)
    v = "vid"  # label video saat ini di filtergraph
    # zoom punch-in singkat pada kata kunci
    if c.get("zoom"):
        tt = detik_kunci(kal, klip)
        if tt:
            import cv2
            cap = cv2.VideoCapture(str(file)); fps = cap.get(cv2.CAP_PROP_FPS) or 30; cap.release()
            # naik cepat 0,25 dtk lalu turun pelan 1,25 dtk ke 1,0 (maks 1,08)
            z = "+".join(f"if(between(it,{t - 0.1:.2f},{t + 0.15:.2f}),(it-{t - 0.1:.2f})/0.25,"
                         f"if(between(it,{t + 0.15:.2f},{t + 1.4:.2f}),1-(it-{t + 0.15:.2f})/1.25,0))" for t in tt)
            graf.append(f"[{v}]zoompan=z='1+0.08*min({z},1)':x='iw/2-(iw/zoom/2)':y='ih*0.4-(ih/zoom*0.4)':d=1:s=1080x1920:fps={fps:.3f}[vz]")
            v = "vz"
    masukan_extra: list[str] = []
    n_in = 1  # input 0 = video sumber
    # B-roll: zoom pelan 1,00→1,08 + sedikit digelapkan, menutupi video selama ±2,4 dtk
    for i, sp in enumerate(g.get("_sisipan") or []):
        lama = sp["b"] - sp["a"]
        if sp.get("gambar"):
            masukan_extra += ["-loop", "1", "-t", f"{lama:.2f}", "-i", sp["file"]]
        else:
            masukan_extra += ["-t", f"{lama:.2f}", "-i", sp["file"]]
        fr = int(lama * 30) + 1
        graf.append(f"[{n_in}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,"
                    f"zoompan=z='1+0.08*on/{fr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,"
                    f"eq=brightness=-0.10,format=yuv420p,setpts=PTS-STARTPTS+{sp['a']:.2f}/TB[br{i}]")
        graf.append(f"[{v}][br{i}]overlay=enable='between(t,{sp['a']:.2f},{sp['b']:.2f})':eof_action=pass[vb{i}]")
        v = f"vb{i}"
        n_in += 1

    def hiasan(nama: str, mulai: float, tahan: float = 0.0) -> None:
        """Tempel animasi transparan anim/<nama>/NN.webp mulai detik `mulai` (frame terakhir ditahan `tahan` dtk)."""
        nonlocal v, n_in
        if not (ANIM_DIR / nama / "00.webp").exists():
            return
        masukan_extra.extend(["-framerate", "30", "-start_number", "0", "-i", str(ANIM_DIR / nama / "%02d.webp")])
        tpad = f",tpad=stop_mode=clone:stop_duration={tahan:.2f}" if tahan > 0 else ""
        tunda = f",tpad=start_duration={mulai:.3f}:start_mode=add:color=black@0" if mulai > 0 else ""
        graf.append(f"[{n_in}:v]format=rgba,scale=1080:1920{tpad}{tunda},setpts=PTS-STARTPTS[an{n_in}]")
        graf.append(f"[{v}][an{n_in}]overlay=eof_action=pass[va{n_in}]")
        v = f"va{n_in}"
        n_in += 1

    # transisi sapuan menutupi setiap pergantian ke/dari B-roll (penuh tepat di titik potong)
    if g.get("transisi"):
        # tiga pita (indigo, cyan, lime) digambar ffmpeg sendiri (tanpa berkas): menyapu kanan→kiri 0,6 dtk,
        # menutup penuh tepat di titik potong. Sumber `color` tak berujung → tidak ada antrean input yang bisa macet.
        n_sw = 0
        for titik in [x for sp in (g.get("_sisipan") or []) for x in (sp["a"], sp["b"])]:
            for i, warna in enumerate(("0xC6FF3D", "0x22D3EE", "0x5B5BF6")):
                t0 = titik - 0.3 + i * 0.06
                graf.append(f"color=c={warna}:s=1300x1920:r=30[sw{n_sw}]")
                graf.append(f"[{v}][sw{n_sw}]overlay=x='W-(t-{t0:.3f})/0.6*(W+1300)':y=0:"
                            f"enable='between(t,{t0:.3f},{t0 + 0.6:.3f})':eof_action=pass:shortest=1[vs{n_sw}]")
                v = f"vs{n_sw}"
                n_sw += 1
    # hiasan judul pembuka: coretan neon / pita kertas sobek
    if klip.judul and g.get("judul_buka", True) and not (g.get("banner", True) and klip.judul):
        if c.get("judul") == "neon":
            hiasan("neon", 0.0, tahan=max(lama_judul(c) - 1.5, 0))
        elif c.get("judul") == "sobek":
            hiasan("sobek", 0.0)
    # emoji otomatis (gambar Twemoji) di atas baris subtitle
    if c.get("emoji") and g.get("subtitle", True):
        rencana = rencana_emoji(kal, klip, c["per"])
        y_emoji = int(1920 * float(g.get("posisi_y", 72)) / 100 - c["uk"] * 1.4 - 150)
        for kode in dict.fromkeys(x[2] for x in rencana):
            masukan_extra += ["-loop", "1", "-t", f"{durasi:.2f}", "-i", str(EMOJI_DIR / f"{kode}.png")]
            kapan = "+".join(f"between(t,{m:.2f},{sl:.2f})" for m, sl, k in rencana if k == kode)
            graf.append(f"[{n_in}:v]scale=150:150,format=rgba[em{n_in}]")
            graf.append(f"[{v}][em{n_in}]overlay=x=(W-w)/2:y={max(y_emoji, 40)}:enable='{kapan}':eof_action=pass[ve{n_in}]")
            v = f"ve{n_in}"
            n_in += 1
    # progress bar tipis di bawah (maju sesuai durasi klip)
    if c.get("progres"):
        graf.append(f"color=c={c['warna']}:s=1080x10:r=30:d={durasi:.2f}[pb]")
        graf.append(f"[{v}][pb]overlay=x='-W+W*t/{durasi:.2f}':y=H-10:eof_action=pass[vp]")
        v = "vp"
    if g.get("subtitle", True) or g.get("banner", True) or c.get("akun") or (g.get("judul_buka", True) and klip.judul) \
            or c.get("watermark"):
        ass = kerja / f"{out.stem}.ass"
        tulis_ass(kal, klip, ass, g, bar_atas)
        fdir = kerja / "font"
        if FONT_DIR.exists() and not fdir.exists():
            try:
                fdir.symlink_to(FONT_DIR, target_is_directory=True)
            except OSError:  # Windows tanpa izin symlink → salin
                import shutil as _sh
                _sh.copytree(FONT_DIR, fdir)
        graf.append(f"[{v}]subtitles=filename='{ass.name}':fontsdir=font[vout]")
    else:
        graf.append(f"[{v}]null[vout]")

    # audio: suara asli (boost) + musik latar + SFX hook
    masukan = ["-ss", f"{klip.mulai:.2f}", "-t", f"{durasi:.2f}", "-i", str(file.resolve()), *masukan_extra]
    aud = [f"[0:a]volume={float(g.get('suara', 100)) / 100:.2f}[a0]"]
    campur = ["[a0]"]
    idx = n_in
    musik = g.get("musik")
    if musik and Path(musik).exists():
        masukan += ["-stream_loop", "-1", "-i", str(Path(musik).resolve())]
        aud.append(f"[{idx}:a]volume={float(g.get('volume_musik', 18)) / 100:.2f},atrim=0:{durasi:.2f},"
                   f"afade=t=out:st={max(durasi - 1.5, 0):.2f}:d=1.5[am]")
        campur.append("[am]")
        idx += 1
    sfx = g.get("sfx")
    if sfx and Path(sfx).exists():
        masukan += ["-i", str(Path(sfx).resolve())]
        aud.append(f"[{idx}:a]volume=0.9[as]")
        campur.append("[as]")
        idx += 1
    if len(campur) > 1:
        aud.append(f"{''.join(campur)}amix=inputs={len(campur)}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    else:
        aud.append("[a0]anull[aout]")

    subprocess.run([
        ffmpeg(), "-y", "-v", "error", *masukan,
        "-filter_complex", ";".join(graf + aud), "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        # KLIP_THREADS (server): batasi inti per render supaya beberapa pengguna tidak saling memperlambat
        *(["-threads", __import__("os").environ["KLIP_THREADS"], "-filter_complex_threads", __import__("os").environ["KLIP_THREADS"]]
          if __import__("os").environ.get("KLIP_THREADS") else []),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out.resolve()),
    ], check=True, cwd=kerja)
