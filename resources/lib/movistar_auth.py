# encoding: utf-8
#
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import unicode_literals, absolute_import, division

import base64
import hashlib
import hmac
import json
import random
import re
import time

try:
  from urllib.parse import parse_qsl, quote_plus, unquote, urlparse
except ImportError:
  from urlparse import parse_qsl, urlparse
  from urllib import quote_plus, unquote

try:
  from Crypto.Cipher import AES
  from Crypto.Util.Padding import unpad
except ImportError:
  try:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import unpad
  except ImportError:
    AES = None
    unpad = None

CONF_URL = 'https://ver.movistarplus.es/feed/conf.php?id=all'
CONF_PATTERN = r'yomvi\.setupConf\((\{.*?\})\);'
ENCODING = 'utf-8'
SALT_PREFIX = b'Salted__'
WEB_CLIENT_ID = '491aafabb70d4644954d8fb21e0b93b1'
WEB_CLIENT_SECRET = 'secret'
WEB_USER_AGENT = (
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
)


class MovistarAuthError(Exception):
  pass


def _evp_key(password, salt):
  pw = password.encode(ENCODING)
  kiv = b''
  prev = b''
  while len(kiv) < 48:
    prev = hashlib.md5(prev + pw + salt).digest()
    kiv += prev
  return kiv[:32], kiv[32:48]


def _decode_seg(seg):
  return bytes.fromhex(''.join(seg[::-1]))


def _decrypt_chunk(data, password):
  if AES is None or unpad is None:
    raise MovistarAuthError(
      'Falta script.module.pycryptodome: no se puede descifrar conf.php')

  raw = base64.b64decode(data.decode(ENCODING))
  if raw.startswith(SALT_PREFIX):
    salt = raw[8:16]
    ct = raw[16:]
  else:
    salt = b''
    ct = raw
  key, iv = _evp_key(password, salt)
  cipher = AES.new(key, AES.MODE_CBC, iv)
  return unpad(cipher.decrypt(ct), AES.block_size).decode(ENCODING)


def decrypt_ua(segments, password):
  return ''.join(_decrypt_chunk(_decode_seg(seg), password) for seg in segments)


def fetch_webplayer_credentials(session, cache=None):
  """Return the current webplayer consumer key and secret."""
  cached = None
  if cache:
    content = cache.load('webplayer_credentials.json', 24 * 60)
    if content:
      try:
        cached = json.loads(content)
      except Exception:
        cached = None
  if cached and cached.get('consumer_key') and cached.get('consumer_secret'):
    return cached['consumer_key'], cached['consumer_secret']

  response = session.get(CONF_URL, timeout=15, allow_redirects=True)
  response.raise_for_status()

  match = re.search(CONF_PATTERN, response.text, re.DOTALL)
  if not match:
    raise MovistarAuthError('No se pudo extraer la configuración webplayer.')

  cfg = json.loads(match.group(1))['config']
  password = base64.b64encode(
    '{}{}{}{}'.format(cfg['version'], cfg['name'],
                      cfg['timestamp'], cfg['language']).encode()
  ).decode()

  ua = cfg['ua']
  consumer_key = decrypt_ua(json.loads(ua['query']), password)
  consumer_secret = decrypt_ua(json.loads(ua['param']), password)

  if cache:
    data = {
      'consumer_key': consumer_key,
      'consumer_secret': consumer_secret,
      'timestamp': int(time.time()),
    }
    cache.save_file('webplayer_credentials.json', json.dumps(data))

  return consumer_key, consumer_secret


def generate_basic_token(device_id):
  return base64.b64encode(
    '{}:webplayer'.format(device_id).encode(ENCODING)).decode(ENCODING)


def _nonce():
  rnd = ''.join(random.choice('abcdefghijklmnopqrstuvwxyz0123456789')
                for _ in range(11))
  return base64.b64encode(
    '{}{}'.format(rnd, int(time.time() * 1000)).encode(ENCODING)
  ).decode(ENCODING)


def _hmac_sha1(key, msg):
  return base64.b64encode(
    hmac.new(key, msg.encode(ENCODING), hashlib.sha1).digest()
  ).decode(ENCODING)


def get_signature(method, url, token, scheme, consumer_key, consumer_secret,
                  params=None, nonce=None, timestamp=None):
  """Generate the base64 OPPlus signature payload."""
  nonce = nonce or _nonce()
  timestamp = timestamp or str(int(time.time()))

  parsed = urlparse(url)
  base_url = parsed.netloc + parsed.path
  merged = {}
  if params:
    merged.update(params)
  merged.update(dict(parse_qsl(parsed.query)))

  merged[scheme] = token
  merged['consumer_key'] = consumer_key
  merged['nonce'] = nonce
  merged['signature_method'] = 'HMAC-SHA1'
  merged['timestamp'] = timestamp
  merged['version'] = '1.0'

  for key in list(merged):
    raw = str(merged[key])
    dec = unquote(raw)
    merged[key] = quote_plus(dec) if '@' in dec else raw

  sorted_enc = [
    '{}%3D{}'.format(quote_plus(k), quote_plus(merged[k]))
    for k in sorted(merged)
  ]
  data_to_sign = '{}&{}&{}'.format(
    method.upper(), quote_plus(base_url), '%26'.join(sorted_enc))
  signature = _hmac_sha1(consumer_secret.encode(ENCODING), data_to_sign)
  payload = (
    'consumer_key="{consumer_key}",nonce="{nonce}",'
    'signature_method="HMAC-SHA1",signature="{signature}",'
    'timestamp="{timestamp}",version="1.0"'
  ).format(consumer_key=consumer_key, nonce=nonce, signature=signature,
           timestamp=timestamp)
  return base64.b64encode(payload.encode(ENCODING)).decode(ENCODING)


def signed_headers(method, url, token, scheme, consumer_key, consumer_secret,
                   base_headers=None, extra=None, params=None):
  headers = dict(base_headers or {})
  signature = get_signature(method, url, token, scheme, consumer_key,
                            consumer_secret, params=params)
  headers['Authorization'] = 'OPPlus {}={},Signature={}'.format(
    scheme, token, signature)
  if extra:
    headers.update(extra)
  return headers


def default_headers(content_type='application/json'):
  headers = {
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8,zh-CN;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Origin': 'https://ver.movistarplus.es',
    'Referer': 'https://ver.movistarplus.es/login?r=%2F',
    'sec-ch-ua': '"Not)A;Brand";v="8", "Google Chrome";v="140", "Chromium";v="140"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'cross-site',
    'User-Agent': WEB_USER_AGENT,
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
  }
  if content_type:
    headers['Content-Type'] = content_type
  return headers


def decode_jwt(token):
  if not token:
    return {}
  try:
    payload = token.split('.')[1]
    padding = len(payload) % 4
    if padding:
      payload += '=' * (4 - padding)
    return json.loads(base64.b64decode(payload).decode(ENCODING))
  except Exception:
    return {}


def token_expire_date(token):
  return decode_jwt(token).get('exp', 0)


def token_expired(token, leeway=60):
  exp = token_expire_date(token)
  if not exp:
    return True
  return exp <= int(time.time()) + leeway
