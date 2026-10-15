import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


HOOK = Path(__file__).resolve().parents[1] / "hooks/remote.zsh"


class RemoteHookTests(unittest.TestCase):
    def run_hook(self, script, capability="truecolor", stdin=""):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "fixture.py"
            helper.write_text("import sys\nassert sys.argv[1:] == ['ensure']\n"
                              "assert sys.stdin.buffer.read() == b''\n"
                              + ("sys.exit(1)\n" if capability is None else "print(" + repr(capability) + ")\n"))
            prefix = ("source " + shlex.quote(str(HOOK)) + "\n"
                      "typeset -g _MOSHERDR_COLORS_HELPER=" + shlex.quote(str(helper)) + "\n"
                      "export TMUX=/tmp/fixture,1,0 TMUX_PANE=%7\n"
                      "unset COLORTERM NO_COLOR FORCE_COLOR\n")
            environment = os.environ.copy()
            for name in ("COLORTERM", "NO_COLOR", "FORCE_COLOR"):
                environment.pop(name, None)
            result = subprocess.run(["zsh", "-f", "-i", "-c", prefix + script],
                                    input=stdin, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=environment,
                                    timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.splitlines()

    def test_verified_truecolor_is_exported_then_removed_at_prompt(self):
        output = self.run_hook("""
_mosherdr_colors_preexec
print -r -- "${COLORTERM-unset}:${_MOSHERDR_OWNS_COLORTERM-unset}"
command python3 -c 'import os; print(os.environ.get("COLORTERM", "unset"))'
_mosherdr_colors_precmd
print -r -- "${COLORTERM-unset}:${_MOSHERDR_OWNS_COLORTERM-unset}"
""")
        self.assertEqual(output, ["truecolor:1", "truecolor", "unset:unset"])

    def test_no_color_and_existing_values_are_preserved_including_empty_values(self):
        for setup in ("export NO_COLOR=1", "export NO_COLOR=''"):
            with self.subTest(setup=setup):
                output = self.run_hook(setup + "\n_mosherdr_colors_preexec\nprint -r -- ${COLORTERM-unset}\n_mosherdr_colors_precmd\nprint -r -- ${+NO_COLOR}")
                self.assertEqual(output, ["unset", "1"])
        for value in ("configured", ""):
            with self.subTest(value=value):
                output = self.run_hook("export COLORTERM=" + shlex.quote(value) + "\n_mosherdr_colors_preexec\n_mosherdr_colors_precmd\nprint -r -- \"${COLORTERM-unset}:${+COLORTERM}\"")
                self.assertEqual(output, [value + ":1"])

    def test_prompt_preserves_later_user_change(self):
        self.assertEqual(self.run_hook("_mosherdr_colors_preexec\nexport COLORTERM=chosen\n_mosherdr_colors_precmd\nprint -r -- $COLORTERM"), ["chosen"])

    def test_generic_path_preserves_term_proxy_force_color_and_typeahead(self):
        output = self.run_hook("""
export TERM=screen HTTPS_PROXY=fixture FORCE_COLOR=1
_mosherdr_colors_preexec
read -r typed
print -r -- "$TERM:$HTTPS_PROXY:$FORCE_COLOR:$typed"
_mosherdr_colors_precmd
print -r -- "$TERM:$HTTPS_PROXY:$FORCE_COLOR"
""", stdin="pending input\n")
        self.assertEqual(output, ["screen:fixture:1:pending input", "screen:fixture:1"])

    def test_unsupported_or_palette_only_never_asserts_rgb_or_force_color(self):
        for capability in ("", "palette", "unexpected", None):
            with self.subTest(capability=capability):
                self.assertEqual(self.run_hook("_mosherdr_colors_preexec\nprint -r -- \"${COLORTERM-unset}:${FORCE_COLOR-unset}\"", capability), ["unset:unset"])

    def test_sourcing_twice_preserves_existing_hooks_traps_and_aliases(self):
        output = self.run_hook("""
existing_preexec() { :; }
existing_precmd() { :; }
preexec_functions+=(existing_preexec)
precmd_functions+=(existing_precmd)
TRAPWINCH() { :; }
alias untouched='print fixture'
source """ + shlex.quote(str(HOOK)) + """
source """ + shlex.quote(str(HOOK)) + """
print -r -- "${(j:,:)preexec_functions}"
print -r -- "${(j:,:)precmd_functions}"
print -r -- "${+functions[TRAPWINCH]}:${aliases[untouched]}"
""")
        self.assertEqual(output, ["existing_preexec,_mosherdr_colors_preexec",
                                  "existing_precmd,_mosherdr_colors_precmd", "1:print fixture"])

    def test_noninteractive_source_has_no_side_effect(self):
        result = subprocess.run(["zsh", "-f", "-c", "source " + shlex.quote(str(HOOK)) + "; print -r -- ${+functions[_mosherdr_colors_preexec]}"],
                                text=True, capture_output=True, check=False, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "0\n", ""))


if __name__ == "__main__":
    unittest.main()
