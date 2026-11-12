"""Các provider cùng một interface. Không chứa agent loop."""
import json
import os
import re
import time
import urllib.error
import urllib.request

# Retry chỉ cho lỗi hạ tầng tạm thời; có trần số lần và trần tổng thời gian chờ.
RETRY_CODES = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 8
# Provider free tier có thể giới hạn token/phút rất chặt (ví dụ Groq: 8000 TPM), nên
# một call có thể phải chờ vài chục giây. Trần tổng thời gian chờ vẫn là trần cứng.
RETRY_BUDGET_SECONDS = 600
# Provider xin chờ lâu hơn mức này thì không còn là blip tạm thời (ví dụ hết quota
# ngày): thử lại chỉ tốn thời gian. Fail ngay và nói rõ quota nào, hơn là nghiền
# RETRY_ATTEMPTS lượt trong 7 phút rồi vẫn fail.
RETRY_MAX_SINGLE_WAIT = 90.0

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    # Groq dùng endpoint tương thích OpenAI nên chung nhánh Chat Completions.
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


# 429 có nhiều nguyên nhân khác nhau (hết token/phút vs hết token/ngày) và cách xử lý
# ngược nhau: một cái chờ là xong, một cái chờ vô ích. Chỉ lấy loại quota + header số.
LIMIT_LABELS = (
    ('tokens per day', 'token/ngày (TPD) - chờ không hết, phải sang ngày mới'),
    ('tokens per minute', 'token/phút (TPM) - chờ là hết'),
    ('requests per day', 'request/ngày (RPD) - chờ không hết'),
    ('requests per minute', 'request/phút (RPM) - chờ là hết'),
)


def _limit_hint(exc):
    """Mô tả quota bị chạm. Không đưa body vào message: body chứa org id, request id."""
    parts = []
    headers = getattr(exc, 'headers', None) or {}
    for name in ('x-ratelimit-limit-tokens', 'x-ratelimit-remaining-tokens',
                 'x-ratelimit-reset-tokens', 'retry-after'):
        value = headers.get(name)
        if value:
            parts.append(f'{name}={value}')
    try:
        message = json.loads(exc.read().decode()).get('error', {}).get('message', '')
    except Exception:
        message = ''
    for needle, label in LIMIT_LABELS:
        if needle in message.lower():
            parts.append(f'quota: {label}')
            break
    return f" ({'; '.join(parts)})" if parts else ''


_DURATION_UNITS = {'ms': 0.001, 's': 1.0, 'm': 60.0, 'h': 3600.0}
# Dưới mức này thì request kế tiếp gần như chắc bị 429, vì một call ở step sau có
# prompt vài nghìn token. Chờ trước rẻ hơn là để 429 ăn mất một lượt thử.
TOKEN_FLOOR = 3000
PACE_MAX_SECONDS = 70.0


def _parse_duration(text):
    """'7.66s', '2m59.56s', '500ms' -> số giây. None nếu không nhận dạng được."""
    if not text:
        return None
    total, found = 0.0, False
    for value, unit in re.findall(r'(\d+(?:\.\d+)?)(ms|s|m|h)', str(text).strip().lower()):
        total += float(value) * _DURATION_UNITS[unit]
        found = True
    return total if found else None


def _retry_after_seconds(exc):
    """Số giây provider yêu cầu chờ, chưa cắt ngọn. None nếu không có header."""
    header = (getattr(exc, 'headers', None) or {}).get('Retry-After')
    try:
        return float(header)
    except (TypeError, ValueError):
        return _parse_duration(header)


def _retry_delay(exc, attempt):
    """Backoff 2^attempt, nới ra nếu provider yêu cầu chờ lâu hơn.

    Phải lấy max chứ không thay thế: Groq trả Retry-After ~2s cho giới hạn
    token/phút, nhưng bucket cần tới ~60s mới hồi. Nếu nghe theo con số nhỏ đó thì
    cả RETRY_ATTEMPTS lần thử cháy hết trong vài giây và case mất trắng.
    """
    backoff = min(2.0 ** attempt, 32.0)
    asked = _retry_after_seconds(exc)
    return max(backoff, min(asked, 60.0)) if asked else backoff


def _pace(headers):
    """Chờ chủ động khi bucket token của provider gần cạn.

    Không bắt buộc, nhưng không có nó thì một lần eval 22 case biến thành chuỗi 429
    vì nhu cầu token vượt xa quota/phút của free tier.
    """
    try:
        remaining = float(headers.get('x-ratelimit-remaining-tokens'))
    except (AttributeError, TypeError, ValueError):
        return
    if remaining >= TOKEN_FLOOR:
        return
    delay = _parse_duration(headers.get('x-ratelimit-reset-tokens'))
    if delay:
        time.sleep(min(delay + 1.0, PACE_MAX_SECONDS))


def post_json(url, headers, payload):
    # Một số provider đứng sau WAF chặn User-Agent mặc định của urllib (HTTP 403).
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'StatusAgentLab/1.0', **headers})
    waited = 0.0
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data, pace_headers = json.load(response), getattr(response, 'headers', None)
        except urllib.error.HTTPError as exc:
            # Do not print response bodies: they may contain account or request data.
            asked = _retry_after_seconds(exc)
            if asked and asked > RETRY_MAX_SINGLE_WAIT:
                raise RuntimeError(f'Provider yêu cầu chờ {asked:.0f}s, quá dài để thử '
                                   f'lại{_limit_hint(exc)}.') from exc
            delay = _retry_delay(exc, attempt)
            # Rate limit hoặc lỗi 5xx tạm thời: chờ rồi thử lại trong budget.
            if exc.code not in RETRY_CODES or attempt == RETRY_ATTEMPTS or waited + delay > RETRY_BUDGET_SECONDS:
                raise RuntimeError(f'Provider trả HTTP {exc.code} sau {attempt} lần thử, '
                                   f'đã chờ {waited:.0f}s{_limit_hint(exc)}.') from exc
            time.sleep(delay)
            waited += delay
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Lỗi tầng vận chuyển (DNS chập chờn, reset, timeout) cũng là lỗi tạm thời:
            # không retry thì một blip mạng làm mất trắng cả case trong eval.
            delay = min(2.0 ** attempt, 32.0)
            if attempt == RETRY_ATTEMPTS or waited + delay > RETRY_BUDGET_SECONDS:
                reason = getattr(exc, 'reason', exc)
                raise RuntimeError(f'Không kết nối được provider: {reason}') from exc
            time.sleep(delay)
            waited += delay
        else:
            # Pace sau khi đã đóng response, để không giữ connection trong lúc chờ.
            _pace(pace_headers)
            return data


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                # Anthropic không nhận hai lượt user liền nhau: gộp message user chèn
                # thêm (ví dụ notice của agent) vào lượt user đang mở.
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append({'type': 'text', 'text': m['content']})
                else:
                    native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
