![made-with-python](https://img.shields.io/badge/Made%20with-Python3-brightgreen)

<!-- LOGO -->
<br />
<p align="center">
  <img src="https://user-images.githubusercontent.com/54740007/108192715-e5958c80-7114-11eb-8240-e884895bb45f.png" alt="Logo" width="80" height="80">

  <h3 align="center">Archive.org-Downloader</h3>

  <p align="center">
    Python3 script to download archive.org books in PDF format !
    <br />
    </p>
</p>


## About The Project

There are many great books available on https://openlibrary.org/ and https://archive.org/, however, you can only borrow them for 1 hour to 14 days but you don't have the option to download it as a PDF to read it offline or share it with your friends. I want to create a program that can solve this problem and retrieve the original book in pdf format.

Of course, the download takes a few minutes depending on the number of pages and the quality of the images you have selected. You must also create an account on https://archive.org/ for the script to work.


## Getting Started
To get started you need to have python3 installed. If it is not the case you can download it here : https://www.python.org/downloads/

### Prerequisites
To run the the script you need install the python modules `requests`, `tqdm`, `img2pdf` with these commands :
```sh
pip install requests
pip install tqdm
pip install img2pdf
```

### Installation
Make sure you've already git installed. Then you can run the following commands to get the scripts on your computer:
   ```sh
   git clone https://github.com/MiniGlome/Archive.org-Downloader.git
   cd Archive.org-Downloader
   ```
   
## Usage
```sh
python3 downloader.py
```
You will be asked to enter an email and password to login to archive.org. Then enter the link to the book you want to download and specify the resolution of the images (0 is the best quality).
The PDF is downloaded in the current folder
