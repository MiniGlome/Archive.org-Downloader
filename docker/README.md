### Install Docker Desktop

See [here](https://www.docker.com/products/docker-desktop/)


### Configure Downloader

```shell
cd ./docker
```

Put your desired book URLs in a `./urls.txt` file:

```shell
echo 'https://archive.org/details/XXXX' >> ./urls.txt
```

Place the following contents in a `./.env` file:

```shell
AD_EMAIL='ARCHIVE.ORG_EMAIL'
AD_PASSWORD='ARCHIVE.ORG_PASSWORD'
AD_RESOLUTION='0'
```


### Run the Downloader

```shell
docker-compose run archive_downloader
```

You can find the downloaded PDFs at `./output/*.pdf`
