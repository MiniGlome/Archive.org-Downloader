FROM python:3.8-slim as requirements
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM requirements
COPY archive-org-downloader.py .
ENTRYPOINT ["/usr/local/bin/python", "archive-org-downloader.py"]
