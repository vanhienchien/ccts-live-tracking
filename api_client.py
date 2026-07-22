import asyncio
import base64
import hashlib
import json
import requests
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization


class CCTSClient:
    """Client đăng nhập & gọi API hệ thống CCTS (Pure API Version - Không Playwright)."""

    def __init__(self, username="esmanager", password="Ccts123.", base_url="https://cloud.cnpowercore.com:8091"):
        self.username = username
        self.password = password
        self.base_url = base_url
        self.session = requests.Session()
        self.token = None
        self.ssoticket = None
        
        self.base_headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US',
            'content-type': 'application/json;charset=UTF-8',
            'origin': 'https://console.cnpowercore.com',
            'priority': 'u=1, i',
            'referer': 'https://console.cnpowercore.com/',
            'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
        }
        self.session.headers.update(self.base_headers)

    def _clean_and_load_public_key(self, pub_key_raw: str):
        """Làm sạch Public Key bị obfuscate 'power'."""
        clean_key = pub_key_raw.strip().replace("\r", "").replace("\n", "").replace("power", "").replace("POWER", "")
        clean_key = "".join(c for c in clean_key if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
        
        missing = len(clean_key) % 4
        if missing:
            clean_key += "=" * (4 - missing)
        
        pem_key = f"-----BEGIN PUBLIC KEY-----\n{clean_key}\n-----END PUBLIC KEY-----"
        
        try:
            return serialization.load_pem_public_key(pem_key.encode('utf-8'))
        except Exception as e:
            print(f"[-] Load Public Key error: {e}")
            raise

    def _encrypt_password(self, pub_key_raw: str, plaintext_password: str) -> str:
        """Mã hóa theo đúng logic frontend: MD5 → RSA PKCS1v15"""
        # Bước 1: MD5
        md5_hash = hashlib.md5(plaintext_password.encode('utf-8')).hexdigest()
        
        # Bước 2: RSA
        public_key = self._clean_and_load_public_key(pub_key_raw)
        encrypted_bytes = public_key.encrypt(
            md5_hash.encode('utf-8'),
            padding.PKCS1v15()
        )
        return base64.b64encode(encrypted_bytes).decode('utf-8')

    async def login(self):
        """Đăng nhập thuần API."""
        print("[+] Đang lấy Public Key...")
        
        def _fetch_key():
            return self.session.get(f"{self.base_url}/authen/index/getPublicKey")
        
        res_key = await asyncio.to_thread(_fetch_key)
        key_data = res_key.json()

        if str(key_data.get("code")) != "200":
            raise Exception(f"[-] Không lấy được Public Key: {key_data}")
            
        pub_key_raw = key_data.get("data")
        print("[+] Đang mã hóa mật khẩu (MD5 + RSA)...")
        
        encrypted_pw = self._encrypt_password(pub_key_raw, self.password)

        payload = {
            "account": self.username,
            "password": encrypted_pw
        }
        
        def _post_login():
            return self.session.post(f"{self.base_url}/authen/login/validate", json=payload)
        
        print("[+] Gọi API login...")
        res_login = await asyncio.to_thread(_post_login)
        login_data = res_login.json()

        if str(login_data.get("code")) != "200" or not login_data.get("success"):
            raise Exception(f"[-] Đăng nhập thất bại: {login_data}")

        self.token = login_data.get("token") or login_data.get("data", {}).get("token")
        self.ssoticket = self.session.cookies.get('ssoticket')

        if not self.token:
            raise Exception("[-] Không tìm thấy token trong response.")
            
        print(f"[✓] Đăng nhập thành công! Token: {self.token[:30]}...")

    async def _post(self, endpoint, payload=None):
        """POST helper - Tương thích hoàn toàn với ccts_data.py"""
        if payload is None:
            payload = {}
        
        # Đảm bảo có token
        if isinstance(payload, dict):
            payload = dict(payload)  # copy
            if "token" not in payload and self.token:
                payload["token"] = self.token

        url = f"{self.base_url}{endpoint}"

        def _execute():
            return self.session.post(url, json=payload, headers=self.base_headers)

        res = await asyncio.to_thread(_execute)
        
        try:
            res_data = res.json()
        except:
            res_data = {"code": "500", "message": "Invalid JSON", "success": False}

        # Tự động re-login nếu token hết hạn
        if res_data.get("code") in ["401", "403", "50001"] or not res_data.get("success", True):
            if "token" in str(res_data.get("message", "")).lower() or str(res_data.get("code")) in ["401", "50001"]:
                print("[!] Token hết hạn. Đang re-login...")
                await self.login()
                # Thử lại lần nữa
                if isinstance(payload, dict):
                    payload["token"] = self.token
                res = await asyncio.to_thread(_execute)
                res_data = res.json()

        return res_data