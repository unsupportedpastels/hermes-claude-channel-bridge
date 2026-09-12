"""Catalog and picker compatibility through an actual local HTTP fixture."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_profile_catalog_uses_real_http_auth_and_shared_models():
    from claude_native_bridge.models import MODELS, MODEL_LABELS
    from claude_native_bridge.provider import profile
    from claude_native_bridge.api_config import TOKEN_ENV

    assert MODELS and tuple(MODEL_LABELS) == MODELS
    assert all(label and label != model for model, label in MODEL_LABELS.items())
    assert profile.fallback_models == MODELS
    assert profile.auth_type == "api_key"
    assert TOKEN_ENV in profile.env_vars
    assert profile.base_url.startswith("http://127.0.0.1:")


def test_runtime_accepts_the_same_catalog_the_picker_advertises():
    from claude_native_bridge.models import MODELS
    from claude_native_bridge.native import MODELS as RUNTIME_MODELS, native_argv

    assert RUNTIME_MODELS == MODELS
    for model in MODELS:
        argv = native_argv(
            "claude", "fixture-session", "/tmp/fixture-mcp.json", model, "low"
        )
        assert argv[argv.index("--model") + 1] == model


def test_unmodified_host_http_picker_and_session_selection(tmp_path):
    import hermes_cli.inventory

    root = Path(__file__).resolve().parents[1]
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "claude-native-bridge").symlink_to(root, target_is_directory=True)
    code = textwrap.dedent("""
        import http.server,json,os,sys,threading
        from pathlib import Path
        TOKEN='fixture-local-api-token-000000000000000000'
        MODELS=['claude-sonnet-5','claude-opus-4-8','claude-opus-5','claude-haiku-4-5-20251001','claude-fable-5-1']
        requests=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.headers.get('Authorization')!='Bearer '+TOKEN:
                    self.send_response(401);self.end_headers();return
                data=({'service':'claude-native-bridge'} if self.path=='/health' else
                      {'object':'list','data':[{'id':m,'object':'model'} for m in MODELS]})
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
            def do_POST(self):
                raise AssertionError('Model selection must not submit inference')
            def log_message(self,*args):pass
        fixture=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=fixture.serve_forever,daemon=True).start()
        def no_inference(event,args):
            if event=='subprocess.Popen':raise RuntimeError('No process launches in picker test')
            if event=='socket.connect' and args[1] != ('127.0.0.1',fixture.server_port):
                raise RuntimeError('Only the local model-catalog fixture may be contacted')
        sys.addaudithook(no_inference)
        home=Path(os.environ['HERMES_HOME']);cfg=home/'config.yaml'
        cfg.write_text(json.dumps({'plugins':{'enabled':['claude-native-bridge']},
            'model':{'provider':'','default':''},'claude_native_bridge_api':{'port':fixture.server_port}}))
        os.environ['CLAUDE_NATIVE_BRIDGE_API_KEY']=TOKEN
        from providers import get_provider_profile
        from hermes_cli.inventory import load_picker_context,build_model_options_payload
        profile=get_provider_profile('claude-native-bridge')
        assert profile.auth_type=='api_key'
        for explicit in (False,True):
            row=next(r for r in build_model_options_payload(load_picker_context(),explicit_only=explicit)['providers'] if r['slug']==profile.name)
            assert row['models']==MODELS,row
        assert '/v1/models' in requests,requests
        from tui_gateway import server
        before=cfg.read_bytes()
        session={'agent':None,'running':False,'session_key':'picker-smoke'}
        server._sessions['picker-smoke']=session
        for model in MODELS:
            response=server.handle_request({'id':model,'method':'config.set','params':{
                'session_id':'picker-smoke','key':'model',
                'value':f'{model} --provider {profile.name} --session','confirm_expensive_model':True}})
            assert 'error' not in response,response
            assert session['model_override']['provider']==profile.name
            assert session['model_override']['model']==model
            assert session['model_override']['base_url']==profile.base_url
            assert session['model_override']['api_mode']=='chat_completions'
        assert cfg.read_bytes()==before,'Session picks changed defaults'
        assert not any(name.endswith('claude_native_bridge.native') for name in sys.modules)
        fixture.shutdown();fixture.server_close()
        print('HTTP discovery and all model selections passed without inference')
    """)
    core_root = Path(hermes_cli.inventory.__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=core_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
