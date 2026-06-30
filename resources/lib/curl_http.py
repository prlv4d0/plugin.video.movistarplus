# encoding: utf-8
#
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import unicode_literals, absolute_import, division

import json
import shutil
import subprocess

try:
  from urllib.parse import urlencode
except ImportError:
  from urllib import urlencode


class CurlResponse(object):
  def __init__(self, status_code, content):
    self.status_code = status_code
    self.content = content
    try:
      self.text = content.decode('utf-8')
    except Exception:
      self.text = ''

  def json(self):
    return json.loads(self.text)


def available():
  return bool(shutil.which('curl'))


def request(method, url, headers=None, data=None, json_data=None, timeout=30):
  curl = shutil.which('curl')
  if not curl:
    raise RuntimeError('curl no está disponible en el sistema.')

  method = method.upper()
  payload = b''
  cmd = [
    curl,
    '--silent',
    '--show-error',
    '--location',
    '--compressed',
    '--max-time', str(timeout),
    '--request', method,
    '--write-out', '\n%{http_code}',
  ]

  for key, value in (headers or {}).items():
    cmd.extend(['--header', '{}: {}'.format(key, value)])

  if json_data is not None:
    payload = json.dumps(json_data).encode('utf-8')
    cmd.extend(['--data-binary', '@-'])
  elif data is not None:
    if isinstance(data, dict):
      payload = urlencode(data).encode('utf-8')
    elif isinstance(data, bytes):
      payload = data
    else:
      payload = str(data).encode('utf-8')
    cmd.extend(['--data-binary', '@-'])

  cmd.append(url)
  proc = subprocess.Popen(
    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  stdout, stderr = proc.communicate(payload)
  if proc.returncode != 0:
    err = stderr.decode('utf-8', 'replace')
    raise RuntimeError('curl falló: {}'.format(err.strip()))

  body, sep, code = stdout.rpartition(b'\n')
  if not sep:
    raise RuntimeError('curl no devolvió código HTTP.')
  return CurlResponse(int(code.decode('ascii')), body)
