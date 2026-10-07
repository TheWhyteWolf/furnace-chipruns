#!/usr/bin/env bash
# Build the headless, patched Furnace ("furnace-batch") that chipruns.py drives.
#
# The patch adds batch flags to Furnace's CLI: -insfile/-insindex import
# instrument presets with Furnace's own loaders (.fui/.dmp/.tfi/.opni/.opm/...),
# -saveas writes the song back out as .fur, and -sysjson/-insinfo dump the chip
# table and a preset's instruments as JSON. Rendering is Furnace's own export.
#
# By default it builds the same commit as the installed furnace-git package, so
# the .fur files it writes open in your Furnace and render the way it plays them.
#
# Usage: ./build-furnace.sh [commit]
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
src="$here/vendor/furnace"
cache="$HOME/.cache/yay/furnace-git/furnace"

commit="${1:-}"
if [[ -z "$commit" ]]; then
  # furnace-git versions look like r11045.c0f85a4a2-1
  commit="$(pacman -Q furnace-git 2>/dev/null | sed -n 's/.*\.\([0-9a-f]\{7,\}\)-[0-9]*$/\1/p')"
  commit="${commit:-c0f85a4a2}"
fi

if [[ ! -d "$src/.git" ]]; then
  if [[ -d "$cache" ]]; then
    git clone -q "$cache" "$src"
  else
    git clone -q https://github.com/tildearrow/furnace.git "$src"
  fi
  git -C "$src" remote set-url origin https://github.com/tildearrow/furnace.git
fi

git -C "$src" cat-file -e "$commit^{commit}" 2>/dev/null || git -C "$src" fetch -q origin
git -C "$src" reset -q --hard
git -C "$src" checkout -q "$commit"
# submodules the headless build needs (SDL, fmt come from the system)
for sub in extern/adpcm extern/json extern/YM2414-LLE; do
  if git -C "$src" config -f .gitmodules --get-regexp path | grep -q " $sub\$"; then
    git -C "$src" submodule update --init "$sub"
  fi
done
git -C "$src" apply "$here/patches/furnace-batch.patch"
# newer Furnace also misses this include when built without the GUI
if [[ -f "$src/src/fileutils/cfile.cpp" ]] && ! grep -q '<cstring>' "$src/src/fileutils/cfile.cpp"; then
  sed -i '0,/^#include "..\/fileutils.h"$/s//#include "..\/fileutils.h"\n#include <cstring>/' "$src/src/fileutils/cfile.cpp"
fi

cmake -S "$src" -B "$src/build" -G Ninja -W no-dev \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF \
  -DUSE_RTMIDI=OFF -DWITH_JACK=OFF -DWITH_PORTAUDIO=OFF \
  -DSYSTEM_FMT=ON -DSYSTEM_LIBSNDFILE=ON -DSYSTEM_ZLIB=ON -DSYSTEM_SDL2=ON -DSYSTEM_FFTW=ON \
  -DWITH_INSTRUMENTS=OFF -DWITH_WAVETABLES=OFF
ninja -C "$src/build"

mkdir -p "$here/bin"
ln -sfn ../vendor/furnace/build/furnace "$here/bin/furnace-batch"
echo "built $here/bin/furnace-batch ($(git -C "$src" rev-parse --short HEAD))"
