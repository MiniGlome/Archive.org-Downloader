import requests
import random, string
from concurrent import futures
from tqdm import tqdm
import time
from datetime import datetime
import argparse
import os
import sys
import shutil
import json
import re
import base64
import hashlib
from Crypto.Cipher import AES
from Crypto.Util import Counter

def display_error(response, message):
	print(message)
	print(response)
	print(response.text)
	exit()

def get_book_infos(session, url):
	r = session.get(url).text
	infos_url = "https:" + r.split('"url":"')[1].split('"')[0].replace("\\u0026", "&")
	response = session.get(infos_url)
	data = response.json()['data']
	title = data['brOptions']['bookTitle'].strip().replace(" ", "_")
	title = ''.join( c for c in title if c not in '<>:"/\\|?*' ) # Filter forbidden chars in directory names (Windows & Linux)
	title = title[:150] # Trim the title to avoid long file names	
	metadata = data['metadata']
	links = []
	for item in data['brOptions']['data']:
		for page in item:
			links.append(page['uri'])

	if len(links) > 1:
		print(f"[+] Found {len(links)} pages")
		return title, links, metadata
	else:
		print(f"[-] Error while getting image links")
		exit()

def login(email, password):
	session = requests.Session()
	session.get("https://archive.org/account/login")

	data = {"username":email, "password":password}

	response = session.post("https://archive.org/account/login", data=data)
	if "bad_login" in response.text:
		print("[-] Invalid credentials!")
		exit()
	elif "Successful login" in response.text:
		print("[+] Successful login")
		return session
	else:
		display_error(response, "[-] Error while login:")

def loan(session, book_id, verbose=True):
	data = {
		"action": "grant_access",
		"identifier": book_id
	}
	response = session.post("https://archive.org/services/loans/loan/searchInside.php", data=data)
	data['action'] = "browse_book"
	response = session.post("https://archive.org/services/loans/loan/", data=data)

	if response.status_code == 400 :
		try:
			if response.json()["error"] == "This book is not available to borrow at this time. Please try again later.":
				print("This book doesn't need to be borrowed")
				return session
			else :
				display_error(response, "Something went wrong when trying to borrow the book.")
		except: # The response is not in JSON format
			display_error(response, "The book cannot be borrowed")

	data['action'] = "create_token"
	response = session.post("https://archive.org/services/loans/loan/", data=data)

	if "token" in response.text:
		if verbose:
			print("[+] Successful loan")
		return session
	else:
		display_error(response, "Something went wrong when trying to borrow the book, maybe you can't borrow this book.")

def return_loan(session, book_id):
	data = {
		"action": "return_loan",
		"identifier": book_id
	}
	response = session.post("https://archive.org/services/loans/loan/", data=data)
	if response.status_code == 200 and response.json()["success"]:
		print("[+] Book returned")
	else:
		display_error(response, "Something went wrong when trying to return the book")

def image_name(pages, page, directory):
	return f"{directory}/{(len(str(pages)) - len(str(page))) * '0'}{page}.jpg"

def deobfuscate_image(image_data, link, obf_header):
	"""
	@Author: https://github.com/justimm
	Decrypts the first 1024 bytes of image_data using AES-CTR.
	The obfuscation_header is expected in the form "1|<base64encoded_counter>"
	where the base64-decoded counter is 16 bytes.
	We derive the AES key by taking the SHA-1 digest of the image URL (with protocol/host removed)
	and using the first 16 bytes.
	For AES-CTR, we use a 16-byte counter block. The first 8 bytes are used as a fixed prefix,
	and the remaining 8 bytes (interpreted as a big-endian integer) are used as the initial counter value.
	"""
	try:
		version, counter_b64 = obf_header.split('|')
	except Exception as e:
		raise ValueError("Invalid X-Obfuscate header format") from e

	if version != '1':
		raise ValueError("Unsupported obfuscation version: " + version)

	# Derive AES key: replace protocol/host in link with '/'
	aesKey = re.sub(r"^https?:\/\/.*?\/", "/", link)
	sha1_digest = hashlib.sha1(aesKey.encode('utf-8')).digest()
	key = sha1_digest[:16]

	# Decode the counter (should be 16 bytes)
	counter_bytes = base64.b64decode(counter_b64)
	if len(counter_bytes) != 16:
		raise ValueError(f"Expected counter to be 16 bytes, got {len(counter_bytes)}")

	prefix = counter_bytes[:8]
	initial_value = int.from_bytes(counter_bytes[8:], byteorder='big')

	# Create AES-CTR cipher with a 64-bit counter length.
	ctr = Counter.new(64, prefix=prefix, initial_value=initial_value, little_endian=False)
	cipher = AES.new(key, AES.MODE_CTR, counter=ctr)

	decrypted_part = cipher.decrypt(image_data[:1024])
	new_data = decrypted_part + image_data[1024:]
	return new_data	

def download_one_image(session, link, i, directory, book_id, pages):
	headers = {
		"Referer": "https://archive.org/",
		"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
		"Sec-Fetch-Site": "same-site",
		"Sec-Fetch-Mode": "no-cors",
		"Sec-Fetch-Dest": "image",
	}
	retry = True
	response = None
	while retry:
		try:
			response = session.get(link, headers=headers)
			if response.status_code == 403:
				session = loan(session, book_id, verbose=False)
				raise Exception("Borrow again")
			elif response.status_code == 200:
				retry = False
		except:
			time.sleep(1)	# Wait 1 second before retrying

	image = image_name(pages, i, directory)

	obf_header = response.headers.get("X-Obfuscate")
	image_content = None
	if obf_header:
		try:
			image_content = deobfuscate_image(response.content, link, obf_header)
		except Exception as e:
			print(f"[ERROR] Deobfuscation failed: {e}")
			return
	else:
		image_content = response.content
	
	with open(image, "wb") as f:
		f.write(image_content)

def download(session, n_threads, directory, links, scale, book_id):
	print("Downloading pages...")
	links = [f"{link}&rotate=0&scale={scale}" for link in links]
	pages = len(links)

	tasks = []
	with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
		for link in links:
			i = links.index(link)
			tasks.append(executor.submit(download_one_image, session=session, link=link, i=i, directory=directory, book_id=book_id, pages=pages))
		for task in tqdm(futures.as_completed(tasks), total=len(tasks)):
			pass
	
	images = [image_name(pages, i, directory) for i in range(len(links))]
	return images

def make_pdf(pdf, title, directory):
	file = title+".pdf"
	# Handle the case where multiple books with the same name are downloaded
	i = 1
	while os.path.isfile(os.path.join(directory, file)):
		file = f"{title}({i}).pdf"
		i += 1

	with open(os.path.join(directory, file),"wb") as f:
		f.write(pdf)
	print(f"[+] PDF saved as \"{file}\"")

if __name__ == "__main__":

	my_parser = argparse.ArgumentParser()
	my_parser.add_argument('-e', '--email', help='Your archive.org email', type=str, required=True)
	my_parser.add_argument('-p', '--password', help='Your archive.org password', type=str, required=True)
	my_parser.add_argument('-u', '--url', help='Link to the book (https://archive.org/details/XXXX). You can use this argument several times to download multiple books', action='append', type=str)
	my_parser.add_argument('-d', '--dir', help='Output directory', type=str)
	my_parser.add_argument('-f', '--file', help='File where are stored the URLs of the books to download', type=str)
	my_parser.add_argument('-r', '--resolution', help='Image resolution (10 to 0, 0 is the highest), [default 3]', type=int, default=3)
	my_parser.add_argument('-t', '--threads', help="Maximum number of threads, [default 50]", type=int, default=50)
	my_parser.add_argument('-j', '--jpg', help="Output to individual JPG's rather than a PDF", action='store_true')
	my_parser.add_argument('-m', '--meta', help="Output the metadata of the book to a json file", action='store_true')
	my_parser.add_argument('-M', '--meta-only', help="Only download metadata without images or PDF", action='store_true')

	if len(sys.argv) == 1:
		my_parser.print_help(sys.stderr)
		sys.exit(1)
	args = my_parser.parse_args()

	if args.url is None and args.file is None:
		my_parser.error("At least one of --url and --file required")

	email = args.email
	password = args.password
	scale = args.resolution
	n_threads = args.threads
	d = args.dir

	if d == None:
		d = os.getcwd()
	elif not os.path.isdir(d):
		print(f"Output directory does not exist!")
		exit()

	if args.url is not None:
		urls = args.url
	else:
		if os.path.exists(args.file):
			with open(args.file) as f:
				urls = f.read().strip().split("\n")
		else:
			print(f"{args.file} does not exist!")
			exit()

	# Check the urls format
	for url in urls:
		if not url.startswith("https://archive.org/details/"):
			print(f"{url} --> Invalid url. URL must starts with \"https://archive.org/details/\"")
			exit()

	print(f"{len(urls)} Book(s) to download")
	session = login(email, password)

	for url in urls:
		book_id = list(filter(None, url.split("/")))[3]
		print("="*40)
		print(f"Current book: https://archive.org/details/{book_id}")
		title, links, metadata = get_book_infos(session, url)

		if args.meta or args.meta_only:
			print("Writing metadata.json...")
			# Write metadata to output directory
			metadata_path = os.path.join(d, f"{title}_metadata.json")
			i = 1
			while os.path.exists(metadata_path):
				metadata_path = os.path.join(d, f"{title}_metadata({i}).json")
				i += 1
			with open(metadata_path,'w') as f:
				json.dump(metadata,f)

		if not args.meta_only:
			# Create temporary directory for images
			directory = os.path.join(d, title)
			# Handle the case where multiple books with the same name are downloaded
			i = 1
			_directory = directory
			while os.path.isdir(directory):
				directory = f"{_directory}({i})"
				i += 1
			os.makedirs(directory)
			# Only loan the book if we need to download images
			session = loan(session, book_id)
			images = download(session, n_threads, directory, links, scale, book_id)

			if not args.jpg: # Create pdf with images and remove the images folder
				import img2pdf

				# prepare PDF metadata
				# sometimes archive metadata is missing
				pdfmeta = { }
				# ensure metadata are str
				for key in ["title", "creator", "associated-names"]:
					if key in metadata:
						if isinstance(metadata[key], str):
							pass
						elif isinstance(metadata[key], list):
							metadata[key] = "; ".join(metadata[key])
						else:
							raise Exception("unsupported metadata type")
				# title
				if 'title' in metadata:
					pdfmeta['title'] = metadata['title']
				# author
				if 'creator' in metadata and 'associated-names' in metadata:
					pdfmeta['author'] = metadata['creator'] + "; " + metadata['associated-names']
				elif 'creator' in metadata:
					pdfmeta['author'] = metadata['creator']
				elif 'associated-names' in metadata:
					pdfmeta['author'] = metadata['associated-names']
				# date
				if 'date' in metadata:
					try:
						pdfmeta['creationdate'] = datetime.strptime(metadata['date'][0:4], '%Y')
					except:
						pass
				# keywords
				pdfmeta['keywords'] = [f"https://archive.org/details/{book_id}"]

				pdf = img2pdf.convert(images, **pdfmeta)
				make_pdf(pdf, title, args.dir if args.dir != None else "")
			# Clean up temporary directory
			for filename in os.listdir(directory):
				file_path = os.path.join(directory, filename)
				try:
					if os.path.isfile(file_path) or os.path.islink(file_path):
						os.unlink(file_path)
					elif os.path.isdir(file_path):
						shutil.rmtree(file_path)
				except Exception as e:
					print(f"Failed to delete {file_path}. Reason: {e}")
			try:
				os.rmdir(directory)  # Remove the empty directory
			except Exception as e:
				print(f"Failed to remove directory {directory}. Reason: {e}")
			
			# Return the loan since we borrowed it for downloading
			return_loan(session, book_id)
