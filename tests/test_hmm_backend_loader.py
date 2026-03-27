import unittest
from unittest import mock

import bot


class HmmBackendLoaderTests(unittest.TestCase):
    def test_prefers_rust_module_when_available(self):
        rust_mod = object()

        with mock.patch("bot.importlib.import_module", return_value=rust_mod) as import_module:
            module, backend = bot._import_hmm_module()

        self.assertIs(module, rust_mod)
        self.assertEqual(backend, "rust")
        import_module.assert_called_once_with("doge_hmm")

    def test_falls_back_to_python_module(self):
        py_mod = object()

        def _import_side_effect(name: str):
            if name == "doge_hmm":
                raise ImportError("doge_hmm missing")
            if name == "hmm_regime_detector":
                return py_mod
            raise AssertionError(f"unexpected module import: {name}")

        with mock.patch("bot.importlib.import_module", side_effect=_import_side_effect) as import_module:
            module, backend = bot._import_hmm_module()

        self.assertIs(module, py_mod)
        self.assertEqual(backend, "python")
        self.assertEqual(import_module.call_count, 2)
        self.assertEqual(import_module.call_args_list[0].args[0], "doge_hmm")
        self.assertEqual(import_module.call_args_list[1].args[0], "hmm_regime_detector")

    def test_raises_if_both_backends_unavailable(self):
        def _import_side_effect(name: str):
            raise ImportError(f"{name} unavailable")

        with mock.patch("bot.importlib.import_module", side_effect=_import_side_effect):
            with self.assertRaises(ImportError) as ctx:
                bot._import_hmm_module()

        message = str(ctx.exception)
        self.assertIn("doge_hmm", message)
        self.assertIn("hmm_regime_detector", message)


if __name__ == "__main__":
    unittest.main()
