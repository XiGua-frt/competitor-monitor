import json
import re
import uuid
import time
import random
import threading
import urllib.parse
import hashlib

import blackboxprotobuf
from loguru import logger
from protobuf_to_dict import protobuf_to_dict

from api.tiktok import TiktokAPI
from builder.auth import BrowserEvidenceError, TiktokAuth
from websocket import WebSocketApp
from urllib.parse import urlencode
from utils.tiktok_utils import trans_cookies, send_common_message, send_strangerconversation_msg, send_live_room_message
import static.Tiktok_Request_pb2 as Tiktok_Request

BASE_URL = "wss://im-ws.tiktok.com/ws/v2"


class TiktokSendMsgWs:

    def __init__(self, cookies_str="", prefix="", *, auth=None):
        # The old class opened a long-running socket with static Chrome-132
        # fields.  It now delegates one-shot sends to the current
        # TiktokWebAPI command-100 implementation; live browser evidence is
        # still required by that implementation, so construction is safe.
        self.auth = auth or TiktokAuth.from_cookie(cookies_str)
        self.tiktok_api = TiktokAPI(self.auth)
        self.ws = None
        # 今天是否还能发送消息
        self.state = 0
        # 错误次数
        self.prefix = prefix
        self.error_times = 0
        self.author_url = 'https://www.tiktok.com/@cato_ovo'
        self.cookies_str = self.auth.cookie_str
        self.cookies = trans_cookies(self.cookies_str)
        self.access_key = ""
        self.msToken = self.auth.ms_token
        self.verifyFp = self.cookies["s_v_web_id"] if "s_v_web_id" in self.cookies else ''
        self.myid = self.auth.odin_id

    def generate_access_key(self):
        app_key = 'e1bd35ec9db7b8d846de66ed140b1ad9'
        wid = self.tiktok_api.get_wid(self.cookies_str)
        secret = f'9{app_key}{wid}f8a69f1719916z'
        m = hashlib.md5()
        m.update(secret.encode())
        return m.hexdigest()

    def heartbeat(self):
        while True:
            send_strangerconversation_msg(self)
            self.ws.send("hi")
            time.sleep(15)
            self.ws.send("hi")
    def on_open(self, ws):
        logger.info(f"{self.prefix} SOCKET连接成功")
        threading.Thread(target=self.heartbeat).start()

    def on_message(self, ws, message):
        if message == "hi":
            # logger.info(f"{self.prefix} 接收到心跳")
            pass
        else:
            frame = Tiktok_Request.Frame()
            frame.ParseFromString(message)
            payload = frame.payload
            response = Tiktok_Request.Response()
            response.ParseFromString(payload)
            response = protobuf_to_dict(response)
            if response['status_code'] == 0 and response['cmd'] == 100:
                send_message_body = response['body']['send_message_body']
                logger.info(f'{self.prefix} ==========================================================================')
                logger.info(f'{self.prefix} 发送消息结果')
                logger.info(f'{self.prefix} status: {send_message_body["status"]} check_code: {send_message_body["check_code"]}')
                if 'check_message' in send_message_body:
                    check_message = send_message_body['check_message']
                    check_message = json.loads(check_message)
                    logger.info(f'{self.prefix} status_code: {check_message["status_code"]} msg_type: {check_message["status_msg"]["msg_type"]} ')
                    msg_content = check_message["status_msg"]["msg_content"]["tips"]
                    logger.info(f'{self.prefix} msg_content: {msg_content}')
                    if '该消息无法发送' in msg_content:
                        self.error_times += 1
                    if self.error_times >= 3:
                        self.state = 1
                        logger.error(f'{self.prefix} 今天已经不能发送消息了')
                        self.ws.close()
                logger.info(f'{self.prefix} ==========================================================================')
            elif response['status_code'] == 0 and response['cmd'] == 500:
                message = response['body']['has_new_message_notify']['message']
                content = message['content']
                conversation_id = message['conversation_id']
                my_id = conversation_id.split(":")[3]
                to_id = conversation_id.split(":")[2]
                res = self.tiktok_api.get_simple_user_info(my_id, self.cookies_str)
                print(res)
                nickname = res['users'][0]['im_user_profile']['unique_id']
                my_url = f"https://www.tiktok.com/@{nickname}"
                res = self.tiktok_api.get_simple_user_info(to_id, self.cookies_str)
                nickname = res['users'][0]['im_user_profile']['unique_id']
                to_url = f"https://www.tiktok.com/@{nickname}"
                logger.info(f"{self.prefix} 接收到 {my_url} 发送给 {to_url} 的消息: {content}")
            # logger.info(f'{self.prefix} {response}')


    def on_error(self, ws, error):
        logger.error(error)

    def on_close(self, ws, close_status_code, close_msg):
        logger.info(f"{self.prefix} SOCKET关闭 {close_status_code} {close_msg}")
        if self.state == 0:
            self.start_ws()

    def start_ws(self):
        raise BrowserEvidenceError(
            "旧版长连接入口已停用；请调用 TiktokWebAPI.send_im_message，"
            "由当前浏览器 wid、header map、ticket-guard 和 command-100 帧建立一次性连接"
        )

    def build_webcast_msg(self, user_url):
        res = self.tiktok_api.get_user_live_info(user_url, self.cookies_str)
        room_id = res['LiveRoom']['liveRoomUserInfo']['user']['roomId']
        room_owner_id = res['LiveRoom']['liveRoomUserInfo']['user']['id']
        room_owner_sec_id = res['LiveRoom']['liveRoomUserInfo']['user']['secUid']
        room_owner_name = res['LiveRoom']['liveRoomUserInfo']['user']['nickname']
        avatar = res['LiveRoom']['liveRoomUserInfo']['user']['avatarThumb']
        cover_url = res['LiveRoom']['liveRoomUserInfo']['liveRoom']['coverUrl']
        msg = {
            "room_id": room_id,
            "room_owner_id": room_owner_id,
            "room_owner_sec_id": room_owner_sec_id,
            "room_owner_name": room_owner_name,
            "room_owner_avatar": {
                "uri": "",
                "url_list": [
                    avatar
                ]
            },
            "cover_url": {
                "uri": "",
                "url_list": [
                    cover_url
                ]
            },
            "push_detail": room_owner_name
        }
        msg = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
        return msg

    def send_message(self, toid, message):
        raise BrowserEvidenceError(
            "请调用 TiktokWebAPI.send_im_message，显式传入浏览器 conversation_short_id；"
            "旧接口没有该字段，拒绝猜测或补齐"
        )

    def send_live_message(self, toid, user_url):
        raise BrowserEvidenceError(
            "直播分享写请求尚未完成 TikTok Chrome 抓包与纯计算迁移，已 fail-closed；不会发送旧版静态 X-Bogus"
        )

if __name__ == "__main__":
    cookies_str = r''
    tiktokSendMsgWs = TiktokSendMsgWs(cookies_str)


    webcast_user_url = [
        'https://www.tiktok.com/@miumiu98261',
        'https://www.tiktok.com/@miumiu072406',
        'https://www.tiktok.com/@xy00108',
    ]
    receive_user_url = [
        'https://www.tiktok.com/@anho88888',
        'https://www.tiktok.com/@happydd49',
        'https://www.tiktok.com/@dream.chaser2955',
        'https://www.tiktok.com/@sun.415737'
    ]


    threading.Thread(target=tiktokSendMsgWs.start_ws).start()

    # https://www.tiktok.com/@kingdomroladno
    while True:
        user_url = input("Enter user_url:")
        if user_url.strip() == "":
            user_url = 'https://www.tiktok.com/@anho88888'
        message = input("Enter message: ")
        user_info = tiktokSendMsgWs.tiktok_api.get_user_info(user_url, cookies_str)
        toid = user_info['userInfo']['user']['id']
        tiktokSendMsgWs.send_message(toid, message)
        # tiktokSendMsgWs.send_live_message(toid, message)
        logger.info(f"正在发送消息给用户 {user_url} ")

