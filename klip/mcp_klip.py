# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
# kredit-maliki: Template oleh Maliki Saputra (@malikisaputra) — https://malikisaputra.biz.id · Lisensi Nonkomersial Maliki Studio 1.0 (alat/LISENSI.md): dilarang dijual ulang.
"""Server MCP Klip — supaya Claude Code / Claude Desktop bisa menyuruh Klip memotong video.

Jalan lokal di komputer pengguna (stdio). Kunci AI tetap milik pengguna (.env Klip / GEMINI_API_KEY).

Pasang di Claude Code:
  claude mcp add klip -- uv run /path/ke/alat/klip/mcp_klip.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

AKAR = Path(__file__).resolve().parent
HASIL = Path(os.environ.get("KLIP_MCP_HASIL", Path.home() / "Klip-hasil"))
PRESET = ["sorot", "karaoke", "terbaca", "minimal", "podcast", "maliki", "lime", "neon", "elegan", "sobek"]
JUDUL = ["kuning", "neon", "serif", "kotak", "sobek"]

mcp = FastMCP("klip")
_proses: dict[str, subprocess.Popen] = {}


def _folder(id_: str) -> Path:
    f = (HASIL / id_).resolve()
    if HASIL.resolve() not in f.parents:
        raise ValueError("id tidak sah")
    return f


def _jalan(id_: str, arg: list[str]) -> None:
    f = _folder(id_)
    f.mkdir(parents=True, exist_ok=True)
    log = open(f / "log.txt", "a", encoding="utf-8")
    _proses[id_] = subprocess.Popen(["uv", "run", "-q", str(AKAR / "klip.py"), *arg], cwd=AKAR,
                                    stdout=log, stderr=subprocess.STDOUT)


def _ringkas(f: Path) -> list[dict]:
    try:
        daftar = json.loads((f / "klip.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in daftar:
        x = {k: d.get(k) for k in ("no", "judul", "mulai", "selesai", "skor", "caption")}
        for k in ("file", "sampul", "teks_unggahan"):
            if d.get(k):
                x[k] = str(f / d[k])
        out.append(x)
    return out


@mcp.tool()
def analisis(sumber: str, jumlah: int = 3, durasi: int = 30, arahan: str = "", rentang: str = "",
             ai: str = "gemini") -> dict:
    """Mulai mencari momen terbaik dari video (link YouTube atau path file lokal). Berjalan di latar;
    pantau dengan `status`. Tidak merender — setelah selesai, pilih klip lalu panggil `render`.
    rentang contoh "10:00-25:30" (opsional). ai: gemini (bawaan, kunci milik pengguna), openai (Ollama dll.), lokal."""
    id_ = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    arg = [sumber, "--keluar", str(_folder(id_)), "--jumlah", str(jumlah), "--durasi", str(durasi),
           "--ai", ai, "--hanya-analisis"]
    if ai == "gemini":
        arg += ["--transkrip", "gemini"]
    if arahan:
        arg += ["--arahan", arahan]
    if rentang:
        arg += ["--rentang", rentang]
    _jalan(id_, arg)
    return {"id": id_, "folder": str(_folder(id_)), "pesan": "Analisis dimulai. Cek dengan status(id)."}


@mcp.tool()
def render(id: str, nomor: str = "", preset: str = "maliki", gaya_judul: str = "", rasio: str = "9:16",
           tanpa_subtitle: bool = False) -> dict:
    """Render klip hasil `analisis`. nomor: mis. "1,3" (kosong = semua). preset subtitle lihat `gaya`."""
    if preset not in PRESET:
        return {"galat": f"preset tidak dikenal; pilih: {', '.join(PRESET)}"}
    g = {"preset": preset, "rasio": rasio}
    if gaya_judul in JUDUL:
        g["gaya_judul"] = gaya_judul
    if tanpa_subtitle:
        g["subtitle"] = False
    arg = ["--render", "--keluar", str(_folder(id)), "--gaya", json.dumps(g)]
    if nomor:
        arg += ["--pilih", nomor]
    _jalan(id, arg)
    return {"id": id, "pesan": "Render dimulai. Cek dengan status(id)."}


@mcp.tool()
def status(id: str) -> dict:
    """Status proses (jalan/selesai/gagal), 12 baris log terakhir, dan daftar klip beserta path file."""
    f = _folder(id)
    p = _proses.get(id)
    keadaan = "jalan" if p and p.poll() is None else ("gagal" if p and p.returncode else "selesai")
    try:
        ekor = (f / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
    except OSError:
        ekor = []
    return {"id": id, "status": keadaan, "log": ekor, "klip": _ringkas(f)}


@mcp.tool()
def daftar() -> list[dict]:
    """Daftar semua proses Klip yang pernah dibuat lewat MCP (terbaru dulu)."""
    if not HASIL.exists():
        return []
    return [{"id": f.name, "klip": len(_ringkas(f))} for f in sorted(HASIL.iterdir(), reverse=True) if f.is_dir()][:20]


@mcp.tool()
def gaya() -> dict:
    """Pilihan gaya yang tersedia untuk `render`."""
    return {"preset_subtitle": PRESET, "gaya_judul": JUDUL, "rasio": ["9:16"]}


if __name__ == "__main__":
    mcp.run()
