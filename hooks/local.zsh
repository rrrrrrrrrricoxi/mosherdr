# Observe one ordinary foreground connection; preserve mosh aliases/functions.
# Capture the helper beside this sourced file, rather than the caller's cwd.
typeset -g _mosherdr_helper="${${(%):-%N}:A:h:h}/agent.py"

function _mosherdr_restore {
  emulate -L zsh
  if [[ -n ${_mosherdr_monitor_pid-} ]]; then
    kill -TERM "$_mosherdr_monitor_pid" 2>/dev/null
  fi
  if [[ -n ${_mosherdr_source-} && -x ${_mosherdr_python-} ]]; then
    "$_mosherdr_python" -B "$_mosherdr_helper" cleanup \
      --source "$_mosherdr_source" </dev/null >/dev/null 2>&1
  fi
  unset _mosherdr_monitor_pid _mosherdr_source _mosherdr_python
  return 0
}

function _mosherdr_preexec {
  emulate -L zsh
  setopt no_bg_nice
  _mosherdr_restore
  [[ ${HERDR_ENV-} == 1 && ${HERDR_AGENT+x} != x ]] || return 0
  [[ ${HERDR_PANE_ID-} =~ '^w[123456789ABCDEFGHJKMNPQRSTVWXYZ0]+:p[123456789ABCDEFGHJKMNPQRSTVWXYZ0]+$' && ${HERDR_SOCKET_PATH-} == /* ]] || return 0
  [[ $1 != *$'\n'* && -r $_mosherdr_helper ]] || return 0
  local -a words
  words=("${(@z)1}")
  [[ ${words[1]-} == mosh && ${words[2]-} =~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' ]] || return 0
  local word
  for word in "${words[@]}"; do
    case $word in
      *';'*|*'&'*|*'|'*|*'<'*|*'>'*|*'('*|*')'*|*'{'*|*'}'*|*'$'*|*'`'*) return 0 ;;
    esac
  done
  local interpreter=${commands[python3]-}
  [[ -x $interpreter ]] || return 0
  "$interpreter" -B "$_mosherdr_helper" configured --host "${words[2]}" \
    </dev/null >/dev/null 2>&1 || return 0
  typeset -g _mosherdr_python=$interpreter
  typeset -g _mosherdr_source="custom:mosherdr.${$}.${RANDOM}.${RANDOM}"
  "$interpreter" -B "$_mosherdr_helper" watch --host "${words[2]}" --shell-pid $$ \
    --source "$_mosherdr_source" \
    </dev/null >/dev/null 2>&1 &!
  typeset -g _mosherdr_monitor_pid=$!
  return 0
}

# Re-sourcing is silent and preserves the user's hooks and signal traps.
typeset -ga preexec_functions precmd_functions
preexec_functions=("${(@)preexec_functions:#_mosherdr_preexec}" _mosherdr_preexec)
precmd_functions=(_mosherdr_restore "${(@)precmd_functions:#_mosherdr_restore}")
