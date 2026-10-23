"""The .deb ships every module the agent imports (packaging/deb/build.sh).
Run: python -m unittest -v test_packaging
"""
import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _local_imports(module):
    with open(os.path.join(ROOT, module + ".py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
    return {n for n in names if os.path.isfile(os.path.join(ROOT, n + ".py"))}


class DebContentsTests(unittest.TestCase):
    def test_build_ships_every_module_status_imports(self):
        with open(os.path.join(ROOT, "packaging", "deb", "build.sh"), encoding="utf-8") as f:
            listed = set(re.findall(r"\b([a-z_]+)\.py\b", f.read()))
        needed, todo = set(), ["status"]
        while todo:
            m = todo.pop()
            if m not in needed:
                needed.add(m)
                todo.extend(_local_imports(m))
        self.assertEqual(sorted(needed - listed), [],
                         "add these to the file list in packaging/deb/build.sh")

    def test_build_ships_the_mes_contract_checker_and_schemas(self):
        with open(os.path.join(ROOT, "packaging", "deb", "build.sh"), encoding="utf-8") as f:
            text = f.read()
        for name in ("validate.py", "catalogue.json", "envelope.schema.json", "MANIFEST.sha256",
                     "events/*.schema.json"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
