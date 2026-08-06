import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
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

    def test_template_static_references_exist(self):
        references = set()
        for template in (PROJECT_ROOT / "templates").glob("*.html"):
            text = template.read_text(encoding="utf-8")
            references.update(re.findall(r'["\'](/static/[^"\'?]+)', text))

        self.assertTrue(references)
        missing = [path for path in sorted(references) if not (PROJECT_ROOT / path.lstrip("/")).is_file()]
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
