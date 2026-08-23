#!/usr/bin/env python3
"""
Archive.org-Downloader
Download books from archive.org / openlibrary.org as PDF (or as page JPEGs).

The script logs into archive.org, borrows the book (only when the item requires it),
downloads every page image from the BookReader image server, decrypts the obfuscated
pages when needed, validates them, and assembles a PDF with the book's metadata.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from concurrent import futures
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

__version__ = "2.0.0"
REPO_URL = "https://github.com/DABH/Archive.org-Downloader"
USER_AGENT = f"Archive.org-Downloader/{__version__} (+{REPO_URL})"
# openlibrary.org's edge drops about half of the connections from non-browser user agents
BROWSER_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

# ---------------------------------------------------------------------------
# Dependencies: fail fast with a message that names the interpreter in use,
# because the most common "it doesn't work" report is a module installed for a
# different Python than the one running the script.
# ---------------------------------------------------------------------------


def _missing_dependency(module, pip_name):
    sys.stderr.write(
        f"[-] Python module '{module}' is not installed for {sys.executable}\n"
        f"    Install the requirements with:\n"
        f"        {sys.executable} -m pip install -r requirements.txt\n"
        f"    (or: {sys.executable} -m pip install {pip_name})\n"
    )
    sys.exit(2)


try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    _missing_dependency("requests", "requests")

try:
    from tqdm import tqdm
except ImportError:
    _missing_dependency("tqdm", "tqdm")

try:
    # pip's "pycryptodome" installs the Crypto namespace, Debian's python3-pycryptodome
    # (and pip's pycryptodomex) install the Cryptodome namespace. Accept both.
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
except ImportError:
    try:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util import Counter
    except ImportError:
        _missing_dependency("Crypto (pycryptodome)", "pycryptodome")

try:
    import img2pdf
except ImportError:  # only needed when building a PDF; checked again in main()
    img2pdf = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARCHIVE = "https://archive.org"
LOAN_URL = f"{ARCHIVE}/services/loans/loan/"
SEARCH_INSIDE_URL = f"{ARCHIVE}/services/loans/loan/searchInside.php"
CSRF_URL = f"{ARCHIVE}/services/csrf-token"
LOGIN_URL = f"{ARCHIVE}/services/account/login/"
XAUTHN_URL = f"{ARCHIVE}/services/xauthn/?op=login"
METADATA_URL = f"{ARCHIVE}/metadata/"
DETAILS_URL = f"{ARCHIVE}/details/"

DEFAULT_TIMEOUT = (15, 120)  # (connect, read) seconds
LOAN_TOKEN_REFRESH_SECONDS = 120  # the archive.org web reader polls create_token every 120 s
IMAGE_ARCHIVE_SUFFIXES = ("_jp2.zip", "_jp2.tar", "_tif.zip", "_tif.tar", "_jpg.zip", "_jpg.tar")
# archives of the same scan that are not a separate volume (e.g. book_raw_jp2.zip, book_orig_jp2.tar)
NOT_A_VOLUME_RE = re.compile(r"_(raw|orig|original|bw|color|colour|gray|grey|cropped|uncropped)$", re.I)
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

IMAGE_HEADERS = {
    "Referer": "https://archive.org/",
    "Accept": "image/jpeg,image/*;q=0.8,*/*;q=0.5",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Dest": "image",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DownloaderError(Exception):
    """A failure that aborts the current book (the run continues with the next one)."""


class LoginError(DownloaderError):
    """Login failed; aborts the whole run."""


class LoanError(DownloaderError):
    """The book could not be borrowed / the loan could not be refreshed."""


class PageError(Exception):
    """A single page could not be downloaded. `permanent` means retrying is pointless."""

    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def log(msg):
    tqdm.write(msg)


def response_summary(response, limit=300):
    """Short, human readable description of an HTTP response for error messages."""
    body = (response.text or "").strip()
    if "<html" in body[:500].lower():
        title = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
        body = "HTML page" + (f" ({title.group(1).strip()})" if title else "")
    return f"HTTP {response.status_code} {body[:limit]}"


def json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


class Session(requests.Session):
    """requests.Session with a default timeout, a User-Agent and a pool sized for our threads."""

    def __init__(self, pool_size=10):
        super().__init__()
        self.headers["User-Agent"] = USER_AGENT
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=max(10, pool_size), max_retries=0)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(method, url, **kwargs)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def login(session, email, password):
    """
    Log into archive.org. Tries the CSRF-token JSON login used by the website first,
    then the xauthn endpoint used by the official `internetarchive` package.
    """
    errors = []

    # Strategy 1: CSRF token + JSON login (what the archive.org website does).
    try:
        response = session.get(CSRF_URL)
        data = json_or_none(response)
        if not data or not data.get("success"):
            raise LoginError(f"could not get a login token: {response_summary(response)}")
        token = data["value"]["token"]
        headers = {"X-Csrf-Token": token, "Content-Type": "application/x-www-form-urlencoded"}
        payload = json.dumps({"username": email, "password": password, "t": token})
        response = session.post(LOGIN_URL, headers=headers, data=payload)
        data = json_or_none(response)
        if data and data.get("success"):
            if "logged-in-user" in session.cookies:
                return
            errors.append("login reported success but no session cookie was set")
        elif data and data.get("value") == "bad_login":
            raise LoginError("invalid credentials")
        else:
            errors.append(f"login endpoint answered {response_summary(response, 120)}")
    except requests.RequestException as e:
        errors.append(f"network error: {e}")

    # Strategy 2: xauthn (used by the `internetarchive` python package). Gives precise reasons.
    try:
        response = session.post(XAUTHN_URL, data={"email": email, "password": password})
        data = json_or_none(response) or {}
        if data.get("success") and "logged-in-user" in session.cookies:
            return
        reason = (data.get("values") or {}).get("reason") or data.get("error")
        if reason in ("account_bad_password", "account_not_found"):
            raise LoginError("invalid credentials")
        errors.append(f"xauthn answered {reason or response_summary(response, 120)}")
    except requests.RequestException as e:
        errors.append(f"network error: {e}")

    raise LoginError("; ".join(errors))


# ---------------------------------------------------------------------------
# URL / identifier handling
# ---------------------------------------------------------------------------

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OPENLIBRARY_RE = re.compile(r"^/(books|works)/(OL\d+[MW])(?:[/.]|$)")


def parse_book_url(url):
    """
    Turn a user supplied URL (or bare identifier) into ("archive", identifier, volume)
    or ("openlibrary", olid, None).

    Accepted forms:
      IDENTIFIER
      https://archive.org/details/IDENTIFIER[/VOLUME][/page/n5][/mode/2up][?view=theater]
      https://archive.org/stream/IDENTIFIER/...
      https://openlibrary.org/books/OL12345M[/Title]
      https://openlibrary.org/works/OL12345W[/Title]
    """
    url = url.strip()
    if IDENTIFIER_RE.match(url) and "." not in url[:1]:
        return "archive", url, None

    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower().split("@")[-1].split(":")[0]

    if host in ("openlibrary.org", "www.openlibrary.org"):
        m = OPENLIBRARY_RE.match(parts.path)
        if not m:
            raise DownloaderError(f"unsupported openlibrary.org URL: {url}")
        return "openlibrary", m.group(2), None

    if host not in ("archive.org", "www.archive.org"):
        raise DownloaderError(f"not an archive.org URL: {url}")

    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0] not in ("details", "stream", "embed"):
        raise DownloaderError(f"cannot find a book identifier in: {url} (expected https://archive.org/details/IDENTIFIER)")
    identifier = segments[1]
    if not IDENTIFIER_RE.match(identifier):
        raise DownloaderError(f"invalid archive.org identifier '{identifier}' in: {url}")
    volume = None
    if segments[0] == "details" and len(segments) > 2 and segments[2] not in ("page", "mode", "search", "theater"):
        volume = segments[2]  # multi-volume item: /details/ITEM/SUBPREFIX
    return "archive", identifier, volume


def resolve_openlibrary(session, olid):
    """Map an Open Library edition/work id to the archive.org identifier (ocaid)."""
    # archive.org indexes the Open Library ids of its scans; ask it first (openlibrary.org is flaky).
    field = "openlibrary_edition" if olid.endswith("M") else "openlibrary_work"
    queries = [f"{field}:{olid}"]
    if field == "openlibrary_work":  # prefer a lendable scan over a print-disabled-only one
        queries.insert(0, f"{field}:{olid} AND collection:inlibrary")
    for query in queries:
        try:
            response = session.get(f"{ARCHIVE}/advancedsearch.php", params={
                "q": query, "fl[]": "identifier", "rows": 5, "sort[]": "downloads desc", "output": "json"})
            docs = (json_or_none(response) or {}).get("response", {}).get("docs", [])
            if docs:
                return docs[0]["identifier"]
        except requests.RequestException:
            pass

    headers = {"User-Agent": BROWSER_USER_AGENT}
    for attempt in range(4):
        try:
            if olid.endswith("M"):
                data = session.get(f"https://openlibrary.org/books/{olid}.json", headers=headers).json()
                ocaid = data.get("ocaid")
            else:
                data = session.get(f"https://openlibrary.org/works/{olid}/editions.json?limit=100", headers=headers).json()
                ocaid = next((e["ocaid"] for e in data.get("entries", []) if e.get("ocaid")), None)
            if not ocaid:
                raise DownloaderError(f"openlibrary.org {olid} has no scanned copy on archive.org")
            return ocaid
        except (requests.RequestException, ValueError) as e:
            if attempt == 3:
                raise DownloaderError(f"could not resolve openlibrary.org {olid} to an archive.org book: {e}")
            time.sleep(2 * (attempt + 1))


def read_url_file(path):
    with open(path, encoding="utf-8-sig") as f:
        lines = [line.strip() for line in f.read().splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


# ---------------------------------------------------------------------------
# Item / book information
# ---------------------------------------------------------------------------


def get_item_metadata(session, identifier):
    response = session.get(f"{METADATA_URL}{identifier}")
    data = json_or_none(response)
    if response.status_code != 200 or data is None:
        raise DownloaderError(f"could not read item metadata for '{identifier}': {response_summary(response)}")
    if not data or "metadata" not in data:
        raise DownloaderError(f"item '{identifier}' does not exist on archive.org")
    if data.get("is_dark"):
        raise DownloaderError(f"item '{identifier}' is not available (it has been taken down)")
    return data


def image_archive_prefixes(item):
    """Return the BookReader sub-prefixes (one per volume) found in the item's files."""
    prefixes = []
    for f in item.get("files", []):
        name = f.get("name", "")
        for suffix in IMAGE_ARCHIVE_SUFFIXES:
            if name.endswith(suffix):
                prefix = name[: -len(suffix)]
                if prefix not in prefixes and not NOT_A_VOLUME_RE.search(prefix):
                    prefixes.append(prefix)
    # natural order: item, item-1, item-2, ... item-10 (not item-1, item-10, item-2)
    return sorted(prefixes, key=lambda p: [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", p)])


def find_reader_urls(session, identifier, item, volume=None):
    """
    Return a list of (subprefix, BookReaderJSIA url). The details page normally
    embeds the URL; if that scraping fails we build it from the metadata API.
    """
    prefixes = image_archive_prefixes(item)
    mediatype = item.get("metadata", {}).get("mediatype")
    if mediatype != "texts" and not prefixes:
        raise DownloaderError(
            f"'{identifier}' is not a book (mediatype={mediatype}); download its files from the item page instead")

    scraped = None
    try:
        response = session.get(f"{DETAILS_URL}{identifier}")
        m = re.search(r'"url":"(//[^"]*?BookReaderJSIA\.php[^"]*)"', response.text)
        if m:
            scraped = "https:" + m.group(1).replace("\\u0026", "&").replace("&amp;", "&")
    except requests.RequestException:
        pass

    server = item.get("server")
    directory = item.get("dir")
    if not scraped and not (server and directory):
        raise DownloaderError(f"'{identifier}' has no online book reader (no page images to download)")
    if not scraped and not prefixes:
        raise DownloaderError(
            f"'{identifier}' has no page images (no *_jp2.zip); this item cannot be read online")

    def build(subprefix):
        if scraped:
            parts = urlsplit(scraped)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["subPrefix"] = subprefix
            return urlunsplit(parts._replace(query=urlencode(query)))
        return (f"https://{server}/BookReader/BookReaderJSIA.php?"
                + urlencode({"id": identifier, "itemPath": directory, "server": server,
                             "format": "jsonp", "subPrefix": subprefix, "requestUri": f"/details/{identifier}"}))

    if volume:
        candidates = [p for p in prefixes if p == volume or p.endswith("/" + volume) or p.split("/")[-1] == volume]
        if not candidates:
            raise DownloaderError(f"volume '{volume}' not found in item '{identifier}' (available: {', '.join(prefixes) or 'none'})")
        return [(candidates[0], build(candidates[0]))]

    if scraped and (len(prefixes) <= 1):
        query = dict(parse_qsl(urlsplit(scraped).query))
        return [(query.get("subPrefix", identifier), scraped)]
    return [(p, build(p)) for p in prefixes] if prefixes else [(identifier, scraped)]


class BookInfo:
    def __init__(self, data):
        br = data.get("brOptions") or {}
        self.title = (br.get("bookTitle") or "").strip()
        self.lending = data.get("lendingInfo") or {}
        self.metadata = data.get("metadata") or {}
        self.leaves = [page for spread in (br.get("data") or []) for page in spread]
        self.raw = data

    @property
    def links(self):
        return [leaf["uri"] for leaf in self.leaves if leaf.get("uri")]

    @property
    def is_preview_only(self):
        return any("BookReaderPreview.php" in link or "fail=preview" in link for link in self.links)


def get_book_info(session, reader_url):
    response = session.get(reader_url)
    payload = json_or_none(response)
    if response.status_code != 200 or not isinstance(payload, dict) or "data" not in payload:
        raise DownloaderError(f"could not read the book reader data: {response_summary(response)}")
    info = BookInfo(payload["data"])
    if not info.leaves:
        raise DownloaderError("the book reader returned no pages")
    return info


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------


class Loan:
    """
    Manages the browse loan of one book. Thread-safe: many download threads may ask
    for a refresh at once, only one actually talks to the server.
    """

    def __init__(self, session, identifier):
        self.session = session
        self.identifier = identifier
        self.lock = threading.Lock()
        self.borrowed = False  # True when *we* took the loan (so we return it at the end)
        self.last_refresh = 0.0
        self.headers = {"Origin": ARCHIVE, "Referer": f"{DETAILS_URL}{identifier}"}

    def _post(self, action, url=LOAN_URL):
        response = self.session.post(url, data={"action": action, "identifier": self.identifier}, headers=self.headers)
        data = json_or_none(response)
        if not isinstance(data, dict):
            data = {"error": response_summary(response, 150)}
        return response.status_code, data

    def _create_token(self):
        status, data = self._post("create_token")
        if data.get("success") and "token" in json.dumps(data):
            self.last_refresh = time.monotonic()
            return True, None
        return False, data.get("error") or f"HTTP {status}"

    def borrow(self):
        """Start (or re-use) a browse loan and get an access token."""
        with self.lock:
            status, data = self._post("browse_book")
            if status == 401:
                raise LoanError("the session is not logged in anymore (please retry)")
            if not data.get("success"):
                error = data.get("error") or f"HTTP {status}"
                # A book we already hold: browse_book fails but create_token works.
                ok, _ = self._create_token()
                if not ok:
                    raise LoanError(f"could not borrow the book: {error}")
                return
            self.borrowed = True
            ok, error = self._create_token()
            if not ok:
                raise LoanError(f"borrowed, but could not get an access token: {error}")
            # Grants full-text search access; harmless if it fails.
            try:
                self._post("grant_access", SEARCH_INSIDE_URL)
            except requests.RequestException:
                pass

    def refresh(self, force=False):
        """Refresh the loan token; re-borrow if the loan lapsed. Rate limited across threads."""
        with self.lock:
            if not force and time.monotonic() - self.last_refresh < 20:
                return
            ok, error = self._create_token()
            if ok:
                return
            status, data = self._post("browse_book")
            if status == 401:
                raise LoanError("the session is not logged in anymore")
            if data.get("success"):
                self.borrowed = True
                ok, error = self._create_token()
                if ok:
                    return
            raise LoanError(f"lost access to the book and could not borrow it again: {error}")

    def give_back(self):
        if not self.borrowed:
            return
        try:
            status, data = self._post("return_loan")
        except requests.RequestException as e:
            log(f"[-] Could not return the book: {e}")
            return
        if data.get("success"):
            self.borrowed = False
            log("[+] Book returned")
        else:
            log(f"[-] Could not return the book: {data.get('error') or status}")


def describe_unavailable(lending):
    status = lending.get("lendingStatus") or {}
    if lending.get("isPrintDisabledOnly"):
        return "this book is only available to patrons with print disabilities"
    if status.get("user_at_max_loans"):
        return "your account has reached its maximum number of simultaneous loans"
    if status.get("users_on_waitlist") or status.get("available_to_waitlist"):
        return (f"no copy is available right now ({status.get('users_on_waitlist', 0)} on the waitlist, "
                f"next return: {status.get('next_browse_expiration') or status.get('next_borrow_expiration') or 'unknown'})")
    browsable = lending.get("isAvailableForBrowsing")
    if browsable is None:
        browsable = status.get("available_to_browse")
    if browsable is False and not lending.get("isAvailable"):
        return "no copy of this book is available to borrow at the moment"
    if status.get("is_login_required") and not lending.get("userid"):
        return "the session is not logged in"
    return None


# ---------------------------------------------------------------------------
# Page images
# ---------------------------------------------------------------------------


def deobfuscate_image(image_data, link, obf_header):
    """
    Decrypt the first 1024 bytes of an obfuscated page image (AES-128-CTR).
    The header is "1|<base64 16-byte counter>", the key is the first 16 bytes of the
    SHA-1 of the request URL with the scheme and host stripped ("/BookReader/...").
    Reverse engineered by https://github.com/justimm from archive.org's BookReader.
    """
    try:
        version, counter_b64 = obf_header.split("|", 1)
    except ValueError:
        raise ValueError(f"invalid X-Obfuscate header: {obf_header!r}")
    if version != "1":
        raise ValueError(f"unsupported obfuscation version {version!r} - the script needs an update ({REPO_URL})")

    key = hashlib.sha1(re.sub(r"^https?://.*?/", "/", link).encode("utf-8")).digest()[:16]
    counter_bytes = base64.b64decode(counter_b64)
    if len(counter_bytes) != 16:
        raise ValueError(f"expected a 16 byte counter, got {len(counter_bytes)}")
    ctr = Counter.new(64, prefix=counter_bytes[:8], initial_value=int.from_bytes(counter_bytes[8:], "big"))
    cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
    return cipher.decrypt(image_data[:1024]) + image_data[1024:]


def is_valid_jpeg(data):
    """Cheap structural check: JPEG start marker, and an end marker near the end (not truncated)."""
    return len(data) > 512 and data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-64:]


def is_placeholder(response, data):
    """archive.org serves a 'page temporarily unavailable' PNG when the reader has no access."""
    return (data[:8] == b"\x89PNG\r\n\x1a\n"
            or "image/png" in response.headers.get("Content-Type", "")
            or "preview-unavailable" in response.url)


def page_filename(directory, index, total):
    return os.path.join(directory, f"{index:0{len(str(total - 1))}d}.jpg")


def download_one_page(session, loan, link, path, stop, max_attempts, needs_loan):
    """Download one page image to `path`, validating it. Raises PageError on failure."""
    last_error = "unknown error"
    for attempt in range(max_attempts):
        if stop.is_set():
            raise PageError("cancelled")
        if attempt:
            time.sleep(min(30.0, 1.5 ** attempt) + random.random())
        try:
            response = session.get(link, headers=IMAGE_HEADERS, allow_redirects=False)
        except requests.RequestException as e:
            last_error = f"network error: {e}"
            continue

        status = response.status_code
        if status == 200:
            data = response.content
            obf_header = response.headers.get("X-Obfuscate")
            if obf_header:
                try:
                    data = deobfuscate_image(data, response.url, obf_header)
                except ValueError as e:
                    raise PageError(f"could not decrypt page: {e}", permanent=True)
            if is_placeholder(response, data):
                last_error = "archive.org served a 'page unavailable' placeholder (no access to this page)"
                if needs_loan and loan:
                    loan.refresh()
                continue
            if not is_valid_jpeg(data):
                last_error = f"invalid/truncated image data ({len(data)} bytes, {response.headers.get('Content-Type')})"
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return

        if status in (401, 403, 404) or 300 <= status < 400:
            last_error = f"HTTP {status} (access to the page was denied or the loan expired)"
            if needs_loan and loan:
                loan.refresh(force=(status != 404 or attempt > 0))
            elif status == 404 and attempt >= 2:
                raise PageError("HTTP 404 - this page image does not exist on the server", permanent=True)
            continue

        if status == 429 or status >= 500:
            retry_after = response.headers.get("Retry-After")
            last_error = f"HTTP {status}"
            if retry_after and retry_after.isdigit():
                time.sleep(min(60, int(retry_after)))
            continue

        last_error = f"unexpected HTTP {status}"
        if attempt >= 1:
            raise PageError(last_error, permanent=True)

    raise PageError(f"giving up after {max_attempts} attempts: {last_error}")


def download_pages(session, loan, links, directory, n_threads, max_attempts, needs_loan, resume):
    """
    Download all pages into `directory` (zero padded file names).
    Returns the list of (index, error) for pages that could not be downloaded.
    """
    total = len(links)
    paths = [page_filename(directory, i, total) for i in range(total)]
    todo = []
    for i, path in enumerate(paths):
        if resume and os.path.isfile(path):
            with open(path, "rb") as f:
                if is_valid_jpeg(f.read()):
                    continue
        todo.append(i)
    if resume and len(todo) < total:
        log(f"[+] Resuming: {total - len(todo)} pages already downloaded")

    stop = threading.Event()
    failures = {}

    def refresher():
        # Keep the loan token fresh while the download runs (as the web reader does).
        while not stop.wait(LOAN_TOKEN_REFRESH_SECONDS):
            try:
                loan.refresh(force=True)
            except (LoanError, requests.RequestException) as e:
                log(f"[-] Loan refresh failed: {e}")

    if needs_loan and loan:
        threading.Thread(target=refresher, daemon=True).start()

    fatal = None
    permanent = set()
    max_failures = max(10, len(todo) // 4)  # more than this and something is wrong with the whole book
    with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
        tasks = {}
        for i in todo:
            future = executor.submit(download_one_page, session, loan, links[i], paths[i], stop, max_attempts, needs_loan)
            tasks[future] = i
        try:
            for future in tqdm(futures.as_completed(tasks), total=len(tasks), unit="page",
                               initial=0, desc="Downloading", leave=True):
                i = tasks[future]
                try:
                    future.result()
                except PageError as e:
                    failures[i] = str(e)
                    if e.permanent:
                        permanent.add(i)
                    if len(failures) > max_failures:
                        fatal = DownloaderError(
                            f"aborting: {len(failures)} pages failed already (last error: {e})")
                        stop.set()
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                except DownloaderError as e:  # LoanError etc: nothing else can succeed
                    fatal = e
                    stop.set()
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
        except KeyboardInterrupt:
            stop.set()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            stop.set()

    if fatal:
        raise fatal

    # One more sequential pass over the failures (the bulk download may have raced a loan refresh).
    retryable = [i for i in sorted(failures) if i not in permanent]
    if retryable:
        log(f"[-] {len(retryable)} pages failed, retrying them one by one...")
        stop = threading.Event()
        if needs_loan and loan:
            try:
                loan.refresh(force=True)
            except DownloaderError as e:
                log(f"[-] {e}")
        for i in retryable:
            try:
                download_one_page(session, loan, links[i], paths[i], stop, max_attempts, needs_loan)
                del failures[i]
            except PageError as e:
                failures[i] = str(e)
            except DownloaderError as e:
                log(f"[-] {e}")
                break

    return sorted(failures.items())


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def safe_filename(title, fallback, max_bytes=150):
    """Make a title usable as a file/directory name on Windows, macOS and Linux."""
    name = re.sub(r"\s+", " ", str(title or "")).strip()
    name = "".join(c for c in name if c.isprintable())
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.replace(" ", "_").strip(" ._")
    if not name or name.upper() in WINDOWS_RESERVED_NAMES:
        name = fallback
    while len(name.encode("utf-8")) > max_bytes:
        name = name[:-1]
    return name.rstrip(" ._") or fallback


def unique_path(path):
    """Return `path`, or `path(1)`, `path(2)`... if it already exists."""
    root, ext = os.path.splitext(path)
    if ext.lower() not in (".pdf", ".json"):
        root, ext = path, ""
    candidate, i = path, 1
    while os.path.exists(candidate):
        candidate = f"{root}({i}){ext}"
        i += 1
    return candidate


def metadata_to_str(value):
    if isinstance(value, (list, tuple)):
        return "; ".join(metadata_to_str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def pdf_metadata(metadata, identifier):
    meta = {"keywords": [f"{DETAILS_URL}{identifier}"]}
    if metadata.get("title"):
        meta["title"] = metadata_to_str(metadata["title"])
    authors = [metadata_to_str(metadata[k]) for k in ("creator", "associated-names") if metadata.get(k)]
    if authors:
        meta["author"] = "; ".join(authors)
    year = re.search(r"\d{4}", metadata_to_str(metadata.get("date", "")))
    if year:
        # timezone-aware: a naive datetime makes img2pdf crash on Windows for years < 1970
        meta["creationdate"] = datetime(int(year.group()), 1, 1, tzinfo=timezone.utc)
    return meta


def build_pdf(images, pdf_path, meta):
    tmp = pdf_path + ".part"
    try:
        with open(tmp, "wb") as f:
            img2pdf.convert(images, outputstream=f, **meta)
        os.replace(tmp, pdf_path)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise DownloaderError(f"could not build the PDF: {e}")


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Per-book driver
# ---------------------------------------------------------------------------


def download_book(session, identifier, volume, args, output_dir):
    print("=" * 40)
    print(f"Current book: {DETAILS_URL}{identifier}" + (f" (volume {volume})" if volume else ""))

    item = get_item_metadata(session, identifier)
    readers = find_reader_urls(session, identifier, item, volume)
    if len(readers) > 1:
        print(f"[+] Multi-volume item: {len(readers)} volumes will be downloaded")

    loan = Loan(session, identifier)
    ok = True
    try:
        for subprefix, reader_url in readers:
            try:
                info = get_book_info(session, reader_url)
            except DownloaderError as e:
                if len(readers) > 1 and not volume:
                    print(f"[-] Skipping '{subprefix}': {e}")
                    continue
                raise
            needs_loan = bool(info.lending.get("isLendingRequired"))
            if needs_loan:
                reason = describe_unavailable(info.lending)
                already = info.lending.get("userHasBorrowed") or info.lending.get("userHasBrowsed")
                if reason and not already:
                    raise LoanError(f"cannot borrow this book: {reason}")
                loan.borrow()
                print("[+] Successful loan" if loan.borrowed else "[+] Book already borrowed by this account")
                info = get_book_info(session, reader_url)  # page links change once the loan is active
                if info.is_preview_only:
                    raise LoanError("the loan did not take effect: archive.org only offers preview pages "
                                    "(is the account throttled? try again later or with another account)")
            else:
                if info.is_preview_only:
                    reason = describe_unavailable(info.lending) or "it is not lendable"
                    raise DownloaderError(f"only a limited preview of this book is available: {reason}")
                print("[+] This book does not need to be borrowed")

            links = [f"{link}&rotate=0&scale={args.resolution}" for link in info.links]
            print(f"[+] Found {len(links)} pages")

            title = safe_filename(info.title or item.get("metadata", {}).get("title"), identifier)
            if len(readers) > 1:
                title = safe_filename(f"{title}_{subprefix.split('/')[-1]}", identifier)
            directory = os.path.join(output_dir, title)
            if not (args.resume and os.path.isdir(directory)):
                directory = unique_path(directory)
            os.makedirs(directory, exist_ok=True)

            failures = download_pages(session, loan, links, directory, args.threads, args.retries, needs_loan, args.resume)
            if failures:
                shown = ", ".join(str(i + 1) for i, _ in failures[:10]) + (" ..." if len(failures) > 10 else "")
                print(f"[-] {len(failures)} of {len(links)} pages could not be downloaded (pages {shown})")
                print(f"    last error: {failures[-1][1]}")
                print(f"    the downloaded pages are kept in \"{directory}\"; run again with --resume to retry")
                ok = False
                continue

            images = [page_filename(directory, i, len(links)) for i in range(len(links))]
            if args.jpg:
                if args.meta:
                    write_json(os.path.join(directory, "metadata.json"), info.metadata)
                print(f"[+] {len(images)} pages saved in \"{directory}\"")
            else:
                pdf_path = unique_path(os.path.join(output_dir, title + ".pdf"))
                build_pdf(images, pdf_path, pdf_metadata(info.metadata, identifier))
                if args.meta:
                    write_json(unique_path(os.path.join(output_dir, title + ".json")), info.metadata)
                print(f"[+] PDF saved as \"{pdf_path}\" ({len(images)} pages)")
                if not args.keep_images:
                    shutil.rmtree(directory, ignore_errors=True)
    finally:
        loan.give_back()
    return ok


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="Download archive.org / openlibrary.org books as PDF.",
        epilog="Credentials can also be given with the ARCHIVE_ORG_EMAIL and ARCHIVE_ORG_PASSWORD "
               "environment variables; the password is prompted for when it is not provided at all.")
    parser.add_argument("-e", "--email", default=os.environ.get("ARCHIVE_ORG_EMAIL"), help="Your archive.org email")
    parser.add_argument("-p", "--password", default=os.environ.get("ARCHIVE_ORG_PASSWORD"), help="Your archive.org password")
    parser.add_argument("-u", "--url", action="append",
                        help="Link to the book (https://archive.org/details/XXXX), an openlibrary.org link, "
                             "or a bare identifier. Can be used several times to download multiple books")
    parser.add_argument("-d", "--dir", help="Output directory (default: current directory)")
    parser.add_argument("-f", "--file", help="File where the URLs of the books to download are stored (one per line)")
    parser.add_argument("-r", "--resolution", type=int, default=3, metavar="[0-10]",
                        help="Image resolution: 0 is the highest (original scan), 10 the lowest. "
                             "archive.org halves the size at 2, 4 and 8 (1 = 0, 3 = 2, 5-7 = 4). [default 3]")
    parser.add_argument("-t", "--threads", type=int, default=10, help="Maximum number of download threads [default 10]")
    parser.add_argument("-j", "--jpg", action="store_true", help="Output individual JPG files rather than a PDF")
    parser.add_argument("-m", "--meta", action="store_true", help="Also write the book metadata to a JSON file")
    parser.add_argument("--keep-images", action="store_true", help="Keep the page images after the PDF is built")
    parser.add_argument("--resume", action="store_true",
                        help="Reuse an existing output folder and skip pages that were already downloaded")
    parser.add_argument("--retries", type=int, default=8, help="Attempts per page before giving up [default 8]")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv=None):
    parser = build_parser()
    if len(sys.argv) == 1 and argv is None:
        parser.print_help(sys.stderr)
        print("\nOn Windows, run the script with:  python archive-org-downloader.py ...", file=sys.stderr)
        return 1
    args = parser.parse_args(argv)

    if args.url is None and args.file is None:
        parser.error("at least one of --url and --file is required")
    if not 0 <= args.resolution <= 10:
        parser.error("--resolution must be between 0 (highest) and 10 (lowest)")
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if args.retries < 1:
        parser.error("--retries must be at least 1")
    if not args.jpg and img2pdf is None:
        _missing_dependency("img2pdf", "img2pdf")
    if not args.email:
        parser.error("no email given (use --email or the ARCHIVE_ORG_EMAIL environment variable)")
    if not args.password:
        if sys.stdin.isatty():
            args.password = getpass.getpass("archive.org password: ")
        else:
            parser.error("no password given (use --password or the ARCHIVE_ORG_PASSWORD environment variable)")

    output_dir = args.dir or os.getcwd()
    if not os.path.isdir(output_dir):
        print(f"[-] Output directory does not exist: {output_dir}")
        return 1

    urls = list(args.url or [])
    if args.file:
        if not os.path.exists(args.file):
            print(f"[-] {args.file} does not exist")
            return 1
        urls += read_url_file(args.file)
    if not urls:
        print("[-] No URLs to download")
        return 1

    books = []
    for url in urls:
        try:
            books.append(parse_book_url(url))
        except DownloaderError as e:
            print(f"[-] {e}")
            return 1

    print(f"{len(books)} book(s) to download")
    session = Session(pool_size=args.threads)
    try:
        login(session, args.email, args.password)
    except LoginError as e:
        print(f"[-] Login failed: {e}")
        if "credentials" in str(e):
            print("    Check the email/password. If the password contains special characters ($ ! ? & \" '...) "
                  "quote it, or use the ARCHIVE_ORG_PASSWORD environment variable instead.")
        else:
            print(f"    archive.org may have changed its login API: update the script from {REPO_URL}")
        return 1
    except requests.RequestException as e:
        print(f"[-] Login failed: network error: {e}")
        return 1
    print("[+] Successful login")

    failed = 0
    for kind, identifier, volume in books:
        try:
            if kind == "openlibrary":
                identifier = resolve_openlibrary(session, identifier)
            if not download_book(session, identifier, volume, args, output_dir):
                failed += 1
        except DownloaderError as e:
            print(f"[-] {e}")
            failed += 1
        except requests.RequestException as e:
            print(f"[-] Network error: {e}")
            failed += 1
        except KeyboardInterrupt:
            print("\n[-] Interrupted")
            return 130

    if failed:
        print("=" * 40)
        print(f"[-] {failed} of {len(books)} book(s) failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
