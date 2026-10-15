# Source from an interactive remote zsh. No command or application is wrapped.
[[ -o interactive ]] || return 0

typeset -g _MOSHERDR_COLORS_HELPER="${${(%):-%N}:A:h:h}/colors.py"

_mosherdr_colors_precmd() {
  if [[ ${_MOSHERDR_OWNS_COLORTERM:-0} == 1 ]]; then
    # Preserve a value that the user or a command changed after our preexec.
    [[ ${COLORTERM-} != truecolor ]] || unset COLORTERM
    unset _MOSHERDR_OWNS_COLORTERM
  fi
  return 0
}

_mosherdr_colors_preexec() {
  _mosherdr_colors_precmd
  [[ -n ${TMUX-} && -n ${TMUX_PANE-} && -r $_MOSHERDR_COLORS_HELPER ]] || return 0
  local capability
  capability=$(command python3 -B "$_MOSHERDR_COLORS_HELPER" ensure </dev/null 2>/dev/null) || return 0
  if [[ $capability == truecolor && ${+NO_COLOR} == 0 && ${+COLORTERM} == 0 ]]; then
    export COLORTERM=truecolor
    typeset -g _MOSHERDR_OWNS_COLORTERM=1
  fi
  return 0
}

autoload -Uz add-zsh-hook
add-zsh-hook -d preexec _mosherdr_colors_preexec
add-zsh-hook -d precmd _mosherdr_colors_precmd
add-zsh-hook preexec _mosherdr_colors_preexec
add-zsh-hook precmd _mosherdr_colors_precmd
