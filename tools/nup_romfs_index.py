"""Index a NUP container's RomFS in place, so the game runs from the .nup without a 25 GB romfs.bin copy.

A NUP (tools/extract_nup.py) stores every RomFS file as one extent inside the container, raw or zstd. The runtime
(runtime/romfs.c) reads one image through romfs-index.bin, so the raw files, 146,000 of 186,592 and 21.7 of 24.4 GB
for Ocarina of Time 1.1.1, can be served straight from the .nup at their extent offsets. The zstd files are written
out once, decompressed and 16-aligned, into <out>/romfs-aux.bin (2.8 GB), and their index rows carry the aux flag
(tools/romfs_index.py AUX, bit 63 of the offset; the index is then TOTKRFS2).

    .venv-py314/bin/python tools/nup_romfs_index.py "<game>.nup" --out build/extracted/oot111-nup
    .venv/bin/python tools/build_game.py ... --romfs build/extracted/oot111-nup
    TOTK_ROMFS=<game>.nup TOTK_ROMFS_AUX=build/extracted/oot111-nup/romfs-aux.bin build/oot111/dkb

<out> gets index.json (as tools/dkb_pack_romfs.py writes it, plus "image": the .nup path, and the aux image's size
and SHA-256), romfs-aux.bin, and on POSIX a romfs.bin symlink to the .nup, so <out> also serves as the runtime's RomFS
folder (TOTK_ROMFS=<out>, aux found next to the symlink) and build_game.py takes it as --romfs.

The whole container is hashed by default (ROMFS_IMAGE_SHA256, what TOTK_ROMFS_VERIFY=full checks; ~25 s at 1 GB/s);
--no-hash records no hash and that mode refuses. --check N reads N sampled files back through the written index and
compares them with the extracted tree (--extracted, the folder tools/extract_nup.py wrote), every zstd file included.

zstd decompression needs Python 3.14 (compression.zstd), the zstandard module or a zstd program (extract_nup.Zstd).
Everything written is game data: keep <out> under build/.
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_nup as nupmod   # noqa: E402
import romfs_index             # noqa: E402

ALIGN = 16
CHUNK = 8 << 20


def pick_package(nup, which):
    if which == 'auto':
        if len(nup.packages) == 1:
            return nup.packages[0]
        return nupmod.pick(nup, 'base')
    return nupmod.pick(nup, which)


def hash_file(path, label):
    h = hashlib.sha256()
    size = os.path.getsize(path)
    t0 = time.time()
    with open(path, 'rb') as f:
        done = 0
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if done % (4 << 30) < len(chunk):
                print(f'  {label}: hashed {done / 2**30:.1f} of {size / 2**30:.1f} GiB '
                      f'({done / max(time.time() - t0, 1e-6) / 1e6:.0f} MB/s)')
    return h.hexdigest()


def write_aux(nup, zstd, entries, aux_path):
    """Decompress every zstd entry into aux_path, 16-aligned. Returns {file index: offset}, the aux size."""
    offsets = {}
    total = sum(nup.files[i][1] for _, i in entries)
    done = 0
    t0 = time.time()
    with open(aux_path, 'wb') as out:
        off = 0
        for n, (parts, idx) in enumerate(entries, 1):
            name, size, ext_off, ext_size, sha, codec = nup.files[idx]
            h = hashlib.sha256()
            written = 0
            nup.f.seek(ext_off)
            for chunk in zstd.stream(nup.f, ext_size):
                h.update(chunk)
                out.write(chunk)
                written += len(chunk)
            if written != size or h.digest() != sha:
                raise nupmod.NupError(f'{"/".join(parts)}: decompressed to {written} bytes, SHA-256 '
                                      f'{"ok" if h.digest() == sha else "differs"}; the container is corrupt')
            offsets[idx] = off
            off += size
            pad = (-off) % ALIGN
            if pad:
                out.write(b'\0' * pad)
                off += pad
            done += size
            if n % 5000 == 0 or n == len(entries):
                print(f'  aux: {n}/{len(entries)} zstd files, {done / 1e9:.2f} of {total / 1e9:.2f} GB '
                      f'({done / max(time.time() - t0, 1e-6) / 1e6:.0f} MB/s)')
    return offsets, off


def build(args):
    nup = nupmod.Nup(args.nup)
    pkg = pick_package(nup, args.package)
    print(f'{nup.path.name}: package [{pkg.index}] {pkg.kind_name} {pkg.title_id:016x} '
          f'{pkg.display_version or pkg.version} "{pkg.title or "-"}"')
    entries = nup.package_files(pkg, 'romfs')
    if not entries:
        raise nupmod.NupError('the package has no RomFS')
    raw = [(p, i) for p, i in entries if nup.files[i][5] == nupmod.CODEC_RAW]
    comp = [(p, i) for p, i in entries if nup.files[i][5] != nupmod.CODEC_RAW]
    raw_bytes = sum(nup.files[i][1] for _, i in raw)
    comp_bytes = sum(nup.files[i][1] for _, i in comp)
    print(f'  romfs: {len(entries)} files; {len(raw)} raw ({raw_bytes / 1e9:.2f} GB) served from the .nup, '
          f'{len(comp)} zstd ({comp_bytes / 1e9:.2f} GB) go to romfs-aux.bin')
    for _, i in raw:
        name, size, ext_off, ext_size, sha, codec = nup.files[i]
        if size and ext_size != size:
            raise nupmod.NupError(f'{name}: raw extent of {ext_size} bytes for a {size}-byte file')
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    aux_path = out / 'romfs-aux.bin'
    aux_offsets, aux_size = write_aux(nup, nupmod.Zstd(), comp, aux_path) if comp else ({}, 0)
    aux_sha = hash_file(aux_path, 'aux') if comp else ''
    files = []
    for parts, i in entries:
        name, size, ext_off, ext_size, sha, codec = nup.files[i]
        path = '/'.join(parts)
        if codec == nupmod.CODEC_RAW:
            files.append({'path': path, 'offset': ext_off if size else 0, 'size': size})
        else:
            files.append({'path': path, 'offset': aux_offsets[i], 'size': size, 'aux': 1})
    image = str(nup.path.resolve())
    image_sha = '' if args.no_hash else hash_file(image, 'image')
    manifest = {'status': 'pass',
                'source': 'nup extents in place (tools/nup_romfs_index.py); zstd files decompressed into romfs-aux.bin',
                'image': image, 'romfs_size': nup.file_size, 'romfs_sha256': image_sha,
                'aux': 'romfs-aux.bin', 'aux_size': aux_size, 'aux_sha256': aux_sha,
                'package': pkg.index, 'display_version': pkg.display_version, 'files': files}
    (out / 'index.json').write_text(json.dumps(manifest), encoding='utf-8')
    link = out / 'romfs.bin'
    if os.name != 'nt' and not args.no_link:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(image)
        print(f'  {link} -> {image}')
    print(f'  wrote {out / "index.json"} ({len(files)} files) and {aux_path} ({aux_size / 1e9:.2f} GB)')
    nup.close()
    return manifest


def check(args, manifest=None):
    """Read N sampled files back through the written index (raw from the .nup, zstd from romfs-aux.bin) and compare
    them byte for byte with the extracted tree. Every aux file is included when N covers them."""
    out = args.out
    if manifest is None:
        manifest = json.loads((out / 'index.json').read_text(encoding='utf-8'))
    index = romfs_index.build_index(manifest)
    count = struct.unpack_from('<Q', index, 8)[0]
    strings = struct.unpack_from('<Q', index, 16)[0]
    rows = [struct.unpack_from('<QQII', index, 24 + k * 24) for k in range(count)]
    names = {k: index[strings + r[2]:strings + r[2] + r[3] - 1].decode() for k, r in enumerate(rows)}
    image = open(manifest['image'], 'rb')
    aux = open(out / manifest['aux'], 'rb') if manifest.get('aux_size') else None
    n = min(args.check, count)
    picks = sorted({int(k * count / n) for k in range(n)})   # evenly over the sorted index
    aux_rows = [k for k, r in enumerate(rows) if r[0] & romfs_index.AUX]
    if aux_rows:
        picks = sorted(set(picks) | {aux_rows[int(k * len(aux_rows) / max(1, n // 4))] for k in range(max(1, n // 4))})
    bad = 0
    bytes_ = 0
    t0 = time.time()
    for k in picks:
        off, size, _, _ = rows[k]
        src = aux if off & romfs_index.AUX else image
        src.seek(off & ~romfs_index.AUX)
        got = src.read(size)
        want = (args.extracted / names[k]).read_bytes()
        bytes_ += size
        if got != want:
            bad += 1
            print(f'  MISMATCH {names[k]} ({"aux" if off & romfs_index.AUX else "nup"} offset {off & ~romfs_index.AUX}, '
                  f'{size} bytes)')
    image.close()
    if aux:
        aux.close()
    print(f'  check: {len(picks)} files ({len([k for k in picks if rows[k][0] & romfs_index.AUX])} from aux), '
          f'{bytes_ / 1e6:.1f} MB compared in {time.time() - t0:.1f} s: {"FAIL" if bad else "all equal"}')
    return bad == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('nup', type=Path, help='the .nup file')
    ap.add_argument('--out', type=Path, required=True, help='folder for index.json, romfs-aux.bin and the romfs.bin link')
    ap.add_argument('--package', default='auto', help='auto (the only package, else base), base, update, or an index')
    ap.add_argument('--no-hash', action='store_true', help='do not hash the whole .nup (TOTK_ROMFS_VERIFY=full then refuses)')
    ap.add_argument('--no-link', action='store_true', help='no romfs.bin symlink to the .nup in --out')
    ap.add_argument('--check', type=int, metavar='N', help='read N sampled files back and compare with --extracted')
    ap.add_argument('--extracted', type=Path, help='the extracted romfs folder (tools/extract_nup.py --out X -> X/romfs)')
    ap.add_argument('--check-only', action='store_true', help='only --check an index.json already in --out')
    args = ap.parse_args(argv)
    try:
        manifest = None if args.check_only else build(args)
        if args.check:
            if not args.extracted:
                sys.exit('--check needs --extracted <folder>')
            if not check(args, manifest):
                return 1
        return 0
    except nupmod.NupError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
