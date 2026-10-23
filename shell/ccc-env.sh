#!/usr/bin/env bash

_ccc_configs_dir() {
    local base
    if [[ -n ${XDG_CONFIG_HOME:-} ]]; then
        base=$XDG_CONFIG_HOME
        if [[ $base != /* ]]; then
            printf "ccc-use: XDG_CONFIG_HOME must be an absolute path, got '%s'\n" "$base" >&2
            return 1
        fi
    else
        base=${HOME:-/}
        if [[ -n ${HOME:-} && $base != /* ]]; then
            printf "ccc-use: HOME must be an absolute path, got '%s'\n" "$base" >&2
            return 1
        fi
        [[ $base == */ ]] || base+=/
        base+=.config
    fi
    [[ $base == */ ]] || base+=/
    printf '%s\n' "${base}ccc/configs"
}

_ccc_variant_names() {
    local dir name
    dir=$(_ccc_configs_dir) || return
    [[ -d "$dir" ]] || return 0
    while IFS= read -r name; do
        printf '%s\n' "$name"
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | LC_ALL=C sort)
}

_ccc_config_files() {
    local variant dir candidate name
    dir=$(_ccc_configs_dir) || return
    variant=$1
    candidate=
    while IFS= read -r name; do
        if [[ $name == "$variant" ]]; then
            candidate=$name
            break
        fi
    done < <(_ccc_variant_names)
    [[ -n $candidate ]] || return 0
    variant=$candidate
    dir=$dir/$variant
    [[ -d "$dir" ]] || return 0
    find "$dir" -mindepth 1 -maxdepth 1 -type f -name '*.toml' -printf '%f\n' | LC_ALL=C sort
}

# Warns when CCC_CONFIG (site config) names a file under the configs dir ($1),
# which holds only agents configs. Never changes CCC_CONFIG.
_ccc_warn_stale_site_config() {
    if [[ -n ${CCC_CONFIG:-} && $CCC_CONFIG == "$1/"* ]]; then
        printf 'ccc-use: CCC_CONFIG=%s names a file under %s, which holds agents configs, not site config; run: unset CCC_CONFIG\n' "$CCC_CONFIG" "$1" >&2
    fi
}

ccc-use() {
    local dir variant file current
    local -a matches
    if [[ $# -eq 1 && $1 == off ]]; then
        unset CCC_AGENTS_CONFIG
        if dir=$(_ccc_configs_dir 2>/dev/null); then
            _ccc_warn_stale_site_config "$dir"
        fi
        return 0
    fi
    dir=$(_ccc_configs_dir) || return 1
    _ccc_warn_stale_site_config "$dir"
    if [[ $# -eq 0 ]]; then
        if [[ -v CCC_AGENTS_CONFIG ]]; then
            printf 'current: %s\n' "$CCC_AGENTS_CONFIG"
        else
            printf 'current: (default)\n'
        fi
        printf 'configs: %s\n' "$dir"
        while IFS= read -r variant; do
            while IFS= read -r file; do
                printf '%s %s\n' "$variant" "${file%.toml}"
            done < <(_ccc_config_files "$variant")
        done < <(_ccc_variant_names)
        return 0
    fi
    if [[ $# -ne 1 && $# -ne 2 ]]; then
        printf 'usage: ccc-use [off|<variant> [<prefs>]]\n' >&2
        return 1
    fi

    variant=
    while IFS= read -r current; do
        if [[ $current == "$1" ]]; then
            variant=$current
            break
        fi
    done < <(_ccc_variant_names)
    if [[ -z $variant ]]; then
        printf 'ccc-use: unknown variant: %s\n' "$1" >&2
        return 1
    fi

    if [[ $# -eq 2 ]]; then
        file=
        while IFS= read -r current; do
            if [[ ${current%.toml} == "$2" ]]; then
                file=$current
                break
            fi
        done < <(_ccc_config_files "$variant")
        if [[ -z $file ]]; then
            printf 'ccc-use: unknown preference for %s: %s\n' "$variant" "$2" >&2
            return 1
        fi
    else
        mapfile -t matches < <(_ccc_config_files "$variant")
        if [[ ${#matches[@]} -ne 1 ]]; then
            printf 'ccc-use: %s has %d preferences' "$variant" "${#matches[@]}" >&2
            if [[ ${#matches[@]} -gt 1 ]]; then
                printf ': ' >&2
                for current in "${matches[@]}"; do
                    printf '%s ' "${current%.toml}" >&2
                done
            fi
            printf '\n' >&2
            return 1
        fi
        file=${matches[0]}
    fi
    export CCC_AGENTS_CONFIG="$dir/$variant/$file"
}

_ccc_use() {
    local cur variant candidate
    COMPREPLY=()
    _ccc_configs_dir >/dev/null || return 1
    cur=${COMP_WORDS[COMP_CWORD]}
    if [[ $COMP_CWORD -eq 1 ]]; then
        while IFS= read -r candidate; do
            [[ $candidate == "$cur"* ]] && COMPREPLY+=("$candidate")
        done < <({ printf '%s\n' off; _ccc_variant_names; } | LC_ALL=C sort)
    elif [[ $COMP_CWORD -eq 2 ]]; then
        variant=${COMP_WORDS[1]}
        while IFS= read -r candidate; do
            candidate=${candidate%.toml}
            [[ $candidate == "$cur"* ]] && COMPREPLY+=("$candidate")
        done < <(_ccc_config_files "$variant")
    else
        COMPREPLY=()
    fi
}

complete -F _ccc_use ccc-use
