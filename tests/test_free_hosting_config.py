"""免費代管的公開 URL、登入回呼與持久化資料庫安全設定。"""
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_SCRIPT = (
    "import application_config as c; "
    "print(c.APP_URL); print(c.GOOGLE_REDIRECT_URI); print(c.ASSET_VERSION)"
)


class FreeHostingConfigTests(unittest.TestCase):
    def run_config(self, **overrides):
        env = os.environ.copy()
        for key in (
            "APP_URL", "RENDER_EXTERNAL_URL", "RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA",
            "GOOGLE_REDIRECT_URI", "RENDER", "ENV", "DB_HOST", "DB_USER", "DB_PASSWORD",
        ):
            env.pop(key, None)
        # 本機 .env 可能含有 TiDB 憑證；明確清空，避免測試誤讀私人開發設定。
        for key in ("DB_HOST", "DB_USER", "DB_PASSWORD"):
            env[key] = ""
        env.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", CONFIG_SCRIPT], cwd=ROOT, env=env,
            capture_output=True, text=True, check=False,
        )

    def test_render_public_url_populates_oauth_callback_and_asset_version(self):
        result = self.run_config(
            RENDER_EXTERNAL_URL="https://etf-system.onrender.com",
            RENDER_GIT_COMMIT="abc0123456789def",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([
            "https://etf-system.onrender.com",
            "https://etf-system.onrender.com/api/auth/google/callback",
            "abc012345678",
        ], result.stdout.strip().splitlines())

    def test_explicit_custom_domain_and_google_callback_take_priority(self):
        result = self.run_config(
            APP_URL="https://invest.example.org/",
            RENDER_EXTERNAL_URL="https://etf-system.onrender.com",
            GOOGLE_REDIRECT_URI="https://invest.example.org/google/callback",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([
            "https://invest.example.org",
            "https://invest.example.org/google/callback",
        ], result.stdout.strip().splitlines()[:2])

    def test_render_fails_closed_without_persistent_database(self):
        result = self.run_config(RENDER="true", ENV="production", JWT_SECRET="test-secret")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DB_HOST", result.stderr)
        self.assertIn("SQLite", result.stderr)

    def test_render_allows_configured_mysql_credentials(self):
        result = self.run_config(
            RENDER="true", ENV="production", JWT_SECRET="test-secret",
            DB_HOST="mysql.example.org", DB_USER="user", DB_PASSWORD="password",
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
