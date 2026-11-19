"""Download and extract the Eight Portions Recipe Box dataset (3 JSON files)."""

from pathlib import Path
import urllib.request
import zipfile

DATASET_URL = "https://eightportions.com/recipes_raw.zip"
EXPECTED_JSON_FILES = [
    "recipes_raw_nosource_ar.json",
    "recipes_raw_nosource_epi.json",
    "recipes_raw_nosource_fn.json",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
ZIP_PATH = RAW_DIR / "recipes_raw.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RecipeBoxLoader/1.0)"}


def download_and_extract() -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if all((RAW_DIR / fname).exists() for fname in EXPECTED_JSON_FILES):
        print(f"All 3 JSON files already exist in {RAW_DIR}", flush=True)
        return RAW_DIR

    # Remove corrupt/partial zip if present
    if ZIP_PATH.exists() and not zipfile.is_zipfile(ZIP_PATH):
        print("Removing incomplete zip file...", flush=True)
        ZIP_PATH.unlink()

    if not ZIP_PATH.exists():
        print(f"Downloading {DATASET_URL} ...", flush=True)
        req = urllib.request.Request(DATASET_URL, headers=HEADERS)
        tmp_path = ZIP_PATH.with_suffix(".zip.part")
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as out:
            total = int(resp.headers.get("Content-Length", "0") or 0)
            downloaded = 0
            next_report_mb = 10
            while True:
                chunk = resp.read(512 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                mb = downloaded / (1024 * 1024)
                if mb >= next_report_mb:
                    if total:
                        pct = (downloaded / total) * 100
                        print(f"  {mb:.1f} / {total / (1024 * 1024):.1f} MB ({pct:.0f}%)", flush=True)
                    else:
                        print(f"  {mb:.1f} MB downloaded...", flush=True)
                    next_report_mb += 10
        tmp_path.rename(ZIP_PATH)
        print(f"Downloaded {ZIP_PATH.name} ({ZIP_PATH.stat().st_size / (1024 * 1024):.1f} MB)", flush=True)

    print(f"Extracting {ZIP_PATH.name} into {RAW_DIR} ...", flush=True)
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        zf.extractall(RAW_DIR)

    for fname in EXPECTED_JSON_FILES:
        target = RAW_DIR / fname
        if not target.exists():
            matches = list(RAW_DIR.rglob(fname))
            if matches:
                matches[0].rename(target)

    missing = [f for f in EXPECTED_JSON_FILES if not (RAW_DIR / f).exists()]
    if missing:
        raise FileNotFoundError(f"Expected JSON files missing after extract: {missing}")

    print("Ready! Extracted JSON files:", flush=True)
    for fname in EXPECTED_JSON_FILES:
        size_mb = (RAW_DIR / fname).stat().st_size / (1024 * 1024)
        print(f"  - {fname} ({size_mb:.1f} MB)", flush=True)

    return RAW_DIR


if __name__ == "__main__":
    download_and_extract()
