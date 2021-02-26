import requests
import random, string
import json
from tqdm import tqdm
import img2pdf
import time
import tempfile


def get_book_infos(session, url):
	r = session.get(url).text
	infos_url = "https:" + r[r.find('url:')+6 : r.find('type:')-21]
	response = session.get(infos_url)
	data = json.loads(response.text)['data']
	title = "".join([c for c in data['brOptions']['bookTitle'] if c in string.ascii_letters+string.digits+" "]).strip()
	links = []
	for item in data['brOptions']['data']:
		for page in item:
			links.append(page['uri'])

	if len(links) > 1:
		print(f"[+] Found {len(links)} pages")
		return title, links
	else:
		print(f"[-] Error while getting image links")
		exit()

def format_data(content_type, fields):
	data = ""
	for name, value in fields.items():
		data += f"--{content_type}\x0d\x0aContent-Disposition: form-data; name=\"{name}\"\x0d\x0a\x0d\x0a{value}\x0d\x0a"
	data += content_type+"--"
	return data

def login(email, password):
	session = requests.Session()
	session.get("https://archive.org/account/login")
	content_type = "----WebKitFormBoundary"+"".join(random.sample(string.ascii_letters + string.digits, 16))

	headers = {'Content-Type': 'multipart/form-data; boundary='+content_type}
	data = format_data(content_type, {"username":email, "password":password, "submit_by_js":"true"})

	response = session.post("https://archive.org/account/login", data=data, headers=headers)
	if "bad_login" in response.text:
		print("[-] Invalid credentials !")
		exit()
	elif "Successful login" in response.text:
		print("[+] Successful login")
		return session
	else:
		print("[-] Error while login :")
		print(response)
		print(response.text)
		exit()

def loan(session, book_id, verbose=True):
	data = {
		"action": "grant_access",
		"identifier": book_id
	}
	response = session.post("https://archive.org/services/loans/loan/searchInside.php", data=data)
	data['action'] = "browse_book"
	response = session.post("https://archive.org/services/loans/loan/", data=data)
	data['action'] = "create_token"
	response = session.post("https://archive.org/services/loans/loan/", data=data)

	if "token" in response.text:
		if verbose:
			print("[+] Successful loan")
		return session
	else:
		print("Something went wrong when trying to borrow the book")
		print(response)
		print(response.text)
		exit()

def download(session, directory, links, scale, book_id):
	headers = {
		"Referer": "https://archive.org/",
		"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
		"Sec-Fetch-Site": "same-site",
		"Sec-Fetch-Mode": "no-cors",
		"Sec-Fetch-Dest": "image",
	}
	print("Donwloading pages...")
	images = []
	for i in tqdm(range(len(links))):
		retry = True
		while retry:
			try:
				response = session.get(f"{links[i]}&rotate=0&scale={scale}", headers=headers)
				if response.status_code == 403:
					session = loan(session, book_id, verbose=False)
					raise Exception("Borrow again")
				retry = False
			except:
				# print(" Timeout, retrying in 1s")
				time.sleep(1)	# Wait 1 second before retrying
		image = f"{directory}/{i}.jpg"
		with open(image,"wb") as f:
			f.write(response.content)
		images.append(image)

	return images


def make_pdf(pdf, title):
	with open(f"{title}.pdf","wb") as f:
	    f.write(pdf)
	print(f"[+] PDF saved as \"{title}.pdf\"")

if __name__ == "__main__":

	email = input("Enter your email : ")
	password = input("Enter your password : ")
	url = input("Link to the book (https://archive.org/details/XXXX) : ")
	scale = int(input("Image resolution (10 to 0, 0 is the highest), [default 3] : ") or "3")	
	book_id = url.split("/")[-1]
	print("="*40)

	with tempfile.TemporaryDirectory() as directory:
		session = login(email, password)
		session = loan(session, book_id)
		title, links = get_book_infos(session, url)
		images = download(session, directory, links, scale, book_id)
		pdf = img2pdf.convert(images)

	make_pdf(pdf, title)

