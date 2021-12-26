#!/usr/bin/env python3

import argparse
import datetime
import time
from typing import Optional, Iterator, Dict, Set
import os
import json
import dataclasses
import threading
import string
import random
from urllib.request import Request, urlopen, urlretrieve
from urllib.parse import urlparse, parse_qs, urlencode
import queue
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
import logging

logger = logging.getLogger()

TOKEN_FILE = '.token.json'
LOCATIONS_FILE = '.file_locations.json'

CLIENT_CONFIG_FILE = 'client_config.json'

BASE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
AUTH_CALLBACK_PATH = '/oauth_callback'
AUTH_SCOPE = 'https://www.googleapis.com/auth/photoslibrary.readonly'

BASE_PHOTOS_API_URL = 'https://photoslibrary.googleapis.com'

TOKEN_URL = 'https://oauth2.googleapis.com/token'
TOKEN_HEADERS = {"Content-type": "application/x-www-form-urlencoded"}

@dataclasses.dataclass
class TokenData:
  access_token: str
  expire_time: int
  refresh_token: str

@dataclasses.dataclass
class ClientConfig:
  client_id: str
  client_secret: str

@dataclasses.dataclass
class FileLocation:
  relative_path: str
  absolute_path: str

@dataclasses.dataclass
class MediaItem:
  video: bool
  media_id: str
  filename: str
  base_url: str

  def download_url(self) -> str:
    return self.base_url + ('=dv' if self.video else '=d')

@dataclasses.dataclass
class Download:
  image: MediaItem
  location: FileLocation

class AuthCallbackHandler(BaseHTTPRequestHandler):

  def __init__(self, key_queue, *args):
    self.key_queue = key_queue
    super().__init__(*args)

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    parsed_url = urlparse(self.path)
    if parsed_url.path == AUTH_CALLBACK_PATH:
      query_params = parse_qs(parsed_url.query)
      message = None
      if 'code' in query_params and query_params['code']:
        self.key_queue.put(query_params['code'][0])
        message = 'Successfully authorized app.'
      elif 'error' in query_params and query_params['error']:
        message = 'OAuth error: {}'.format(query_params['error'][0])
      else:
        message = 'Unknown params for callback'
      self.wfile.write(message.encode('utf-8'))
      print(message)

  def log_request(*args, **kwargs):
    pass

def get_auth_token(client_config: ClientConfig) -> TokenData:
  key_queue = queue.Queue()
  server = HTTPServer(
      ('', 0), lambda *args: AuthCallbackHandler(key_queue, *args))
  server_thread = threading.Thread(target=server.serve_forever)
  server_thread.daemon = True
  server_thread.start()
  server_url = 'http://localhost:{}'.format(server.server_port)
  logging.debug('Listening at {}', server_url)
  redirect_uri = server_url + AUTH_CALLBACK_PATH

  code_verifier = ''.join(
      random.choices(string.ascii_letters + string.digits + '-._~', k = 128))
  auth_params = {
      'client_id': client_config.client_id,
      'redirect_uri': redirect_uri,
      'response_type': 'code',
      'scope': AUTH_SCOPE,
      'code_challenge': code_verifier,
      'code_challenge_method': 'plain'
  }
  auth_url = '{}?{}'.format(BASE_AUTH_URL, urlencode(auth_params))
  print('Waiting for auth, go to {}'.format(auth_url))
  key = key_queue.get()

  logging.debug('Shutting down server')
  server.shutdown()
  return make_auth_token_request(
    client_config,
    code=key,
    code_verifier=code_verifier,
    grant_type='authorization_code',
    redirect_uri=redirect_uri)

def refresh_auth_token(
    old_token: TokenData, client_config: ClientConfig
) -> TokenData:
  new_token = make_auth_token_request(
      client_config,
      refresh_token=old_token.refresh_token,
      grant_type='refresh_token')
  new_token.refresh_token = old_token.refresh_token
  return new_token

def make_auth_token_request(client_config: ClientConfig, **kwargs) -> TokenData:
  params = {
    'client_id': client_config.client_id,
    'client_secret': client_config.client_secret,
    **kwargs
  }
  request = Request(
      TOKEN_URL,
      data=urlencode(params).encode(),
      headers=TOKEN_HEADERS,
      method='POST')
  token_json = urlopen(request).read().decode()
  return decode_json_token(json.loads(token_json))

def decode_json_token(token_dict: dict) -> TokenData:
  refresh_token = (
      token_dict['refresh_token'] if 'refresh_token' in token_dict else None)
  expire_time = None
  if 'expires_in' in token_dict:
    expire_time = time.time() + int(token_dict['expires_in'])
  else: 
    expire_time = token_dict['expire_time']
  return TokenData(
      access_token=token_dict['access_token'],
      expire_time=expire_time,
      refresh_token=refresh_token)

def read_json_file(path: str) -> Optional[dict]:
  if not os.path.isfile(path):
    return None
  with open(path) as f:
    return json.loads(f.read())

def write_token(token: TokenData, token_file: str) -> None:
  with open(token_file, 'w') as f:
    f.write(json.dumps(dataclasses.asdict(token)))

def read_token(token_file: str) -> Optional[TokenData]:
  token_dict = read_json_file(token_file)
  if not token_dict:
    return None
  return decode_json_token(token_dict)

def list_images(
    token: TokenData,
    client_config: ClientConfig,
    token_file: str,
    max_images: int
) -> Iterator[MediaItem]:
  params = {'pageSize': '100'}
  images_returned = 0
  print('Requesting image list ', end='')
  while True:
    token = maybe_refresh_token(token, client_config, token_file)
    response = api_request('/v1/mediaItems?{}'.format(urlencode(params)), token)
    print('.', end='', flush=True)
    for media_item in response['mediaItems']:
      video = media_item['mediaMetadata'].get('video')
      if not video or video['status'] == 'READY':
        yield MediaItem(
            video,
            media_id=media_item['id'],
            filename=media_item['filename'],
            base_url=media_item['baseUrl'])
        images_returned = images_returned + 1
        if max_images and images_returned == max_images:
          print()
          return
    next_page_token = response.get('nextPageToken')
    if not next_page_token:
      print()
      return
    params['pageToken'] = next_page_token

def api_request(path: str, token: TokenData):
  url = BASE_PHOTOS_API_URL + path
  logging.debug('Making request to {}'.format(url))
  request = Request(
      url,
      headers={'Authorization': 'Bearer {}'.format(token.access_token)})
  return json.loads(urlopen(request).read().decode())

def maybe_refresh_token(
    token: TokenData, client_config: ClientConfig, token_file: str
) -> TokenData:
  remaining_time = token.expire_time - time.time() 
  if remaining_time < 0:
    logging.debug('Refreshing token')
    token = refresh_auth_token(token, client_config)
    write_token(token, token_file)
  return token

def read_client_config() -> ClientConfig:
  client_config = read_json_file(CLIENT_CONFIG_FILE)
  if not client_config:
    raise Exception('{} file required'.config(CLIENT_CONFIG_FILE))
  client_id = client_config.get('client_id')
  client_secret = client_config.get('client_secret')
  if not client_id or not client_secret:
    raise Exception('client_id and client_secret required within {}'.format(
      CLIENT_CONFIG_FILE))
  return ClientConfig(client_id=client_id, client_secret=client_secret)

def read_image_locations(locations_file: str) -> Dict[str, str]:
  return read_json_file(locations_file) or {}

def write_image_locations(
    image_locations: Dict[str, str], locations_file: str) -> None:
  with open(locations_file, 'w') as f:
    f.write(json.dumps(image_locations, indent=2))

def find_unused_file(
    preferred_name: str,
    output_dir: str,
    image_locations: Dict[str, str],
    pending_locations: Set[str]
) -> FileLocation:
  suffix = 0
  while True:
    relative = None
    if suffix:
      base_name, extension = os. path.splitext(preferred_name)
      relative = '{}-{}{}'.format(base_name, suffix, extension)
    else:
      relative = preferred_name
    absolute = os.path.join(output_dir, relative)
    if (not os.path.isfile(absolute) and
        relative not in image_locations.values() and
        relative not in pending_locations):
      return FileLocation(relative_path=relative, absolute_path=absolute)
    suffix = suffix + 1

class DownloadThread(threading.Thread):

  def __init__(self, pending_download_queue, completed_download_queue):
    super().__init__()
    self.pending_download_queue = pending_download_queue
    self.completed_download_queue = completed_download_queue

  def run(self):
    try:
      while True:
        download = self.pending_download_queue.get(False)
        logging.debug(
            'Download %s to %s',
            download.image.download_url(),
            download.location.absolute_path)
        urlretrieve(
            download.image.download_url(), download.location.absolute_path)
        self.completed_download_queue.put(download)
    except queue.Empty:
      #This is expected when downloading is done
      pass


parser = argparse.ArgumentParser(
    description='Syncs all google photos to a local directory.')
parser.add_argument('--output_dir', '-o', type=str, required=True)
parser.add_argument('--max_downloads', type=int, default=500)
parser.add_argument('--max_images_to_sync', type=int, default=None)
parser.add_argument('--download_threads', type=int, default=10)
args = parser.parse_args()

client_config = read_client_config()
token_file = os.path.join(args.output_dir, TOKEN_FILE)
token = read_token(token_file)
if not token:
  logging.debug('Getting a new token')
  token = get_auth_token(client_config)
  write_token(token, token_file)

locations_file = os.path.join(args.output_dir, LOCATIONS_FILE)
image_locations = read_image_locations(locations_file)

images_to_download = [
    i for i in list_images(token, client_config, token_file, args.max_images_to_sync)
    if i.media_id not in image_locations]
if args.max_downloads != -1 and args.max_downloads < len(images_to_download):
  raise Exception(
      'Number of images to download ({}) exceeds the max_downloads ({}). '
      'Increase the limit or use --max_downloads=-1'.format(
        len(images_to_download), args.max_downloads))

image_count_to_download = len(images_to_download)
print('Downloading {} images'.format(image_count_to_download))
pending_locations = set()
pending_download_queue = queue.Queue()
completed_download_queue = queue.Queue()
for image in images_to_download:
  location = find_unused_file(
      image.filename, args.output_dir, image_locations, pending_locations)
  pending_locations.add(location.relative_path)
  pending_download_queue.put(Download(image=image, location=location))

pending_download_count = image_count_to_download
for _ in range(args.download_threads):
  DownloadThread(pending_download_queue, completed_download_queue).start()

while pending_download_count > 0:
  completed_downloads_count = image_count_to_download - pending_download_count
  if completed_downloads_count % 100 == 0:
    if completed_downloads_count > 0:
      print()
    print('[{}/{}] '.format(
        completed_downloads_count, image_count_to_download), end='')
  download = completed_download_queue.get()
  image_locations[download.image.media_id] = download.location.relative_path
  print('.', end='', flush=True)
  if pending_download_count % 20 == 0:
    write_image_locations(image_locations, locations_file)
  pending_download_count = pending_download_count - 1

if image_count_to_download > 0:
  print()
print('Done')
write_image_locations(image_locations, locations_file)
