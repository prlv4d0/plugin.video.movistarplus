# encoding: utf-8
#
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import unicode_literals, absolute_import, division

endpoints = {
    "avatares": "https://ottcache.dof6.com/vod/config/perfiles/config/avatares.json",
    "borradofavoritos": "https://grmovistar.imagenio.telefonica.net/asfe/rest/users/favorites/{family}/{contentId}",
    "borrargrabacionindividual": "https://grmovistar.imagenio.telefonica.net/asfe/rest/users/npvrRecordings/{showId}",
    "buscar_best": "https://perso.dof6.com/movistarplus/{deviceType}/users/contents/search?accountnumber={ACCOUNTNUMBER}&profile={profile}&term={texto}&mode=VODRU7D&showSeries=series&distilledTvRights={distilledTvRights}&version=8&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}&scope=DAZN",
    "canales": "https://ottcache.dof6.com/movistarplus/{deviceType}/{profile}/contents/channels?mdrm={mdrm}&tlsstream=true&demarcation={demarcation}&version=8",
    "consultar": "https://ottcache.dof6.com/movistarplus/{deviceType}/contents/browse?profile={profile}&sort={sort}&version=8&start={start}&end={end}&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}",
    "favoritos": "https://perso.dof6.com/movistarplus/{deviceType}/users/{DIGITALPLUSUSERIDC}/favorites?profile={PROFILE}&version=8&mediaType=FOTOV&accountNumber={ACCOUNTNUMBER}&idsOnly={idsOnly}&start={start}&end={end}&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}",
    "ficha": "https://ottcache.dof6.com/movistarplus/{deviceType}/contents/{id}/details?profile={profile}&mediaType={mediatype}&version=8&mode={mode}&catalog={catalog}&channels={channels}&state={state}&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}&legacyBoxOffice={legacyBoxOffice}",
    "grabaciones": "https://perso.dof6.com/movistarplus/npvr/{deviceType}/users/{DIGITALPLUSUSERIDC}/recordings?profile={PROFILE}&mediaType=FOTOH&version=8&idsOnly={idsOnly}&start={start}&end={end}&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}",
    "grabarprograma": "https://grmovistar.imagenio.telefonica.net/asfe/rest/users/npvrRecordings",
    "grabartemporada": "https://grmovistar.imagenio.telefonica.net/asfe/rest/users/npvrScheduledSeasons",
    "listaperfiles": "https://grmovistar.imagenio.telefonica.net/asfe/rest/users/profiles?state=0&isForKids=0",
    "marcadofavoritos2": "https://grmovistar.imagenio.telefonica.net/asfe/rest/users/favorites/{family}",
    "rejilla": "https://ottcache.dof6.com/movistarplus/{deviceType}/{profile}/epg?from={UTCDATETIME}&span={DURATION}&channel={CHANNELS}&network={NETWORK}&version=8&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}",
    "setUpStream": "https://alkasvaspub.imagenio.telefonica.net/asvas/ccs/{PID}/{deviceCode}/{PLAYREADYID}/Session",
    "tearDownStream": "https://alkasvaspub.imagenio.telefonica.net/asvas/ccs/{PID}/{deviceCode}/{PLAYREADYID}/Session/{SessionID}",
    "ultimasreproducciones": "https://perso.dof6.com/movistarplus/{deviceType}/users/{DIGITALPLUSUSERIDC}/viewings?profile={PROFILE}&container=trackedseries&mediaType=FOTOH&idsOnly={idsOnly}&accountNumber={ACCOUNTNUMBER}&version=8&start={start}&end={end}&mdrm={mdrm}&tlsstream=true&demarcation={demarcation}",
    "account_info": "https://soter-pf.sve.video.telefonicaservices.com/service/login/webplayer/accountInfo?legacy=true",
    "delete_device": "https://soterpe-pf.sve.video.telefonicaservices.com/service/session/users/{ACCOUNTNUMBER}/devices/clientzone/{DEVICEID}",
    "devices": "https://soterpe-pf.sve.video.telefonicaservices.com/service/session/clientzone/{ACCOUNTNUMBER}/devices",
    "epg_nueva": "https://soteroc-pf.cdn.sve.video.telefonicaservices.com/service/contents/webplayer/DIFUSION/contents/epg",
    "initdata": "https://soter-pf.sve.video.telefonicaservices.com/service/login/initData?legacy=true",
    "register_device": "https://soter-pf.sve.video.telefonicaservices.com/service/login/devices?deviceId={DEVICEID}&deviceType=webplayer&accountId={ACCOUNTNUMBER}&legacy=true",
    "renovacion_cdntoken2": "https://soter-pf.sve.video.telefonicaservices.com/service/tcdnidserver/{ACCOUNTNUMBER}/devices/webplayer/cdn/token/refresh",
    "token": "https://soter-pf.sve.video.telefonicaservices.com/service/oidc/connect/token?deviceClass=webplayer"
}
