#!/bin/sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
bin_dir=${HOME}/.local/bin
applications_dir=${XDG_DATA_HOME:-${HOME}/.local/share}/applications

install -d "$bin_dir" "$applications_dir"
install -m 755 "$project_dir/proton_selector.py" "$bin_dir/proton-selector"
install -m 644 \
    "$project_dir/proton_selector_i18n.py" \
    "$bin_dir/proton_selector_i18n.py"
install -m 644 \
    "$project_dir/proton-selector.desktop" \
    "$applications_dir/proton-selector.desktop"

printf '%s\n' "Installed Proton Selector:"
printf '  %s\n' "$bin_dir/proton-selector"
printf '  %s\n' "$bin_dir/proton_selector_i18n.py"
printf '  %s\n' "$applications_dir/proton-selector.desktop"
