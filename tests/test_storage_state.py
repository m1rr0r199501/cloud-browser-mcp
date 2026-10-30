"""State artifacts and scope/restore semantics; real cloud runs are separate."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from cbmcp.errors import InvalidArgument, ScopeViolation
from cbmcp.scope import ScopeRule
from cbmcp.storage_state import export_state, filter_state, import_state, normalise_origins, restore_state


A = "https://a.sso-fixture.invalid"
B = "https://b.sso-fixture.invalid"


def cookie(name="sso", domain=".sso-fixture.invalid"):
    return {"name": name, "value": "SYNTHETIC_SECRET", "domain": domain, "path": "/",
        "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}


def state():
    return {"cookies": [cookie(), cookie("host-only", "a.sso-fixture.invalid")],
        "origins": [{"origin": A, "localStorage": [{"name": "app-a", "value": "A_SECRET"}]},
            {"origin": B, "localStorage": [{"name": "app-b", "value": "B_SECRET"}]}]}


class Context:
    def __init__(self, value=None):
        self.value = copy.deepcopy(value or {"cookies": [], "origins": []})
        self.pages = [SimpleNamespace(url="about:blank")]
        self.writes = 0
        self.reads = 0

    async def storage_state(self, **_options):
        self.reads += 1
        return copy.deepcopy(self.value)

    async def set_storage_state(self, value):
        self.writes += 1
        self.value = copy.deepcopy(value)
        self.value["cookies"].reverse()
        self.value["origins"].reverse()
        for item in self.value["origins"]:
            item["localStorage"].reverse()

    async def add_cookies(self, cookies):
        def key(item):
            return item['name'], item['domain'], item['path'], item.get('partitionKey')
        values = {key(item): item for item in self.value['cookies']}
        values.update({key(item): copy.deepcopy(item) for item in cookies})
        self.value['cookies'] = list(values.values())

    async def new_page(self):
        context = self
        class Page:
            url = 'about:blank'

            async def route(self, _pattern, handler):
                self.handler = handler

            async def goto(self, url, **_options):
                self.url = url

            async def evaluate(self, _expression, entries):
                origin = self.url.rstrip('/')
                item = next((item for item in context.value['origins'] if item['origin'] == origin), None)
                if item is None:
                    item = {'origin': origin, 'localStorage': []}
                    context.value['origins'].append(item)
                values = {entry['name']: entry['value'] for entry in item['localStorage']}
                values.update({entry['name']: entry['value'] for entry in entries})
                item['localStorage'] = [{'name': name, 'value': value} for name, value in values.items()]

            async def close(self):
                context.pages.remove(self)
        page = Page()
        self.pages.append(page)
        return page


def session(tmp_path, value=None, **extra):
    return SimpleNamespace(context=Context(value), dir=tmp_path, lease_id="test-lease",
        scope=ScopeRule(mode="enforce", allow=("*.sso-fixture.invalid",)), recon=None, **extra)


async def test_private_scoped_export_has_real_hash_and_no_secret_in_receipt(tmp_path):
    source = session(tmp_path, state())
    result = await export_state(source, [A])
    path = tmp_path / "auth-state" / result["path"].split("/")[-1]
    raw = path.read_bytes()
    payload = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == result["content_hash"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert payload["storage_state"]["cookies"][0]["httpOnly"]
    assert [row["origin"] for row in payload["storage_state"]["origins"]] == [A]
    assert "SYNTHETIC_SECRET" not in json.dumps(result) and "A_SECRET" not in json.dumps(result)
    assert result["backend_identity_verified_by_tool"] is False


async def test_fresh_restore_roundtrip_accepts_browser_order_changes(tmp_path):
    exported = await export_state(session(tmp_path / "source", state()), [A, B])
    target = session(tmp_path / "target")
    result = await restore_state(target, exported["path"], exported["content_hash"])
    assert result["cookie_count"] == 2 and result["storage_origin_count"] == 2
    assert target.context.writes == 1
    assert result["backend_identity_verified_by_tool"] is False


async def test_sibling_restore_uses_parent_cookie_but_not_host_only_cookie_or_other_origin_storage(tmp_path):
    exported = await export_state(session(tmp_path / "source", state()), [A])
    target = session(tmp_path / "target")
    result = await restore_state(target, exported["path"], exported["content_hash"], [B])
    assert result["cookie_count"] == 1 and result["storage_origin_count"] == 0
    assert target.context.value["cookies"][0]["name"] == "sso"
    assert target.context.value["origins"] == []


async def test_existing_profile_state_is_not_overwritten(tmp_path):
    exported = await export_state(session(tmp_path / "source", state()), [A])
    target = session(tmp_path / "target", state())
    before = copy.deepcopy(target.context.value)
    with pytest.raises(InvalidArgument, match="existing profile state was left intact"):
        await restore_state(target, exported["path"], exported["content_hash"], [A])
    assert target.context.value == before and target.context.writes == 0


async def test_restore_before_business_navigation(tmp_path):
    exported = await export_state(session(tmp_path / "source", state()), [A])
    target = session(tmp_path / "target")
    target.context.pages[0].url = A
    with pytest.raises(InvalidArgument, match="before target navigation"):
        await restore_state(target, exported["path"], exported["content_hash"], [A])
    assert target.context.writes == 0


async def test_tampered_state_hash_rejected_before_browser_mutation(tmp_path):
    exported = await export_state(session(tmp_path / "source", state()), [A])
    target = session(tmp_path / "target")
    with pytest.raises(InvalidArgument, match="hash changed"):
        await restore_state(target, exported["path"], "0" * 64, [A])
    assert target.context.writes == 0 and target.context.reads == 0


async def test_scope_checked_for_export_and_restore(tmp_path):
    source = session(tmp_path / "source", state())
    with pytest.raises(ScopeViolation):
        await export_state(source, ["https://outside.invalid"])
    assert source.context.reads == 0
    exported = await export_state(source, [A])
    target = session(tmp_path / "target")
    with pytest.raises(ScopeViolation):
        await restore_state(target, exported["path"], exported["content_hash"], ["https://outside.invalid"])
    assert target.context.writes == 0


@pytest.mark.parametrize("origin", [A + "/", A + "/path", A + "?x=1", A + "#x",
    "https://user:pass@a.sso-fixture.invalid", "file:///tmp/x", A + "\n", "https://a.sso-fixture.invalid:0"])
def test_origins_are_actual_origins(origin):
    with pytest.raises(InvalidArgument):
        normalise_origins([origin], ScopeRule())


def test_default_ports_canonicalise_without_losing_origin_boundary():
    assert normalise_origins([A + ":443", A, "http://a.sso-fixture.invalid:80"], ScopeRule()) == [A, "http://a.sso-fixture.invalid"]


def test_ipv6_cookie_and_origin_are_not_lost_to_bracket_representation():
    value = {"cookies": [cookie(domain="[::1]")], "origins": []}
    origins = normalise_origins(["http://[::1]:80"], ScopeRule())
    assert filter_state(value, origins)["cookies"] == value["cookies"]


@pytest.mark.parametrize("origin", ["https://*.sso-fixture.invalid", "https://a%2Esso-fixture.invalid"])
def test_origin_is_not_a_pattern_or_encoded_host(origin):
    with pytest.raises(InvalidArgument):
        normalise_origins([origin], ScopeRule())


async def test_plain_playwright_state_import_is_supported_without_claiming_source_authentication(tmp_path):
    path = tmp_path / "plain.json"
    raw = json.dumps(state()).encode()
    path.write_bytes(raw)
    target = session(tmp_path / "target")
    result = await restore_state(target, str(path), hashlib.sha256(raw).hexdigest(), [A])
    assert result["source_format"] == "playwright"
    assert result["client_mock_installed_in_source"] is None


async def test_mock_origin_is_preserved_as_provenance_not_verified_login(tmp_path):
    source = session(tmp_path / "source", state())
    source.recon = object()
    exported = await export_state(source, [A])
    result = await restore_state(session(tmp_path / "target"), exported["path"], exported["content_hash"])
    assert exported["client_mock_installed"] and result["client_mock_installed_in_source"]
    assert not result["backend_identity_verified_by_tool"]


async def test_symlink_and_malformed_state_rejected_without_content_leak(tmp_path):
    path = tmp_path / "malformed.json"
    raw = b'{"password":"DO_NOT_ECHO"'
    path.write_bytes(raw)
    link = tmp_path / "symlink.json"
    link.symlink_to(path)
    for candidate in [path, link]:
        with pytest.raises(InvalidArgument) as error:
            await restore_state(session(tmp_path / "target"), str(candidate), hashlib.sha256(raw).hexdigest(), [A])
        assert "DO_NOT_ECHO" not in str(error.value)


def test_additional_storage_components_are_not_silently_discarded():
    value = state()
    value["origins"][0]["indexedDB"] = [{"name": "not-exported"}]
    with pytest.raises(InvalidArgument, match="additional storage components"):
        filter_state(value, [A])


async def test_native_tool_available_without_javascript_debugger_group(settings):
    from mcp.server.mcpserver import MCPServer
    from cbmcp.tools import register_all

    settings.enable_debugger_tools = False
    server = MCPServer("test")
    register_all(server, settings)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "browser_state" in tools and "js_recon" not in tools
    assert "expected_hash" in tools["browser_state"].input_schema["properties"]


async def test_sso_import_merges_current_profile_after_navigation_and_preserves_other_state(tmp_path):
    exported = await export_state(session(tmp_path/'source', state()), [A])
    existing = {'cookies': [cookie('unrelated', 'b.sso-fixture.invalid'),
        {**cookie(), 'value': 'OLD_SSO'}], 'origins': [
        {'origin': B, 'localStorage': [{'name': 'app-b', 'value': 'KEEP_B'}]},
        {'origin': A, 'localStorage': [{'name': 'keep-local', 'value': 'KEEP_A'}]}]}
    target = session(tmp_path/'target', existing)
    original_page = target.context.pages[0]
    original_page.url = B + '/already-testing'
    result = await import_state(target, exported['path'], exported['content_hash'], [A])
    assert target.context.writes == 0
    assert target.context.pages == [original_page] and original_page.url == B + '/already-testing'
    assert result['profile_switched'] is False and not result['backend_identity_verified_by_tool']
    assert next(item for item in target.context.value['cookies'] if item['name']=='unrelated') == existing['cookies'][0]
    assert next(item for item in target.context.value['cookies'] if item['name']=='sso')['value'] == 'SYNTHETIC_SECRET'
    assert next(item for item in target.context.value['origins'] if item['origin']==B) == existing['origins'][0]
    a_values = next(item for item in target.context.value['origins'] if item['origin']==A)['localStorage']
    assert {'name':'keep-local','value':'KEEP_A'} in a_values
    assert {'name':'app-a','value':'A_SECRET'} in a_values


async def test_sso_import_does_not_copy_other_application_storage(tmp_path):
    exported = await export_state(session(tmp_path/'source', state()), [A, B])
    target = session(tmp_path/'target')
    await import_state(target, exported['path'], exported['content_hash'], [B])
    assert [item['name'] for item in target.context.value['cookies']] == ['sso']
    assert [item['origin'] for item in target.context.value['origins']] == [B]


async def test_sso_import_rejects_wrong_hash_out_of_scope_and_mock_without_mutation(tmp_path):
    source = session(tmp_path/'source', state())
    exported = await export_state(source, [A])
    target = session(tmp_path/'target')
    for origins, digest in [([A], '0'*64), (['https://outside.invalid'], exported['content_hash']),
            ([A, B], exported['content_hash'])]:
        with pytest.raises((InvalidArgument, ScopeViolation)):
            await import_state(target, exported['path'], digest, origins)
    source.recon = object()
    mocked = await export_state(source, [A])
    with pytest.raises(InvalidArgument, match='mock'):
        await import_state(target, mocked['path'], mocked['content_hash'], [A])
    assert target.context.value == {'cookies': [], 'origins': []}


async def test_sso_account_labels_are_reported_for_agent_judgment_without_an_import_gate(tmp_path):
    exported=await export_state(session(tmp_path/'source',state()),[A],account_key='user_a')
    target=session(tmp_path/'target')
    described=await import_state(target,exported['path'],exported['content_hash'],[A],account_key='user_b')
    assert described['account_key']=='user_b' and described['source_account_key']=='user_a'
    assert target.context.value['cookies'] and not described['backend_identity_verified_by_tool']
    result=await import_state(target,exported['path'],exported['content_hash'],[A],account_key='user_a')
    assert result['account_key']=='user_a' and not result['profile_switched']
