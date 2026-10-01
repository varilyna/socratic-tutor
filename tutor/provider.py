
import json
import os
import ssl
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from tutor.schemas import for_prompt


class ModelError(RuntimeError):
    pass


def load_env(path: str = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("GIGACHAT_"):
                os.environ.setdefault(key, value.strip().strip("\"'"))


class GigaChatClient:
    def __init__(self, auth_key: str, model: str = "GigaChat", *,
                 scope: str = "GIGACHAT_API_PERS",
                 base_url: str = "https://api.giga.chat/v1",
                 auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                 ca_bundle: str = ""):
        if not auth_key.strip():
            raise ModelError("Добавьте ключ авторизации GigaChat в локальный .env.")
        for url in (base_url, auth_url):
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ModelError("Адрес API должен быть HTTPS без учетных данных в URL.")
        self.auth_key = auth_key.strip()
        self.model, self.scope = model, scope
        self.base_url, self.auth_url = base_url.rstrip("/"), auth_url
        try:
            self.context = ssl.create_default_context()
            if ca_bundle:
                self.context.load_verify_locations(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            raise ModelError("Не удалось открыть указанный файл CA-сертификатов.") from exc
        self.token = ""
        self.expires_at = 0.0
        self.calls = 0
        self.total_tokens = 0

    def _request(self, url: str, body: bytes, headers: dict) -> dict:
        try:
            request = Request(url, data=body, headers=headers, method="POST")
            with urlopen(request, context=self.context, timeout=40) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                raise ModelError("API вернул неожиданный формат ответа.")
            return data
        except HTTPError as exc:
            messages = {
                400: "API отклонил параметры запроса.",
                401: "Ошибка авторизации. Проверьте ключ и область доступа.",
                402: "API сообщил о необходимости проверить баланс токенов.",
                403: "API отказал в доступе. Проверьте права проекта.",
                404: "API не нашел модель или адрес запроса.",
                422: "API отклонил запрос: проверьте размер текста и модель.",
                429: "Достигнут лимит запросов или токенов. Попробуйте позже.",
            }
            if exc.code == 401:
                self.token, self.expires_at = "", 0
            raise ModelError(messages.get(exc.code, f"Ошибка API: HTTP {exc.code}.")) from exc
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ModelError("Не удалось проверить TLS-сертификат. Укажите официальный CA PEM в GIGACHAT_CA_BUNDLE.") from exc
            raise ModelError("Не удалось подключиться к API. Проверьте сеть и адрес сервиса.") from exc
        except (TimeoutError, OSError, ValueError) as exc:
            raise ModelError("API не ответил вовремя или вернул некорректные данные.") from exc

    def _access_token(self) -> str:
        if self.token and time.time() < self.expires_at - 60:
            return self.token
        data = self._request(self.auth_url, urlencode({"scope": self.scope}).encode(), {
            "Authorization": f"Basic {self.auth_key}",
            "RqUID": str(uuid.uuid4()), "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            token = data["access_token"]
            expiry = float(data["expires_at"]) / 1000
            if not isinstance(token, str) or not token:
                raise ValueError()
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelError("API авторизации не вернул корректный токен.") from exc
        self.token, self.expires_at = token, expiry
        return self.token

    def complete(self, system: str, payload: dict, *, max_tokens: int = 1800) -> str:
        body = {
            "model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        schema = for_prompt(system)
        if schema:
            body["response_format"] = {"type": "json_schema", "schema": schema, "strict": True}
        data = self._request(self.base_url + "/chat/completions",
                             json.dumps(body, ensure_ascii=False).encode("utf-8"), {
                                 "Authorization": f"Bearer {self._access_token()}",
                                 "Content-Type": "application/json", "Accept": "application/json",
                             })
        self.calls += 1
        self.total_tokens += int(data.get("usage", {}).get("total_tokens", 0) or 0)
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("Ответ модели обрезан. Попробуйте сократить материал или число тем.")
            result = choice["message"]["content"]
            if not isinstance(result, str) or not result.strip():
                raise ValueError()
            return result.strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("Модель вернула пустой или некорректный ответ.") from exc
