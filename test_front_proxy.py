"""Real loopback test of Hub authorization and embedded-engine binding."""
import base64
import http.client
import json
import os
from pathlib import Path
import tempfile
import time
import threading
import unittest
import zstandard
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from front_proxy import OPAQUE_HISTORY_NOTE, drop_foreign_opaque_history, make_front
from storage import ProfileStore

ALIAS = "fixture-route/fixture-beta"
TOKEN = "synthetic_local_capability_0123456789"
ACCOUNT = "fixture-route/7b1c22c4-1151-418a-a137-6341ff54fcaf"


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class FrontTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="provider-front-test-")
        self.root = Path(self.temp.name)
        from test_neutral_python_compat import isolated_native_catalog
        isolated_native_catalog(self, self.root)
        self.store = ProfileStore(self.root / "store")
        provider = self.store.save("External", "chat_completions", "https://old.example/v1", "fixture-beta", "synthetic-key")
        profile, _ = self.store.resolve_profile(provider["id"])
        self.store.bind_engine_route(ALIAS, profile["id"], profile["_revision"], ACCOUNT)
        self.provider = provider
        self.engine_home = self.root / "engine"; self.engine_home.mkdir()
        self.config = {"defaultProvider": "openai", "providers": {
            "openai": {"adapter":"openai-responses", "baseUrl":"https://chatgpt.com/backend-api/codex",
                       "authMode":"forward", "codexAccountMode":"direct"},
            "fixture-route": {"adapter":"openai-chat", "baseUrl":"https://old.example/v1",
                              "defaultModel":"fixture-beta", "apiKey":"keychain:"+ACCOUNT}}}
        self.write_config()
        self.seen = []
        self.inner_done_marker = False
        parent = self
        class Inner(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                parent.seen.append({"headers": dict(self.headers), "body": json.loads(body)})
                payload = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                if parent.inner_done_marker: payload += b'data: [DONE]\n\n'
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("x-codex-turn-state", "synthetic-state")
                self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        self.inner = ThreadingHTTPServer(("127.0.0.2", 0), Inner)
        self.front = make_front(self.store, TOKEN, self.inner.server_port, self.engine_home,probe_capability="synthetic-management-probe-token")
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.inner, self.front)]
        for thread in self.threads: thread.start()

    def write_config(self):
        (self.engine_home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        for service in (self.front, self.inner): service.shutdown(); service.server_close()
        for thread in self.threads: thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, token=TOKEN, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=5)
        headers = {"X-Provider-Hub-Token": token, "Authorization": "Bearer synthetic-codex-token",
                   "ChatGPT-Account-ID": "synthetic-account", "Content-Type": "application/json"}
        headers.update(extra_headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse(); result = response.status, dict(response.getheaders()), response.read()
        conn.close(); return result

    def test_native_and_managed_name_collision_does_not_forward_credentials(self):
        self.store.set_route('native-alpha', self.provider['id'])
        status, _, _ = self.request('POST','/v1/responses',{'model':'native-alpha','input':'fixture'})
        self.assertEqual(status,409)
        self.assertEqual(self.seen,[])

    def test_selected_legacy_binding_with_baseline_hash_still_uses_its_connection(self):
        (self.store.root/'baseline-catalog.json').write_text(json.dumps({'models':[{
            'slug':ALIAS,'visibility':'list','comp_hash':'legacy-host-compatibility'}]}),encoding='utf-8')
        (Path(os.environ['CODEX_HOME'])/'config.toml').write_text('model='+json.dumps(ALIAS),encoding='utf-8')
        status, _, _ = self.request('POST','/v1/responses',{'model':ALIAS,'input':'fixture'})
        self.assertEqual(status,200)
        self.assertEqual(self.seen[-1]['body']['model'],ALIAS)

    def test_expected_binding_headers_are_exact_paired_and_local_only(self):
        binding = self.store.get_engine_binding(ALIAS)
        expected = {'X-Provider-Hub-Expected-Revision': binding['_revision'],
                    'X-Provider-Hub-Expected-Account': binding['account'],
                    'X-Provider-Hub-Deadline': repr(time.monotonic() + 10)}
        body = {'model': ALIAS, 'input': 'synthetic fixture'}
        self.assertEqual(self.request('POST', '/v1/responses', body, extra_headers=expected)[0], 200)
        forwarded = {k.lower() for k in self.seen[-1]['headers']}
        self.assertNotIn('x-provider-hub-expected-revision', forwarded)
        self.assertNotIn('x-provider-hub-expected-account', forwarded)
        self.assertNotIn('x-provider-hub-deadline', forwarded)
        count = len(self.seen)
        for headers in [dict(expected, **{'X-Provider-Hub-Expected-Revision': 'stale'}),
                        dict(expected, **{'X-Provider-Hub-Expected-Account': 'other/account'}),
                        {'X-Provider-Hub-Expected-Revision': binding['_revision']},
                        {'X-Provider-Hub-Expected-Account': binding['account'],
                    'X-Provider-Hub-Deadline': repr(time.monotonic() + 10)}]:
            self.assertEqual(self.request('POST', '/v1/responses', body, extra_headers=headers)[0], 409)
        self.assertEqual(len(self.seen), count)
        self.assertEqual(self.request('POST', '/v1/responses', body)[0], 200)
        self.assertEqual(self.request('POST', '/v1/responses',
            {'model': 'native-alpha', 'input': 'fixture'}, extra_headers=expected)[0], 409)

    def test_probe_deadline_invalid_expired_or_delayed_never_reaches_inner(self):
        from unittest.mock import patch
        binding = self.store.get_engine_binding(ALIAS)
        headers = {'X-Provider-Hub-Expected-Revision': binding['_revision'],
                   'X-Provider-Hub-Expected-Account': binding['account']}
        body = {'model': ALIAS, 'input': 'synthetic fixture'}
        for value in ('nan', 'inf', '-inf', '-1', '0', 'bad', ''):
            self.assertEqual(self.request('POST', '/v1/responses', body,
                extra_headers=dict(headers, **{'X-Provider-Hub-Deadline': value}))[0], 400)
        self.assertEqual(self.request('POST', '/v1/responses', body,
            extra_headers=dict(headers, **{'X-Provider-Hub-Deadline': repr(time.monotonic() - 1)}))[0], 408)
        deadline = time.monotonic() + 0.2
        def delayed(request, **kwargs):
            threading.Event().wait(max(0, deadline - time.monotonic()) + 0.05)
            return request, False
        with patch('front_proxy.drop_foreign_opaque_history', side_effect=delayed):
            self.assertEqual(self.request('POST', '/v1/responses', body,
                extra_headers=dict(headers, **{'X-Provider-Hub-Deadline': repr(deadline)}))[0], 408)
        self.assertEqual(self.seen, [])

    def test_probe_inner_timeout_uses_remaining_deadline(self):
        from unittest.mock import patch
        binding = self.store.get_engine_binding(ALIAS)
        deadline = time.monotonic() + 2
        headers = {'X-Provider-Hub-Expected-Revision': binding['_revision'],
                   'X-Provider-Hub-Expected-Account': binding['account'],
                   'X-Provider-Hub-Deadline': repr(deadline)}
        connection, timeouts = http.client.HTTPConnection, []
        def connect(host, *args, **kwargs):
            conn = connection(host, *args, **kwargs)
            if host == '127.0.0.2':
                request = conn.request
                def observe(*a, **kw):
                    timeouts.append((conn.timeout, deadline - time.monotonic()))
                    return request(*a, **kw)
                conn.request = observe
            return conn
        with patch('front_proxy.http.client.HTTPConnection', side_effect=connect):
            self.assertEqual(self.request('POST', '/v1/responses',
                {'model': ALIAS, 'input': 'fixture'}, extra_headers=headers)[0], 200)
        self.assertEqual(len(timeouts), 1)
        timeout, remaining = timeouts[0]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, remaining + 0.02)
        self.assertLessEqual(timeout, 2)

    def test_probe_proxy_rejects_binding_changed_between_local_check_and_send(self):
        import capability_probes as probes
        from unittest.mock import patch
        binding = self.store.get_engine_binding(ALIAS)
        original = self.store.get_engine_binding
        sent = []
        real_open = probes.build_opener(probes.ProxyHandler({}), probes._NoRedirect()).open
        new_account = 'hub-test/' + ACCOUNT.split('/', 1)[1]
        def transport(request, timeout):
            if request.get_method() == 'GET':
                return real_open(request, timeout=timeout)
            sent.append(request)
            if len(sent) == 2:
                # Local send() already checked A. Switch to a valid B immediately
                # before the real HTTP request so only proxy preflight can stop it.
                self.config['providers']['hub-test'] = dict(
                    self.config['providers']['fixture-route'], apiKey='keychain:' + new_account)
                self.write_config()
                self.store.bind_engine_route(ALIAS, binding['id'], binding['_revision'], new_account)
            return real_open(request, timeout=timeout)
        # Synthetic inner returns a function call first. Keep actual proxy socket
        # transport and routing; only replace terminal engine frame generation.
        call = {'type': 'function_call', 'name': 'capability_lookup',
                'call_id': 'synthetic-call', 'arguments': '{"key":"sample"}'}
        raw = ('data: ' + json.dumps({'type':'response.completed',
            'response': {'status':'completed', 'output':[call]}}) + '\n\n').encode()
        with patch('ocx_service._state', return_value={'port': self.front.server_port}), \
             patch('service._read_capability', return_value=TOKEN), \
             patch('front_proxy.safe_responses_stream', side_effect=lambda response: iter([raw])):
            result = probes.probe_capability(self.store, ALIAS, 'tool',
                                             expected_binding=binding, transport=transport)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(self.seen), 1)  # B never reaches the synthetic kernel
        self.assertEqual(original(ALIAS)['account'], new_account)
        self.assertEqual(self.request('POST', '/v1/responses',
            {'model': ALIAS, 'input': 'ordinary chat'})[0], 200)
        self.assertEqual(len(self.seen), 2)  # ordinary chat can use B

    def test_each_model_of_one_connection_reaches_the_front_and_disable_blocks_all(self):
        from engine_control import activate_provider, disable_provider
        from copy import deepcopy
        from unittest.mock import patch
        p=self.store.save("Multi","responses","https://fixture.invalid/v1","native-alpha","synthetic-shared-key",
                          models=["native-alpha","native-beta"])
        outer=self
        class Manager:
            home=outer.engine_home
            def snapshot(self): return deepcopy(outer.config)
            def replace_provider(self,name,value):
                outer.config["providers"][name]=deepcopy(value);outer.write_config();return True
            def delete_provider(self,name):
                outer.config["providers"].pop(name,None);outer.write_config();return True
        manager=Manager()
        with patch("engine_control._requires_private_network",return_value=False):
            activated=activate_provider(self.store,p["id"],manager,lambda *args:True)
        self.assertTrue(activated["ok"],activated)
        for alias in activated["aliases"]:
            self.assertEqual(self.request("POST","/v1/responses",{"model":alias,"input":"fixture"})[0],200)
            binding = self.store.get_engine_binding(alias)
            self.assertIn(binding["model"], p["models"])
            self.assertEqual(self.seen[-1]["body"]["model"],
                             binding["account"].split("/", 1)[0] + "/" + binding["model"])
        self.assertTrue(disable_provider(self.store,p["id"],manager)["ok"])
        count=len(self.seen)
        for alias in activated["aliases"]:
            self.assertEqual(self.request("POST","/v1/responses",{"model":alias,"input":"fixture"})[0],503)
        self.assertEqual(len(self.seen),count)

    def test_disabled_official_child_cannot_bypass_through_a_direct_request(self):
        from engine_control import disable_provider
        p=self.store.save("Official","codex_subscription","","native-alpha",
                          models=["native-alpha","native-beta"])
        self.assertTrue(disable_provider(self.store,p["id"],None)["ok"])
        for model in p["models"]:
            self.assertEqual(self.request("POST","/v1/responses",{"model":model,"input":"fixture"})[0],503)
        self.assertEqual(self.seen,[])

    def test_private_management_probe_can_test_disabled_official_without_enabling_it(self):
        from engine_control import disable_provider
        p=self.store.save("Official","codex_subscription","","native-alpha")
        self.assertTrue(disable_provider(self.store,p['id'],None)['ok'])
        body={'model':'native-alpha','input':'Reply exactly PROVIDER_HUB_PROBE_OK','max_output_tokens':256,'store':False,'stream':True}
        for token,expected in [('wrong',503),('synthetic-management-probe-token',200)]:
            conn=http.client.HTTPConnection('127.0.0.1',self.front.server_port,timeout=5)
            conn.request('POST','/v1/responses',json.dumps(body),{'X-Provider-Hub-Token':TOKEN,'X-Provider-Hub-Management-Probe':token,'Content-Type':'application/json'})
            response=conn.getresponse();response.read();self.assertEqual(response.status,expected);conn.close()
        self.assertTrue(self.store.is_disabled(p['id']))
        self.assertNotIn('x-provider-hub-management-probe',{k.lower() for k in self.seen[-1]['headers']})
        self.assertEqual(self.request('POST','/v1/responses',body)[0],503)

    def test_authenticated_forward_preserves_codex_state_and_never_forwards_capability_header(self):
        status, headers, output = self.request("POST", "/v1/responses", {"model": ALIAS, "input": "fixture"})
        self.assertEqual(status, 200); self.assertIn(b"response.completed", output)
        self.assertEqual(headers["x-codex-turn-state"], "synthetic-state")
        forwarded = {k.lower():v for k,v in self.seen[0]["headers"].items()}
        self.assertEqual(forwarded["authorization"], "Bearer synthetic-codex-token")
        self.assertEqual(forwarded["x-opencodex-api-key"], TOKEN)
        self.assertNotIn("x-provider-hub-token", forwarded)
        self.assertEqual(self.request("GET", "/v1/_provider_hub/health", token="wrong")[0], 401)
        health = json.loads(self.request("GET", "/v1/_provider_hub/health")[2])
        self.assertTrue(health["external_route_ready"])
        self.assertIs(health["capability_binding_guard"], True)

    def test_health_reports_any_ready_named_route(self):
        from engine_control import activate_provider
        from copy import deepcopy
        from unittest.mock import patch
        p=self.store.save("Multi","responses","https://fixture.invalid/v1","native-alpha","synthetic-shared-key",
                          models=["native-alpha"])
        outer=self
        class Manager:
            home=outer.engine_home
            def snapshot(self): return deepcopy(outer.config)
            def replace_provider(self,name,value):
                outer.config["providers"][name]=deepcopy(value);outer.write_config();return True
            def delete_provider(self,name):
                outer.config["providers"].pop(name,None);outer.write_config();return True
        manager=Manager()
        with patch("engine_control._requires_private_network",return_value=False):
            activated=activate_provider(self.store,p["id"],manager,lambda *args:True)
        self.assertTrue(activated["ok"],activated)
        health=json.loads(self.request("GET","/v1/_provider_hub/health")[2])
        self.assertTrue(health["external_route_ready"])

    def test_foreign_hosted_tool_items_are_not_replayed(self):
        body={"model":ALIAS,"input":[
            {"type":"message","role":"user","content":[{"type":"input_text","text":"portable"}]},
            {"type":"web_search_call","status":"completed","action":{"type":"search","query":"synthetic"}},
            {"type":"image_generation_call","status":"completed","result":"synthetic-image"},
        ]}
        status, _, output = self.request("POST", "/v1/responses", body)
        self.assertEqual(status, 200)
        forwarded=json.dumps(self.seen[-1]["body"])
        self.assertNotIn("web_search_call", forwarded)
        self.assertNotIn("image_generation_call", forwarded)
        self.assertIn("portable", forwarded)

    def test_completed_stream_with_done_does_not_add_a_failure_event(self):
        self.inner_done_marker=True
        status, _, output=self.request('POST','/v1/responses',{'model':ALIAS,'input':'fixture'})
        self.assertEqual(status,200)
        self.assertIn(b'response.completed',output)
        self.assertIn(b'data: [DONE]',output)
        self.assertNotIn(b'response.failed',output)

    def test_changed_profile_or_engine_cannot_route_using_stale_binding(self):
        self.store.save("External", "chat_completions", "https://new.example/v1", "fixture-beta", "new-key", self.provider["id"])
        self.assertEqual(self.request("POST", "/v1/responses", {"model": ALIAS, "input":"fixture"})[0], 503)
        self.assertEqual(self.seen, [])
        self.assertFalse(json.loads(self.request("GET", "/v1/_provider_hub/health")[2])["external_route_ready"])
        self.assertEqual(self.request("POST", "/v1/responses", {"model":"native-alpha","input":"fixture"})[0], 200)
        self.config["providers"]["openai"]["baseUrl"] = "https://other.example/v1"
        self.write_config()
        self.assertEqual(self.request("POST", "/v1/responses", {"model":"native-alpha","input":"fixture"})[0], 503)

    def test_foreign_encrypted_compaction_never_reaches_a_third_party(self):
        body={"model":ALIAS,"input":[
            {"type":"message","role":"user","content":[{"type":"input_text","text":"portable"}]},
            {"type":"reasoning","summary":[],"encrypted_content":"synthetic-opaque-history"},
            {"type":"message","role":"assistant","content":[{"type":"output_text","text":"kept"}],
             "encrypted_content":"synthetic-opaque-history"},
            {"type":"compaction","encrypted_content":"synthetic-opaque-history"},
            {"type":"function_call","call_id":"call_a","name":"read_file","arguments":"{}",
             "encrypted_function_args":["synthetic-opaque-history"]},
            {"type":"function_call_output","call_id":"call_a","output":"portable result"},
        ]}
        status, _, output = self.request("POST", "/v1/responses", body)
        self.assertEqual(status, 200)
        self.assertNotIn(b"synthetic-opaque-history", output)
        forwarded=json.dumps(self.seen[-1]["body"], ensure_ascii=False)
        self.assertNotIn("synthetic-opaque-history", forwarded)
        self.assertIn("portable", forwarded)
        self.assertIn("portable result", forwarded)
        self.assertIn("无法读取", forwarded)
        self.assertNotIn("当前聊天标识", forwarded)
        self.assertNotIn("rollout_path", forwarded)
        self.assertEqual(self.request("POST", "/v1/responses", {"model":"fixture-beta","input":"fixture"})[0], 503)

    def test_local_compaction_summaries_and_new_compaction_trigger_reach_engine(self):
        v1 = "ocx1:" + base64.b64encode("earlier checkpoint".encode()).decode()
        v2 = "ocx2:" + base64.b64encode(json.dumps({"version": 2, "summary": "earlier decisions",
                                                      "reasoning": ["retained context"]}).encode()).decode()
        items = [{"type": "compaction", "encrypted_content": v1},
                 {"type": "compaction_summary", "encrypted_content": v2},
                 {"type": "context_compaction"}, {"type": "compaction_trigger"}]
        status, _, _ = self.request("POST", "/v1/responses", {"model": ALIAS, "input": items})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[-1]["body"]["input"], items)

    def test_foreign_reasoning_can_keep_its_plain_summary_without_ciphertext(self):
        item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "portable summary"}],
                "encrypted_content": "synthetic-opaque"}
        status, _, _ = self.request("POST", "/v1/responses", {"model": ALIAS, "input": [item]})
        self.assertEqual(status, 200)
        forwarded = self.seen[-1]["body"]["input"]
        self.assertEqual(forwarded[0]["summary"][0]["text"], "portable summary")
        self.assertNotIn("encrypted_content", forwarded[0])

    def test_plain_compaction_summary_survives_and_only_opaque_content_is_flagged(self):
        for kind in ("compaction", "compaction_summary", "context_compaction"):
            for opaque in (None, "", "synthetic-opaque"):
                with self.subTest(kind=kind, opaque=bool(opaque)):
                    item = {"type": kind, "summary": [{"type": "summary_text", "text": "KEEP_DECISION"}]}
                    if opaque is not None: item["encrypted_content"] = opaque
                    clean, changed = drop_foreign_opaque_history({"input": [item]})
                    content = json.dumps(clean, ensure_ascii=False)
                    self.assertTrue(changed)
                    self.assertIn("KEEP_DECISION", content)
                    self.assertEqual(OPAQUE_HISTORY_NOTE in content, bool(opaque))
                    self.assertNotIn("synthetic-opaque", content)

    def test_empty_context_compaction_is_only_a_marker(self):
        for value in (None, ""):
            with self.subTest(value=value):
                clean, _ = drop_foreign_opaque_history({"input": [
                    {"type": "context_compaction", "encrypted_content": value}]})
                self.assertNotIn(OPAQUE_HISTORY_NOTE, json.dumps(clean, ensure_ascii=False))

    def test_verified_chat_guides_model_to_find_only_relevant_task(self):
        from unittest.mock import patch
        thread_id = "11111111-1111-4111-8111-111111111111"
        body = {"model": ALIAS, "input": [{"type":"compaction", "encrypted_content":"foreign-cipher"}]}
        with patch("front_proxy.verified_compacted_thread", return_value=thread_id):
            status, _, _ = self.request("POST", "/v1/responses", body)
        self.assertEqual(status, 200)
        text = json.dumps(self.seen[-1]["body"], ensure_ascii=False)
        self.assertIn(thread_id, text)
        self.assertIn("只检索与当前任务有关", text)
        self.assertNotIn("foreign-cipher", text)

    def test_zstd_compressed_codex_request_is_checked_and_forwarded_uncompressed(self):
        body={"model":ALIAS,"input":"compressed-fixture"}
        compressed=zstandard.ZstdCompressor().compress(json.dumps(body).encode())
        conn=http.client.HTTPConnection('127.0.0.1',self.front.server_port,timeout=5)
        conn.request('POST','/v1/responses',compressed,{'X-Provider-Hub-Token':TOKEN,
                     'Content-Type':'application/json','Content-Encoding':'zstd'})
        response=conn.getresponse();self.assertEqual(response.status,200);response.read();conn.close()
        self.assertEqual(self.seen[0]['body'],body)
        self.assertNotIn('content-encoding',{k.lower() for k in self.seen[0]['headers']})


if __name__ == "__main__": unittest.main()
