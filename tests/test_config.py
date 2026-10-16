import json
from pathlib import Path
import tempfile
import unittest

import config


class ConfigTests(unittest.TestCase):
    def test_hosts_and_colors(self):
        value = {"version": 1, "hosts": {"example-host": {"colors": {
            "enabled": True, "foreground": "#123456", "background": "#abcdef"}}}}
        self.assertEqual(config.validate(value), value)

    def test_no_shell_or_ssh_options_in_alias(self):
        for host in ("-oProxyCommand=x", "user@host", "host;id", "host\n", "$(id)", ""):
            with self.subTest(host=host), self.assertRaises(config.ConfigError):
                config.validate_host(host)

    def test_palette_requires_explicit_complete_profile(self):
        for profile in ({"enabled": True}, {"enabled": "yes"},
                        {"enabled": True, "foreground": "red", "background": "#000000"}):
            with self.assertRaises(config.ConfigError):
                config.validate_colors(profile)

    def test_explicit_palette_requires_exactly_sixteen_rgb_colors(self):
        palette = ["#%06x" % value for value in range(16)]
        profile = {"enabled": True, "foreground": "#ffffff", "background": "#000000",
                   "palette": palette}
        self.assertEqual(config.validate_colors(profile), profile)
        for invalid in (palette[:15], palette + ["#000000"], " ".join(palette),
                        [*palette[:15], "red"], [*palette[:15], 123]):
            with self.subTest(invalid=invalid if isinstance(invalid, str) else len(invalid)), \
                    self.assertRaises(config.ConfigError):
                config.validate_colors({**profile, "palette": invalid})

    def test_missing_config_disables_hosts(self):
        with tempfile.TemporaryDirectory() as path:
            self.assertEqual(config.load_config(Path(path) / "missing.json")["hosts"], {})

    def test_malformed_or_unknown_settings_rejected(self):
        with tempfile.TemporaryDirectory() as path:
            target = Path(path) / "config.json"
            for value in ('{broken', json.dumps({"credentials": "not-supported"})):
                target.write_text(value)
                with self.assertRaises(config.ConfigError):
                    config.load_config(target)


if __name__ == "__main__":
    unittest.main()
