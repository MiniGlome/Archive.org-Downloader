![made-with-python](https://img.shields.io/badge/Made%20with-Python3-brightgreen)

<!-- LOGO -->
<br />
<p align="center">
  <img src="https://user-images.githubusercontent.com/54740007/108192715-e5958c80-7114-11eb-8240-e884895bb45f.png" alt="Logo" width="80" height="80">

  <h3 align="center">Archive.org-Downloader</h3>

  <p align="center">
    Python3 script to download archive.org books in PDF format
    <br />
    </p>
</p>

> This is a maintained fork of [MiniGlome/Archive.org-Downloader](https://github.com/MiniGlome/Archive.org-Downloader)
> (which also offers a web version at [archive-dl.com](https://archive-dl.com/)).
> The script was rewritten for reliability after studying the upstream issue tracker, the open pull requests and
> the current archive.org BookReader / lending API. See [What changed in this fork](#what-changed-in-this-fork).

## About The Project

There are many great books available on https://openlibrary.org/ and https://archive.org/, however, you can only borrow them for 1 hour to 14 days and you don't have the option to download them as a PDF to read offline. This program retrieves the page scans of a book and assembles them into a PDF.

The download takes from a few seconds to a few minutes depending on the number of pages and the resolution you select. You must have an account on https://archive.org/ for the script to work.

## Getting Started

You need Python 3.9 or newer: https://www.python.org/downloads/

### Installation

```sh
git clone https://github.com/DABH/Archive.org-Downloader.git
cd Archive.org-Downloader
python3 -m venv venv               # optional but recommended
source venv/bin/activate           # Windows: venv\Scripts\activate
python3 -m pip install -r requirements.txt
```

The script needs the modules `requests`, `tqdm`, `img2pdf` and `pycryptodome`. If you get
`ModuleNotFoundError`, install the requirements with the **same** Python you run the script with
(`python3 -m pip install ...`, not just `pip install ...`).

## Usage

```
usage: archive-org-downloader.py [-h] [-e EMAIL] [-p PASSWORD] [-u URL] [-d DIR] [-f FILE] [-r [0-10]] [-t THREADS]
                                 [-j] [-m] [--keep-images] [--resume] [--retries RETRIES] [--version]

Download archive.org / openlibrary.org books as PDF.

options:
  -h, --help            show this help message and exit
  -e, --email EMAIL     Your archive.org email
  -p, --password PASSWORD
                        Your archive.org password
  -u, --url URL         Link to the book (https://archive.org/details/XXXX), an openlibrary.org link, or a bare
                        identifier. Can be used several times to download multiple books
  -d, --dir DIR         Output directory (default: current directory)
  -f, --file FILE       File where the URLs of the books to download are stored (one per line)
  -r, --resolution [0-10]
                        Image resolution: 0 is the highest (original scan), 10 the lowest. archive.org halves the size
                        at 2, 4 and 8 (1 = 0, 3 = 2, 5-7 = 4). [default 3]
  -t, --threads THREADS
                        Maximum number of download threads [default 10]
  -j, --jpg             Output individual JPG files rather than a PDF
  -m, --meta            Also write the book metadata to a JSON file
  --keep-images         Keep the page images after the PDF is built
  --resume              Reuse an existing output folder and skip pages that were already downloaded
  --retries RETRIES     Attempts per page before giving up [default 8]
  --version             show program's version number and exit

Credentials can also be given with the ARCHIVE_ORG_EMAIL and ARCHIVE_ORG_PASSWORD environment variables; the password
is prompted for when it is not provided at all.
```

### Examples

Download a book at the highest resolution:

```sh
python3 archive-org-downloader.py -e you@example.com -p 'your-password' -r 0 -u https://archive.org/details/toobusytocooktim00losa
```

Keep the password out of your shell history (you will be prompted for it):

```sh
export ARCHIVE_ORG_EMAIL=you@example.com
python3 archive-org-downloader.py -u https://archive.org/details/toobusytocooktim00losa
```

Download several books listed in a text file (one URL per line, `#` starts a comment) into a folder, as JPG pages with metadata:

```sh
python3 archive-org-downloader.py -f books.txt -d ~/Books -j -m
```

Accepted book links: `https://archive.org/details/ID` (with or without `/page/...`, `/mode/2up`, `?view=theater`),
`https://archive.org/details/ID/VOLUME` for one volume of a multi-volume item (all volumes are downloaded when
no volume is given), `https://openlibrary.org/books/OL...M/...`, `https://openlibrary.org/works/OL...W`, or just the identifier.

### Resolution

archive.org serves the scans in power-of-two reductions: `-r 0` (or 1) is the original scan, `-r 2` (or 3) is half size,
`-r 4` a quarter, `-r 8` an eighth. A 230-page book is roughly 250 MB at `-r 0` and 90 MB at `-r 2`.

## Troubleshooting

* **Update first.** archive.org changes its login/reader APIs a few times a year; most "it stopped working" reports are
  fixed by `git pull`.
* **`Login failed: invalid credentials`** although the password works in the browser: your shell probably mangled it.
  Quote the password (`-p 'p@ss$word'`), or use the `ARCHIVE_ORG_PASSWORD` environment variable / the interactive prompt.
* **`cannot borrow this book: ...`**: the book is not lendable for your account right now (waitlist, print-disabled only,
  loan limit reached). The script refuses to produce a PDF of "page unavailable" placeholders.
* **Some pages failed**: the downloaded pages are kept; run the same command again with `--resume`.
* **Windows**: run the script as `python archive-org-downloader.py ...` (double-clicking or omitting `python` drops the arguments).
* **`No module named 'Crypto'`**: `python3 -m pip install pycryptodome` (the Debian package `python3-pycryptodome` also works).

## What changed in this fork

Compared with upstream (`ea0d807`, August 2026) the script was rewritten with the same command line interface plus a few
options. Fixes, in order of how often users hit them:

* **No more endless hangs.** Every request has a timeout; page downloads retry a bounded number of times with backoff,
  honour `Retry-After`, and give up with a clear error instead of spinning forever on 404/429/5xx. Ctrl-C stops the
  download immediately and still returns the loan.
* **Lost loans are recovered.** archive.org answers **404** (not 403) when a loan lapses; the script now re-borrows on
  401/403/404/redirects, refreshes the loan token every two minutes during long downloads (as the web reader does),
  and serialises the re-borrow so 50 threads do not storm the loan API.
* **No more silent placeholder PDFs.** Borrowing decisions come from the reader's `lendingInfo` instead of matching an
  English error string; books that cannot be borrowed (waitlist, print-disabled only, throttled account) fail with an
  explanation; every page is checked to be a real, complete JPEG before it is accepted.
* **No more crash after a complete download.** Timezone-aware PDF dates (fixes the Windows `OSError: [Errno 22]`
  for pre-1970 books, upstream PR #159), metadata of any shape, PDF streamed to disk instead of built in RAM, missing or
  corrupt pages reported before `img2pdf` runs, page images kept and resumable when something fails.
* **Better item discovery.** Uses the metadata API to detect non-existent items, items without a book reader, and
  multi-volume items; the reader URL is scraped with a regex and rebuilt from the metadata API if the page layout
  changes again.
* **Login.** CSRF-token JSON login with an `xauthn` fallback, precise error messages (bad password vs. API change),
  credentials from environment variables or an interactive prompt, `pycryptodome` **and** `pycryptodomex`/Debian
  `Cryptodome` import support, and a dependency check that names the interpreter to install into.
* **Small things.** URL parsing with `urllib` (query strings, `www.`, `http://`, `/stream/`, Open Library links, bare
  identifiers, `#` comments and CRLF in `-f` files), safe file names (newlines, trailing dots, reserved names, byte
  length), `-m` works without `-j`, `--keep-images`, `--resume`, exit code 1 when a book failed, a multi-book batch no
  longer aborts on the first failure, default of 10 threads.

Run the offline unit tests with `python3 -m unittest discover -s tests`.

## License

This project is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md), like the upstream project.
Use it only for books you are allowed to download, and respect archive.org's terms of use.
