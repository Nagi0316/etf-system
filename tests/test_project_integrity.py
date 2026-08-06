import ast
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAMING_ROOTS = (
    "deployment",
    "routes",
    "scripts",
    "services",
    "static",
    "templates",
    "tests",
)
PUBLIC_PAGES = (
    "/",
    "/auth",
    "/backtest",
    "/etf-detail/0050",
    "/etf-list",
    "/login",
    "/notifications",
    "/portfolio",
    "/profile",
    "/watchlist",
)


class ProjectIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_public_pages_render(self):
        for path in PUBLIC_PAGES:
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(200, response.status_code)
                self.assertIn("text/html", response.headers.get("content-type", ""))

    def test_static_assets_use_automatic_cache_version(self):
        response = self.client.get("/")
        self.assertNotIn("v=20260806", response.text)
        self.assertRegex(
            response.text,
            r"/static/css/tailwind\.min\.css\?v=[a-zA-Z0-9_-]+",
        )

    def test_security_and_static_cache_headers(self):
        response = self.client.get("/")
        policy = response.headers.get("content-security-policy", "")
        self.assertNotIn("unsafe-eval", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("base-uri 'self'", policy)

        asset = self.client.get("/static/favicon.svg?v=test")
        self.assertEqual(200, asset.status_code)
        self.assertEqual(
            "public, max-age=31536000, immutable",
            asset.headers.get("cache-control"),
        )

    def test_async_route_handlers_contain_real_async_work(self):
        redundant = []
        for route_file in (PROJECT_ROOT / "routes").glob("*_routes.py"):
            tree = ast.parse(route_file.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                is_route = any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"
                    for decorator in node.decorator_list
                )
                if is_route and not any(
                    isinstance(child, ast.Await) for child in ast.walk(node)
                ):
                    redundant.append(f"{route_file.name}:{node.name}")

        self.assertEqual([], redundant)

    def test_template_static_references_exist(self):
        references = set()
        for template in (PROJECT_ROOT / "templates").glob("*.html"):
            text = template.read_text(encoding="utf-8")
            references.update(re.findall(r'["\'](/static/[^"\'?]+)', text))

        self.assertTrue(references)
        missing = [path for path in sorted(references) if not (PROJECT_ROOT / path.lstrip("/")).is_file()]
        self.assertEqual([], missing)

    def test_custom_file_names_follow_ascii_snake_case(self):
        """Prevent ambiguous, encoded, or punctuation-heavy names from returning."""
        invalid = []
        paths = list(PROJECT_ROOT.glob("*.py"))
        for directory in NAMING_ROOTS:
            paths.extend((PROJECT_ROOT / directory).rglob("*"))

        for path in paths:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if any(part.startswith(".") for part in path.relative_to(PROJECT_ROOT).parts):
                continue
            if path.name == "__init__.py":
                continue
            logical_stem = path.name.split(".", 1)[0]
            if not re.fullmatch(r"[a-z][a-z0-9_]*", logical_stem):
                invalid.append(str(path.relative_to(PROJECT_ROOT)))

        self.assertEqual([], sorted(invalid))


if __name__ == "__main__":
    unittest.main()
