"""智慧树只读请求客户端。实例仅在创建它的任务线程中使用。"""
import hashlib
import json
import re
import urllib.parse
from copy import deepcopy

BASE = 'https://hiexam-server.zhihuishu.com/zhsathome'
LIST_URL = 'https://hike-examstu.zhihuishu.com/zhsathome/homework/findImportantReminderList'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
HEADERS = {'Referer': 'https://hiexam.zhihuishu.com/',
           'Origin': 'https://hiexam.zhihuishu.com'}


class PlatformError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def user_uuid(state):
    for cookie in (state or {}).get('cookies', []):
        if cookie.get('name') == 'CASLOGC':
            try:
                value = json.loads(urllib.parse.unquote(cookie.get('value') or ''))
                return str(value.get('uuid') or '')
            except (ValueError, TypeError):
                pass
    return ''


def account_key(state):
    uuid = user_uuid(state)
    return hashlib.sha256(('zhs:' + uuid).encode()).hexdigest()[:24] if uuid else ''


def saved_state():
    import store
    return store.load_state('zhs') or {}


def normalize_storage_state(state):
    """兼容旧版保活写入的空sameSite、expires，不修改磁盘上的凭证。"""
    value = deepcopy(state)
    for cookie in value.get('cookies', []):
        same_site = cookie.get('sameSite')
        if same_site not in ('Strict', 'Lax', 'None'):
            cookie['sameSite'] = 'Lax'
        expiry = cookie.get('expires')
        if not isinstance(expiry, (int, float)) or isinstance(expiry, bool):
            cookie['expires'] = -1
        cookie['path'] = cookie.get('path') or '/'
        for field in ('httpOnly', 'secure'):
            if cookie.get(field) is None:
                cookie[field] = False
    return value


class ZhsClient:
    def __init__(self, state=None, context=None):
        self.state = normalize_storage_state(saved_state() if state is None else state)
        self.uuid = user_uuid(self.state)
        self.account_key = account_key(self.state)
        if not self.uuid:
            raise PlatformError('need_login', '请先连接智慧树账号')
        self.context = context
        self._playwright = None
        self._owned = False
        self._cache = {}

    def __enter__(self):
        if self.context is None:
            import store
            store.prepare_browser()
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            try:
                self.context = self._playwright.request.new_context(
                    storage_state=self.state, user_agent=UA,
                    extra_http_headers=HEADERS)
                self._owned = True
            except Exception:
                self._playwright.stop()
                raise
        return self

    def __exit__(self, *args):
        try:
            if self._owned:
                self.context.dispose()
        finally:
            if self._playwright:
                self._playwright.stop()

    def get(self, path, params=None, timeout=25000):
        url = path if path.startswith('https://') else BASE + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        if url in self._cache:
            cached = self._cache[url]
            if isinstance(cached, Exception):
                raise cached
            return cached
        transport = getattr(self.context, 'request', self.context)
        for attempt in range(2):
            try:
                response = transport.fetch(url, headers=HEADERS, timeout=timeout)
                status = response.status
                if status == 401:
                    raise PlatformError('need_login', '智慧树登录已失效，请重新扫码')
                if status in (408, 429) or status >= 500:
                    if attempt == 0:
                        continue
                    raise PlatformError('network', '智慧树暂时不可用（HTTP %s）' % status)
                if status >= 400:
                    raise PlatformError('upstream', '智慧树拒绝请求（HTTP %s）' % status)
                try:
                    payload = json.loads(response.text())
                except (ValueError, TypeError):
                    raise PlatformError('parse', '平台响应不是有效JSON')
                if not isinstance(payload, dict):
                    raise PlatformError('parse', '平台响应结构异常')
                code = str(payload.get('status', ''))
                message = str(payload.get('msg') or payload.get('message') or '')
                if code != '200':
                    if re.search('未登录|登录.*(失效|过期)|重新登录|认证.*失效', message):
                        raise PlatformError('need_login', '智慧树登录已失效，请重新扫码')
                    if re.search('未公布|尚未公布|没有.*查看权限|无权查看|无权限', message):
                        raise PlatformError('closed', '平台尚未公布或未开放查看权限')
                    raise PlatformError('upstream', '平台业务响应异常（%s）' % (code or '缺少状态'))
                self._cache[url] = payload
                return payload
            except PlatformError as error:
                self._cache[url] = error
                raise
            except Exception:
                if attempt == 0:
                    continue
                error = PlatformError('network', '请求超时或连接失败，已重试一次')
                self._cache[url] = error
                raise error

    def paper(self, homework_id):
        return self.get('/homeworkUserPaper/getDetailList',
                        {'homeworkId': homework_id, 'uuid': self.uuid})
