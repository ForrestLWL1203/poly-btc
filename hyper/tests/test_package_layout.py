import unittest
from pathlib import Path


class PackageLayoutTests(unittest.TestCase):
    def test_hyper_root_contains_only_shared_python_foundations(self):
        package_root = Path(__file__).resolve().parents[1]
        root_modules = {path.name for path in package_root.glob("*.py")}

        self.assertEqual(
            root_modules,
            {"__init__.py", "config.py", "params.py", "storage.py", "util.py"},
        )

    def test_business_responsibility_directories_are_packages(self):
        package_root = Path(__file__).resolve().parents[1]

        for name in ("copy", "discovery", "execution", "market", "ops", "selection"):
            with self.subTest(package=name):
                self.assertTrue((package_root / name / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
