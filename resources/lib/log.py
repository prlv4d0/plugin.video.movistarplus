# encoding: utf-8
#
# SPDX-License-Identifier: LGPL-2.1-or-later

import json

def _log(message, level_name='LOGDEBUG'):
  try:
    import xbmc
    import xbmcaddon
    level = getattr(xbmc, level_name, xbmc.LOGDEBUG)
    xbmc.log('[{}] {}'.format(xbmcaddon.Addon().getAddonInfo('id'), message), level)
  except:
    print(message)

def LOG(message):
  _log(message, 'LOGDEBUG')

def INFO(message):
  _log(message, 'LOGINFO')

def ERROR(message):
  _log(message, 'LOGERROR')

def print_json(data):
  LOG(json.dumps(data, indent=4))
