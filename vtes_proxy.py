#!/usr/bin/env python3
"""
vtes_proxy.py - Generate a print-ready A4 PDF of VTES proxy cards.

Cards are looked up by name on the KRCG API (https://api.krcg.org), which
returns an official scan for each card. Images are cropped/scaled to the
exact physical size of a real card (63.5mm x 88.9mm, standard trading-card
size) and tiled onto A4 pages with thin cutting guide lines, so that once
printed at 100% scale ("no scaling"/"actual size") and cut along the lines,
the proxies are exactly the size of a real card and fit in a sleeve with the
genuine cards.

If a card can't be found on KRCG (typically because it hasn't been released
yet), you'll be prompted to supply a local image file for it.

Requirements:
    pip install requests pillow reportlab

Usage:
    python vtes_proxy.py "6 Information Exchange" "6x Deadly Secrets" "1 Kaiser"

    # or from a text file, one entry per line:
    python vtes_proxy.py --list decklist.txt

    decklist.txt:
        6 Information Exchange
        6x Deadly Secrets
        1 Kaiser
        # comments and blank lines are ignored

    Useful options:
        -o out.pdf            output file (default vtes_proxies.pdf)
        --card-width 63.5      card width in mm
        --card-height 88.9     card height in mm
        --margin 5              minimum page margin in mm
        --gap 0                 gap between cards in mm (0 = edge to edge)
        --no-cutlines           skip the cutting guide lines
"""

import argparse
import io
import re
import sys
from dataclasses import dataclass
from urllib.parse import quote

import requests
from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

KRCG_API = "https://api.krcg.org"

# Standard trading-card size (same as Magic: The Gathering / VTES cards).
DEFAULT_CARD_W_MM = 63.5
DEFAULT_CARD_H_MM = 88.9
DEFAULT_MARGIN_MM = 5.0
DEFAULT_GAP_MM = 0.0
TARGET_DPI = 300


@dataclass
class CardEntry:
    name: str
    count: int


# --------------------------------------------------------------------------
# Parsing the requested card list
# --------------------------------------------------------------------------

def parse_entry(text: str) -> CardEntry:
    m = re.match(r"^\s*(\d+)\s*[xX]?\s*(.+?)\s*$", text)
    if not m:
        raise ValueError(
            f"Can't parse card entry {text!r} (expected e.g. '3 Fame' or '3x Fame')"
        )
    return CardEntry(name=m.group(2), count=int(m.group(1)))


def load_entries(args) -> list[CardEntry]:
    entries = []
    for raw in args.cards:
        entries.append(parse_entry(raw))
    if args.list:
        with open(args.list, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                entries.append(parse_entry(line))
    if not entries:
        sys.exit("No cards specified. Pass card entries as arguments or use --list.")
    return entries


# --------------------------------------------------------------------------
# KRCG lookups
# --------------------------------------------------------------------------

def krcg_lookup(name: str):
    """Return (status, resolved_name, image_url).

    status is one of "found", "found_no_image", "not_found".
    """
    url = f"{KRCG_API}/card/{quote(name)}"
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [warn] network error looking up {name!r}: {e}")
        return "not_found", None, None
    if r.status_code == 404:
        return "not_found", None, None
    r.raise_for_status()
    data = r.json()
    resolved_name = data.get("printed_name") or data.get("name")
    image_url = data.get("url")
    if not image_url:
        return "found_no_image", resolved_name, None
    return "found", resolved_name, image_url


def krcg_suggestions(name: str):
    url = f"{KRCG_API}/complete/{quote(name)}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return []


def ask_user_for_image(name: str) -> Image.Image:
    while True:
        path = input(f"  Please provide a path to an image file for '{name}': ").strip().strip('"')
        if not path:
            print("  A path is required, please try again.")
            continue
        try:
            img = Image.open(path)
            img.load()
            return img
        except Exception as e:
            print(f"  Couldn't open that image ({e}), please try again.")


def resolve_card_image(name: str, cache: dict) -> Image.Image:
    """Return a PIL Image for one copy of `name`, resolving via KRCG or asking the user."""
    if name in cache:
        return cache[name]

    status, resolved_name, image_url = krcg_lookup(name)

    if status == "not_found":
        suggestions = krcg_suggestions(name)
        if len(suggestions) == 1:
            print(f"  '{name}' not found directly, trying closest match '{suggestions[0]}'")
            status, resolved_name, image_url = krcg_lookup(suggestions[0])
        elif suggestions:
            print(f"  '{name}' not found exactly. Did you mean: {', '.join(suggestions)}")

    if status == "not_found" or image_url is None:
        if status == "found_no_image":
            print(f"  '{resolved_name}' exists on KRCG but has no scan yet (likely unreleased).")
        else:
            print(f"  Could not find '{name}' on KRCG (api.krcg.org) - probably not released yet.")
        img = ask_user_for_image(resolved_name or name)
        cache[name] = img
        return img

    print(f"  Found '{resolved_name}' -> {image_url}")
    try:
        r = requests.get(image_url, timeout=30)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img.load()
    except Exception as e:
        print(f"  [warn] failed to download image for '{resolved_name}': {e}")
        img = ask_user_for_image(resolved_name or name)

    cache[name] = img
    return img


# --------------------------------------------------------------------------
# Image fitting
# --------------------------------------------------------------------------

def fit_image(img: Image.Image, card_w_mm: float, card_h_mm: float, dpi=TARGET_DPI) -> Image.Image:
    """Crop/scale the source scan to exactly fill the card's aspect ratio."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    target_w = max(1, round(card_w_mm / 25.4 * dpi))
    target_h = max(1, round(card_h_mm / 25.4 * dpi))
    return ImageOps.fit(img, (target_w, target_h), method=Image.LANCZOS)


# --------------------------------------------------------------------------
# PDF layout
# --------------------------------------------------------------------------

def draw_cut_grid(c, origin_x, top_y, cols, rows, card_w, card_h, gap, mark_len):
    col_edges = []
    x = origin_x
    for _ in range(cols):
        col_edges.append(x)
        col_edges.append(x + card_w)
        x += card_w + gap
    col_edges = sorted(set(col_edges))

    row_edges = []
    y = top_y
    for _ in range(rows):
        row_edges.append(y)
        row_edges.append(y - card_h)
        y -= card_h + gap
    row_edges = sorted(set(row_edges))

    left, right = col_edges[0], col_edges[-1]
    bottom, top = row_edges[0], row_edges[-1]

    c.setLineWidth(0.25)
    c.setStrokeColorRGB(0.4, 0.4, 0.4)
    for x in col_edges:
        c.line(x, bottom - mark_len, x, top + mark_len)
    for y in row_edges:
        c.line(left - mark_len, y, right + mark_len, y)


def build_pdf(images, out_path, card_w_mm, card_h_mm, margin_mm, gap_mm, draw_cutlines=True):
    page_w, page_h = A4  # points

    card_w = card_w_mm * mm
    card_h = card_h_mm * mm
    gap = gap_mm * mm
    margin = margin_mm * mm

    cols = max(1, int((page_w - 2 * margin + gap) // (card_w + gap)))
    rows = max(1, int((page_h - 2 * margin + gap) // (card_h + gap)))

    content_w = cols * card_w + (cols - 1) * gap
    content_h = rows * card_h + (rows - 1) * gap
    origin_x = (page_w - content_w) / 2
    origin_y = (page_h - content_h) / 2
    top_y = page_h - origin_y

    per_page = cols * rows
    n_pages = (len(images) + per_page - 1) // per_page
    print(
        f"\nLayout: {cols} x {rows} = {per_page} cards per A4 page "
        f"(card size {card_w_mm}mm x {card_h_mm}mm). "
        f"{len(images)} card(s) -> {n_pages} page(s)."
    )

    c = canvas.Canvas(out_path, pagesize=A4)
    mark_len = 4 * mm

    idx = 0
    for _ in range(n_pages):
        page_images = images[idx: idx + per_page]
        idx += per_page
        for k, img in enumerate(page_images):
            col = k % cols
            row = k // cols
            x = origin_x + col * (card_w + gap)
            y = top_y - (row + 1) * card_h - row * gap
            c.drawImage(ImageReader(img), x, y, width=card_w, height=card_h)
        if draw_cutlines:
            draw_cut_grid(c, origin_x, top_y, cols, rows, card_w, card_h, gap, mark_len)
        c.showPage()

    c.save()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a print-ready A4 PDF of VTES proxy cards using the KRCG API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python vtes_proxy.py \"6 Information Exchange\" \"6x Deadly Secrets\" \"1 Kaiser\"\n"
            "  python vtes_proxy.py --list decklist.txt -o proxies.pdf\n"
        ),
    )
    parser.add_argument("cards", nargs="*", help="Card entries, e.g. '6 Information Exchange' or '6x Deadly Secrets'")
    parser.add_argument("-l", "--list", help="Text file with one card entry per line")
    parser.add_argument("-o", "--output", default="vtes_proxies.pdf", help="Output PDF path")
    parser.add_argument("--card-width", type=float, default=DEFAULT_CARD_W_MM, help="Card width in mm (default 63.5)")
    parser.add_argument("--card-height", type=float, default=DEFAULT_CARD_H_MM, help="Card height in mm (default 88.9)")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN_MM, help="Minimum page margin in mm")
    parser.add_argument("--gap", type=float, default=DEFAULT_GAP_MM, help="Gap between cards in mm (default 0)")
    parser.add_argument("--no-cutlines", action="store_true", help="Don't draw cutting guide lines")
    args = parser.parse_args()

    entries = load_entries(args)

    print("Resolving cards via KRCG (https://api.krcg.org) ...")
    cache = {}
    images = []
    for entry in entries:
        print(f"\n{entry.name} x{entry.count}")
        base_img = resolve_card_image(entry.name, cache)
        fitted = fit_image(base_img, args.card_width, args.card_height)
        images.extend([fitted] * entry.count)

    if not images:
        sys.exit("No images to place, aborting.")

    build_pdf(images, args.output, args.card_width, args.card_height, args.margin, args.gap, draw_cutlines=not args.no_cutlines)
    print(f"\nDone! Wrote {len(images)} card(s) to {args.output}")
    print("Print at 100% / 'actual size' (no 'fit to page') for correct dimensions.")


if __name__ == "__main__":
    main()
