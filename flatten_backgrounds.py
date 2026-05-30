from PIL import Image
from pathlib import Path
import sys

INPUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("skills_raw")
OUTPUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("skills")

def flatten_to_white(src: Path, dst: Path):
    img = Image.open(src).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img)
    bg.convert("RGB").save(dst, "PNG")

def main():
    if not INPUT_DIR.exists():
        print(f"Input directory not found: {INPUT_DIR}")
        print("Usage: python flatten_backgrounds.py [input_dir] [output_dir]")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pngs = list(INPUT_DIR.glob("*.png"))
    if not pngs:
        print(f"No .png files found in {INPUT_DIR}")
        sys.exit(1)

    print(f"Processing {len(pngs)} images: {INPUT_DIR} -> {OUTPUT_DIR}")
    for p in pngs:
        flatten_to_white(p, OUTPUT_DIR / p.name)
    print("Done.")

if __name__ == "__main__":
    main()
