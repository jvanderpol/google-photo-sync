#!/usr/bin/env python3

from typing import Dict, Iterator, List, Optional, Set

import argparse
import dataclasses
import datetime
import http.server
import json
import logging
import math
import os
import queue
import random
import socketserver
import string
import sys
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger()

TOKEN_FILE = '.token.json'
LOCATIONS_FILE = '.file_locations.json'

MAX_IDS_PER_BATCH_GET = 50

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

def parse_media_item(media_item: Dict) -> Optional[MediaItem]:
  video = media_item['mediaMetadata'].get('video')
  if video and video['status'] != 'READY':
    return None
  return MediaItem(
      video,
      media_id=media_item['id'],
      filename=media_item['filename'],
      base_url=media_item['baseUrl'])

def normalized_path(path: str) -> str:
  return path.lower()

@dataclasses.dataclass
class Download:
  image: MediaItem
  location: FileLocation

@dataclasses.dataclass
class DownloadResult:
  download: Download
  success: bool

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
        try:
          urllib.request.urlretrieve(
              download.image.download_url(), download.location.absolute_path)
          self.completed_download_queue.put(
              DownloadResult(download=download, success=True))
        except:
          logging.error(
              'Error downloading %s',
              download.image.download_url(),
              exc_info=True)
          if os.path.isfile(download.location.absolute_path):
            os.remove(download.location.absolute_path)
          self.completed_download_queue.put(
              DownloadResult(download=download, success=False))
    except queue.Empty:
      #This is expected when downloading is done
      pass


class AuthCallbackHandler(http.server.BaseHTTPRequestHandler):

  def __init__(self, key_queue, *args):
    self.key_queue = key_queue
    super().__init__(*args)

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    parsed_url = urllib.parse.urlparse(self.path)
    if parsed_url.path == AUTH_CALLBACK_PATH:
      query_params = urllib.parse.parse_qs(parsed_url.query)
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

def read_client_config(path: str) -> ClientConfig:
  client_config = read_json_file(path)
  if not client_config:
    raise Exception('{} file required'.config(path))
  client_id = client_config.get('client_id')
  client_secret = client_config.get('client_secret')
  if not client_id or not client_secret:
    raise Exception('client_id and client_secret required within {}'.format(
      path))
  return ClientConfig(client_id=client_id, client_secret=client_secret)

def confirm(question: str) -> bool:
  answer = ''
  while answer not in ["y", "n"]:
    answer = input("{} [Y/N] ".format(question).lower())
  return answer == 'y'

def get_auth_token(client_config: ClientConfig) -> TokenData:
  key_queue = queue.Queue()
  server = http.server.HTTPServer(
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
  auth_url = '{}?{}'.format(BASE_AUTH_URL, urllib.parse.urlencode(auth_params))
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

def make_auth_token_request(client_config: ClientConfig, **kwargs) -> TokenData:
  params = {
    'client_id': client_config.client_id,
    'client_secret': client_config.client_secret,
    **kwargs
  }
  request = urllib.request.Request(
      TOKEN_URL,
      data=urllib.parse.urlencode(params).encode(),
      headers=TOKEN_HEADERS,
      method='POST')
  token_json = urllib.request.urlopen(request).read().decode()
  return decode_json_token(json.loads(token_json))

def read_token(token_file: str) -> Optional[TokenData]:
  token_dict = read_json_file(token_file)
  if not token_dict:
    return None
  return decode_json_token(token_dict)

def write_token(token: TokenData, token_file: str) -> None:
  with open(token_file, 'w') as f:
    f.write(json.dumps(dataclasses.asdict(token)))

class ImageSync(object):
  def __init__(
      self,
      client_config: ClientConfig,
      token_file: str,
      token: str,
      locations_file: str,
      image_locations: Dict[str, str],
      output_dir: str,
      download_threads: int):
    self.client_config = client_config
    self.token_file = token_file
    self.token = token
    self.locations_file = locations_file
    self.image_locations = image_locations
    self.output_dir = output_dir
    self.download_threads = download_threads

  def list_images(
      self, max_images: int
  ) -> Iterator[MediaItem]:
    params = {'pageSize': '100'}
    images_returned = 0
    print('Requesting image list ', end='')
    while True:
      response = self.api_request('/v1/mediaItems?{}'.format(
        urllib.parse.urlencode(params)))
      print('.', end='', flush=True)
      for media_item_data in response['mediaItems']:
        media_item = parse_media_item(media_item_data)
        if media_item:
          yield media_item
        images_returned = images_returned + 1
        if max_images and images_returned == max_images:
          print()
          return
      next_page_token = response.get('nextPageToken')
      if not next_page_token:
        print()
        return
      params['pageToken'] = next_page_token

  def api_request(self, path: str):
    self.maybe_refresh_token()
    url = BASE_PHOTOS_API_URL + path
    logging.debug('Making request to {}'.format(url))
    request = urllib.request.Request(
        url,
        headers={'Authorization': 'Bearer {}'.format(self.token.access_token)})
    return json.loads(urllib.request.urlopen(request).read().decode())

  def maybe_refresh_token(self) -> None:
    remaining_time = self.token.expire_time - time.time() 
    if remaining_time < 60:
      logging.debug('Refreshing token')
      new_token = make_auth_token_request(
          self.client_config,
          refresh_token=self.token.refresh_token,
          grant_type='refresh_token')
      new_token.refresh_token = self.token.refresh_token
      self.token = new_token
      write_token(self.token, self.token_file)

  def write_image_locations(self) -> None:
    with open(self.locations_file, 'w') as f:
      f.write(json.dumps(self.image_locations, indent=2))

  def find_unused_file(
      self, preferred_name: str, normalized_used_locations: Set[str]
  ) -> FileLocation:
    suffix = 0
    while True:
      relative = None
      if suffix:
        base_name, extension = os. path.splitext(preferred_name)
        relative = '{}-{}{}'.format(base_name, suffix, extension)
      else:
        relative = preferred_name
      absolute = os.path.join(self.output_dir, relative)
      if (not os.path.isfile(absolute) and
          normalized_path(relative) not in normalized_used_locations):
        return FileLocation(relative_path=relative, absolute_path=absolute)
      suffix = suffix + 1

  def sync(self, max_images_to_sync: int, max_downloads: int) -> bool:
    images_to_download = [
        i for i in self.list_images(max_images_to_sync)
        if i.media_id not in self.image_locations]
    if max_downloads != -1 and max_downloads < len(images_to_download):
      raise Exception(
          'Number of images to download ({}) exceeds the max_downloads ({}). '
          'Increase the limit or use --max_downloads=-1'.format(
            len(images_to_download), max_downloads))

    normalized_used_locations = set(
        [normalized_path(f) for f in self.image_locations.values()])
    downloads = []
    for image in images_to_download:
      location = self.find_unused_file(
          image.filename, normalized_used_locations)
      normalized_used_locations.add(normalized_path(location.relative_path))
      downloads.append(Download(image=image, location=location))
    success = self.download(downloads)
    print('Done')
    return success

  def download(self, downloads: List[Download]) -> None:
    image_count_to_download = len(downloads)
    print('Downloading {} images'.format(image_count_to_download))
    pending_download_queue = queue.Queue()
    completed_download_queue = queue.Queue()
    for download in downloads:
      pending_download_queue.put(download)
    pending_download_count = len(downloads)
    for _ in range(self.download_threads):
      DownloadThread(pending_download_queue, completed_download_queue).start()

    success = True
    while pending_download_count > 0:
      completed_downloads_count = (
          image_count_to_download - pending_download_count)
      if completed_downloads_count % 100 == 0:
        if completed_downloads_count > 0:
          print()
        print('[{}/{}] '.format(
            completed_downloads_count, image_count_to_download), end='')
      result = completed_download_queue.get()
      self.image_locations[result.download.image.media_id] = (
          result.download.location.relative_path)
      print('.' if result.success else 'E', end='', flush=True)
      success = success and result.success
      if pending_download_count % 20 == 0:
        self.write_image_locations()
      pending_download_count = pending_download_count - 1

    if image_count_to_download > 0:
      print()
    self.write_image_locations()
    return success

  def get_media_items(self, media_ids: List[str]) -> Dict[str, str]:
    media_items = {}
    print('Getting image lists ({}) '.format(
      math.ceil(len(media_ids) / MAX_IDS_PER_BATCH_GET)), end='')
    for i in range(0, len(media_ids), MAX_IDS_PER_BATCH_GET):
      batch_ids = media_ids[i:i + MAX_IDS_PER_BATCH_GET]
      params = urllib.parse.urlencode(
          [('mediaItemIds', media_id) for media_id in batch_ids])
      response = self.api_request('/v1/mediaItems:batchGet?{}'.format(params))
      print('.', end='', flush=True)
      for media_item_result in response['mediaItemResults']:
        error = media_item_result.get('status')
        if error:
          print('Error looking up media:{}'.format(str(error)))
        else:
          media_item = parse_media_item(media_item_result['mediaItem'])
          if media_item:
            media_items[media_item.media_id] = media_item
    return media_items

  def reconcile(self) -> bool:
    success = True
    _, _, all_files = next(os.walk(self.output_dir), (None, None, []))
    non_hidden_files = set([f for f in all_files if not f.startswith('.')])
    expected_files = set(self.image_locations.values())
    files_to_delete = [
        f for f in non_hidden_files
        if f not in expected_files]
    if files_to_delete:
      print('About to delete:\n  {}'.format('\n  '.join(files_to_delete)))
      if confirm("Delete {} files?".format(len(files_to_delete))):
        for file in files_to_delete:
          os.remove(os.path.join(self.output_dir, file))
    entries_to_download = [
        (k, v) for k, v in self.image_locations.items()
        if v not in non_hidden_files]
    if entries_to_download:
      relative_paths = [v for k, v in entries_to_download]
      print('About to download:\n  {}'.format('\n  '.join(relative_paths)))
      if confirm("Download {} files?".format(len(relative_paths))):
        media_items = self.get_media_items([k for k, v in entries_to_download])
        downloads = []
        for media_id, relative_path in entries_to_download:
          media_item = media_items.get(media_id)
          if media_item:
            location = FileLocation(
                relative_path=relative_path,
                absolute_path=os.path.join(self.output_dir, relative_path))
            downloads.append(Download(image=media_item, location=location))
        success = self.download(downloads)
    print('Done')
    return success

def main():
  parser = argparse.ArgumentParser(
      description='Syncs all google photos to a local directory.')
  parser.add_argument('--client_config', '-c', type=str, required=True)
  parser.add_argument('--output_dir', '-o', type=str, required=True)
  parser.add_argument('--max_downloads', type=int, default=500)
  parser.add_argument('--max_images_to_sync', type=int, default=None)
  parser.add_argument('--download_threads', type=int, default=10)
  parser.add_argument('--reconcile', action='store_true')
  parser.add_argument('--debug', action='store_true')
  args = parser.parse_args()

  if args.debug:
    logger.setLevel(logging.DEBUG)

  client_config = read_client_config(args.client_config)
  token_file = os.path.join(args.output_dir, TOKEN_FILE)
  token = read_token(token_file)
  if not token:
    logging.debug('Getting a new token')
    token = get_auth_token(client_config)
    write_token(token, token_file)

  locations_file = os.path.join(args.output_dir, LOCATIONS_FILE)
  image_locations = read_json_file(locations_file) or {}
  image_sync = ImageSync(
      client_config, token_file, token, locations_file, image_locations,
      args.output_dir, args.download_threads)

  success = True
  if args.reconcile:
    success = image_sync.reconcile()
  else:
    success = image_sync.sync(args.max_images_to_sync, args.max_downloads)
  if not success:
    sys.exit(1)

if __name__ == "__main__":
  main()
