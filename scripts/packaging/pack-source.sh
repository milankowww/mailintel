#!/usr/bin/env sh
# Package committed source only; never include private working-tree files.
set -eu
name=${1:-mailintel-source.tar.gz}
case "$name" in
  */*|..|'') printf '%s\n' 'Use a filename without directory components.' >&2; exit 1 ;;
esac
case "$name" in *.tar.gz) ;; *) name="$name.tar.gz" ;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
mkdir -p "$repo_root/dist"
git -C "$repo_root" archive --format=tar.gz --prefix=mailintel/ -o "$repo_root/dist/$name" HEAD
printf '%s\n' "$repo_root/dist/$name"
