from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PDF_RELATIVE = "packaging/Telegram名片工具-使用文档.pdf"
PDF_NAME = "Telegram名片工具-使用文档.pdf"


class PackagingAssetTests(unittest.TestCase):
    def test_ui_describes_optional_phone_queries_in_plain_language(self):
        content = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.assertIn("补充 Telegram 姓名（缺少姓名时查询）", content)
        self.assertIn("过滤无法解析的号码（查询不到则跳过）", content)
        self.assertIn("极速直发（不添加通讯录", content)
        self.assertIn("默认发送前添加到 Telegram 通讯录", content)

    def test_application_imports_on_packaging_python(self):
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pdf_guide_exists(self):
        guide = ROOT / PDF_RELATIVE
        self.assertTrue(guide.is_file())
        self.assertGreater(guide.stat().st_size, 0)

    def test_pyinstaller_spec_collects_pdf(self):
        content = (ROOT / "Telegram名片工具.spec").read_text(encoding="utf-8")
        self.assertIn(PDF_RELATIVE, content)

    def test_macos_builds_stage_pdf_next_to_app(self):
        for script_name in ("build-macos.sh", "build-macos-universal.sh"):
            content = (ROOT / script_name).read_text(encoding="utf-8")
            self.assertIn(PDF_NAME, content)
            self.assertIn("PACKAGE_DIR", content)
            self.assertTrue(
                'cp "$PDF_GUIDE" "$PACKAGE_DIR/' in content
                or 'cp "$PDF_GUIDE" "$STAGED_PACKAGE_DIR/' in content
            )

    def test_macos_build_preserves_bundle_seal(self):
        content = (ROOT / "build-macos.sh").read_text(encoding="utf-8")
        self.assertIn("ditto --norsrc --noextattr", content)
        self.assertIn("codesign --verify --deep --strict", content)

    def test_windows_builds_stage_pdf_next_to_executable(self):
        batch = (ROOT / "build-windows.bat").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/build-windows.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"dist\\TelegramCardTool\\{PDF_NAME}", batch)
        self.assertIn(f"dist\\TelegramCardTool\\{PDF_NAME}", workflow)


if __name__ == "__main__":
    unittest.main()
