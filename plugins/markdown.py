import re
import shutil
from pathlib import Path

from markdownify import markdownify as md

from .base import Plugin

# Image links produced from the chapter HTML: "./Images/x.png" or "../Images/x.png"
# (chapters in subfolders). Captures the file name.
_IMAGE_LINK_RE = re.compile(r"\]\((?:\./|(?:\.\./)+)?Images/([^)\s]+)")


class MarkdownPlugin(Plugin):
    def convert(self, html: str, title: str = "") -> str:
        markdown = md(
            html,
            heading_style="ATX",
            code_language_callback=self._detect_language,
            strip=["script", "style"],
        )

        markdown = self._fix_image_paths(markdown)
        markdown = self._clean_whitespace(markdown)

        if title and not markdown.startswith("#"):
            markdown = f"# {title}\n\n{markdown}"

        return markdown

    def save_chapter(self, html: str, title: str, output_path: Path) -> str:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        markdown = self.convert(html, title)
        output_path.write_text(markdown, encoding="utf-8")
        return markdown

    def generate_book(
        self,
        book_info: dict,
        chapters: list[tuple[str, str, str]],
        output_dir: Path,
    ):
        md_dir = output_dir / "Markdown"
        md_dir.mkdir(parents=True, exist_ok=True)

        readme = f"# {book_info.get('title', 'Unknown')}\n\n"
        readme += f"**Authors:** {', '.join(book_info.get('authors', []))}\n\n"
        readme += f"**Publishers:** {', '.join(book_info.get('publishers', []))}\n\n"
        readme += "## Chapters\n\n"

        images: set[str] = set()
        for filename, title, html in chapters:
            md_filename = filename.replace(".html", ".md").replace(".xhtml", ".md")
            markdown = self.save_chapter(html, title, md_dir / md_filename)
            images.update(_IMAGE_LINK_RE.findall(markdown))
            readme += f"- [{title}]({md_filename})\n"

        (md_dir / "README.md").write_text(readme, encoding="utf-8")
        self._copy_images(images, output_dir / "OEBPS" / "Images", md_dir / "Images")

    @staticmethod
    def _copy_images(names: set[str], source_dir: Path, target_dir: Path):
        """Copy the images the Markdown links to next to it (Markdown/Images/).

        The downloaded images live in OEBPS/Images, which is removed after an
        EPUB is built, so the Markdown export keeps its own copy.
        """
        for name in names:
            source = source_dir / name
            target = target_dir / name
            if source.is_file() and not target.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    def _detect_language(self, el):
        classes = el.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()

        for cls in classes:
            if cls.startswith("language-"):
                return cls.replace("language-", "")
            if cls.startswith("lang-"):
                return cls.replace("lang-", "")

        return None

    def _fix_image_paths(self, markdown: str) -> str:
        return re.sub(r"\]\(Images/", "](./Images/", markdown)

    def _clean_whitespace(self, markdown: str) -> str:
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        return markdown.strip() + "\n"
