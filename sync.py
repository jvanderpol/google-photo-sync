#!/usr/bin/env python3

import datetime
import time
from typing import Optional, Iterator
import os.path
import json
import dataclasses
import threading
import string
import random
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, urlencode
from queue import Queue
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver

TOKEN_FILE = 'token.json'

CLIENT_ID = 'TODO'
CLIENT_SECRET = 'TODO'

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
class MediaItem:
  media_id: str
  filename: str
  base_url: str

  def download_url(self) -> str:
    return self.base_url + '=d'

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

def get_auth_token() -> TokenData:
  key_queue = Queue()
  server = HTTPServer(('', 0), lambda *args: AuthCallbackHandler(key_queue, *args))
  server_thread = threading.Thread(target=server.serve_forever)
  server_thread.daemon = True
  server_thread.start()
  server_url = 'http://localhost:{}'.format(server.server_port)
  print('Listening at {}'.format(server_url))
  redirect_uri = server_url + AUTH_CALLBACK_PATH

  code_verifier = ''.join(random.choices(string.ascii_letters + string.digits + '-._~', k = 128))
  auth_params = {
      'client_id': CLIENT_ID,
      'redirect_uri': redirect_uri,
      'response_type': 'code',
      'scope': AUTH_SCOPE,
      'code_challenge': code_verifier,
      'code_challenge_method': 'plain'
  }
  auth_url = '{}?{}'.format(BASE_AUTH_URL, urlencode(auth_params))
  print('Waiting for auth, go to {}'.format(auth_url))
  key = key_queue.get()

  print('Shutting down server')
  server.shutdown()
  return make_auth_token_request(
    code=key,
    code_verifier=code_verifier,
    grant_type='authorization_code',
    redirect_uri=redirect_uri)

def refresh_auth_token(old_token: TokenData) -> TokenData:
  new_token = make_auth_token_request(
      refresh_token=old_token.refresh_token,
      grant_type='refresh_token')
  new_token.refresh_token = old_token.refresh_token
  return new_token

def make_auth_token_request(**kwargs) -> TokenData:
  params = {
    'client_id': CLIENT_ID,
    'client_secret': CLIENT_SECRET,
    **kwargs
  }
  request = Request(
      TOKEN_URL,
      data=urlencode(params).encode(),
      headers=TOKEN_HEADERS,
      method='POST')
  token_json = urlopen(request).read().decode()
  return decode_json_token(token_json)

def decode_json_token(token_json: str) -> TokenData:
  token_dict = json.loads(token_json)
  refresh_token = token_dict['refresh_token'] if 'refresh_token' in token_dict else None
  expire_time = None
  if 'expires_in' in token_dict:
    expire_time = time.time() + int(token_dict['expires_in'])
  else: 
    expire_time = token_dict['expire_time']
  return TokenData(
      access_token=token_dict['access_token'],
      expire_time=expire_time,
      refresh_token=refresh_token)

def write_token(token: TokenData) -> None:
  with open(TOKEN_FILE, 'w') as f:
    f.write(json.dumps(dataclasses.asdict(token)))

def read_token() -> Optional[TokenData]:
  if not os.path.isfile(TOKEN_FILE):
    return None
  with open(TOKEN_FILE) as f:
    print('reading token from {}'.format(TOKEN_FILE))
    return decode_json_token(f.read())

def list_images(token: TokenData) -> Iterator[MediaItem]:
  pages = 0
  params = {'pageSize': '1'}
  while True:
    token = maybe_refresh_token(token)
    response = api_request('/v1/mediaItems?{}'.format(urlencode(params)), token)
    for media_item in response['mediaItems']:
      yield MediaItem(
          media_id=media_item['id'],
          filename=media_item['filename'],
          base_url=media_item['baseUrl'])
    next_page_token = response.get('nextPageToken')
    pages = pages + 1
    if pages > 3:
      return
    if not next_page_token:
      return
    params['pageToken'] = next_page_token

def api_request(path: str, token: TokenData):
  url = BASE_PHOTOS_API_URL + path
  print('Making request to {}'.format(url))
  request = Request(
      url,
      headers={'Authorization': 'Bearer {}'.format(token.access_token)})
  return json.loads(urlopen(request).read().decode())

def maybe_refresh_token(token: TokenData) -> TokenData:
  remaining_time = token.expire_time - time.time() 
  if remaining_time < 0:
    print('Refreshing token')
    token = refresh_auth_token(token)
    write_token(token)
  return token

token = read_token()
if not token:
  print('Getting a new token')
  token = get_auth_token()
  write_token(token)

local_image_locations = {}
for image in list_images(token):
  print(image.filename)
  local_image_locations[image.filename] = image.media_id

print(json.dumps(local_image_locations))
