# shellcheck shell=bash
# Configuration resolution:
#   OCRL_* env  >  repo .opencode-review-loop.json  >  user config  >  defaults

ocrl_user_config_path() {
    printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/opencode-review-loop/config.json"
}

# Every supported key, with its default. A plain variable rather than a
# heredoc: ocrl_config_defaults used to fork cat(1) on every hook invocation.
# shellcheck disable=SC2034  # read by the other library files
OCRL_CONFIG_DEFAULTS='{
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
}'

ocrl_config_defaults() { printf '%s' "$OCRL_CONFIG_DEFAULTS"; }

# Environment overrides, as a JSON object built only from variables that are set.
ocrl_config_from_env() {
    local key var val
    local -a args=()
    local filter='{}'
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
                args+=(--argjson "v_$key" "$val")
                filter+=" | .[\"$key\"] = \$v_$key"
                ;;
            timeout_sec | max_failures | max_stop_blocks | max_defers | chunk_diff_bytes | \
                hard_diff_ceiling | max_file_bytes | max_reason_bytes | max_findings | \
                max_findings_bytes | ttl_hours)
                [[ $val =~ ^[0-9]+$ ]] || continue
                args+=(--argjson "v_$key" "$val")
                filter+=" | .[\"$key\"] = \$v_$key"
                ;;
            ignore_globs)
                # Comma-separated in the environment, an array everywhere else.
                args+=(--arg "v_$key" "$val")
                filter+=" | .[\"$key\"] = (\$v_$key | split(\",\") | map(select(length>0)))"
                ;;
            *)
                args+=(--arg "v_$key" "$val")
                filter+=" | .[\"$key\"] = \$v_$key"
                ;;
        esac
    done
    if [ "${#args[@]}" -eq 0 ]; then
        printf '{}'
        return 0
    fi
    jq -cn "${args[@]}" "$filter"
}

# ocrl_config_load <repo_root> -- merged config JSON, also left in OCRL_CONFIG.
#
# Precedence: defaults < user < repo < environment. One jq call does the whole
# merge; reading and validating each file separately cost up to six.
OCRL_CONFIG='{}'
ocrl_config_load() {
    local repo=$1 env_cfg user
    local -a files=()
    user=$(ocrl_user_config_path)
    [ -f "$user" ] && files+=("$user")
    [ -n "$repo" ] && [ -f "$repo/.opencode-review-loop.json" ] &&
        files+=("$repo/.opencode-review-loop.json")
    env_cfg=$(ocrl_config_from_env)

    # A malformed config file fails the whole jq, and the defaults stand rather
    # than a half-applied merge.
    OCRL_CONFIG=$(jq -cs \
        --argjson defaults "$OCRL_CONFIG_DEFAULTS" \
        --argjson env "$env_cfg" \
        'reduce .[] as $f ($defaults; if ($f | type) == "object" then . * $f else . end) * $env' \
        "${files[@]}" </dev/null 2>/dev/null)
    [ -n "$OCRL_CONFIG" ] || OCRL_CONFIG=$(jq -c --argjson e "$env_cfg" '. * $e' <<<"$OCRL_CONFIG_DEFAULTS")
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
