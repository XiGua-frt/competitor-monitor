import json
import re
import time

import requests
from bs4 import BeautifulSoup

from loguru import logger
from utils.tiktok_utils import trans_cookies, generate_requests, generate_html_headers, generate_request_headers
from builder.auth import BrowserEvidenceError
from .tiktok_web import TiktokWebAPI


class LegacyTiktokAPI:
    def __init__(self, *args, **kwargs):
        # This class is retained only as a source reference.  Its historical
        # methods contain hard-coded Chrome-132/device/signature fields and
        # must never be allowed to issue a request after the evidence-driven
        # refactor.  The public ``TiktokAPI`` below delegates to
        # ``TiktokWebAPI`` instead.
        raise BrowserEvidenceError(
            "LegacyTiktokAPI 已停用；请使用 TiktokAPI/TiktokWebAPI，"
            "否则无法保证浏览器字段与纯计算签名一致"
        )

    tiktok_url = 'https://www.tiktok.com'

    def get_user_posted(self, secUid, cursor, cookies_str):
        cookies = trans_cookies(cookies_str)
        url = self.tiktok_url + "/api/post/item_list/"
        params = {
           "WebIdLastTime": "1737115998",
           "aid": "1988",
           "app_language": "zh-Hans",
           "app_name": "tiktok_web",
           "browser_language": "zh-CN",
           "browser_name": "Mozilla",
           "browser_online": "true",
           "browser_platform": "Win32",
           "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
           "channel": "tiktok_web",
           "cookie_enabled": "true",
           "count": "16",
           "coverFormat": "2",
           "cursor": cursor,
           "data_collection_enabled": "false",
           "device_id": "7460856262408259088",
           "device_platform": "web_pc",
           "focus_state": "true",
           "from_page": "user",
           "history_len": "2",
           "is_fullscreen": "false",
           "is_page_visible": "true",
           "language": "zh-Hans",
           "needPinnedItemIds": "true",
           "odinId": re.findall(r'multi_sids=(.*?)%3A', cookies_str)[0],
           "os": "windows",
           "post_item_list_request_type": "0",
           "priority_region": "",
           "referer": "",
           "region": "JP",
           "root_referer": "https://www.tiktok.com/@gunvw",
           "screen_height": "1440",
           "screen_width": "2560",
           "secUid": secUid,
           "tz_name": "Asia/Shanghai",
           "user_is_login": "false",
           "verifyFp": cookies["s_v_web_id"],
           "webcast_language": "zh-Hans",
           "msToken": cookies["msToken"],
        }
        url, headers = generate_requests(url, params, cookies_str)
        response = requests.get(url, headers=headers)
        res_json = response.json()
        return res_json


    def get_live_room_info(self, live_url, cookies_str):
        if 'vt.tiktok.com' in live_url:
            logger.info("redirect short live url")
            res = requests.get(live_url, allow_redirects=False)
            live_url = res.headers['Location']
            logger.info(f"閲嶅畾鍚戝悗鐨勭洿鎾棿閾炬帴: {live_url}")
        headers = generate_html_headers(live_url)
        cookies = trans_cookies(cookies_str)
        response = requests.get(live_url, headers=headers, cookies=cookies)
        res_text = response.text
        soup = BeautifulSoup(res_text, 'html.parser')
        script = soup.find_all('script', attrs={'id': 'SIGI_STATE'})[0]
        script_text = script.text
        script_text = script_text.replace('false', 'False').replace('true', 'True').replace('null', 'None').replace('undefined', 'None')
        ans = eval(script_text)
        try:
            user = ans['LiveRoom']['liveRoomUserInfo']['user']
            user_id, secUid, uniqueId, room_id, status = user['id'], user['secUid'], user['uniqueId'], user['roomId'], user['status']
            return user_id, secUid, uniqueId, room_id, status, live_url
        except Exception as e:
            return None

        # ans = re.findall(r'"id":"(.*?)","nickname":".*?","secUid":"(.*?)","secret":.*?,"uniqueId":"(.*?)","verified":.*?,"roomId":"(.*?)",', res_text)
        # user_id, secUid, uniqueId, room_id = ans[0]
        # return user_id, secUid, uniqueId, room_id


    def get_webcast_rank_list(self, referer, anchor_id, room_id, cookies_str):
        cookies = trans_cookies(cookies_str)
        url = "https://webcast.tiktok.com/webcast/ranklist/online_audience/"
        params = {
            "aid": "1988",
            "anchor_id": anchor_id,
            "app_language": "zh-Hans",
            "app_name": "tiktok_web",
            "browser_language": "zh-CN",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "data_collection_enabled": "true",
            "device_id": "7460856262408259088",
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "user",
            "history_len": "4",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "os": "windows",
            "priority_region": "",
            "referer": referer,
            "region": "JP",
            "room_id": room_id,
            "root_referer": "https://www.tiktok.com/@gunvw",
            "screen_height": "1440",
            "screen_width": "2560",
            "tz_name": "Asia/Shanghai",
            "user_is_login": "true",
            "verifyFp": cookies["s_v_web_id"],
            "webcast_language": "zh-Hans",
            "msToken": cookies["msToken"],
        }
        url, headers = generate_requests(url, params, cookies_str)
        response = requests.get(url, headers=headers)
        res_json = response.json()
        return res_json

    def get_user_info(self, user_url, cookies_str):
        cookies = trans_cookies(cookies_str)
        headers = generate_html_headers(user_url)
        response = requests.get(user_url, headers=headers, cookies=cookies)
        res_text = response.text
        user_info = re.findall(r'"webapp\.user-detail":(.*?),"webapp\.a-b"', res_text)[0]
        user_info = user_info.replace('false', 'False').replace('true', 'True').replace('null', 'None').replace('undefined', 'None')
        user_info = eval(user_info)
        return user_info

    def get_user_live_info(self, user_url, cookies_str, proxies=None):
        live_url = user_url + '/live'
        cookies = trans_cookies(cookies_str)
        headers = generate_html_headers(user_url)

        response = requests.get(live_url, headers=headers, cookies=cookies, proxies=proxies)
        res_text = response.text
        soup = BeautifulSoup(res_text, 'html.parser')
        script = soup.find_all('script', attrs={'id': 'SIGI_STATE'})[0]
        script_text = script.text
        user_live_info = json.loads(script_text)
        return user_live_info

    def get_user_info_and_text(self, user_url, cookies_str, proxies=None):
        cookies = trans_cookies(cookies_str)
        headers = generate_html_headers(user_url)

        response = requests.get(user_url, headers=headers, cookies=cookies, proxies=proxies)
        res_text = response.text
        user_info = re.findall(r'"webapp\.user-detail":(.*?),"webapp\.a-b"', res_text)[0]
        user_info = user_info.replace('false', 'False').replace('true', 'True').replace('null', 'None').replace('undefined', 'None')
        user_info = eval(user_info)
        return user_info, res_text

    def get_simple_user_info(self, user_id, cookies_str):
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.tiktok.com/messages?lang=zh-Hans",
            "sec-ch-ua": "\"Not A(Brand\";v=\"8\", \"Chromium\";v=\"132\", \"Google Chrome\";v=\"132\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        }
        url = "https://www.tiktok.com/tiktok/v1/im/user/profile/"
        cookies = trans_cookies(cookies_str)
        params = {
            "aid": "1988",
            "user_ids": f"[\"{user_id}\"]"
        }
        response = requests.get(url, headers=headers, cookies=cookies, params=params)
        res_json = response.json()
        return res_json


    def get_webcast_user_info(self, target_uid, room_id, cookies_str):
        cookies = trans_cookies(cookies_str)
        url = "https://webcast.tiktok.com/webcast/user/"
        params = {
            "aid": "1988",
            "app_language": "zh-Hans",
            "app_name": "tiktok_web",
            "browser_language": "zh-CN",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "current_room_id": room_id,
            "data_collection_enabled": "true",
            "device_id": "7460856262408259088",
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "user",
            "history_len": "3",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "os": "windows",
            "owner_user_id": re.findall(r'multi_sids=(.*?)%3A', cookies_str)[0],
            "priority_region": "",
            "referer": "",
            "region": "JP",
            "screen_height": "1440",
            "screen_width": "2560",
            "target_uid": target_uid,
            "tz_name": "Asia/Shanghai",
            "user_is_login": "true",
            "verifyFp": cookies["s_v_web_id"],
            "webcast_language": "zh-Hans",
            "msToken": cookies["msToken"],
        }
        url, headers = generate_requests(url, params, cookies_str)
        response = requests.get(url, headers=headers)
        res_json = response.json()
        return res_json

    def get_wid(self, cookies_str):
        cookies = trans_cookies(cookies_str)
        headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.tiktok.com/messages?lang=zh-Hans',
            'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
            'x-pns-referrer': 'https://www.tiktok.com/messages',
            'x-web-privacy-sdk-ver': '1.0.1',
        }
        params = {
            'locale': 'zh-Hans',
            'appId': '1988',
            'theme': 'default',
            'tea': '1',
        }

        response = requests.get('https://www.tiktok.com/api/v1/web-cookie-privacy/config', params=params, cookies=cookies, headers=headers)
        res_json = response.json()
        wid = res_json['body']['consent']['wid']
        return wid
    def search_live_room(self, keyword, offset, cookies_str):
        headers = generate_request_headers(cookies_str)
        cookies = trans_cookies(cookies_str)
        url = "https://www.tiktok.com/api/search/live/full/"
        params = {
            "WebIdLastTime": "1737115998",
            "aid": "1988",
            "app_language": "zh-Hans",
            "app_name": "tiktok_web",
            "browser_language": "zh-CN",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "count": "20",
            "data_collection_enabled": "true",
            "device_id": "7460856262408259088",
            "device_platform": "web_pc",
            "device_type": "web_h264",
            "focus_state": "true",
            "from_page": "search",
            "history_len": "15",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "keyword": keyword,
            "odinId": re.findall(r'multi_sids=(.*?)%3A', cookies_str)[0],
            "offset": str(offset),
            "os": "windows",
            "priority_region": "",
            "referer": "https://www.tiktok.com/live",
            "region": "JP",
            "root_referer": "https://www.tiktok.com/@gunvw",
            "screen_height": "1440",
            "screen_width": "2560",
            "tz_name": "Asia/Shanghai",
            "user_is_login": "true",
            "verifyFp": cookies["s_v_web_id"],
            "web_search_code": "{\"tiktok\":{\"client_params_x\":{\"search_engine\":{\"ies_mt_user_live_video_card_use_libra\":1,\"mt_search_general_user_live_card\":1}},\"search_server\":{}}}",
            "webcast_language": "zh-Hans"
        }
        response = requests.get(url, headers=headers, params=params)
        res_json = response.json()
        return res_json

    def spider_some_comment(self, aweme_id, cookies_str):
        cookies = trans_cookies(cookies_str)
        url = "https://www.tiktok.com/api/comment/list/"
        params = {
            "WebIdLastTime": str(int(time.time())),
            "aid": "1988",
            "app_language": "ja-JP",
            "app_name": "tiktok_web",
            "aweme_id": aweme_id,
            "browser_language": "zh-CN",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "count": "20",
            "current_region": "JP",
            "cursor": "0",
            "data_collection_enabled": "false",
            "device_id": "7473127097071519239",
            "device_platform": "web_pc",
            "enter_from": "tiktok_web",
            "focus_state": "false",
            "fromWeb": "1",
            "from_page": "video",
            "history_len": "5",
            "is_fullscreen": "false",
            "is_non_personalized": "false",
            "is_page_visible": "true",
            "odinId": re.findall(r'multi_sids=(.*?)%3A', cookies_str)[0],
            "os": "windows",
            "priority_region": "",
            "referer": "",
            "region": "RU",
            "screen_height": "1440",
            "screen_width": "2560",
            "tz_name": "Asia/Shanghai",
            "user_is_login": "false",
            "webcast_language": "zh-Hans",
            "msToken": cookies["msToken"],
        }
        url, headers = generate_requests(url, params, cookies_str)
        response = requests.get(url, headers=headers, cookies=cookies, params=params)
        print(response.text)
        res_json = response.json()
        return res_json


class TiktokAPI(TiktokWebAPI):
    """Compatibility facade backed by the evidence-driven API layer.

    The previous implementation is retained as ``LegacyTiktokAPI`` for
    reference only. New calls go through ``TiktokAuth``/``Params`` and refuse
    to send a signed request until browser query evidence has been supplied.
    """

    def get_user_posted(self, secUid, cursor, cookies_str):
        return super().get_user_posted(secUid, cursor, auth=self._auth(cookies_str))

    def get_live_room_info(self, live_url, cookies_str):
        return super().get_live_room_info(live_url, auth=self._auth(cookies_str))

    def get_user_info(self, user_url, cookies_str):
        return super().get_user_info(user_url, auth=self._auth(cookies_str))

    def get_video_detail(self, video_url, cookies_str):
        """Parse the standalone video's hydration itemStruct."""
        return super().get_video_detail(video_url, auth=self._auth(cookies_str))

    def get_user_info_and_text(self, user_url, cookies_str, proxies=None):
        auth = self._auth(cookies_str)
        html = self.get_user_html(user_url, auth=auth)
        return super().get_user_info(user_url, auth=auth), html

    def search_live_room(self, keyword, offset, cookies_str):
        return super().search_live_room(keyword, offset, auth=self._auth(cookies_str))

    def get_wid(self, cookies_str):
        return super().get_wid(auth=self._auth(cookies_str))

    def spider_some_comment(self, aweme_id, cookies_str):
        return super().get_comments(aweme_id, auth=self._auth(cookies_str))

    def get_comment_replies(self, item_id, comment_id, cookies_str, **kwargs):
        return super().get_comment_replies(
            item_id, comment_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_all_comments(self, aweme_id, cookies_str, **kwargs):
        """Read all top-level comments with response-owned pagination."""
        return super().get_all_comments(
            aweme_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_all_comment_replies(self, item_id, comment_id, cookies_str, **kwargs):
        """Read all child replies with response-owned pagination."""
        return super().get_all_comment_replies(
            item_id, comment_id, auth=self._auth(cookies_str), **kwargs,
        )

    def post_comment(self, aweme_id, text, cookies_str, **kwargs):
        """Compatibility wrapper for the evidence-backed comment write."""
        return super().post_comment(
            aweme_id, text, auth=self._auth(cookies_str), **kwargs,
        )

    def post_comment_reply(self, aweme_id, reply_id, text, cookies_str, **kwargs):
        """Compatibility wrapper for a captured comment-reply write."""
        return super().post_comment_reply(
            aweme_id, reply_id, text, auth=self._auth(cookies_str), **kwargs,
        )

    def get_shop_product_detail(self, product_url, cookies_str, **kwargs):
        """Read the complete Shop PDP SSR product component."""
        return super().get_shop_product_detail(
            product_url, auth=self._auth(cookies_str), **kwargs,
        )

    def get_shop_product_reviews(self, product_url, cookies_str, **kwargs):
        """Read the Shop PDP's evidence-backed initial review page."""
        return super().get_shop_product_reviews(
            product_url, auth=self._auth(cookies_str), **kwargs,
        )

    def get_shop_product_review_page(self, product_url, cookies_str, **kwargs):
        """Read one BSID-signed Shop review page."""
        return super().get_shop_product_review_page(
            product_url, auth=self._auth(cookies_str), **kwargs,
        )

    def get_all_shop_product_reviews(self, product_url, cookies_str, **kwargs):
        """Read every Shop review page with fresh per-page BSID values."""
        return super().get_all_shop_product_reviews(
            product_url, auth=self._auth(cookies_str), **kwargs,
        )

    def get_related_items(self, item_id, cookies_str, **kwargs):
        """Compatibility wrapper for the video-page related list."""
        return super().get_related_items(
            item_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_collection_list(self, sec_uid, cookies_str, **kwargs):
        """Compatibility wrapper for the captured profile Favorites tab."""
        return super().get_collection_list(
            sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def get_repost_list(self, sec_uid, cookies_str, **kwargs):
        """Compatibility wrapper for the captured profile Reposts tab."""
        return super().get_repost_list(
            sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def get_collected_item_list(self, sec_uid, cookies_str, **kwargs):
        """Compatibility wrapper for the profile collected-video tab."""
        return super().get_collected_item_list(
            sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def check_playlist_name(self, name, cookies_str, **kwargs):
        return super().check_playlist_name(
            name, auth=self._auth(cookies_str), **kwargs,
        )

    def post_collection_create(self, name, cookies_str, **kwargs):
        return super().post_collection_create(
            name, auth=self._auth(cookies_str), **kwargs,
        )

    def post_collection_modify_info(self, collection_id, collection_name,
                                    cookies_str, **kwargs):
        return super().post_collection_modify_info(
            collection_id, collection_name, auth=self._auth(cookies_str), **kwargs,
        )

    def post_collection_modify_items(self, collection_id, commit_ids,
                                     cookies_str, **kwargs):
        return super().post_collection_modify_items(
            collection_id, commit_ids, auth=self._auth(cookies_str), **kwargs,
        )

    def post_collection_move_items(self, from_collection_id,
                                   target_collection_id, item_ids,
                                   cookies_str, **kwargs):
        return super().post_collection_move_items(
            from_collection_id, target_collection_id, item_ids,
            auth=self._auth(cookies_str), **kwargs,
        )

    def get_collection_candidate_item_list(self, sec_uid, cookies_str, **kwargs):
        return super().get_collection_candidate_item_list(
            sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def get_collection_detail(self, collection_id, cookies_str, **kwargs):
        return super().get_collection_detail(
            collection_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_collection_item_list(self, collection_id, cookies_str, **kwargs):
        return super().get_collection_item_list(
            collection_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_profile_followers(self, sec_uid, cookies_str, **kwargs):
        """Compatibility wrapper for profile ``scene=67`` user lists."""
        return super().get_profile_followers(
            sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def get_profile_following(self, sec_uid, cookies_str, **kwargs):
        """Compatibility wrapper for profile ``scene=21`` user lists."""
        return super().get_profile_following(
            sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def get_following_item_list(self, cookies_str, **kwargs):
        """Compatibility wrapper for the captured following-page Feed."""
        return super().get_following_item_list(
            auth=self._auth(cookies_str), **kwargs,
        )

    def get_simple_user_info(self, user_id, cookies_str, **kwargs):
        """Compatibility wrapper for the unsigned IM profile lookup."""
        return super().get_im_user_profile(
            user_id, auth=self._auth(cookies_str), **kwargs,
        )

    def post_app_open_times_upload(self, cookies_str, **kwargs):
        """Compatibility wrapper for the captured unsigned telemetry POST."""
        return super().post_app_open_times_upload(
            auth=self._auth(cookies_str), **kwargs,
        )

    def post_item_digg(self, aweme_id, cookies_str, **kwargs):
        """Compatibility wrapper for the captured like/unlike write."""
        return super().post_item_digg(
            aweme_id, auth=self._auth(cookies_str), **kwargs,
        )

    def post_item_collect(self, item_id, sec_uid, cookies_str, **kwargs):
        """Compatibility wrapper for the captured Favorites write."""
        return super().post_item_collect(
            item_id, sec_uid, auth=self._auth(cookies_str), **kwargs,
        )

    def post_follow_user(self, user_id, sec_user_id, cookies_str, **kwargs):
        """Compatibility wrapper for the captured follow/unfollow write."""
        return super().post_follow_user(
            user_id, sec_user_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_webcast_rank_list(self, referer, anchor_id, room_id, cookies_str):
        return super().get_webcast_rank_list(
            anchor_id, room_id, auth=self._auth(cookies_str), referer=referer,
        )

    def get_webcast_user_info(self, target_uid, room_id, cookies_str, owner_user_id=None):
        if not owner_user_id:
            raise BrowserEvidenceError(
                "TikTok /webcast/user/ 的浏览器请求包含 owner_user_id；"
                "请从当前直播页取证后显式传入，拒绝发送空字段。"
            )
        return super().get_webcast_user_info(
            target_uid, room_id, owner_user_id=owner_user_id,
            auth=self._auth(cookies_str)
        )

    def get_webcast_room_create_info(self, cookies_str, **kwargs):
        """Compatibility wrapper for the signed live room/create_info read."""
        return super().get_webcast_room_create_info(
            auth=self._auth(cookies_str), **kwargs,
        )

    def post_epiphron_feature_upload(self, cookies_str, room_id, owner_id,
                                     user_id, local_timeregi_stamp, **kwargs):
        """Compatibility wrapper for live watch feature telemetry."""
        return super().post_epiphron_feature_upload(
            room_id, owner_id, user_id, local_timeregi_stamp,
            auth=self._auth(cookies_str), **kwargs,
        )

    def post_project_create(self, creation_id, cookies_str, **kwargs):
        """Compatibility wrapper for the unsigned Creator Studio project init."""
        return super().post_project_create(
            creation_id, auth=self._auth(cookies_str), **kwargs,
        )

    def post_creator_poi_list(self, cookies_str, **kwargs):
        """Compatibility wrapper for Creator Studio location search."""
        return super().post_creator_poi_list(
            auth=self._auth(cookies_str), **kwargs,
        )

    def get_im_messages_per_user_combo(self, inboxes, cookies_str, **kwargs):
        """Compatibility wrapper for the captured IM protobuf pull."""
        return super().get_im_messages_per_user_combo(
            inboxes, auth=self._auth(cookies_str), **kwargs,
        )

    def get_im_messages_per_user_init(self, cookies_str, **kwargs):
        """Compatibility wrapper for the captured IM user-init protobuf pull."""
        return super().get_im_messages_per_user_init(
            auth=self._auth(cookies_str), **kwargs,
        )

    def post_prefetch_explore_item_list(self, cookies_str, **kwargs):
        """Compatibility wrapper for the captured Explore prefetch POST."""
        return super().post_prefetch_explore_item_list(
            auth=self._auth(cookies_str), **kwargs,
        )

    def post_recharge_check_external_entry(self, profile_url, cookies_str,
                                           **kwargs):
        """Compatibility wrapper for the captured wallet eligibility POST."""
        return super().post_recharge_check_external_entry(
            profile_url, auth=self._auth(cookies_str), **kwargs,
        )

    def post_popup_callback(self, cookies_str, **kwargs):
        """Compatibility wrapper for the unsigned policy-popup callback."""
        return super().post_popup_callback(auth=self._auth(cookies_str), **kwargs)

    def post_csp_pa_prompt(self, cookies_str, **kwargs):
        """Compatibility wrapper for the unsigned privacy prompt probe."""
        return super().post_csp_pa_prompt(auth=self._auth(cookies_str), **kwargs)

    def get_music_dsp_platform_list(self, cookies_str, **kwargs):
        """Compatibility wrapper for the unsigned music DSP platform list."""
        return super().get_music_dsp_platform_list(
            auth=self._auth(cookies_str), **kwargs,
        )

    def get_user_detail(self, unique_id, cookies_str, **kwargs):
        """Compatibility wrapper for the signed profile detail API."""
        return super().get_user_detail(
            unique_id, auth=self._auth(cookies_str), **kwargs,
        )

    def post_recommend_feed(self, cookies_str, **kwargs):
        """Compatibility wrapper for the captured gzip homepage POST."""
        return super().post_recommend_feed(
            auth=self._auth(cookies_str), **kwargs,
        )

    def get_im_messages_by_conversation(self, conversation_id,
                                        conversation_short_id,
                                        conversation_type, anchor_index,
                                        direction, limit, cookies_str,
                                        **kwargs):
        """Compatibility wrapper for the captured conversation protobuf pull."""
        return super().get_im_messages_by_conversation(
            conversation_id, conversation_short_id, conversation_type,
            anchor_index, direction, limit, auth=self._auth(cookies_str),
            **kwargs,
        )

    def receive_im_messages(self, conversation_id, conversation_short_id,
                            conversation_type, anchor_index, direction, limit,
                            cookies_str, **kwargs):
        """Pull and decode text IM messages in one call."""
        return super().receive_im_messages(
            conversation_id, conversation_short_id, conversation_type,
            anchor_index, direction, limit,
            auth=self._auth(cookies_str), **kwargs,
        )

    def send_im_message(self, conversation_id, conversation_short_id, text,
                        cookies_str=None, **kwargs):
        """Compatibility wrapper for the current Chrome IM WebSocket send."""
        auth = kwargs.pop("auth", None) or self._auth(cookies_str)
        return super().send_im_message(
            conversation_id, conversation_short_id, text,
            auth=auth, **kwargs,
        )

    def apply_upload_inner(self, file_size, cookies_str, **kwargs):
        """Compatibility wrapper for fresh Creator Studio TOS authorization."""
        return super().apply_upload_inner(
            file_size, auth=self._auth(cookies_str), **kwargs,
        )

    def apply_image_upload(self, file_size, cookies_str, **kwargs):
        """Compatibility wrapper for Photo Mode ImageX authorization."""
        return super().apply_image_upload(
            file_size, auth=self._auth(cookies_str), **kwargs,
        )

    def commit_image_upload(self, session_key, cookies_str, **kwargs):
        """Compatibility wrapper for Photo Mode ImageX commit."""
        return super().commit_image_upload(
            session_key, auth=self._auth(cookies_str), **kwargs,
        )

    def upload_photo_bytes(self, data, cookies_str, **kwargs):
        """Compatibility wrapper for the complete ImageX image upload."""
        return super().upload_photo_bytes(
            data, auth=self._auth(cookies_str), **kwargs,
        )

    def get_upload_candidates(self, cookies_str, **kwargs):
        """Compatibility wrapper for signed VOD upload candidates."""
        return super().get_upload_candidates(
            auth=self._auth(cookies_str), **kwargs,
        )

    def probe_upload_candidates(self, candidates, cookies_str, **kwargs):
        """Compatibility wrapper for the four captured speed probes."""
        return super().probe_upload_candidates(
            candidates, auth=self._auth(cookies_str), **kwargs,
        )

    def upload_tos_bytes(self, upload_response, data, cookies_str, **kwargs):
        """Compatibility wrapper for the captured cross-site TOS upload."""
        return super().upload_tos_bytes(
            upload_response, data, auth=self._auth(cookies_str), **kwargs,
        )

    def commit_upload_inner(self, session_key, cookies_str, **kwargs):
        """Compatibility wrapper for TOS CommitUploadInner."""
        return super().commit_upload_inner(
            session_key, auth=self._auth(cookies_str), **kwargs,
        )

    def upload_media_bytes(self, data, cookies_str, **kwargs):
        """Compatibility wrapper for video post-upload or image commit."""
        return super().upload_media_bytes(
            data, auth=self._auth(cookies_str), **kwargs,
        )

    def publish_media(self, media, project_body, cookies_str, **kwargs):
        """One-call Creator upload + publish wrapper.

        ``project_body`` remains the exact JSON string captured from the same
        browser session (or a callback accepted by the web facade); this
        compatibility layer never reconstructs the signed nested payload.
        """
        return super().publish_media(
            media, project_body=project_body, auth=self._auth(cookies_str),
            **kwargs,
        )

    def creator_publish(self, media, text, cookies_str=None, **kwargs):
        """One-call full Creator publish; private visibility is the default."""
        auth = kwargs.pop("auth", None) or self._auth(cookies_str)
        return super().creator_publish(media, text, auth=auth, **kwargs)

    def creator_publish_photos(self, images, text, cookies_str=None, **kwargs):
        """One-call Photo Mode publish; private visibility is the default."""
        auth = kwargs.pop("auth", None) or self._auth(cookies_str)
        return super().creator_publish_photos(images, text, auth=auth, **kwargs)

    def enable_video_transcode(self, video_id, cookies_str, **kwargs):
        return super().enable_video_transcode(
            video_id, auth=self._auth(cookies_str), **kwargs,
        )

    def get_video_transcode_result(self, video_id, cookies_str, **kwargs):
        return super().get_video_transcode_result(
            video_id, auth=self._auth(cookies_str), **kwargs,
        )

