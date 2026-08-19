from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PDF_RELATIVE = "packaging/Telegram名片工具-使用文档.pdf"
PDF_NAME = "Telegram名片工具-使用文档.pdf"


class PackagingAssetTests(unittest.TestCase):
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
            self.assertIn('cp "$PDF_GUIDE" "$PACKAGE_DIR/', content)

    def test_windows_builds_stage_pdf_next_to_executable(self):
        batch = (ROOT / "build-windows.bat").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/build-windows.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"dist\\TelegramCardTool\\{PDF_NAME}", batch)
        self.assertIn(f"dist\\TelegramCardTool\\{PDF_NAME}", workflow)


if __name__ == "__main__":
    unittest.main()
