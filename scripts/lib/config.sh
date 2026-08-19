# shellcheck shell=bash
# Configuration resolution:
#   OCRL_* env  >  repo .opencode-review-loop.json  >  user config  >  defaults

ocrl_user_config_path() {
    printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/opencode-review-loop/config.json"
}

# ocrl_config_defaults -- a JSON object with every supported key.
ocrl_config_defaults() {
    cat <<'JSON'
{
  "model": "openai/gpt-5.6-sol",
  "variant": "",
  "block_severity": "low",
  "timeout_sec": 900,
  "max_failures": 2,
  "max_stop_blocks": 3,
  "max_defers": 3,
  "verify_cmd": "",
  "pure": true,
  "disable_project_config": false,
  "chunk_diff_bytes": 400000,
  "hard_diff_ceiling": 8388608,
  "max_file_bytes": 16777216,
  "max_reason_bytes": 32768,
  "max_findings": 200,
  "max_findings_bytes": 65536,
  "allow_dirty": false,
  "ttl_hours": 24,
  "ignore_globs": []
}
JSON
}

# Environment overrides, as a JSON object built only from variables that are set.
ocrl_config_from_env() {
    local out='{}' key var val
    for key in model variant block_severity timeout_sec max_failures max_stop_blocks \
        max_defers verify_cmd pure disable_project_config chunk_diff_bytes \
        hard_diff_ceiling max_file_bytes max_reason_bytes max_findings \
        max_findings_bytes allow_dirty ttl_hours ignore_globs; do
        var="OCRL_${key^^}"
        [ -n "${!var+set}" ] || continue
        val=${!var}
        case "$key" in
            pure | disable_project_config | allow_dirty)
                case "$val" in
                    1 | true | TRUE | yes | on) val=true ;;
                    *) val=false ;;
                esac
                out=$(jq -c --arg k "$key" --argjson v "$val" '.[$k]=$v' <<<"$out")
                ;;
            timeout_sec | max_failures | max_stop_blocks | max_defers | chunk_diff_bytes | \
                hard_diff_ceiling | max_file_bytes | max_reason_bytes | max_findings | \
                max_findings_bytes | ttl_hours)
                [[ $val =~ ^[0-9]+$ ]] || continue
                out=$(jq -c --arg k "$key" --argjson v "$val" '.[$k]=$v' <<<"$out")
                ;;
            ignore_globs)
                # Comma-separated in the environment, an array everywhere else.
                out=$(jq -c --arg k "$key" --arg v "$val" \
                    '.[$k]=($v | split(",") | map(select(length>0)))' <<<"$out")
                ;;
            *)
                out=$(jq -c --arg k "$key" --arg v "$val" '.[$k]=$v' <<<"$out")
                ;;
        esac
    done
    printf '%s' "$out"
}

ocrl_read_json_file() {
    local path=$1
    if [ -f "$path" ] && jq -e 'type=="object"' "$path" >/dev/null 2>&1; then
        jq -c . "$path"
    else
        printf '{}'
    fi
}

# ocrl_config_load <repo_root> -- merged config JSON on stdout.
OCRL_CONFIG='{}'
ocrl_config_load() {
    local repo=$1 defaults user repo_cfg env_cfg
    defaults=$(ocrl_config_defaults)
    user=$(ocrl_read_json_file "$(ocrl_user_config_path)")
    repo_cfg='{}'
    [ -n "$repo" ] && repo_cfg=$(ocrl_read_json_file "$repo/.opencode-review-loop.json")
    env_cfg=$(ocrl_config_from_env)
    OCRL_CONFIG=$(jq -cs '.[0] * .[1] * .[2] * .[3]' \
        <(printf '%s' "$defaults") \
        <(printf '%s' "$user") \
        <(printf '%s' "$repo_cfg") \
        <(printf '%s' "$env_cfg"))
    printf '%s' "$OCRL_CONFIG"
}

# ocrl_cfg <key> -- scalar value from the loaded config.
ocrl_cfg() {
    jq -r --arg k "$1" '.[$k] // "" | if type=="array" then join(",") else tostring end' <<<"$OCRL_CONFIG"
}

# ocrl_cfg_array <key> -- one element per line.
ocrl_cfg_array() {
    jq -r --arg k "$1" '(.[$k] // []) | .[]' <<<"$OCRL_CONFIG"
}

# Severity ranking used by the blocking rule.
ocrl_severity_rank() {
    case "${1,,}" in
        info | trivial | nit) printf '1' ;;
        low | minor) printf '2' ;;
        medium | moderate | major) printf '3' ;;
        high | serious) printf '4' ;;
        critical | blocker | fatal) printf '5' ;;
        # An unrecognised severity is treated as the most severe: an unparsable
        # label must never be a way to slip past the gate.
        *) printf '5' ;;
    esac
}
