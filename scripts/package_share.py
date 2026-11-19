"""Build a shareable zip of AegisOps for other laptops (Docker is the only requirement there).

    python scripts/package_share.py            -> dist/aegisops-share.zip

Leaves out secrets (.env, ingest keys), virtualenvs, local state and caches.
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "data", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".terraform", ".git",
             ".tmp_appdata", ".tmp_localappdata", "node_modules", "graphify-out"}
SKIP_FILES = {".env", "aegis_ingest_key", "scratch_verification_output.json", "terraform.tfvars"}


def included(p: Path) -> bool:
    rel = p.relative_to(ROOT)
    return not (any(part in SKIP_DIRS or part.startswith(".venv") for part in rel.parts)
                or p.name in SKIP_FILES or p.suffix in {".pyc", ".tfstate"})


def main() -> None:
    out = ROOT / "dist" / "aegisops-share.zip"
    out.parent.mkdir(exist_ok=True)
    files = [p for p in ROOT.rglob("*") if p.is_file() and included(p)]
    crlf = [str(p) for p in files if p.suffix in {".sh", ".tftpl"} and b"\r\n" in p.read_bytes()]
    assert not crlf, f"Windows line endings break these scripts on Linux; convert to LF: {crlf}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, Path("aegisops") / p.relative_to(ROOT))
    names = zipfile.ZipFile(out).namelist()
    assert not any(n.endswith("/.env") or "/.venv" in n for n in names), "secret or venv leaked into zip"
    print(f"{out} ({len(names)} files, {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
