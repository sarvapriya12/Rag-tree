import unittest

from app.file_graph import dependencies


class FileGraphTest(unittest.TestCase):
    def test_local_python_imports_resolve_only_known_files(self):
        paths = {"app/api.py", "app/auth.py", "app/util/helpers.py", "app/sub/service.py"}
        code = "from . import auth\nfrom .util import helpers\nimport requests\n"
        self.assertEqual(dependencies("app/api.py", code, paths),
                         {"app/auth.py", "app/util/helpers.py"})
        self.assertEqual(dependencies("app/sub/service.py", "from .. import auth", paths),
                         {"app/auth.py"})

    def test_typescript_relative_imports_resolve_index_files(self):
        paths = {"src/page.tsx", "src/components/Panel.tsx", "src/lib/index.ts"}
        code = "import Panel from './components/Panel';\nexport { helper } from './lib';\n"
        self.assertEqual(dependencies("src/page.tsx", code, paths),
                         {"src/components/Panel.tsx", "src/lib/index.ts"})


if __name__ == "__main__":
    unittest.main()
