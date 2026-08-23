"""Offline unit tests: python -m unittest discover -s tests"""
import base64
import hashlib
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "archive-org-downloader.py")
spec = importlib.util.spec_from_file_location("downloader", SCRIPT)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)


class ParseBookUrlTests(unittest.TestCase):
    def test_details_urls(self):
        cases = {
            "https://archive.org/details/abc123": ("archive", "abc123", None),
            "http://www.archive.org/details/abc123/": ("archive", "abc123", None),
            "archive.org/details/abc123": ("archive", "abc123", None),
            "https://archive.org/details/abc123/page/n5/mode/2up": ("archive", "abc123", None),
            "https://archive.org/details/abc123?view=theater#page/n7": ("archive", "abc123", None),
            "https://archive.org/details/abc123/mode/2up": ("archive", "abc123", None),
            "https://archive.org/stream/abc123/abc123_djvu.txt": ("archive", "abc123", None),
            "https://archive.org/details/bdrc-W1KG16651/bdrc-W1KG16651-I1KG16693": ("archive", "bdrc-W1KG16651", "bdrc-W1KG16651-I1KG16693"),
            "abc123": ("archive", "abc123", None),
            "  https://archive.org/details/abc123  ": ("archive", "abc123", None),
            "https://openlibrary.org/books/OL7141826M/Earth_evolution": ("openlibrary", "OL7141826M", None),
            "https://openlibrary.org/works/OL45804W": ("openlibrary", "OL45804W", None),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(dl.parse_book_url(url), expected)

    def test_rejects_bad_urls(self):
        for url in ["https://example.com/details/abc", "https://archive.org/", "https://archive.org/details/",
                    "https://archive.org/search?query=x", "https://openlibrary.org/authors/OL1A"]:
            with self.subTest(url=url):
                with self.assertRaises(dl.DownloaderError):
                    dl.parse_book_url(url)


class SafeFilenameTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(dl.safe_filename("Too busy to cook? : recipes", "id"), "Too_busy_to_cook__recipes")

    def test_newlines_and_trailing_dots(self):
        self.assertEqual(dl.safe_filename("Vanishing Los Angeles\n     \n   County...", "id"), "Vanishing_Los_Angeles_County")

    def test_empty_and_reserved(self):
        self.assertEqual(dl.safe_filename("", "fallback"), "fallback")
        self.assertEqual(dl.safe_filename("???", "fallback"), "fallback")
        self.assertEqual(dl.safe_filename("con", "fallback"), "fallback")
        self.assertEqual(dl.safe_filename(None, "fallback"), "fallback")

    def test_byte_length_cap(self):
        name = dl.safe_filename("中" * 200, "id")
        self.assertLessEqual(len(name.encode("utf-8")), 150)
        self.assertTrue(name)


class ImageChecksTests(unittest.TestCase):
    def test_jpeg_validation(self):
        good = b"\xff\xd8" + b"\x00" * 1000 + b"\xff\xd9"
        self.assertTrue(dl.is_valid_jpeg(good))
        self.assertFalse(dl.is_valid_jpeg(good[:-2]))  # truncated
        self.assertFalse(dl.is_valid_jpeg(b"<html>" + b"x" * 1000))
        self.assertFalse(dl.is_valid_jpeg(b"\xff\xd8\xff\xd9"))  # too small

    def test_deobfuscate_round_trip(self):
        link = "https://ia800506.us.archive.org/BookReader/BookReaderImages.php?zip=/x.zip&file=y.jp2&id=z&rotate=0&scale=0"
        counter = os.urandom(16)
        key = hashlib.sha1(b"/BookReader/BookReaderImages.php?zip=/x.zip&file=y.jp2&id=z&rotate=0&scale=0").digest()[:16]
        plain = b"\xff\xd8" + os.urandom(3000) + b"\xff\xd9"
        ctr = dl.Counter.new(64, prefix=counter[:8], initial_value=int.from_bytes(counter[8:], "big"))
        encrypted = dl.AES.new(key, dl.AES.MODE_CTR, counter=ctr).encrypt(plain[:1024]) + plain[1024:]
        header = "1|" + base64.b64encode(counter).decode()
        self.assertEqual(dl.deobfuscate_image(encrypted, link, header), plain)

    def test_deobfuscate_rejects_unknown_version(self):
        with self.assertRaises(ValueError):
            dl.deobfuscate_image(b"x" * 2000, "https://a/b", "2|" + base64.b64encode(b"0" * 16).decode())
        with self.assertRaises(ValueError):
            dl.deobfuscate_image(b"x" * 2000, "https://a/b", "garbage")


class MetadataTests(unittest.TestCase):
    def test_pdf_metadata(self):
        meta = dl.pdf_metadata({"title": ["A", "B"], "creator": "X", "associated-names": ["Y", "Z"], "date": "c1909"}, "id1")
        self.assertEqual(meta["title"], "A; B")
        self.assertEqual(meta["author"], "X; Y; Z")
        self.assertEqual(meta["creationdate"].year, 1909)
        self.assertIsNotNone(meta["creationdate"].tzinfo)  # naive datetimes crash img2pdf on Windows
        self.assertEqual(meta["keywords"], ["https://archive.org/details/id1"])

    def test_pdf_metadata_missing(self):
        meta = dl.pdf_metadata({"date": {"weird": 1}}, "id1")
        self.assertNotIn("creationdate", meta)
        self.assertNotIn("author", meta)

    def test_metadata_to_str(self):
        self.assertEqual(dl.metadata_to_str(["a", ["b", "c"], 3]), "a; b; c; 3")
        self.assertEqual(dl.metadata_to_str({"k": 1}), '{"k": 1}')


class PathTests(unittest.TestCase):
    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "book.pdf")
            self.assertEqual(dl.unique_path(p), p)
            open(p, "w").close()
            self.assertEqual(dl.unique_path(p), os.path.join(d, "book(1).pdf"))
            open(os.path.join(d, "book(1).pdf"), "w").close()
            self.assertEqual(dl.unique_path(p), os.path.join(d, "book(2).pdf"))
            folder = os.path.join(d, "book")
            os.makedirs(folder)
            self.assertEqual(dl.unique_path(folder), os.path.join(d, "book(1)"))

    def test_page_filename_padding(self):
        self.assertTrue(dl.page_filename("dir", 5, 232).endswith(os.path.join("dir", "005.jpg")))
        self.assertTrue(dl.page_filename("dir", 5, 10).endswith("5.jpg"))
        self.assertTrue(dl.page_filename("dir", 5, 11).endswith("05.jpg"))

    def test_read_url_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("﻿https://archive.org/details/a  \r\n\r\n# comment\r\n https://archive.org/details/b\r\n")
        try:
            self.assertEqual(dl.read_url_file(f.name), ["https://archive.org/details/a", "https://archive.org/details/b"])
        finally:
            os.remove(f.name)


class ReaderUrlTests(unittest.TestCase):
    def test_image_archive_prefixes(self):
        item = {"files": [{"name": "book_jp2.zip"}, {"name": "book_orig_jp2.tar"}, {"name": "vol2/book2_jp2.zip"},
                          {"name": "book.pdf"}, {"name": "book_jp2.zip"}, {"name": "book_raw_jp2.zip"}]}
        self.assertEqual(dl.image_archive_prefixes(item), ["book", "vol2/book2"])

    def test_image_archive_prefixes_natural_order(self):
        item = {"files": [{"name": f"it-{i}_jp2.zip"} for i in (10, 11, 1, 2)] + [{"name": "it_jp2.zip"}]}
        self.assertEqual(dl.image_archive_prefixes(item), ["it", "it-1", "it-2", "it-10", "it-11"])

    def test_book_info_preview_detection(self):
        data = {"brOptions": {"bookTitle": " T ", "data": [[{"uri": "https://x/BookReader/BookReaderPreview.php?id=a&page=leaf1&fail=preview&"}]]},
                "lendingInfo": {"isLendingRequired": True}, "metadata": {}}
        info = dl.BookInfo(data)
        self.assertTrue(info.is_preview_only)
        self.assertEqual(info.title, "T")
        data["brOptions"]["data"] = [[{"uri": "https://x/BookReader/BookReaderImages.php?zip=a&file=b&id=c"}]]
        self.assertFalse(dl.BookInfo(data).is_preview_only)


if __name__ == "__main__":
    unittest.main()
