# encoding: utf-8
#
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import unicode_literals, absolute_import, division

import sys
import json
import requests
import io
import os
import time
import re
import shutil
import glob
import uuid
import traceback

from datetime import datetime, timedelta

from .endpoints import endpoints
from .log import LOG, INFO, ERROR, print_json
from .network import Network
from .cache import Cache
from .timeconv import *
from .useragent import useragent
from . import curl_http
from .movistar_auth import (
    WEB_CLIENT_ID,
    WEB_CLIENT_SECRET,
    MovistarAuthError,
    default_headers,
    decode_jwt,
    fetch_webplayer_credentials,
    generate_basic_token,
    signed_headers,
    token_expired,
    token_expire_date,
)

class Movistar(object):
    account = {'username': '', 'password': '',
               'device_id': '',
               'id': None,
               'pid': None,
               'encoded_user': '',
               'profile_id': '0',
               'platform': '',
               'access_token': '',
               'session_token': '',
               'ssp_token': '',
               'demarcation': 0}

    add_extra_info = True
    dplayer = 'webplayer'
    device_code = 'WP_DASH'
    manufacturer = 'Chrome'
    account_dir = 'account_1'

    def __init__(self, config_directory, reuse_devices=False):
      self.account = dict(Movistar.account)
      self.logged = False
      self.expired_access_token = False
      self.uses_new_api = True
      self.entitlements = self.empty_entitlements()

      self.net = Network()
      self.net.headers = default_headers(None)
      self.net.headers['Accept'] = 'application/json, text/javascript, */*; q=0.01'

      self.quality = 'HD' # or UHD

      content = Movistar.load_file_if_exists(config_directory + 'account.txt')
      if content: self.account_dir = content
      LOG('account_dir: {}'.format(self.account_dir))

      account_dir = os.path.join(config_directory, self.account_dir)
      if not os.path.exists(account_dir):
        os.makedirs(account_dir)
        if self.account_dir == 'account_1':
          for ext in ['*.conf', '*.json', '*.key']:
            for f in glob.glob(os.path.join(config_directory, ext)):
              shutil.move(f, account_dir)
          if os.path.exists(config_directory + 'cache'):
            shutil.move(config_directory + 'cache', account_dir)

      self.config_dir = config_directory
      config_directory = account_dir + '/'

      self.cache = Cache(config_directory)
      if not os.path.exists(config_directory + 'cache'):
        os.makedirs(config_directory + 'cache')

      self.endpoints = endpoints

      content = self.cache.load_file('profile_id.conf')
      if content:
        self.account['profile_id'] = content

      tokens = self.load_tokens()
      if tokens:
        if token_expired(tokens.get('accessToken_init')):
          self.expired_access_token = True
          self.renew_init_tokens(tokens)
          tokens = self.load_tokens()
        self.apply_tokens(tokens)

      data = self.cache.load_file('searchs.json')
      self.search_list = json.loads(data) if data else []

    def empty_entitlements(self):
      return {
        'activePurchases': [],
        'partners': [],
        'activePackages': [],
        'vodSubscription': [],
        'linearSubscription': [],
        'suscripcion': '',
        'tvRights': [],
        'distilledTvRights': [],
      }

    def load_tokens(self):
      content = self.cache.load_file('tokens.json')
      if not content:
        return {}
      try:
        data = json.loads(content)
      except Exception:
        return {}
      if 'accessToken' in data and 'accessToken_init' not in data:
        LOG('Ignoring legacy tokens.json')
        return {}
      return data

    def save_tokens(self, data):
      self.cache.save_file('tokens.json', json.dumps(data, ensure_ascii=False))

    def cleanup_legacy_auth_files(self):
      for filename in ['auth.key', 'access_token.conf', 'cdn.conf']:
        self.cache.remove_file(filename)

    def apply_tokens(self, data):
      account_info = data.get('account_info') or {}
      init_data = data.get('init_data') or {}
      ofertas = account_info.get('ofertas') or []
      oferta = ofertas[0] if ofertas else {}

      self.account['username'] = data.get('username', '')
      self.account['id'] = data.get('account_nbr') or oferta.get('accountNumber')
      self.account['encoded_user'] = account_info.get('cod_usuario_cifrado', '')
      self.account['profile_id'] = str(data.get('profile_id', self.account.get('profile_id') or '0'))
      self.account['platform'] = oferta.get('@id_perfil') or data.get('client_segment') or 'OTT'
      self.account['device_id'] = data.get('device_id_actual') or ''
      self.account['access_token'] = data.get('accessToken_init') or ''
      self.account['session_token'] = init_data.get('token', '')
      self.account['ssp_token'] = data.get('sspToken_init') or init_data.get('sspToken', '')
      self.account['demarcation'] = init_data.get('demarcation', 0)
      self.account['pid'] = init_data.get('pid')

      self.entitlements = self.empty_entitlements()
      for key in ['activePurchases', 'partners', 'activePackages',
                  'tvRights', 'distilledTvRights']:
        value = init_data.get(key, [])
        self.entitlements[key] = value if isinstance(value, list) else []
      linear = init_data.get('linearSubscription') or ''
      vod = init_data.get('vodSubscription') or ''
      self.entitlements['linearSubscription'] = linear.split(',') if isinstance(linear, str) and linear else []
      self.entitlements['vodSubscription'] = vod.split(',') if isinstance(vod, str) and vod else []
      self.entitlements['suscripcion'] = init_data.get('suscripcion', '')

      self.logged = bool(self.account['id'] and self.account['device_id'] and self.account['access_token'])
      self.expired_access_token = token_expired(self.account['access_token']) if self.account['access_token'] else False
      if self.account['device_id']:
        self.cache.save_file('device_id.conf', self.account['device_id'])
      if account_info:
        self.cache.save_file('account.json', json.dumps(account_info, ensure_ascii=False))

    def get_consumer_credentials(self):
      return fetch_webplayer_credentials(self.net.session, self.cache)

    def new_api_headers(self, content_type='application/json'):
      headers = default_headers(content_type)
      headers['Accept'] = 'application/json, text/javascript, */*; q=0.01'
      return headers

    def signed_api_headers(self, method, url, token, scheme, content_type='application/json',
                           extra=None, params=None):
      consumer_key, consumer_secret = self.get_consumer_credentials()
      headers = self.new_api_headers(content_type)
      return signed_headers(method, url, token, scheme, consumer_key,
                            consumer_secret, headers, extra, params)

    def is_f5_forbidden(self, response):
      if response.status_code != 403:
        return False
      content = response.content[:500].decode('utf-8', 'replace')
      return 'F5 site:' in content or 'The requested URL was rejected' in content

    def api_request(self, method, url, headers=None, data=None, json_data=None,
                    allow_redirects=True, force_curl=False):
      method = method.upper()
      if force_curl:
        return curl_http.request(
          method, url, headers=headers, data=data, json_data=json_data)

      try:
        if method == 'GET':
          response = self.net.session.get(url, headers=headers, allow_redirects=allow_redirects)
        elif method == 'POST':
          if json_data is not None:
            response = self.net.session.post(url, json=json_data, headers=headers)
          else:
            response = self.net.session.post(url, data=data, headers=headers)
        elif method == 'PUT':
          response = self.net.session.put(url, headers=headers)
        elif method == 'DELETE':
          response = self.net.session.delete(url, headers=headers)
        else:
          raise ValueError('Método HTTP no soportado: {}'.format(method))
      except Exception as exc:
        INFO('new api request failed with requests; retrying with curl: {}'.format(exc))
        return curl_http.request(
          method, url, headers=headers, data=data, json_data=json_data)

      if self.is_f5_forbidden(response):
        INFO('new api request blocked by F5 with requests; retrying with curl')
        try:
          return curl_http.request(
            method, url, headers=headers, data=data, json_data=json_data)
        except Exception as exc:
          ERROR('curl fallback failed: {}'.format(exc))
      return response

    def response_json(self, response):
      content = response.content.decode('utf-8')
      try:
        return json.loads(content)
      except Exception:
        return {'error': content, 'status_code': response.status_code}

    def safe_error_message(self, result):
      if isinstance(result, dict):
        for key in ['error_description', 'message', 'error', 'resultText']:
          value = result.get(key)
          if value:
            return str(value)
        return json.dumps({k: result[k] for k in result if k not in ['access_token', 'accessToken', 'sspToken', 'legacyAccessToken']})
      return str(result)

    def init_payload(self, account_nbr, device_id):
      return {
        'accountNumber': str(account_nbr),
        'sessionUserProfile': int(self.account.get('profile_id') or 0),
        'isKidProfile': False,
        'deviceId': str(device_id),
        'streamMiscellanea': 'HTTPS',
        'deviceManufacturerProduct': self.manufacturer,
        'streamDRM': 'Widevine',
        'streamFormat': 'DASH',
      }

    def request_login_token(self, username, password, device_id):
      INFO('new api login step: token request')
      url = self.endpoints['token']
      data = {
        'grant_type': 'password',
        'username': username,
        'password': password,
        'scope': 'api',
        'client_id': WEB_CLIENT_ID,
        'client_secret': WEB_CLIENT_SECRET,
      }
      basic_token = generate_basic_token(device_id)
      headers = self.signed_api_headers(
        'POST', url, basic_token, 'Basic',
        content_type='application/x-www-form-urlencoded',
        extra={'Host': 'soter-pf.sve.video.telefonicaservices.com'},
        params=data)
      response = self.api_request('POST', url, data=data, headers=headers)
      result = self.response_json(response)
      INFO('new api login step: token response {}'.format(response.status_code))
      if response.status_code != 200:
        raise MovistarAuthError('token {}: {}'.format(
          response.status_code, self.safe_error_message(result)))
      token = result.get('access_token')
      if not token:
        raise MovistarAuthError('Login sin access_token.')
      return token

    def request_account_info(self, access_token_login):
      INFO('new api login step: accountInfo request')
      url = self.endpoints['account_info']
      headers = self.signed_api_headers(
        'GET', url, access_token_login, 'Bearer',
        extra={'Host': 'soter-pf.sve.video.telefonicaservices.com'})
      response = self.api_request('GET', url, headers=headers, allow_redirects=True)
      result = self.response_json(response)
      INFO('new api login step: accountInfo response {}'.format(response.status_code))
      if response.status_code != 200:
        raise MovistarAuthError('accountInfo {}: {}'.format(
          response.status_code, self.safe_error_message(result)))
      return result

    def put_device_id(self, device_id, access_token_login, account_nbr):
      INFO('new api login step: device register request')
      url = self.endpoints['register_device'].format(
        DEVICEID=device_id, ACCOUNTNUMBER=account_nbr)
      headers = self.signed_api_headers(
        'PUT', url, access_token_login, 'Bearer',
        extra={'Host': 'soter-pf.sve.video.telefonicaservices.com'})
      response = self.api_request('PUT', url, headers=headers)
      INFO('new api login step: device register response {}'.format(response.status_code))
      if response.status_code in (200, 201, 409):
        return True
      ERROR('put_device_id failed: {} {}'.format(response.status_code, response.content[:500]))
      return False

    def request_init_data(self, access_token, account_nbr, device_id):
      INFO('new api login step: initData request')
      url = self.endpoints['initdata']
      headers = self.signed_api_headers(
        'POST', url, access_token, 'Bearer',
        extra={'Host': 'soter-pf.sve.video.telefonicaservices.com'})
      response = self.api_request(
        'POST', url, json_data=self.init_payload(account_nbr, device_id), headers=headers)
      result = self.response_json(response)
      INFO('new api login step: initData response {}'.format(response.status_code))
      if response.status_code != 200:
        raise MovistarAuthError('initData {}: {}'.format(
          response.status_code, self.safe_error_message(result)))
      return result

    def request_tcdn_token(self, access_token_init, account_nbr):
      INFO('new api login step: tcdn request')
      url = self.endpoints['renovacion_cdntoken2'].format(ACCOUNTNUMBER=account_nbr)
      headers = self.signed_api_headers(
        'POST', url, access_token_init, 'Bearer',
        extra={'Host': 'soter-pf.sve.video.telefonicaservices.com'})
      response = self.api_request('POST', url, headers=headers, force_curl=True)
      if response.status_code >= 500:
        time.sleep(1)
        response = self.api_request('POST', url, headers=headers, force_curl=True)
      result = self.response_json(response)
      INFO('new api login step: tcdn response {}'.format(response.status_code))
      if response.status_code != 200:
        raise MovistarAuthError('tcdn {}: {}'.format(
          response.status_code, self.safe_error_message(result)))
      return result.get('access_token', '')

    def build_token_state(self, username, password, device_id, access_token_login,
                          account_info, init_data, tcdn_token):
      oferta = (account_info.get('ofertas') or [{}])[0]
      account_nbr = oferta.get('accountNumber')
      access_token_init = init_data.get('accessToken')
      payload = decode_jwt(access_token_init)
      if not account_nbr:
        account_nbr = payload.get('id') or payload.get('accountNumber')
      client_segment = payload.get('s') or payload.get('clientSegment')
      return {
        'username': username,
        'account_nbr': account_nbr,
        'device_id_actual': device_id,
        'client_segment': client_segment,
        'access_token_login': access_token_login,
        'accessToken_init': access_token_init,
        'legacyAccessToken_init': init_data.get('legacyAccessToken'),
        'sspToken_init': init_data.get('sspToken'),
        'access_token_tcdn': tcdn_token,
        'profile_id': self.account.get('profile_id') or '0',
        'account_info': account_info,
        'init_data': init_data,
        'accessToken_init_lista': [{
          'device_id': device_id,
          'accessToken_init': access_token_init,
          'fecha_obtencion': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
          'expira': self.token_expire_string(access_token_init),
          'ultimo_uso': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
          'ssp': bool(init_data.get('sspToken')),
          'device_type': 'Web',
          'device_type_code': 'WP_DASH',
        }],
      }

    def token_expire_string(self, token):
      exp = token_expire_date(token)
      if not exp:
        return 'Desconocida'
      return datetime.fromtimestamp(exp).strftime('%d/%m/%Y %H:%M:%S')

    def login(self, username, password):
      INFO('new api login started')
      device_id = self.account.get('device_id') or self.cache.load_file('device_id.conf')
      if device_id:
        device_id = device_id.strip('"')
      else:
        device_id = uuid.uuid4().hex

      try:
        access_token_login = self.request_login_token(username, password, device_id)
        account_info = self.request_account_info(access_token_login)
        account_nbr = (account_info.get('ofertas') or [{}])[0].get('accountNumber')
        if not account_nbr:
          raise MovistarAuthError('No se pudo obtener el número de cuenta.')
        if not self.put_device_id(device_id, access_token_login, account_nbr):
          raise MovistarAuthError('No se pudo registrar el dispositivo.')
        init_data = self.request_init_data(access_token_login, account_nbr, device_id)
        access_token_init = init_data.get('accessToken')
        if not access_token_init:
          raise MovistarAuthError('initData no devolvió accessToken.')
        try:
          tcdn_token = self.request_tcdn_token(access_token_init, account_nbr)
        except Exception as exc:
          ERROR('new api login tcdn token failed, continuing without CDN token: {}'.format(exc))
          tcdn_token = ''
        data = self.build_token_state(
          username, password, device_id, access_token_login, account_info,
          init_data, tcdn_token)
        self.cleanup_legacy_auth_files()
        self.save_tokens(data)
        self.apply_tokens(data)
        INFO('new api login finished')
        return True, json.dumps({'ok': True})
      except Exception as exc:
        ERROR('new api login failed: {}'.format(exc))
        ERROR(traceback.format_exc())
        return False, str(exc)

    def get_account_info(self):
      tokens = self.load_tokens()
      return tokens.get('account_info') or {}

    def change_device(self, id):
      tokens = self.load_tokens()
      for item in tokens.get('accessToken_init_lista', []):
        if item.get('device_id') == id and item.get('accessToken_init'):
          tokens['device_id_actual'] = id
          tokens['accessToken_init'] = item['accessToken_init']
          try:
            init_data = self.request_init_data(item['accessToken_init'], tokens.get('account_nbr'), id)
            tokens['init_data'] = init_data
            tokens['accessToken_init'] = init_data.get('accessToken') or item['accessToken_init']
            tokens['legacyAccessToken_init'] = init_data.get('legacyAccessToken')
            tokens['sspToken_init'] = init_data.get('sspToken')
          except Exception as exc:
            LOG('change_device init refresh failed: {}'.format(exc))
          self.save_tokens(tokens)
          self.apply_tokens(tokens)
          return
      self.account['device_id'] = id
      self.cache.save_file('device_id.conf', id)

    def normalize_devices(self, data):
      if isinstance(data, dict):
        for key in ['devices', 'deviceList', 'items', 'data', 'result']:
          if isinstance(data.get(key), list):
            data = data[key]
            break
      if not isinstance(data, list):
        return []
      res = []
      for d in data:
        device_id = d.get('Id') or d.get('deviceId') or d.get('id')
        if not device_id or device_id == '-':
          continue
        reg_date = d.get('RegistrationDate') or ''
        try:
          reg_date = isodate2str(reg_date)
        except Exception:
          pass
        res.append({
          'id': device_id,
          'name': d.get('Name') or d.get('name') or 'Dispositivo',
          'type': d.get('DeviceType') or d.get('deviceType') or '',
          'type_code': d.get('DeviceTypeCode') or d.get('deviceTypeCode') or '',
          'playing': d.get('ContentPlaying') or d.get('contentPlaying') or '',
          'reg_date': reg_date,
          'in_ssp': d.get('IsInSsp', d.get('isInSsp', False)),
        })
      return res

    def get_devices(self):
      tokens = self.load_tokens()
      access_token = tokens.get('accessToken_init') or self.account.get('access_token')
      account_nbr = tokens.get('account_nbr') or self.account.get('id')
      device_id = tokens.get('device_id_actual') or self.account.get('device_id')
      if not access_token or not account_nbr:
        return []
      url = self.endpoints['devices'].format(ACCOUNTNUMBER=account_nbr)
      try:
        headers = self.signed_api_headers(
          'GET', url, access_token, 'Bearer',
          extra={
            'Host': 'soterpe-pf.sve.video.telefonicaservices.com',
            'X-Movistarplus-Deviceid': device_id,
          })
        response = self.api_request('GET', url, headers=headers, allow_redirects=True)
        devices = self.normalize_devices(self.response_json(response))
        self.update_token_device_types(devices)
        return devices
      except Exception as exc:
        LOG('get_devices failed: {}'.format(exc))
        return []

    def update_token_device_types(self, devices):
      tokens = self.load_tokens()
      token_list = tokens.get('accessToken_init_lista', [])
      if not token_list:
        return
      changed = False
      by_id = {d.get('id'): d for d in devices}
      for item in token_list:
        device = by_id.get(item.get('device_id'))
        if not device:
          continue
        if item.get('device_type') != device.get('type'):
          item['device_type'] = device.get('type')
          changed = True
        if item.get('device_type_code') != device.get('type_code'):
          item['device_type_code'] = device.get('type_code')
          changed = True
        if item.get('ssp') != device.get('in_ssp'):
          item['ssp'] = device.get('in_ssp')
          changed = True
      if changed:
        self.save_tokens(tokens)

    def register_device(self):
      tokens = self.load_tokens()
      token = tokens.get('access_token_login')
      account_nbr = tokens.get('account_nbr')
      device_id = tokens.get('device_id_actual') or self.account.get('device_id')
      if not token or token_expired(token) or not account_nbr or not device_id:
        return ''
      return 'OK' if self.put_device_id(device_id, token, account_nbr) else ''

    def unregister_device(self):
      return self.delete_device(self.account['device_id'])

    def request_device_id(self):
      return uuid.uuid4().hex

    def is_protected_device(self, device):
      device_type = device.get('type') or device.get('device_type') or ''
      type_code = device.get('type_code') or device.get('device_type_code') or ''
      return type_code in ['STB', 'STB_IPTV', 'IPTV'] or device_type in ['STB', 'STB_IPTV', 'IPTV']

    def delete_device(self, device_id):
      tokens = self.load_tokens()
      for device in self.get_devices():
        if device.get('id') == device_id and self.is_protected_device(device):
          return 'ERROR: no se puede eliminar un decodificador oficial.'
      access_token = tokens.get('accessToken_init') or self.account.get('access_token')
      account_nbr = tokens.get('account_nbr') or self.account.get('id')
      if not access_token or not account_nbr:
        return ''
      url = self.endpoints['delete_device'].format(ACCOUNTNUMBER=account_nbr, DEVICEID=device_id)
      try:
        headers = self.signed_api_headers(
          'DELETE', url, access_token, 'Bearer',
          extra={'Host': 'soterpe-pf.sve.video.telefonicaservices.com'})
        response = self.api_request('DELETE', url, headers=headers)
        if response.status_code in (200, 204):
          token_list = [
            item for item in tokens.get('accessToken_init_lista', [])
            if item.get('device_id') != device_id
          ]
          tokens['accessToken_init_lista'] = token_list
          if tokens.get('device_id_actual') == device_id and token_list:
            tokens['device_id_actual'] = token_list[0].get('device_id')
            tokens['accessToken_init'] = token_list[0].get('accessToken_init')
          self.save_tokens(tokens)
        return response.content.decode('utf-8')
      except Exception as exc:
        LOG('delete_device failed: {}'.format(exc))
        return str(exc)

    def rename_device(self, device_id, name):
      return ''

    def clear_session(self):
      return ''

    def open_session(self, data, session_token=None, session_id=None):
      return {'resultCode': 0, 'resultData': {'cToken': self.get_ssp_token()}}

    def delete_session_id(self, session_token, id = '0'):
      headers = self.net.headers.copy()
      headers['Content-Type'] = 'text/plain;charset=UTF-8'
      data = '{"X-HZId":"' + session_token +'","X-Content-Type":"application/json","X-Operation":"DELETE"}'
      url = self.endpoints['tearDownStream'].format(PID=self.account['pid'], deviceCode=self.device_code, PLAYREADYID=self.account['device_id'], SessionID=id)
      response = self.net.session.post(url, headers=headers, data=data)
      content = response.content.decode('utf-8')
      return content

    """
    def delete_last_session(self):
      token = self.cache.load_file('session_token.conf')
      if token:
        self.delete_session0(token)
        os.remove(self.cache.config_directory + 'session_token.conf')
    """

    def delete_session(self, device_id = None):
      return ''

    def get_cdntoken(self):
      tokens = self.load_tokens()
      tcdn_token = tokens.get('access_token_tcdn')
      if tcdn_token and not token_expired(tcdn_token):
        return tcdn_token

      if token_expired(tokens.get('accessToken_init')):
        self.renew_init_tokens(tokens)
        tokens = self.load_tokens()

      access_token = tokens.get('accessToken_init') or self.account.get('access_token')
      account_nbr = tokens.get('account_nbr') or self.account.get('id')
      if not access_token or not account_nbr:
        return ''
      try:
        tcdn_token = self.request_tcdn_token(access_token, account_nbr)
        if tcdn_token:
          tokens['access_token_tcdn'] = tcdn_token
          self.save_tokens(tokens)
        return tcdn_token
      except Exception as exc:
        LOG('get_cdntoken failed: {}'.format(exc))
        return ''

    def usable_token_items(self, tokens):
      items = []
      current = tokens.get('device_id_actual')
      if tokens.get('accessToken_init') and current:
        items.append({
          'device_id': current,
          'accessToken_init': tokens.get('accessToken_init'),
        })
      for item in tokens.get('accessToken_init_lista', []):
        if self.is_protected_device(item):
          continue
        if not item.get('device_id') or not item.get('accessToken_init'):
          continue
        if item.get('device_id') == current:
          continue
        items.append(item)
      return items

    def store_renewed_init_data(self, tokens, device_id, init_data):
      access_token = init_data.get('accessToken')
      if not access_token:
        return False
      tokens['device_id_actual'] = device_id
      tokens['accessToken_init'] = access_token
      tokens['legacyAccessToken_init'] = init_data.get('legacyAccessToken')
      tokens['sspToken_init'] = init_data.get('sspToken')
      tokens['init_data'] = init_data
      token_list = tokens.setdefault('accessToken_init_lista', [])
      found = False
      for item in token_list:
        if item.get('device_id') == device_id:
          item['accessToken_init'] = access_token
          item['expira'] = self.token_expire_string(access_token)
          item['ultimo_uso'] = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
          item['ssp'] = bool(init_data.get('sspToken'))
          found = True
          break
      if not found:
        token_list.append({
          'device_id': device_id,
          'accessToken_init': access_token,
          'fecha_obtencion': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
          'expira': self.token_expire_string(access_token),
          'ultimo_uso': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
          'ssp': bool(init_data.get('sspToken')),
          'device_type': 'Web',
          'device_type_code': 'WP_DASH',
        })
      try:
        tokens['access_token_tcdn'] = self.request_tcdn_token(
          access_token, tokens.get('account_nbr'))
      except Exception as exc:
        LOG('tcdn refresh after init failed: {}'.format(exc))
      self.save_tokens(tokens)
      self.apply_tokens(tokens)
      return True

    def renew_init_tokens(self, tokens=None):
      tokens = tokens or self.load_tokens()
      account_nbr = tokens.get('account_nbr') or self.account.get('id')
      if not account_nbr:
        return False

      for item in self.usable_token_items(tokens):
        try:
          init_data = self.request_init_data(
            item.get('accessToken_init'), account_nbr, item.get('device_id'))
          return self.store_renewed_init_data(tokens, item.get('device_id'), init_data)
        except Exception as exc:
          LOG('renew with accessToken_init failed for {}: {}'.format(
            item.get('device_id'), exc))

      login_token = tokens.get('access_token_login')
      device_id = tokens.get('device_id_actual') or self.account.get('device_id')
      if login_token and not token_expired(login_token) and device_id:
        try:
          if self.put_device_id(device_id, login_token, account_nbr):
            init_data = self.request_init_data(login_token, account_nbr, device_id)
            return self.store_renewed_init_data(tokens, device_id, init_data)
        except Exception as exc:
          LOG('renew with access_token_login failed: {}'.format(exc))

      username = tokens.get('username')
      password = tokens.get('password')
      if username and not password:
        stored_username, stored_password = self.load_credentials()
        if stored_username == username:
          password = stored_password
      if username and password:
        try:
          device_id = device_id or uuid.uuid4().hex
          login_token = self.request_login_token(username, password, device_id)
          if self.put_device_id(device_id, login_token, account_nbr):
            init_data = self.request_init_data(login_token, account_nbr, device_id)
            tokens['access_token_login'] = login_token
            return self.store_renewed_init_data(tokens, device_id, init_data)
        except Exception as exc:
          LOG('renew with stored credentials failed: {}'.format(exc))

      return False

    def add_new_device(self):
      tokens = self.load_tokens()
      username = tokens.get('username')
      password = tokens.get('password')
      if username and not password:
        stored_username, stored_password = self.load_credentials()
        if stored_username == username:
          password = stored_password
      account_nbr = tokens.get('account_nbr')
      if not username or not password or not account_nbr:
        return False, 'No hay credenciales guardadas para crear dispositivo.'
      device_id = uuid.uuid4().hex
      try:
        login_token = self.request_login_token(username, password, device_id)
        if not self.put_device_id(device_id, login_token, account_nbr):
          return False, 'No se pudo registrar el dispositivo.'
        init_data = self.request_init_data(login_token, account_nbr, device_id)
        tokens['access_token_login'] = login_token
        self.store_renewed_init_data(tokens, device_id, init_data)
        return True, device_id
      except Exception as exc:
        LOG('add_new_device failed: {}'.format(exc))
        return False, str(exc)

    def sync_devices(self):
      devices = self.get_devices()
      tokens = self.load_tokens()
      if not devices or not tokens:
        return False, 'No hay dispositivos para sincronizar.'
      known = {item.get('device_id'): item for item in tokens.get('accessToken_init_lista', [])}
      for device in devices:
        if self.is_protected_device(device) or device.get('id') in known:
          continue
        tokens.setdefault('accessToken_init_lista', []).append({
          'device_id': device.get('id'),
          'accessToken_init': '',
          'fecha_obtencion': '',
          'expira': 'Desconocida',
          'ultimo_uso': '',
          'ssp': device.get('in_ssp', False),
          'device_type': device.get('type', ''),
          'device_type_code': device.get('type_code', ''),
        })
      api_ids = set([d.get('id') for d in devices])
      tokens['accessToken_init_lista'] = [
        item for item in tokens.get('accessToken_init_lista', [])
        if item.get('device_id') in api_ids or item.get('accessToken_init')
      ]
      self.save_tokens(tokens)
      self.update_token_device_types(devices)
      return True, '{} dispositivos sincronizados.'.format(len(devices))

    def get_session_token(self):
      return self.account.get('session_token', '')

    def get_ssp_token(self):
      tokens = self.load_tokens()
      if token_expired(tokens.get('sspToken_init')):
        self.renew_init_tokens(tokens)
        tokens = self.load_tokens()
      return tokens.get('sspToken_init') or self.account.get('ssp_token', '')

    def update_session_token(self):
      return

    def get_profiles(self):
      headers = self.net.headers.copy()
      headers['X-HZId'] = self.account['session_token']
      url = self.endpoints['listaperfiles']
      data = self.net.load_data(url, headers=headers)
      res = []
      for d in data.get('items', []):
        p = {}
        p['id'] = d['id']
        p['name'] = d['name']
        p['for_kids'] = d['isForKids']
        p['type'] = d['typeID']
        p['image_id'] = d['imageID']
        res.append(p)
      return res

    def change_profile(self, id):
      self.account['profile_id'] = id
      self.cache.save_file('profile_id.conf', self.account['profile_id'])
      tokens = self.load_tokens()
      if tokens:
        tokens['profile_id'] = id
        if self.renew_init_tokens(tokens):
          return

    def load_epg_data(self, date_str, duration=2, channels=''):
      demarcation = self.account['demarcation']
      url = self.endpoints['rejilla'].format(deviceType='webplayer', profile=self.account['platform'], UTCDATETIME=date_str, DURATION=duration, CHANNELS=channels, NETWORK='movistarplus', mdrm='true', demarcation=demarcation)
      if self.quality == 'UHD': url += '&filterQuality=UHD'
      #LOG(url)
      data = self.net.load_data(url)
      return data

    def get_epg(self, date=None, duration=2, channels=None):
      if channels or date:
        if not date:
          today = datetime.today()
          date = today.strftime('%Y-%m-%dT00:00:00')
        epg_data = self.load_epg_data(date, duration, channels if channels else '')
        if not epg_data or 'error' in epg_data: return {}
        if channels:
          data = []
          data.append(epg_data)
        else:
          data = epg_data
      else:
        cache_filename = 'epg_{}_{}.json'.format(self.quality, duration)
        content = self.cache.load(cache_filename, 6*60)
        if content:
          data = json.loads(content)
        else:
          today = datetime.today()
          str_now = today.strftime('%Y-%m-%dT00:00:00')
          data = self.load_epg_data(str_now, duration=duration)
          if not 'error' in data:
            self.cache.save_file(cache_filename, json.dumps(data, ensure_ascii=False))

      epg = {}
      if 'error' in data: return epg

      for ch in data:
        if isinstance(ch, dict):
          programs = ch.get('Pases') or ch.get('Programas') or ch.get('programs') or []
          if not programs and ch.get('FechaHoraInicio'):
            programs = [ch]
          channel_data = ch.get('Canal') or ch
        else:
          programs = ch
          channel_data = ch[0].get('Canal', {}) if ch else {}
        id = (channel_data.get('CodCadenaTv') or channel_data.get('ChannelId') or
              channel_data.get('id') or channel_data.get('CasId'))
        if not id: continue
        if not id in epg: epg[id] = []
        for p in programs:
          pr = {}
          if not p.get('FechaHoraInicio') or not p.get('FechaHoraFin'):
            continue
          pr['start'] = int(p['FechaHoraInicio'])
          pr['end'] = int(p['FechaHoraFin'])
          pr['start_str'] = timestamp2str(pr['start'])
          pr['end_str'] = timestamp2str(pr['end'])
          pr['date_str'] = timestamp2str(pr['start'], '%a %d %H:%M')
          pr['desc1'] = p.get('Titulo') or p.get('Nombre') or ''
          pr['desc2'] = ''
          if 'TituloHorLinea2' in p:
            pr['desc2'] = p['TituloHorLinea2']
          pr['show_id'] = p.get('ShowId') or p.get('Id') or ''
          if 'SerialId' in p:
            pr['serie_id'] = p['SerialId']
          pr['id'] = p.get('Id') or pr['show_id']
          #if 'links' in p: pr['links'] = p['links']
          epg[id].append(pr)

      return epg

    def find_program_epg(self, epg, id, timestamp):
      found = False
      c = 0
      programs = []
      if not id in epg: return programs
      for p in epg[id]:
        #print(p)
        if (p['start'] <= timestamp) and (timestamp <= p['end']):
          found = True
        if found:
          programs.append(p)
          c += 1
          if (c > 5): break;
      return programs

    def colorize_title(self, title):
      s = title['info']['title']

      stype = title.get('stream_type')
      if stype == 'u7d': s += ' (U7D)'
      elif stype == 'rec': s += ' (REC)'
      if title.get('video_format') == '4K': s += " (4K)"

      color1 = 'yellow'
      color2 = 'red'

      available = True
      if 'subscribed' in title and title['subscribed'] == False: available = False
      if 'url' in title and title['url'] == '': available = False

      if not available:
        color1 = 'gray'
        color2 = 'gray'
        s = '[COLOR gray]' + s +'[/COLOR]'
      elif 'start' in title:
        aired = (title['start'] <= (time.time() * 1000))
        if not aired:
          color1 = 'blue'
          color2 = 'blue'
          s = '[COLOR blue]' + s +'[/COLOR]'
      if title.get('desc1', '') != '':
        s += ' - [COLOR {}]{}[/COLOR]'.format(color1, title['desc1'])
      if title.get('desc2', '') != '':
        s += ' - [COLOR {}]{}[/COLOR]'.format(color2, title['desc2'])
      return s

    def add_epg_info(self, channels, epg, timestamp):
      for ch in channels:
        programs = self.find_program_epg(epg, ch['id'], timestamp)
        plot = ''
        for i in range(len(programs)):
          desc1 = programs[i]['desc1']
          desc2 = programs[i]['desc2']
          if i == 0:
            ch['desc1'] = desc1
            ch['desc2'] = desc2
          plot += "[B]{}[/B] {} {}\n".format(programs[i]['start_str'], desc1, desc2)
          if self.add_extra_info:
            ch['art']['poster'] = ch['art']['fanart'] = None
            ch['show_id'] = programs[i]['show_id']
            self.add_video_extra_info(ch)
            if 'plot' in ch['info']:
              plot = plot + ch['info']['plot']
            break
        ch['info']['plot'] = plot

    def is_subscribed_channel(self, products):
      if not products:
        return True
      for e in self.entitlements['activePackages']:
        if isinstance(e, dict) and e.get('name') in products:
           return True
      for e in self.entitlements['activePurchases']:
        if isinstance(e, dict) and e.get('name') in products:
           return True
      for e in self.entitlements['tvRights']:
        if e in products:
           return True
      return False

    def is_subscribed_vod(self, products):
      #for p in products:
      #  if p['Nombre'] in self.entitlements['vodSubscription']:
      #     return True
      #return False
      return self.is_subscribed_channel(products)

    def get_channels(self):
      demarcation = self.account['demarcation']
      profile = self.account['platform']
      cache_filename = 'channels_{}.json'.format(self.quality)
      content = self.cache.load(cache_filename)
      if content:
        data = json.loads(content)
      else:
        url = self.endpoints['epg_nueva']
        data = self.net.load_data(url, self.new_api_headers(None))
        if 'error' in data:
          url = self.endpoints['canales'].format(deviceType='webplayer', profile=profile, mdrm='true', demarcation=demarcation)
          if self.quality == 'UHD': url += '&filterQuality=UHD'
          data = self.net.load_data(url)
        if not 'error' in data:
          self.cache.save_file(cache_filename, json.dumps(data, ensure_ascii=False))

      res = []
      if 'error' in data: return res

      for c in data:
        name = (c.get('Nombre') or c.get('Name') or '').strip()
        if not name:
          continue
        channel_id = c.get('CodCadenaTv') or c.get('ChannelId') or c.get('id') or c.get('CasId') or name
        channel_id = str(channel_id)
        t = {}
        t['info'] = {}
        t['art'] = {}
        t['type'] = 'movie'
        t['stream_type'] = 'tv'
        t['info']['mediatype'] = 'movie'
        t['channel_name'] = name
        t['info']['title'] = str(c.get('Dial', '0')) +'. ' + t['channel_name']
        t['id'] = channel_id
        t['cas_id'] = c.get('CasId')
        #if add_epg_info:
        #  t['desc1'] = c['Nombre']
        #  t['desc2'] = ''
        t['dial'] = c.get('Dial', '0')
        t['url'] = c.get('PuntoReproduccion') or c.get('UrlVideo') or ''
        if 'Logo' in c:
          t['art']['icon'] = t['art']['thumb'] = t['art']['poster'] = c['Logo']
        elif 'Logos' in c:
          t['art']['icon'] = t['art']['thumb'] = t['art']['poster'] = c['Logos'][0]['uri']
        t['session_request'] = '{"contentID":"'+ t['id'] +'", "streamType":"CHN"}'
        t['subscribed'] = self.is_subscribed_channel(c.get('tvProducts', []))
        t['info']['playcount'] = 1 # Set as watched
        # epg
        #if add_epg_info:
        #  program = c['Pases'][0]
        #  t['desc1'] = program['Titulo']
        #  if 'TituloHorLinea2' in program:
        #    t['desc2'] = program['TituloHorLinea2']
        #  t['start'] = int(program['FechaHoraInicio'])
        #  t['end'] = int(program['FechaHoraFin'])
        #  t['start_str'] = timestamp2str(t['start'])
        #  t['end_str'] = timestamp2str(t['end'])
        #  t['info']['plot'] = '[B]{}-{}[/B] {}\n{}'.format(t['start_str'], t['end_str'], t['desc1'], t['desc2'])

        res.append(t)

      return res

    def get_channels_with_epg(self):
      channels = self.get_channels()
      epg = self.get_epg(duration=1)
      import time
      now = int(time.time() * 1000)
      self.add_epg_info(channels, epg, now)
      return channels

    def download_list(self, url, use_hz = False):
      headers = self.net.headers.copy()
      headers['Accept'] = 'application/vnd.miviewtv.v1+json'
      headers['Content-Type'] = 'application/json'
      if use_hz:
        headers['Authorization'] = 'Bearer ' + self.account['access_token']
        headers['X-Hzid'] = self.account['session_token']
      return self.net.load_data(url, headers)

    def add_to_wishlist(self, id, stype='vod'):
      LOG('add_to_wishlist: {} {}'.format(id, stype))
      url = self.endpoints['marcadofavoritos2'].format(family=stype)
      headers = self.net.headers.copy()
      headers['Accept'] = 'application/vnd.miviewtv.v1+json'
      headers['Content-Type'] = 'application/json'
      headers['X-Hzid'] = self.account['session_token']
      post_data = {'objectID': id}
      response = self.net.session.post(url, data=json.dumps(post_data), headers=headers)
      content = response.content.decode('utf-8')
      if response.status_code != 201:
        data = json.loads(content)
        if 'resultCode' in data:
          return data['resultCode'], data['resultText']
      return response.status_code, ''

    def delete_from_wishlist(self, id, stype='vod'):
      url = self.endpoints['borradofavoritos'].format(family=stype, contentId=id)
      headers = self.net.headers.copy()
      headers['Accept'] = 'application/vnd.miviewtv.v1+json'
      headers['Content-Type'] = 'application/json'
      headers['X-Hzid'] = self.account['session_token']
      response = self.net.session.delete(url, headers=headers)
      content = response.content.decode('utf-8')
      if response.status_code != 204:
        data = json.loads(content)
        if 'resultCode' in data:
          return data['resultCode'], data['resultText']
      return response.status_code, ''

    def get_wishlist_url(self):
      url = self.endpoints['favoritos'].format(
              deviceType=self.dplayer, DIGITALPLUSUSERIDC=self.account['encoded_user'], PROFILE=self.account['platform'],
              ACCOUNTNUMBER=self.account['id'], idsOnly='false', start=1, end=50, mdrm='true', demarcation=self.account['demarcation'])
      #url += '&filter=AD-SINX&topic=CN'
      if self.quality == 'UHD': url += '&filterQuality=UHD'
      url += '&_='+ str(int(time.time()*1000))
      return url

    def get_recordings_url(self):
      url = self.endpoints['grabaciones'].format(
              deviceType=self.dplayer, DIGITALPLUSUSERIDC=self.account['encoded_user'], PROFILE=self.account['platform'],
              idsOnly='false', start=1, end=50, mdrm='true', demarcation=self.account['demarcation'])
      #url += '&state=Completed&_='+ str(int(time.time()*1000))
      url += '&_='+ str(int(time.time()*1000))
      return url

    def get_viewings_url(self):
      url = self.endpoints['ultimasreproducciones'].format(
              deviceType=self.dplayer, DIGITALPLUSUSERIDC=self.account['encoded_user'], PROFILE=self.account['platform'],
              ACCOUNTNUMBER=self.account['id'], idsOnly='false', start=1, end=50, mdrm='true', demarcation=self.account['demarcation'])
      url += '&filter=AD-SINX'
      url += '&_='+ str(int(time.time()*1000))
      return url

    def get_search_url(self, search_term):
      url = self.endpoints['buscar_best'].format(
                 deviceType=self.dplayer,
                 ACCOUNTNUMBER=self.account['id'],
                 profile=self.account['platform'],
                 texto=search_term,
                 distilledTvRights=','.join(self.entitlements['distilledTvRights']),
                 mdrm='true', demarcation=self.account['demarcation'])
      if self.quality == 'UHD': url += '&filterQuality=UHD'
      return url

    def add_search(self, search_term):
      self.search_list.append(search_term)
      self.cache.save_file('searchs.json', json.dumps(self.search_list, ensure_ascii=False))

    def delete_search(self, search_term):
      self.search_list = [s for s in self.search_list if s != search_term]
      self.cache.save_file('searchs.json', json.dumps(self.search_list, ensure_ascii=False))

    def get_favorite_data(self, links):
      res = {}
      for link in links:
        name = link['rel']
        if 'favorites' in name:
          d = {}
          d['id'] = link['id']
          d['family'] = link.get('href', '').split("/")[-2]
          res[name] = d
      return res

    def get_ficha_url(self, id, mode='GLOBAL', catalog=''):
      url = self.endpoints['ficha'].format(deviceType=self.dplayer, id=id, profile=self.account['platform'], mediatype='FOTOV', version='7.1', mode=mode, catalog=catalog, channels='', state='', mdrm='true', demarcation=self.account['demarcation'], legacyBoxOffice='')
      url = url.replace('state=&', '')
      if self.quality == 'UHD': url += '&filterQuality=UHD'
      #print(url)
      return url

    def get_title(self, data):
      t = {}
      t['info'] = {}
      t['art'] = {}
      ed = data['DatosEditoriales']
      t['id'] = ed['Id']
      t['info']['title'] = ed['Titulo']
      t['art']['poster'] = ed.get('Imagen', '').replace('ywcatalogov', 'dispficha')
      if 'Imagenes' in ed:
        for i in ed['Imagenes']:
          if i['id'] == 'detail':
            t['art']['poster'] = i['uri']
      t['art']['thumb'] = t['art']['poster']
      t['info']['genre'] = ed['GeneroComAntena']
      if ed.get('TipoComercial') == 'Impulsivo': return None # Alquiler
      if 'Seguible' in ed: t['seguible'] = ed['Seguible']
      if 'links' in data:
        #t['links'] = data['links']
        t['favorite_data'] = self.get_favorite_data(data['links'])
      if ed['TipoContenido'] in ['Individual', 'Episodio']:
        t['type'] = 'movie'
        t['stream_type'] = 'vod'
        t['info']['mediatype'] = 'movie'
        t['info']['duration'] = ed['DuracionEnSegundos']
        t['url'] = ''
        t['session_request'] = ''
        if len(data['VodItems']) > 0:
          video = data['VodItems'][0]
          t['subscribed'] = self.is_subscribed_vod(video.get('tvProducts', []))
          if not 'UrlVideo' in video: return None
          t['url'] = video['UrlVideo']
          t['availability'] = {'start': video.get('FechaInicioPublicacion'), 'end': video.get('FechaFinPublicacion')}
          if video['AssetType'] == 'VOD':
            t['session_request'] = '{"contentID":' + str(t['id']) + ',"drmMediaID":"' + video['CasId'] +'", "streamType":"AST"}'
          elif video['AssetType'] == 'U7D':
            t['stream_type'] = 'u7d'
            t['session_request'] = '{"contentID":' + str(t['id']) + ', "streamType":"CUTV"}'
          elif video['AssetType'] == 'NPVR':
            t['stream_type'] = 'rec'
            t['session_request'] = '{"contentID":' + str(t['id']) + ', "streamType":"NPVR"}'
          if 'ShowId' in video: t['show_id'] = video['ShowId']
          if self.add_extra_info:
            self.add_video_extra_info(t)
        elif 'Pases' in data and len(data['Pases']) > 0:
          video = data['Pases'][0]
          t['subscribed'] = self.is_subscribed_vod(video.get('tvProducts', []))
          if 'ShowId' in video: t['show_id'] = video['ShowId']
          if 'Canal' in video and 'CasId' in video['Canal']:
            t['cas_id'] = video['Canal']['CasId']
          stype = None
          if 'catalog=catchup' in ed['Ficha']:
            stype = 'catch-up'
            t['stream_type'] = 'u7d'
            t['session_request'] = '{"contentID":' + str(t['id']) + ', "streamType":"CUTV"}'
          elif 'catalog=npvr' in ed['Ficha']:
            stype = 'npvr'
            t['stream_type'] = 'rec'
            t['session_request'] = '{"contentID":' + str(t['id']) + ', "streamType":"NPVR"}'
          if 'UrlVideo' in video:
            t['url'] = video['UrlVideo']
          else:
            if stype:
              for l in video['links']:
                if l['rel'] == stype:
                  t['url'] = l['href']
                  break
        if 'Recording' in data:
          t['stream_type'] = 'rec'
          t['rec'] = {'id': data['Recording']['id'],
                      'name': data['Recording']['name'],
                      'start': data['Recording']['beginTime'],
                      'end': data['Recording']['endTime']}
          if t['url'] == '': t['info']['title'] += ' (' + isodate2str(t['rec']['start']) + ')'
        if t['url'] == '' and t['stream_type'] == 'vod': t['subscribed'] = False
      if ed['TipoContenido'] == 'Serie':
        t['type'] = 'series'
        t['info']['mediatype'] = 'tvshow'
        t['subscribed'] = self.is_subscribed_vod(data.get('tvProducts', []))
      if ed['TipoContenido'] == 'Temporada':
        t['type'] = 'season'
        t['info']['mediatype'] = 'season'
        t['subscribed'] = self.is_subscribed_vod(data.get('tvProducts', []))
      if 'DatosAccesoAnonimo' in data:
        da = data['DatosAccesoAnonimo']
        t['video_format'] = da.get('FormatoVideo')
        if 'HoraInicio' in da and da['HoraInicio'] != None:
          t['start'] = int(da['HoraInicio'])
          t['start_str'] = timestamp2str(t['start'])
          t['date_str'] = timestamp2str(t['start'], '%a %d %H:%M')
          if t['url'] == '': t['info']['title'] += ' (' + t['date_str'] +')'
      return t

    def get_list(self, data):
      res = []
      for d in data:
        #print_json(d)
        t = self.get_title(d)
        if t and t['id'] != '':
          res.append(t)
      return res

    def add_video_extra_info(self, t):
      #LOG('add_video_extra_info: t: {}'.format(t))
      try:
        if 'show_id' in t:
          catalog = 'events'
          id = t['show_id']
        elif 'id' in t:
          catalog = ''
          id = t['id']
        else:
          return
        #LOG('id: {} catalog: {}'.format(id, catalog))

        prefix = 'event' if catalog=='events' else 'info'
        cache_filename = 'cache/{}_{}.json'.format(prefix, id)
        content = self.cache.load(cache_filename, 30*24*60)
        if content:
          data = json.loads(content)
        else:
          url = self.get_ficha_url(id=id, catalog=catalog)
          #print(url)
          data = self.net.load_data(url)
          self.cache.save_file(cache_filename, json.dumps(data, ensure_ascii=False))

        if not 'info' in t: t['info'] = {}
        if not 'art' in t: t['art'] = {}

        if not t['info'].get('title'):
          if 'TituloEpisodio' in data:
            t['info']['title'] = data['TituloEpisodio']
          else:
            t['info']['title'] = data['Titulo']

        if 'Serie' in data:
          if not t['info'].get('episode'): t['info']['episode'] = data['NumeroEpisodio']
          if not t['info'].get('tvshowtitle'): t['info']['tvshowtitle'] = data['Serie']['TituloSerie']
          if not t['info'].get('season') and data['Serie'].get('Temporada'):
            m = re.search(r'T(\d+)', data['Serie']['Temporada'])
            if m: t['info']['season'] = m.group(1)

        if not 'duration' in t['info']:
          t['info']['duration'] = data.get('Duracion', 0) * 60

        if data.get('Sinopsis'):
          t['info']['plot'] = data['Sinopsis']
        if data.get('Actores'):
          t['info']['cast'] = data['Actores'].split(', ')
        if data.get('Directores'):
          t['info']['director'] = data['Directores'].split(', ')
        if data.get('Anno'):
          t['info']['year'] = data['Anno']
        if data.get('Nacionalidad'):
          t['info']['country'] = data['Nacionalidad']
        im_thumb = im_default = im_season = im_fanart = None
        for im in data['Imagenes']:
          if im['id'] == 'horizontal': im_thumb = im['uri']
          elif im['id'] == 'default': im_default = im['uri']
          elif im['id'] == 'watch2tgr-end': im_fanart = im['uri']
          elif im['id'] == 'temporada': im_season = im['uri']
        if im_thumb and not t['art'].get('thumb'): t['art']['thumb'] = im_thumb
        if im_season:
          if not t['art'].get('poster'): t['art']['poster'] = im_season
        if im_fanart:
          if not t['art'].get('fanart'): t['art']['fanart'] = im_fanart
        if im_default:
          if not t['art'].get('poster'): t['art']['poster'] = im_default
          if not t['art'].get('icon'): t['art']['icon'] = im_default
          if not t['art'].get('fanart'): t['art']['fanart'] = im_default
      except:
        pass

    def get_seasons(self, id):
      url = self.get_ficha_url(id)
      #print(url)
      data = self.net.load_data(url)
      #print_json(data)
      res = []
      c = 1
      for d in data['Temporadas']:
        t = {}
        t['id'] = d['Id']
        t['info'] = {}
        t['art'] = {}
        t['type'] = 'season'
        t['info']['mediatype'] = 'season'
        t['info']['title'] = d['Titulo']
        t['info']['tvshowtitle'] = data['TituloSerie']
        t['info']['plot'] = data['Descripcion']
        t['info']['season'] = c
        if 'Imagen' in data:
          t['art']['poster'] = t['art']['thumb'] = data['Imagen']
        elif 'Imagenes' in data:
          for im in data['Imagenes']:
            if im['id'] == 'ver-details': t['art']['poster'] = im['uri']
        t['subscribed'] = self.is_subscribed_vod(data.get('tvProducts', []))
        if 'Seguible' in data: t['seguible'] = data['Seguible']
        #t['video_format'] = data.get('FormatoVideo')
        c += 1
        res.append(t)

      return res

    def get_episodes(self, id):
      url = self.get_ficha_url(id=str(id))
      #print(url)
      data = self.net.load_data(url)
      #print_json(data)
      #Movistar.save_file('/tmp/episodes.json', json.dumps(data, ensure_ascii=False))
      res = []
      for d in data['Episodios']:
        ed = d['DatosEditoriales']
        t = {}
        t['id'] = ed['Id']
        t['info'] = {}
        t['art'] = {}
        t['type'] = 'movie'
        t['info']['mediatype'] = 'episode'
        t['stream_type'] = 'vod'
        t['info']['title'] = ed['TituloEpisodio']
        t['info']['episode'] = ed['NumeroEpisodio']
        t['info']['duration'] = ed['DuracionEnSegundos']
        t['info']['tvshowtitle'] = data.get('TituloSerie', '')
        if 'Imagen' in data:
          t['art']['poster'] = data['Imagen']
        elif 'Imagenes' in data:
          for im in data['Imagenes']:
            if im['id'] == 'ver-details': t['art']['poster'] = im['uri']
        for im in ed['Imagenes']:
          if im['id'] == 'horizontal': t['art']['thumb'] = im['uri']
        t['info']['season'] = '0'
        if ed.get('Temporada'):
          m = re.search(r'T(\d+)', ed['Temporada'])
          if m: t['info']['season'] = m.group(1)
        t['url'] = ''
        t['session_request'] = ''
        if len(d['VodItems']) > 0:
          video = d['VodItems'][0]
          #if video.get('AssetType') == 'SOON': continue
          if not 'UrlVideo' in video: continue
          t['subscribed'] = self.is_subscribed_vod(video.get('tvProducts', []))
          t['url'] = video['UrlVideo']
          t['availability'] = {'start': video.get('FechaInicioPublicacion'), 'end': video.get('FechaFinPublicacion')}
          if video['AssetType'] == 'VOD':
            t['session_request'] = '{"contentID":' + str(t['id']) + ',"drmMediaID":"' + video['CasId'] +'", "streamType":"AST"}'
          elif video['AssetType'] == 'U7D':
            t['stream_type'] = 'u7d'
            t['session_request'] = '{"contentID":' + str(t['id']) + ', "streamType":"CUTV"}'
          if 'ShowId' in video: t['show_id'] = video['ShowId']
        elif 'Pases' in d and len(d['Pases']) > 0:
          video = d['Pases'][0]
          t['subscribed'] = self.is_subscribed_vod(video.get('tvProducts', []))
          if 'ShowId' in video: t['show_id'] = video['ShowId']
          if 'Canal' in video and 'CasId' in video['Canal']:
            t['cas_id'] = video['Canal']['CasId']
          stype = None
          if 'VODRU7D' in ed['Ficha']:
            stype = 'catch-up'
            t['stream_type'] = 'u7d'
            t['session_request'] = '{"contentID":' + str(t['id']) + ', "streamType":"CUTV"}'
          if 'UrlVideo' in video:
            t['url'] = video['UrlVideo']
          else:
            if stype:
              for l in video['links']:
                if l['rel'] == stype:
                  t['url'] = l['href']
                  break
        if 'DatosAccesoAnonimo' in d:
          da = d['DatosAccesoAnonimo']
          t['video_format'] = da.get('FormatoVideo')
          if 'HoraInicio' in da and da['HoraInicio'] != None:
            t['start'] = int(da['HoraInicio'])
            t['start_str'] = timestamp2str(t['start'])
            t['date_str'] = timestamp2str(t['start'], '%a %d %H:%M')
            if t['url'] == '': t['info']['title'] += ' (' + t['date_str'] +')'
        if self.add_extra_info:
          self.add_video_extra_info(t)
        res.append(t)

      return res

    def get_u7d_url(self, url):
      headers = self.net.headers.copy()
      headers['Content-Type'] = 'application/json'
      headers['X-HZId'] = self.account['session_token']
      data = self.net.load_data(url, headers)
      return data

    def order_recording(self, program_id):
      headers = self.net.headers.copy()
      headers['Content-Type'] = 'application/json'
      headers['X-Hzid'] = self.account['session_token']
      url = self.endpoints['grabarprograma']
      data = '{"tvProgramID":"' + str(program_id) +'"}'
      response = self.net.session.post(url, data=data, headers=headers)
      content = response.content.decode('utf-8')
      try:
        data = json.loads(content)
        return data
      except:
        return None

    def order_recording_season(self, season_id):
      headers = self.net.headers.copy()
      headers['Content-Type'] = 'application/json'
      headers['X-Hzid'] = self.account['session_token']
      url = self.endpoints['grabartemporada']
      data = '{"tvSeasonID":"' + str(season_id) +'"}'
      response = self.net.session.post(url, data=data, headers=headers)
      content = response.content.decode('utf-8')
      try:
        data = json.loads(content)
        return data
      except:
        return None

    def delete_recording(self, program_id):
      headers = self.net.headers.copy()
      headers['Content-Type'] = 'application/json'
      headers['X-Hzid'] = self.account['session_token']
      url = self.endpoints['borrargrabacionindividual'].format(showId=program_id)
      response = self.net.session.delete(url, headers=headers)
      content = response.content.decode('utf-8')
      try:
        data = json.loads(content)
        return data
      except:
        return None

    def epg_to_movies(self, channel_id, date=None, duration=2):
      LOG('epg_to_movies: {} {}'.format(channel_id, date))
      epg = self.get_epg(channels=channel_id, date=date, duration=duration)
      res = []
      if not channel_id in epg: return res
      for p in epg[channel_id]:
        if sys.version_info[0] < 3:
          p['date_str'] = unicode(p['date_str'], 'utf-8')
        name = '[B]' + p['date_str'] + '[/B] ' + p['desc1']
        if p['desc2']: name += ' - '+ p['desc2']
        plot = name +"\n" + p['desc2']
        t = {'info': {}, 'art': {}}
        t['info']['title'] = name
        t['info']['plot'] = plot
        t['type'] = 'movie'
        t['stream_type'] = 'u7d'
        t['info']['mediatype'] = 'movie'
        t['url'] = ''
        t['id'] = p['id']
        t['show_id'] = p['show_id']
        if 'serie_id' in p:
          t['serie_id'] = p['serie_id']
        t['session_request'] = '{"contentID":' + str(t['id']) +',"streamType":"CUTV"}'
        t['info']['playcount'] = 1 # Set as watched
        t['start'] = p['start']
        t['end'] = p['end']
        t['aired'] = (p['end'] <= (time.time() * 1000))
        t['subscribed'] = True # Fix me
        """
        if 'links' in p:
          for link in p['links']:
            if link['rel'] == 'start-over':
              t['url'] = link['href']
        """
        t['url'] = 'https://grmovistar.imagenio.telefonica.net/asfe/rest/tvMediaURLs?tvProgram.id='+ str(t['show_id']) +'&svc=startover'
        if self.add_extra_info:
          self.add_video_extra_info(t)
        res.append(t)
      return res

    def get_subtitles(self, manifest_url):
      base_url = os.path.dirname(manifest_url)
      headers = self.net.headers.copy()
      headers['x-tcdn-token'] = self.get_cdntoken()
      content = self.net.load_url(manifest_url, headers)
      rx = r'<AdaptationSet id="\d+" contentType="text" mimeType="application\/ttml\+xml" lang="(.*?)">.*?<BaseURL>(.*?)<\/BaseURL>'
      matches = re.findall(rx, content, flags=re.MULTILINE | re.DOTALL)
      res = []
      for m in matches:
        sub = {}
        sub['lang'] = m[0]
        sub['filename'] = m[1]
        sub['url'] = base_url +'/' + sub['filename']
        if sub['lang'] == 'srd': sub['lang'] = 'es [CC]'
        if sub['lang'] == 'qaa': sub['lang'] = 'en [VO]'
        res.append(sub)
      return res

    def download_subtitles(self, sublist):
      output_dir = self.cache.config_directory + 'subtitles'
      #LOG('output_dir: {}'.format(output_dir))
      if not os.path.exists(output_dir):
        os.makedirs(output_dir)
      headers = self.net.headers.copy()
      headers['x-tcdn-token'] = self.get_cdntoken()
      res = []
      for s in sublist:
        filename = output_dir + os.sep + s['lang'] + '.ttml'
        LOG('filename: {}'.format(filename))
        content = self.net.load_url(s['url'], headers)
        with io.open(filename, 'w', encoding='utf-8', newline='') as handle:
          handle.write(content)
        res.append(filename)
      return res

    def get_vod_list_url(self, cat='movies'):
      #sort = 'FE'
      sort = 'MA'
      #sort = 'AZ'
      url = self.endpoints['consultar'].format(deviceType=self.dplayer, profile=self.account['platform'], sort=sort, start=1, end=50, mdrm='true', demarcation=self.account['demarcation'])
      filter = '&filter=AC-OM,AC-MA,MA-GBLCICLO59,TD-CUP,RG-SINCAT,AC-PREPUB,' + self.entitlements['suscripcion']
      #filter = '&filter=AC-OM,AC-MA,MA-GBLCICLO59,TD-CUP,RG-SINCAT,AC-PREPUB'
      url += '&mode=VOD' + filter
      if cat == 'tvshows':
        url += '&topic=SR&showSeries=series'
      elif cat == 'movies':
        url += '&topic=CN'
      elif cat == 'documentaries':
        url += '&topic=DC'
      elif cat == 'kids':
        url += '&topic=IN'
      if self.quality == 'UHD': url += '&filterQuality=UHD'
      #LOG('vod url: {}'.format(url))
      return url

    def get_vod_sections(self):
      from collections import OrderedDict

      profile = self.account['platform']
      #profile = 'OTT'
      #profile = 'LITE'

      content = self.cache.load('vod_sections2.json')
      if content:
        data = json.loads(content)
      else:
        url = 'https://ottcache.dof6.com/movistarplus/yomvi/phone.android/{}/configuration/config_item?format=json&uisegment='.format(profile)
        data = self.net.load_data(url)
        self.cache.save_file('vod_sections2.json', json.dumps(data, ensure_ascii=False))

      main_menu_items = []
      main_menu = data.get('VOD', {}).get('Menu', [])
      for m in main_menu:
        if m['@id'] == 'MENU-PRINCIPAL':
          for i in m['Item']:
            main_menu_items.append(i['@id'])

      submenu = data.get('VOD', {}).get('Submenu', [])
      res = OrderedDict()
      for o in submenu:
        #print_json(o)
        menu = {}
        menu['visible'] = True
        menu['id'] = o.get('@id')
        if menu['id'] not in main_menu_items: continue

        if 'Modulo' in o:
          section_name = o['@nombre'] if '@nombre' in o else o['@P'] if '@P' in o else 'sin nombre'
          #print(section_name)
          modulo = o['Modulo']

          section = []

          if isinstance(modulo, dict):
            modulo = [modulo]

          if isinstance(modulo, list):
            for m in modulo:

              sort = ''
              if 'Modificador' in m:
                #print(m['Modificador'])
                for p in m['Modificador']:
                  #print(p)
                  #if p['@type'] == 'novisible': continue
                  if p['@id'] == 'ordenacion': sort = p['@selected']

              if 'consulta' in m and m['consulta'].get('@endpoint_ref', '') == 'consultar':
                if not '@nombre' in m: continue
                #print (menu['id'], m['@nombre'])
                c = {}
                c['name'] = m['@nombre']
                pars = ''

                parametros = m['consulta']['parametro']
                if isinstance(parametros, dict):
                  parametros = [parametros]

                for par in parametros:
                  #print_json(par)
                  pars += '&{}={}'.format(par['@id'], par['@value'])

                pars = pars.replace('{suscripcion}', self.entitlements['suscripcion'])
                c['url'] = self.endpoints['consultar'].format(deviceType=self.dplayer, profile=profile, sort=sort, start=1, end=50, mdrm='true', demarcation=self.account['demarcation'])
                c['url'] += pars
                #print(c['url'])
                if self.quality == 'UHD': c['url'] += '&filterQuality=UHD'
                section.append(c)
          if len(section) > 0:
            menu['name'] = section_name
            menu['data'] = section
            res[menu['id']] = menu

      return res

    def install_key_file(self, filename):
      if sys.version_info[0] > 2:
        filename = bytes(filename, 'utf-8')
      shutil.copyfile(filename, self.cache.config_directory + 'auth.key')

    def export_key_file(self, filename):
      if sys.version_info[0] > 2:
        filename = bytes(filename, 'utf-8')
      if self.account['access_token']:
        data = {'timestamp': int(time.time()*1000),
                'response':{'access_token': self.account['access_token'], 'token_type': 'bearer'}};
        with io.open(filename, 'w', encoding='utf-8', newline='') as handle:
         handle.write(json.dumps(data, ensure_ascii=False))
      else:
        shutil.copyfile(self.cache.config_directory + 'auth.key', filename)

    def load_key_file(self):
      content = self.cache.load_file('auth.key')
      if content:
        data = json.loads(content)
        if 'response' in data:
          self.account['access_token'] = data['response']['access_token']
        elif 'data' in data:
          data = json.loads(data['data'])
          self.account['access_token'] = data['response']['access_token']

    def save_key_file(self, d):
      data = {'timestamp': int(time.time()*1000), 'response': d}
      self.cache.save_file('auth.key', json.dumps(data, ensure_ascii=False))

    def import_credentials(self, filename):
      if sys.version_info[0] > 2:
        filename = bytes(filename, 'utf-8')
      shutil.copyfile(filename, self.cache.config_directory + 'credentials.json')

    def export_credentials(self, filename):
      if sys.version_info[0] > 2:
        filename = bytes(filename, 'utf-8')
      shutil.copyfile(self.cache.config_directory + 'credentials.json', filename)

    def delete_session_files(self):
      for f in ['access_token.conf', 'account.json', 'device_id.conf', 'devices.json', 'profile_id.conf', 'tokens.json', 'channels2.json', 'channels_UHD.json', 'channels_HD.json', 'epg2.json', 'epg_UHD.json', 'epg_HD.json', 'cdn.conf']:
        self.cache.remove_file(f)

    def get_profile_image_url(self, img_id):
      content = self.cache.load_file('avatars.json')
      if content:
        data = json.loads(content)
      else:
        url = self.endpoints['avatares']
        data = self.net.load_data(url)
        self.cache.save_file('avatars.json', json.dumps(data, ensure_ascii=False))
      #print_json(data)
      for avatar in data:
        if avatar['id'] == img_id:
          for link in avatar['links']:
            if link['sizes'] == '512x512':
              return link['href']
      return None

    def export_channels(self):
      if sys.version_info[0] >= 3:
        from urllib.parse import urlencode
      else:
        from urllib import urlencode
      channels = self.get_channels()
      res = []
      for c in channels:
        t = {}
        t['subscribed'] = c['subscribed']
        t['name'] = c['channel_name']
        t['id'] = c['id']
        t['logo'] = c['art']['icon']
        t['preset'] = c['dial']
        args = urlencode({'action': 'play', 'stype': 'tv', 'id': c['id'], 'url': c['url'], 'session_request': c['session_request']})
        t['stream'] = 'plugin://plugin.video.movistarplus/?' + args
        if 'cas_id' in c: t['cas_id'] = c['cas_id']
        res.append(t)
      return res

    def export_channels_to_m3u8(self, filename, only_subscribed=False):
      channels = self.export_channels()
      items = []
      for t in channels:
        if only_subscribed and not t['subscribed']: continue
        url = t['stream']
        if 'cas_id' in t: url += '&cas_id=' + t['cas_id']
        item = '#EXTINF:-1 tvg-name="{name}" tvg-id="{id}" tvg-logo="{logo}" tvg-chno="{preset}" group-title="Movistar+",{name}\n{stream}\n\n'.format(
            name=t['name'], id=t['id'], logo=t['logo'], preset=t['preset'], stream=url)
        items.append(item)
      res = '#EXTM3U\n## Movistar+\n{}'.format(''.join(items))
      with io.open(filename, 'w', encoding='utf-8', newline='') as handle:
        handle.write(res)

    def read_m3u_groups(self, ini_filename):
      if not ini_filename or not os.path.exists(ini_filename):
        return {}, {}
      try:
        import configparser
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read(ini_filename, encoding='utf-8')
        groups = {}
        if parser.has_section('grupos'):
          for key, value in parser.items('grupos'):
            for channel in [c.strip() for c in value.split(',') if c.strip()]:
              groups[channel] = key
        keys = {}
        if parser.has_section('kid_key'):
          for key, value in parser.items('kid_key'):
            pairs = []
            for pair in value.split():
              if ':' in pair:
                pairs.append(pair.strip())
            if pairs:
              keys[key] = ','.join(pairs)
        return groups, keys
      except Exception as exc:
        LOG('read_m3u_groups failed: {}'.format(exc))
        return {}, {}

    def generate_m3u(self, ini_filename=None):
      token = self.get_cdntoken()
      if not token:
        return False, 'No se pudo obtener token TCDN.'
      groups, keys = self.read_m3u_groups(ini_filename)
      channels = self.get_channels()
      if not channels:
        return False, 'No se encontraron canales.'

      output = os.path.join(self.cache.config_directory, 'movistarplus_token.m3u')
      lines = ['#EXTM3U catchup-type="default" catchup-days="40"\n']
      headers = 'X-TCDN-token={}&User-Agent={}&Origin=https://ver.movistarplus.es&Referer=https://ver.movistarplus.es/'.format(
        token, useragent)
      for channel in channels:
        name = channel.get('channel_name') or channel['info'].get('title')
        group = groups.get(name, 'Movistar+')
        logo = channel.get('art', {}).get('icon', '')
        url = channel.get('url', '')
        if not url:
          continue
        lines.append('#EXTINF:-1 tvg-id="{id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}\n'.format(
          id=channel.get('id', name), name=name, logo=logo, group=group))
        lines.append('#KODIPROP:inputstream=inputstream.adaptive\n')
        lines.append('#KODIPROP:inputstream.adaptive.manifest_type=mpd\n')
        lines.append('#KODIPROP:inputstream.adaptive.manifest_headers={}\n'.format(headers))
        if keys.get(name):
          lines.append('#KODIPROP:inputstream.adaptive.drm_legacy=org.w3.clearkey|{}\n'.format(keys[name]))
        lines.append('{}\n\n'.format(url))

      with io.open(output, 'w', encoding='utf-8', newline='') as handle:
        handle.write(''.join(lines))
      return True, 'M3U generado: {}'.format(output)

    def needs_hcsmno(self, cas_id):
      if not cas_id:
        return False
      cache_filename = 'hcsmno_{}.conf'.format(cas_id)
      cached = self.cache.load_file(cache_filename)
      if cached in ['0', '1']:
        return cached == '1'

      token = self.get_cdntoken()
      if not token:
        return False

      fake_date = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
      start = '{}T00:00:00Z'.format(fake_date)
      end = '{}T01:00:00Z'.format(fake_date)
      headers = self.net.headers.copy()
      headers['X-TCDN-Token'] = token

      old_url = 'https://stover-wp0.cdn.telefonica.com/{}/vxfmt=dp/Manifest.mpd?device_profile=DASH_TV_PLAYREADY&start_time={}&end_time={}'.format(
        cas_id, start, end)
      new_url = 'https://stoverhcsmno-wp0.cdn.telefonica.com/{}/vxfmt=dp/Manifest.mpd?device_profile=DASH_TV_PLAYREADY&start_time={}&end_time={}'.format(
        cas_id, start, end)
      try:
        response = self.net.session.get(old_url, headers=headers, allow_redirects=True, timeout=10)
        if response.status_code in [200, 302]:
          self.cache.save_file(cache_filename, '0')
          return False
      except Exception as exc:
        LOG('old catchup domain check failed: {}'.format(exc))
      try:
        response = self.net.session.get(new_url, headers=headers, allow_redirects=True, timeout=10)
        if response.status_code in [200, 302]:
          self.cache.save_file(cache_filename, '1')
          return True
      except Exception as exc:
        LOG('hcsmno domain check failed: {}'.format(exc))
      return False

    def export_epg(self, date=None, duration=2):
      if sys.version_info[0] >= 3:
        from urllib.parse import urlencode
      else:
        from urllib import urlencode

      res = {}
      epg = self.get_epg(date, duration)
      channels = self.get_channels()
      for channel in channels:
        id = channel['id']
        if not id in epg: continue
        res[id] = []
        for e in epg[id]:
          t = {}
          t['title'] = e['desc1']
          t['subtitle'] = e['desc2']
          t['start'] = datetime.utcfromtimestamp(e['start']/1000).strftime('%Y-%m-%dT%H:%M:%SZ')
          t['stop'] = datetime.utcfromtimestamp(e['end']/1000).strftime('%Y-%m-%dT%H:%M:%SZ')

          if True:
            program_id = str(e['show_id'])
            url = 'https://grmovistar.imagenio.telefonica.net/asfe/rest/tvMediaURLs?tvProgram.id='+ program_id +'&svc=startover'
            session_request = '{"contentID":' + program_id +',"streamType":"CUTV"}'
            args = urlencode({'action': 'play', 'stype': 'u7d', 'id': program_id, 'url': url, 'session_request': session_request})
            t['stream'] = 'plugin://plugin.video.movistarplus/?' + args

          if False and self.add_extra_info and id in ['HOLLYW', 'TCM', 'AMC', 'MV3', 'MV2', 'CPSER', 'FOXGE', 'TNT']:
            i = {'id': e['id'], 'show_id': e['show_id']}
            self.add_video_extra_info(i)
            #print(i)
            t['description'] = i['info'].get('plot')
            t['image'] = i['art'].get('poster')
            t['credits'] = []
            for text in i['info'].get('director', []):
              t['credits'].append({'type': 'director', 'name': text})
            for text in i['info'].get('cast', []):
              t['credits'].append({'type': 'actor', 'name': text})

          res[id].append(t)
      return res

    def export_epg_to_xml(self, filename, ndays=3, report_func=None, only_subscribed=False):
      if sys.version_info[0] < 3:
        # Python 2
        from cgi import escape as html_escape
      else:
        # Python 3
        from html import escape as html_escape

      channels = self.export_channels()
      res = []
      res.append('<?xml version="1.0" encoding="UTF-8"?>\n' + 
                 '<!DOCTYPE tv SYSTEM "xmltv.dtd">\n' + 
                 '<tv>\n')

      for t in channels:
        if only_subscribed and not t['subscribed']: continue
        res.append('<channel id="{}">\n'.format(t['id']) + 
                  '  <display-name>{}</display-name>\n'.format(t['name']) + 
                  '  <icon src="{}"/>\n'.format(t['logo']) + 
                  '</channel>\n')

      if True:
        epg = {}
        today = datetime.today()
        total_items = 0
        for i in range(0, ndays, 1):
          date = today + timedelta(days=i)
          strdate = date.strftime('%Y-%m-%dT00:00:00')
          LOG('epg: {}'.format(strdate))
          if report_func:
            report_func(date.strftime('%d/%m/%Y'))
          e = self.export_epg(strdate, 1)
          LOG('epg: channels: {}'.format(len(e)))
          for ch in e:
            if not ch in epg: epg[ch] = []
            total_items += len(e[ch])
            epg[ch].extend(e[ch])
          LOG('epg: total items: {}'.format(total_items))
      else:
        epg = self.export_epg()

      for ch in channels:
        if not ch['id'] in epg: continue
        if only_subscribed and not ch['subscribed']: continue
        for e in epg[ch['id']]:
          #LOG('* e:{}'.format(e))
          start = datetime.strptime(e['start'], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%d%H%M%S +0000")
          stop = datetime.strptime(e['stop'], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%d%H%M%S +0000")
          url = e.get('stream', None)
          if url:
            url = url.replace('&', '&amp;')
          res.append('<programme start="{}" stop="{}" channel="{}"'.format(start, stop, ch['id']) +
                    #(' catchup-id="{}"'.format(url) if url else "") +
                    '>\n' +
                    '  <title>{}</title>\n'.format(html_escape(e['title'])) +
                    '  <sub-title>{}</sub-title>\n'.format(html_escape(e['subtitle'])))
          if 'image' in e:
            res.append('  <icon src="{}"/>\n'.format(e['image']))
          if 'description' in e:
            res.append('  <desc>{}</desc>\n'.format(html_escape(e['description'])))
          if 'credits' in e and len(e['credits']) > 0:
            res.append('  <credits>\n');
            for c in e['credits']:
              if c['type'] == 'director':
                res.append('    <director>{}</director>\n'.format(c['name']))
              elif c['type'] == 'actor':
                res.append('    <actor>{}</actor>\n'.format(c['name']))
            res.append('  </credits>\n');
          res.append('</programme>\n')
      res.append('</tv>\n')
      with io.open(filename, 'w', encoding='utf-8', newline='') as handle:
        handle.write(''.join(res))

    def save_credentials(self, username, password):
      from .b64 import encode_base64
      data = {'username': username, 'password': encode_base64(password)}
      self.cache.save_file('credentials.json', json.dumps(data, ensure_ascii=False))

    def load_credentials(self):
      from .b64 import decode_base64
      content = self.cache.load_file('credentials.json')
      if content:
        data = json.loads(content)
        return data['username'], decode_base64(data['password'])
      else:
        return '', ''

    def get_token_properties(self, token=None):
      from .b64 import decode_base64
      if not token:
        token = self.account['access_token']
      data = None
      try:
        l = token.split('.')
        if len(l) > 1:
          padding = len(l[1]) % 4
          l[1] += '=' * (4 - padding) if padding != 0 else ''
          s = decode_base64(l[1])
          data = json.loads(s)
      except:
        pass
      return data

    def get_token_expire_date(self, token):
      data = self.get_token_properties(token)
      return data.get('exp', 0)

    def get_accounts(self):
      accounts = []
      for d in os.listdir(self.config_dir):
        if os.path.isdir(os.path.join(self.config_dir, d)) and d.startswith("account_"):
          account = {'id': d, 'name': d.replace('account_', 'Cuenta ')}
          content = Movistar.load_file_if_exists(os.path.join(self.config_dir, d, 'account.json'))
          if content:
            data = json.loads(content)
            login = data.get('login')
            if login:
              if '@' in login:
                p1, p2 = login.split('@')
                account['login'] = p1[:3] +'...@'+ p2
              else:
                account['login'] = login[:3]
          accounts.append(account)
      return accounts

    def switch_account(self, name):
      Movistar.save_file(self.config_dir + 'account.txt', name)

    def create_new_account(self):
      accounts = self.get_accounts()
      name = None
      n = 1
      while True:
        name = 'account_' + str(n)
        if not any(account.get("id") == name for account in accounts):
          break;
        n += 1
      if name:
        os.makedirs(self.config_dir + name)
      return name

    def download_manifest(self, manifest_url):
      headers = self.net.headers.copy()
      headers['x-tcdn-token'] = self.get_cdntoken()
      content = self.net.load_url(manifest_url, headers)
      return content

    @staticmethod
    def load_file_if_exists(filename):
      content = None
      if os.path.exists(filename):
        with io.open(filename, 'r', encoding='utf-8') as handle:
          content = handle.read()
      return content

    @staticmethod
    def save_file(filename, content):
      if sys.version_info[0] < 3:
        if not isinstance(content, unicode):
          content = unicode(content, 'utf-8')
      with io.open(filename, 'w', encoding='utf-8') as handle:
        handle.write(content)
