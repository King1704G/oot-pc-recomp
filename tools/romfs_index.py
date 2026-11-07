"""Build the RomFS index the runtime binary-searches (runtime/romfs.c) and its romfs_config.h.

Adapted from MacNativeGames games/totk/scripts/prepare_romfs_backend.py @ b75be8cf (MIT, Copyright (c) 2026
MacNativeGames contributors).

Input: <romfs>/index.json ({status, romfs_size, romfs_sha256, files: [{path, offset, size}]}) as tools/extract.py
writes it, and <romfs>/romfs.bin, whose size must match. Output, both in the build directory (never in the repo):

    romfs-index.bin   TOTKRFS1 layout: header <8sQQ (magic, count, strings offset), count 24-byte rows <QQII (offset,
                      size, name offset, name length including the NUL), then the NUL-terminated names; rows sorted by
                      the UTF-8 bytes of the path, which is the order strcmp sees
                      TOTKRFS2: the same, and a row whose offset has AUX (bit 63) set lies in the aux image
                      (romfs-aux.bin, tools/nup_romfs_index.py) at offset & ~AUX instead of the main image
    romfs_config.h    ROMFS_IMAGE_SIZE, ROMFS_IMAGE_SHA256, ROMFS_INDEX_SHA256, ROMFS_FILE_COUNT, ROMFS_AUX_SIZE,
                      ROMFS_AUX_SHA256

Both are rewritten only when their bytes change, so Ninja does not rebuild romfs.c for nothing.

The manifest's "image" names the image file when it is not <romfs>/romfs.bin (a .nup indexed in place); "aux",
"aux_size" and "aux_sha256" describe the aux image, and an entry's "aux": 1 puts it there. An empty "romfs_sha256"
is allowed for an indexed-in-place image (TOTK_ROMFS_VERIFY=full then refuses at run time).

    .venv\\Scripts\\python tools\\romfs_index.py --romfs build\\extracted\\v121\\romfs --out build\\v121

Standard library only.
"""
import argparse
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

from check_content import GENERATED_MARKER

MAGIC = b'TOTKRFS1'
MAGIC_AUX = b'TOTKRFS2'
HEADER = struct.Struct('<8sQQ')
ROW = struct.Struct('<QQII')
AUX = 1 << 63


def build_index(manifest):
    """The index bytes for an index.json manifest; exits on a malformed one."""
    files = sorted(manifest['files'], key=lambda e: e['path'].encode())
    size = manifest['romfs_size']
    aux_size = manifest.get('aux_size', 0)
    strings = bytearray()
    rows = bytearray()
    previous = None
    any_aux = False
    for item in files:
        path = item['path']
        name = path.encode()
        if not name or name.startswith(b'/') or b'\\' in name or b'\0' in name:
            sys.exit(f'romfs_index: malformed path in index.json (entry {len(rows) // ROW.size})')
        if name == previous:
            sys.exit('romfs_index: duplicate path in index.json')
        previous = name
        aux = bool(item.get('aux'))
        limit = aux_size if aux else size
        if item['offset'] < 0 or item['size'] < 0 or item['offset'] + item['size'] > limit or item['offset'] >= AUX:
            sys.exit(f'romfs_index: entry {len(rows) // ROW.size} lies outside the {"aux " if aux else ""}image')
        any_aux |= aux
        rows += ROW.pack(item['offset'] | (AUX if aux else 0), item['size'], len(strings), len(name) + 1)
        strings += name + b'\0'
    return HEADER.pack(MAGIC_AUX if any_aux else MAGIC, len(files), HEADER.size + len(rows)) + bytes(rows) + bytes(strings)


def write_if_changed(path, data):
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write(romfs, out):
    """Write romfs-index.bin and romfs_config.h into out. Returns (file count, index bytes, seconds)."""
    t0 = time.monotonic()
    manifest = json.loads((romfs / 'index.json').read_text(encoding='utf-8'))
    if manifest.get('status') != 'pass':
        sys.exit(f'romfs_index: {romfs / "index.json"} does not record a verified extraction')
    image = Path(manifest['image']) if manifest.get('image') else romfs / 'romfs.bin'
    if not image.is_file() or image.stat().st_size != manifest['romfs_size']:
        sys.exit(f'romfs_index: {image} is missing or its size differs from index.json')
    if not manifest['romfs_sha256'] and not manifest.get('image'):
        sys.exit(f'romfs_index: {romfs / "index.json"} records no image SHA-256')
    aux_size = manifest.get('aux_size', 0)
    if aux_size:
        aux = romfs / manifest.get('aux', 'romfs-aux.bin')
        if not aux.is_file() or aux.stat().st_size != aux_size:
            sys.exit(f'romfs_index: {aux} is missing or its size differs from index.json')
    index = build_index(manifest)
    count = len(manifest['files'])
    write_if_changed(out / 'romfs-index.bin', index)
    config = (f'// RomFS image and index identity, written by tools/romfs_index.py; do not edit. {GENERATED_MARKER}\n'
              '#pragma once\n'
              f'#define ROMFS_IMAGE_SIZE {manifest["romfs_size"]}ull\n'
              f'#define ROMFS_IMAGE_SHA256 "{manifest["romfs_sha256"].lower()}"\n'
              f'#define ROMFS_INDEX_SHA256 "{hashlib.sha256(index).hexdigest()}"\n'
              f'#define ROMFS_FILE_COUNT {count}ull\n'
              f'#define ROMFS_AUX_SIZE {aux_size}ull\n'
              f'#define ROMFS_AUX_SHA256 "{manifest.get("aux_sha256", "").lower()}"\n')
    write_if_changed(out / 'romfs_config.h', config.encode())
    return count, len(index), time.monotonic() - t0


def write_placeholder(out):
    """romfs_config.h without a RomFS (no extracted game on this machine): totk.exe builds, and fs_init stops with a
    named fatal."""
    (out / 'romfs-index.bin').unlink(missing_ok=True)
    config = (f'// No RomFS found by tools/romfs_index.py; do not edit. {GENERATED_MARKER}\n'
              '#pragma once\n'
              '#define ROMFS_MISSING 1\n'
              '#define ROMFS_IMAGE_SIZE 0ull\n'
              '#define ROMFS_IMAGE_SHA256 ""\n'
              '#define ROMFS_INDEX_SHA256 ""\n'
              '#define ROMFS_FILE_COUNT 0ull\n'
              '#define ROMFS_AUX_SIZE 0ull\n'
              '#define ROMFS_AUX_SHA256 ""\n')
    write_if_changed(out / 'romfs_config.h', config.encode())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--romfs', type=Path, required=True, help='directory with romfs.bin and index.json')
    ap.add_argument('--out', type=Path, required=True, help='build directory')
    args = ap.parse_args()
    count, size, dt = write(args.romfs.resolve(), args.out.resolve())
    print(f'romfs index: {count} files, {size:,} bytes, {dt:.1f} s')


if __name__ == '__main__':
    main()
