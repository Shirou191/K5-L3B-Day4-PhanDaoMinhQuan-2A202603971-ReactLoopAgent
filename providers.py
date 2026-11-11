"""Các provider cùng một interface. Không chứa agent loop."""
import json
import os
import time
import urllib.error
import urllib.request

# Retry chỉ cho lỗi hạ tầng tạm thời; có trần số lần và trần tổng thời gian chờ.
RETRY_CODES = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 5
RETRY_BUDGET_SECONDS = 150

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    # Groq dùng endpoint tương thích OpenAI nên chung nhánh Chat Completions.
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


def _retry_delay(exc, attempt):
    """Chờ theo Retry-After nếu provider cung cấp, nếu không thì backoff 2^attempt."""
    header = (exc.headers or {}).get('Retry-After') if hasattr(exc, 'headers') else None
    try:
        return max(1.0, min(float(header), 60.0))
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 32.0)


def post_json(url, headers, payload):
    # Một số provider đứng sau WAF chặn User-Agent mặc định của urllib (HTTP 403).
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'StatusAgentLab/1.0', **headers})
    waited = 0.0
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not print response bodies: they may contain account or request data.
            delay = _retry_delay(exc, attempt)
            # Rate limit hoặc lỗi 5xx tạm thời: chờ rồi thử lại trong budget.
            if exc.code not in RETRY_CODES or attempt == RETRY_ATTEMPTS or waited + delay > RETRY_BUDGET_SECONDS:
                raise RuntimeError(f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.') from exc
            time.sleep(delay)
            waited += delay
        except urllib.error.URLError as exc:
            raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


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
