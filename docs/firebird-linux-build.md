# Ocarina of Time 1.1.1 on Linux: the Firebird build

What it took to build and run the `oot-port` branch on an Arch-based Linux box (CachyOS, kernel 7.2, clang 23.1,
Mesa 26.2.4 RADV, AMD Radeon AI PRO R9700) on 2026-11-07, as a worked example of the Linux path in
[building.md](building.md) and [linux/README.md](../linux/README.md) (which describe Bananza). Every command runs from
the repository root. Nothing from the game goes into the repository: `build/`, `generated/` and `logs/` are ignored.

## Packages

```sh
sudo pacman -Syy    # once: the mirrors' dotnet packages were newer than the local database and every download 404'd
sudo pacman -S --needed clang lld ninja make pkgconf sdl3 opus libvpx zstd dav1d vulkan-headers vulkan-icd-loader \
    vulkan-validation-layers shaderc spirv-tools dotnet-sdk uv git unicorn
```

`vulkan-memory-allocator` is not in the Arch repositories; `tools/build_game.py` fetches the pinned VMA 3.4.0 header
into `toolchain/vma/` on first use. The runtime links `libdav1d` on Linux (the Ocarina cutscenes are AV1), so the
`dav1d` package is required here although the Bananza list does not name it.

## Python environments

```sh
uv venv --python 3.12 .venv          && uv pip install --python .venv/bin/python -r tools/requirements.txt
uv venv --python 3.14 .venv-py314    && uv pip install --python .venv-py314/bin/python -r tools/requirements-py314.txt
```

## Kernel: userfaultfd

The write watch needs unprivileged userfaultfd (building.md, "Linux"). Arch ships it off:

```sh
sudo sysctl -w vm.unprivileged_userfaultfd=1
echo 'vm.unprivileged_userfaultfd = 1' | sudo tee /etc/sysctl.d/99-oot-recomp-userfaultfd.conf
```

## Game files

The dump was an Eden/Eglantine `.nup` container of the 1.1.1 update (the container labels it `v131072`; the
display version inside is 1.1.1 and every ExeFS hash matches `tools/data/expected-oot111.json`). The container
holds loose, already decrypted files, so no keys are needed:

```sh
.venv-py314/bin/python tools/extract_nup.py "<game>.nup" --list
.venv-py314/bin/python tools/extract_nup.py "<game>.nup" --out build/extracted/oot111-dump --package 0
```

`main` is a ZBIC NSO (NSO flag bit 7, firmware 22+). `tools/nso_plain.py` turns it into the plain NSO the lifter
reads, with `build/zbic/zbic_dec` built from a zstd that has ZBIC support (hexkyz/zstd with `ZSTD_ZBIC_SUPPORT=1`;
Eglantine/Lichii vendors the same tree through CPM as `zbic`, which is what was used here):

```sh
Z=<path to a hexkyz zstd checkout or Lichii's .cache/cpm/zbic/<hash>/src>
mkdir -p build/zbic build/extracted/oot111
clang -O2 -DZSTD_ZBIC_SUPPORT=1 -I "$Z" tools/zbic/zbic_dec.c "$Z/zstd.c" -o build/zbic/zbic_dec
.venv/bin/python tools/nso_plain.py build/extracted/oot111-dump/exefs build/extracted/oot111/exefs
sha256sum build/extracted/oot111/exefs/main    # ca344998… = exefs_sha256.main in expected-oot111.json
.venv/bin/python tools/dkb_pack_romfs.py --dir build/extracted/oot111-dump/romfs --out build/extracted/oot111/romfs
```

The RomFS pack is 24.4 GiB; allow about 55 GB for extraction plus pack plus the build.

## Build

```sh
.venv/bin/python tools/build_xlate.py                         # shader translator, 16 s with .NET 10
.venv/bin/python recomp/gen.py --exefs build/extracted/oot111/exefs --out generated/oot111 \
    --modules main,subsdk0,sdk --patches recomp/patches-oot111.txt --jobs 28      # 24 s, 1.2 GB of C
.venv/bin/python tools/build_game.py --version oot111 --gen generated/oot111 --out build/oot111 \
    --expected tools/data/expected-oot111.json --exefs build/extracted/oot111/exefs \
    --romfs build/extracted/oot111/romfs --profile build/profile-oot111               # 66 s compile on 16C/32T
build/oot111/dkb --selftest                                   # layout, dispatch table, host registry: 0 failures
```

The executable keeps the inherited name `build/oot111/dkb`.

## Running from a .nup without unpacking

The packed `romfs.bin` is a 24.4 GiB copy of data the `.nup` already holds: the container stores every RomFS file
as one extent at a 16-aligned offset, 145,999 of them raw (23.25 GB) and 40,588 zstd-compressed (2.99 GB
decompressed). `tools/nup_romfs_index.py` indexes the raw files in place and writes only the compressed ones out,
decompressed, into `romfs-aux.bin`; the runtime reads the raw files straight from the `.nup`. The disk cost of the
RomFS drops from 24.4 GiB to 2.8 GiB, which is what makes a handheld with a 15 GB-free disk possible.

```sh
.venv-py314/bin/python tools/nup_romfs_index.py "<game>.nup" --out build/extracted/oot111-nup \
    --check 400 --extracted build/extracted/oot111-dump/romfs         # 2.8 GB aux + 25 GB hashed: ~45 s
.venv/bin/python tools/build_game.py --version oot111 --gen generated/oot111 --out build/oot111-nup \
    --expected tools/data/expected-oot111.json --exefs build/extracted/oot111/exefs \
    --romfs build/extracted/oot111-nup --profile build/profile-oot111   # the same objects, a new index + romfs.c
TOTK_ROMFS="<game>.nup" TOTK_ROMFS_AUX=build/extracted/oot111-nup/romfs-aux.bin build/oot111-nup/dkb --probe-fs
```

`--out` gets `index.json` (the `dkb_pack_romfs.py` shape plus `image`, the `.nup` path, and the aux image's size and
SHA-256), `romfs-aux.bin`, and a `romfs.bin` symlink to the `.nup`, so the folder also serves as `TOTK_ROMFS` and as
`build_game.py --romfs`. The index is `TOTKRFS2`: a row whose offset has bit 63 set lies in the aux image
(`tools/romfs_index.py` `AUX`); `romfs_config.h` gains `ROMFS_AUX_SIZE` and `ROMFS_AUX_SHA256`, and a `TOTKRFS1`
index from a packed `romfs.bin` still loads. At run time `TOTK_ROMFS` may name the `.nup` itself or a folder with
`romfs.bin` (file or link); the aux image is `TOTK_ROMFS_AUX`, else `romfs-aux.bin` next to the main image, else
next to `romfs-index.bin`. `TOTK_ROMFS_VERIFY=full` hashes the whole `.nup` (and the aux image every time, 3 GB);
`--no-hash` at index time makes that mode refuse. `--check N` reads N files back through the written index, a
quarter of them from the aux image, and compares them with the extracted tree: 499 files, all equal.

Verified on Firebird (`build/oot111-nup/dkb`, `logs/nup-run2.log`): `--probe-fs` samples 64 files plus the largest
(385.6 MiB) against the `.nup` at their index offsets, all equal; a 150 s run with the fake-button sequence
`1@20,1@30,1@40,1@50,1@62` (title, Continue, the save, Load, confirm) reached Lon Lon Ranch, read 7.6 GB through
20,760 positional reads (`OOT_FS_STATS=1`, slowest 20 ms), ended with `boot: passed`, 0 shader failures and peak
RSS 8.2 GB; frame rates 52.9 / 46.4 fps mean over the two ranch minutes against 53.3 / 47.6 from the packed image.
The three-press sequence in "Running" stops at the Load confirmation with a cold pipeline cache; five presses land.

What a handheld needs on its disk with this scheme: the executable (302 MB), `romfs-aux.bin` (2.8 GB),
`romfs-index.bin` (15 MB), `build/xlate/` (129 MB), the `exefs` (~80 MB) and the shader/pipeline caches it builds
(85 MB + 5 MB here), about 3.4 GB beside the `.nup` the emulator already keeps.

The `romfs cache` (`romfs_cache.c`) copies files from whichever image a row points into. `tools/shader_census.py`
and other scripts that read `romfs.bin` by `index.json` offsets do not know aux rows yet; run them on the packed
layout.

## Fix needed: fiber stack alignment

Every run crashed about two seconds after the guest started, with or without a GPU, with SIGSEGV in a `guest`
thread inside glibc's `vsnprintf` (`vmovdqa %xmm0,(%rsp)` with `rsp` 8 mod 16). The chain was lifted code ->
`kern_svc` CreateThread -> `plat_thread_new_ex` -> `snprintf`. `plat_fiber_swap` returns into `plat_fiber_entry` with
`rsp` at `top - 8`, so the fiber's first call entered its function with `rsp` 16-byte aligned, where the SysV ABI
wants 8 mod 16; everything on a fiber ran 8 bytes off. A glibc built for x86-64-v3 (CachyOS) spills with aligned AVX
stores and faults; a baseline glibc happens not to. `plat_fiber_entry` now realigns `rsp` before the call
(`runtime/platform/linux.c`).

## Saves

The port keeps its saves under `<profile>/save/<uid>/`, where `<uid>` is the random local account the first run
writes to `<profile>/account.uid`. The save file system is the same one the emulator stores under
`nand/user/save/0000000000000000/<profile id>/0400486024DEC000/` (`account.sav`, `option.sav`, `slot_NN/`), so an
emulator save set copied into `build/profile-oot111/save/<uid>/` after the first run loads with Continue.

## Running

From the repository root (the game finds `build/xlate/totk_xlate` upwards from the executable):

```sh
DISPLAY=:0 DRI_PRIME='pci-0000_07_00_0!' build/oot111/dkb                  # pinned to one GPU on a two-GPU box
DISPLAY=:0 DRI_PRIME='pci-0000_07_00_0!' build/oot111/dkb --seconds 120 --shot build/oot111/shot.png
TOTK_FAKE_BUTTONS="1@20+0.3,1@30+0.3,1@40+0.3" TOTK_SHOTS=w0-1000:10 TOTK_SHOTS_DIR=logs/shots ...   # unattended
```

With `DRI_PRIME=<pci>!` the Vulkan loader exposes only that card, so the port's `gpu 0` is the pinned one
(`/proc/<pid>/fdinfo/*` `drm-pdev` confirms it). Logs go to `logs/totk.log` next to the executable when stderr is not
redirected.

Measured on the R9700 (output 2560x1440, scene 1600x900 by the game's own setting, handheld profile not applied):

| Where | Frames / 60 s | Mean fps | 1% low | Peak RSS |
|---|---|---|---|---|
| Title screen (AV1 movie + menu) | 3504 | 58.4 | 14.1 | ~2.6 GB |
| Lon Lon Ranch (adult Link, save slot 4), first minute | 3202 | 53.3 | 13.9 | 8.2 GB |
| Lon Lon Ranch, second minute | 2854 | 47.6 | 12.2 | 8.2 GB |

Both runs ended with `boot: passed`, 0 unimplemented NVN calls, 0 validation errors, 0 shader failures. The world
is rendered but its lighting is blown out (white terrain, magenta sky), the same state [oot-status.md](oot-status.md)
describes: compute passes with 64-bit atomics are skipped, mesh draws are not replayed, bindless shadow lookups are
stand-ins. The frame-rate dips are pipeline builds and shader translation on first sight; `pipeline-keys.bin` warms
them on the next start.

A third run ended after about 75 game seconds in the ranch with a GPU reset of the pinned card (`radv/amdgpu: The
CS has been cancelled because the context is lost`, `vkQueueSubmit2` -4, exit 70; the kernel logged
`amdgpu 0000:07:00.0: GPU reset(13) succeeded`). Two earlier runs of 120 and 150 game seconds finished with
`boot: passed`. Not attributed yet; `RADV_DEBUG=hang` on a repeat is the next step.
