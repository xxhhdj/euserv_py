#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EUserv 自动续期脚本 - 多账号多线程版本
支持多账号配置、多线程并发处理、自动登录、验证码识别、检查到期状态、自动续期并发送 Telegram 通知
"""

import os

import sys
import io
import re
import json
import time
import threading
import logging
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image
import ddddocr
import requests
from bs4 import BeautifulSoup
from imap_tools import MailBox, AND
from urllib.parse import quote

# from dotenv import load_dotenv
# load_dotenv('dev.env')  # 本地配置

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 兼容新版 Pillow
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

# 全局 OCR 实例（线程安全）
ocr = ddddocr.DdddOcr(beta=True)
ocr_lock = threading.Lock()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"


# ============== 配置数据类 ==============
class AccountConfig:
    """单个账号配置"""
    def __init__(self, email, password, imap_server='imap.gmail.com', email_password=''):
        self.email = email
        self.password = password
        self.imap_server = imap_server
        self.email_password = email_password if email_password else password


class GlobalConfig:
    """全局配置"""
    def __init__(self, telegram_bot_token="", telegram_chat_id="", bark_url="", max_workers=3, max_login_retries=3):
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.bark_url = bark_url  # 新增：Bark 推送 URL
        self.max_workers = max_workers
        self.max_login_retries = max_login_retries


# ============== 配置区 ==============
# 全局配置
GLOBAL_CONFIG = GlobalConfig(
    telegram_bot_token=os.getenv("TG_BOT_TOKEN"), # tg的api token
    telegram_chat_id=os.getenv("TG_CHAT_ID"), # tg的userid
    bark_url=os.getenv("BARK_URL"),  #ios系统bark推送,基础格式：https://api.day.app/your_key/，或自建服务器：https://your-bark-server.com/your_key/
    max_workers=3,
    max_login_retries=5
)


# 账号列表配置
ACCOUNTS = [
    AccountConfig(
        imap_server=os.getenv("IMAP_SERV", "imap.gmail.com"),
        password=os.getenv("EUSERV_PASSWORD"),
        imap_server=os.getenv("IMAP_SERV"),
        email_password=os.getenv("EMAIL_PASS")  # Gmail 应用专用密码
    ),
    # 添加更多账号示例：
    AccountConfig(
        email=os.getenv("EUSERV_EMAIL2"),
        password=os.getenv("EUSERV_PASSWORD2"),
        imap_server=os.getenv("IMAP_SERV2"),
        email_password=os.getenv("EMAIL_PASS2")  # Gmail 应用专用密码
    ),
    AccountConfig(
        email=os.getenv("EUSERV_EMAIL3"),
        password=os.getenv("EUSERV_PASSWORD3"),
        imap_server=os.getenv("IMAP_SERV3"),
        email_password=os.getenv("EMAIL_PASS3")  # Gmail 应用专用密码
    ),
    AccountConfig(
        email=os.getenv("EUSERV_EMAIL4"),
        password=os.getenv("EUSERV_PASSWORD4"),
        imap_server=os.getenv("IMAP_SERV4"),
        email_password=os.getenv("EMAIL_PASS4")  # Gmail 应用专用密码
    ),
    AccountConfig(
        email=os.getenv("EUSERV_EMAIL5"),
        password=os.getenv("EUSERV_PASSWORD5"),
        imap_server=os.getenv("IMAP_SERV5"),
        email_password=os.getenv("EMAIL_PASS5")  # Gmail 应用专用密码
    ),
]

# ====================================


def recognize_and_calculate(captcha_image_url: str, session: requests.Session) -> Optional[str]:
    """识别并计算验证码（线程安全）"""
    
    # 数字字符纠正映射表（用于操作数）
    DIGIT_CORRECTIONS = {
        'O': '0', 'o': '0',  # 字母O → 数字0
        'D': '0', 'Q': '0',  # D/Q可能是0
        'I': '1', 'i': '1', 'l': '1', '|': '1',  # I/l/竖线 → 数字1
        'Z': '2', 'z': '2',  # 字母Z → 数字2
        'S': '5', 's': '5',  # 字母S → 数字5
        'G': '6', 'b': '6',  # 字母G → 数字6
        'B': '8', 'g': '8',  # 字母B → 数字8
    }
    
    # 运算符映射表（用于中间位置）
    OPERATOR_CORRECTIONS = {
        'T': '+', 't': '+', 'F': '+', 'f': '+', 'r': '+', # T → 加号
        'I': '-', 'i': '-', '|': '-', '1': '-', 'l': '-',  # 竖线类 → 减号
        'x': '×', 'X': '×',  # x/X → 乘号
        '*': '×', '×': '×',  # 统一乘号
        '÷': '/', ':': '/',  # 统一除号
        '+': '+', '-': '-', '/': '/',  # 保留原有运算符
    }
    
    def aggressive_digit_convert(text: str) -> str:
        """激进的数字转换：尽可能把所有字符转为数字"""
        result = []
        for char in text:
            if char.isdigit():
                result.append(char)
            elif char in DIGIT_CORRECTIONS:
                result.append(DIGIT_CORRECTIONS[char])
            elif char.upper() in DIGIT_CORRECTIONS:
                result.append(DIGIT_CORRECTIONS[char.upper()])
            else:
                # 字母无法转换，保留原样
                result.append(char)
        return ''.join(result)
    
    logger.info("正在处理验证码...")
    try:
        logger.debug("尝试自动识别验证码...")
        response = session.get(captcha_image_url)
        img = Image.open(io.BytesIO(response.content)).convert('RGB')
        
        # 颜色过滤（保留橙色文字，噪点变白）
        pixels = img.load()
        width, height = img.size
        for x in range(width):
            for y in range(height):
                r, g, b = pixels[x, y]
                if not (r > 200 and 100 < g < 220 and b < 80):
                    pixels[x, y] = (255, 255, 255)
        
        # 转灰度 + 二值化
        img = img.convert('L')
        threshold = 200
        img = img.point(lambda x: 0 if x < threshold else 255, '1')
        
        # 去边框
        border = 10
        pixels = img.load()
        for x in range(width):
            for y in range(height):
                if x < border or x >= width - border or y < border or y >= height - border:
                    pixels[x, y] = 255
        
        output = io.BytesIO()
        img.save(output, format='PNG')
        processed_bytes = output.getvalue()
        
        # OCR 识别（加锁保证线程安全）
        with ocr_lock:
            text = ocr.classification(processed_bytes, png_fix=True).strip()
        
        logger.debug(f"OCR 原始识别: {text}")

        # 预处理：去除空格
        raw_text = text.strip().replace(' ', '')
        text_len = len(raw_text)
        
        logger.info(f"验证码长度: {text_len}, 内容: {raw_text}")
        
        # ===== 情况1：长度 >= 6，按纯字母数字验证码处理 =====
        if text_len >= 6:
            logger.info(f"检测到 >= 6 位验证码，按纯字母数字处理: {raw_text}")
            return raw_text.upper()  # 统一大写返回
        
        # ===== 情况2：长度 < 6，按运算验证码处理 =====
        logger.info(f"检测到 < 6 位验证码，按运算验证码处理: {raw_text}")
        
        # 尝试多种解析策略
        # 策略1：标准3位格式 (数字 运算符 数字)
        if text_len == 3:
            left_char, mid_char, right_char = raw_text[0], raw_text[1], raw_text[2]
            
            # 左右转数字，中间转运算符
            left_corrected = DIGIT_CORRECTIONS.get(left_char, left_char)
            right_corrected = DIGIT_CORRECTIONS.get(right_char, right_char)
            op_char = OPERATOR_CORRECTIONS.get(mid_char, mid_char)
            
            logger.debug(f"3位纠正: '{left_char}'→'{left_corrected}' '{mid_char}'→'{op_char}' '{right_char}'→'{right_corrected}'")
            
            if left_corrected.isdigit() and right_corrected.isdigit():
                result = calculate_operation(int(left_corrected), op_char, int(right_corrected), raw_text)
                if result is not None:
                    return result
        
        # 策略2：正则匹配运算表达式（支持多位数）
        # 先进行字符纠正
        corrected_text = raw_text
        for old, new in DIGIT_CORRECTIONS.items():
            corrected_text = corrected_text.replace(old, new)
        
        # 匹配模式：数字 + 运算符 + 数字
        pattern = r'^(\d+)([+\-×*/÷:xX])(\d+)$'
        match = re.match(pattern, corrected_text)
        
        if match:
            left_str, op, right_str = match.groups()
            op = OPERATOR_CORRECTIONS.get(op, op)  # 运算符纠正
            
            left = int(left_str)
            right = int(right_str)
            
            logger.debug(f"正则匹配成功: {left} {op} {right}")
            result = calculate_operation(left, op, right, raw_text)
            if result is not None:
                return result
        
        # 策略3：激进纠正 - 强制把所有非数字转为数字，再尝试解析
        logger.warning(f"常规解析失败，尝试激进纠正...")
        aggressive_text = aggressive_digit_convert(raw_text)
        logger.debug(f"激进纠正结果: {raw_text} → {aggressive_text}")
        
        # 如果纠正后全是数字，尝试按位置推断运算符
        if aggressive_text.isdigit() and len(aggressive_text) >= 3:
            # 假设：倒数第二位可能是被误识别的运算符
            # 例如："253" 可能是 "2+3"（中间的5被误识别）
            if len(aggressive_text) == 3:
                left = int(aggressive_text[0])
                right = int(aggressive_text[2])
                # 尝试常见运算符
                for op in ['+', '-', '×', '/']:
                    result = calculate_operation(left, op, right, raw_text, silent=True)
                    if result is not None and 0 <= int(result) <= 20:  # 结果在合理范围
                        logger.info(f"激进推断成功: {left} {op} {right} = {result}")
                        return result
        
        # 策略4：如果还有字母，再次尝试强制转换
        if not aggressive_text.isdigit():
            logger.warning(f"包含无法转换的字符: {aggressive_text}")
            # 最后尝试：移除所有非数字非运算符字符
            cleaned = re.sub(r'[^0-9+\-×*/÷]', '', corrected_text)
            match = re.match(r'^(\d+)([+\-×*/÷])(\d+)$', cleaned)
            if match:
                left_str, op, right_str = match.groups()
                result = calculate_operation(int(left_str), op, int(right_str), raw_text)
                if result is not None:
                    logger.info(f"清理后解析成功: {cleaned}")
                    return result
        
        # 所有策略都失败，返回原始文本
        logger.warning(f"所有解析策略均失败，返回原始文本: {raw_text}")
        return raw_text
        
    except Exception as e:
        logger.error(f"验证码识别发生错误: {e}", exc_info=True)
        return None


def calculate_operation(left: int, op: str, right: int, raw_text: str, silent: bool = False) -> Optional[str]:
    """
    执行运算并返回结果
    silent: 是否静默模式（不输出日志，用于批量尝试）
    """
    try:
        if op == '+':
            result = left + right
            op_name = '加'
        elif op == '-':
            result = left - right
            op_name = '减'
        elif op in {'×', '*', 'x', 'X'}:
            result = left * right
            op_name = '乘'
        elif op in {'/', '÷', ':'}:
            if right == 0:
                if not silent:
                    logger.warning("除数为0，无法计算")
                return None
            if left % right != 0:
                if not silent:
                    logger.warning(f"除法非整除: {left} ÷ {right} = {left / right}")
                return None
            result = left // right
            op_name = '除'
        else:
            if not silent:
                logger.warning(f"未知运算符: {op}")
            return None
        
        if not silent:
            logger.info(f"验证码计算: {left} {op_name} {right} = {result}")
        return str(result)
    except Exception as e:
        if not silent:
            logger.error(f"计算错误: {e}")
        return None







def get_euserv_pin(email: str, email_password: str, imap_server: str) -> Optional[str]:
    """从邮箱获取 EUserv PIN 码"""
    try:
        logger.info(f"正在从邮箱 {email} 获取 PIN 码...")
        with MailBox(imap_server).login(email, email_password) as mailbox:
            for msg in mailbox.fetch(AND(from_='no-reply@euserv.com', body='PIN'), limit=1, reverse=True):
                logger.debug(f"找到邮件: {msg.subject}, 收件时间: {msg.date_str}")
                
                match = re.search(r'PIN:\s*\n?(\d{6})', msg.text)
                if match:
                    pin = match.group(1)
                    logger.info(f"✅ 提取到 PIN 码: {pin}")
                    return pin
                else:
                    match_fallback = re.search(r'(\d{6})', msg.text)
                    if match_fallback:
                        pin = match_fallback.group(1)
                        logger.warning(f"⚠️ 备选匹配 PIN 码: {pin}")
                        return pin
                    
            logger.warning("❌ 未找到符合条件的 EUserv 邮件")
            return None

    except Exception as e:
        logger.error(f"获取 PIN 码时发生错误: {e}", exc_info=True)
        return None


class EUserv:
    """EUserv 操作类"""
    
    def __init__(self, config: AccountConfig):
        self.config = config
        self.session = requests.Session()
        self.sess_id = None
        self.c_id = None
        
    def login(self) -> bool:
        """登录 EUserv（支持验证码和 PIN）"""
        logger.info(f"正在登录账号: {self.config.email}")
        
        headers = {
            'user-agent': USER_AGENT,
            'origin': 'https://www.euserv.com'
        }
        url = "https://support.euserv.com/index.iphp"
        captcha_url = "https://support.euserv.com/securimage_show.php"
        
        try:
            # 获取 sess_id
            sess = self.session.get(url, headers=headers)
            sess_id_match = re.search(r'sess_id["\']?\s*[:=]\s*["\']?([a-zA-Z0-9]{30,100})["\']?', sess.text)
            if not sess_id_match:
                sess_id_match = re.search(r'sess_id=([a-zA-Z0-9]{30,100})', sess.text)
            
            if not sess_id_match:
                logger.error("❌ 无法获取 sess_id")
                return False
            
            sess_id = sess_id_match.group(1)
            logger.debug(f"获取到 sess_id: {sess_id[:20]}...")
            
            # 访问 logo
            logo_png_url = "https://support.euserv.com/pic/logo_small.png"
            self.session.get(logo_png_url, headers=headers)
            
            # 提交登录表单
            login_data = {
                'email': self.config.email,
                'password': self.config.password,
                'form_selected_language': 'en',
                'Submit': 'Login',
                'subaction': 'login',
                'sess_id': sess_id
            }
            
            logger.debug("提交登录表单...")
            response = self.session.post(url, headers=headers, data=login_data)
            response.raise_for_status()

            #解析返回页面
            soup = BeautifulSoup(response.text, "html.parser")

            # 检查登录错误
            if 'Please check email address/customer ID and password' in response.text:
                logger.error("❌ 用户名或密码错误")
                return False
            if 'kc2_login_iplock_cdown' in response.text:
                logger.error("❌ 密码错误次数过多，账号被锁定，请5分钟后重试")
                return False
            
            # 处理验证码
            if 'captcha' in response.text.lower():
                logger.info("⚠️ 需要验证码，正在识别...")

                max_captcha_retries = 10  # 验证码最多重试10次
                for captcha_attempt in range(max_captcha_retries):
                    if captcha_attempt > 0:
                        logger.warning(f"验证码识别失败，第 {captcha_attempt + 1}/{max_captcha_retries} 次重试...")
                        time.sleep(3)  # 等待一下再重试

                    # 识别验证码
                    captcha_code = recognize_and_calculate(captcha_url, self.session)
                
                    if not captcha_code:
                        logger.error("❌ 验证码识别失败")
                        return False
                    
                    captcha_data = {
                        'subaction': 'login',
                        'sess_id': sess_id,
                        'captcha_code': captcha_code
                    }
                
                    response = self.session.post(url, headers=headers, data=captcha_data)
                    response.raise_for_status()
                    
                    # 检查验证码是否正确
                    if 'captcha' in response.text.lower():
                        logger.warning(f"❌ 验证码错误（第 {captcha_attempt + 1} 次）")
                        if captcha_attempt < max_captcha_retries - 1:
                            continue  # 继续重试
                        else:
                            logger.error("❌ 验证码错误次数过多，重新进入登录流程")
                            return False
                    else:
                        soup = BeautifulSoup(response.text, "html.parser")
                        logger.info("✅ 验证码验证成功")
                        break  # 验证码正确，跳出循环
            

            # 处理 PIN 验证
            if 'PIN that you receive via email' in response.text:
                self.c_id = soup.find("input", {"name": "c_id"})["value"]
                logger.info("⚠️ 需要 PIN 验证")
                time.sleep(3)  # 等待邮件到达
                
                pin = get_euserv_pin(
                    self.config.email,
                    self.config.email_password,
                    self.config.imap_server
                )
                
                if not pin:
                    logger.error("❌ 获取 PIN 码失败")
                    return False
                
                
                login_confirm_data = {
                    'pin': pin,
                    'sess_id': sess_id,
                    'Submit': 'Confirm',
                    'subaction': 'login',
                    'c_id': self.c_id,
                }
                response = self.session.post(url, headers=headers, data=login_confirm_data)
                response.raise_for_status()


            # 检查登录成功
            success_checks = [
                'Hello' in response.text,
                'Confirm or change your customer data here' in response.text,
                'logout' in response.text.lower() and 'customer' in response.text.lower()
            ]
            
            if any(success_checks):
                logger.info(f"✅ 账号 {self.config.email} 登录成功")
                self.sess_id = sess_id
                return True
            else:
                logger.error(f"❌ 账号 {self.config.email} 登录失败")
                return False
                
        except Exception as e:
            logger.error(f"❌ 登录过程出现异常: {e}", exc_info=True)
            return False
    


    def update_info(self):
        # 判断当前日期是否为2号或22号，一个月更新两次
        current_day = datetime.now().day
        if current_day not in [2, 22]:
            return

        logger.info(f"更新用户信息...")
        try:
            # 更新用户信息，euserv每隔一段时间就需要用户更新信息，每个月2号，22号
            #1.进入用户界面
            url = f"https://support.euserv.com/index.iphp?sess_id={self.sess_id}&action=show_customerdata"
            showinfo_data = {
                'sess_id': self.sess_id,
                'action': 'show_customerdata'
            }
            headers = {'user-agent': USER_AGENT, 
                       'host': 'support.euserv.com',
                       'referer': 'https://support.euserv.com/index.iphp?sess_id={self.sess_id}&subaction=show_kwk_main'
                       }
            
            logger.info(f"进入用户界面...")
            response = self.session.get(url=url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')

            if not self.c_id:
                self.c_id = soup.find("input", {"name": "c_id"})["value"]
            c_att = soup.select_one('#c_att option[selected]').get('value')
            c_street = soup.find('input', {'name': 'c_street'})['value']
            c_streetno = soup.find('input', {'name': 'c_streetno'})['value']
            c_postal = soup.find('input', {'name': 'c_postal'})['value']
            c_city = soup.find('input', {'name': 'c_city'})['value']
            c_country = soup.select_one('#c_country option[selected]').get('value')
            c_phone_country_prefix = soup.find('input', {'name': 'c_phone_country_prefix'})['value']      
            c_phone_password = soup.find('input', {'name': 'c_phone_password'})['value'] 
            c_fax_country_prefix = soup.find('input', {'name': 'c_fax_country_prefix'})['value'] 
            c_tac_date = soup.find('input', {'name': 'c_tac_date'})['value'] 
            c_website = soup.find('input', {'name': 'c_website'})['value'] 
            c_firstcontact = soup.select_one('#c_firstcontact option[selected]').get('value')
            c_emailabo_contract = soup.find('input', {'name': 'c_emailabo_contract'})['value'] 
            c_emailabo_products = soup.find('input', {'name': 'c_emailabo_products'})['value'] 
            c_forumnick = soup.find('input', {'name': 'c_forumnick'})['value'] 
            c_hrno = soup.find('input', {'name': 'c_hrno'})['value'] 
            c_hrcourt = soup.find('input', {'name': 'c_hrcourt'})['value'] 
            c_taxid = soup.find('input', {'name': 'c_taxid'})['value'] 
            c_identifier = soup.find('input', {'name': 'c_identifier'})['value'] 
            c_birthplace = soup.find('input', {'name': 'c_birthplace'})['value'] 
            c_country_of_birth = soup.select_one('#c_country_of_birth option[selected]').get('value')

            c_birthdays = soup.find_all('input', {'name': 'c_birthday[]'})
            c_birthday_value = []
            for c_birthday in c_birthdays:
                if c_birthday:
                    c_birthday_value.append(c_birthday['value'].strip())
                else:
                    c_birthday_value.append('')

            c_phones = soup.find_all('input', {'name': 'c_phone[]'})
            c_phone_value = []
            for c_phone in c_phones:
                if c_phone:
                    c_phone_value.append(c_phone['value'].strip())
                else:
                    c_phone_value.append('')

            c_faxs = soup.find_all('input', {'name': 'c_fax[]'})
            c_fax_value = []
            for c_fax in c_faxs:
                if c_fax:
                    c_fax_value.append(c_fax['value'].strip())
                else:
                    c_fax_value.append('')     

            upInfo_data = {
                'sess_id': self.sess_id,
                'subaction': 'kc2_customer_data_update',
                'c_id': self.c_id,
                'c_org': '',
                'c_ustid[]': ['', ''],
                'c_att': c_att,
                'c_street': c_street,
                'c_streetno': c_streetno,
                'c_postal': c_postal,
                'c_city': c_city,
                'c_country': c_country,
                'c_birthday[]': c_birthday_value,
                'c_phone_country_prefix': c_phone_country_prefix,
                'c_phone[]': c_phone_value,
                'c_phone_password': c_phone_password,
                'c_fax_country_prefix': c_fax_country_prefix,
                'c_fax[]': c_fax_value,
                'c_tac_date': c_tac_date,
                'c_website': c_website,
                'c_firstcontact': c_firstcontact,
                'c_emailabo_contract': c_emailabo_contract,
                'c_emailabo_products': c_emailabo_products,
                'c_forumnick': c_forumnick,
                'c_hrno': c_hrno,
                'c_hrcourt': c_hrcourt,
                'c_taxid': c_taxid,
                'c_identifier': c_identifier,
                'c_birthplace': c_birthplace,
                'c_country_of_birth': c_country_of_birth
            }

            url = f"https://support.euserv.com/index.iphp"
            logger.info(f"提交保存用户信息...")
            response = self.session.post(url=url, headers=headers, data=upInfo_data)
            response.raise_for_status()

            if 'customer data has been changed' in response.text:
                logger.info(f"保存用户信息成功")
            else:
                logger.info(f"保存用户信息失败，接口返回response={response.text}")

        except Exception as e:
            logger.error(f"❌ 更新用户信息异常: {e}", exc_info=True)
            return False


    def get_servers(self) -> Dict[str, Tuple[bool, str]]:
        """获取服务器列表"""
        logger.info(f"正在获取账号 {self.config.email} 的服务器列表...")
        
        if not self.sess_id:
            logger.error("❌ 未登录")
            return {}
        
        url = f"https://support.euserv.com/index.iphp?sess_id={self.sess_id}"
        headers = {'user-agent': USER_AGENT, 'origin': 'https://www.euserv.com'}
        
        try:
            detail_response = self.session.get(url=url, headers=headers)
            detail_response.raise_for_status()

            soup = BeautifulSoup(detail_response.text, 'html.parser')
            servers = {}

            selector = '#kc2_order_customer_orders_tab_content_1 .kc2_order_table.kc2_content_table tr, #kc2_order_customer_orders_tab_content_2 .kc2_order_table.kc2_content_table tr'
            for tr in soup.select(selector):
                server_id = tr.select('.td-z1-sp1-kc')
                if len(server_id) != 1:
                    continue
                
                action_containers = tr.select('.td-z1-sp2-kc .kc2_order_action_container')
                if not action_containers:
                    continue
                    
                action_text = action_containers[0].get_text()
                logger.debug(f"续期信息: {action_text}")

                can_renew = action_text.find("Contract extension possible from") == -1
                can_renew_date = ""
                
                if not can_renew:
                    date_pattern = r'\b\d{4}-\d{2}-\d{2}\b'
                    match = re.search(date_pattern, action_text)
                    if match:
                        can_renew_date = match.group(0)
                        can_renew = datetime.today().date() >= datetime.strptime(can_renew_date, "%Y-%m-%d").date()

                server_id_text = server_id[0].get_text().strip()
                servers[server_id_text] = (can_renew, can_renew_date)
            
            logger.info(f"✅ 账号 {self.config.email} 找到 {len(servers)} 台服务器")
            return servers
            
        except Exception as e:
            logger.error(f"❌ 获取服务器列表失败: {e}", exc_info=True)
            return {}
    
    def renew_server(self, order_id: str) -> bool:
        """续期服务器"""
        logger.info(f"正在续期服务器 {order_id}...")
        
        url = "https://support.euserv.com/index.iphp"
        headers = {
            'user-agent': USER_AGENT,
            'Host': 'support.euserv.com',
            'origin': 'https://support.euserv.com',
            'Referer': 'https://support.euserv.com/index.iphp'
        }
        
        try:
            # 步骤1: 选择订单
            logger.debug("步骤1: 选择订单...")
            data = {
                'Submit': 'Extend contract',
                'sess_id': self.sess_id,
                'ord_no': order_id,
                'subaction': 'choose_order',
                'show_contract_extension': '1',
                'choose_order_subaction': 'show_contract_details'
            }
            resp1 = self.session.post(url, headers=headers, data=data)
            resp1.raise_for_status()
            
            # 步骤2: 触发发送 PIN
            logger.debug("步骤2: 触发发送 PIN...")
            data = {
                'sess_id': self.sess_id,
                'subaction': 'show_kc2_security_password_dialog',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1'
            }
            resp2 = self.session.post(url, headers=headers, data=data)
            resp2.raise_for_status()
            # 检查PIN发送响应
            if resp2.status_code != 200:
                logger.error("❌ PIN发送请求失败")
                return False
            
            # 步骤3: 获取 PIN
            logger.debug("步骤3: 等待并获取 PIN 码...")
            time.sleep(8)
            pin = get_euserv_pin(
                self.config.email,
                self.config.email_password,
                self.config.imap_server
            )
            
            if not pin:
                logger.error(f"❌ 获取续期 PIN 码失败")
                return False
        
            # 步骤4: 验证 PIN 获取 token
            logger.debug("步骤4: 验证 PIN 获取 token...")
            data = {
                'sess_id': self.sess_id,
                'auth': pin,
                'subaction': 'kc2_security_password_get_token',
                'prefix': 'kc2_customer_contract_details_extend_contract_',
                'type': '1',
                'ident': 'kc2_customer_contract_details_extend_contract_' + order_id
            }
            
            resp3 = self.session.post(url, headers=headers, data=data)
            resp3.raise_for_status()

            result = json.loads(resp3.text)
            if result.get('rs') != 'success':
                logger.error(f"❌ 获取 token 失败: {result.get('rs', 'unknown')}")
                if 'error' in result:
                    logger.error(f"错误信息: {result['error']}")
                return False
            
            token = result['token']['value']
            logger.debug(f"✅ 获取到 token: {token[:20]}...")
            time.sleep(2)

            # 步骤4.5: 弹出小窗
            logger.debug("步骤4.5: 确认续期图...")
            data = {
                'sess_id': self.sess_id,
                'subaction': 'kc2_customer_contract_details_get_extend_contract_confirmation_dialog',
                'token': token
            }
            resp4 = self.session.post(url, headers=headers, data=data)
            resp4.raise_for_status()


            # 步骤5: 提交续期请求
            logger.debug("步骤5: 提交续期请求...")
            data = {
                'sess_id': self.sess_id,
                'ord_id': order_id,
                'subaction': 'kc2_customer_contract_details_extend_contract_term',
                'token': token
            }
      
            resp5 = self.session.post(url, headers=headers, data=data)
            resp5.raise_for_status()
            # with open('debug_resp5.html', 'w', encoding='utf-8') as f:
            #     f.write(resp5.text)
            
            logger.info(f"✅ 服务器 {order_id} 续期成功")
            return True
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON 解析失败: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"❌ 服务器 {order_id} 续期失败: {e}", exc_info=True)
            return False




def send_bark(title: str, content: str, config: GlobalConfig):
    """
    发送 Bark 推送通知
    
    Args:
        title: 推送标题
        content: 推送内容
        config: 全局配置对象
    """
    if not config.bark_url:
        logger.warning("⚠️ 未配置 Bark URL，跳过 Bark 通知")
        return
    
    try:
        # 确保 URL 以 / 结尾
        bark_url = config.bark_url.rstrip('/') + '/'
        
        # URL 编码标题和内容
        encoded_title = quote(title)
        encoded_content = quote(content)
        
        post_url = bark_url.rstrip('/')
        data = {
            "title": title,
            "body": content,
            "sound": "telegraph",  # 推送音效
            "group": "EUserv",     # 分组
            "icon": "https://www.euserv.com/favicon.ico"  # 自定义图标
        }
        
        # 发送请求
        response = requests.post(post_url, json=data, timeout=20)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 200:
                logger.info("✅ Bark 推送发送成功")
            else:
                logger.error(f"❌ Bark 推送失败: {result.get('message', '未知错误')}")
        else:
            logger.error(f"❌ Bark 推送失败: HTTP {response.status_code}")
            
    except Exception as e:
        logger.error(f"❌ Bark 推送异常: {e}", exc_info=True)



def send_telegram(message: str, config: GlobalConfig):
    """发送 Telegram 通知"""
    if not config.telegram_bot_token or not config.telegram_chat_id:
        logger.warning("⚠️ 未配置 Telegram，跳过通知")
        return
    
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    data = {
        "chat_id": config.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            logger.info("✅ Telegram 通知发送成功")
        else:
            logger.error(f"❌ Telegram 通知失败: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ Telegram 异常: {e}", exc_info=True)


def send_notification(title: str, message: str, config: GlobalConfig):
    """
    统一发送通知（支持 Telegram 和 Bark）
    
    Args:
        title: 通知标题（主要用于 Bark）
        message: 通知内容
        config: 全局配置对象
    """
    # 发送 Telegram 通知
    send_telegram(message, config)
    
    # 发送 Bark 通知（将 HTML 格式转为纯文本）
    plain_message = re.sub(r'<[^>]+>', '', message)  # 移除 HTML 标签
    send_bark(title, plain_message, config)


def process_account(account_config: AccountConfig, global_config: GlobalConfig) -> Dict:
    """处理单个账号的续期任务"""
    result = {
        'email': account_config.email,
        'success': False,
        'servers': {},
        'renew_results': [],
        'error': None
    }
    
    try:
        euserv = EUserv(account_config)
        
        # 登录（最多重试）
        login_success = False
        for attempt in range(global_config.max_login_retries):
            if attempt > 0:
                logger.info(f"账号 {account_config.email} 第 {attempt + 1} 次登录尝试...")
                time.sleep(5)
            
            if euserv.login():
                login_success = True
                break
        
        if not login_success:
            result['error'] = "登录失败"
            return result
        
        # 更新用户信息
        euserv.update_info()

        # 获取服务器列表
        servers = euserv.get_servers()
        result['servers'] = servers
        
        if not servers:
            result['error'] = "未找到任何服务器"
            result['success'] = True  # 登录成功，只是没有服务器
            return result
        
        # 检查并续期
        for order_id, (can_renew, can_renew_date) in servers.items():
            logger.info(f"检查服务器: {order_id}")
            if can_renew:
                logger.info(f"⏰ 服务器 {order_id} 可以续期")
                if euserv.renew_server(order_id):
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': True,
                        'message': f"✅ 服务器 {order_id} 续期成功"
                    })
                else:
                    result['renew_results'].append({
                        'order_id': order_id,
                        'success': False,
                        'message': f"❌ 服务器 {order_id} 续期失败"
                    })
            else:
                logger.info(f"✓ 服务器 {order_id} 暂不需要续期（可续期日期: {can_renew_date}）")
        
        result['success'] = True
        
    except Exception as e:
        logger.error(f"处理账号 {account_config.email} 时发生异常: {e}", exc_info=True)
        result['error'] = str(e)
    
    return result


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("EUserv 多账号自动续期脚本（多线程版本）")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置账号数: {len(ACCOUNTS)}")
    logger.info(f"最大并发线程: {GLOBAL_CONFIG.max_workers}")
    logger.info("=" * 60)
    
    if not ACCOUNTS:
        logger.error("❌ 未配置任何账号")
        sys.exit(1)
    
    # 使用线程池处理多个账号
    all_results = []
    with ThreadPoolExecutor(max_workers=GLOBAL_CONFIG.max_workers) as executor:
        # 提交所有任务
        future_to_account = {
            executor.submit(process_account, account, GLOBAL_CONFIG): account 
            for account in ACCOUNTS
            if account.email and str(account.email).strip() and account.password and str(account.password).strip() and account.email_password and str(account.email_password).strip()
        }
        
        # 等待任务完成
        for future in as_completed(future_to_account):
            account = future_to_account[future]
            try:
                result = future.result()
                all_results.append(result)
            except Exception as e:
                logger.error(f"处理账号 {account.email} 时发生未预期的异常: {e}", exc_info=True)
                all_results.append({
                    'email': account.email,
                    'success': False,
                    'error': f"未预期的异常: {str(e)}"
                })
    
    # 生成汇总报告
    logger.info("\n" + "=" * 60)
    logger.info("处理结果汇总")
    logger.info("=" * 60)
    
    message_parts = [f"<b>🔄 EUserv 多账号续期报告</b>\n"]
    message_parts.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    message_parts.append(f"处理账号数: {len(all_results)}\n")
    
    for result in all_results:
        email = result['email']
        logger.info(f"\n账号: {email}")
        message_parts.append(f"\n<b>📧 账号: {email}</b>")
        
        if not result['success']:
            error_msg = result.get('error', '未知错误')
            logger.error(f"  ❌ 处理失败: {error_msg}")
            message_parts.append(f"  ❌ 处理失败: {error_msg}")
            continue
        
        servers = result.get('servers', {})
        logger.info(f"  服务器数量: {len(servers)}")
        
        renew_results = result.get('renew_results', [])
        if renew_results:
            logger.info(f"  续期操作: {len(renew_results)} 个")
            for renew_result in renew_results:
                logger.info(f"    {renew_result['message']}")
                message_parts.append(f"  {renew_result['message']}")
        else:
            logger.info("  ✓ 所有服务器均无需续期")
            message_parts.append("  ✓ 所有服务器均无需续期")
            for order_id, (can_renew, can_renew_date) in servers.items():
                if can_renew_date:
                    message_parts.append(f"    订单 {order_id}: 可续期日期 {can_renew_date}")
    
    # 发送 Telegram 通知
    message = "\n".join(message_parts)
    # send_telegram(message, GLOBAL_CONFIG)
    send_notification("EUserv 续期报告", message, GLOBAL_CONFIG)
    
    logger.info("\n" + "=" * 60)
    logger.info("执行完成")
    logger.info("=" * 60)
    os._exit(0)


if __name__ == "__main__":
    main()
